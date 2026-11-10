"""The campaign's own public events (DF1107-08): ``content/events.json`` -> ``/events.html`` and a next-event home notice.

An optional JSON array of at most 50 objects, stored order kept. A missing file means no events output at all (``read_events``
returns None); a present ``[]`` is valid and also switches every output off. Each event: ``id`` (lowercase slug), ``title``
(1-120 plain characters), ``starts_at`` / ``ends_at`` (``YYYY-MM-DDTHH:MM:SS`` plus ``Z`` or ``+HH:MM``; a zone offset is
required, at most 14:00), ``description_md`` (1-1200 characters, safe Markdown subset), then EITHER ``venue`` (1-160 plain
characters, published as written) with ``online`` absent or false, OR ``online`` true with no venue; optional ``url`` (https
only, never fetched) and ``accessibility_note`` (1-400 plain characters). Every published string goes through the shared
privacy lint (``privacy_patterns``); a refusal names the place and the category, never the text.

Events are the campaign's own gatherings: they are not governing-body meetings and have no comment kit or outcome.

Classification is deterministic and start-based: with a UTC ``cutoff`` (the build day at 00:00:00 UTC), an event is upcoming
when ``starts_at >= cutoff`` and past otherwise (one already underway at the cutoff counts as past). ``partition`` sorts copies:
upcoming by UTC start then id, past newest start first then id. Later newsletter or calendar units reuse these helpers.
"""
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .agency_cards import text as plain_text
from .content import ContentError, SLUG, _https, _read_json
from .privacy_patterns import categories

EVENT_KEYS = {"id", "title", "starts_at", "ends_at", "description_md", "venue", "online", "url", "accessibility_note"}
EVENT_REQUIRED = {"id", "title", "starts_at", "ends_at", "description_md"}
MAX_EVENTS, MAX_TITLE, MAX_VENUE, MAX_DESCRIPTION, MAX_NOTE = 50, 120, 160, 1200, 400
MAX_OFFSET_HOURS = 14
TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(Z|[+-]\d{2}:\d{2})", re.ASCII)
KEY_NAME = re.compile(r"[A-Za-z0-9_]{1,40}")


def parse_moment(value):
    """The aware ``datetime`` of a strict ``YYYY-MM-DDTHH:MM:SS(Z|±HH:MM)`` string, or None when it is anything else."""
    match = TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return None
    zone = match.group(2)
    if zone != "Z":
        hours, minutes = int(zone[1:3]), int(zone[4:6])
        if minutes > 59 or hours > MAX_OFFSET_HOURS or (hours == MAX_OFFSET_HOURS and minutes):
            return None
    try:
        moment = datetime.fromisoformat(match.group(1) + ("+00:00" if zone == "Z" else zone))
        moment.astimezone(timezone.utc)  # refuse instants outside the UTC range: partition converts every start
    except (ValueError, OverflowError):
        return None
    return moment


def _line(value, limit):
    """Trimmed single-line plain text of 1..limit characters: no angle brackets, no Unicode control or format characters."""
    return (isinstance(value, str) and 1 <= len(value) <= limit and value == value.strip()
            and "<" not in value and ">" not in value
            and all(unicodedata.category(char) not in ("Cc", "Cf") and char not in "\u2028\u2029" for char in value))


def _has_surrogate(value):
    """True for a string holding any lone/explicit surrogate code point U+D800-U+DFFF (never a real supplementary scalar)."""
    return isinstance(value, str) and any("\ud800" <= char <= "\udfff" for char in value)


def _key_label(key):
    return key if isinstance(key, str) and KEY_NAME.fullmatch(key) else f"{len(str(key))} chars"


