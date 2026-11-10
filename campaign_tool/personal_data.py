"""Read-only register of the private files that can hold a person's name, email, phone or postal address (DF0022-14).

``personal_data_register(root)`` lists, for the nine private file kinds this tool writes (or reads from the organizer),
the path, purpose, record count and the oldest and newest recorded date. Any other file under ``kit/`` or ``private/``
is listed as an unknown kind and is never opened. Record values are never read into the output and nothing is changed:
no file is created, moved, deleted, chmod-ed or touched. Deleting anything is the organizer's decision.
"""
import json
import os
import re
import stat
from datetime import date

NOTICE = "Deletion is the organizer's decision. Nothing was changed."
UNKNOWN_PURPOSE = "Unknown kind; review locally"
MAX_ENTRIES = 10000
SETTINGS_CAP, STORE_CAP = 64 * 1024, 4 * 1024 * 1024
REAL_DIRS = "personal-data requires real campaign directories"
MISSING_DIR = "personal-data directory does not exist"
INCOMPLETE = "personal-data inventory incomplete; review private folders locally"
COLUMNS = ("kind", "path", "purpose", "status", "record_count", "oldest_record_date", "newest_record_date")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


class PersonalDataError(ValueError):
    """The campaign folder cannot be inventoried completely; nothing is printed and nothing was changed."""


class _Bad(Exception):
    """A recognized store is unreadable or malformed; only the row's status says so, never why."""


# --- adapters: each returns (record count, [record dates]) or raises _Bad -----------------------------------------

def _pairs(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _Bad
    return dict(pairs)


def _refuse(_constant):
    raise _Bad


def _day(value):
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        raise _Bad
    try:
        date.fromisoformat(value)
    except ValueError:
        raise _Bad from None
    return value


def _document(data, keys, container, optional=()):
    """The parsed store when it is {schema_version: 1, ...} with at least ``keys``; ``container`` is the type of
    the value under the second key (None when there is none)."""
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_refuse)
    except (ValueError, RecursionError):
        raise _Bad from None
    if (not isinstance(document, dict) or not set(keys) <= set(document) <= set(keys) | set(optional)
            or type(document["schema_version"]) is not int or document["schema_version"] != 1):
        raise _Bad
    if container is not None and not isinstance(document[keys[1]], container):
        raise _Bad
    return document


def _requester(data):
    document = _document(data, ("schema_version",), None,
                         ("requester", "fee_cap_usd", "date_from", "date_to"))
    if "requester" not in document:
        return 0, []
    lines = document["requester"]
    if not isinstance(lines, list) or not lines or not all(isinstance(line, str) for line in lines):
        raise _Bad
    return 1, []


def _roster(data):
    document = _document(data, ("schema_version", "reviewers"), list)
    days = []
    for entry in document["reviewers"]:
        if (not isinstance(entry, dict) or not {"name", "roles", "added_on"} <= set(entry)
                or not set(entry) <= {"name", "roles", "added_on", "ssh_public_key", "interest_agencies",
                                      "interest_note", "retired_on"}
                or not isinstance(entry["name"], str) or not isinstance(entry["roles"], list)
                or not isinstance(entry.get("retired_on", {}), dict)
                or not all(role in entry["roles"] for role in entry.get("retired_on", {}))):
            raise _Bad
        added = _day(entry["added_on"])
        days.append(added)
        for day in entry.get("retired_on", {}).values():  # DF1109B-14: dates only, none before added_on (as the loader)
            if _day(day) < added:
                raise _Bad
            days.append(day)
    return len(document["reviewers"]), days


def _review_assignments(data):
    document = _document(data, ("schema_version", "assignments"), dict)
    days = []
    for entry in document["assignments"].values():
        if (not isinstance(entry, dict) or set(entry) != {"reviewer", "on", "recorded_at"}
                or not isinstance(entry["reviewer"], str) or not isinstance(entry["recorded_at"], str)):
            raise _Bad
        days.append(_day(entry["on"]))
    return len(days), days


def _comments(data):
    document = _document(data, ("schema_version", "comments"), dict)
    days = []
    for entry in document["comments"].values():
        if (not isinstance(entry, dict) or not {"on", "channel", "recorded_at"} <= set(entry)
                or not set(entry) <= {"on", "channel", "recorded_at", "letter_sha256", "letter_bytes"}
                or not isinstance(entry["channel"], str) or not isinstance(entry["recorded_at"], str)):
            raise _Bad
        days.append(_day(entry["on"]))
    return len(days), days


