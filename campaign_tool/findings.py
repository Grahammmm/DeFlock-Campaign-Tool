"""Offline review receipts for finding drafts (self-asserted reviewer names; no identity check).

A receipt is bound to the hash of the draft without its publication fields, so moving a draft
through states keeps it bound while any edit to the reviewed fields makes it stale."""
import contextlib
import errno
import json
import os
import re
import stat
import tempfile
import unicodedata
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

from .content import FINDING_KEYS
from .review import ROLES, _who, content_hash, review_blockers

PUBLICATION_FIELDS = ("state", "published_at", "updated_at", "corrections")
DECISIONS = ("approve", "reject", "changes_requested")
MAX_DRAFT_BYTES = 1024 * 1024
MAX_RECEIPTS_BYTES = 4 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def reviewed_view(draft):
    return {k: v for k, v in draft.items() if k not in PUBLICATION_FIELDS}


def reviewed_hash(draft):
    return content_hash(reviewed_view(draft))


def _no_constant(name):
    raise ValueError(name)


def _read_json(path, limit, what):
    """Parse one regular, non-symlink file; errors name the path, never its content."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if os.path.islink(path):
            raise ValueError(f"{path}: {what} is a symlink; refusing to follow it") from None
        raise ValueError(f"{path}: cannot open {what} ({exc.strerror})") from None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{path}: {what} is not a regular file")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path}: {what} is larger than {limit} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{path}: {what} is not UTF-8") from None
    try:
        return json.loads(text, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise ValueError(f"{path}: {what} is not valid JSON") from None


def load_draft(path):
    draft = _read_json(path, MAX_DRAFT_BYTES, "draft")
    if not isinstance(draft, dict):
        raise ValueError(f"{path}: draft must be a JSON object")
    unknown = sorted(set(draft) - FINDING_KEYS)
    if unknown:
        raise ValueError(f"{path}: unknown key {unknown[0]!r}")
    if not isinstance(draft.get("id"), str) or not _ID.fullmatch(draft["id"]):
        raise ValueError(f"{path}: key 'id' must match [A-Za-z0-9_-]{{1,64}}")
    return draft


def _receipts_path(root, draft):
    for folder in (root / "private", root / "private" / "reviews"):
        if os.path.lexists(folder) and (os.path.islink(folder) or not os.path.isdir(folder)):
            raise ValueError(f"{folder}: must be a real directory, not a symlink or a file")
    return root / "private" / "reviews" / (draft["id"] + ".json")


def _load_receipts(path):
    if not os.path.lexists(path):
        return []
    receipts = _read_json(path, MAX_RECEIPTS_BYTES, "receipt file")
    if not isinstance(receipts, list):
        raise ValueError(f"{path}: receipt file must be a JSON list")
    return receipts


@contextlib.contextmanager
def _locked(folder):
    """Hold an exclusive lock on the stable private/reviews/.lock file (never replaced, so every
    writer locks the same inode) for the whole read/check/append/replace transaction."""
    path = folder / ".lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        if os.path.islink(path):
            raise ValueError(f"{path}: lock file is a symlink; refusing to follow it") from None
        raise ValueError(f"{path}: cannot open lock file ({exc.strerror})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{path}: lock file is not a regular file")
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:  # Windows: msvcrt.LK_LOCK gives up after about 10 s of contention, so keep retrying
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError as exc:
                    if exc.errno != errno.EDEADLOCK:
                        raise
        yield
    finally:
        os.close(fd)  # closing the descriptor releases either lock kind


def _text(name, value, limit):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"--{name} must not be empty")
    if len(value) > limit:
        raise ValueError(f"--{name} is longer than {limit} characters")
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError(f"--{name} contains control characters")
    return value.strip()


def add_receipt(root, draft, role, decision, reviewer, rationale, now=None):
    if role not in ROLES:
        raise ValueError(f"--role must be one of {sorted(ROLES)}")
    if decision not in DECISIONS:
        raise ValueError(f"--decision must be one of {list(DECISIONS)}")
    reviewer = _text("reviewer", reviewer, 120)
    rationale = _text("rationale", rationale, 2000)
    if _who(reviewer) == _who(draft.get("author")):
        raise ValueError("the author cannot review their own finding")
    digest = reviewed_hash(draft)
    path = _receipts_path(root, draft)
    for folder in (root / "private", path.parent):
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
    with _locked(path.parent):
        receipts = _load_receipts(path)
        if any(isinstance(r, dict) and r.get("content_sha256") == digest and r.get("role") == role
               and r.get("decision") == decision and _who(r.get("reviewer")) == _who(reviewer) for r in receipts):
            raise ValueError(f"{path}: this receipt is already recorded for the current content")
        when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        receipt = {"content_sha256": digest, "role": role, "decision": decision, "reviewer": reviewer,
                   "rationale": rationale, "reviewed_at": when}
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".review-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(receipts + [receipt], out, indent=2, ensure_ascii=False)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return dict(receipt, id=draft["id"], written=str(path))


def review_status(root, draft):
    """Read-only: current-content receipts, stale ones, and the review_blockers list."""
    digest = reviewed_hash(draft)
    receipts = _load_receipts(_receipts_path(root, draft))
    current = sum(isinstance(r, dict) and r.get("content_sha256") == digest for r in receipts)
    return {"id": draft["id"], "content_sha256": digest, "receipts_for_this_content": current,
            "stale_receipts": len(receipts) - current,
            "review_blockers": review_blockers(reviewed_view(draft), receipts)}
