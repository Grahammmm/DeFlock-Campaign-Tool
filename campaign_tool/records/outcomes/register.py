"""Private, read-only request register. Dates/actions are review-required reminders.

Canonical requests and typed occurrence joins win; CSV fills absent fields only.
Mail subjects never establish membership, fulfillment, or a determination.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
import html
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from zoneinfo import ZoneInfo

from .. import correspondence
from ..intake.folder import secure_open
from ..ledger.store import checked_path

SCHEMA = "records-request-register-v1"
MAX_ROWS = 50000
MAX_CSV_BYTES = 8 * 1024 * 1024
# Explicit aliases, not substring guesses. Unknown columns are reported.
COLUMNS = {
    "agency": ("agency", "agency name", "agency_id"),
    "request_id": ("request id", "request_id", "request #", "request number", "external_ref"),
    "asked": ("what was asked", "request scope", "scope", "records requested", "request description"),
    "submitted_at": ("submitted_at", "date sent", "sent date", "request date", "date submitted"),
    "received_at": ("request received date", "agency received date"),
    "determination_at": ("determination_at", "determination date"),
    "determination_due_at": ("determination_due_at", "determination due", "response due", "deadline"),
    "production_at": ("production_at", "production date", "date received"),
    "arrived": ("what arrived", "records received", "production received"),
    "withheld": ("withheld", "records withheld", "withholding"),
    "missing": ("missing", "records missing", "outstanding", "outstanding items"),
    "extension": ("extension", "extension claimed", "14-day extension"),
    "status": ("status", "request status"),
}
LIMITS = [
    "Private; REVIEW REQUIRED. Recorded assertions are not semantic or legal acceptance.",
    "Gov. Code 7922.535: provisional determination reminder, not a production deadline.",
    "10 calendar days from recorded receipt (otherwise submission proxy); an explicitly recorded extension adds 14 days. Weekend last days roll forward to Monday (CCP 12a/12b). Receipt, holiday rules and lawful written extension require human verification.",
    "Mail/attachments prove recorded delivery only, not what responsive records were fulfilled. Unknown scope, withholding and missing items remain unknown.",
]


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _day(value):
    text = _text(value)
    try:
        # Do not accept a valid prefix followed by garbage.
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is not None:
            return moment.astimezone(ZoneInfo("America/Los_Angeles")).date()
        return None
    except (ValueError, OverflowError):
        for fmt in ("%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                pass
        return None


def read_tracker(path):
    """Missing is a coverage gap; unreadable/malformed is an error, never empty."""
    try:
        path = checked_path(path, existing=False)
        if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("tracker_not_regular")
        with secure_open(path) as source:
            raw = source.read(MAX_CSV_BYTES + 1)
    except FileNotFoundError:
        return [], ["tracker_missing"]
    if len(raw) > MAX_CSV_BYTES:
        raise ValueError("tracker_byte_limit")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), strict=True)
    names = reader.fieldnames or []
    normalized = [_text(n).casefold() for n in names]
    if not names or len(set(normalized)) != len(names):
        raise ValueError("tracker_header_invalid")
    mapping = {}
    for field, aliases in COLUMNS.items():
        matches = [names[i] for i, name in enumerate(normalized) if name in aliases]
        if len(matches) > 1:
            raise ValueError("tracker_column_ambiguous:" + field)
        if matches:
            mapping[field] = matches[0]
    gaps = ["tracker_column_missing:" + f for f in ("agency", "request_id") if f not in mapping]
    output = []
    for number, row in enumerate(reader, 2):
        if number > MAX_ROWS + 1:
            raise ValueError("tracker_row_limit")
        if None in row or any(v is None for v in row.values()):
            raise ValueError("tracker_row_invalid")
        if not any(_text(v) for v in row.values()):
            continue
        output.append({**{field: _text(row[column]) for field, column in mapping.items()},
                       "source": {"file": Path(path).name, "row": number}})
    return output, gaps


def read_ledger(path):
    """Reuse the run's immutable/no-active-WAL boundary; never initialize/migrate."""
    from ..run_stages import snapshot_ro
    checked_path(path)
    data, gaps = {}, []
    with snapshot_ro(Path(path)) as con:
        con.row_factory = sqlite3.Row
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, columns in correspondence.TABLES.items():
            if table not in tables:
                data[table] = []
                gaps.append("ledger_table_missing:" + table)
                continue
            actual = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            if not set(columns.split(",")) <= actual:
                # Legacy count-only snapshots have only id/sha; expose that limit.
                data[table] = []
                gaps.append("ledger_columns_missing:" + table)
                continue
            rows = con.execute(f"SELECT {columns} FROM {table} ORDER BY 1 LIMIT ?", (MAX_ROWS + 1,)).fetchall()
            if len(rows) > MAX_ROWS:
                raise ValueError("ledger_row_limit:" + table)
            data[table] = [dict(row) for row in rows]
    return data, gaps


