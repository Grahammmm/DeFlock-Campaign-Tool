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

from . import content
from .content import FINDING_KEYS
from .evidence import PREFIX, _ledger_hashes, _object_present
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
def _locked(folder, name=".lock"):
    """Hold an exclusive lock on a stable lock file (private/reviews/.lock for receipts,
    private/.publish.lock for publication; never replaced, so every writer locks the same inode)
    for the whole read/check/append/replace transaction."""
    path = folder / name
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


def _real_dir(path):
    """True for a real directory, False when absent; a symlink or a file refuses."""
    if os.path.lexists(path) and (os.path.islink(path) or not os.path.isdir(path)):
        raise ValueError(f"{path}: must be a real directory, not a symlink or a file")
    return os.path.isdir(path)


def _taken(folder, what):
    """(ids, slugs) of every *.json in ``folder``; an unreadable file refuses rather than being skipped."""
    ids, slugs = set(), set()
    for path in sorted(folder.glob("*.json")) if _real_dir(folder) else []:
        document = _read_json(path, MAX_DRAFT_BYTES, what)
        slugs.add(path.stem)
        if isinstance(document, dict):
            ids.add(document.get("id"))
            slugs.add(document.get("slug"))
    return ids, slugs


def cited_problems(root, finding):
    """Each cited sha256 must be in content/sources.json, the ledger and private/objects/ (12-hex prefix + code)."""
    private = root / "private"
    objects, ledger = private / "objects", private / "ledger.sqlite"
    if private.is_symlink() or objects.is_symlink() or ledger.is_symlink():
        raise ValueError("private/, private/objects and private/ledger.sqlite must not be symlinks")
    library = root / "content" / "sources.json"
    registered = ({entry["sha256"] for entry in content.load_sources(content._read_json(library))}
                  if library.is_file() else set())
    stored = _ledger_hashes(ledger) if ledger.is_file() else set()
    problems = []
    for sha in sorted({source["sha256"] for source in finding["sources"]}):
        if sha not in registered:
            problems.append(f"{sha[:PREFIX]} unregistered")
        if sha not in stored:
            problems.append(f"{sha[:PREFIX]} not_ingested")
        elif not _object_present(objects, sha):
            problems.append(f"{sha[:PREFIX]} object_missing")
    return problems


def publish(root, draft_path, approved_by, now=None):
    """Write content/findings/<slug>.json as published and log it privately; never builds or deploys."""
    draft = load_draft(draft_path)
    blockers = review_blockers(reviewed_view(draft), _load_receipts(_receipts_path(root, draft)))
    if blockers:
        raise ValueError(f"review blockers: {blockers}")
    approved_by = _text("approved-by", approved_by, 120)
    if _who(approved_by) == _who(draft.get("author")):
        raise ValueError("the author cannot approve publication of their own finding")
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    document = dict(reviewed_view(draft), state="published", published_at=when, corrections=[])
    finding = content.load_finding(document, str(draft_path))
    if not _real_dir(root / "content"):
        raise ValueError(f"{root / 'content'}: no content/ directory in this campaign")
    if not _real_dir(root / "private"):
        raise ValueError(f"{root / 'private'}: no private/ directory in this campaign")
    # One campaign-wide lock covers the uniqueness checks, the write and the log entry, so two
    # concurrent publishes cannot both pass the checks before either writes.
    with _locked(root / "private", ".publish.lock"):
        return _publish_locked(root, draft, finding, document, approved_by, when)


def _publish_locked(root, draft, finding, document, approved_by, when):
    folder = root / "content" / "findings"
    if not _real_dir(folder):
        folder.mkdir()
    target = folder / (finding["slug"] + ".json")
    if os.path.lexists(target):
        raise ValueError(f"content/findings/{target.name} already exists; publish a new slug or use a correction")
    if finding["id"] in _taken(folder, "published finding")[0]:
        raise ValueError(f"id {finding['id']!r} is already published under another slug")
    withdrawn_ids, withdrawn_slugs = _taken(root / "private" / "withdrawn", "withdrawn finding")
    if finding["id"] in withdrawn_ids or finding["slug"] in withdrawn_slugs:
        raise ValueError(f"id {finding['id']!r} or slug {finding['slug']!r} was withdrawn and stays taken")
    problems = cited_problems(root, finding)
    if problems:
        raise ValueError(f"cited records not registered and stored: {problems}")
    log = root / "private" / "publication-log.jsonl"
    try:  # open (and check) the log before the finding is written, so a bad log never leaves an unlogged publish
        log_fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise ValueError(f"{log}: cannot open publication log ({exc.strerror})") from None
    try:
        if not stat.S_ISREG(os.fstat(log_fd).st_mode):
            raise ValueError(f"{log}: publication log is not a regular file")
        os.chmod(log, 0o600)
        data = (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        fd, temporary = tempfile.mkstemp(dir=folder, prefix=".publish-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            mask = os.umask(0)
            os.umask(mask)
            os.chmod(temporary, 0o666 & ~mask)
            os.replace(temporary, target)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
        entry = {"at": when, "action": "publish", "id": finding["id"], "slug": finding["slug"],
                 "content_sha256": reviewed_hash(draft), "approved_by": approved_by}
        os.write(log_fd, (json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(log_fd)
    finally:
        os.close(log_fd)
    return {"published": finding["slug"], "path": f"content/findings/{target.name}"}
