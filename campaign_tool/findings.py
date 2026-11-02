"""Offline review receipts for finding drafts (self-asserted reviewer names unless SSH-signed).

A receipt is bound to the hash of the draft without its publication fields, so moving a draft
through states keeps it bound while any edit to the reviewed fields makes it stale. Signed receipts
verify against the organizer-held private/reviewers.allowed_signers (never approval.py or /etc/deflock)."""
import contextlib
import errno
import json
import os
import re
import shutil
import stat
import subprocess
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
SIGN_NAMESPACE, MAX_KEY_BYTES, MAX_SIGNATURE_BYTES = "deflock-finding-review", 1024, 8192
_KEY, _PRINCIPAL = re.compile(r"(ssh-ed25519|ecdsa-sha2-nistp256) [A-Za-z0-9+/]+={0,3}"), re.compile(r"[A-Za-z0-9._-]+")


def reviewed_view(draft):
    return {k: v for k, v in draft.items() if k not in PUBLICATION_FIELDS}


def reviewed_hash(draft):
    return content_hash(reviewed_view(draft))


def _no_constant(name):
    raise ValueError(name)


def _read_json(path, limit, what, raw=False):
    """Parse one regular, non-symlink file (``raw``: return its UTF-8 text); errors name the path, never its content."""
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
    if raw:
        return text
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


def receipt_payload(id_, content_sha256, role, decision, reviewer, rationale):
    """The exact bytes a reviewer signs: canonical JSON, no timestamp (signable before reviewed_at exists)."""
    return json.dumps({"id": id_, "content_sha256": content_sha256, "role": role, "decision": decision,
                       "reviewer": reviewer, "rationale": rationale, "namespace": SIGN_NAMESPACE},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _checked(root, draft, role, decision, reviewer, rationale):
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
    return reviewer, rationale, entry


def review_payload(root, draft, role, decision, reviewer, rationale):
    reviewer, rationale, _ = _checked(root, draft, role, decision, reviewer, rationale)  # read-only
    return receipt_payload(draft["id"], reviewed_hash(draft), role, decision, reviewer, rationale)


def add_receipt(root, draft, role, decision, reviewer, rationale, now=None, signature=None):
    reviewer, rationale, entry = _checked(root, draft, role, decision, reviewer, rationale)
    digest, signed = reviewed_hash(draft), {}
    if signature is not None:  # verified before any directory or file is written
        if not entry or "ssh_public_key" not in entry:
            raise ValueError(f"{reviewer} has no ssh_public_key on the roster (finding reviewers add --ssh-key)")
        signed = {"signature": _read_json(signature, MAX_SIGNATURE_BYTES, "signature", raw=True),
                  "signed_by": principal(entry["name"])}
        if not _verifies(root, signed["signed_by"], signed["signature"],
                         receipt_payload(draft["id"], digest, role, decision, reviewer, rationale)):
            raise ValueError(f"{signature}: bad signature for {signed['signed_by']} over this receipt; nothing written")
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
                   "rationale": rationale, "reviewed_at": when, **signed}
        _write_private(path, receipts + [receipt], ".review-")
    return dict(receipt, id=draft["id"], written=str(path))


def principal(name):
    """SSH principal for a roster name: whitespace runs become '_'; only [A-Za-z0-9._-] allowed."""
    value = "_".join(name.split())
    if not _PRINCIPAL.fullmatch(value):
        raise ValueError(f"reviewer name {name!r} cannot be an SSH principal (use letters, digits, '.', '_', '-')")
    return value


