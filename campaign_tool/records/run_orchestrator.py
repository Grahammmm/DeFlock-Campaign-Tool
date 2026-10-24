"""Stage orchestrator for one records run (plan T11; stdlib only).

The host wrapper ``deploy/records-runtime/run-records.sh`` takes the overlap
lock, waits for Docker and then calls ``run`` here. This module runs the
seven work stages from a per-target stage plan and writes a run report v1
(``schemas/records-run-report-v1.json``). The report starts with the notify
stage pending (``skipped``/``NOTIFY_PENDING``). The notify helper or the
``notify-fallback`` command fills it in afterwards.

Stages are pluggable commands. A stage plan entry gives an argv, which is
usually ``docker exec`` into the records container. A stage reports
back through its exit status and a single JSON line on stdout, as defined by
``schemas/records-stage-result-v1.json``. Stage stderr is discarded and stdout
is only parsed. Neither is copied into the report or log, so record text,
paths or URLs a stage might print cannot reach the report.

``stub-stage`` is a synthetic backend for TEST targets and the fault-injection
harness. Every stub result carries the ``STUB_STAGE_BACKEND`` needs-G item,
so a stub run can never be mistaken for real coverage. Live targets refuse it.

Phase 1 (G, 2026-10-19) runs without OCR. The extract stage handles native
text only. Documents that need OCR are queued as ``awaiting_ocr`` and never
dropped. The stage reports them in ``counts.skipped``, in an info needs-G item
``AWAITING_OCR`` with the document count, and in the stage's optional
``outcomes`` list. That list has one outcome code per document considered, and
its counts sum to ``counts.input``.

Run exit codes: 0 when every work stage is ok/warn/skipped, otherwise
``10 + index`` of the first failed work stage (sync=10 ... outputs=16).
70 means an internal error and 78 an invalid target config. A notify outcome
never changes the exit code. Machine-valid reports are review-required, not
semantic acceptance of the run.
"""

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from campaign_tool import law
from campaign_tool.records import run_report

WORK_STAGES = run_report.STAGE_ORDER[:-1]
STAGE_EXIT_BASE = 10
EXIT_INTERNAL = 70
EXIT_CONFIG = 78
EXIT_NOT_NEEDED = 3
MAX_STAGE_TIMEOUT = 2700
RESULT_TAIL_BYTES = 65536
KILL_GRACE_SECONDS = 2
CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
TARGET_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
RUN_ID_RE = re.compile(r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
ARGV_TOKENS = ("{run_id}", "{target}")
STAGE_RESULT_SCHEMA = run_report.REPO_ROOT / "schemas" / "records-stage-result-v1.json"
STUB_CODE = "STUB_STAGE_BACKEND"
AWAITING_OCR_CODE = "AWAITING_OCR"
NEW_DATA_KEYS = ("documents", "pages", "extracted_units", "portal_releases",
                 "request_status_changes")


class TargetConfigError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stage_exit_code(name):
    return STAGE_EXIT_BASE + WORK_STAGES.index(name)


def zero_counts():
    return {"input": 0, "output": 0, "failed": 0, "skipped": 0}


# --- target config ----------------------------------------------------------

def load_target_config(path):
    """Load and strictly validate a per-target stage plan; raise TargetConfigError."""
    from .portal.policy import strict_json
    try:
        config = strict_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TargetConfigError(f"target config unreadable: {type(exc).__name__}") from None
    return validate_target_config(config)


def validate_target_config(config):
    def fail(message):
        raise TargetConfigError(message)

    if not isinstance(config, dict):
        fail("$: expected object")
    allowed = {"target_config_version", "target", "mode", "stages"}
    if set(config)-{'portal'} != allowed:
        fail(f"$: expected exactly fields {sorted(allowed)}, got {sorted(config)}")
    from .portal.policy import PortalError, fetch_settings
    try:
        fetch_settings(config)
    except PortalError:
        fail('$.portal.fetch: invalid settings')
    if config["target_config_version"] != 1:
        fail("$.target_config_version: expected 1")
    if not isinstance(config["target"], str) or not TARGET_RE.match(config["target"]):
        fail("$.target: expected upper-case name like TEST or LIVE")
    if config["mode"] not in ("test", "live"):
        fail("$.mode: expected test or live")
    stages = config["stages"]
    if not isinstance(stages, list):
        fail("$.stages: expected list")
    names = tuple(stage.get("name") if isinstance(stage, dict) else None for stage in stages)
    if names != WORK_STAGES:
        fail(f"$.stages: expected work stages {list(WORK_STAGES)} in order, got {list(names)}")
    for index, stage in enumerate(stages):
        where = f"$.stages[{index}]"
        extra = set(stage) - {"name", "enabled", "argv", "timeout_seconds", "on_fail"}
        if extra:
            fail(f"{where}: unexpected field(s) {sorted(extra)}")
        enabled = stage.get("enabled", True)
        if not isinstance(enabled, bool):
            fail(f"{where}.enabled: expected boolean")
        if stage.get("on_fail", "stop") not in ("stop", "continue"):
            fail(f"{where}.on_fail: expected stop or continue")
        if not enabled:
            continue
        argv = stage.get("argv")
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(arg, str) and arg and "\x00" not in arg for arg in argv)):
            fail(f"{where}.argv: expected non-empty list of non-empty strings")
        if not os.path.isabs(argv[0]):
            fail(f"{where}.argv[0]: expected an absolute executable path")
        timeout = stage.get("timeout_seconds")
        if (not isinstance(timeout, int) or isinstance(timeout, bool)
                or not 1 <= timeout <= MAX_STAGE_TIMEOUT):
            fail(f"{where}.timeout_seconds: expected integer 1..{MAX_STAGE_TIMEOUT}")
        if config["mode"] == "live" and "stub-stage" in argv:
            fail(f"{where}.argv: the stub stage backend is refused in a live target")
        stage_argv(config,stage)
    return config


