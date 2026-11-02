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
from datetime import date, datetime, timezone

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
MAX_ROSTER_BYTES, MAX_REVIEWERS = 256 * 1024, 200
ROSTER_KEYS, REVIEWER_KEYS = frozenset(("schema_version", "reviewers")), frozenset(("name", "roles", "added_on"))
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


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
    if not stat.S_ISREG(os.fstat(fd).st_mode):  # checked before fdopen, which refuses a directory itself
        os.close(fd)
        raise ValueError(f"{path}: {what} is not a regular file")
    with os.fdopen(fd, "rb") as handle:
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
    roster = _load_roster(root)
    entry = roster and _rostered(roster, reviewer)
    if roster is not None and (entry is None or role not in entry["roles"]):
        raise ValueError(f"{reviewer} is not " + ("on the reviewer roster" if entry is None
                                                  else f"assigned the {role} role"))
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
        _write_private(path, receipts + [receipt], ".review-")
    return dict(receipt, id=draft["id"], written=str(path))


def _write_private(path, document, prefix):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=prefix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def review_status(root, draft):
    """Read-only: current-content receipts, stale ones, and the review_blockers list."""
    digest = reviewed_hash(draft)
    roster = _load_roster(root)
    receipts = _load_receipts(_receipts_path(root, draft))
    current = sum(isinstance(r, dict) and r.get("content_sha256") == digest for r in receipts)
    return {"id": draft["id"], "content_sha256": digest, "receipts_for_this_content": current,
            "stale_receipts": len(receipts) - current,
            "review_blockers": review_blockers(reviewed_view(draft), receipts),
            **({} if roster is None else {"unrostered_receipts": _unrostered(roster, receipts)})}


def _roster_path(root):
    folder = root / "private"
    if os.path.lexists(folder) and (os.path.islink(folder) or not os.path.isdir(folder)):
        raise ValueError(f"{folder}: must be a real directory, not a symlink or a file")
    return folder / "reviewers.json"


def _rostered(roster, name):
    who = _who(name)
    return next((e for e in roster["reviewers"] if who is not None and _who(e["name"]) == who), None)


def _unrostered(roster, receipts):
    """Distinct (reviewer, role) pairs of receipts, current or stale, not rostered for that role."""
    pairs = [{"reviewer": r.get("reviewer"), "role": r.get("role")} for r in receipts if isinstance(r, dict)]
    return [p for i, p in enumerate(pairs) if p not in pairs[:i]
            and ((e := _rostered(roster, p["reviewer"])) is None or p["role"] not in e["roles"])]


def _object(where, document, expected):
    if not isinstance(document, dict):
        raise ValueError(f"{where}: must be a JSON object")
    for key in sorted(set(document) ^ expected):
        raise ValueError(f"{where}: {'unknown' if key in document else 'missing'} key {key!r}")


def _load_roster(root):
    """None when private/reviewers.json does not exist; otherwise the validated roster."""
    path = _roster_path(root)
    if not os.path.lexists(path):
        return None
    roster = _read_json(path, MAX_ROSTER_BYTES, "reviewer roster")
    _object(path, roster, ROSTER_KEYS)
    if type(roster["schema_version"]) is not int or roster["schema_version"] != 1:
        raise ValueError(f"{path}: key 'schema_version' must be 1")
    if not isinstance(roster["reviewers"], list) or len(roster["reviewers"]) > MAX_REVIEWERS:
        raise ValueError(f"{path}: key 'reviewers' must be a list of at most {MAX_REVIEWERS} entries")
    seen = set()
    for index, entry in enumerate(roster["reviewers"]):
        where = f"{path}: reviewers[{index}]"
        _object(where, entry, REVIEWER_KEYS)
        who, roles, added = _who(entry["name"]), entry["roles"], entry["added_on"]
        if who is None or who in seen:
            raise ValueError(f"{where}.name is empty or duplicates an earlier reviewer")
        seen.add(who)
        if not isinstance(roles, list) or not roles or not all(isinstance(r, str) and r in ROLES for r in roles):
            raise ValueError(f"{where}.roles must be a non-empty list of {sorted(ROLES)}")
        try:
            date.fromisoformat(added if isinstance(added, str) and _DATE.fullmatch(added) else "")
        except ValueError:
            raise ValueError(f"{where}.added_on must be a YYYY-MM-DD date") from None
    return roster


def add_reviewer(root, name, roles, today=None):
    """Add a reviewer or merge roles into an existing one (roles are never removed; added_on is kept)."""
    name = _text("name", name, 120)
    if not roles or any(role not in ROLES for role in roles):
        raise ValueError(f"--role must be one of {sorted(ROLES)}")
    path = _roster_path(root)
    _load_roster(root)  # refuse a bad roster before any write
    path.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with _locked(path.parent):
        roster = _load_roster(root) or {"schema_version": 1, "reviewers": []}
        entry = _rostered(roster, name)
        if entry is None and len(roster["reviewers"]) >= MAX_REVIEWERS:
            raise ValueError(f"{path}: the roster already has {MAX_REVIEWERS} reviewers")
        if entry is None:
            entry = {"name": name, "roles": [], "added_on": str(today or date.today())}
            roster["reviewers"].append(entry)
        entry["roles"] = sorted(set(entry["roles"]) | set(roles))
        _write_private(path, roster, ".reviewers-")
    return dict(entry, written=str(path))


def list_reviewers(root):
    roster = _load_roster(root)  # read-only
    return {"roster": "absent" if roster is None else "present", "reviewers": roster["reviewers"] if roster else []}