def load_events(document, problems=None):
    """Validate a parsed ``content/events.json`` and return the events (copies, file order).

    With ``problems`` a list, every independent refusal (``events.json[N].field: message``, zero-based) is appended and None is
    returned; without it the first raises ``ContentError`` (build)."""
    found = []

    def refuse(where, message):
        found.append(f"events.json{where}: {message}")

    def finish(result):
        if not found:
            return result
        if problems is None:
            raise ContentError(found[0])
        problems.extend(found)
        return None

    if not isinstance(document, list):
        refuse("", "must be a JSON array of events")
        return finish(None)
    if len(document) > MAX_EVENTS:
        refuse("", f"must hold at most {MAX_EVENTS} events")
        return finish(None)
    accepted, first_at = [], {}
    for index, event in enumerate(document):
        base = f"[{index}]"
        if not isinstance(event, dict):
            refuse(base, "expected an object")
            continue
        before = len(found)
        for key in sorted(set(event) - EVENT_KEYS, key=str):
            refuse(base, f"unknown field {_key_label(key)}")
        for key in sorted(EVENT_REQUIRED - set(event)):
            refuse(f"{base}.{key}", "missing required field")
        bad = set()  # fields whose type or shape is already refused: not linted

        def check(key, ok, message):
            if key in event and not ok(event[key]):
                refuse(f"{base}.{key}", message)
                bad.add(key)

        check("id", lambda v: isinstance(v, str) and SLUG.fullmatch(v) is not None,
              "must be a lowercase id of 1 to 64 letters, digits and hyphens starting with a letter")
        check("title", lambda v: _line(v, MAX_TITLE) and bool(v.strip()),
              f"must be trimmed plain text of 1 to {MAX_TITLE} characters without <, > or control characters")
        check("venue", lambda v: _line(v, MAX_VENUE),
              f"must be trimmed plain text of 1 to {MAX_VENUE} characters without <, > or control characters")
        check("accessibility_note", lambda v: _line(v, MAX_NOTE),
              f"must be trimmed plain text of 1 to {MAX_NOTE} characters without <, > or control characters")
        check("description_md", lambda v: (isinstance(v, str) and 1 <= len(v) <= MAX_DESCRIPTION and bool(v.strip())
                                          and plain_text(v) and "![" not in v),
              f"must be non-blank text of at most {MAX_DESCRIPTION} characters without <, >, images or control characters")
        for key in ("title", "venue", "description_md", "accessibility_note"):  # shape-valid text must be encodable as UTF-8
            if key in event and key not in bad and _has_surrogate(event[key]):
                refuse(f"{base}.{key}", "must contain valid Unicode scalar values")
                bad.add(key)
        check("online", lambda v: v is True or v is False, "must be true or false")
        if "url" in event:
            try:
                _https(event["url"], "url")
            except ContentError:
                refuse(f"{base}.url", "must be an https:// URL")
                bad.add("url")
        moments = {}
        for key in ("starts_at", "ends_at"):
            if key in event:
                moments[key] = parse_moment(event[key])
                if moments[key] is None:
                    refuse(f"{base}.{key}", "must be YYYY-MM-DDTHH:MM:SS followed by Z or a zone offset such as -07:00 (at most 14:00) and be representable in UTC")
                    bad.add(key)
        if len(moments) == 2 and None not in moments.values() and moments["ends_at"] < moments["starts_at"]:
            refuse(f"{base}.ends_at", "is before starts_at")
        online = event.get("online")
        if "online" in event and "online" not in bad:
            if online is True and "venue" in event:
                refuse(f"{base}.venue", "must be left out when online is true")
            elif online is False and "venue" not in event:
                refuse(f"{base}.venue", "is required unless online is true")
        elif "online" not in event and "venue" not in event:
            refuse(f"{base}.venue", "is required unless online is true")
        if "id" in event and "id" not in bad:
            if event["id"] in first_at:
                shown = "" if categories(event["id"]) else f" {event['id']!r}"  # lint before interpolating
                refuse(f"{base}.id", f"duplicates events[{first_at[event['id']]}].id{shown}")
            else:
                first_at[event["id"]] = index
        for key in ("id", "title", "venue", "description_md", "accessibility_note", "url"):
            value = event.get(key)
            if key in event and key not in bad and isinstance(value, str) and (hits := categories(value)):
                refuse(f"{base}.{key}", "privacy lint: " + ", ".join(hits))
        if len(found) == before:
            accepted.append(dict(event))
    return finish(accepted)


def read_events(root, problems=None):
    """``load_events`` of ``<root>/content/events.json``; None when the file is absent, ``[]`` when present and empty.

    A symlink (even dangling), folder, FIFO or other non-regular path is refused before any read; a read failure is a named
    ``ContentError``. ``problems``: see ``load_events`` (content-check collects, build raises)."""
    path = Path(root) / "content" / "events.json"
    try:
        if path.is_symlink():
            raise ContentError("events.json: symlinks are not read")
        if not os.path.lexists(path):
            return None
        if not path.is_file():
            raise ContentError("events.json: must be a regular file, not a symlink or folder")
        try:
            document = _read_json(path)
        except OSError:
            raise ContentError("events.json: could not be read") from None
        except RecursionError:
            raise ContentError("events.json: not valid UTF-8 JSON (nested too deeply)") from None
    except ContentError as exc:
        if problems is None:
            raise
        problems.append(str(exc))
        return None
    return load_events(document, problems)


def build_cutoff(day):
    """The reference instant for a build day: that date at 00:00:00 UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def partition(events, cutoff):
    """``(upcoming, past)`` as new lists; the input is not reordered. Upcoming means ``starts_at >= cutoff``."""
    def utc(event):
        return parse_moment(event["starts_at"]).astimezone(timezone.utc)
    upcoming = sorted((ev for ev in events if utc(ev) >= cutoff), key=lambda ev: (utc(ev), ev["id"]))
    past = sorted((ev for ev in events if utc(ev) < cutoff), key=lambda ev: (-utc(ev).timestamp(), ev["id"]))
    return upcoming, past