def _roll_weekend(day):
    return day + timedelta(days=7 - day.weekday()) if day.weekday() >= 5 else day


def deadline(row, today, issues):
    """Running means a clock started with no recorded determination, even if overdue."""
    start = _day(row.get("received_at")) or _day(row.get("submitted_at"))
    determination = _day(row.get("determination_at"))
    extension_text = _text(row.get("extension")).casefold()
    extension = extension_text in ("yes", "true", "14", "14 days", "extended")
    if extension_text and extension_text not in ("no", "false", "0", "none") and not extension:
        issues.append("extension_unresolved")
    for field in ("received_at", "submitted_at", "determination_at", "determination_due_at", "production_at"):
        if row.get(field) and _day(row[field]) is None:
            issues.append("invalid_date:" + field)
    if start and start > today:
        issues.append("request_date_in_future")
    if determination and (determination > today or (start and determination < start)):
        issues.append("determination_date_inconsistent")
        determination = None
    base = _roll_weekend(start + timedelta(days=10)) if start else None
    maximum = _roll_weekend(start + timedelta(days=24)) if start else None
    computed = maximum if extension else base
    recorded = _day(row.get("determination_due_at"))
    # Keep the recorded date visible; do not silently treat a longer date as a lawful extension.
    if recorded and computed and recorded != computed:
        issues.append("recorded_due_differs_from_computed")
    state = ("determined" if determination else "unknown" if not computed else
             "not_started" if start > today else "past_due_reminder" if today > computed else
             "due_today" if today == computed else "running")
    return {"state": state, "running": None if state == "unknown" else state in ("running", "due_today", "past_due_reminder"),
            "base_due": base.isoformat() if base else None,
            "extended_due": maximum.isoformat() if maximum else None,
            "due": computed.isoformat() if computed else None,
            "recorded_due": recorded.isoformat() if recorded else None,
            "determination_at": determination.isoformat() if determination else None,
            "extension_recorded": extension,
            "basis": "recorded_receipt" if _day(row.get("received_at")) else "submission_proxy" if start else "unknown",
            "legal_timeliness": "not_assessed"}