def _verifies(root, who, signature, payload):
    """ssh-keygen -Y verify against the campaign allowed-signers file (never the /etc/deflock approval file)."""
    keygen, path = shutil.which("ssh-keygen"), _signers_path(root)
    if keygen is None:
        raise ValueError("ssh-keygen not found; signed receipts need OpenSSH 8.2+")
    if not isinstance(signature, str) or "-----BEGIN SSH SIGNATURE-----" not in signature:
        return False
    with tempfile.TemporaryDirectory(prefix="deflock-review-") as tmp:
        sig = os.path.join(tmp, "receipt.sig")
        with open(sig, "w", encoding="utf-8") as out:
            out.write(signature)
        try:
            return subprocess.run([keygen, "-Y", "verify", "-f", str(path), "-I", who, "-n", SIGN_NAMESPACE, "-s", sig],
                                  input=payload, capture_output=True, timeout=30, check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


def receipt_trust(root, draft, receipts):
    """Current-hash receipts: signed only when the signature re-verifies now; all others count as self-asserted."""
    digest, roster, _ = reviewed_hash(draft), _load_roster(root), _signers_path(root)  # a symlink refuses
    trust = {"signed": 0, "self_asserted": 0, "bad_signatures": []}
    for r in (r for r in receipts if isinstance(r, dict) and r.get("content_sha256") == digest):
        good, entry = False, roster and _rostered(roster, r.get("reviewer"))
        with contextlib.suppress(ValueError):  # no usable principal or no ssh-keygen: cannot verify
            good = ("signed_by" in r and bool(entry) and r["signed_by"] == principal(entry["name"])
                    and _verifies(root, r["signed_by"], r.get("signature"), receipt_payload(
                        draft["id"], digest, r.get("role"), r.get("decision"), r.get("reviewer"), r.get("rationale"))))
        if not good and ("signature" in r or "signed_by" in r):
            trust["bad_signatures"].append({k: r.get(k) for k in ("reviewer", "role", "decision", "reviewed_at")})
        trust["signed" if good else "self_asserted"] += 1
    return trust


def trust_line(trust):
    bad = len(trust["bad_signatures"])
    return (f"Review receipts: {trust['signed']} signed, {trust['self_asserted']} self-asserted"
            + (f", {bad} with a signature that does not verify." if bad else "."))


def _signers_path(root):
    path = root / "private" / "reviewers.allowed_signers"
    if os.path.islink(path) or (os.path.lexists(path) and not os.path.isfile(path)):
        raise ValueError(f"{path}: allowed-signers file must be a regular file, not a symlink")
    return path


def _write_private(path, document, prefix, text=None):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=prefix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(json.dumps(document, indent=2, ensure_ascii=False) + "\n" if text is None else text)
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
            "stale_receipts": len(receipts) - current, **receipt_trust(root, draft, receipts),
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


def _object(where, document, expected, optional=frozenset()):
    if not isinstance(document, dict):
        raise ValueError(f"{where}: must be a JSON object")
    for key in sorted((set(document) - optional) ^ expected):
        raise ValueError(f"{where}: {'unknown' if key in document else 'missing'} key {key!r}")


def _public_key(value, where):
    if (not isinstance(value, str) or len(value.encode("utf-8")) > MAX_KEY_BYTES
            or any(unicodedata.category(ch) == "Cc" for ch in value.rstrip("\n"))
            or not _KEY.fullmatch(" ".join(value.split()[:2]))):
        raise ValueError(f"{where} must be one ssh-ed25519 or ecdsa-sha2-nistp256 key line of <= {MAX_KEY_BYTES} bytes")
    return " ".join(value.split()[:2])  # the comment is dropped


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
    seen, principals = set(), set()
    for index, entry in enumerate(roster["reviewers"]):
        where = f"{path}: reviewers[{index}]"
        _object(where, entry, REVIEWER_KEYS, frozenset(("ssh_public_key",)))
        who, roles, added = _who(entry["name"]), entry["roles"], entry["added_on"]
        if who is None or who in seen:
            raise ValueError(f"{where}.name is empty or duplicates an earlier reviewer")
        seen.add(who)
        if "ssh_public_key" in entry:
            if _public_key(entry["ssh_public_key"], f"{where}.ssh_public_key") != entry["ssh_public_key"] \
                    or principal(entry["name"]) in principals:
                raise ValueError(f"{where}.ssh_public_key must be a bare key line under a unique principal")
            principals.add(principal(entry["name"]))
        if not isinstance(roles, list) or not roles or not all(isinstance(r, str) and r in ROLES for r in roles):
            raise ValueError(f"{where}.roles must be a non-empty list of {sorted(ROLES)}")
        try:
            date.fromisoformat(added if isinstance(added, str) and _DATE.fullmatch(added) else "")
        except ValueError:
            raise ValueError(f"{where}.added_on must be a YYYY-MM-DD date") from None
    return roster


def add_reviewer(root, name, roles, today=None, ssh_key=None):
    """Add a reviewer or merge roles into an existing one (roles are never removed; added_on is kept)."""
    name = _text("name", name, 120)
    if not roles or any(role not in ROLES for role in roles):
        raise ValueError(f"--role must be one of {sorted(ROLES)}")
    key = None if ssh_key is None else _public_key(_read_json(ssh_key, MAX_KEY_BYTES, "SSH key", raw=True), ssh_key)
    path, signers = _roster_path(root), _signers_path(root)
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
        if key is not None:
            who = principal(entry["name"])  # before any write
            if any(e is not entry and "ssh_public_key" in e and principal(e["name"]) == who for e in roster["reviewers"]):
                raise ValueError(f"principal {who} already has a key under another roster name")
            entry["ssh_public_key"] = key
        _write_private(path, roster, ".reviewers-")
        _write_private(signers, None, ".signers-", "".join(
            f'{principal(e["name"])} namespaces="{SIGN_NAMESPACE}" {e["ssh_public_key"]}\n'
            for e in roster["reviewers"] if "ssh_public_key" in e))
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
    receipts = _load_receipts(_receipts_path(root, draft))
    blockers = review_blockers(reviewed_view(draft), receipts)
    if blockers:
        raise ValueError(f"review blockers: {blockers}")
    trust = receipt_trust(root, draft, receipts)
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
        return dict(_publish_locked(root, draft, finding, document, approved_by, when), receipt_trust=trust)


def _publish_locked(root, draft, finding, document, approved_by, when):
    folder = root / "content" / "findings"
    if not _real_dir(folder):
        folder.mkdir()
    target = folder / (finding["slug"] + ".json")
    if os.path.lexists(target):
        raise ValueError(f"content/findings/{target.name} already exists; publish a new slug or use a correction")
    if finding["id"] in _taken(folder, "published finding")[0]:
        raise ValueError(f"id {finding['id']!r} is already published under another slug")
    withdrawn_ids, withdrawn_slugs = withdrawn_names(root)
    if finding["id"] in withdrawn_ids or finding["slug"] in withdrawn_slugs:
        raise ValueError(f"id {finding['id']!r} or slug {finding['slug']!r} was withdrawn and stays taken")
    problems = cited_problems(root, finding)
    if problems:
        raise ValueError(f"cited records not registered and stored: {problems}")
    entry = {"at": when, "action": "publish", "id": finding["id"], "slug": finding["slug"],
             "content_sha256": reviewed_hash(draft), "approved_by": approved_by}
    _write_logged(root, folder, target, document, entry)
    return {"published": finding["slug"], "path": f"content/findings/{target.name}"}


def withdrawn_names(root):
    """(ids, slugs) kept taken by private/withdrawn/*.json; an unreadable or non-JSON file is named, never skipped."""
    return _taken(root / "private" / "withdrawn", "withdrawn finding")


def _open_log(root):
    """Open (and check) private/publication-log.jsonl for appending before anything is written."""
    log = root / "private" / "publication-log.jsonl"
    try:
        log_fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise ValueError(f"{log}: cannot open publication log ({exc.strerror})") from None
    if not stat.S_ISREG(os.fstat(log_fd).st_mode):
        os.close(log_fd)
        raise ValueError(f"{log}: publication log is not a regular file")
    os.chmod(log, 0o600)
    return log_fd


def _append(log_fd, entry):
    os.write(log_fd, (json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
    os.fsync(log_fd)


def _write_logged(root, folder, target, document, entry):
    """Atomically (re)write ``target`` and append ``entry`` to private/publication-log.jsonl (0600)."""
    log_fd = _open_log(root)  # a bad log refuses before the finding is written, never leaving an unlogged publish
    try:
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
        _append(log_fd, entry)
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


def _one_published(folder, finding_id):
    """(path, document) of the single content/findings/*.json whose id is ``finding_id``; symlinks refuse."""
    matches = []
    for path in sorted(folder.glob("*.json")) if _real_dir(folder) else []:
        document = _read_json(path, MAX_DRAFT_BYTES, "published finding")
        if isinstance(document, dict) and document.get("id") == finding_id:
            matches.append((path, document))
    if len(matches) != 1:
        raise ValueError(f"id {finding_id!r}: {len(matches)} published findings have it; need exactly one")
    return matches[0]


def _correct_locked(root, finding_id, date, note, from_draft, moment):
    folder = root / "content" / "findings"
    target, existing = _one_published(folder, finding_id)
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


def withdraw(root, finding_id, reason, now=None):
    """Move the published finding with ``finding_id`` to private/withdrawn/<slug>.json (dir 0700, file 0600) with
    ``withdrawn: {at, reason}``; its id and slug stay taken. The log line omits the reason. No notice page, no build."""
    reason = _text("reason", reason, 1000)
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for folder in (root / "content", root / "private"):
        if not _real_dir(folder):
            raise ValueError(f"{folder}: no {folder.name}/ directory in this campaign")
    with _locked(root / "private", ".publish.lock"):
        return _withdraw_locked(root, finding_id, reason, when)


def _withdraw_locked(root, finding_id, reason, when):
    source, document = _one_published(root / "content" / "findings", finding_id)
    folder = root / "private" / "withdrawn"
    if not _real_dir(folder):
        folder.mkdir(mode=0o700)
    os.chmod(folder, 0o700)
    target = folder / source.name
    if os.path.lexists(target):
        raise ValueError(f"private/withdrawn/{target.name} already exists; nothing was moved")
    log_fd = _open_log(root)  # a bad log refuses before anything is written
    try:
        _write_private(target, dict(document, withdrawn={"at": when, "reason": reason}), ".withdraw-")
        if hasattr(os, "O_DIRECTORY"):  # POSIX: make the new entry durable before the content file goes
            dir_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        try:
            os.unlink(source)
        except OSError as exc:  # never zero copies: keep both, delete nothing, name the leftover
            raise ValueError(f"private/withdrawn/{target.name} was written but the leftover content/findings/"
                             f"{source.name} could not be removed ({exc.strerror}); both copies remain and nothing "
                             f"was deleted or logged. Remove content/findings/{source.name} by hand, then run build") from None
        _append(log_fd, {"at": when, "action": "withdraw", "id": finding_id, "slug": source.stem})
    finally:
        os.close(log_fd)
    return {"withdrawn": source.stem, "path": f"private/withdrawn/{target.name}"}
