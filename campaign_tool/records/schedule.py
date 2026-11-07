"""Schedule rendering and health for the records pipeline.

The default service promise is two ``records run`` invocations per weekday at
07:00 and 15:00 America/Los_Angeles (DST follows the zone). Records requests do
not arrive on weekends, so no weekend slot exists and a weekend with no run is
never a miss. The calendar is data: a ``Schedule`` (weekdays, times, zone) that
each deployment sets for its own geography. Health is derived from that promise
and the ledger's ``runs`` rows, never from directory timestamps or from a fixed
"hours since" threshold:

- ``ok``      the most recent due slot has a completed run (started at or after
              the slot, or finished after it);
- ``late``    the slot is due, the grace window has not passed and no run has
              started yet;
- ``missed``  the grace window passed with no completed run for that slot;
- ``failed``  the latest run for the slot ended with a failure status;
- ``stalled`` a run started for the slot but never ended and the grace window
              passed (a crash before finalisation);
- ``never``   the ledger has no pipeline runs at all.

Slots that fell before the first recorded run are ``before_first_run`` and are
never counted as misses; ``degraded`` means the latest slot is fine but an
earlier slot in the lookback was missed, failed or stalled.

``records schedule render`` writes fully resolved user-systemd units plus an
activation and a rollback script into ``<root>/ops``. Nothing here enables,
starts or stops anything on the host: activation is the owner's one-time step,
and both scripts take ``--dry-run`` so the exact commands can be reviewed first.
"""
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import shlex
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .ledger import store

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DAY = "(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
DAYS_RE = re.compile(rf"{_DAY}(?:\.\.{_DAY})?(?:,{_DAY}(?:\.\.{_DAY})?)*")
TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
ZONE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+){0,2}")
MAX_TIMES = 6
MIN_TIME_GAP_MINUTES = 60  # a run may take up to 47 minutes; slots closer than this could overlap


class ScheduleError(ValueError):
    """An invalid or unsafe calendar specification."""


def parse_days(text):
    """Weekday numbers (Mon=0) of a strict ``Mon..Fri`` / ``Mon,Wed,Fri`` list, or ``daily``."""
    if not isinstance(text, str):
        raise ScheduleError("days: expected a string")
    if text == "daily":
        return tuple(range(7))
    if not DAYS_RE.fullmatch(text):
        raise ScheduleError(f"days: {text!r} is not 'daily' or a list like 'Mon..Fri' or 'Mon,Wed,Fri'")
    found = set()
    for part in text.split(","):
        first, _, last = part.partition("..")
        start = WEEKDAY_NAMES.index(first)
        end = WEEKDAY_NAMES.index(last) if last else start
        if end < start:
            raise ScheduleError(f"days: range {part!r} runs backwards; list the days instead")
        found.update(range(start, end + 1))
    return tuple(sorted(found))


def parse_times(text):
    """Ascending ``HH:MM`` slot times from a comma list such as ``07:00,15:00``."""
    if not isinstance(text, str):
        raise ScheduleError("times: expected a string")
    parts = text.split(",")
    if not 1 <= len(parts) <= MAX_TIMES or not all(TIME_RE.fullmatch(part) for part in parts):
        raise ScheduleError(f"times: {text!r} is not 1 to {MAX_TIMES} comma-separated 24-hour HH:MM values")
    slots = tuple((int(part[:2]), int(part[3:])) for part in parts)
    if list(slots) != sorted(set(slots)):
        raise ScheduleError("times: must be strictly ascending with no duplicates")
    minutes = [h * 60 + m for h, m in slots]
    gaps = [b - a for a, b in zip(minutes, minutes[1:])] + [minutes[0] + 1440 - minutes[-1]] * (len(minutes) > 1)
    if gaps and min(gaps) < MIN_TIME_GAP_MINUTES:
        raise ScheduleError(f"times: slots must be at least {MIN_TIME_GAP_MINUTES} minutes apart")
    return slots


def parse_zone(text):
    if not isinstance(text, str) or not ZONE_RE.fullmatch(text):
        raise ScheduleError(f"timezone: {text!r} is not an IANA zone name such as America/Los_Angeles")
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ScheduleError(f"timezone: {text!r} is not a known IANA zone") from None
    return text


