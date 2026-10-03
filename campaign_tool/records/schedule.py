"""Schedule rendering and health for the records pipeline.

The approved service promise is three ``records run`` invocations per day at
07:00, 13:00 and 20:30 America/Los_Angeles (DST follows the zone). Health is
derived from that promise and the ledger's ``runs`` rows, never from directory
timestamps or from a fixed "hours since" threshold:

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
import shlex
import sys
from zoneinfo import ZoneInfo

from .ledger import store

TIMEZONE = "America/Los_Angeles"
SLOTS = ((7, 0), (13, 0), (20, 30))
GRACE = timedelta(minutes=45)
RUN_KINDS = ("stage-runner",)
TIMER_NAME = "records-run.timer"
SERVICE_NAME = "records-run.service"
VERSION = "records-schedule-v1"


def _zone():
    return ZoneInfo(TIMEZONE)


def _aware(value):
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(_zone())


def due_slots(now, count=3):
    """The ``count`` most recent slots at or before ``now``, newest first, in the zone."""
    now = _aware(now)
    day = now.date()
    found = []
    for back in range(0, 4):
        date_ = day - timedelta(days=back)
        for hour, minute in sorted(SLOTS, reverse=True):
            slot = datetime(date_.year, date_.month, date_.day, hour, minute, tzinfo=_zone())
            if slot <= now:
                found.append(slot)
                if len(found) == count:
                    return found
    return found


def next_slot(now):
    now = _aware(now)
    for ahead in range(0, 3):
        date_ = now.date() + timedelta(days=ahead)
        for hour, minute in sorted(SLOTS):
            slot = datetime(date_.year, date_.month, date_.day, hour, minute, tzinfo=_zone())
            if slot > now:
                return slot
    raise AssertionError("unreachable")


def _parse(stamp):
    if not stamp:
        return None
    value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(_zone())


def _runs(database):
    with store.ledger(database, readonly=True) as con:
        placeholders = ",".join("?" for _ in RUN_KINDS)
        rows = con.execute(f"SELECT run_id,kind,started_at,ended_at,status FROM runs WHERE kind IN ({placeholders}) "
                           "ORDER BY started_at DESC LIMIT 200", RUN_KINDS).fetchall()
        open_alerts = con.execute("SELECT count(*) FROM alerts WHERE state='open'").fetchone()[0]
        blocked = con.execute("SELECT count(*) FROM stage_state WHERE status='blocked'").fetchone()[0]
    return [dict(row) for row in rows], open_alerts, blocked


def classify_slot(slot, runs, now):
    """Status of one due slot given the ledger's runs (newest first) and the clock.

    A run belongs to the slot when it started between the slot and the following
    slot, or when it started earlier (a Persistent catch-up) and finished inside
    that window. Runs for a later slot never count for an earlier one.
    """
    window_end = slot + GRACE
    following = min(next_slot(slot), now) if next_slot(slot) > slot else now
    candidates = []
    for run in runs:
        started = _parse(run["started_at"])
        ended = _parse(run["ended_at"])
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


def health(root, now=None, lookback=3):
    """Health report for a private records root; never mutates anything."""
    now = _aware(now or datetime.now(ZoneInfo("UTC")))
    root = Path(root)
    database = root / "ledger.sqlite"
    report = {"schema": "records-health-v1", "version": VERSION, "timezone": TIMEZONE,
              "schedule": [f"{h:02d}:{m:02d}" for h, m in SLOTS], "grace_minutes": int(GRACE.total_seconds() // 60),
              "now": now.isoformat(), "next_slot": next_slot(now).isoformat(), "root": str(root)}
    if not database.exists():
        report.update(status="never", reason="ledger_missing", slots=[], exit_code=2)
        return report
    runs, open_alerts, blocked = _runs(database)
    report.update(open_alerts=open_alerts, blocked_stages=blocked,
                  last_run=({k: runs[0][k] for k in ("run_id", "started_at", "ended_at", "status")} if runs else None))
    if not runs:
        report.update(status="never", reason="no_pipeline_runs", slots=[], exit_code=1)
        return report
    first_started = min(_parse(run["started_at"]) for run in runs if run["started_at"])
    slots = []
    for slot in due_slots(now, lookback):
        state, run_id = classify_slot(slot, runs, now)
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
Description=DeFlock records pipeline schedule (07:00, 13:00, 20:30 {timezone})

[Timer]
OnCalendar=*-*-* 07:00:00 {timezone}
OnCalendar=*-*-* 13:00:00 {timezone}
OnCalendar=*-*-* 20:30:00 {timezone}
Persistent=true
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
ExecStartPost={python} -m campaign_tool.records health --root {root} --record
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


def render(root, *, engine_root, python=None, mail_config=None, ocr=False, environment_file=None):
    """Write resolved units and scripts into ``<root>/ops``; return their paths."""
    root = Path(os.path.abspath(root))
    ops = root / "ops"
    ops.mkdir(mode=0o700, parents=True, exist_ok=True)
    python = python or sys.executable
    values = {"version": VERSION, "timezone": TIMEZONE, "service": SERVICE_NAME, "timer": TIMER_NAME,
              "engine_root": shlex.quote(str(Path(engine_root).resolve())), "python": shlex.quote(python),
              "root": shlex.quote(str(root)),
              "mail": (" --mail-config " + shlex.quote(str(Path(mail_config).resolve()))) if mail_config else "",
              "ocr": " --ocr" if ocr else "",
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

def health_main(argv=None):
    parser = argparse.ArgumentParser(prog="records health", description="Schedule-derived health of a records root.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--now", help="ISO-8601 clock override for tests and reviews")
    parser.add_argument("--record", action="store_true", help="write missed/failed/stalled slots as ledger alerts")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.fromisoformat(args.now) if args.now else None
    report = health(args.root, now=now)
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
    show = sub.add_parser("show")
    show.add_argument("--now")
    args = parser.parse_args(argv)
    if args.command == "render":
        written = render(args.root, engine_root=args.engine_root, python=args.python, mail_config=args.mail_config,
                         ocr=args.ocr, environment_file=args.environment_file)
        print(json.dumps({"rendered": written, "activation": "ops/activate.sh --dry-run first; owner approval required",
                          "rollback": "ops/rollback.sh"}, sort_keys=True, indent=1))
        return 0
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(ZoneInfo("UTC"))
    print(json.dumps({"timezone": TIMEZONE, "slots": [f"{h:02d}:{m:02d}" for h, m in SLOTS],
                      "due": [s.isoformat() for s in due_slots(now)], "next": next_slot(now).isoformat()}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(health_main())
