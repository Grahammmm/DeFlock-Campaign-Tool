"""DF1103-09: organizer-entered meetings kept in private ``kit/meetings-manual.json``.

For a body on no supported agenda platform. Offline only: nothing is fetched, sent or published, and the
organizer attests that the agenda URL is official. A later ``meetings`` scan rewrites ``kit/meetings.json`` only,
so a manual entry is never dropped by it or reported as removed. ``read_meetings`` merges both files by id for
``meetings-stage``. Corrections and cancellations change this private file only, never a staged page.
"""
import json
import os
import re
import stat
import unicodedata
from datetime import date, datetime
from pathlib import Path

from .content import AGENCY_ID, SLUG, ContentError, _https, load_meeting
from .kit import _kit_folder, _write_text

MAX_BYTES = 4 * 1024 * 1024
SCAN = "kit/meetings.json"
MANUAL = "kit/meetings-manual.json"
PREFIX = "manual-"
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
STARTS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})")
FIELDS = ("id", "body", "agency_id", "starts_at", "agenda_url", "agenda_item", "source_url", "checked_on")
ENTRY_KEYS = set(FIELDS) | {"source", "time_known", "status", "history"}
HISTORY_KEYS = {"action", "on", "reason", "before"}


class ManualMeetingError(ValueError):
    pass


def _text(value, name, limit):
    """Plain text of 1..limit characters after collapsing spaces; no angle brackets, control or format characters."""
    if not isinstance(value, str) or any(unicodedata.category(c) in ("Cc", "Cf") for c in value) or "<" in value or ">" in value:
        raise ManualMeetingError(f"{name}: must be plain text without angle brackets or control characters")
    collapsed = " ".join(value.split())
    if not 1 <= len(collapsed) <= limit:
        raise ManualMeetingError(f"{name}: must be 1 to {limit} characters")
    return collapsed


def _url(value, name):
    if (not isinstance(value, str) or len(value) > 500 or re.search(r"[\s\x00-\x1f\x7f]", value)):
        raise ManualMeetingError(f"{name}: must be an https:// URL of at most 500 characters without spaces")
    try:
        return _https(value, name)
    except ContentError:
        raise ManualMeetingError(f"{name}: must be an https:// URL with a host and no credentials") from None