def stage_argv(config, entry):
    """Bridge plan settings to the existing stage CLI without changing dry argv."""
    from .portal.policy import fetch_settings
    argv=list(entry['argv'])
    if entry['name']!='portal':
        return argv
    reserved=('--portal-download','--portal-max-items','--portal-total-bytes')
    if any(arg.startswith('--') and any(flag.startswith(arg.split('=',1)[0]) for flag in reserved) for arg in argv):
        raise TargetConfigError('portal fetch flags must come from plan.portal.fetch')
    settings=fetch_settings(config)
    if settings.apply:
        if argv.count('campaign_tool.records.run_stages')!=1 or '--approval-file' not in argv or '--portal-egress-file' not in argv:
            raise TargetConfigError('portal fetch requires the existing stage and both policies')
        argv+=['--portal-download','--portal-max-items',str(settings.max_items),
               '--portal-total-bytes',str(settings.max_bytes_total)]
    return argv


def expand_argv(argv, run_id, target):
    values = {"{run_id}": run_id, "{target}": target}
    out = []
    for arg in argv:
        for token in ARGV_TOKENS:
            arg = arg.replace(token, values[token])
        out.append(arg)
    return out


# --- stage results ----------------------------------------------------------

def load_stage_result_schema():
    return json.loads(STAGE_RESULT_SCHEMA.read_text(encoding="utf-8"))


def parse_stage_result(raw, schema):
    """Return a validated stage result dict from captured stdout, or None."""
    lines = [line for line in raw.decode("utf-8", "replace").splitlines() if line.strip()]
    if not lines:
        return None
    try:
        result = json.loads(lines[-1])
        law.validate(result, schema)
    except ValueError:
        return None
    if result["status"] in ("warn", "fail") and result.get("error_code") is None:
        return None
    if result["status"] == "ok" and result.get("error_code") is not None:
        return None
    seen = set()
    for item in result.get("needs_g", []):
        if item["code"] in seen:
            return None
        seen.add(item["code"])
    if "outcomes" in result:
        try:
            run_report.check_outcomes(result["outcomes"], result["counts"])
        except ValueError:
            return None
    return result


def _group_alive(pgid):
    """True while any process (zombies included) is still in process group pgid."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_gone(proc, grace):
    """Reap the leader and wait up to grace seconds for the whole group to empty.

    Reaping the leader is not enough: a same-group descendant (for example
    one that ignores SIGTERM) keeps the group alive after the leader exits.
    """
    deadline = time.monotonic() + grace
    while True:
        leader_done = proc.poll() is not None
        if leader_done and not _group_alive(proc.pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _kill_group(proc, grace=KILL_GRACE_SECONDS):
    """Terminate the stage's whole process group, bounded.

    SIGTERM, then up to grace seconds for every group member to exit, then
    SIGKILL to whatever remains even if the leader has already been reaped.
    The stage was started with start_new_session=True, so its pgid is its pid
    and the group stays referenced (cannot be reused) while any member lives.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        if _group_gone(proc, grace):
            return
    # SIGKILL cannot be ignored; anything left is a zombie awaiting its
    # (re)parent. Make sure our own leader is reaped either way.
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


