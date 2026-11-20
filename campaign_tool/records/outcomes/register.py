"""Private, read-only request register. Dates/actions are review-required reminders.

Canonical requests and typed occurrence joins win; CSV fills absent fields only.
Mail subjects never establish membership, fulfillment, or a determination.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
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

from ... import law
from .. import correspondence
from ..intake.folder import secure_open
from ..ledger.store import checked_path

SCHEMA = "records-request-register-v1"
MAX_ROWS = 50000
MAX_CSV_BYTES = 8 * 1024 * 1024
HOLIDAY_FILE = "holidays.csv"  # optional custodian input, never shipped
MAX_HOLIDAY_BYTES = 64 * 1024
MAX_HOLIDAYS = 1000
# Explicit aliases, not substring guesses. Unknown columns are reported.
COLUMNS = {
    "agency": ("agency", "agency name", "agency_id"),
    "request_id": ("request id", "request_id", "request #", "request number", "external_ref"),
    "asked": ("what was asked", "request scope", "scope", "records requested", "request description"),
    "submitted_at": ("submitted_at", "submitted", "date sent", "sent date", "request date", "date submitted"),
    "received_at": ("request received date", "agency received date"),
    "determination_at": ("determination_at", "determination date"),
    "determination_due_at": ("determination_due_at", "determination due", "response due", "deadline"),
    "production_at": ("production_at", "production date", "date received"),
    "arrived": ("what arrived", "records received", "production received"),
    "withheld": ("withheld", "records withheld", "withholding"),
    "missing": ("missing", "records missing", "outstanding", "outstanding items"),
    "extension": ("extension", "extension claimed", "14-day extension"),
    "status": ("status", "request status"),
    "promised_at": ("promised_at", "agency promised date", "promised date", "expected production date",
                    "estimated production date"),
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


def read_tracker(path, *, skip_blank=True):
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
        if skip_blank and not any(_text(v) for v in row.values()):
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


def production_admission(value, today, start):
    """Admit a recorded production date as completed-event evidence: (day, issue).

    Absent -> (None, None). Unparsable -> invalid_date. After the report day or before the request
    start (when known) -> production_date_inconsistent. Both bounds are inclusive. Pure.
    """
    text = _text(value)
    if not text:
        return None, None
    day = _day(text)
    if day is None:
        return None, "invalid_date:production_at"
    if day > today or (start and day < start):
        return None, "production_date_inconsistent"
    return day, None


def _roll_weekend(day, holidays=()):
    """First day on or after ``day`` that is neither Saturday/Sunday nor a custodian-listed holiday."""
    while day.weekday() >= 5 or day in holidays:
        day += timedelta(days=1)
    return day


def read_holidays(project):
    """Optional custodian PROJECT/holidays.csv -> (state, frozenset of dates, sha256 prefix).

    State is absent (no file), used (every row valid) or invalid (anything else; no partial list).
    The prefix is the first 12 hex digits of the bounded raw bytes, None when they were unreadable.
    """
    from ..followup_clock import _read_bounded  # bounded, no-follow, nonblocking regular-file reader
    try:
        raw = _read_bounded(checked_path(Path(project) / HOLIDAY_FILE, existing=False), MAX_HOLIDAY_BYTES)
    except FileNotFoundError:
        return "absent", frozenset(), None
    except (OSError, ValueError):
        return "invalid", frozenset(), None
    prefix = hashlib.sha256(raw).hexdigest()[:12]
    try:
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), strict=True))
        days = [date.fromisoformat(r[0]) for r in rows[1:] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", r[0])]
        if (not rows or rows[0] != ["date", "name", "source"] or len(rows) > MAX_HOLIDAYS + 1
                or any(len(r) != 3 or not r[1].strip() or not r[2].strip() for r in rows[1:])
                or len(days) != len(rows) - 1 or len(set(days)) != len(days)):
            raise ValueError("holiday_list_invalid")
    except (ValueError, IndexError, UnicodeError, csv.Error):
        return "invalid", frozenset(), prefix
    return "used", frozenset(days), prefix


def deadline(row, today, issues, holidays=()):
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
    issue = production_admission(row.get("production_at"), today, start)[1]
    if issue:
        issues.append(issue)
    if determination and (determination > today or (start and determination < start)):
        issues.append("determination_date_inconsistent")
        determination = None
    base = _roll_weekend(start + timedelta(days=10), holidays) if start else None
    maximum = _roll_weekend(start + timedelta(days=24), holidays) if start else None
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


def promised(row, today, issues):
    """The agency's own promised date. A production on or after it satisfies the promise."""
    text = _text(row.get("promised_at"))
    if not text:
        return {"state": "none", "date": None}
    day = _day(text)
    if day is None:
        issues.append("invalid_date:promised_at")
        return {"state": "invalid", "date": None}
    start = _day(row.get("received_at")) or _day(row.get("submitted_at"))
    produced, issue = production_admission(row.get("production_at"), today, start)
    if issue:
        issues.append(issue)
    if produced and produced >= day:
        return {"state": "none", "date": day.isoformat()}
    state = "passed" if day < today else "due_today" if day == today else "upcoming"
    return {"state": state, "date": day.isoformat()}


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


