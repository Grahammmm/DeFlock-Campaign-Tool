"""GET /health: read-only pipeline evidence for the private console."""
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import console_data_age, console_layout, console_runs_model, pipeline_health as health
from .outcomes.reading import esc

PATHS = ("/health",)
NAV = ("System health", 20)
STATES = ("stop", "warn", "unknown", "ok")
TOKEN = re.compile(r"[a-z_]{1,64}")
ROLES = {"runtime": "Runtime", "records_root": "Records root"}
REASONS = {
    "arrival_unreadable": "The waiting arrivals could not be read.",
    "none_waiting": "No arrivals are waiting to be processed.",
    "waiting_within_limit": "Arrivals are waiting but are not overdue.",
    "arrival_not_consumed": "Arrivals have been waiting too long for a run.",
    "mail_poll_not_configured": "No mail poll source is configured here.",
    "mail_poll_never": "No mail poll has been recorded.",
    "mail_poll_unreadable": "The last mail poll could not be read.",
    "mail_poll_stale": "No mail poll has been recorded recently enough.",
    "mail_poll_recent": "Mail was checked recently.",
    "run_report_invalid": "The latest run report is unreadable or invalid.",
    "run_report_missing": "No usable run report was found.",
    "run_report_future": "The latest run report says it finished in the future.",
    "no_recent_run": "No run has finished recently and at least two scheduled slots have passed.",
    "run_recent": "A run finished within the expected window.",
    "repeated_failures": "Several runs in a row failed.",
    "latest_run_failed": "The last reported run failed.",
    "runs_succeeding": "The last reported run did not fail.",
    "notify_ok": "No pending notification failure was found.",
    "notify_failed": "A notification attempt failed and no later success was found.",
    "schedule_unreadable": "The run schedule status could not be read.",
    "schedule_ok": "The run schedule is on track.",
    "schedule_late": "The scheduled slot is still within its grace window.",
    "schedule_degraded": "Scheduled processing is degraded.",
    "schedule_missed": "A scheduled run did not start.",
    "missed_slot": "A scheduled run did not start.",
    "schedule_failed": "A scheduled run failed.",
    "schedule_stalled": "A scheduled run has not finished in time.",
    "schedule_gaps": "There are gaps in scheduled processing.",
    "schedule_held": "Scheduled processing is on hold.",
    "schedule_never": "No scheduled run has been recorded.",
    "analysis_not_configured": "No analysis source is configured here.",
    "lane_off": "The analysis lane is switched off.",
    "analysis_stale": "The analysis lane has not recorded a recent cycle.",
    "analysis_error": "Analysis recorded an error or work still waiting to sync.",
    "analysis_unreadable": "The analysis status could not be read.",
    "analysis_ticking": "The analysis lane recorded a recent cycle.",
    "console_export_not_configured": "No console export is configured here.",
    "console_export_no_run": "No latest run report is available for comparison.",
    "console_export_unreadable": "The console export metadata could not be read.",
    "console_export_current": "The export matches the latest run report.",
    "console_export_stale": "The export is behind the latest run report.",
    "console_export_pending": "The latest run is still within its export grace window.",
    "disk_critical": "A filesystem is critically low on free space.",
    "disk_low": "A filesystem is low on free space.",
    "disk_ok": "The checked filesystems have enough free space.",
    "disk_unreadable": "Free space could not be read for a filesystem.",
    "backup_none": "No backup has been recorded for the check to verify.",
    "digests_invalid": "The backup record list could not be read or is malformed.",
    "backup_verify_failed": "The newest recorded backup set failed verification.",
    "backup_stale": "The newest recorded backup is older than the allowed age.",
}


def _token(value):
    return value if isinstance(value, str) and TOKEN.fullmatch(value) else None

def _label(check):
    return (_token(check) or "unrecognized_check").replace("_", " ").capitalize()

def _reason(code):
    code = _token(code) or "unrecognized"
    return REASONS.get(code) or f"No description is available for this reason ({code})."

