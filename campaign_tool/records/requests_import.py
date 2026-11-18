"""Seed request and agency identities from an operator-reviewed tracker (offline)."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import re
import sqlite3

from .ledger.store import checked_path, ledger
from .outcomes.register import read_tracker
from .run_stages import LedgerBusy, snapshot_ro


def _input_rows(tracker):
    rows, gaps = read_tracker(tracker, skip_blank=False)
    if gaps:
        raise ValueError(gaps[0])
    seen = set()
    for row in rows:
        name, ref = row["agency"], row["request_id"]
        number = row["source"]["row"]
        if not name or not ref:
            raise ValueError(f"tracker_row_{number}:missing_agency_or_request_id")
        if not name.isprintable() or not ref.isprintable() or len(ref) > 200:
            raise ValueError(f"tracker_row_{number}:invalid_identity")
        if ref.casefold() in seen:
            raise ValueError(f"tracker_row_{number}:duplicate_request_id")
        seen.add(ref.casefold())
    return rows


def _plan(db, rows):
    ids_by_name = defaultdict(list)
    names_by_id = {}
    for agency_id, name in db.execute("SELECT id, name FROM agencies"):
        key = name.strip().casefold()
        ids_by_name[key].append(agency_id)
        names_by_id[agency_id] = key
    agencies, requests = [], []
    for row in rows:
        name, ref = row["agency"], row["request_id"]
        number = row["source"]["row"]
        key = name.casefold()
        found = ids_by_name[key]
        if len(found) > 1:
            raise ValueError(f"tracker_row_{number}:ambiguous_agency")
        if found:
            agency_id = found[0]
        else:
            agency_id = re.sub(r"[\W_]+", "-", key).strip("-")[:60].rstrip("-")
            if not agency_id or agency_id in names_by_id:
                raise ValueError(f"tracker_row_{number}:agency_id_conflict")
            names_by_id[agency_id] = key
            found.append(agency_id)
            agencies.append((agency_id, name))

        existing = db.execute("SELECT agency_id, external_ref FROM requests WHERE id=?", (ref,)).fetchone()
        if existing is not None:
            if tuple(existing) != (agency_id, ref):
                raise ValueError(f"tracker_row_{number}:request_id_conflict")
            continue
        # An existing agency-scoped reference may use a different canonical ID.
        if db.execute("SELECT 1 FROM requests WHERE agency_id=? AND external_ref=?", (agency_id, ref)).fetchone():
            continue
        requests.append((ref, agency_id, ref, row.get("submitted_at") or None, row.get("status") or "unknown"))
    return agencies, requests


def run(project, tracker, *, apply=False):
    """Plan or atomically insert; never update existing rows or create a ledger."""
    rows = _input_rows(tracker)
    path = checked_path(Path(project) / "processing-v1" / "ledger.sqlite")
    if apply:
        with ledger(path) as db, db:
            db.execute("BEGIN IMMEDIATE")
            agencies, requests = _plan(db, rows)
            db.executemany("INSERT INTO agencies(id, name) VALUES(?, ?)", agencies)
            db.executemany("INSERT INTO requests(id, agency_id, external_ref, submitted_at, status) VALUES(?, ?, ?, ?, ?)",
                           requests)
    else:
        # mode=ro alone could create WAL sidecars, so use the engine's immutable reader.
        with snapshot_ro(path) as db:
            agencies, requests = _plan(db, rows)
    return {"mode": "apply" if apply else "dry-run", "tracker_rows": len(rows),
            "agencies_to_add": len(agencies), "requests_to_add": len(requests),
            "agencies_written": len(agencies) if apply else 0,
            "requests_written": len(requests) if apply else 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path, help="existing records project")
    parser.add_argument("--tracker", required=True, type=Path, help="operator-reviewed tracker CSV")
    parser.add_argument("--apply", action="store_true", help="insert the planned rows (default: dry-run)")
    args = parser.parse_args()
    try:
        result = run(args.project, args.tracker, apply=args.apply)
    except LedgerBusy:
        result = {"error": "requests_import_ledger_busy"}
    except (ValueError, OSError, csv.Error, sqlite3.Error) as exc:
        # Report tracker row numbers or fixed codes only; no CSV cells, paths or SQLite diagnostics.
        message = str(exc)
        code = message if re.fullmatch(r"tracker_[a-z0-9_]+(?::[a-z0-9_]+)?", message) else "requests_import_invalid_input"
        result = {"error": code}
    print(json.dumps(result, sort_keys=True))
    return int("error" in result)
