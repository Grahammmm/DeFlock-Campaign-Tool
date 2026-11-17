"""Validate an existing agency events.tsv by line and rule (REC-TL-1a); read-only, never prints cell text."""
from datetime import date, datetime, timezone
import json
import os
import re
import stat

from .events_ingest import COLUMNS, PACIFIC

MAX_BYTES, MAX_LINES = 2 * 1024 * 1024, 200
TYPES = {"request_sent", "follow_up_sent", "clock_start", "clock_due", "determination", "request_received",
         "reconsideration_sent"}
TIME = re.compile(r"[0-9]{2}:[0-9]{2} P[DS]T")
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?Z")
LATE = re.compile(r"(?<![a-z0-9])(overdue|late|past due)(?![a-z0-9])", re.I)


def _real_date(text):
    try:
        return date.fromisoformat(text) if DAY.fullmatch(text) else None
    except ValueError:
        return None


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                return None
            raw = handle.read(MAX_BYTES + 1)
        return raw.decode("utf-8") if len(raw) <= MAX_BYTES else None
    except (OSError, UnicodeError):
        return None


def _utc_pacific_mismatch(row):
    for text in (row["source"], row["clock_note"]):
        for stamp in STAMP.findall(text):
            utc = datetime.fromisoformat(stamp[:-1] + "+00:00").astimezone(timezone.utc)
            if row["date"] == utc.date().isoformat() and utc.astimezone(PACIFIC).date() < utc.date():
                return True
    return False


def validate(path):
    text = _read(path)
    if text is None:
        return [{"line": 0, "column": "file", "code": "file_invalid"}]
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0].rstrip("\r").split("\t") != list(COLUMNS):
        return [{"line": 1, "column": "header", "code": "header_invalid"}]
    problems, seen = [], set()
    for n, line in enumerate(lines[1:], 2):
        cells = line.rstrip("\r").split("\t")

        def hit(column, code):
            problems.append({"line": n, "column": column, "code": code})

        if len(cells) != len(COLUMNS):
            hit("row", "row_columns")
            continue
        row = dict(zip(COLUMNS, cells))
        if not row["event_id"].strip():
            hit("event_id", "event_id_missing")
        elif row["event_id"] in seen:
            hit("event_id", "event_id_duplicate")
        seen.add(row["event_id"])
        if row["event_type"] not in TYPES:
            hit("event_type", "event_type_unknown")
        if _real_date(row["date"]) is None:
            hit("date", "date_invalid")
        if row["time_local"] and not TIME.fullmatch(row["time_local"]):
            hit("time_local", "time_invalid")
        if row["basis"] not in ("DATED", "INFERRED"):
            hit("basis", "basis_invalid")
        if not row["request_ref"].strip():
            hit("request_ref", "request_ref_missing")
        bad = next((c for c in COLUMNS if "<" in row[c] or ">" in row[c]), None)
        if bad:
            hit(bad, "angle_bracket")
        if _real_date(row["date"]) and _utc_pacific_mismatch(row):
            hit("date", "date_is_utc_date")
        if any(LATE.search(row[c]) for c in ("what_happened", "clock_note", "event_type")):
            note = row["clock_note"].lower()
            if "on our records" not in note or "not a legal finding" not in note:
                hit("clock_note", "overdue_wording")
    return problems


def main_validate(path, as_json=False):
    problems = validate(path)
    if as_json:
        print(json.dumps({"ok": not problems, "problems": problems[:MAX_LINES]}, sort_keys=True))
    else:
        for p in problems[:MAX_LINES]:
            print(f"events.tsv line {p['line']}: {p['code']} ({p['column']})")
    return 1 if problems else 0
