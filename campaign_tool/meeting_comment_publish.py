"""Stage the exact comment the campaign submitted by hand on a meeting, for the public meeting page (DF1407-06).

Offline only: nothing is sent, built or deployed. The text must be byte for byte the file whose SHA-256 ``meetings-comment-sent``
recorded for the meeting, so the public copy is what was submitted. The date and channel come from that private record, never
from a flag. The command writes one optional ``submitted_comment`` key into ``content/meetings/<id>.json``; a later normal site
build renders it. A clean lint is not privacy clearance, and the legal-claim notes are advisory.
"""
import hashlib
import os
import stat
from datetime import date
from pathlib import Path

from .content import COMMENT_MAX_BYTES, _read_json, comment_text_problems, load_meeting
from .kit import _write_json
from .legal_claims import legal_notes
from .meeting_comments import MEETING_ID, MeetingCommentError, read as read_comments


class MeetingCommentPublishError(ValueError):
    pass


def _bytes(path):
    """The bytes of a regular, non-symlink file of at most 16 KiB; never more than one byte past the limit is read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise MeetingCommentPublishError(f"--file: cannot read it as a regular file ({exc.strerror})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MeetingCommentPublishError("--file: must be a regular file, not a symlink or folder")
        data = b""
        while len(data) <= COMMENT_MAX_BYTES and (chunk := os.read(fd, COMMENT_MAX_BYTES + 1 - len(data))):
            data += chunk
    finally:
        os.close(fd)
    return data


def publish(root, meeting_id, file, *, signer_public=False, replace=False, today=None):
    """Stage ``submitted_comment`` for the meeting; returns ``(submitted_comment, advisory notes)``."""
    root, today = Path(root), today or date.today()
    if signer_public is not True:
        raise MeetingCommentPublishError("--signer-public is required: confirm that whoever signed the comment agreed to be "
                                         "named publicly; nothing was staged")
    if not isinstance(meeting_id, str) or not MEETING_ID.fullmatch(meeting_id) or meeting_id in (".", ".."):
        raise MeetingCommentPublishError("invalid meeting id; use 1–80 letters, digits, dots, underscores or hyphens")
    folder = root / "content" / "meetings"
    for part in (root / "content", folder):
        if os.path.islink(part):
            raise MeetingCommentPublishError("content/meetings must not be reached through a symlink")
    path = folder / (meeting_id + ".json")
    if os.path.islink(path) or not os.path.isfile(path):
        raise MeetingCommentPublishError(f"no staged meeting {meeting_id} in content/meetings/")
    document = _read_json(path)
    meeting = load_meeting(document, f"meetings/{meeting_id}.json", today=today)
    if meeting["id"] not in (None, meeting_id):
        raise MeetingCommentPublishError(f"meeting {meeting_id}: id differs from its filename")
    if "submitted_comment" in document and not replace:
        raise MeetingCommentPublishError(f"meeting {meeting_id} already shows a submitted comment; pass --replace to overwrite it")
    try:
        entry = read_comments(root).get(meeting_id)
    except MeetingCommentError as exc:
        raise MeetingCommentPublishError(str(exc)) from None
    if entry is None or "letter_sha256" not in entry:
        raise MeetingCommentPublishError(f"meeting {meeting_id} has no recorded letter hash; record the submission with "
                                         "`meetings-comment-sent --file` first")
    data = _bytes(file)
    if hashlib.sha256(data).hexdigest() != entry["letter_sha256"] or len(data) != entry["letter_bytes"]:
        raise MeetingCommentPublishError("--file is not the letter recorded as submitted for this meeting (its SHA-256 or "
                                         "size differs); publish only the exact file that was submitted")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise MeetingCommentPublishError("--file: must be UTF-8 plain text") from None
    if problems := comment_text_problems(text):
        raise MeetingCommentPublishError("--file: " + "; ".join(problems))
    notes = legal_notes([(f"meetings/{meeting_id}.submitted_comment.text", text)])  # advisory: never refuses
    comment = {"on": entry["on"], "channel": entry["channel"], "text": text, "sha256": entry["letter_sha256"]}
    updated = {**document, "submitted_comment": comment}
    load_meeting(updated, f"meetings/{meeting_id}.json", today=today)  # the whole document, before any write
    if os.path.islink(path):  # checked again right before the write
        raise MeetingCommentPublishError(f"content/meetings/{meeting_id}.json must not be a symlink")
    _write_json(path, updated)
    return comment, notes