def _day(value, name):
    if not isinstance(value, str) or not DAY.fullmatch(value):
        raise ManualMeetingError(f"{name}: must be a real YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ManualMeetingError(f"{name}: must be a real YYYY-MM-DD date") from None


def _starts(value):
    """(normalized timestamp, local start date); the offset is kept, never guessed."""
    if not isinstance(value, str) or not STARTS.fullmatch(value):
        raise ManualMeetingError("starts_at: needs an ISO timestamp with an explicit UTC offset or Z")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ManualMeetingError("starts_at: not a real date and time") from None
    return moment.isoformat(timespec="seconds"), moment.date()


def _id(value):
    if (not isinstance(value, str) or not value.startswith(PREFIX) or not re.fullmatch(r"[a-z0-9-]+", value[len(PREFIX):])
            or len(value) > 80 or not SLUG.fullmatch(value)):
        raise ManualMeetingError("meeting id: use manual- followed by lowercase letters, digits and hyphens (at most 64 characters)")
    return value


def _fields(raw, today):
    """The validated entry fields. ``today`` None skips only the date-order checks (stored entries stay readable)."""
    fields = {"id": _id(raw.get("id")), "body": _text(raw.get("body"), "body", 200)}
    agency = raw.get("agency_id")
    if not isinstance(agency, str) or not AGENCY_ID.fullmatch(agency):
        raise ManualMeetingError("agency_id: invalid agency identifier")
    fields["agency_id"] = agency
    fields["starts_at"], start_day = _starts(raw.get("starts_at"))
    fields["agenda_url"] = _url(raw.get("agenda_url"), "agenda_url")
    fields["agenda_item"] = _text(raw.get("agenda_item"), "item title", 500)
    fields["source_url"] = _url(raw.get("source_url"), "source_url")
    fields["checked_on"] = raw.get("checked_on")
    checked = _day(fields["checked_on"], "checked_on")
    if checked > start_day:
        raise ManualMeetingError("checked_on: must not be after the meeting's start date")
    if today is not None:
        if start_day < today:
            raise ManualMeetingError("starts_at: the meeting date is in the past")
        if checked > today:
            raise ManualMeetingError("checked_on: must not be in the future")
    try:  # the public projection only; private keys never reach the strict content schema
        load_meeting({k: fields[k] for k in ("id", "body", "starts_at", "agenda_url", "agenda_item", "agency_id")}, "meeting")
    except ContentError:
        raise ManualMeetingError("meeting: fields do not form a valid meeting") from None
    return fields


def _entry(fields, status="scheduled", history=()):
    return {**fields, "source": "manual", "time_known": True, "status": status, "history": list(history)}


def _stored_entry(raw, label, with_history=True):
    keys = ENTRY_KEYS if with_history else ENTRY_KEYS - {"history"}
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ManualMeetingError(f"{MANUAL}: {label} is not a manual meeting entry")
    fields = _fields(raw, None)
    if (raw["source"] != "manual" or raw["time_known"] is not True or raw["status"] not in ("scheduled", "cancelled")
            or any(raw[k] != fields[k] for k in ("body", "agenda_item"))):
        raise ManualMeetingError(f"{MANUAL}: {label} has an invalid field")
    if not with_history:
        return {k: v for k, v in _entry(fields, raw["status"]).items() if k != "history"}
    history = raw["history"]
    if not isinstance(history, list):
        raise ManualMeetingError(f"{MANUAL}: {label} history must be a list")
    for number, item in enumerate(history):
        where = f"{label} history[{number}]"
        if (not isinstance(item, dict) or set(item) != HISTORY_KEYS or item["action"] not in ("replace", "cancel")):
            raise ManualMeetingError(f"{MANUAL}: {where} is malformed")
        _day(item["on"], f"{where} on")
        try:
            _text(item["reason"], f"{where} reason", 200)
        except ManualMeetingError:
            raise ManualMeetingError(f"{MANUAL}: {where} has an invalid reason") from None
        before = _stored_entry(item["before"], f"{where} before", with_history=False)
        if before["id"] != raw["id"]:
            raise ManualMeetingError(f"{MANUAL}: {where} belongs to another meeting")
    return {**_entry(fields, raw["status"]), "history": history}


def _read_bytes(path, label):
    """Bytes of a regular, non-symlink file of at most 4 MiB; None when it does not exist."""
    if os.path.islink(path):
        raise ManualMeetingError(f"{label} must not be a symlink")
    if not os.path.lexists(path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ManualMeetingError(f"{label} must be a regular file") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ManualMeetingError(f"{label} must be a regular file")
        data = b""
        while chunk := os.read(fd, 65536):
            data += chunk
            if len(data) > MAX_BYTES:
                raise ManualMeetingError(f"{label}: larger than 4 MiB")
    finally:
        os.close(fd)
    return data


class _RepeatedKey(Exception):
    """A JSON object repeated a decoded member name; carries no key or value."""


def _unique_members(pairs):
    """object_pairs_hook for every JSON object at every depth: a repeated decoded name is ambiguous, never last-wins.

    Without this a later successful add or correction would rewrite the collapsed reading and silently drop history.
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise _RepeatedKey
        result[key] = value
    return result


def read_manual(root):
    """Validated entries of kit/meetings-manual.json ([] when missing); any fault refuses, nothing is reset."""
    root = Path(root)
    if os.path.islink(root / "kit"):
        raise ManualMeetingError("kit/ must not be a symlink")
    data = _read_bytes(root / MANUAL, MANUAL)
    if data is None:
        return []
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_members)
    except _RepeatedKey:
        raise ManualMeetingError(f"{MANUAL}: a JSON object repeats a key") from None
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ManualMeetingError(f"{MANUAL}: not UTF-8 JSON") from None
    return _manual_document(document)


def _manual_document(document):
    """Validate an already decoded private document without reading the filesystem."""
    if (not isinstance(document, dict) or set(document) != {"schema_version", "meetings"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1):
        raise ManualMeetingError(f"{MANUAL}: unsupported schema_version (expected 1)")
    if not isinstance(document["meetings"], list):
        raise ManualMeetingError(f"{MANUAL}: meetings must be a list")
    entries = [_stored_entry(raw, f"meetings[{n}]") for n, raw in enumerate(document["meetings"])]
    ids = [e["id"] for e in entries]
    if len(set(ids)) != len(ids):
        raise ManualMeetingError(f"{MANUAL}: duplicate meeting id")
    return entries


def _read_scan(root):
    """kit/meetings.json meetings, or None when the file is missing; strings are those of meetings-stage."""
    path = Path(root) / "kit" / "meetings.json"
    if os.path.islink(path):
        raise ManualMeetingError("kit/meetings.json must not be a symlink")
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_BYTES:
        raise ManualMeetingError("kit/meetings.json: larger than 4 MiB")
    # The file may grow after the stat above: never read past the ceiling.
    with open(path, "rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ManualMeetingError("kit/meetings.json: larger than 4 MiB")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManualMeetingError(f"kit/meetings.json: not UTF-8 JSON ({exc.__class__.__name__})") from None
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ManualMeetingError("kit/meetings.json: unsupported schema_version (expected 1)")
    if not isinstance(document.get("meetings"), list):
        raise ManualMeetingError("kit/meetings.json: meetings must be a list")
    return document["meetings"]


def read_meetings(root, *, allow_empty=False):
    """Scan and manual meetings merged by id. Both files absent: an error, or [] with ``allow_empty``."""
    scan = _read_scan(root)
    manual = read_manual(root)
    if scan is None and not manual and not os.path.lexists(Path(root) / MANUAL):
        if allow_empty:
            return []
        raise ManualMeetingError("kit/meetings.json not found; run `meetings` first")
    merged = list(scan or []) + manual
    seen = set()
    for meeting in merged:
        meeting_id = meeting.get("id") if isinstance(meeting, dict) else None
        if isinstance(meeting_id, str):
            if meeting_id in seen:
                raise ManualMeetingError(f"meeting id {meeting_id} appears more than once in kit/meetings.json and "
                                         "kit/meetings-manual.json; nothing was staged or changed")
            seen.add(meeting_id)
    return merged


def _save(root, entries):
    entries = sorted(entries, key=lambda e: e["id"])
    text = json.dumps({"schema_version": 1, "meetings": entries}, indent=2, ensure_ascii=False) + "\n"
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ManualMeetingError(f"{MANUAL}: would be larger than 4 MiB; nothing was written")
    kit = Path(root) / "kit"
    _kit_folder(kit, mode=0o700)
    os.chmod(kit, 0o700)
    _write_text(kit / "meetings-manual.json", text)


def _manual_today(today):
    return today or date.today()


def _existing(root, meeting_id):
    """(all manual entries, the one named) after both files validate and no id clashes."""
    read_meetings(root, allow_empty=True)  # validates both stores and refuses a duplicate across them
    manual = read_manual(root)
    found = [e for e in manual if e["id"] == meeting_id]
    return manual, (found[0] if found else None)


def _correction(on, reason, entry, today):
    when = _day(on, "--on")
    if not _day(entry["checked_on"], "checked_on") <= when <= today:
        raise ManualMeetingError(f"--on: must be a date from {entry['checked_on']} through today")
    return {"on": on, "reason": _text(reason, "--reason", 200)}


def _result(entry):
    return {"id": entry["id"], "status": entry["status"], "path": MANUAL}


def record_manual(root, meeting_id, *, body, agency_id, starts_at, agenda_url, agenda_item, source_url, checked_on,
                  replace=False, on=None, reason=None, today=None):
    """Add a manual meeting, or with ``replace`` correct an existing one (dated reason; history keeps the old values)."""
    today = _manual_today(today)
    meeting_id = _id(meeting_id)
    fields = _fields({"id": meeting_id, "body": body, "agency_id": agency_id, "starts_at": starts_at,
                      "agenda_url": agenda_url, "agenda_item": agenda_item, "source_url": source_url,
                      "checked_on": checked_on}, today)
    manual, old = _existing(root, meeting_id)
    if replace:
        if old is None:
            raise ManualMeetingError(f"no manual meeting {meeting_id} to replace")
        note = _correction(on, reason, old, today)
        before = {k: v for k, v in old.items() if k != "history"}
        entry = _entry(fields, "scheduled", old["history"] + [{"action": "replace", **note, "before": before}])
    else:
        if on is not None or reason is not None:
            raise ManualMeetingError("--on and --reason are only for --replace or --cancel")
        if old is not None:
            raise ManualMeetingError(f"manual meeting {meeting_id} already exists; pass --replace with --on and --reason")
        entry = _entry(fields)
    _save(root, [e for e in manual if e["id"] != meeting_id] + [entry])
    return _result(entry)


def cancel_manual(root, meeting_id, *, on, reason, today=None):
    """Mark a scheduled manual meeting cancelled, privately; a staged page is not changed."""
    today = _manual_today(today)
    meeting_id = _id(meeting_id)
    manual, old = _existing(root, meeting_id)
    if old is None:
        raise ManualMeetingError(f"no manual meeting {meeting_id} to cancel")
    if old["status"] == "cancelled":
        raise ManualMeetingError(f"manual meeting {meeting_id} is already cancelled")
    if _starts(old["starts_at"])[1] < today:
        raise ManualMeetingError("starts_at: the meeting date is in the past")
    note = _correction(on, reason, old, today)
    before = {k: v for k, v in old.items() if k != "history"}
    entry = {**old, "status": "cancelled", "history": old["history"] + [{"action": "cancel", **note, "before": before}]}
    _save(root, [e for e in manual if e["id"] != meeting_id] + [entry])
    return _result(entry)
