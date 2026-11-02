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

from . import content
from .content import FINDING_KEYS
from .evidence import PREFIX, _ledger_hashes, _object_present
from .review import ROLES, _who, content_hash, review_blockers

PUBLICATION_FIELDS = ("state", "published_at", "updated_at", "corrections")
DECISIONS = ("approve", "reject", "changes_requested")
MAX_DRAFT_BYTES = 1024 * 1024
MAX_RECEIPTS_BYTES = 4 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
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
    entry = {"at": when, "action": "publish", "id": finding["id"], "slug": finding["slug"],
             "content_sha256": reviewed_hash(draft), "approved_by": approved_by}
    _write_logged(root, folder, target, document, entry)
    return {"published": finding["slug"], "path": f"content/findings/{target.name}"}


def _write_logged(root, folder, target, document, entry):
    """Atomically (re)write ``target`` and append ``entry`` to private/publication-log.jsonl (0600)."""
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
        os.write(log_fd, (json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(log_fd)
    finally:
        os.close(log_fd)


def _check_date(date, moment, published_at, corrections):
    if not isinstance(date, str) or not _DAY.fullmatch(date):
        raise ValueError(f"--date must be YYYY-MM-DD, got {date!r}")
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"--date {date!r} is not a real calendar date") from None
    if day > moment.date():
        raise ValueError(f"--date {date} is in the future (today is {moment.date()} UTC)")
    published_day = datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
    if day < published_day:
        raise ValueError(f"--date {date} is before the publication date {published_day}")
    last = corrections[-1]["date"] if corrections else None
    if last and day < datetime.strptime(last, "%Y-%m-%d").date():
        raise ValueError(f"--date {date} is before the last correction date {last}")
    return date


def correct(root, finding_id, date, note, from_draft=None, now=None):
    """Append one dated correction to the published finding with ``finding_id``; with ``from_draft``
    its reviewed fields are replaced by the draft's, which must clear the same gate as publish.
    id, slug and published_at never change; each correction is logged. Never builds or deploys."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    note = _text("note", note, 1000)
    for folder in (root / "content", root / "private"):
        if not _real_dir(folder):
            raise ValueError(f"{folder}: no {folder.name}/ directory in this campaign")
    with _locked(root / "private", ".publish.lock"):
        return _correct_locked(root, finding_id, date, note, from_draft, moment)


def _correct_locked(root, finding_id, date, note, from_draft, moment):
    folder = root / "content" / "findings"
    matches = []
    for path in sorted(folder.glob("*.json")) if _real_dir(folder) else []:
        document = _read_json(path, MAX_DRAFT_BYTES, "published finding")
        if isinstance(document, dict) and document.get("id") == finding_id:
            matches.append((path, document))
    if len(matches) != 1:
        raise ValueError(f"id {finding_id!r}: {len(matches)} published findings have it; need exactly one")
    target, existing = matches[0]
    current = content.load_finding(existing, f"content/findings/{target.name}")
    date = _check_date(date, moment, current["published_at"], current["corrections"])
    when = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    kept = {"state": existing["state"], "published_at": existing["published_at"], "updated_at": when,
            "corrections": list(existing["corrections"]) + [{"date": date, "note": note}]}
    if from_draft is None:
        document = dict(reviewed_view(existing), **kept)
    else:
        draft = load_draft(from_draft)
        if draft["id"] != finding_id or draft.get("slug") != current["slug"]:
            raise ValueError(f"{from_draft}: draft id and slug must be {finding_id!r} and {current['slug']!r}")
        blockers = review_blockers(reviewed_view(draft), _load_receipts(_receipts_path(root, draft)))
        if blockers:
            raise ValueError(f"review blockers: {blockers}")
        document = dict(reviewed_view(draft), **kept)
    finding = content.load_finding(document, f"content/findings/{target.name}")
    if from_draft is not None:
        problems = cited_problems(root, finding)
        if problems:
            raise ValueError(f"cited records not registered and stored: {problems}")
    entry = {"at": when, "action": "correct", "id": finding_id, "slug": current["slug"], "date": date,
             "content_sha256": reviewed_hash(document),
             "text_changed": reviewed_hash(document) != reviewed_hash(existing)}
    _write_logged(root, folder, target, document, entry)
    return {"corrected": current["slug"], "corrections": len(kept["corrections"])}
