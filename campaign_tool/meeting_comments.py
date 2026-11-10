"""Record a comment the organizer submitted BY HAND on a staged meeting (DF1207-09).

Offline bookkeeping only: nothing is sent, fetched or published. ``kit/comments-sent.json`` is private; for a
letter only its SHA-256 and byte count are kept, never its text or its path.
"""
import hashlib
import json
import os
import re
import stat
from datetime import date, datetime, timezone
from pathlib import Path

from .content import MAX_JSON_BYTES, _read_json, load_meeting
from .kit import _kit_folder, _write_json

CHANNELS = ("email", "portal", "mail", "in_person")
MAX_LETTER_BYTES = 5 * 1024 * 1024
MEETING_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")
SHA256 = re.compile(r"[0-9a-f]{64}")
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
LABEL = "kit/comments-sent.json"


class MeetingCommentError(ValueError):
    pass


def _day(text, label):
    if not isinstance(text, str) or not DAY.fullmatch(text):
        raise MeetingCommentError(f"{label} must be a real YYYY-MM-DD date")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise MeetingCommentError(f"{label} must be a real YYYY-MM-DD date") from None


def _entry(value, meeting_id):
    """One stored entry, or MeetingCommentError naming it; extra keys and wrong types are refused."""
    bad = MeetingCommentError(f"{LABEL}: entry {meeting_id!r} is malformed")
    if not isinstance(value, dict) or not {"on", "channel", "recorded_at"} <= set(value) <= {
            "on", "channel", "recorded_at", "letter_sha256", "letter_bytes"}:
        raise bad
    _day(value["on"], f"{LABEL} entry {meeting_id!r} on")
    if value["channel"] not in CHANNELS or not isinstance(value["recorded_at"], str):
        raise bad
    if ("letter_sha256" in value) != ("letter_bytes" in value):
        raise bad
    if "letter_sha256" in value and (not isinstance(value["letter_sha256"], str)
                                     or not SHA256.fullmatch(value["letter_sha256"])
                                     or type(value["letter_bytes"]) is not int or value["letter_bytes"] < 0):
        raise bad
    return value


def read(root):
    """``{meeting_id: entry}`` from kit/comments-sent.json; ``{}`` when the file does not exist.

    Raises MeetingCommentError when the file is a symlink, not JSON, or not the documented shape."""
    path = Path(root) / "kit" / "comments-sent.json"
    if os.path.islink(path):
        raise MeetingCommentError(f"{LABEL} must not be a symlink")
    if not os.path.lexists(path):
        return {}
    try:
        document = _read_json(path)
    except (ValueError, OSError) as exc:
        raise MeetingCommentError(f"{LABEL} is not readable JSON ({type(exc).__name__})") from None
    if (not isinstance(document, dict) or document.get("schema_version") != 1
            or set(document) != {"schema_version", "comments"} or not isinstance(document["comments"], dict)):
        raise MeetingCommentError(f"{LABEL} is not the expected {{schema_version: 1, comments: {{...}}}} shape")
    return {str(key): _entry(value, key) for key, value in document["comments"].items()}


def _letter(path):
    """``(sha256 hex, byte count)`` of a regular, non-symlink file of at most 5 MiB; the text is not kept."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise MeetingCommentError(f"--file: cannot read it as a regular file ({exc.strerror})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MeetingCommentError("--file: must be a regular file, not a symlink or folder")
        digest, size = hashlib.sha256(), 0
        while chunk := os.read(fd, 65536):
            size += len(chunk)
            if size > MAX_LETTER_BYTES:
                raise MeetingCommentError("--file: must be at most 5 MiB")
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest(), size


def record(root, meeting_id, on, channel, file=None, replace=False, today=None, now=None):
    """Store ``{on, channel, [letter_sha256, letter_bytes], recorded_at}`` for a scheduled staged meeting."""
    root, today = Path(root), today or date.today()
    if (not isinstance(meeting_id, str) or not MEETING_ID.fullmatch(meeting_id) or meeting_id in (".", "..")):
        raise MeetingCommentError("invalid meeting id; use 1–80 letters, digits, dots, underscores or hyphens")
    if channel not in CHANNELS:
        raise MeetingCommentError(f"--channel must be one of {', '.join(CHANNELS)}")
    day = _day(on, "--on")
    path = root / "content" / "meetings" / (meeting_id + ".json")
    if os.path.islink(path) or not path.is_file():
        raise MeetingCommentError(f"no staged meeting {meeting_id}; run `meetings-stage --id {meeting_id}` first")
    meeting = load_meeting(_read_json(path), f"meetings/{path.name}", today=today)
    if meeting["id"] not in (None, meeting_id):
        raise MeetingCommentError(f"meeting {meeting_id}: id differs from its filename; re-stage it")
    if meeting["status"] != "scheduled":
        raise MeetingCommentError(f"meeting {meeting_id} is {meeting['status']}; no comment is recorded")
    if day > today:
        raise MeetingCommentError(f"--on {on} is in the future; record a comment only after submitting it")
    if on > meeting["starts_at"][:10]:
        raise MeetingCommentError(f"--on {on} is after the meeting day {meeting['starts_at'][:10]}")
    comments = read(root)  # a malformed file refuses here, before anything is written
    if meeting_id in comments and not replace:
        raise MeetingCommentError(f"meeting {meeting_id} already has a comment recorded on "
                                  f"{comments[meeting_id]['on']}; pass --replace to overwrite it")
    entry = {"on": on, "channel": channel}
    if file is not None:
        entry["letter_sha256"], entry["letter_bytes"] = _letter(file)
    entry["recorded_at"] = (now or datetime.now(timezone.utc)).isoformat(timespec="microseconds")
    comments[meeting_id] = entry
    document = {"schema_version": 1, "comments": dict(sorted(comments.items()))}
    # RF0015-6: read() refuses a store over MAX_JSON_BYTES, so refuse, before any output, a write whose exact
    # serialization (kit._write_json: indent=2, ensure_ascii=False, trailing newline, UTF-8) would exceed it.
    if len((json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")) > MAX_JSON_BYTES:
        raise MeetingCommentError(f"{LABEL}: update would exceed 2 MiB; nothing was written")
    kit_dir = root / "kit"
    _kit_folder(kit_dir, 0o700)
    target = kit_dir / "comments-sent.json"
    if os.path.islink(target):
        raise MeetingCommentError(f"{LABEL} must not be a symlink")
    _write_json(target, document)
    return entry
