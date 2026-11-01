"""Record records requests an organizer sent by hand; list provisional due dates.

``kit/sent.json`` holds one entry per agency: the send date, the channel, the
draft that was sent and its SHA-256. Nothing is sent from this module; the due
dates come from ``law.deadline`` and are provisional reminders only.
"""
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

from . import kit, law

CHANNELS = ("email", "portal", "mail")
MAX_BYTES = 1 << 20
EARLIEST = date(2000, 1, 1)
SHA256 = re.compile(r"[0-9a-f]{64}")
NOTE = "Provisional reminders only; the period runs from the agency's receipt. " + law.HOLIDAY_NOTE


def _day(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) and kit.DATE.fullmatch(value) else None
    except ValueError:
        return None


def _entry_ok(agency_id, entry):
    return (kit.AGENCY_ID.match(agency_id) and isinstance(entry, dict) and _day(entry.get("sent_on"))
            and entry.get("channel") in CHANNELS and entry.get("draft") == f"requests/{agency_id}.md"
            and isinstance(entry.get("draft_sha256"), str) and SHA256.fullmatch(entry["draft_sha256"])
            and isinstance(entry.get("recorded_at"), str) and isinstance(entry.get("steps"), list))


def read_sent(root):
    """Return the ``requests`` dict of kit/sent.json; {} when absent. Never resets a bad file."""
    path = Path(root) / "kit" / "sent.json"
    if not os.path.lexists(path):
        return {}

    def bad(reason):
        return ValueError("kit/sent.json unreadable: " + reason)
    if path.is_symlink() or not path.is_file():
        raise bad("not a regular file")
    if path.stat().st_size > MAX_BYTES:
        raise bad("larger than 1 MiB")
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise bad("not JSON") from None
    if not isinstance(document, dict) or document.get("schema_version") != 1 \
            or not isinstance(document.get("requests"), dict):
        raise bad("expected schema_version 1 with a requests object")
    for agency_id, entry in document["requests"].items():
        if not _entry_ok(agency_id, entry):
            raise bad(f"malformed entry {agency_id!r}")
    return document["requests"]


def _draft(root, agency_id):
    path = Path(root) / "kit" / "requests" / (agency_id + ".md")
    if path.is_symlink() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_sent(root, agency_id, sent_on, channel, *, replace=False, today=None):
    """Record one hand-sent request in kit/sent.json and return its entry."""
    root = Path(root)
    path = root / "kit" / "agencies.json"
    if not path.is_file() or path.is_symlink():
        raise ValueError("run kit first: kit/agencies.json is missing")
    if agency_id not in {entry["agency_id"] for entry in kit._load_existing(path)}:
        raise ValueError(f"agency_id {agency_id!r} is not in kit/agencies.json")
    digest = _draft(root, agency_id)
    if digest is None:
        raise ValueError(f"kit/requests/{agency_id}.md must be a regular file (not a symlink)")
    sent = _day(sent_on)
    if sent is None:
        raise ValueError("--date must be a real YYYY-MM-DD date")
    if sent > (today or date.today()) or sent < EARLIEST:
        raise ValueError(f"--date {sent_on} must be between 2000-01-01 and today")
    if channel not in CHANNELS:
        raise ValueError("--channel must be one of " + ", ".join(CHANNELS))
    requests = read_sent(root)
    previous = requests.get(agency_id)
    if previous and not replace:
        raise ValueError(f"{agency_id} already recorded as sent on {previous['sent_on']}; use --replace to correct it")
    if previous and previous["steps"]:
        raise ValueError(f"{agency_id} has recorded steps; its entry sent on {previous['sent_on']} cannot be replaced")
    entry = {"sent_on": sent_on, "channel": channel, "draft": f"requests/{agency_id}.md", "draft_sha256": digest,
             "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "steps": []}
    requests[agency_id] = entry
    kit._write_json(root / "kit" / "sent.json",
                    {"schema_version": 1, "requests": dict(sorted(requests.items()))})
    return {"agency_id": agency_id, **entry}


def status(root, today=None, law_base=None):
    """Every recorded request with its provisional determination dates (read-only)."""
    from .cli import read_config  # local import: cli imports this module lazily
    root = Path(root)
    requests = read_sent(root)
    path = root / "kit" / "agencies.json"
    names = {}
    if requests and path.is_file() and not path.is_symlink():
        names = {entry["agency_id"]: entry["name"] for entry in kit._load_existing(path)}
    try:
        package = law.load_package(kit._jurisdiction(read_config(root)), law_base)
        package_status = package["status"]
    except ValueError:
        package, package_status = None, "missing"
    rows = []
    for agency_id, entry in sorted(requests.items(), key=lambda item: (item[1]["sent_on"], item[0])):
        due = law.deadline(package, entry["sent_on"]).isoformat() if package else None
        extended = law.deadline(package, entry["sent_on"], extension=True).isoformat() if package else None
        rows.append({"agency_id": agency_id, "name": names.get(agency_id), "sent_on": entry["sent_on"],
                     "channel": entry["channel"],
                     "draft_changed_since_sent": _draft(root, agency_id) != entry["draft_sha256"],
                     "determination_due": due, "with_extension": extended, "steps": len(entry["steps"])})
    return {"requests": rows, "law_package_status": package_status, "note": NOTE}