PACIFIC = ZoneInfo("America/Los_Angeles")
SENT_FOLDERS = frozenset({"sent", "sent mail", "sent items", "sent messages"})


def _folder_direction(folder):
    """sent (ours), incoming (INBOX tree) or None (cannot be established)."""
    if not isinstance(folder, str) or not folder.strip():
        return None
    parts = [p.strip().casefold() for p in re.split(r"[/.]", folder.strip())]
    if parts[-1] in SENT_FOLDERS:
        return "sent"
    return "incoming" if "inbox" in parts else None


def _instant(value):
    try:
        return correspondence.timestamp(value)
    except correspondence.CorrespondenceError:
        return None


def answer_arrived(deliveries, occurrences, mail_rows, start, determination_text):
    """Classify bound deliveries as an unrecorded agency answer. Pure; never a determination.

    Our own sent copy never counts. A delivery whose direction or time cannot be established
    makes the state unknown rather than guessing.
    """
    result = {"state": "none", "first_arrival": None, "occurrence_ids": []}
    if determination_text:
        return result
    mails = {m["id"]: m for m in mail_rows}
    qualified, unresolved = [], False
    for delivery in deliveries:
        oid = delivery["occurrence_id"]
        occurrence = occurrences[oid]
        kind = occurrence["kind"]
        if kind == "portal":
            stamp = occurrence["acquired_at"]
        elif kind in ("mail", "attachment"):
            root, seen = occurrence, set()
            while root["kind"] == "attachment" and root["id"] not in seen and root["parent_occurrence_id"] in occurrences:
                seen.add(root["id"])
                root = occurrences[root["parent_occurrence_id"]]
            mail = mails.get(root["id"]) if root["kind"] == "mail" else None
            if mail is None or mail.get("eml_sha256") != root["original_sha256"]:
                unresolved = True
                continue
            way = _folder_direction(mail.get("folder"))
            if way == "sent":
                continue
            if way is None:
                unresolved = True
                continue
            stamp = mail.get("received_at")
        else:
            unresolved = True
            continue
        moment = _instant(stamp)
        if moment is None or start is None:
            unresolved = True
            continue
        if moment.astimezone(PACIFIC).date() >= start:
            qualified.append((moment, oid))
    if qualified:
        first = min(m for m, _ in qualified).astimezone(timezone.utc)
        result["first_arrival"] = first.replace(tzinfo=None).isoformat() + "Z"
        result["occurrence_ids"] = sorted({oid for _, oid in qualified})
    result["state"] = "unknown" if unresolved else "arrived_unrecorded" if qualified else "none"
    return result


