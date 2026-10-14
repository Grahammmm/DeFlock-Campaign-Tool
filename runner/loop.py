"""Poll loop: lease a job, dispatch by kind, post the result.

One iteration is one "agent loop, per run" step from the plan: lease
(`GET /api/runner/jobs?lease=300`), run the handler for `kind`, post
`{claim_token, status, outputs, receipt}`. Handlers return a `Result`; anything they
raise becomes `failed` with the exception class and a fixed message (never
record text). 5xx and connection errors back off exponentially up to
`MAX_BACKOFF` seconds; SIGTERM fences further mutations and stops renewal; `--once`
handles at most one job and exits with 0 (job done), 3 (nothing queued) or
1 (job failed/blocked).

Private scratch lives under RUNNER_WORKDIR and envelopes under RUNNER_RESULT_DIR,
with 0700 directories and 0600 files. Raw scratch is removed after a durable result is
saved; the separate result envelope remains until acknowledged. KEEP_WORKDIR=1
retains scratch for synthetic debugging.
"""
import copy
import fcntl
import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .client import FakeWorkspace, Workspace, WorkspaceError

MAX_BACKOFF = 300
RUNNER_KINDS = ("classify_mail", "extract", "digest", "draft_followup", "build_site", "send_request", "intake", "newsletter_draft", "backup")


@dataclass
class Result:
    status: str = "done"                 # done | failed | blocked
    outputs: dict = field(default_factory=dict)
    receipt: dict | None = None
    error: str | None = None