class Schedule:
    """Validated records-run calendar: weekdays, slot times and an IANA zone."""

    def __init__(self, days="Mon..Fri", times="07:00,15:00", timezone="America/Los_Angeles"):
        self.days_spec, self.times_spec, self.timezone = days, times, parse_zone(timezone)
        self.weekdays, self.slots = parse_days(days), parse_times(times)
        self.zone = ZoneInfo(self.timezone)

    def __eq__(self, other):
        return isinstance(other, Schedule) and self.on_calendar() == other.on_calendar()

    def __hash__(self):
        return hash(tuple(self.on_calendar()))

    def on_calendar(self):
        """Values for systemd ``OnCalendar=`` lines, one per slot time."""
        prefix = "" if self.days_spec == "daily" or len(self.weekdays) == 7 else self.days_spec + " "
        return [f"{prefix}*-*-* {h:02d}:{m:02d}:00 {self.timezone}" for h, m in self.slots]

    def label(self):
        return f"{self.days_spec} {self.times_spec} {self.timezone}"

    def runs_per_week(self):
        return len(self.weekdays) * len(self.slots)


DEFAULT_SCHEDULE = Schedule()
ENV_KEYS = ("RECORDS_SCHEDULE_DAYS", "RECORDS_SCHEDULE_TIMES", "RECORDS_SCHEDULE_TIMEZONE")


def configured(environ=None):
    """The deployment's calendar: ``RECORDS_SCHEDULE_DAYS/_TIMES/_TIMEZONE`` over the weekday default.

    The console, admin panel and health commands call this when no calendar is passed, so one
    environment setting keeps every view on the same calendar as the rendered timer.
    """
    environ = os.environ if environ is None else environ
    days, times, zone = (environ.get(key) or default for key, default in
                         zip(ENV_KEYS, (DEFAULT_SCHEDULE.days_spec, DEFAULT_SCHEDULE.times_spec, DEFAULT_SCHEDULE.timezone)))
    return Schedule(days, times, zone)
TIMEZONE = DEFAULT_SCHEDULE.timezone
SLOTS = DEFAULT_SCHEDULE.slots
GRACE = timedelta(minutes=45)
RUN_KINDS = ("stage-runner",)
TIMER_NAME = "records-run.timer"
SERVICE_NAME = "records-run.service"
VERSION = "records-schedule-v1"


def _aware(value, spec):
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(spec.zone)


def _slot_at(spec, date_, hour, minute):
    """The slot's aware wall time, or ``None`` when that wall time does not exist (spring-forward gap).

    systemd skips a calendar time that DST removes rather than shifting it, so the slot
    simply does not occur that day.
    """
    slot = datetime(date_.year, date_.month, date_.day, hour, minute, tzinfo=spec.zone)
    utc = ZoneInfo("UTC")
    if slot.astimezone(utc).astimezone(spec.zone).replace(tzinfo=None) != slot.replace(tzinfo=None):
        return None
    return slot


def due_slots(now, count=3, spec=None):
    """The ``count`` most recent slots at or before ``now``, newest first, in the zone."""
    spec = spec or configured()
    now = _aware(now, spec)
    day = now.date()
    found = []
    # Search back until the count is met; a calendar with at least one weekday always has a slot
    # within a week, so this is bounded by count weeks plus slack for skipped DST days.
    for back in range(0, 7 * count + 15):
        date_ = day - timedelta(days=back)
        if date_.weekday() not in spec.weekdays:
            continue
        for hour, minute in sorted(spec.slots, reverse=True):
            slot = _slot_at(spec, date_, hour, minute)
            if slot is not None and slot <= now:
                found.append(slot)
                if len(found) == count:
                    return found
    return found


def next_slot(now, spec=None):
    spec = spec or configured()
    now = _aware(now, spec)
    for ahead in range(0, 23):  # a one-day-a-week calendar whose slot hits a DST gap needs two weeks
        date_ = now.date() + timedelta(days=ahead)
        if date_.weekday() not in spec.weekdays:
            continue
        for hour, minute in sorted(spec.slots):
            slot = _slot_at(spec, date_, hour, minute)
            if slot is not None and slot > now:
                return slot
    raise AssertionError("unreachable")


def _parse(stamp, spec):
    if not stamp:
        return None
    value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(spec.zone)


def _runs(database):
    with store.ledger(database, readonly=True) as con:
        placeholders = ",".join("?" for _ in RUN_KINDS)
        rows = con.execute(f"SELECT run_id,kind,started_at,ended_at,status FROM runs WHERE kind IN ({placeholders}) "
                           "ORDER BY started_at DESC LIMIT 200", RUN_KINDS).fetchall()
        open_alerts = con.execute("SELECT count(*) FROM alerts WHERE state='open'").fetchone()[0]
        blocked = con.execute("SELECT count(*) FROM stage_state WHERE status='blocked'").fetchone()[0]
    return [dict(row) for row in rows], open_alerts, blocked


