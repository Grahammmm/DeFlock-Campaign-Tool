"""Poll loop: lease a job, dispatch by kind, post the result.

One iteration is one "agent loop, per run" step from the plan: lease
(`GET /api/runner/jobs?lease=300`), run the handler for `kind`, post
`{status, outputs, receipt}`. Handlers return a `Result`; anything they
raise becomes `failed` with the exception class and a short message (never
record text). 5xx and connection errors back off exponentially up to
`MAX_BACKOFF` seconds; SIGTERM finishes the current job and exits; `--once`
handles at most one job and exits with 0 (job done), 3 (nothing queued) or
1 (job failed/blocked).

Every private file the runner touches lives under RUNNER_WORKDIR with 0700
directories and 0600 files; the work directory for a job is removed when
the job finishes unless KEEP_WORKDIR=1.
"""
import json
import os
import shutil
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
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
    workdir: Path = Path("/tmp/deflock-runner")
    keep_workdir: bool = False
    jurisdictions_dir: Path | None = None
    redaction_allowlist: list = field(default_factory=list)
    redaction_denylist: list = field(default_factory=list)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
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
            workdir=Path(env.get("RUNNER_WORKDIR", "/tmp/deflock-runner")),
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
        self.dir = private_dir(Path(settings.workdir) / job["job_id"])

    @property
    def job_id(self):
        return self.job["job_id"]

    @property
    def privacy_tier(self):
        return self.job.get("privacy_tier") or self.settings.privacy_tier

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


def run_job(workspace, settings, job, log, handlers):
    ctx = Context(workspace, settings, job, log)
    kind = job.get("kind")
    log("job.start", job_id=ctx.job_id, kind=kind, attempt=job.get("attempt"))
    started = time.monotonic()
    try:
        handler = handlers.get(kind)
        if handler is None:
            if kind == "send_request":
                result = blocked_kind(ctx)
            else:
                result = Result("blocked", {}, None, f"no handler for kind {kind!r} in this runner version")
        else:
            result = handler(ctx)
    except Exception as exc:  # noqa: BLE001 - every handler failure becomes a job failure
        message = f"{type(exc).__name__}: {str(exc)[:300]}"
        log("job.exception", job_id=ctx.job_id, kind=kind, error=message,
            where=traceback.extract_tb(exc.__traceback__)[-1].name if exc.__traceback__ else "")
        result = Result("failed", {}, None, message)
    finally:
        ctx.cleanup()
    result.outputs = dict(result.outputs or {})
    result.outputs.setdefault("runner", {"kind": kind, "seconds": round(time.monotonic() - started, 3)})
    reply = workspace.post_result(ctx.job_id, result.status, result.outputs, result.receipt, result.error)
    log("job.finish", job_id=ctx.job_id, kind=kind, status=result.status, state=(reply or {}).get("state"),
        followups=len((reply or {}).get("followups") or []) if isinstance(reply, dict) else None)
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

    def request_stop(self, *_):
        self.log("runner.stop_requested")
        self.stop = True

    def install_signals(self):
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

    def step(self):
        """One poll: returns 'done'|'failed'|'blocked' for a handled job, 'idle', or 'backoff'."""
        try:
            job = self.workspace.lease_job(self.settings.lease_seconds)
        except WorkspaceError as exc:
            if exc.retryable:
                self.backoff = min(MAX_BACKOFF, max(self.settings.poll_interval, self.backoff * 2 or 1.0))
                self.log("runner.backoff", seconds=self.backoff, status=exc.status)
                return "backoff"
            raise
        self.backoff = 0.0
        if not job:
            return "idle"
        try:
            return run_job(self.workspace, self.settings, job, self.log, self.handlers).status
        except WorkspaceError as exc:
            self.log("runner.result_post_failed", job_id=job.get("job_id"), status=exc.status)
            if exc.retryable:
                self.backoff = min(MAX_BACKOFF, max(self.settings.poll_interval, self.backoff * 2 or 1.0))
                return "backoff"
            return "failed"

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