LINGER_SETTLE_SECONDS = 0.2


def _reap_lingering(proc):
    """After a normal leader exit, kill any stage descendants left behind.

    Returns True when the group still had members after a short settle
    period (a stage contract violation: work must not outlive the stage).
    """
    if _group_gone(proc, LINGER_SETTLE_SECONDS):
        return False
    _kill_group(proc)
    return True


class Interrupted(Exception):
    pass


# SIGTERM/SIGINT handling: always remember the signal; raise only while a
# stage process is being waited on, so the stage group is killed and the
# report is still written.
_SIGNAL_STATE = {"interrupted": False, "in_stage": False}


def _on_signal(signum, frame):
    _SIGNAL_STATE["interrupted"] = True
    if _SIGNAL_STATE["in_stage"]:
        raise Interrupted()


def run_stage(argv, timeout, schema, work_dir, env):
    """Run one stage command; return (status, counts, error_code, result, seconds)."""
    started = time.monotonic()
    proc = None
    with tempfile.TemporaryFile(dir=work_dir) as out:
        try:
            if _SIGNAL_STATE["interrupted"]:
                raise Interrupted()
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=subprocess.DEVNULL, env=env,
                                        start_new_session=True, close_fds=True)
            except OSError:
                return ("fail", zero_counts(), "STAGE_SPAWN_FAILED", None,
                        time.monotonic() - started)
            # Signals raise Interrupted only while waiting on the stage; one
            # that arrived during spawn is caught by the flag check.
            _SIGNAL_STATE["in_stage"] = True
            if _SIGNAL_STATE["interrupted"]:
                raise Interrupted()
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Remember, never raise, a signal while the group is killed.
                _SIGNAL_STATE["in_stage"] = False
                _kill_group(proc)
                return "fail", zero_counts(), "STAGE_TIMEOUT", None, time.monotonic() - started
            _SIGNAL_STATE["in_stage"] = False
            lingered = _reap_lingering(proc)
        except Interrupted:
            # Interrupted anywhere after spawn (including just after the
            # leader exited): no stage descendant may outlive the run.
            _SIGNAL_STATE["in_stage"] = False
            if proc is not None:
                _kill_group(proc)
            raise
        finally:
            _SIGNAL_STATE["in_stage"] = False
        if lingered:
            return ("fail", zero_counts(), "STAGE_LEFT_PROCESSES", None,
                    time.monotonic() - started)
        size = out.seek(0, os.SEEK_END)
        out.seek(max(0, size - RESULT_TAIL_BYTES))
        result = parse_stage_result(out.read(), schema)
    seconds = time.monotonic() - started
    counts = dict(result["counts"]) if result else zero_counts()
    if rc != 0:
        if result and result["status"] == "fail":
            return "fail", counts, result["error_code"], result, seconds
        code = f"STAGE_EXIT_{rc}" if rc > 0 else f"STAGE_SIGNAL_{-rc}"
        return "fail", counts, code, result, seconds
    if result is None:
        return "fail", zero_counts(), "STAGE_RESULT_INVALID", None, seconds
    return result["status"], counts, result.get("error_code"), result, seconds


# --- report -----------------------------------------------------------------

def stage_entry(name, status, counts=None, seconds=None, code=None):
    return {"name": name, "status": status, "counts": counts or zero_counts(),
            "duration_seconds": None if seconds is None else round(max(seconds, 0.0), 1),
            "error_code": code}


def pending_notify():
    return stage_entry("notify", "skipped", code="NOTIFY_PENDING")


def roll_up(stages):
    statuses = {stage["status"] for stage in stages}
    return "fail" if "fail" in statuses else "warn" if "warn" in statuses else "ok"


def _elapsed(started_at, finished_at):
    parse = run_report._parse_utc
    return int((parse(finished_at) - parse(started_at)).total_seconds())


