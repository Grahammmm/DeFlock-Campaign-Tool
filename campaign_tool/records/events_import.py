"""Offline import of operator-staged agency events; private, review required."""
import argparse
from collections import Counter
import csv
from datetime import date
import io
import json
import os
from pathlib import Path
import re
import stat

from . import approval_queue as aq
from .console_manual_events import append_events, ManualError
from .outcomes import timeline as tl

COLUMNS = ("event_id", "date", "time_local", "agency", "request_ref", "event_type",
           "what_happened", "source", "who", "basis", "clock_note")
TYPE_MAP = {
    "request_sent": "sent", "acknowledged": "acknowledged",
    "extension_notice": "extension notice", "promised_date": "promised date",
    "determination": "determination", "determination_production": "production",
    "production": "production", "denial": "denial", "no_records": "no records",
    "fee_estimate": "fee estimate", "follow_up_sent": "our follow-up sent",
    "clarification_sent": "our follow-up sent", "reconsideration_sent": "our follow-up sent",
    "clarification_requested": "clarification requested", "call": "call",
    "portal_check": "portal check", "closed": "closed",
}
SKIP = frozenset(("clock_start", "clock_due", "overdue_mark", "production_link_fetched",
                  "system_record", "contract_executed", "request_received"))
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _need(ok):
    if not ok:
        raise ValueError("events_import_invalid_input")


def _rows(raw, columns, delimiter):
    _need(isinstance(raw, bytes) and len(raw) <= tl.MAX_INPUT_BYTES)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), delimiter=delimiter)
    _need(reader.fieldnames == list(columns))
    rows = list(reader)
    _need(len(rows) <= tl.MAX_MANUAL and all(None not in r and None not in r.values() for r in rows))
    return rows


def plan(tsv_bytes, request_map, agency_key):
    """Pure plan; request_map keys are (request_ref, agency_key), values request_id."""
    _need(isinstance(agency_key, str) and ID.fullmatch(agency_key))
    events, skipped, unmapped, refused, seen = [], 0, [], [], set()
    for number, row in enumerate(_rows(tsv_bytes, COLUMNS, "\t"), 1):
        ident = row["event_id"]
        safe_id = ident if ID.fullmatch(ident) else "row-" + str(number)
        if (not ID.fullmatch(ident) or ident in seen
                or any("<" in v or ">" in v for v in row.values())):
            refused.append(safe_id)
            continue
        seen.add(ident)
        ref, kind = row["request_ref"], row["event_type"]
        if kind in SKIP or ref == "context" or ref.endswith("(third party)"):
            skipped += 1
            continue
        request = request_map.get((ref, agency_key))
        if kind not in TYPE_MAP or not request:
            unmapped.append(ident)
            continue
        try:
            _need(re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"]))
            date.fromisoformat(row["date"])
            _need(isinstance(request, str) and bool(request.strip())
                  and all(c.isprintable() for c in request) and not any(c in request for c in "<>"))
        except ValueError:
            refused.append(ident)
            continue
        event = dict(event_id="tsv-" + agency_key + "-" + ident, agency_id=agency_key,
                     request_id=request, type=TYPE_MAP[kind], date=row["date"],
                     actor="events.tsv import", summary=row["what_happened"][:200])
        if kind == "promised_date":
            event["new_date"] = row["date"]
        events.append(event)
    return {"events": events, "mapped": dict(sorted(Counter(e["type"] for e in events).items())),
            "skipped": skipped, "unmapped": unmapped, "refused": refused}


def _read(root, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= tl.MAX_INPUT_BYTES)
        raw = stream.read(tl.MAX_INPUT_BYTES + 1)
    _need(len(raw) <= tl.MAX_INPUT_BYTES)
    return raw


def run(project, agency_key, *, apply=False, allow_unmapped=False):
    """Read only staged inputs; default dry run never creates a journal or lock."""
    _need(isinstance(agency_key, str) and ID.fullmatch(agency_key))
    project = Path(project)
    root = aq._open_chain(project / "agency-events")
    try:
        mapping = {}
        for row in _rows(_read(root, "request-map.csv"), ("request_ref", "agency_key", "request_id"), ","):
            key = (row["request_ref"], row["agency_key"])
            _need(all(row.values()) and key not in mapping
                  and not any("<" in v or ">" in v for v in row.values()))
            mapping[key] = row["request_id"]
        result = plan(_read(root, agency_key + ".tsv"), mapping, agency_key)
    finally:
        os.close(root)
    events = result.pop("events")
    known = {e["event_id"] for e in tl.read_manual_events(project / "manual-events.jsonl")}
    result.update(added=0, already_present=sum(e["event_id"] in known for e in events))
    result["blocked"] = bool(result["refused"] or (result["unmapped"] and not allow_unmapped))
    if apply and not result["blocked"] and events:
        result.update(append_events(project, events))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--agency-key", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-unmapped", action="store_true")
    args = parser.parse_args()
    try:
        result = run(args.project, args.agency_key, apply=args.apply, allow_unmapped=args.allow_unmapped)
    except (OSError, ValueError, csv.Error, aq.ApprovalQueueError, ManualError):
        print("events_import_invalid_input")
        return 1
    print(json.dumps(result, sort_keys=True))
    return int(result["blocked"])