def _time(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is not None:
            return stamp.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    return None

def _pacific(stamp):
    try:
        return stamp.astimezone(console_data_age.PACIFIC).strftime("%Y-%m-%d %H:%M %Z") if stamp else "unknown"
    except OverflowError:
        return "unknown"

def read_open_alerts(directory) -> dict:
    """Return state, projected rows, ignored entry count and directory mtime (epoch seconds)."""
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            mtime, rows, ignored = os.fstat(fd).st_mtime, [], 0
            for name in sorted(os.listdir(fd)):
                match = health.ALERT_RE.fullmatch(name)
                if match:
                    try:
                        data = console_runs_model._read_at(fd, name)
                        first, last = (_time(data.get(key)) for key in ("first_seen", "last_seen"))
                        if (data.get("schema") == health.ALERT_SCHEMA and data.get("state") in ("warn", "stop")
                                and (data.get("check"), data.get("reason_code")) == match.groups()
                                and first is not None and last is not None and first <= last):
                            rows.append(dict(check=match[1], reason_code=match[2], state=data["state"],
                                             first_seen=first, last_seen=last))
                            continue
                    except (OSError, ValueError):
                        pass
                ignored += 1
        finally:
            os.close(fd)
    except OSError:
        return dict(state="unavailable", rows=[], ignored=None, mtime=None)
    return dict(state="ok", rows=rows, ignored=ignored, mtime=mtime)

def _state(check):
    return check.get("state") if check.get("state") in STATES else "unknown"

def _details(check, report, roles):
    details = ["Since: " + _pacific(_time(check.get("since")))]
    if check.get("check") == "arrival_backlog":
        for key, label, unit in (("waiting", "Waiting arrivals", ""), ("oldest_waiting_minutes", "Oldest arrival", " min")):
            value = report.get(key)
            if type(value) is int:
                details.append(f"{label}: {value}{unit}")
    value = check.get("free_gib")
    if type(value) in (int, float) and math.isfinite(value):
        details.append(f"Free: {value:.1f} GiB")
    role = roles.get(_token(check.get("path_role")) or "")
    if role:
        details.append("Filesystem: " + role)
    return "; ".join(details)

def _alerts(target):
    if target is None:
        return "<p>Open alerts: unavailable: no runtime target is configured.</p>"
    result = read_open_alerts(target / "alerts")
    if result["state"] != "ok":
        return "<p>Open alerts: unavailable: the alerts directory cannot be read safely.</p>"
    rows = [f"<li><strong>{_label(r['check'])}: {r['state']}</strong> — {_reason(r['reason_code'])}<br>"
            f"First seen: {_pacific(r['first_seen'])}; Last seen: {_pacific(r['last_seen'])}</li>" for r in result["rows"]]
    ignored = result["ignored"]
    return (("<ul>" + "".join(rows) + "</ul>" if rows else "<p>No validated open alerts found.</p>")
            + (f"<p>{ignored} entries not shown (not open alerts or could not be validated).</p>" if ignored else ""))

def _backups(directory):
    """Open backup-check alarms only. Absence of an alarm is never shown as proof that a backup exists or was verified."""
    if directory is None:
        return "<p>Backups: not configured on this console.</p>"
    result = read_open_alerts(directory)
    if result["state"] != "ok":
        return "<p>Backups: unavailable: the backup alerts directory cannot be read safely.</p>"
    rows = [r for r in result["rows"] if r["check"] == "backup"]
    ignored = result["ignored"] + len(result["rows"]) - len(rows)  # other checks' alerts are counted, never shown
    if rows:
        shown = ("<ul>" + "".join(f"<li><strong>{_label(r['check'])}: {r['state']}</strong> — {_reason(r['reason_code'])}<br>"
                                   f"First seen: {_pacific(r['first_seen'])}; Last seen: {_pacific(r['last_seen'])}</li>" for r in rows) + "</ul>")
    else:
        try:
            changed = _pacific(datetime.fromtimestamp(result["mtime"], timezone.utc))
        except (OSError, OverflowError, ValueError):
            changed = "unknown"
        shown = (f"<p>No open backup alert. The alerts directory last changed {changed}. "
                 "This is not evidence that a backup exists or was verified.</p>")
    return shown + (f"<p>{ignored} entries not shown (not open backup alerts or could not be validated).</p>" if ignored else "")

def _disk(label, path):
    from . import admin_panel

    if path is None:
        return f"<li>{label}: unavailable: no runtime is configured.</li>"
    usage = admin_panel._load_system(path, timers=())["disk"]  # dashboard's disk reader, with no timer probes
    if usage is None:
        return f"<li>{label}: unavailable: free space could not be read.</li>"
    return f"<li>{label}: {usage['free'] / health.GIB:.1f} GiB free of {usage['total'] / health.GIB:.1f} GiB</li>"

def _export(pin, path, now):
    if pin is None:
        return "<p>Console export: unavailable: this console is serving live project data.</p>"
    prefix = f"<p>Console export: run {esc(pin.run_id)}"
    exported = _time(pin.exported_at)
    if exported is None or exported > now:
        return prefix + "; unavailable: the export time is missing, invalid or in the future.</p>"
    prefix += f", exported {_pacific(exported)} ({(now - exported) // timedelta(hours=1)} h ago).</p>"
    try:
        reports = console_runs_model.load_history(path, now)["reports"]
    except (OSError, ValueError):
        reports = []
    if not reports:
        return prefix + "<p>Run comparison: unavailable: no valid archived run report could be read.</p>"
    newest = reports[0]
    start = next((_time(r["started_at"]) for r in reports if r["run_id"] == pin.run_id), None)
    start = start or console_data_age._run_start(pin.run_id) or exported
    if newest["run_id"] == pin.run_id:
        status = "Current: the export matches that run."
    elif datetime.fromisoformat(newest["started_at"]) > start:
        status = "Stale: a newer run has completed."
    else:
        status = "Run comparison: unavailable: the export does not match and the run order cannot be confirmed."
    return prefix + f"<p>Newest archived run: {esc(newest['run_id'])}, finished {_pacific(_time(newest['finished_at']))}. {status}</p>"

def serve(handler):
    if handler.path != "/health":
        return False
    from . import admin_panel, console_oversight  # avoid the layout's route discovery cycle

    options, pin = handler.options, handler.pin
    path = options.get("run_report_path")
    target = console_oversight._target(path)
    runtime = target.parent if target is not None else None
    now = admin_panel.parse_as_of(options.get("as_of"))[0]
    root = Path(options["project"]) / "processing-v1"
    system = options.get("system", True)
    roles = dict(ROLES, records_root="Mounted export") if pin else ROLES
    report, fault = {}, "no runtime target is configured."
    if target is not None:
        try:
            report = health.assess(runtime_dir=str(runtime), target=target.name, records_root=root, now=now,
                                   analysis_root=handler.server.analysis_root, check_disk=system,
                                   pinned_export_run_id=pin.run_id if pin else None)
        except (health.HealthError, OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            fault = "the pipeline assessment could not be read."
    checks = report.get("checks", [])
    states = {_state(check) for check in checks}
    verdict = next((state for state in STATES if state in states), "unknown")
    body = [f"<h1>System health</h1><p><strong>Verdict: {verdict}</strong></p><h2>Pipeline checks</h2>"]
    body.extend(f"<h3>{_label(c.get('check'))}: {_state(c)}</h3><p>{_reason(c.get('reason_code'))}<br>"
                f"{_details(c, report, roles)}</p>" for c in checks)
    if not checks:
        body.append(f"<p>Health checks: unavailable: {'no checks were returned.' if report else fault}</p>")
    body.extend(("<h2>Open alerts</h2>", _alerts(target), "<h2>Backups</h2>", _backups(handler.server.backup_alerts_dir),
                 "<h2>Free disk space</h2>",
                 "<ul>" + _disk(roles["runtime"], runtime) + _disk(roles["records_root"], root) + "</ul>" if system
                 else "<p>Disk readings: unavailable: Disk checks are disabled.</p>",
                 "<h2>Console export freshness</h2>", _export(pin, path, now),
                 '<p><a href="/jobs">Jobs</a> · <a href="/runs">Run history</a></p>'))
    handler._send(200, console_layout.shell(NAV[0], "\n".join(body), active=NAV[0]))
    return True
