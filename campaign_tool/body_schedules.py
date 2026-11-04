"""DF0022-10: organizer-verified regular meeting schedules (``content/body-schedules.json``) and agenda-watch dates.

A schedule is a rule an organizer read off the body's own calendar page ("first and third Tuesday at 18:30"). It
yields dates to *watch for an agenda*, never a scheduled meeting: holidays, special meetings and skipped sessions stay
unknown until an organizer stages a verified meeting. Nothing here fetches, sends or publishes anything.
"""
import calendar
import os
import re
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from .content import ContentError, _https, _read_json, _require_keys, plain_text, utc_moment

FILE = "body-schedules.json"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ORDINALS = (1, 2, 3, 4, "last")
ROW_KEYS = {"body", "weekday", "ordinals", "local_start_time", "time_zone", "source_url", "checked_on"}
MAX_ROWS, MAX_URL_CHARS, MAX_BODY_CHARS, STALE_DAYS = 100, 500, 200, 365
TIME = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _zone(value, label):
    if not isinstance(value, str) or not value or len(value) > 64 or not plain_text(value) or not value.isascii():
        raise ContentError(f"{label}.time_zone: must be an IANA time zone name such as America/Los_Angeles")
    try:
        return ZoneInfo(value)
    except (ValueError, OSError, KeyError):  # ZoneInfoNotFoundError is a KeyError
        raise ContentError(f"{label}.time_zone: unknown time zone {value}") from None


def _row(row, index, today):
    label = f"{FILE}.schedules[{index}]"
    _require_keys(row, ROW_KEYS, ROW_KEYS, label)
    body = row["body"]
    if (not isinstance(body, str) or not 1 <= len(body) <= MAX_BODY_CHARS or not plain_text(body)
            or body != body.strip() or any(char in body for char in "\n\r\t")):
        raise ContentError(f"{label}.body: 1 to {MAX_BODY_CHARS} plain-text characters with no leading or trailing "
                           "space, newline or tab")
    if not isinstance(row["weekday"], str) or row["weekday"] not in WEEKDAYS:
        raise ContentError(f"{label}.weekday: must be monday through sunday")
    ordinals = row["ordinals"]
    if (not isinstance(ordinals, list) or not ordinals
            or any(not ((type(item) is int and 1 <= item <= 4) or (type(item) is str and item == "last"))
                   for item in ordinals)
            or len(set(ordinals)) != len(ordinals)):
        raise ContentError(f"{label}.ordinals: use unique 1, 2, 3, 4 or last")
    start = row["local_start_time"]
    if not isinstance(start, str) or not TIME.fullmatch(start):
        raise ContentError(f"{label}.local_start_time: use HH:MM from 00:00 to 23:59")
    _zone(row["time_zone"], label)
    url = _https(row["source_url"], f"{label}.source_url")
    if len(row["source_url"]) > MAX_URL_CHARS:
        raise ContentError(f"{label}.source_url: at most {MAX_URL_CHARS} characters")
    checked = row["checked_on"]
    try:
        if not isinstance(checked, str) or not DAY.fullmatch(checked):
            raise ValueError
        checked_day = date(int(checked[:4]), int(checked[5:7]), int(checked[8:]))
    except ValueError:
        raise ContentError(f"{label}.checked_on: must be a real YYYY-MM-DD date") from None
    if checked_day > today:
        raise ContentError(f"{label}.checked_on: {checked} is in the future")
    return {"body": body, "weekday": row["weekday"], "ordinals": sorted(ordinals, key=ORDINALS.index),
            "local_start_time": start, "time_zone": row["time_zone"], "source_url": url, "checked_on": checked}


def load_body_schedules(document, *, today=None):
    """Validate a parsed ``body-schedules.json``; return its rows sorted by literal body. Refuses, never skips."""
    today = today or date.today()
    _require_keys(document, {"schema_version", "schedules"}, {"schema_version", "schedules"}, FILE)
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ContentError(f"{FILE}.schema_version: must be 1")
    rows = document["schedules"]
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ContentError(f"{FILE}.schedules: must be a list of at most {MAX_ROWS} schedules")
    loaded, seen = [], set()
    for index, row in enumerate(rows):
        schedule = _row(row, index, today)
        if schedule["body"] in seen:
            raise ContentError(f"{FILE}: duplicate body {schedule['body']}")
        seen.add(schedule["body"])
        loaded.append(schedule)
    return sorted(loaded, key=lambda item: item["body"])


def read_body_schedules(root, *, today=None):
    """``load_body_schedules`` of ``<root>/content/body-schedules.json``; [] only when nothing is there at all.

    A symlink (even a dangling one) is refused by ``_read_json``, never mistaken for absence."""
    path = Path(root) / "content" / FILE
    if not os.path.lexists(path):
        return []
    if not path.is_symlink() and not path.is_file():
        raise ContentError(f"{FILE}: must be a regular file, not a folder")
    try:
        document = _read_json(path)
    except OSError:
        raise ContentError(f"{FILE}: could not be read") from None
    return load_body_schedules(document, today=today)


def stale(schedule, as_of):
    """True when the organizer's check is more than 365 days old (exactly 365 is still fresh)."""
    return (as_of - date.fromisoformat(schedule["checked_on"])).days > STALE_DAYS


def _month_dates(schedule, year, month):
    weekday = WEEKDAYS.index(schedule["weekday"])
    first = 1 + (weekday - date(year, month, 1).weekday()) % 7
    last_day = calendar.monthrange(year, month)[1]
    days = set()
    for ordinal in schedule["ordinals"]:
        if ordinal == "last":
            days.add(first + 7 * ((last_day - first) // 7))
        else:
            days.add(first + 7 * (ordinal - 1))  # first <= 7, so the fourth is never past day 28
    return [date(year, month, day) for day in sorted(days)]


def _staged_days(schedule, meetings):
    """Local calendar dates (in the schedule's zone) of every staged meeting of exactly this body, any status."""
    zone, days = ZoneInfo(schedule["time_zone"]), set()
    for meeting in meetings:
        if meeting["body"] != schedule["body"]:
            continue
        try:
            days.add(utc_moment(meeting["starts_at"]).astimezone(zone).date())
        except (OverflowError, ValueError):  # an instant outside the calendar range cannot be a candidate date
            continue
    return days


def next_expected_dates(schedule, meetings, as_of, *, limit=2):
    """The next ``limit`` regular dates on or after ``as_of`` that have no staged meeting of this body.

    Calendar dates only, never a time of day: the Nth weekday is the first occurrence plus 7*(N-1) days and ``last``
    is the final occurrence in the month (the fourth and the last may coincide; ``last`` is never a fifth)."""
    staged, found = _staged_days(schedule, meetings), []
    year, month = as_of.year, as_of.month
    while len(found) < limit and (year, month) <= (9999, 12):
        found.extend(day for day in _month_dates(schedule, year, month) if day >= as_of and day not in staged)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return found[:limit]
