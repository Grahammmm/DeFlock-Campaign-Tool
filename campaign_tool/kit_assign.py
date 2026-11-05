"""Private request owners (DF1103-05): a teammate's label per recorded request in kit/assignments.json (0600).

An owner is an alias the volunteer team chose, not a verified identity. Assigning never changes a due date, a response
step or any other store, and nothing is sent or published from this module.
"""
import json
import os
import unicodedata
from pathlib import Path

from . import kit, kit_track

LABEL_MAX = 40
MAX_BYTES = 1 << 20
DIGITS_MAX = 6  # seven or more decimal digits make a label phone-like
FILE = "kit/assignments.json"


def owner_label(value, flag="--to"):
    """The normalized owner label (whitespace collapsed, case kept, 1-40 characters) or ValueError naming ``flag``.

    The refused text is never echoed. No email address, phone number, markup or control character is accepted."""
    if not isinstance(value, str) or any(c in "<>" or unicodedata.category(c).startswith("C") for c in value):
        raise ValueError(f"{flag} must be a plain-text label without <, > or control characters")
    text = " ".join(value.split())
    if not text or len(text) > LABEL_MAX:
        raise ValueError(f"{flag} must be 1-{LABEL_MAX} characters after collapsing whitespace")
    if "@" in unicodedata.normalize("NFKC", text):
        raise ValueError(f"{flag} is a teammate's label, not an email address")
    if sum(c.isdecimal() for c in text) > DIGITS_MAX:
        raise ValueError(f"{flag} is a teammate's label, not a phone number")
    return text


def _pairs(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _kit_dir(root):
    kit_dir = Path(root) / "kit"
    if kit_dir.is_symlink():
        raise ValueError("kit/ must not be a symlink")
    return kit_dir


def _entry_ok(key, label):
    parts = kit_track._split(key) if isinstance(key, str) else None
    return bool(parts and kit.AGENCY_ID.match(parts[0]) and key == kit_track._key(*parts)
                and isinstance(label, str) and _label_ok(label))


def _label_ok(label):
    try:
        return owner_label(label) == label
    except ValueError:
        return False


def read_assignments(root):
    """``{sent.json key: owner label}`` from kit/assignments.json; {} when absent. Never resets a bad file.

    Orphans (a key with no recorded request) are kept as stored; callers ignore them."""
    path = _kit_dir(root) / "assignments.json"

    def bad(reason):
        return ValueError(f"{FILE} unreadable: {reason}")
    if not os.path.lexists(path):
        return {}
    if path.is_symlink() or not path.is_file():
        raise bad("not a regular file")
    if path.stat().st_size > MAX_BYTES:
        raise bad("larger than 1 MiB")
    try:
        document = json.loads(path.read_bytes().decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, ValueError):  # json.JSONDecodeError and the duplicate-key error are ValueErrors
        raise bad("not JSON, or a key is repeated") from None
    if not isinstance(document, dict) or set(document) != {"schema_version", "assignments"} \
            or type(document["schema_version"]) is not int or document["schema_version"] != 1 \
            or not isinstance(document["assignments"], dict):
        raise bad("expected schema_version 1 and an assignments object only")
    for key, label in document["assignments"].items():
        if not _entry_ok(key, label):
            raise bad("an assignment is malformed")
    return document["assignments"]


def assign(root, agency_id, *, request="main", to=None, clear=False):
    """Set (``to``) or remove (``clear``) the owner of one recorded request; return what is stored afterwards.

    Everything is checked before anything is written; a repeat of the stored state writes nothing."""
    if (to is None) == (not clear):
        raise ValueError("give exactly one of --to and --clear")
    request = kit_track._request(request)
    label = None if clear else owner_label(to, "--to")
    root = Path(root)
    kit_dir = _kit_dir(root)
    agencies = kit_dir / "agencies.json"
    if not agencies.is_file() or agencies.is_symlink():
        raise ValueError("run kit first: kit/agencies.json is missing")
    if not isinstance(agency_id, str) or not any(a["agency_id"] == agency_id for a in kit._load_existing(agencies)):
        raise ValueError("--agency is not in kit/agencies.json")
    key = kit_track._key(agency_id, request)
    if key not in kit_track.read_sent(root):
        raise ValueError(f"no recorded request {kit_track._label(agency_id, request)}: "
                         "owners are assigned to requests recorded with kit-sent or kit-sent-import")
    stored = read_assignments(root)
    result = {"agency_id": agency_id, "request": request, "owner": label}
    if stored.get(key) == label:  # same owner, or clearing a request that has none: nothing to write
        return {**result, "changed": False}
    updated = {k: v for k, v in stored.items() if k != key}
    if label is not None:
        updated[key] = label
    document = {"schema_version": 1, "assignments": dict(sorted(updated.items()))}
    if len(json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8")) + 1 > MAX_BYTES:
        raise ValueError(f"{FILE} would be larger than 1 MiB; owner not recorded")
    kit._write_json(kit_dir / "assignments.json", document)  # temp file in kit/ (0600), then an atomic replace
    return {**result, "changed": True}