def build_report(run_id, mode, started_at, stages, new_data, review_required, needs_g):
    finished_at = utc_now()
    report = {
        "schema_version": 1,
        "report_kind": "flock-records-run-report",
        "run_id": run_id,
        "mode": mode,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _elapsed(started_at, finished_at),
        "overall_status": roll_up(stages),
        "stages": stages,
        "new_data": {key: new_data.get(key, 0) for key in NEW_DATA_KEYS},
        "review_required_count": review_required,
        "notify_failed": False,
        "needs_g": needs_g,
    }
    run_report.validate_run_report(report)
    return report


def atomic_write_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def report_mode(config, scheduled):
    if config["mode"] == "test":
        return "test"
    return "scheduled" if scheduled else "manual"


def first_failed_exit(stages):
    for stage in stages:
        if stage["name"] in WORK_STAGES and stage["status"] == "fail":
            return stage_exit_code(stage["name"])
    return 0


def execute(config, run_id, mode, started_at, deadline_epoch, report_path, work_dir,
            env=None):
    """Run every work stage and write the report. Return the run exit code."""
    schema = load_stage_result_schema()
    env = dict(os.environ if env is None else env)
    env.update({"RECORDS_RUN_ID": run_id, "RECORDS_RUN_TARGET": config["target"]})
    stages, new_data, needs_g = [], {}, {}
    review_required = 0
    blocked_by = None
    for entry in config["stages"]:
        name = entry["name"]
        if blocked_by is not None:
            stages.append(stage_entry(name, "skipped", code=blocked_by))
            continue
        if not entry.get("enabled", True):
            stages.append(stage_entry(name, "skipped", code="STAGE_DISABLED"))
            continue
        remaining = int(deadline_epoch - time.time())
        if remaining < 1:
            stages.append(stage_entry(name, "fail", seconds=0, code="RUN_DEADLINE_REACHED"))
            blocked_by = "UPSTREAM_FAILED"
            continue
        argv = expand_argv(stage_argv(config,entry), run_id, config["target"])
        try:
            status, counts, code, result, seconds = run_stage(
                argv, min(entry["timeout_seconds"], remaining), schema, work_dir, env)
        except Interrupted:
            stages.append(stage_entry(name, "fail", seconds=0, code="RUN_INTERRUPTED"))
            blocked_by = "UPSTREAM_FAILED"
            continue
        if code == "STAGE_TIMEOUT" and entry["timeout_seconds"] > remaining:
            code = "RUN_DEADLINE_REACHED"
        stages.append(stage_entry(name, status, counts, seconds, code))
        if result and result.get("outcomes") and dict(result["counts"]) == counts:
            stages[-1]["outcomes"] = [dict(item) for item in result["outcomes"]]
        if result:
            for key, value in result.get("new_data", {}).items():
                new_data[key] = new_data.get(key, 0) + value
            review_required += result.get("review_required", 0)
            for item in result.get("needs_g", []):
                key = (item["code"], name)
                if key in needs_g:
                    needs_g[key]["count"] += item["count"]
                else:
                    needs_g[key] = {"code": item["code"], "stage": name,
                                    "severity": item["severity"], "count": item["count"]}
        if status == "fail" and entry.get("on_fail", "stop") == "stop":
            blocked_by = "UPSTREAM_FAILED"
    stages.append(pending_notify())
    report = build_report(run_id, mode, started_at, stages, new_data, review_required,
                          list(needs_g.values()))
    atomic_write_json(report_path, report)
    return first_failed_exit(stages)


def abort_report(run_id, mode, started_at, reason, stage="sync"):
    """Report for a run stopped before or outside stage execution."""
    started_seconds = run_report._parse_utc(started_at).timestamp()
    stages = []
    for name in WORK_STAGES:
        if name == stage:
            stages.append(stage_entry(name, "fail", seconds=time.time() - started_seconds,
                                      code=reason))
        elif WORK_STAGES.index(name) < WORK_STAGES.index(stage):
            stages.append(stage_entry(name, "skipped", code="NOT_REACHED"))
        else:
            stages.append(stage_entry(name, "skipped", code="UPSTREAM_FAILED"))
    stages.append(pending_notify())
    needs_g = [{"code": reason, "stage": stage, "severity": "blocking", "count": 1}]
    return build_report(run_id, mode, started_at, stages, {}, 0, needs_g)


# --- notify outcome ---------------------------------------------------------

