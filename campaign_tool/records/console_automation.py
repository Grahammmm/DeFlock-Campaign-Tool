"""Read-only automation overview: sanitized run metadata, user units and open alarm codes.

No ledger, records, credentials, network or health writers are consulted. The report
is the explicitly configured --run-report, not a guessed project file. A completed
run is historical evidence, never proof that its outputs were accepted or installed.
"""
import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import console_jobs as jobs, console_layout, pipeline_health, run_report
from .outcomes.reading import esc

# Include missing pilot companion units: absence must remain visible, not all-clear.
COMPANIONS = ("deflockslo-daily-mail.timer", "flock-daily.timer", "inbox-watch.timer",
              "deflock-analysis-cards.timer", "deflock-console-health.timer")
SERVICES = ("records-run.service", "records-console.service", "deflock-console-hosted.service",
            "deflock-admin-tunnel.service")
UNREPORTED = ("Arrival", "OCR", "Facts", "Policy comparison", "Gap proposals", "Follow-up drafts",
              "Findings lane", "Packets", "Console export", "Decisions apply")
CODE = re.compile(r"[a-z][a-z_]{0,63}")
STALE_HOURS = pipeline_health.LIMITS["run_max_hours"]


def _read(path):
    """Bounded, nonblocking, no-follow metadata read; never open a FIFO or device."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("not_regular")
        raw = handle.read(pipeline_health.MAX_JSON_BYTES + 1)
    if len(raw) > pipeline_health.MAX_JSON_BYTES:
        raise ValueError("too_large")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("not_object")
    return data


def report(path, now):
    if not path:
        return {"state": "unknown", "code": "run_report_not_configured", "data": None}
    try:
        data = _read(path)
        run_report.validate_run_report(data)
        finished = pipeline_health._finished(data)
        if finished > now:
            raise ValueError("future")
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return {"state": "unknown", "code": "run_report_missing_invalid_or_future", "data": None}
    stale = now - finished > timedelta(hours=STALE_HOURS)
    return {"state": "stale" if stale else "reported", "code": "old_report" if stale else "run_report_valid",
            "data": data}


def alarms(path):
    """Read existing health alarms; missing monitoring is unknown, not zero alarms.

    Cleared health files are excluded. Notify failures have no clearing schema, so
    count their markers separately without claiming they are unresolved health alarms.
    """
    result = {"state": "unknown", "rows": [], "notify_markers": 0}
    if not path or Path(path).name != "run-report.json":
        return result
    directory = Path(path).parent / "alerts"
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            return result
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= 1000:
                    return result
                if re.fullmatch(r"notify-failed-.*\.json", entry.name):
                    result["notify_markers"] += 1
                if not pipeline_health.ALERT_RE.fullmatch(entry.name):
                    continue
                data = _read(entry.path)
                if (data.get("schema") != pipeline_health.ALERT_SCHEMA or data.get("state") not in ("warn", "stop")
                        or not all(isinstance(data.get(k), str) and CODE.fullmatch(data[k])
                                   for k in ("check", "reason_code"))):
                    raise ValueError("invalid_alert")
                if entry.name != "health-%s-%s.json" % (data["check"], data["reason_code"]):
                    raise ValueError("mismatched_alert")
                result["rows"].append((data["check"], data["reason_code"], data["state"]))
    except (OSError, ValueError, TypeError):
        return result
    return dict(result, state="read", rows=sorted(result["rows"]))


def _service(unit, runner):
    props = jobs._show(runner, unit, jobs.SERVICE_PROPS + ("UnitFileState", "ActiveEnterTimestamp"))
    row = {"unit": unit, "state": "UNKNOWN", "last": "unknown", "next": "not scheduled (service)",
           "detail": "service state unavailable"}
    if props is None:
        return row
    if props["LoadState"] == "not-found":
        return dict(row, state="NOT INSTALLED", detail="service is missing")
    if props["LoadState"] != "loaded":
        return row
    active = props.get("ActiveState", "unknown")
    result, status = props.get("Result", "unknown"), props.get("ExecMainStatus", "unknown")
    state = "RUNNING" if active == "active" else "FAILED" if active == "failed" else active.upper()
    return dict(row, state=state,
                last=jobs._stamp(props.get("ExecMainExitTimestamp") or props.get("ActiveEnterTimestamp") or ""),
                detail="enabled=%s; result=%s exit=%s; running is not an accepted run" % (
                    props.get("UnitFileState", "unknown"), result, status))


def load(path=None, *, units=None, runner=jobs.system_runner, now=None):
    now = now or datetime.now(timezone.utc)
    units = sorted(set(jobs.discover(extra=COMPANIONS) if units is None else units))
    timers = [jobs.inspect(unit, runner) for unit in units]
    for row in timers:
        if row["state"] == "ok" and row["active"] != "active":
            row.update(state="unknown", detail="last service succeeded but the trigger is not active")
    return {"now": now.isoformat(), "report": report(path, now), "alarms": alarms(path),
            "timers": timers, "services": [_service(unit, runner) for unit in SERVICES]}


def _table(caption, headers, rows):
    width = ' style="min-width:48rem"' if len(headers) > 3 else ''
    column = ' style="min-width:10rem"' if width else ''
    return ('<div class="scroll" role="region" tabindex="0" aria-label="%s"><table%s><caption>%s</caption>'
            '<thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>') % (
                esc(caption), width, esc(caption), "".join('<th scope="col"%s>%s</th>' % (column, esc(h)) for h in headers),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % esc(v) for v in row) for row in rows))


def render(model):
    rep, alarm = model["report"], model["alarms"]
    data = rep["data"]
    out = ['<h1>Automation</h1><p>Read-only. Nothing here runs, retries, starts, stops, sends or publishes. '
           'Reported success is machine-valid and review-required, not semantic acceptance.</p>',
           '<p>Checked %s. Run metadata and systemd are separate sources; an export may be older.</p>' % esc(model["now"])]
    out.append('<h2>Open alarms and gaps</h2>')
    problems = [("Run report", rep["code"])] if rep["state"] != "reported" else []
    for row in model["timers"]:
        if row["state"] != "ok":
            problems.append((row["unit"], jobs.SHOWN[row["state"]] + ": " + row["detail"]))
    for row in model["services"]:
        if (row["state"] in ("UNKNOWN", "FAILED", "NOT INSTALLED")
                or row["unit"] != "records-run.service" and row["state"] != "RUNNING"):
            problems.append((row["unit"], row["state"]))
    if data:
        problems += [(s["name"], s["error_code"] or s["status"]) for s in data["stages"] if s["status"] != "ok"]
        problems += [(g["stage"], "%s (needs G); count=%s" % (g["code"], g["count"])) for g in data["needs_g"]]
    out.append('<p>Saved health alarms: %s. This is not an all-clear: a stopped monitor cannot refresh alarms.</p>' %
               (str(len(alarm["rows"])) if alarm["state"] == "read" else "UNKNOWN (missing or unreadable)"))
    problems += [(check, reason + " (" + state + ")") for check, reason, state in alarm["rows"]]
    if alarm["notify_markers"]:
        problems.append(("Notify failure markers", str(alarm["notify_markers"]) + " (historical; resolution unknown)"))
    out.append(_table("Alarm codes and evidence gaps", ("Source", "Problem"), problems) if problems else
               '<p>No problems in the available sources. This does not prove all automation is running.</p>')
    out.append('<h2>Stages</h2><p>Wide tables scroll sideways inside their labelled regions (touch or keyboard).</p>')
    if data:
        out.append('<p>Configured report: run %s; overall %s; finished %s. %s</p>' % (
            esc(data["run_id"]), esc(data["overall_status"]), esc(data["finished_at"]),
            'STALE: older than %s hours; historical evidence only.' % STALE_HOURS if rep["state"] == "stale" else
            'Report age is within %s hours; this is not a schedule guarantee.' % STALE_HOURS))
    else:
        out.append('<p>Run report unavailable. Stage results and last run are UNKNOWN.</p>')
    pipeline_timer = next((r for r in model["timers"] if r["unit"] == "records-run.timer"), None)
    next_run = (pipeline_timer["next_run"] if pipeline_timer and pipeline_timer["enabled"] in jobs.ENABLED_OK
                and pipeline_timer["active"] == "active" else "unknown")
    out.append('<p>Stage last run means the containing run finished at that time; individual stage timestamps are not '
               'recorded. Next run is the next pipeline opportunity, not a promise that a held or skipped stage will run.</p>')
    stages = {s["name"]: s for s in data["stages"]} if data else {}
    rows = []
    for name in run_report.STAGE_ORDER:
        stage = stages.get(name)
        rows.append((name, stage["status"] + (" (historical)" if rep["state"] == "stale" else "") if stage else "UNKNOWN",
                     data["finished_at"] if stage and stage["status"] != "skipped" else "unknown / not run",
                     next_run, stage["error_code"] or "none reported" if stage else "no stage evidence",
                     "; ".join("%s=%s" % item for item in stage["counts"].items()) if stage else "unknown"))
    # Do not turn outputs=ok into invented evidence for unreported substeps.
    rows += [(name, "UNKNOWN (not separately reported)", "unknown", "unknown", "not proved by outputs=ok", "unknown")
             for name in UNREPORTED]
    out.append(_table("Stage evidence", ("Stage", "State", "Last run (UTC)", "Next opportunity", "Code / gap", "Counts"), rows))
    if data and "ocr" in data:
        out.append('<p>OCR counters (not proof of acceptance): %s.</p>' % esc(
            "; ".join("%s=%s" % item for item in sorted(data["ocr"].items()))))
    out.append('<h2>Timers and arrival trigger</h2><p>Unit names include shipped templates and expected pilot companions. '
               'Not installed is a gap, not a pass. Timer success refers only to its last completed service execution.</p>')
    out.append(_table("User units (systemctl --user show)", ("Unit", "State", "Installed / enabled / active", "Last trigger", "Next run", "Result"),
                      [(r["unit"], jobs.SHOWN[r["state"]], r["installed"] + " / " + r["enabled"] + " / " + r["active"],
                        r["last_trigger"], r["next_run"], r["last_result"] + "; " + r["detail"]) for r in model["timers"]]))
    out.append('<h2>Companion services</h2>')
    out.append(_table("Service lifecycle (not stage acceptance)", ("Unit", "State", "Last exit / activation", "Next run", "Detail"),
                      [(r["unit"], r["state"], r["last"], r["next"], r["detail"]) for r in model["services"]]))
    return console_layout.shell("Automation", "\n".join(out), active="Automation")


def serve(handler):
    if handler.path != "/automation":
        return False
    options = handler.options
    hooks = getattr(handler.server, "jobs_hooks", {})
    runner = hooks.get("runner") or (jobs.system_runner if options.get("system", True) else jobs._disabled)
    units = jobs.discover(extra=(*COMPANIONS, *options.get("timers", ())))
    handler._send(200, render(load(options.get("run_report_path"), units=units, runner=runner)))
    return True
