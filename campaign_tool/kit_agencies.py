"""kit-agency: add, select and deselect agencies in kit/agencies.json without hand-editing JSON.

Validates the whole list first (duplicate ids, non-boolean ``selected``), writes atomically through
``kit._write_json`` and keeps the file mode. ``kit`` keeps an existing agencies.json, so changes survive a re-run.
Offline; nothing is fetched or sent.
"""
import os
import stat
from datetime import date
from pathlib import Path

from . import kit

KINDS = tuple(kit.LAW_ENFORCEMENT) + kit.GOVERNING + ("other",)


def _path(root):
    path = Path(root) / "kit" / "agencies.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("run kit first: kit/agencies.json is missing")
    return path


def load_agencies(root):
    """Return the raw agency list after kit's own checks plus duplicate and ``selected`` checks. Read-only."""
    path = _path(root)
    kit._load_existing(path)
    agencies = kit._read_json(path)
    seen = set()
    for entry in agencies:
        if entry["agency_id"] in seen:
            raise ValueError(f"duplicate agency_id {entry['agency_id']} in kit/agencies.json")
        seen.add(entry["agency_id"])
    for entry in agencies:
        if "selected" in entry and not isinstance(entry["selected"], bool):
            raise ValueError(f"{entry['agency_id']}: selected must be true or false")
    return agencies


def _save(root, agencies):
    path = _path(root)
    mode = stat.S_IMODE(path.stat().st_mode)
    kit._write_json(path, agencies)
    os.chmod(path, mode)


def set_selected(root, agency_id, value):
    """Set ``selected`` on the one entry with ``agency_id``; no write when it already has that value."""
    agencies = load_agencies(root)
    matches = [entry for entry in agencies if entry["agency_id"] == agency_id]
    if len(matches) != 1:
        raise ValueError(f"agency_id {agency_id!r} must appear exactly once in kit/agencies.json (found {len(matches)})")
    entry, value = matches[0], bool(value)
    changed = entry.get("selected", True) is not value
    if changed:
        entry["selected"] = value
        _save(root, agencies)
    return {"agency_id": agency_id, "selected": value, "changed": changed}


def _has_control(text):
    return any(ord(c) < 32 or 127 <= ord(c) < 160 or c in "\u2028\u2029" for c in text)


def add_agency(root, agency_id, name, kind, note=None, today=None):
    """Append an organizer-added agency (selected, unverified). Refuses bad fields by name; file unchanged then."""
    from .cli import read_config  # local import: cli imports this module lazily
    agencies = load_agencies(root)
    state = read_config(Path(root))["state"].lower()
    if not isinstance(agency_id, str) or not kit.AGENCY_ID.match(agency_id):
        raise ValueError("agency_id must be lowercase like 'ca-example-police'")
    if agency_id[:2] != state:
        raise ValueError(f"agency_id must start with '{state}-' for this campaign's state")
    if any(entry["agency_id"] == agency_id for entry in agencies):
        raise ValueError(f"agency_id {agency_id} already exists in kit/agencies.json")
    if not isinstance(name, str) or not 3 <= len(name.strip()) <= 120 or _has_control(name):
        raise ValueError("name must be 3-120 characters with no control characters")
    if kind not in KINDS:
        raise ValueError("kind must be one of " + ", ".join(KINDS))
    if note is not None and (not isinstance(note, str) or len(note) > 300):
        raise ValueError("note must be at most 300 characters")
    entry = {"agency_id": agency_id, "name": name.strip(), "kind": kind, "selected": True, "verified": False,
             "added_by": "organizer", "added_on": (today or date.today()).isoformat()}
    if note is not None:
        entry["note"] = note
    agencies.append(entry)
    _save(root, agencies)
    return entry
