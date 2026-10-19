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
dropped. The stage reports them in ``counts.skipped`` plus an info needs-G
item ``AWAITING_OCR`` with the document count, so the run report counts them
without a schema change.

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
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TargetConfigError(f"target config unreadable: {type(exc).__name__}") from None
    return validate_target_config(config)


def validate_target_config(config):
    def fail(message):
        raise TargetConfigError(message)

    if not isinstance(config, dict):
        fail("$: expected object")
    allowed = {"target_config_version", "target", "mode", "stages"}
    if set(config) != allowed:
        fail(f"$: expected exactly fields {sorted(allowed)}, got {sorted(config)}")
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
    return config


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
    return result


def _kill_group(proc):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=KILL_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue


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
    with tempfile.TemporaryFile(dir=work_dir) as out:
        _SIGNAL_STATE["in_stage"] = True
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
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                return "fail", zero_counts(), "STAGE_TIMEOUT", None, time.monotonic() - started
            except Interrupted:
                _SIGNAL_STATE["in_stage"] = False
                _kill_group(proc)
                raise
        finally:
            _SIGNAL_STATE["in_stage"] = False
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
        argv = expand_argv(entry["argv"], run_id, config["target"])
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
