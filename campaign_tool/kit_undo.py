"""kit-response-undo: take the latest recorded step off a hand-sent request, keeping the evidence.

Private and local: it reads and writes ``kit/sent.json`` and the append-only ``kit/sent-undone.json`` only.
Nothing is fetched, sent or published, and no ledger object, source or public content is touched.

Two files are written in a fixed order and are **not** atomic together. The audit receipt is written (and fsynced)
first; only then is ``sent.json`` replaced with the shortened step list and the receipt's ``undo_id`` stored on the
entry. A crash or write failure between the two leaves a receipt whose id is not on the entry: that is an *attempted*
undo, never counted as applied, and a retry writes a new receipt with a new id.
"""
import hashlib
import json
import os
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import kit, kit_track

ARCHIVE = "sent-undone.json"
REASON_MAX = 200
ARCHIVE_MAX_BYTES = 16 << 20  # the archive only grows; far above any real organizer's history


def clean_reason(reason):
    """The reason in the same normalized form as a step note (1-200 characters); the text is never echoed."""
    if not isinstance(reason, str) or any(c in "<>" or unicodedata.category(c).startswith("C") for c in reason):
        raise ValueError("--reason must be plain text without <, > or control characters")
    text = " ".join(reason.split())
    if not text or len(text) > REASON_MAX:
        raise ValueError(f"--reason must be 1-{REASON_MAX} characters after collapsing whitespace")
    return text


def _digest(entry):  # a stable hash of one sent.json entry
    return hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def read_archive(kit_dir):
    """(path, receipts) of kit/sent-undone.json; no file means no receipts. A bad file is never reset."""
    path = Path(kit_dir) / ARCHIVE

    def bad(reason):
        return ValueError("kit/sent-undone.json unreadable: " + reason)
    if not os.path.lexists(path):
        return path, []
    if path.is_symlink() or not path.is_file():
        raise bad("not a regular file")
    if path.stat().st_size > ARCHIVE_MAX_BYTES:
        raise bad("too large")
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise bad("not JSON") from None
    if (not isinstance(document, dict) or document.get("schema_version") != 1
            or not isinstance(document.get("undos"), list)):
        raise bad("expected schema_version 1 with an undos list")
    ids = [item.get("undo_id") if isinstance(item, dict) else None for item in document["undos"]]
    if not all(isinstance(i, str) and kit_track.UNDO_ID.fullmatch(i) for i in ids) or len(set(ids)) != len(ids):
        raise bad("every receipt needs its own undo_id")
    return path, document["undos"]


def _fsync(path):  # the file's bytes (and, for a folder, its entries) are on disk before the next write
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def undo_step(root, agency_id, *, request="main", reason, today=None):
    """Remove the last recorded step of a hand-sent request and return what was removed (kind, date, count)."""
    root, request = Path(root), kit_track._request(request)
    reason = clean_reason(reason)
    kit_dir = root / "kit"
    if kit_dir.is_symlink():
        raise ValueError("kit/ must not be a symlink")
    requests = kit_track.read_sent(root)
    key, label = kit_track._key(agency_id, request), kit_track._label(agency_id, request)
    entry = requests.get(key) if "#" not in agency_id else None
    if entry is None:
        raise ValueError(f"no sent request {label}; nothing to undo")
    if entry["channel"] == "muckrock":
        raise ValueError("a MuckRock request is not hand-sent; kit-response-undo only takes email, portal or mail requests")
    steps = entry["steps"]
    if not steps:
        raise ValueError(f"{label} has no recorded step to undo")
    removed = steps[-1]
    if not kit_track.stored_step_ok(entry, removed):
        raise ValueError(f"kit/sent.json unreadable: the last step of {label!r} is malformed")
    kit_track.validate_steps({**entry, "steps": steps[:-1]}, label, today=today)  # record_step's own rules
    undo_ids = [*entry.get("undo_ids", []), uuid.uuid4().hex]
    if len(undo_ids) > kit_track.UNDO_IDS_MAX:
        raise ValueError(f"{label} already has the most undone steps this file keeps")
    archive_path, receipts = read_archive(kit_dir)  # refused before either write
    after = {**entry, "steps": steps[:-1], "undo_ids": undo_ids}
    receipt = {"undo_id": undo_ids[-1], "agency_id": agency_id, "request": request,
               "undone_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "reason": reason,
               "step": removed, "entry_before_sha256": _digest(entry), "entry_after_sha256": _digest(after)}
    kit._write_json(archive_path, {"schema_version": 1, "undos": [*receipts, receipt]})  # audit first (new inode, 0600)
    _fsync(archive_path)
    _fsync(kit_dir)
    requests[key] = after
    kit._write_json(kit_dir / "sent.json", {"schema_version": 1, "requests": dict(sorted(requests.items()))})
    return {"agency_id": agency_id, "request": request, "label": label, "kind": removed["kind"], "on": removed["on"],
            "undone_steps": len(undo_ids)}
