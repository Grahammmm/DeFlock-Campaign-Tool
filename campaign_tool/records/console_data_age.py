"""One data-age strip for private console responses and static renders."""
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import console_automation, pipeline_health, run_report
from .outcomes.reading import esc

DEFAULT_STALE_HOURS = 13
MAX_STALE_HOURS = 168
PACIFIC = ZoneInfo("America/Los_Angeles")
RUN_STAMP = re.compile(r"run_([0-9]{8}T[0-9]{6}Z)_[0-9a-f]{8}")


def _run_start(run_id):
    match = RUN_STAMP.fullmatch(run_id)
    if match is not None:
        try:
            return datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _reports(path, now):
    """Use the health report format and recent window for this configured target only."""
    path = Path(path)
    latest = console_automation.report(path, now)["data"]
    if latest is not None:
        yield latest
    if path.name != "run-report.json":
        return
    directory = path.parent / "reports"
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            return
        names = sorted(p for p in directory.iterdir() if p.suffix == ".json" and RUN_STAMP.fullmatch(p.stem))
    except OSError:
        return
    for file in names[-pipeline_health.LIMITS["report_window"]:]:
        data = console_automation.report(file, now)["data"]
        if data is not None and data["run_id"] == file.stem:
            yield data


def _newer_run(path, pin, exported, now):
    if not path:
        return False
    reports = list(_reports(path, now))
    # Prefer the real start of the pinned run, then its encoded start. For a legacy
    # ID of unknown age, only a run that started after the export proves it is newer.
    start = next((run_report._parse_utc(r["started_at"]) for r in reports if r["run_id"] == pin.run_id), None)
    start = start or _run_start(pin.run_id) or exported
    return any(r["run_id"] != pin.run_id and run_report._parse_utc(r["started_at"]) > start for r in reports)


def _pacific(stamp):
    return stamp.astimezone(PACIFIC).strftime("%H:%M PT, %Y-%m-%d")


def _export_label(pin, path, now, stale_hours):
    try:
        exported = datetime.fromisoformat(pin.exported_at)
        if exported.tzinfo is None or exported > now:
            return "Data age: unknown"
        exported = exported.astimezone(timezone.utc)
        stamp = _pacific(exported)
    except (TypeError, ValueError, OverflowError):
        return "Data age: unknown"
    age = now - exported
    label = f"Data from run {esc(pin.run_id)}, exported {stamp} ({age // timedelta(hours=1)} h ago)"
    if age > timedelta(hours=stale_hours):
        reason = f"this export is more than {stale_hours} hours old."
    elif _newer_run(path, pin, exported, now):
        reason = "a newer run has completed since the run in this export."
    else:
        return label
    return label + f' <strong style="color:#b00020">STALE: {reason}</strong>'


def insert(body, *, live=False, pin=None, now=None, report_path=None, stale_hours=DEFAULT_STALE_HOURS, static=False):
    """Insert after the shared header; preserve empty redirects and headerless result fragments."""
    if not body:
        return body
    label = "Data age: unknown"
    if live:
        label = "Live project data"
        if static:
            label += f" · Static snapshot rendered {_pacific(now)}"
    elif pin is not None:
        label = _export_label(pin, report_path, now, stale_hours)
    strip = f'<div class="data-age" style="grid-column:1/-1;overflow-wrap:anywhere">{label}</div>'
    head, header, rest = body.partition("</header>")
    return head + header + strip + rest if header else strip + body