def build_register(data, tracker=(), *, as_of, gaps=(), holiday=("absent", frozenset(), None)):
    """``holiday`` is read_holidays' result; absent leaves every output byte-identical to the legacy register."""
    today = _day(as_of)
    if today is None:
        raise ValueError("as_of_invalid")
    holiday_state, holidays, holiday_prefix = holiday
    if holiday_state == "invalid":
        gaps = [*gaps, "holiday_list_invalid"]
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
        if holiday_state == "invalid":
            issues.append("holiday_list_invalid")
        clock = deadline(row, today, issues, holidays)
        promise = promised(row, today, issues)
        started = _day(row.get("received_at")) or _day(row.get("submitted_at"))
        deliveries = []
        for oid, occurrence in sorted(occurrences.items()):
            if row["ledger_id"] is None or row["ledger_id"] not in members[oid]:
                continue
            deliveries.append({"occurrence_id": oid, "original_sha256": occurrence["original_sha256"],
                               "kind": occurrence["kind"], "arrived_at": occurrence["acquired_at"],
                               "mime": originals[occurrence["original_sha256"]]["mime_detected"],
                               "mail": [{"id": m["id"], "received_at": m["received_at"], "subject": _subject(m)}
                                        for m in messages.get(occurrence["original_sha256"], [])]})
        arrival = answer_arrived(deliveries, occurrences, data["mail_messages"], started,
                                 _text(row.get("determination_at")))
        if arrival["state"] == "unknown":
            issues.append("arrival_unresolved")
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
        elif arrival["state"] == "arrived_unrecorded" and not closed:
            action = "record answer"
            reason = (f"An agency reply arrived on {_instant(arrival['first_arrival']).astimezone(PACIFIC).date().isoformat()}; "
                      "record the determination, extension or production before any reminder")
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
                       "promised": promise, "request_date": started.isoformat() if started else None,
                       "answer_arrived": arrival,
                       "next_action": action, "action_reason": reason,
                       "sources": row["sources"], "issues": sorted(set(issues)), "fulfillment": "not_assessed"})
        if holiday_state != "absent":
            output[-1].update(holiday_list=holiday_state, holiday_list_sha256_prefix=holiday_prefix)
    report = {"schema": SCHEMA, "as_of": today.isoformat(), "visibility": "private", "review_required": True,
              "requests": output, "request_count": len(output), "gaps": sorted(set(gaps)),
              "binding_gaps": join_gaps,
              "unassigned_occurrences": sum(not members[oid] for oid in occurrences if oid not in excluded),
              "limits": LIMITS}
    if holiday_state != "absent":
        # Private run snapshot of the custodian's list (dates only, no names or sources) for the calculator.
        report["holiday_list"] = {"state": holiday_state, "sha256_prefix": holiday_prefix}
        report["limits"] = [*LIMITS, law.CUSTODIAN_HOLIDAY_NOTE if holiday_state == "used"
                            else law.CUSTODIAN_HOLIDAY_INVALID_NOTE]
        if holiday_state == "used":
            report["holiday_list"]["dates"] = sorted(d.isoformat() for d in holidays)
    return report


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
        if row["promised"]["state"] not in ("none", "invalid"):
            clock += f"; promised {row['promised']['date']} ({row['promised']['state']})"
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
                                gaps=gaps + tracker_gaps, holiday=read_holidays(project))
        payloads = {"CURRENT-REQUEST-REGISTER.json": json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    "CURRENT-REQUEST-REGISTER.md": markdown(report)}
        directory = checked_path(project / "run-outputs", existing=False)
        directory.mkdir(mode=0o700, exist_ok=True)
        target = checked_path(directory / run_id, existing=False)
        if target.exists():
            # The per-run directory is shared with the fact-sheet and comparison
            # outputs; only this register's own pair is verified or published.
            present = [name for name in payloads if os.path.lexists(target / name)]
            if present:
                # Exact replay stays compatible with the outputs stage; changed or
                # partial runs cannot overwrite earlier evidence under the same id.
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
        if not target.exists():
            os.rename(staging, target)
            return report
        if not target.is_dir():
            raise OSError("register_run_already_exists")
        # Shared directory: link both files without clobbering, and withdraw a
        # half-published pair so a partial register is never left behind.
        published = []
        try:
            for name in payloads:
                os.link(staging / name, target / name)
                published.append(target / name)
        except OSError:
            for path in published:
                os.unlink(path)
            raise
        for name in payloads:
            os.unlink(staging / name)
        os.rmdir(staging)
        return report
    except (ValueError, UnicodeError, csv.Error, sqlite3.Error, LedgerBusy) as error:
        raise OSError("register_source_unavailable_or_invalid") from error