def _subject(mail):
    try:
        value = json.loads(mail["headers"])
        if (value.get("schema") != "verified-eml-headers-v1" or
                value.get("source_original_sha256") != mail["eml_sha256"] or value.get("parser_defects")):
            return None
        subjects = [v for k, v in value["fields"] if k.casefold() == "subject" and isinstance(v, str)]
        return subjects[0] if len(subjects) == 1 else None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def build_register(data, tracker=(), *, as_of, gaps=()):
    today = _day(as_of)
    if today is None:
        raise ValueError("as_of_invalid")
    agencies = {a["id"]: a["name"] for a in data["agencies"]}
    alias = {}
    for aid, name in agencies.items():
        for key in (aid.casefold(), name.strip().casefold()):
            alias.setdefault(key, set()).add(aid)
    records = {}
    for request in data["requests"]:
        key = (request["agency_id"], request["external_ref"] or request["id"])
        if key in records:
            raise ValueError("ledger_request_identity_ambiguous")
        records[key] = {**request, "request_id": key[1], "agency": agencies.get(key[0], key[0]),
                        "ledger_id": request["id"], "sources": [{"table": "requests", "id": request["id"]}], "issues": []}
    for number, item in enumerate(tracker):
        agency = _text(item.get("agency"))
        matches = alias.get(agency.casefold(), set())
        aid = next(iter(matches)) if len(matches) == 1 else "tracker:" + agency.casefold()
        rid = _text(item.get("request_id"))
        key = (aid, rid or f"unknown-row-{number + 2}")
        row = records.setdefault(key, {"agency": agency or "Unknown agency", "request_id": key[1],
                                       "sources": [], "issues": [], "ledger_id": None})
        row["sources"].append(item["source"])
        if not agency or not rid or len(matches) > 1:
            row["issues"].append("tracker_identity_unresolved")
        for field, value in item.items():
            if field in ("agency", "request_id", "source") or not value:
                continue
            if row.get(field) and row[field] != value:
                # Ledger takes precedence; repeated tracker rows do not silently erase conflicts.
                if not (_day(row[field]) and _day(row[field]) == _day(value)):
                    row["issues"].append("source_conflict:" + field)
            else:
                row[field] = value
    join_gaps = []
    members, _, occurrences, originals, excluded = correspondence._request_membership(data, join_gaps)
    messages = {}
    for mail in data["mail_messages"]:
        messages.setdefault(mail["eml_sha256"], []).append(mail)
    output = []
    for key, row in sorted(records.items()):
        issues = row["issues"]
        clock = deadline(row, today, issues)
        deliveries = []
        for oid, occurrence in sorted(occurrences.items()):
            if row["ledger_id"] is None or row["ledger_id"] not in members[oid]:
                continue
            deliveries.append({"occurrence_id": oid, "original_sha256": occurrence["original_sha256"],
                               "kind": occurrence["kind"], "arrived_at": occurrence["acquired_at"],
                               "mime": originals[occurrence["original_sha256"]]["mime_detected"],
                               "mail": [{"id": m["id"], "received_at": m["received_at"], "subject": _subject(m)}
                                        for m in messages.get(occurrence["original_sha256"], [])]})
        asked = row.get("asked") or "Unknown — request scope not recorded"
        withheld = row.get("withheld") or "Unknown — not assessed"
        missing = row.get("missing") or "Unknown — fulfillment not assessed"
        status = _text(row.get("status")).casefold()
        outstanding = any(_text(row.get(f)).casefold() not in ("", "none", "no", "nothing", "0")
                          for f in ("withheld", "missing"))
        closed = status in ("complete", "completed", "closed", "withdrawn", "fulfilled")
        if issues or not row.get("asked") or clock["state"] in ("unknown", "not_started"):
            action, reason = "clarify", "Resolve missing/conflicting scope, identity or dates."
        elif status in ("clarification requested", "needs clarification", "clarify"):
            action, reason = "clarify", "Recorded request status calls for clarification."
        elif outstanding:
            action, reason = "follow up", "Recorded withheld or outstanding items need review."
        elif closed:
            action, reason = "nothing", "Tracker/ledger records closure; fulfillment not independently assessed."
        elif clock["state"] in ("past_due_reminder", "due_today"):
            action, reason = "follow up", "Provisional determination reminder reached; verify receipt and holiday rules."
        elif clock["state"] == "running":
            action, reason = "wait", "Provisional determination period is still running."
        elif status in ("open", "pending", "partial", "awaiting production", "determined"):
            action, reason = "follow up", "Determination recorded; verify outstanding production (no statutory production date inferred)."
        else:
            action, reason = "clarify", "Determine production/closure status; delivery alone is not fulfillment."
        output.append({"agency": row["agency"], "agency_key": key[0], "request_id": row["request_id"],
                       "ledger_request_id": row["ledger_id"], "asked": asked,
                       "arrived": deliveries, "reported_arrived": row.get("arrived") or "Not recorded",
                       "production_at": row.get("production_at"), "withheld": withheld, "missing": missing,
                       "status": row.get("status") or "unknown", "deadline": clock,
                       "next_action": action, "action_reason": reason,
                       "sources": row["sources"], "issues": sorted(set(issues)), "fulfillment": "not_assessed"})
    return {"schema": SCHEMA, "as_of": today.isoformat(), "visibility": "private", "review_required": True,
            "requests": output, "request_count": len(output), "gaps": sorted(set(gaps)),
            "binding_gaps": join_gaps,
            "unassigned_occurrences": sum(not members[oid] for oid in occurrences if oid not in excluded),
            "limits": LIMITS}


