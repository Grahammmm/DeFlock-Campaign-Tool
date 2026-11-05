"""Check a proposed MASTER-REQUEST-TRACKER.csv before a person applies it to a live project.

Read-only. Nothing is sent, fetched or written. A clean result means the file is well formed and every
source id resolves; it does not mean the facts are right. Every row stays review-required.

    python3 -m campaign_tool.records.tracker_proposal FILE [--mail-index INDEX] [--expect-rows N]

INDEX is a tab- or comma-separated file with ``folder`` and ``uid`` columns (the mail store's own message
index). Without it, source ids are checked for shape only.
"""
import argparse
import csv
import datetime
import re
import sys

REQUIRED = (
    "request_id", "agency", "scope", "submitted_at", "agency received date", "receipt_basis",
    "extension", "promised_dates", "status", "asked_items", "owner", "next_action",
    "source_message_ids", "non_mail_sources",
)
BASES = ("recorded_receipt", "submission_proxy")
STATUSES = ("waiting_on_agency", "ball_in_our_court", "closed_by_agency", "unknown")
NO_MAIL = "none-in-mail-store"
_ID = re.compile(r"^([A-Za-z][\w-]*):(\d+)(?:\.\.(\d+))?$")
_ITEM = re.compile(r"^(?:(\d+) \[T([123?])\] \S|unnumbered \[T\?\] \S)")


def _date(value):
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def expand_ids(text):
    """Return (set of (folder, uid), problems) for ``Sent:3; INBOX:70..87``."""
    found, problems = set(), []
    for token in (t.strip() for t in text.split(";")):
        if not token:
            continue
        m = _ID.match(token)
        if not m or (m.group(3) and int(m.group(3)) < int(m.group(2))):
            problems.append("source_id_malformed:" + token)
            continue
        low = int(m.group(2))
        for uid in range(low, int(m.group(3) or low) + 1):
            found.add((m.group(1), uid))
    return found, problems


def load_mail_index(path):
    with open(path, newline="", encoding="utf-8") as fh:
        text = fh.read()
    dialect = csv.excel_tab if "\t" in text.splitlines()[0] else csv.excel
    rows = csv.DictReader(text.splitlines(), dialect=dialect)
    if not rows.fieldnames or "folder" not in rows.fieldnames or "uid" not in rows.fieldnames:
        raise ValueError("mail_index_columns_missing")
    return {(r["folder"], int(r["uid"])) for r in rows if (r.get("uid") or "").isdigit()}


def _check_items(text):
    problems, expected = [], 1
    for part in (p.strip() for p in text.split("||")):
        m = _ITEM.match(part)
        if not m:
            problems.append("asked_item_malformed:" + part[:40])
        elif m.group(1):
            if int(m.group(1)) != expected:
                problems.append("asked_item_number:%s!=%d" % (m.group(1), expected))
            expected += 1
    return problems


def check_proposal(path, mail_index=None):
    """Return (rows, problems). Problems are strings like ``26-133:receipt_basis_invalid``."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, strict=True)
        names = reader.fieldnames or []
        missing = [c for c in REQUIRED if c not in names]
        if missing:
            return [], ["header_missing:" + c for c in missing]
        rows = list(reader)
    problems, seen = [], set()
    for number, row in enumerate(rows, 2):
        rid = (row["request_id"] or "").strip()
        tag = rid or "row%d" % number

        def bad(code):
            problems.append("%s:%s" % (tag, code))

        if not rid:
            bad("request_id_blank")
        elif rid.casefold() in seen:
            bad("request_id_duplicate")
        seen.add(rid.casefold())
        for col in ("agency", "scope", "extension", "promised_dates", "owner", "next_action"):
            if not (row[col] or "").strip():
                bad(col + "_blank")
        sent, got = _date(row["submitted_at"]), _date(row["agency received date"])
        if sent is None:
            bad("submitted_at_not_iso_date")
        if got is None:
            bad("agency_received_date_not_iso_date")
        if sent and got and got < sent - datetime.timedelta(days=1):
            bad("received_before_sent")
        if row["receipt_basis"] not in BASES:
            bad("receipt_basis_invalid")
        if row["status"] not in STATUSES:
            bad("status_invalid")
        problems += [tag + ":" + p for p in _check_items(row["asked_items"] or "")]
        if not (row["asked_items"] or "").strip():
            bad("asked_items_blank")
        raw_ids = (row["source_message_ids"] or "").strip()
        if raw_ids == NO_MAIL:
            if not (row["non_mail_sources"] or "").strip():
                bad("no_mail_source_without_other_source")
        else:
            ids, errs = expand_ids(raw_ids)
            problems += [tag + ":" + e for e in errs]
            if not ids and not errs:
                bad("source_message_ids_blank")
            if mail_index is not None:
                problems += ["%s:source_id_not_in_mail_index:%s:%d" % (tag, f, u) for f, u in sorted(ids - mail_index)]
    return rows, problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--mail-index")
    ap.add_argument("--expect-rows", type=int)
    args = ap.parse_args(argv)
    index = load_mail_index(args.mail_index) if args.mail_index else None
    rows, problems = check_proposal(args.file, index)
    if args.expect_rows is not None and len(rows) != args.expect_rows:
        problems.append("row_count:%d!=%d" % (len(rows), args.expect_rows))
    print("rows=%d agencies=%d problems=%d" % (len(rows), len({r["agency"] for r in rows}), len(problems)))
    for p in problems:
        print("PROBLEM", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