def notify_reason_code(reason):
    code = "NOTIFY_" + re.sub(r"[^A-Z0-9]+", "_", str(reason).upper()).strip("_")
    return code[:64] if CODE_RE.match(code[:64]) else "NOTIFY_FAILED"


def record_notify_outcome(report, sent, reason=None, seconds=None):
    """Fill the notify stage, notify_failed and timing; return the report."""
    if sent:
        entry = stage_entry("notify", "ok", {"input": 1, "output": 1, "failed": 0, "skipped": 0},
                            seconds or 0)
    else:
        entry = stage_entry("notify", "fail", {"input": 1, "output": 0, "failed": 1, "skipped": 0},
                            seconds or 0, notify_reason_code(reason))
    report["stages"][-1] = entry
    report["notify_failed"] = not sent
    report["overall_status"] = roll_up(report["stages"])
    report["finished_at"] = max(report["finished_at"], utc_now())
    report["duration_seconds"] = _elapsed(report["started_at"], report["finished_at"])
    run_report.validate_run_report(report)
    return report


def notify_pending(report):
    stage = report["stages"][-1]
    return stage["status"] == "skipped" and stage["error_code"] == "NOTIFY_PENDING"


def write_notify_alert(alerts_dir, report, reason, exit_code):
    alerts = Path(alerts_dir)
    alerts.mkdir(parents=True, exist_ok=True)
    alert = {"schema": "records-notify-alert-v1", "kind": "notify_failed",
             "run_id": report["run_id"], "run_overall_status": report["overall_status"],
             "run_exit_code": exit_code, "reason": reason, "at": utc_now()}
    atomic_write_json(alerts / f"notify-failed-{report['run_id']}.json", alert)


# --- local understanding alerts (explicit opt-in; never a transport) ---------


def _alert_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def understanding_identity(value):
    """Canonical content SHA256 used by callers to pin admitted metadata."""
    import hashlib
    return hashlib.sha256(_alert_json(value)).hexdigest()


def _alert_baseline(baseline):
    if (not isinstance(baseline, dict)
            or set(baseline) != {"schema_version", "needs_g"}
            or type(baseline["schema_version"]) is not int
            or baseline["schema_version"] != 1
            or not isinstance(baseline["needs_g"], list)):
        raise ValueError("invalid alert baseline")
    counts = {}
    for row in baseline["needs_g"]:
        if (not isinstance(row, dict) or set(row) != {"stage", "code", "count"}
                or row["stage"] not in run_report.STAGE_ORDER
                or not isinstance(row["code"], str) or not CODE_RE.fullmatch(row["code"])
                or type(row["count"]) is not int or row["count"] < 0):
            raise ValueError("invalid alert baseline")
        key = (row["stage"], row["code"])
        if key in counts:
            raise ValueError("duplicate alert baseline identity")
        counts[key] = row["count"]
    return counts


def _alert_report(report):
    try:
        run_report.validate_run_report(report)
        if type(report["schema_version"]) is not int or not RUN_ID_RE.fullmatch(report["run_id"]):
            raise ValueError()
        for stage in report["stages"]:
            if not all(type(n) is int for n in stage["counts"].values()):
                raise ValueError()
            if stage["error_code"] is not None and not CODE_RE.fullmatch(stage["error_code"]):
                raise ValueError()
            if any(type(row["count"]) is not int or not CODE_RE.fullmatch(row["code"])
                   for row in stage.get("outcomes", [])):
                raise ValueError()
        if any(type(n) is not int for n in report["new_data"].values()):
            raise ValueError()
        if type(report["review_required_count"]) is not int:
            raise ValueError()
        if any(type(row["count"]) is not int or not CODE_RE.fullmatch(row["code"])
               for row in report["needs_g"]):
            raise ValueError()
        # Also reject NaN/infinity even if a permissive schema accepts them.
        understanding_identity(report)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValueError("invalid alert report") from None


