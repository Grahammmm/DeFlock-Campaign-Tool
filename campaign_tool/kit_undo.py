"""kit-response-undo: take the latest recorded step off a hand-sent request, keeping the evidence.

Private and local: it reads and writes ``kit/sent.json`` and the append-only ``kit/sent-undone.json`` only.
Nothing is fetched, sent or published, and no ledger object, source or public content is touched.

Two files are written in a fixed order and are **not** atomic together. The audit receipt is written (and fsynced)
first; only then is ``sent.json`` replaced with the shortened step list and the receipt's ``undo_id`` stored on the
entry. A crash or write failure between the two leaves a receipt whose id is not on the entry: that is an *attempted*
undo, never counted as applied, and a retry writes a new receipt with a new id.

The archive is read through one bounded, non-following descriptor, and an undo that would push its serialized size
past ``ARCHIVE_MAX_BYTES`` is refused before either file changes; see ``docs/KIT-UNDO-ARCHIVE.md``.
"""
import errno
import hashlib
import json
import os
import stat
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


def _read_bounded(path, bad):
    """The bytes of one regular file opened without following a symlink, never more than the cap plus one."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if os.path.lexists(path):
            raise bad("not a regular file") from None
        return None
    except OSError as exc:
        raise bad("not a regular file" if exc.errno in (errno.ELOOP, errno.ENXIO) else "cannot be opened") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise bad("not a regular file")
        if info.st_size > ARCHIVE_MAX_BYTES:
            raise bad("too large")
        chunks, total = [], 0
        while total <= ARCHIVE_MAX_BYTES:
            try:
                chunk = os.read(fd, min(1 << 20, ARCHIVE_MAX_BYTES + 1 - total))
            except OSError:
                raise bad("cannot be read") from None
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > ARCHIVE_MAX_BYTES:
            raise bad("too large")
        return b"".join(chunks)
    finally:
        os.close(fd)


def serialized_size(document):
    """Exact bytes ``kit._write_json`` writes for the document (indent=2, non-ASCII kept, final newline)."""
    return len((json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


class _DuplicateKey(Exception):
    pass


def _unique_members(pairs):  # object_pairs_hook: a repeated decoded member name at any depth is ambiguous
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def read_archive(kit_dir):
    """(path, receipts) of kit/sent-undone.json; no file means no receipts. A bad file is never reset.

    Any repeated object member name anywhere in the file (escaped spellings included) is refused as
    ambiguous, so a last-key-wins read can never drop earlier receipts; nothing is written on refusal.

    A document the JSON decoder cannot finish because its nesting exhausts the interpreter (a RecursionError raised
    while decoding, object-pairs hook included) is refused with one fixed, value-free message. Only that decode/load
    call is covered: later failures, such as re-encoding the archive, keep their own diagnostics."""
    path = Path(kit_dir) / ARCHIVE

    def bad(reason):
        return ValueError("kit/sent-undone.json unreadable: " + reason)
    data = _read_bounded(path, bad)
    if data is None:
        return path, []
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_members)
    except _DuplicateKey:
        raise bad("duplicate JSON keys") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise bad("not JSON") from None
    except RecursionError:
        raise bad("JSON nesting is too deep") from None
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
    """Remove the last recorded step of a hand-sent request and return what was removed (kind, date, count).

    Both prospective outputs (kit/sent-undone.json and the complete kit/sent.json) are measured against their
    readers' byte caps before the audit receipt or any other write; either overflow refuses with nothing changed."""
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
    candidate = {"schema_version": 1, "undos": [*receipts, receipt]}
    try:
        too_big = serialized_size(candidate) > ARCHIVE_MAX_BYTES  # the reader's own limit, checked before any write
    except UnicodeEncodeError:
        raise ValueError("kit/sent-undone.json cannot be written as UTF-8; nothing was changed") from None
    except RecursionError:  # a decodable archive need not be re-encodable by the indented writer
        raise ValueError("kit/sent-undone.json cannot be serialized; nothing was changed") from None
    if too_big:
        raise ValueError("kit/sent-undone.json would exceed 16 MiB; nothing was changed")
    # Active store: the complete prospective kit/sent.json must fit its own reader's cap (independent of the archive).
    active = {"schema_version": 1, "requests": dict(sorted({**requests, key: after}.items()))}
    try:
        active_too_big = serialized_size(active) > kit_track.MAX_BYTES
    except UnicodeEncodeError:
        raise ValueError("kit/sent.json cannot be written as UTF-8; nothing was changed") from None
    if active_too_big:
        raise ValueError("kit/sent.json would exceed 1 MiB; nothing was changed")
    kit._write_json(archive_path, candidate)  # audit first (new inode, 0600)
    _fsync(archive_path)
    _fsync(kit_dir)
    kit._write_json(kit_dir / "sent.json", active)
    return {"agency_id": agency_id, "request": request, "label": label, "kind": removed["kind"], "on": removed["on"],
            "undone_steps": len(undo_ids)}
