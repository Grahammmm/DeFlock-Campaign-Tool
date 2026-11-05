"""DF1107-07: organizer-verified public statements of decision makers (``content/statements.json``).

A JSON array of 0 to 200 rows. Each row names an official already listed in ``content/officials.json`` (``agency_id`` is
the officials body's agency id; ``official`` is the stored name, compared literally and case-sensitively), the day it was
said, the quote exactly as the official source has it, its context, the https address of that official source and the
day the organizer checked it. Optional: ``recording_time`` (H:MM:SS into a video) and a ``context_note`` of campaign
context. Nothing here fetches an address, shortens, merges or paraphrases a quote, or judges what it means.

Every refusal names ``statements.json[N].FIELD`` (N counts from 0) and never repeats a supplied quote, note or address.
"""
import json
import os
import re
import stat
import unicodedata
from datetime import date
from pathlib import Path

from .content import AGENCY_ID, ContentError, _calendar_day, _https, _quote
from .privacy_patterns import refusal

FILE = "statements.json"
MAX_ROWS, MAX_BYTES, MAX_URL, MAX_NOTE = 200, 2 * 1024 * 1024, 500, 200
REQUIRED = ("agency_id", "official", "said_on", "quote", "context", "source_url", "checked_on")
ROW_KEYS = REQUIRED + ("recording_time", "context_note")
CONTEXTS = ("meeting", "press_release", "newsletter", "interview", "other")
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
RECORDING = re.compile(r"(?:0|[1-9][0-9]{0,2}):[0-5][0-9]:[0-5][0-9]", re.ASCII)
PUBLIC_FIELDS = ("official", "quote", "source_url", "recording_time", "context_note")


def _day(value):
    if not (isinstance(value, str) and DAY.fullmatch(value)):
        return None
    try:
        return _calendar_day(value, "day")
    except (ContentError, ValueError):
        return None


def _row(raw, index, officials, today):
    """-> (problem lines, canonical row or None). ``officials`` None skips only the roster-membership check."""
    where = f"{FILE}[{index}]"
    if not isinstance(raw, dict):
        return [f"{where}: must be an object"], None
    problems = [f"{where}.{key}: missing" for key in REQUIRED if key not in raw]
    problems += [f"{where}: unknown field not allowed" for key in raw if key not in ROW_KEYS]
    problems += [f"{where}.{key}: must be omitted rather than null" for key in ROW_KEYS[7:] if key in raw and raw[key] is None]
    if problems:
        return problems, None

    agency, name = raw["agency_id"], raw["official"]
    if not isinstance(agency, str) or not AGENCY_ID.fullmatch(agency):
        problems.append(f"{where}.agency_id: invalid agency identifier")
    elif officials is not None and agency not in officials:
        problems.append(f"{where}.agency_id: no officials listed for {agency} in officials.json")
    if not isinstance(name, str) or not name:
        problems.append(f"{where}.official: must be the exact name listed in officials.json")
    elif (officials is not None and isinstance(agency, str) and agency in officials
          and name not in {person["name"] for person in officials[agency]}):
        problems.append(f"{where}.official: not listed in officials.json for {agency} (names match exactly)")

    said, checked = _day(raw["said_on"]), _day(raw["checked_on"])
    if said is None:
        problems.append(f"{where}.said_on: must be a real YYYY-MM-DD date")
    if checked is None:
        problems.append(f"{where}.checked_on: must be a real YYYY-MM-DD date")
    if said is not None and checked is not None and said > checked:
        problems.append(f"{where}.said_on: must not be after checked_on")
    if said is not None and said > today:
        problems.append(f"{where}.said_on: is in the future")
    if checked is not None and checked > today:
        problems.append(f"{where}.checked_on: must not be after the build date")

    try:
        _quote(raw, where)
    except ContentError as exc:
        problems.append(str(exc))
    if not isinstance(raw["context"], str) or raw["context"] not in CONTEXTS:
        problems.append(f"{where}.context: choose one of {list(CONTEXTS)}")
    url = raw["source_url"]
    try:
        if not isinstance(url, str) or len(url) > MAX_URL:
            raise ContentError("length")
        _https(url, "url")
    except ContentError:
        problems.append(f"{where}.source_url: must be an https:// URL of at most {MAX_URL} characters")
    if "recording_time" in raw and not (isinstance(raw["recording_time"], str) and RECORDING.fullmatch(raw["recording_time"])):
        problems.append(f"{where}.recording_time: use H:MM:SS, for example 1:02:45")
    if "context_note" in raw:
        note = raw["context_note"]
        if (not isinstance(note, str) or not 1 <= len(note) <= MAX_NOTE or not note.strip() or "<" in note or ">" in note
                or any(unicodedata.category(char) in ("Cc", "Cf") for char in note)):
            problems.append(f"{where}.context_note: must be 1 to {MAX_NOTE} plain-text characters without angle brackets "
                            "or control characters")
    for key in PUBLIC_FIELDS:  # the shared privacy lint: a quoted source is no exemption
        if key in raw and isinstance(raw[key], str) and (note := refusal(f"{where}.{key}", raw[key])):
            problems.append(note)
    if problems:
        return problems, None
    return [], {key: raw[key] for key in ROW_KEYS if key in raw}


def check_statements(document, officials, today):
    """-> (validated rows in stored order, problem lines). ``officials`` None skips only the roster check."""
    if not isinstance(document, list):
        return [], [f"{FILE}: must be a list of at most {MAX_ROWS} statements"]
    if len(document) > MAX_ROWS:
        return [], [f"{FILE}: must be a list of at most {MAX_ROWS} statements"]
    rows, problems = [], []
    for index, raw in enumerate(document):
        found, row = _row(raw, index, officials, today)
        problems += found
        if row is not None:
            rows.append(row)
    return rows, problems


def _parse(data):
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):  # includes UnicodeDecodeError and JSONDecodeError
        raise ContentError(f"{FILE}: not valid UTF-8 JSON") from None


def _read_document(root):
    """The parsed document, or None when the file is absent. A link, folder, FIFO or oversized file is refused unread."""
    path = Path(root) / "content" / FILE
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise ContentError(f"{FILE}: cannot be inspected") from None
    if stat.S_ISLNK(info.st_mode):
        raise ContentError(f"{FILE}: symlinks are not read")
    if not stat.S_ISREG(info.st_mode):
        raise ContentError(f"{FILE}: must be a regular file")
    if info.st_size > MAX_BYTES:
        raise ContentError(f"{FILE}: file too large")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError:
        raise ContentError(f"{FILE}: could not be read") from None
    if len(data) > MAX_BYTES:
        raise ContentError(f"{FILE}: file too large")
    return _parse(data)


def read_statements(root, officials, today=None, problems=None):
    """The validated rows of ``<root>/content/statements.json`` in stored order; None when the file is absent.

    ``officials``: the validated ``{agency_id: [person, ...]}`` roster, or None to skip only the membership check.
    Without ``problems`` the first problem raises ContentError; with a list every problem is appended and None returned."""
    today = today or date.today()
    try:
        document = _read_document(root)
        if document is None:
            return None
    except ContentError as exc:
        if problems is None:
            raise
        problems.append(str(exc))
        return None
    rows, found = check_statements(document, officials, today)
    if found:
        if problems is None:
            raise ContentError(found[0])
        problems.extend(found)
        return None
    return rows


def newest_first(rows):
    """A copy ordered by ``said_on`` newest first; equal days keep their stored order."""
    return sorted(rows, key=lambda row: row["said_on"], reverse=True)