def _understanding_comparison(report, baseline, prior, max_age):
    """Pure comparison of validated, pinned report metadata; no I/O."""
    report_hash = understanding_identity(report)
    prior_hash = understanding_identity(prior) if prior is not None else None
    result = {"verification": "UNVERIFIED", "reason_code": "PRIOR_UNVERIFIED",
              "run_id": report["run_id"], "report_sha256": report_hash,
              "prior_sha256": prior_hash, "baseline_sha256": understanding_identity(baseline),
              "events": [], "review_required": True, "publication_ready": False,
              "activation_allowed": False, "sent": False}
    if prior is None:
        return result
    age = (run_report._parse_utc(report["started_at"])
           - run_report._parse_utc(prior["finished_at"])).total_seconds()
    if (prior["run_id"] == report["run_id"] or prior["mode"] != report["mode"]
            or age < 0 or age > max_age):
        return result
    result.update(verification="VERIFIED", reason_code="LOCAL_COMPARISON_ONLY")
    prior_stages = {s["name"]: s for s in prior["stages"]}
    previous = {(r["stage"], r["code"]): r["count"] for r in prior["needs_g"]}
    accepted = _alert_baseline(baseline)

    def event(reason, stage, code, count, prior_count, allowance):
        row = {"reason_code": reason, "stage": stage, "code": code,
               "count": count, "prior_count": prior_count, "baseline_count": allowance,
               "run_id": report["run_id"], "report_sha256": report_hash,
               "prior_run_id": prior["run_id"], "prior_sha256": prior_hash,
               "baseline_sha256": result["baseline_sha256"], "audience": "internal"}
        row["event_id"] = understanding_identity(row)
        result["events"].append(row)

    timeouts = {"STAGE_TIMEOUT", "RUN_DEADLINE_REACHED", "NOTIFY_HELPER_TIMEOUT"}
    for stage in report["stages"]:
        old = prior_stages[stage["name"]]
        if stage["status"] == "fail" and (
                old["status"] != "fail" or old["error_code"] != stage["error_code"]
                or stage["counts"]["failed"] > old["counts"]["failed"]):
            event("NEW_TIMEOUT" if stage["error_code"] in timeouts else "NEW_FAILURE",
                  stage["name"], stage["error_code"], stage["counts"]["failed"],
                  old["counts"]["failed"], 0)
    for row in sorted(report["needs_g"], key=lambda r: (r["stage"], r["code"])):
        key = (row["stage"], row["code"])
        old, allowance = previous.get(key, 0), accepted.get(key, 0)
        if row["count"] > max(old, allowance):
            event("GAP_WIDENED", *key, row["count"], old, allowance)
    return result


def _alert_directory(root):
    """Open an existing absolute directory, refusing every symlink component."""
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("alert root must be an existing absolute directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(root.anchor, flags)
    try:
        for component in root.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _alert_regular(fd):
    import stat
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("alert leaf must be a single-link regular file")


def _alert_load(fd):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate alert state key")
            obj[key] = value
        return obj

    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=unique)


def _alert_validate_state(state, baseline):
    if (not isinstance(state, dict) or set(state) != {"schema_version", "baseline", "entries"}
            or type(state["schema_version"]) is not int or state["schema_version"] != 1
            or _alert_json(state["baseline"]) != _alert_json(baseline)
            or not isinstance(state["entries"], list)):
        raise ValueError("invalid alert state")
    seen, last = set(), None
    for entry in state["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
                "report", "prior_report", "max_prior_age_seconds", "result"}:
            raise ValueError("invalid alert entry")
        report, prior, age = entry["report"], entry["prior_report"], entry["max_prior_age_seconds"]
        _alert_report(report)
        _alert_report(prior)
        if last is None:
            seen.add(prior["run_id"])
        if type(age) is not int or age < 0 or report["run_id"] in seen:
            raise ValueError("invalid alert history")
        if last is not None and _alert_json(prior) != _alert_json(last):
            raise ValueError("broken alert history")
        expected = _understanding_comparison(report, baseline, prior, age)
        if (expected["verification"] != "VERIFIED"
                or _alert_json(entry["result"]) != _alert_json(expected)):
            raise ValueError("invalid alert evidence")
        seen.add(report["run_id"])
        last = report