def classify_slot(slot, runs, now, spec=None):
    """Status of one due slot given the ledger's runs (newest first) and the clock.

    A run belongs to the slot when it started between the slot and the following
    slot, or when it started earlier (a Persistent catch-up) and finished inside
    that window. Runs for a later slot never count for an earlier one.
    """
    spec = spec or configured()
    window_end = slot + GRACE
    following = min(next_slot(slot, spec), now)
    candidates = []
    for run in runs:
        started = _parse(run["started_at"], spec)
        ended = _parse(run["ended_at"], spec)
        if started is None:
            continue
        in_window = slot <= started < following or started == following == now
        caught_up = started < slot and ended is not None and slot <= ended < following
        if in_window or caught_up:
            candidates.append((started, ended, run))
    if not candidates:
        if now < window_end:
            return "late", None
        return "missed", None
    started, ended, run = max(candidates, key=lambda item: item[0])
    if ended is None:
        return ("running" if now < window_end else "stalled"), run["run_id"]
    # Fail closed: an unknown terminal status is never a completed run.
    status = {"completed": "ok", "completed_with_gaps": "gaps", "partial": "gaps",
              "held": "held", "failed": "failed", "interrupted": "failed"}.get(run["status"], "failed")
    return status, run["run_id"]


def health(root, now=None, lookback=3, spec=None):
    """Health report for a private records root; never mutates anything.

    Only slots the calendar actually has are judged: a weekend (or any day the
    calendar skips) with no run is not a miss, and the report stays ``ok`` until
    the next real slot is due.
    """
    spec = spec or configured()
    now = _aware(now or datetime.now(ZoneInfo("UTC")), spec)
    root = Path(root)
    database = root / "ledger.sqlite"
    report = {"schema": "records-health-v1", "version": VERSION, "timezone": spec.timezone,
              "days": spec.days_spec, "schedule": [f"{h:02d}:{m:02d}" for h, m in spec.slots], "grace_minutes": int(GRACE.total_seconds() // 60),
              "now": now.isoformat(), "next_slot": next_slot(now, spec).isoformat(), "root": str(root)}
    if not database.exists():
        report.update(status="never", reason="ledger_missing", slots=[], exit_code=2)
        return report
    runs, open_alerts, blocked = _runs(database)
    report.update(open_alerts=open_alerts, blocked_stages=blocked,
                  last_run=({k: runs[0][k] for k in ("run_id", "started_at", "ended_at", "status")} if runs else None))
    if not runs:
        report.update(status="never", reason="no_pipeline_runs", slots=[], exit_code=1)
        return report
    first_started = min(_parse(run["started_at"], spec) for run in runs if run["started_at"])
    slots = []
    for slot in due_slots(now, lookback, spec):
        state, run_id = classify_slot(slot, runs, now, spec)
        if run_id is None and slot < first_started:
            state = "before_first_run"  # the schedule was not active yet; not a miss
        slots.append({"slot": slot.isoformat(), "status": state, "run_id": run_id})
    report["slots"] = slots
    latest = slots[0]["status"]
    missed = sum(1 for s in slots if s["status"] in ("missed", "failed", "stalled", "gaps", "held"))
    report["missed_in_lookback"] = missed
    if latest in ("ok", "running", "before_first_run"):
        status = "ok" if missed == 0 else "degraded"
    elif latest == "late":
        status = "late" if missed == 0 else "degraded"
    else:
        status = latest
    report["status"] = status
    report["exit_code"] = 0 if status in ("ok", "late") else 1
    return report


def record_health(root, report):
    """Persist missed/failed/stalled slots as keyed alerts so `records status` shows them."""
    database = Path(root) / "ledger.sqlite"
    if not database.exists():
        return 0
    stamp = datetime.now(ZoneInfo("UTC")).isoformat()
    written = 0
    with store.ledger(database) as con:
        for slot in report.get("slots", []):
            if slot["status"] not in ("missed", "failed", "stalled", "gaps", "held"):
                continue
            key = f"schedule:{slot['status']}:{slot['slot']}"
            con.execute("INSERT INTO alerts(id,key,first_seen,last_seen,count,owner,state) VALUES(?,?,?,?,1,'owner','open') "
                        "ON CONFLICT(key) DO UPDATE SET last_seen=excluded.last_seen,count=alerts.count+1,state='open'",
                        ("alert-" + key, key, stamp, stamp))
            written += 1
        con.commit()
    return written


# ----- unit rendering -------------------------------------------------------------------------

TIMER = """# Rendered by `records schedule render` ({version}). Review, then activate with ops/activate.sh.
[Unit]
Description=DeFlock records pipeline schedule ({label})

[Timer]
{on_calendar}Persistent=true
AccuracySec=1min
Unit={service}

[Install]
WantedBy=timers.target
"""

SERVICE = """# Rendered by `records schedule render` ({version}). One intake owner; no second timer.
[Unit]
Description=DeFlock records pipeline run
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory={engine_root}
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
TimeoutStartSec=2760
TimeoutStopSec=60
{environment}ExecStart={python} -m campaign_tool.records run --unattended --max-originals-per-run 200 --root {root}{mail}{ocr} --json
ExecStartPost={python} -m campaign_tool.records health --root {root}{calendar_flags} --record
"""

ACTIVATE = """#!/bin/sh
# Activate the records schedule as the ONLY intake owner. Review first with --dry-run.
# Usage: ops/activate.sh [--dry-run] [--retire LEGACY_TIMER ...]
set -eu
OPS="$(cd "$(dirname "$0")" && pwd)"
UNITS="${{XDG_CONFIG_HOME:-$HOME/.config}}/systemd/user"
DRY=0; RETIRE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --retire) shift; RETIRE="$RETIRE $1" ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
  shift
done
run() {{ if [ "$DRY" = 1 ]; then echo "+ $*"; else echo "+ $*"; "$@"; fi; }}
STATE="$OPS/activation-state.txt"
run mkdir -p "$UNITS"
if [ "$DRY" = 0 ]; then : > "$STATE"; fi
for legacy in $RETIRE; do
  enabled="$(systemctl --user is-enabled "$legacy" 2>/dev/null || true)"
  active="$(systemctl --user is-active "$legacy" 2>/dev/null || true)"
  if [ "$DRY" = 0 ]; then echo "$legacy enabled=$enabled active=$active" >> "$STATE"; fi
  run systemctl --user disable --now "$legacy"
done
run cp "$OPS/{service}" "$UNITS/{service}"
run cp "$OPS/{timer}" "$UNITS/{timer}"
run systemctl --user daemon-reload
run systemctl --user enable --now "{timer}"
run systemctl --user list-timers "{timer}" --no-pager
echo "activated {timer}; next run per list-timers above; health: {python} -m campaign_tool.records health --root {root}"
"""

ROLLBACK = """#!/bin/sh
# Roll back the records schedule: stop/disable the new timer, restore retired legacy timers.
# Usage: ops/rollback.sh [--dry-run]
set -eu
OPS="$(cd "$(dirname "$0")" && pwd)"
UNITS="${{XDG_CONFIG_HOME:-$HOME/.config}}/systemd/user"
DRY=0
[ "${{1:-}}" = "--dry-run" ] && DRY=1
run() {{ if [ "$DRY" = 1 ]; then echo "+ $*"; else echo "+ $*"; "$@"; fi; }}
STATE="$OPS/activation-state.txt"
run systemctl --user disable --now "{timer}"
run rm -f "$UNITS/{timer}" "$UNITS/{service}"
run systemctl --user daemon-reload
if [ -f "$STATE" ]; then
  while read -r unit flags; do
    [ -n "$unit" ] || continue
    case "$flags" in
      *enabled=enabled*) run systemctl --user enable --now "$unit" ;;
      *) echo "leaving $unit as it was ($flags)" ;;
    esac
  done < "$STATE"
fi
echo "rolled back {timer}; originals, ledger and checkpoints are untouched"
"""


def render(root, *, engine_root, python=None, mail_config=None, ocr=False, environment_file=None,
           spec=None):
    """Write resolved units and scripts into ``<root>/ops``; return their paths."""
    spec = spec or DEFAULT_SCHEDULE
    root = Path(os.path.abspath(root))
    ops = root / "ops"
    ops.mkdir(mode=0o700, parents=True, exist_ok=True)
    python = python or sys.executable
    values = {"version": VERSION, "label": spec.label(), "service": SERVICE_NAME, "timer": TIMER_NAME,
              "engine_root": shlex.quote(str(Path(engine_root).resolve())), "python": shlex.quote(python),
              "root": shlex.quote(str(root)),
              "mail": (" --mail-config " + shlex.quote(str(Path(mail_config).resolve()))) if mail_config else "",
              "ocr": " --ocr" if ocr else "",
              "on_calendar": "".join(f"OnCalendar={line}\n" for line in spec.on_calendar()),
              "calendar_flags": (f" --days {spec.days_spec} --times {spec.times_spec} --timezone {spec.timezone}"
                                 if spec != DEFAULT_SCHEDULE else ""),
              "environment": ("EnvironmentFile=" + shlex.quote(str(Path(environment_file).resolve())) + "\n") if environment_file else ""}
    files = {SERVICE_NAME: SERVICE, TIMER_NAME: TIMER, "activate.sh": ACTIVATE, "rollback.sh": ROLLBACK}
    written = {}
    for name, template in files.items():
        path = ops / name
        path.write_text(template.format(**values))
        os.chmod(path, 0o700 if name.endswith(".sh") else 0o600)
        written[name] = str(path)
    return written


# ----- CLI --------------------------------------------------------------------------------------

def _add_calendar_flags(parser):
    parser.add_argument("--days", help="run days: 'Mon..Fri' (default), 'Mon,Wed,Fri' or 'daily'; default $RECORDS_SCHEDULE_DAYS")
    parser.add_argument("--times", help="slot times HH:MM, comma-separated (default 07:00,15:00); default $RECORDS_SCHEDULE_TIMES")
    parser.add_argument("--timezone", help="IANA zone of the slot times (default America/Los_Angeles); default $RECORDS_SCHEDULE_TIMEZONE")


def _spec_from(parser, args):
    try:
        base = configured()
        return Schedule(args.days or base.days_spec, args.times or base.times_spec, args.timezone or base.timezone)
    except ScheduleError as error:
        parser.error(str(error))


def health_main(argv=None):
    parser = argparse.ArgumentParser(prog="records health", description="Schedule-derived health of a records root.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--now", help="ISO-8601 clock override for tests and reviews")
    parser.add_argument("--record", action="store_true", help="write missed/failed/stalled slots as ledger alerts")
    parser.add_argument("--json", action="store_true")
    _add_calendar_flags(parser)
    args = parser.parse_args(argv)
    now = datetime.fromisoformat(args.now) if args.now else None
    report = health(args.root, now=now, spec=_spec_from(parser, args))
    if args.record:
        report["alerts_recorded"] = record_health(args.root, report)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"records health: {report['status']} (next slot {report['next_slot']})")
        for slot in report.get("slots", []):
            print(f"  {slot['slot']}  {slot['status']}  {slot['run_id'] or ''}")
        if report.get("last_run"):
            print(f"  last run {report['last_run']['run_id']} {report['last_run']['status']} started {report['last_run']['started_at']}")
        print(f"  open alerts: {report.get('open_alerts', 0)}; blocked stages: {report.get('blocked_stages', 0)}")
    return report["exit_code"]