class Log:
    """Structured JSON lines on stderr. Values are identifiers and counts only."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr

    def __call__(self, event, **fields):
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
        for key, value in fields.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, sort_keys=True)[:400]
            elif isinstance(value, str):
                value = value[:400]
            record[key] = value
        self.stream.write(json.dumps(record, sort_keys=True) + "\n")
        self.stream.flush()


@dataclass
class Settings:
    workspace_url: str = ""
    runner_token: str = ""
    privacy_tier: str = "redacted_cloud"
    model_base_url: str = ""
    model_api_key: str = ""
    model_id: str = "local"
    poll_interval: float = 15.0
    lease_seconds: int = 300
    max_job_seconds: int = 3600
    workdir: Path = Path("/tmp/deflock-runner")
    result_dir: Path | None = None
    keep_workdir: bool = False
    jurisdictions_dir: Path | None = None
    redaction_allowlist: list = field(default_factory=list)
    redaction_denylist: list = field(default_factory=list)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        # redacted_cloud is the default: records are held locally and only redacted text
        # reaches the model (docs/RUNNER.md "Privacy tiers" lists what the redactor does
        # and does not catch; known names go in REDACTION_DENYLIST). strict_local keeps
        # everything on the box or the tailnet.
        tier = env.get("PRIVACY_TIER", "redacted_cloud")
        if tier not in ("redacted_cloud", "strict_local"):
            raise ValueError("PRIVACY_TIER must be redacted_cloud|strict_local")
        def csv(name):
            return [x.strip() for x in env.get(name, "").split(",") if x.strip()]
        jd = env.get("JURISDICTIONS_DIR")
        return cls(
            workspace_url=env.get("WORKSPACE_URL", ""),
            runner_token=env.get("RUNNER_TOKEN", ""),
            privacy_tier=tier,
            model_base_url=env.get("MODEL_BASE_URL", ""),
            model_api_key=env.get("MODEL_API_KEY", ""),
            model_id=env.get("MODEL_ID", "local"),
            poll_interval=float(env.get("POLL_INTERVAL", "15")),
            lease_seconds=int(env.get("LEASE_SECONDS", "300")),
            max_job_seconds=int(env.get("MAX_JOB_SECONDS", "3600")),
            workdir=Path(env.get("RUNNER_WORKDIR", "/tmp/deflock-runner")),
            result_dir=Path(env["RUNNER_RESULT_DIR"]) if env.get("RUNNER_RESULT_DIR") else None,
            keep_workdir=env.get("KEEP_WORKDIR", "") == "1",
            jurisdictions_dir=Path(jd) if jd else None,
            redaction_allowlist=csv("REDACTION_ALLOWLIST"),
            redaction_denylist=csv("REDACTION_DENYLIST"),
        )

    def model_config(self):
        from campaign_tool.digest.model import ModelConfig
        if not self.model_base_url:
            return None
        return ModelConfig(base_url=self.model_base_url, api_key=self.model_api_key, model_id=self.model_id,
                           privacy_tier=self.privacy_tier)


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def private_write(path, data):
    path = Path(path)
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8"))
    os.chmod(path, 0o600)
    return path


class Context:
    """What a handler gets: the workspace client, settings, a private job directory and a logger."""

    def __init__(self, workspace, settings, job, log):
        self.workspace = workspace
        self.settings = settings
        self.job = job
        self.log = log
        private_dir(settings.workdir)
        # Neither a previous attempt nor another runner may clean up our scratch.
        scratch = private_dir(Path(settings.workdir) / "scratch")
        self.dir = private_dir(tempfile.mkdtemp(prefix="job-", dir=scratch))

    @property
    def job_id(self):
        return self.job["job_id"]

    @property
    def privacy_tier(self):
        # The stricter of the campaign's tier and the operator's: a strict_local box never
        # labels its output redacted_cloud, and a strict_local campaign is honoured anywhere.
        job_tier = self.job.get("privacy_tier") or self.settings.privacy_tier
        return "strict_local" if "strict_local" in (job_tier, self.settings.privacy_tier) else job_tier

    def write(self, name, data):
        return private_write(self.dir / name, data)

    def cleanup(self):
        if not self.settings.keep_workdir:
            shutil.rmtree(self.dir, ignore_errors=True)


def default_handlers():
    from .handlers import build_site, classify_mail, digest, draft_followup, extract
    return {
        "classify_mail": classify_mail.run,
        "extract": extract.run,
        "digest": digest.run,
        "draft_followup": draft_followup.run,
        "build_site": build_site.run,
    }


def blocked_kind(ctx):
    return Result("blocked", {}, None, f"{ctx.job['kind']} is not a runner job; it runs in the workspace after approval")


class ClaimLost(WorkspaceError):
    def __init__(self, reason):
        super().__init__(409, reason)
        self.reason = reason


class ClaimLease:
    """Bounded, fail-closed heartbeat and guard for subsequent handler mutations.

    In-process computation cannot be killed safely here. Once ownership is lost,
    its later writes are refused; already in-flight non-queue writes cannot be
    rolled back. The Worker's atomic result transition is the final authority.
    """

    def __init__(self, workspace, settings, job, log, stopping=lambda: False,
                 monotonic=time.monotonic, wall_clock=time.time):
        self.workspace, self.settings, self.job, self.log = workspace, settings, job, log
        self.stopping, self.monotonic, self.wall_clock = stopping, monotonic, wall_clock
        self.deadline = monotonic() + settings.max_job_seconds
        self.lease_deadline = self._deadline(job.get("leased_until"))
        self.reason = None
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        if not isinstance(job.get("claim_token"), str) or not job["claim_token"]:
            raise ClaimLost("missing_claim_token")

    def _deadline(self, value):
        try:
            expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                raise ValueError("lease timestamp needs timezone")
            return self.monotonic() + max(0, expires.timestamp() - self.wall_clock())
        except (ValueError, AttributeError, TypeError):
            raise ClaimLost("invalid_lease_deadline") from None

    def check(self):
        if self.reason:
            raise ClaimLost(self.reason)
        if self.stopping():
            self.reason = "runner_stopping"
        elif self.monotonic() >= self.deadline:
            self.reason = "job_runtime_exhausted"
        elif self.monotonic() >= self.lease_deadline:
            self.reason = "lease_expired"
        if self.reason:
            self.closed.set()
            raise ClaimLost(self.reason)

    def renew(self):
        with self.lock:
            if self.closed.is_set():
                return False
            self.check()
            seconds = min(self.settings.lease_seconds, int(self.deadline - self.monotonic()))
            if seconds < 30:
                return False  # do not renew past the absolute runtime budget
            try:
                reply = self.workspace.renew_job(self.job["job_id"], self.job["claim_token"], seconds)
                self.check()
                if reply.get("claim_token") != self.job["claim_token"]:
                    raise ClaimLost("claim_mismatch")
                self.lease_deadline = min(self.deadline, self._deadline(reply.get("leased_until")))
                return not self.closed.is_set()
            except WorkspaceError:
                self.reason = self.reason or "lease_renewal_failed"
                self.closed.set()
                raise ClaimLost(self.reason) from None

    def _heartbeat(self):
        while not self.closed.is_set():
            delay = min(self.settings.lease_seconds / 3, self.deadline - self.monotonic(),
                        self.lease_deadline - self.monotonic())
            if self.closed.wait(max(0, delay)):
                return
            try:
                if not self.renew():
                    return
            except ClaimLost:
                return
            except Exception:
                self.reason = "lease_renewal_failed"
                self.closed.set()
                return

    def start(self):
        self.check()
        self.thread = threading.Thread(target=self._heartbeat, name="runner-lease", daemon=True)
        self.thread.start()

    def finish(self):
        self.reason = self.reason or "handler_finished"
        self.closed.set()
        # An already in-flight request has a ten-second transport timeout. It
        # cannot start another renewal after closed; do not block handler cleanup.


class ClaimedWorkspace:
    """Expose only handler operations, guarding each mutation with lease ownership."""
    MUTATIONS = frozenset({"put_original", "post_receipt", "post_correspondence", "propose", "put_site_file"})

    def __init__(self, workspace, claim):
        self._claim = claim
        if isinstance(workspace, Workspace):
            # Guard EACH actual transport attempt, including retries after a
            # timeout or 5xx. Keep the underlying opener/security policy intact.
            self._workspace = copy.copy(workspace)
            opener = workspace.opener
            def guarded_opener(*args, **kwargs):
                with claim.lock:
                    claim.check()
                return opener(*args, **kwargs)
            self._workspace.opener = guarded_opener
        else:
            self._workspace = workspace

    def __getattr__(self, name):
        if name == "get_original":
            self._claim.check()
            return self._workspace.get_original
        if name not in self.MUTATIONS:
            raise AttributeError(name)
        def mutate(*args, **kwargs):
            # Never hold the heartbeat lock over a potentially slow upload.
            with self._claim.lock:
                self._claim.check()
            return getattr(self._workspace, name)(*args, **kwargs)
        return mutate


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ResultOutbox:
    """Persist only the result request, never job inputs or downloaded originals."""

    def __init__(self, workspace, settings, log):
        self.workspace, self.settings, self.log = workspace, settings, log
        # Avoid sending a previous workspace/campaign's envelope after changing
        # configuration. A rotated token needs explicit local migration.
        scope = (str(getattr(workspace, "base", settings.workspace_url)) + "\0"
                 + getattr(workspace, "token", settings.runner_token))
        key = hashlib.sha256(scope.encode()).hexdigest()
        root = private_dir(settings.workdir)
        result_root = private_dir(settings.result_dir or root / "results")
        self.root = private_dir(result_root / key)
        _fsync_directory(result_root)
        _fsync_directory(result_root.parent)

    def save(self, job, result, ctx):
        envelope = {"version": 1, "job_id": job["job_id"], "claim_token": job["claim_token"],
                    "status": result.status, "outputs": result.outputs, "receipt": result.receipt,
                    "error": result.error, "scratch": ctx.dir.name}
        data = json.dumps(envelope, sort_keys=True, allow_nan=False).encode("utf-8")
        fd, name = tempfile.mkstemp(prefix="result-", suffix=".tmp", dir=self.root)
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            final = path.with_suffix(".json")
            os.replace(path, final)
            _fsync_directory(self.root)
            return final
        finally:
            path.unlink(missing_ok=True)

    def pending(self):
        return sorted(self.root.glob("result-*.json"))

    def deliver(self, path):
        # Multiple local runners can share a workdir. Exact replay is harmless,
        # but one process owns each delivery/cleanup while its HTTP call runs.
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            if not path.exists():
                return None
            envelope = json.load(stream)
            scratch = envelope.get("scratch", "")
            if (envelope.get("version") != 1 or not isinstance(scratch, str)
                    or not scratch.startswith("job-") or Path(scratch).name != scratch):
                raise ValueError("invalid result envelope")
            # Also recovers a crash just after fsync, before scratch cleanup.
            if not self.settings.keep_workdir:
                shutil.rmtree(Path(self.settings.workdir) / "scratch" / scratch, ignore_errors=True)
            try:
                reply = self.workspace.post_result(envelope["job_id"], envelope["status"],
                                                   envelope["outputs"], envelope["receipt"], envelope["error"],
                                                   claim_token=envelope["claim_token"])
            except WorkspaceError as exc:
                if exc.status == 409:
                    # Retain rejected evidence privately, but do not retry a
                    # terminal conflict forever or hold up unrelated jobs.
                    os.replace(path, path.with_suffix(".conflict"))
                    _fsync_directory(self.root)
                    self.log("runner.result_conflict", job_id=envelope["job_id"])
                raise
            if (not isinstance(reply, dict) or reply.get("job_id") != envelope["job_id"]
                    or reply.get("state") not in ("queued", "done", "failed", "blocked")):
                raise WorkspaceError(0, "invalid result acknowledgment", retryable=True)
            path.unlink(missing_ok=True)
            _fsync_directory(self.root)
            self.log("job.finish", job_id=envelope["job_id"], status=envelope["status"],
                     state=reply.get("state"), followups=len(reply.get("followups") or []))
            return envelope["status"]


def run_job(workspace, settings, job, log, handlers, *, stopping=lambda: False, outbox=None):
    claim = ClaimLease(workspace, settings, job, log, stopping=stopping)
    ctx = Context(ClaimedWorkspace(workspace, claim), settings, job, log)
    outbox = outbox or ResultOutbox(workspace, settings, log)
    kind = job.get("kind")
    log("job.start", job_id=ctx.job_id, kind=kind, attempt=job.get("attempt"))
    started = time.monotonic()
    cleanup = False
    try:
        claim.start()
        try:
            handler = handlers.get(kind)
            if handler is None:
                if kind == "send_request":
                    result = blocked_kind(ctx)
                else:
                    result = Result("blocked", {}, None, f"no handler for kind {kind!r} in this runner version")
            else:
                result = handler(ctx)
        except ClaimLost:
            raise
        except Exception as exc:  # Every handler failure becomes a job failure; never log its record-bearing message.
            message = f"{type(exc).__name__}: handler failed"
            log("job.exception", job_id=ctx.job_id, kind=kind, error=message,
                where=traceback.extract_tb(exc.__traceback__)[-1].name if exc.__traceback__ else "")
            result = Result("failed", {}, None, message)
        claim.check()
        result.outputs = dict(result.outputs or {})
        result.outputs.setdefault("runner", {"kind": kind, "seconds": round(time.monotonic() - started, 3)})
        path = outbox.save(job, result, ctx)
        cleanup = True
    except ClaimLost as exc:
        cleanup = True
        log("job.claim_lost", job_id=ctx.job_id, reason=exc.reason)
        return Result("failed", error=exc.reason)
    finally:
        claim.finish()
        if cleanup:
            ctx.cleanup()
    outbox.deliver(path)
    return result


class Runner:
    def __init__(self, workspace, settings, handlers=None, log=None, sleep=time.sleep):
        self.workspace = workspace
        self.settings = settings
        self.handlers = handlers if handlers is not None else default_handlers()
        self.log = log or Log()
        self.sleep = sleep
        self.stop = False
        self.backoff = 0.0
        if type(settings.lease_seconds) is not int or not 30 <= settings.lease_seconds <= 3600:
            raise ValueError("LEASE_SECONDS must be between 30 and 3600")
        if type(settings.max_job_seconds) is not int or not 30 <= settings.max_job_seconds <= 86400:
            raise ValueError("MAX_JOB_SECONDS must be between 30 and 86400")
        self.outbox = ResultOutbox(workspace, settings, self.log)

    def request_stop(self, *_):
        self.log("runner.stop_requested")
        self.stop = True

    def install_signals(self):
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

    def step(self):
        """One poll: returns 'done'|'failed'|'blocked' for a handled job, 'idle', or 'backoff'."""
        job = None
        try:
            pending = self.outbox.pending()
            if pending:
                for path in pending:
                    status = self.outbox.deliver(path)
                    if status:
                        self.backoff = 0.0
                        return status
                return "idle"  # another local process is delivering these results
            job = self.workspace.lease_job(min(self.settings.lease_seconds, self.settings.max_job_seconds))
        except WorkspaceError as exc:
            if exc.retryable:
                self.backoff = min(MAX_BACKOFF, max(self.settings.poll_interval, self.backoff * 2 or 1.0))
                self.log("runner.backoff", seconds=self.backoff, status=exc.status)
                return "backoff"
            if pending and exc.status == 409:
                self.log("runner.result_post_failed", status=exc.status)
                return "failed"
            raise
        self.backoff = 0.0
        if not job:
            return "idle"
        try:
            return run_job(self.workspace, self.settings, job, self.log, self.handlers,
                           stopping=lambda: self.stop, outbox=self.outbox).status
        except WorkspaceError as exc:
            self.log("runner.result_post_failed", job_id=job.get("job_id"), status=exc.status)
            if exc.retryable:
                self.backoff = min(MAX_BACKOFF, max(self.settings.poll_interval, self.backoff * 2 or 1.0))
                return "backoff"
            if exc.status == 409:
                return "failed"
            raise

    def run(self, once=False):
        private_dir(self.settings.workdir)
        self.log("runner.start", tier=self.settings.privacy_tier, model=bool(self.settings.model_base_url), once=once)
        while not self.stop:
            outcome = self.step()
            if once:
                return {"done": 0, "idle": 3, "backoff": 3}.get(outcome, 1)
            if outcome == "backoff":
                self.sleep(self.backoff)
            elif outcome == "idle":
                self.sleep(self.settings.poll_interval)
        self.log("runner.exit")
        return 0


def make_workspace(settings, fake=False):
    if fake:
        return FakeWorkspace(settings.privacy_tier)
    return Workspace(settings.workspace_url, settings.runner_token)
