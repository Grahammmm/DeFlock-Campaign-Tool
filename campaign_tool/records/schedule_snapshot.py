"""Content-free schedule evidence from the host wrapper's validated run reports.

Only timestamps, fixed status codes and counts cross this boundary. The host
recomputes slots at its current clock; a saved 'ok' is never replayed as health.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from . import run_report, schedule

SCHEMA = "records-schedule-snapshot-v1"
MAX_BYTES = 128 * 1024
STATUSES = frozenset({"running", "completed", "completed_with_gaps", "partial", "held", "failed", "interrupted"})


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamp")
    return stamp


def validate(data, now):
    if not isinstance(data, dict) or set(data) != {"schema", "generated_at", "runs"}:
        raise ValueError("snapshot")
    if data["schema"] != SCHEMA or _stamp(data["generated_at"]) > now:
        raise ValueError("snapshot clock")
    generated = _stamp(data["generated_at"])

    if not isinstance(data["runs"], list) or len(data["runs"]) > 200:
        raise ValueError("runs")
    for run in data["runs"]:
        if not isinstance(run, dict) or set(run) != {"started_at", "ended_at", "status"}:
            raise ValueError("run")
        started = _stamp(run["started_at"])
        ended = _stamp(run["ended_at"]) if run["ended_at"] is not None else None
        if started > generated or (ended is not None and not started <= ended <= generated):
            raise ValueError("run clock")
        if not isinstance(run["status"], str) or run["status"] not in STATUSES:
            raise ValueError("status")
    return data


def export(reports, now=None):
    now = now or datetime.now(timezone.utc)
    runs = []
    names = sorted(Path(reports).glob("run_*.json"), reverse=True)[:200]
    for path in names:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("report size")
        report = json.loads(raw)
        run_report.validate_run_report(report)
        if report["mode"] == "test":
            continue
        status = {"ok": "completed", "warn": "completed_with_gaps", "fail": "failed"}[report["overall_status"]]
        runs.append({"started_at": report["started_at"], "ended_at": report["finished_at"], "status": status})
    data = {"schema": SCHEMA, "generated_at": now.isoformat(),
            "runs": runs}
    return validate(data, now)


def health(data, now, spec):
    validate(data, now)
    # Local numeric identities are used only by slot classification, never exported.
    runs = [dict(run, run_id=str(index)) for index, run in enumerate(data["runs"])]
    return schedule.health_from_runs(runs, 0, 0, now=now, spec=spec)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--reports", help="export metadata from this target's host run reports to stdout")
    group.add_argument("--out", help="validate stdin and atomically install this host snapshot")
    args = parser.parse_args(argv)
    try:
        if args.reports:
            print(json.dumps(export(args.reports), sort_keys=True))
        else:
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("size")
            data = validate(json.loads(raw), datetime.now(timezone.utc))
            path = Path(args.out)
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
    except Exception:
        print("schedule snapshot: refused schedule_unreadable", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