def schedule_main(argv=None):
    parser = argparse.ArgumentParser(prog="records schedule", description="Render schedule units; never activates.")
    sub = parser.add_subparsers(dest="command", required=True)
    render_ = sub.add_parser("render")
    render_.add_argument("--root", required=True)
    render_.add_argument("--engine-root", dest="engine_root", required=True, help="checkout or install root containing campaign_tool")
    render_.add_argument("--python", help="interpreter to run the engine with (default: this one)")
    render_.add_argument("--mail-config", dest="mail_config")
    render_.add_argument("--environment-file", dest="environment_file", help="owner-only env file for MODEL_* settings")
    render_.add_argument("--ocr", action="store_true")
    _add_calendar_flags(render_)
    show = sub.add_parser("show")
    show.add_argument("--now")
    _add_calendar_flags(show)
    args = parser.parse_args(argv)
    if args.command == "render":
        written = render(args.root, engine_root=args.engine_root, python=args.python, mail_config=args.mail_config,
                         ocr=args.ocr, environment_file=args.environment_file, spec=_spec_from(parser, args))
        print(json.dumps({"rendered": written, "activation": "ops/activate.sh --dry-run first; owner approval required",
                          "rollback": "ops/rollback.sh"}, sort_keys=True, indent=1))
        return 0
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(ZoneInfo("UTC"))
    spec = _spec_from(parser, args)
    print(json.dumps({"timezone": spec.timezone, "days": spec.days_spec, "slots": [f"{h:02d}:{m:02d}" for h, m in spec.slots],
                      "due": [s.isoformat() for s in due_slots(now, 3, spec)], "next": next_slot(now, spec).isoformat()}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(health_main())