def _corrections(data):
    document = _document(data, ("schema_version", "requests"), list)
    allowed = {"id", "finding_id", "received_on", "summary", "state", "decided_on", "note"}
    days = []
    for item in document["requests"]:
        if (not isinstance(item, dict) or not {"id", "finding_id", "received_on", "summary", "state"} <= set(item)
                or not set(item) <= allowed or type(item["id"]) is not int
                or item["state"] not in ("open", "accepted", "declined")):
            raise _Bad
        days.append(_day(item["received_on"]))
        if "decided_on" in item:
            _day(item["decided_on"])
    return len(days), days


def _contacts(data):
    document = _document(data, ("schema_version", "requests"), dict)
    days = []
    for entry in document["requests"].values():
        if not isinstance(entry, dict):
            raise _Bad
        contacts = entry.get("contacts", [])
        if not isinstance(contacts, list):
            raise _Bad
        for contact in contacts:
            if not isinstance(contact, dict):
                raise _Bad
            days.append(_day(contact.get("on")))
    return len(days), days


def _volunteers(data):
    document = _document(data, ("schema_version", "assignments"), dict)
    if not all(isinstance(label, str) for label in document["assignments"].values()):
        raise _Bad
    return len(document["assignments"]), []


_SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)
_CONSENT = {"organization", "confirmed_on", "how", "public_sha256", "correspondence"}
_COSIGN = {"cosign", "cosign_confirmed_on", "cosign_how", "cosign_correspondence"}