def record_understanding_alerts(report, baseline, ledger_root, *, baseline_sha256,
                                prior_report=None, prior_sha256=None,
                                max_prior_age_seconds):
    """Persist new failure/timeout/gap evidence locally, without sending anything.

    Caller supplies an admitted output directory and canonical baseline/prior
    pins (``understanding_identity``). No baseline, root, age policy or transport
    is inferred from environment, credentials or installed runtime defaults.
    Missing/stale prior evidence is UNVERIFIED, not a successful empty diff.
    Existing notify outcome and report objects are never changed. This adapter
    does not satisfy notify delivery, semantic acceptance or live-apply gates.
    """
    import fcntl
    # Snapshot mutable caller inputs before validation or fingerprinting.
    report = json.loads(_alert_json(report))
    baseline = json.loads(_alert_json(baseline))
    prior_report = json.loads(_alert_json(prior_report))
    _alert_report(report)
    _alert_baseline(baseline)
    if baseline_sha256 != understanding_identity(baseline):
        raise ValueError("alert baseline pin mismatch")
    if type(max_prior_age_seconds) is not int or max_prior_age_seconds < 0:
        raise ValueError("invalid prior age policy")
    if prior_report is not None:
        _alert_report(prior_report)
        if prior_sha256 != understanding_identity(prior_report):
            prior_report = None
    result = _understanding_comparison(report, baseline, prior_report, max_prior_age_seconds)
    root_fd = _alert_directory(ledger_root)
    lock_fd = None
    try:
        lock_fd = os.open("understanding-alerts.lock", os.O_RDWR | os.O_CREAT
                          | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=root_fd)
        _alert_regular(lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        name = "understanding-alerts-v1.json"
        try:
            state_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                               | os.O_CLOEXEC, dir_fd=root_fd)
        except FileNotFoundError:
            state = {"schema_version": 1, "baseline": baseline, "entries": []}
        else:
            try:
                _alert_regular(state_fd)
            except BaseException:
                os.close(state_fd)
                raise
            state = _alert_load(state_fd)
        _alert_validate_state(state, baseline)
        if state["entries"] and report["run_id"] == state["entries"][0]["prior_report"]["run_id"]:
            raise ValueError("alert seed identity conflict")
        for entry in state["entries"]:
            if entry["report"]["run_id"] == report["run_id"]:
                if _alert_json(entry["report"]) != _alert_json(report):
                    raise ValueError("alert run identity conflict")
                replay = dict(entry["result"], events=[], reason_code="ALREADY_RECORDED")
                return replay
        if (state["entries"] and _alert_json(prior_report)
                != _alert_json(state["entries"][-1]["report"])):
            return dict(result, verification="UNVERIFIED", reason_code="PRIOR_UNVERIFIED", events=[])
        if result["verification"] != "VERIFIED":
            return result
        state["entries"].append({"report": report, "prior_report": prior_report,
                                 "max_prior_age_seconds": max_prior_age_seconds, "result": result})
        # Unique temp files are retained if a write fails; never truncate state.
        temp_name = ".understanding-alerts-" + os.urandom(16).hex() + ".tmp"
        temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                          | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=root_fd)
        with os.fdopen(temp_fd, "wb") as stream:
            stream.write(_alert_json(state) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
        return result
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(root_fd)


# --- stub backend -----------------------------------------------------------

def stub_stage(stage, fixture_path):
    """Synthetic stage backend. Return the process exit code."""
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    if fixture.get("stub_fixture_version") != 1:
        print("stub fixture: expected stub_fixture_version 1", file=sys.stderr)
        return 2
    spec = fixture.get("stages", {}).get(stage, {})
    time.sleep(float(spec.get("sleep_seconds", 0)))
    emit = spec.get("emit", "result")
    if emit == "invalid":
        print("not a stage result")
    elif emit == "result":
        result = {"stage_result_version": 1, "status": "ok", "counts": zero_counts(),
                  "error_code": None}
        result.update(spec.get("result", {}))
        needs_g = [item for item in result.get("needs_g", []) if item.get("code") != STUB_CODE]
        result["needs_g"] = needs_g + [{"code": STUB_CODE, "severity": "info", "count": 1}]
        print(json.dumps(result, sort_keys=True))
    return int(spec.get("exit_code", 0))


# --- command line -----------------------------------------------------------

def _check_run_id(run_id):
    if not RUN_ID_RE.match(run_id):
        raise SystemExit(f"run_orchestrator: invalid run id {run_id!r}")


def cmd_plan(args):
    config = load_target_config(args.target_config)
    total = len(run_report.STAGE_ORDER)
    print(f"plan target={config['target']} target_mode={config['mode']}")
    for index, entry in enumerate(config["stages"], start=1):
        name = entry["name"]
        if not entry.get("enabled", True):
            print(f"stage {index}/{total} {name}: disabled (reported skipped STAGE_DISABLED)")
            continue
        argv = expand_argv(entry["argv"], "{run_id}", config["target"])
        print(f"stage {index}/{total} {name}: exit-on-fail={stage_exit_code(name)} "
              f"timeout={entry['timeout_seconds']}s on_fail={entry.get('on_fail', 'stop')} "
              f"argv={shlex.join(argv)}")
    print(f"stage {total}/{total} notify: non-fatal; never changes the exit code")
    return 0


def cmd_check(args):
    load_target_config(args.target_config)
    return 0


def cmd_run(args):
    _check_run_id(args.run_id)
    run_report._parse_utc(args.started_at)
    config = load_target_config(args.target_config)
    if args.expect_target and args.expect_target != config["target"]:
        raise TargetConfigError("target config names a different target")
    mode = report_mode(config, args.scheduled)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    return execute(config, args.run_id, mode, args.started_at, args.deadline_epoch,
                   args.report, args.work_dir)


def cmd_abort(args):
    _check_run_id(args.run_id)
    if not CODE_RE.match(args.reason):
        raise SystemExit("run_orchestrator: invalid reason code")
    try:
        mode = report_mode(load_target_config(args.target_config), args.scheduled)
    except TargetConfigError:
        mode = "scheduled" if args.scheduled else "manual"
    report = abort_report(args.run_id, mode, args.started_at, args.reason, args.stage)
    atomic_write_json(args.report, report)
    return 0


def cmd_notify_fallback(args):
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    run_report.validate_run_report(report)
    if args.only_if_pending and not notify_pending(report):
        return EXIT_NOT_NEEDED
    record_notify_outcome(report, sent=False, reason=args.reason)
    atomic_write_json(args.report, report)
    write_notify_alert(args.alerts_dir, report, args.reason, args.exit_code)
    return 0


def cmd_stub_stage(args):
    return stub_stage(args.stage, args.fixture)


def build_parser():
    parser = argparse.ArgumentParser(prog="run_orchestrator",
                                     description="Stage orchestrator for one records run.")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="list every stage of a target (dry-run)")
    plan.add_argument("--target-config", required=True)
    plan.set_defaults(func=cmd_plan)

    check = sub.add_parser("check", help="validate a target config")
    check.add_argument("--target-config", required=True)
    check.set_defaults(func=cmd_check)

    run = sub.add_parser("run", help="run the work stages and write the report")
    run.add_argument("--target-config", required=True)
    run.add_argument("--expect-target")
    run.add_argument("--run-id", required=True)
    run.add_argument("--started-at", required=True)
    run.add_argument("--deadline-epoch", type=int, required=True)
    run.add_argument("--report", required=True)
    run.add_argument("--work-dir", required=True)
    run.add_argument("--scheduled", action="store_true")
    run.set_defaults(func=cmd_run)

    abort = sub.add_parser("abort", help="write a report for a run stopped before its stages")
    abort.add_argument("--run-id", required=True)
    abort.add_argument("--started-at", required=True)
    abort.add_argument("--target-config", required=True)
    abort.add_argument("--scheduled", action="store_true")
    abort.add_argument("--reason", required=True)
    abort.add_argument("--stage", choices=WORK_STAGES, default="sync")
    abort.add_argument("--report", required=True)
    abort.set_defaults(func=cmd_abort)

    fallback = sub.add_parser("notify-fallback", help="record a failed notify outcome")
    fallback.add_argument("--report", required=True)
    fallback.add_argument("--alerts-dir", required=True)
    fallback.add_argument("--reason", required=True,
                          choices=("helper_failed", "helper_timeout", "helper_no_outcome"))
    fallback.add_argument("--exit-code", type=int, required=True)
    fallback.add_argument("--only-if-pending", action="store_true")
    fallback.set_defaults(func=cmd_notify_fallback)

    stub = sub.add_parser("stub-stage", help="synthetic stage backend (TEST and harness only)")
    stub.add_argument("--stage", choices=WORK_STAGES, required=True)
    stub.add_argument("--fixture", required=True)
    stub.set_defaults(func=cmd_stub_stage)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TargetConfigError as exc:
        print(f"run_orchestrator: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except (OSError, ValueError) as exc:
        print(f"run_orchestrator: internal error ({type(exc).__name__})", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
