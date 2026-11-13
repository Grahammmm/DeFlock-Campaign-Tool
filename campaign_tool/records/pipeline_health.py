"""Silent-stop watch for the records pipeline: one content-free "Run health" line.

``records health`` knows the configured run calendar (weekdays twice a day by
default). This module adds what it cannot
see: mail that arrived but no run consumed, a mail poll that stopped, repeated
failed runs, a failed notify, a run report that went stale and (when their roots
are given) an analysis lane that stopped ticking or a console export left behind
the newest run. ``assess`` only
reads existing state files. ``write_alerts`` is the only write: one small alert
file per open problem under ``<runtime>/<target>/alerts``. Nothing here runs,
retries, restarts, notifies, clears a hold or writes the ledger, and no timer is
installed by this module. Reports and the status line carry fixed codes and
counts only: never a path, id, address or record text.
"""
import argparse
import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import arrival, run_report, schedule

SCHEMA = "records-health-v1"
ALERT_SCHEMA = "records-health-alert-v1"
LIMITS = {"arrival_max_minutes": 30, "mail_poll_warn_hours": 6, "mail_poll_stop_hours": 24, "run_max_hours": 26,
          "failure_streak": 3, "report_window": 20, "analysis_max_minutes": 60, "export_grace_minutes": 15}
# An optional check whose root was not given reports unknown but never moves the overall state or the line.
NOT_CONFIGURED = frozenset({"analysis_not_configured", "console_export_not_configured"})
# analysis_bridge.FINAL: any other logged sync outcome (PROFILE_MISSING, TEXT_CONFLICT, CONFIG_INVALID) leaves the pair pending.
SYNC_FINAL = frozenset({"SYNCED", "UNCHANGED", "NO_AGENCIES", "ANALYSIS_READY_MISSING"})
# Receive outcomes that refused an export; "ERROR LOCKED" is a busy review lock, retried next tick, and never alarms.
ANALYSIS_ERRORS = frozenset({"EXPORT_INVALID", "EXPORT_CONFLICT"})
EXIT = {"ok": 0, "warn": 1, "unknown": 2, "stop": 3}
TARGET_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
ALERT_RE = re.compile(r"^health-([a-z_]+)-([a-z_]+)\.json$")
MAX_JSON_BYTES = 1024 * 1024
LINE_MAX = 199
WORD = {"ok": "ok", "warn": "WARNING", "unknown": "UNKNOWN", "stop": "STOPPED"}
SCHEDULE_STATE = {"ok": "ok", "late": "ok", "degraded": "warn", "missed": "stop", "failed": "stop",
                  "stalled": "stop", "gaps": "stop", "held": "stop", "never": "unknown"}


class HealthError(Exception):
    """Carries a fixed code only."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _target_dir(runtime_dir, target):
    if not isinstance(target, str) or not TARGET_RE.match(target) or target in (".", ".."):
        raise HealthError("target_invalid")
    root = Path(runtime_dir)
    if not root.is_absolute():
        raise HealthError("runtime_invalid")
    return root / target


def _read_json(path):
    """Parsed JSON object, ``None`` when the file is absent; ValueError when unreadable."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("unreadable") from None
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("too large")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