def _digest(value, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise _Bad


def _consent(data):
    document = _document(data, ("schema_version", "supporters"), dict)
    days = []
    for record in document["supporters"].values():
        if not isinstance(record, dict) or not _CONSENT <= set(record) <= _CONSENT | _COSIGN:
            raise _Bad
        grant = set(record) & _COSIGN
        if grant and grant != _COSIGN:
            raise _Bad
        if record["organization"] is not True or not isinstance(record["how"], str):
            raise _Bad
        _digest(record["public_sha256"])
        _digest(record["correspondence"], nullable=True)
        days.append(_day(record["confirmed_on"]))
        if grant:
            _day(record["cosign_confirmed_on"])
            scope = record["cosign"]
            if not (scope == "any" and isinstance(scope, str)
                    or isinstance(scope, list) and all(isinstance(item, str) for item in scope)):
                raise _Bad
            if not isinstance(record["cosign_how"], str):
                raise _Bad
            _digest(record["cosign_correspondence"], nullable=True)
    return len(days), days


def _speakers(data):
    document = _document(data, ("schema_version", "assignments", "meeting_id", "kit_sha256"), list)
    _digest(document["kit_sha256"])
    if not isinstance(document["meeting_id"], str):
        raise _Bad
    for item in document["assignments"]:
        if (not isinstance(item, dict) or set(item) != {"label", "point"}
                or not isinstance(item["label"], str) or not isinstance(item["point"], str)):
            raise _Bad
    return len(document["assignments"]), []


# (kind, path or pattern, fixed purpose, adapter, size cap); the order here is not the output order.
CATALOG = (
    ("requester", "kit/request-settings.json", "Requester contact block", _requester, SETTINGS_CAP),
    ("reviewer_roster", "private/reviewers.json", "Reviewer names and roles", _roster, STORE_CAP),
    ("review_assignments", "private/review-assignments/*.json", "Finding review assignments",
     _review_assignments, STORE_CAP),
    ("comments_sent", "kit/comments-sent.json", "Manually submitted comment log", _comments, STORE_CAP),
    ("correction_requests", "private/correction-requests.json", "Correction request summaries",
     _corrections, STORE_CAP),
    ("contact_log", "kit/sent.json", "Organizer contact notes", _contacts, STORE_CAP),
    ("volunteer_assignments", "kit/assignments.json", "Private request owners", _volunteers, STORE_CAP),
    ("supporters_consent", "kit/supporters-consent.json", "Supporting organization consent", _consent, STORE_CAP),
    ("speaker_plans", "kit/meetings/speakers/*.json", "Private speaker coverage", _speakers, STORE_CAP),
)


def _classify(parts):
    """The catalog entry for a campaign-relative path (tuple of names), or None for an unknown one."""
    for entry in CATALOG:
        pattern = tuple(entry[1].split("/"))
        if len(pattern) != len(parts):
            continue
        if pattern[-1] == "*.json":
            if (pattern[:-1] == parts[:-1] and parts[-1].endswith(".json") and len(parts[-1]) > 5
                    and not parts[-1].startswith(".")):
                return entry
        elif pattern == parts:
            return entry
    return None


# --- reading --------------------------------------------------------------------------------------------------

def _read_known(dir_fd, name, st, entry):
    """(count, dates) of one recognized file, read relative to its directory descriptor; _Bad on any problem."""
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > entry[4]:
        raise _Bad
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        raise _Bad from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > entry[4]:
            raise _Bad
        chunks, size = [], 0
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                raise _Bad from None
            if not chunk:
                break
            size += len(chunk)
            if size > entry[4]:
                raise _Bad
            chunks.append(chunk)
    finally:
        os.close(fd)
    return entry[3](b"".join(chunks))


def _row(kind, path, purpose, status, count=None, days=()):
    return {"kind": kind, "path": path, "purpose": purpose, "status": status, "record_count": count,
            "oldest_record_date": min(days) if days else None, "newest_record_date": max(days) if days else None}


def _walk(dir_fd, parts, state):
    try:
        with os.scandir(dir_fd) as scan:
            entries = sorted(scan, key=lambda item: item.name)
    except OSError:
        raise PersonalDataError(INCOMPLETE) from None
    state["seen"] += len(entries)
    if state["seen"] > MAX_ENTRIES:
        raise PersonalDataError(INCOMPLETE)
    for item in entries:
        try:
            st = item.stat(follow_symlinks=False)
        except OSError:
            raise PersonalDataError(INCOMPLETE) from None
        key = parts + (item.name,)
        path = "/".join(key)
        entry = _classify(key)
        if entry is not None:
            state["matched"].add(entry[0])
            try:
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    raise _Bad
                count, days = _read_known(dir_fd, item.name, st, entry)
                state["rows"].append(_row(entry[0], path, entry[2], "present", count, days))
            except _Bad:
                state["rows"].append(_row(entry[0], path, entry[2], "unreadable"))
        elif stat.S_ISDIR(st.st_mode):
            try:
                child = os.open(item.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except OSError:
                raise PersonalDataError(INCOMPLETE) from None
            try:
                _walk(child, key, state)
            finally:
                os.close(child)
        else:  # a file, a symlink (never followed), a FIFO or another special entry: listed, never opened
            state["rows"].append(_row("unknown", path, UNKNOWN_PURPOSE, "unknown_kind"))


def _real_root(root):
    root = os.path.abspath(os.fspath(root))
    here = os.sep
    for part in [p for p in root.split(os.sep) if p]:
        here = os.path.join(here, part)
        if os.path.islink(here):
            raise PersonalDataError(REAL_DIRS)
    if not os.path.isdir(root):
        raise PersonalDataError(MISSING_DIR)
    try:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise PersonalDataError(MISSING_DIR) from None


def personal_data_register(root):
    """The register for the campaign folder ``root`` as a JSON-compatible dict; never reads or prints a record value."""
    root_fd = _real_root(root)
    state = {"rows": [], "matched": set(), "seen": 0}
    try:
        for name in ("kit", "private"):
            try:
                st = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                raise PersonalDataError(INCOMPLETE) from None
            if not stat.S_ISDIR(st.st_mode):
                raise PersonalDataError(REAL_DIRS)
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            except OSError:
                raise PersonalDataError(REAL_DIRS) from None
            try:
                _walk(child, (name,), state)
            finally:
                os.close(child)
    finally:
        os.close(root_fd)
    rows = state["rows"]
    for kind, path, purpose, _adapter, _cap in CATALOG:
        if kind not in state["matched"]:
            rows.append(_row(kind, path, purpose, "absent", 0))
    rows.sort(key=lambda row: (row["kind"], row["path"]))
    return {"schema_version": 1, "files": rows, "notice": NOTICE}


# --- text -----------------------------------------------------------------------------------------------------

def _safe(text):
    """Printable ASCII as is; every other character (control, bidi, non-ASCII, undecodable byte) as a \\u escape."""
    out = []
    for char in text:
        code = ord(char)
        if 32 <= code < 127 and char != "\\":
            out.append(char)
        else:
            out.append("\\u%04x" % code if code <= 0xFFFF else "\\U%08x" % code)
    return "".join(out)


def render_text(report):
    """Tab-separated columns (the JSON row keys), one row per line, then the notice once."""
    lines = ["\t".join(COLUMNS)]
    for row in report["files"]:
        lines.append("\t".join(_safe(str(row[key])) if row[key] is not None else "-" for key in COLUMNS))
    lines.append(report["notice"])
    return "\n".join(lines)


def exit_code(report):
    """0 for a complete report; 1 when any recognized store was unreadable."""
    return 1 if any(row["status"] == "unreadable" for row in report["files"]) else 0