def markdown(report):
    def cell(value):
        return html.escape(str(value)).replace("|", "&#124;").replace("\r", " ").replace("\n", "<br>")
    lines = ["# Current request register", "", f"As of {report['as_of']} (Pacific date). {report['request_count']} requests. Private; REVIEW REQUIRED.", "",
             "| Agency / request | Asked | Arrived / when | Withheld / missing | Determination reminder | Next action | Sources |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in report["requests"]:
        deliveries = "; ".join(
            f"{d['kind']} ({d['mime'] or 'unknown type'}) {d['occurrence_id']} at {d['arrived_at'] or 'unknown date'}"
            + "".join(f"; mail subject: {m['subject']}" for m in d["mail"] if m["subject"])
            for d in row["arrived"])
        arrival = row["reported_arrived"] + ("; " + deliveries if deliveries else "")
        if row["production_at"]:
            arrival += "; recorded production: " + row["production_at"]
        due = row["deadline"]
        clock = f"{due['state']}; due {due['due'] or 'unknown'}; base {due['base_due'] or 'unknown'}; +14 {due['extended_due'] or 'unknown'}"
        if due["recorded_due"]:
            clock += "; recorded due " + due["recorded_due"]
        if row["issues"]:
            clock += "; CHECK: " + ", ".join(row["issues"])
        citations = "; ".join(f"ledger requests:{s['id']}" if "table" in s else f"{s['file']}:row {s['row']}" for s in row["sources"])
        cells = [row["agency"] + " / " + row["request_id"], row["asked"], arrival,
                 row["withheld"] + " / " + row["missing"], clock,
                 row["next_action"] + ": " + row["action_reason"], citations]
        lines.append("| " + " | ".join(cell(c) for c in cells) + " |")
    lines += ["", "Coverage gaps: " + (", ".join(report["gaps"]) or "none"),
              f"Unassigned deliveries: {report['unassigned_occurrences']}; binding gaps: {len(report['binding_gaps'])}.", ""]
    lines += ["- " + text for text in report["limits"]]
    return "\n".join(lines) + "\n"


def write_for_run(project, run_id, *, as_of=None):
    """Write both private files under run-outputs/<run_id>/, never canonical state.

    Assemble both before publishing the directory; an identical replay is a
    no-op, while changed runs are refused. Failed staging residue is retained.
    """
    from ..run_stages import LedgerBusy
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id):
        raise OSError("register_run_id_invalid")
    try:
        project = checked_path(project, existing=False)
        data, gaps = read_ledger(project / "processing-v1" / "ledger.sqlite")
        tracker, tracker_gaps = read_tracker(project / "MASTER-REQUEST-TRACKER.csv")
        report = build_register(data, tracker, as_of=as_of or datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat(),
                                gaps=gaps + tracker_gaps)
        payloads = {"CURRENT-REQUEST-REGISTER.json": json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    "CURRENT-REQUEST-REGISTER.md": markdown(report)}
        directory = checked_path(project / "run-outputs", existing=False)
        directory.mkdir(mode=0o700, exist_ok=True)
        target = checked_path(directory / run_id, existing=False)
        if target.exists():
            # Exact replay stays compatible with the outputs stage; changed runs
            # cannot overwrite earlier evidence under the same run identifier.
            for name, text in payloads.items():
                expected = text.encode("utf-8")
                with secure_open(checked_path(target / name)) as source:
                    if source.read(len(expected) + 1) != expected:
                        raise OSError("register_run_already_exists")
            return report
        staging = Path(tempfile.mkdtemp(prefix=".register-", dir=directory))
        for name, text in payloads.items():
            fd = os.open(staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
        # Refuse occupied/symlink target again before publishing; rename refuses nonempty directories.
        checked_path(target, existing=False)
        if target.exists():
            raise OSError("register_run_already_exists")
        os.rename(staging, target)
        return report
    except (ValueError, UnicodeError, csv.Error, sqlite3.Error, LedgerBusy) as error:
        raise OSError("register_source_unavailable_or_invalid") from error