def _finished(report):
    return datetime.strptime(report["finished_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _future(report, now):
    """True when an already schema-valid report finished strictly after the aware assessment clock."""
    return _finished(report) > now.astimezone(timezone.utc)


def _check(name, state, reason, since=None, **extra):
    item = {"check": name, "state": state, "reason_code": reason, "since": since.isoformat() if since else None}
    item.update(extra)
    return item


def _ago(delta):
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 120:
        return f"{minutes} min"
    return f"{minutes // 60}h" if minutes < 48 * 60 else f"{minutes // 1440}d"


# ----- checks ---------------------------------------------------------------------------------

def _arrival_backlog(runtime_dir, now, limits):
    try:
        stamps = arrival.pending(runtime_dir)
        seen = [arrival.parse_seen_at(item["seen_at"]) for item in stamps]
    except (arrival.ArrivalError, ValueError, OSError):
        return _check("arrival_backlog", "unknown", "arrival_unreadable"), 0, None
    if not seen:
        return _check("arrival_backlog", "ok", "none_waiting"), 0, None
    oldest = min(seen)
    age = now - oldest
    stale = age > timedelta(minutes=limits["arrival_max_minutes"])
    state, reason = ("stop", "arrival_not_consumed") if stale else ("ok", "waiting_within_limit")
    return _check("arrival_backlog", state, reason, oldest), len(seen), age


def _mail_poll(mail_store, now, limits):
    if mail_store is None:
        return _check("mail_poll", "unknown", "mail_poll_not_configured")
    try:
        state = _read_json(Path(mail_store) / "mail-poll" / "state.json")
        if state is None:
            return _check("mail_poll", "unknown", "mail_poll_never")
        updated = datetime.fromisoformat(str(state["updated_at"]).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    except (ValueError, KeyError, OSError):
        return _check("mail_poll", "unknown", "mail_poll_unreadable")
    age = now - updated
    if age > timedelta(hours=limits["mail_poll_stop_hours"]):
        return _check("mail_poll", "stop", "mail_poll_stale", updated)
    if age > timedelta(hours=limits["mail_poll_warn_hours"]):
        return _check("mail_poll", "warn", "mail_poll_stale", updated)
    return _check("mail_poll", "ok", "mail_poll_recent", updated)


def _valid_report(data):
    try:
        run_report.validate_run_report(data)
    except (ValueError, KeyError, TypeError):
        return False
    return True


def _recent_reports(target_dir, limits, *, now):
    """Newest ``report_window`` valid reports (oldest first) and the count of invalid or future ones skipped.

    A report that finished after ``now`` is not evidence of completed processing and is never returned."""
    directory = target_dir / "reports"
    try:
        names = sorted(name for name in os.listdir(directory) if re.match(r"^run_[0-9TZ]{16}_[0-9a-f]{8}\.json$", name))
    except OSError:
        return [], 0
    valid, bad = [], 0
    for name in names[-limits["report_window"]:]:
        try:
            data = _read_json(directory / name)
        except (ValueError, OSError):
            bad += 1
            continue
        if data is not None and _valid_report(data) and not _future(data, now):
            valid.append(data)
        else:
            bad += 1
    return valid, bad


def _last_run(target_dir, now, limits, spec):
    try:
        latest = _read_json(target_dir / "run-report.json")
    except (ValueError, OSError):
        return _check("last_run", "stop", "run_report_invalid"), None
    if latest is None:
        return _check("last_run", "unknown", "run_report_missing"), None
    if not _valid_report(latest):
        return _check("last_run", "stop", "run_report_invalid"), None
    if _future(latest, now):
        return _check("last_run", "stop", "run_report_future"), None
    finished = _finished(latest)
    # A run is only overdue once the hours limit has passed AND two calendar slots have come and gone
    # since it finished. Without the second test a weekend (no slots) would read as "no recent run".
    previous = schedule.due_slots(now, 2, spec)
    skipped_two = len(previous) == 2 and finished < previous[1].astimezone(timezone.utc)
    if now - finished > timedelta(hours=limits["run_max_hours"]) and skipped_two:
        return _check("last_run", "stop", "no_recent_run", finished), latest
    return _check("last_run", "ok", "run_recent", finished), latest


def _run_failures(target_dir, latest, limits, now):
    series, _ = _recent_reports(target_dir, limits, now=now)
    if not series and latest is not None:
        series = [latest]
    if not series:
        return _check("run_failures", "unknown", "run_report_missing")
    streak = limits["failure_streak"]
    tail = series[-streak:]
    if len(tail) == streak and all(item["overall_status"] == "fail" for item in tail):
        return _check("run_failures", "stop", "repeated_failures", _finished(tail[0]))
    if series[-1]["overall_status"] == "fail":
        return _check("run_failures", "warn", "latest_run_failed", _finished(series[-1]))
    return _check("run_failures", "ok", "runs_succeeding")


def _notify(target_dir, limits, now):
    alerts = target_dir / "alerts"
    try:
        failed = [name for name in os.listdir(alerts) if re.match(r"^notify-failed-.*\.json$", name)]
    except OSError:
        failed = []
    if not failed:
        return _check("notify", "ok", "notify_ok")
    series, _ = _recent_reports(target_dir, limits, now=now)
    good = [item for item in series if item["overall_status"] != "fail" and not item["notify_failed"]]
    cutoff = _finished(good[-1]).timestamp() if good else None
    newest = 0.0
    for name in failed:
        try:
            newest = max(newest, os.lstat(alerts / name).st_mtime)
        except OSError:
            continue
    if cutoff is None or newest > cutoff:
        return _check("notify", "warn", "notify_failed",
                      datetime.fromtimestamp(newest, timezone.utc) if newest else None)
    return _check("notify", "ok", "notify_ok")


def _schedule(records_root, now, spec):
    try:
        result = schedule.health(records_root, now=now, spec=spec)
        state = SCHEDULE_STATE[result["status"]]
    except Exception:  # fail closed: an unreadable ledger is never ok
        return _check("schedule", "unknown", "schedule_unreadable")
    return _check("schedule", state, "schedule_" + result["status"])


def _ledger_error(path):
    """True when the newest entry of any tick agency, packet, received run or run x agency pair failed or left its sync
    pending. The whole ledger is read (analysis-sync does too): a pending sync outcome is logged once, not every cycle."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    newest = {}
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        for line in filter(bytes.strip, handle):
            entry = json.loads(line.decode("utf-8"))
            if not isinstance(entry, dict) or not isinstance(entry.get("outcome"), str):
                raise ValueError("not a ledger entry")
            newest[tuple(str(entry.get(key)) for key in ("kind", "agency", "run_id", "agency_id"))] = entry["outcome"]
    return any((o.startswith("ERROR") and o != "ERROR LOCKED") or o in ANALYSIS_ERRORS or (k[0] == "sync" and o not in SYNC_FINAL)
               for k, o in newest.items())


def _analysis(analysis_root, now, limits):
    """The records-analysis lane: off, ticking (run-analysis.sh appends to .analysis/cycle.log every cycle), stale or erroring."""
    if analysis_root is None:
        return _check("analysis", "unknown", "analysis_not_configured")
    base = Path(analysis_root) / ".analysis"
    try:
        if not Path(analysis_root).is_absolute() or not stat.S_ISDIR(os.lstat(analysis_root).st_mode):
            raise ValueError("analysis root")
        if not os.path.exists(base / "LANE-ON"):
            return _check("analysis", "ok", "lane_off")
        try:  # a lane switched on that never completed a cycle counts from the LANE-ON switch
            info = os.lstat(base / "cycle.log")
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("cycle log")
        except FileNotFoundError:
            info = os.stat(base / "LANE-ON")
        tick = datetime.fromtimestamp(info.st_mtime, timezone.utc)
        if now - tick > timedelta(minutes=limits["analysis_max_minutes"]):
            return _check("analysis", "stop", "analysis_stale", tick)
        if _ledger_error(base / "analysis-ledger.jsonl"):
            return _check("analysis", "warn", "analysis_error")
    except (ValueError, OSError):
        return _check("analysis", "unknown", "analysis_unreadable")
    return _check("analysis", "ok", "analysis_ticking", tick)


def _console_export(export_root, latest, now, limits):
    """Does CONSOLE_EXPORT_DIR/current/EXPORT.json name the newest run report's run (allowing a post-step grace)?"""
    if export_root is None:
        return _check("console_export", "unknown", "console_export_not_configured")
    if latest is None:
        return _check("console_export", "unknown", "console_export_no_run")
    try:
        if not Path(export_root).is_absolute():
            raise ValueError("export root")
        manifest = _read_json(Path(export_root) / "current" / "EXPORT.json")
        exported = None if manifest is None else manifest["run_id"]
    except (ValueError, KeyError, OSError):
        return _check("console_export", "unknown", "console_export_unreadable")
    if exported == latest["run_id"]:
        return _check("console_export", "ok", "console_export_current")
    finished = _finished(latest)
    if now - finished > timedelta(minutes=limits["export_grace_minutes"]):
        return _check("console_export", "stop", "console_export_stale", finished)
    return _check("console_export", "ok", "console_export_pending", finished)


# ----- assessment and line ----------------------------------------------------------------------

def assess(*, runtime_dir, target, records_root, mail_store=None, now=None, limits=LIMITS,
           spec=None, analysis_root=None, console_export_root=None):
    """Read-only health report; unknown is never reported as ok."""
    spec = spec or schedule.configured()
    now = (now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        raise HealthError("now_naive")
    now = now.astimezone(timezone.utc)
    target_dir = _target_dir(runtime_dir, target)
    backlog, waiting, age = _arrival_backlog(runtime_dir, now, limits)
    last_run, latest = _last_run(target_dir, now, limits, spec)
    checks = [backlog, _mail_poll(mail_store, now, limits), last_run, _run_failures(target_dir, latest, limits, now),
              _notify(target_dir, limits, now), _schedule(records_root, now, spec), _analysis(analysis_root, now, limits),
              _console_export(console_export_root, latest, now, limits)]
    states = {item["state"] for item in checks if item["reason_code"] not in NOT_CONFIGURED}
    overall = next(state for state in ("stop", "warn", "unknown", "ok") if state in states)
    report = {"schema": SCHEMA, "now": now.isoformat(), "state": overall, "exit_code": EXIT[overall],
              "waiting": waiting, "oldest_waiting_minutes": None if age is None else int(age.total_seconds() // 60),
              "last_run_finished_at": latest["finished_at"] if latest else None, "checks": checks}
    return report


def status_line(report):
    """``Run health: <state> - <reason>``: single line, fixed words, relative times, under 200 characters."""
    state = report["state"]
    now = datetime.fromisoformat(report["now"])
    if state == "ok":
        finished = report.get("last_run_finished_at")
        ago = _ago(now - datetime.strptime(finished, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)) if finished else "?"
        text = f"Run health: ok - last run {ago} ago, {report['waiting']} waiting"
    else:
        reasons = []
        for item in report["checks"]:
            if item["state"] != state or item["reason_code"] in NOT_CONFIGURED:
                continue
            reason = item["reason_code"]
            if reason == "arrival_not_consumed":
                reason += f" ({report['waiting']} waiting, oldest {report['oldest_waiting_minutes']} min)"
            reasons.append(reason)
        text = f"Run health: {WORD[state]} - " + "; ".join(reasons)
    return text[:LINE_MAX]


# ----- alerts (the only write) ------------------------------------------------------------------

def _safe_dir(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        try:
            os.mkdir(path, 0o700)
        except OSError:
            raise HealthError("alerts_unsafe") from None
        info = os.lstat(path)
    except OSError:
        raise HealthError("alerts_unsafe") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise HealthError("alerts_unsafe")


def _safe_file(path):
    """Existing alert JSON object, or ``None`` when absent; refuses links, foreign owners and loose modes."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise HealthError("alerts_unsafe") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        raise HealthError("alerts_unsafe")
    try:
        data = _read_json(path)
    except (ValueError, OSError):
        raise HealthError("alerts_unsafe") from None
    return data


def _put(directory, name, data):
    tmp = directory / ("." + name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, (json.dumps(data, sort_keys=True) + "\n").encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, directory / name)


def write_alerts(report, alerts_dir):
    """Write/refresh one alert file per open problem; mark resolved ones cleared. Returns files written."""
    alerts_dir = Path(alerts_dir)
    _safe_dir(alerts_dir)
    stamp = report["now"]
    states = {item["check"]: item for item in report["checks"]}
    # Validate every health file first so an unsafe one writes nothing at all.
    existing = {}
    for name in sorted(os.listdir(alerts_dir)):
        match = ALERT_RE.match(name)
        if match:
            existing[name] = (match.group(1), match.group(2), _safe_file(alerts_dir / name))
    for item in report["checks"]:
        if item["state"] in ("stop", "warn"):
            name = f"health-{item['check']}-{item['reason_code']}.json"
            if name not in existing:
                _safe_file(alerts_dir / name)
    written = 0
    for item in report["checks"]:
        if item["state"] not in ("stop", "warn"):
            continue
        name = f"health-{item['check']}-{item['reason_code']}.json"
        prior = existing.get(name, (None, None, None))[2]
        first = prior.get("first_seen", stamp) if prior else stamp
        count = (prior.get("count", 0) if prior else 0) + 1
        _put(alerts_dir, name, {"schema": ALERT_SCHEMA, "check": item["check"], "state": item["state"],
                                "reason_code": item["reason_code"], "first_seen": first, "last_seen": stamp,
                                "count": count})
        written += 1
    for name, (check, reason, data) in existing.items():
        current = states.get(check)
        if current is None or current["state"] == "unknown":
            continue
        if current["state"] in ("stop", "warn") and current["reason_code"] == reason:
            continue
        cleared = dict(data or {}, state="cleared", cleared_at=stamp)
        _put(alerts_dir, name[:-len(".json")] + ".cleared.json", cleared)
        os.unlink(alerts_dir / name)
    return written


# ----- CLI --------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="records pipeline-health", description="Print one Run health line.")
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--root", required=True, help="records root holding ledger.sqlite")
    parser.add_argument("--mail-store")
    parser.add_argument("--now", help="ISO-8601 clock override for tests and reviews")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--record-alerts", action="store_true")
    parser.add_argument("--analysis-root", help="analysis root of records-analysis (run-analysis.sh ANALYSIS_ROOT)")
    parser.add_argument("--analysis-max-minutes", type=int, default=LIMITS["analysis_max_minutes"])
    parser.add_argument("--console-export-root", help="console export directory (run-records.sh CONSOLE_EXPORT_DIR)")
    schedule._add_calendar_flags(parser)
    args = parser.parse_args(argv)
    spec = schedule._spec_from(parser, args)
    if args.analysis_max_minutes < 1:
        parser.error("--analysis-max-minutes: expected a positive number of minutes")
    try:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
        report = assess(runtime_dir=args.runtime_dir, target=args.target, records_root=args.root,
                        mail_store=args.mail_store, now=now, spec=spec, analysis_root=args.analysis_root,
                        console_export_root=args.console_export_root,
                        limits=dict(LIMITS, analysis_max_minutes=args.analysis_max_minutes))
        if args.record_alerts:
            report["alerts_written"] = write_alerts(report, _target_dir(args.runtime_dir, args.target) / "alerts")
    except (HealthError, ValueError) as error:
        print("records pipeline-health: refused " + (error.code if isinstance(error, HealthError) else "now_invalid"),
              file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True) if args.json else status_line(report))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
