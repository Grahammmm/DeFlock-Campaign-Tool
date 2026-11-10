"""Consent-backed supporting organizations (DF2207-13).

Public ``content/supporters.json`` lists organizations (name, kind, optional https URL, joined date). Each public row
needs a private record in ``kit/supporters-consent.json`` whose ``public_sha256`` binds that exact row, so an edited or
copied row is never covered by an old permission. Only organizations are listed: every row carries an explicit
``organization: true`` attestation. The privacy lint refuses email addresses, phone numbers and contact labels; it does
not recognise a person's name, so the attestation and the organizer's own review stay mandatory.

Both stores are validated whole before either is touched. The two files are replaced one after the other with a
rollback of the first on an ordinary failure; this is not a crash-atomic transaction and assumes one writer.
"""
import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .content import HOST, SLUG, SHA256, ContentError, _slug, load_meeting

KINDS = ("civil_liberties", "community", "faith", "labor", "neighborhood", "business", "other")
HOWS = ("email", "letter", "meeting", "form")
PUBLIC_REL, PRIVATE_REL = ("content", "supporters.json"), ("kit", "supporters-consent.json")
PUBLIC_LABEL, PRIVATE_LABEL = "content/supporters.json", "kit/supporters-consent.json"
ROW_KEYS = ("id", "name", "kind", "url", "joined_on", "organization")
CONSENT_KEYS = ("organization", "confirmed_on", "how", "public_sha256", "correspondence")
# DF2207-14: the optional co-sign grant group of a private record. Listing consent never implies it; all four or none.
COSIGN_KEYS = ("cosign", "cosign_confirmed_on", "cosign_how", "cosign_correspondence")
MAX_COSIGN_MEETINGS, MAX_COSIGNERS = 100, 30
MAX_SUPPORTERS, MAX_BYTES, MAX_NAME, MAX_URL = 200, 1024 * 1024, 120, 300
EARLIEST = date(2000, 1, 1)
DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
URL_CHARS = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:%/-]+")
PHONE = re.compile(r"\+?\d(?:[\s().\-]{0,3}\d){6,}")
CONTACT = re.compile(r"\b(?:e-?mail|phone|tel|telephone|mobile|cell|fax|contact|whatsapp|sms|person|individual|"
                     r"resident|signer|name)\s*[:=]|\b(?:mailto|tel|sms|callto)\s*:", re.I)
ID_TEXT = re.compile(r"[a-z0-9-]{1,64}")


class SupporterError(ContentError):
    """A supporters store or request is refused; nothing is written."""


class _Duplicate(ValueError):
    pass


# --- validation ---------------------------------------------------------------------------------

def derive_id(name):
    """Lower ASCII name, every run of other characters one hyphen, trimmed."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def name_key(name):
    return unicodedata.normalize("NFKC", name).casefold()


def row_hash(row):
    """SHA-256 of the canonical public row; the consent record binds exactly this."""
    text = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lint(text):
    """Reason for refusing ``text`` as contact data, else None. Names no person; see the module docstring."""
    plain = unicodedata.normalize("NFKC", text)
    if "@" in plain:
        return "must not contain an email address"
    if PHONE.search(plain):
        return "must not contain a phone number"
    if CONTACT.search(plain):
        return "must not contain a contact or person label"
    return None


def _name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_NAME:
        raise SupporterError(f"name: must be text of 1-{MAX_NAME} characters")
    if value != value.strip():
        raise SupporterError("name: must not start or end with spaces (it is never cleaned silently)")
    if not value.isprintable() or "<" in value or ">" in value:
        raise SupporterError("name: must be plain single-line text without controls or angle brackets")
    reason = _lint(value)
    if reason:
        raise SupporterError(f"name: {reason}")
    return value


def _url(value):
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_URL or not URL_CHARS.fullmatch(value.removeprefix("https://")):
        raise SupporterError(f"url: must be null or an https:// address of at most {MAX_URL} characters")
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment or "?" in value or "#" in value
            or parts.username or parts.password or "@" in parts.netloc or not HOST.fullmatch(parts.netloc)):
        raise SupporterError("url: must be https with a plain host, no credentials, port, query or fragment")
    decoded = unquote(parts.netloc) + unquote(parts.path)
    if not decoded.isprintable() or _lint(decoded) or "mailto" in decoded.lower():
        raise SupporterError("url: must not carry contact data")
    return value


def _date(value, label, today):
    if not isinstance(value, str) or not DATE.fullmatch(value):
        raise SupporterError(f"{label}: must be a date written YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise SupporterError(f"{label}: not a real date") from None
    if parsed < EARLIEST or parsed > today:
        raise SupporterError(f"{label}: must be between {EARLIEST.isoformat()} and today")
    return value


def _kind(value):
    if value not in KINDS:
        raise SupporterError("kind: must be one of " + ", ".join(KINDS))
    return value


def _how(value):
    if value not in HOWS:
        raise SupporterError("how: must be one of " + ", ".join(HOWS))
    return value


def _row(row, today):
    """A validated copy of one public row, or SupporterError (the message never repeats a value)."""
    if not isinstance(row, dict) or set(row) != set(ROW_KEYS):
        raise SupporterError("row must have exactly the fields " + ", ".join(ROW_KEYS))
    if row["organization"] is not True:
        raise SupporterError("organization: must be the attestation true")
    name = _name(row["name"])
    supporter_id = derive_id(name)
    if row["id"] != supporter_id or not SLUG.fullmatch(supporter_id):
        raise SupporterError("id: must be the lowercase hyphenated form of the name, starting with a letter "
                             "(at most 64 characters)")
    return {"id": supporter_id, "name": name, "kind": _kind(row["kind"]), "url": _url(row["url"]),
            "joined_on": _date(row["joined_on"], "joined_on", today), "organization": True}


def cosign_scope(value):
    """A validated co-sign grant scope: the text ``any`` or 1-100 unique meeting ids (copied, order kept)."""
    if isinstance(value, str) and value == "any":
        return "any"
    if (isinstance(value, list) and 1 <= len(value) <= MAX_COSIGN_MEETINGS
            and all(isinstance(item, str) and SLUG.fullmatch(item) and item != "any" for item in value)
            and len(set(value)) == len(value)):
        return list(value)
    raise SupporterError(f"cosign: must be the text any or a list of 1-{MAX_COSIGN_MEETINGS} unique meeting ids")


def _digest_or_none(value, key):
    if value is not None and not (isinstance(value, str) and SHA256.fullmatch(value)):
        raise SupporterError(f"{key}: must be a lowercase SHA-256 hex digest or null")
    return value


def _record(record, today):
    base = set(CONSENT_KEYS)
    if not isinstance(record, dict) or not base <= set(record) <= base | set(COSIGN_KEYS):
        raise SupporterError("record must have exactly the fields " + ", ".join(CONSENT_KEYS)
                             + " (and optionally the co-sign group " + ", ".join(COSIGN_KEYS) + ")")
    grant = set(record) & set(COSIGN_KEYS)
    if grant and grant != set(COSIGN_KEYS):
        raise SupporterError("the co-sign group must have all of " + ", ".join(COSIGN_KEYS) + " or none")
    if record["organization"] is not True:
        raise SupporterError("organization: must be the attestation true")
    for key in ("public_sha256", "correspondence"):
        value = record[key]
        if not (value is None and key == "correspondence") and not (isinstance(value, str) and SHA256.fullmatch(value)):
            raise SupporterError(f"{key}: must be a lowercase SHA-256 hex digest" + (" or null" if key == "correspondence" else ""))
    checked = {"organization": True, "confirmed_on": _date(record["confirmed_on"], "confirmed_on", today),
               "how": _how(record["how"]), "public_sha256": record["public_sha256"],
               "correspondence": record["correspondence"]}
    if grant:
        checked.update({"cosign": cosign_scope(record["cosign"]),
                        "cosign_confirmed_on": _date(record["cosign_confirmed_on"], "cosign_confirmed_on", today),
                        "cosign_how": _how(record["cosign_how"]),
                        "cosign_correspondence": _digest_or_none(record["cosign_correspondence"], "cosign_correspondence")})
    return checked


def _ident(value, index):
    return value if isinstance(value, str) and ID_TEXT.fullmatch(value) else f"supporters[{index}]"


# --- reading ------------------------------------------------------------------------------------

def _hook(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise _Duplicate("duplicate key")
        seen[key] = value
    return seen


def _refuse_constant(_name_):
    raise ValueError("not a JSON number")


def _parse(data):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_hook, parse_constant=_refuse_constant)
    except _Duplicate:
        raise SupporterError("a JSON object repeats a key") from None
    except (ValueError, RecursionError):
        raise SupporterError("not valid JSON") from None


def _walk(root, parts):
    """The target Path when it exists as one regular unlinked file; None when absent. Any link, non-regular or
    hardlinked entry on the way (root excluded) is refused, never followed."""
    current = Path(root)
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        except OSError:
            raise SupporterError(f"{'/'.join(parts[:index + 1])}: cannot be inspected") from None
        where = "/".join(parts[:index + 1])
        if stat.S_ISLNK(info.st_mode):
            raise SupporterError(f"{where}: is a symbolic link; links are never followed")
        if index < len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise SupporterError(f"{where}: must be a folder")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SupporterError(f"{where}: must be a regular file with a single link")
    return current


def _read(root, parts, private):
    """File bytes, or None when the file is absent. A private file with group/other access is refused."""
    target = _walk(root, parts)
    if target is None:
        return None
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise SupporterError("/".join(parts) + ": cannot be read") from None
    try:
        stream = os.fdopen(fd, "rb")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    with stream as handle:  # Only the stream owns the descriptor after handoff.
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SupporterError("/".join(parts) + ": must be a regular file with a single link")
        if private and info.st_mode & 0o077:
            raise SupporterError("/".join(parts) + ": must be private (mode 0600); group/other access is refused")
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise SupporterError("/".join(parts) + ": file too large")
    return data


def _document(data, label):
    document = _parse(data)
    if not isinstance(document, dict) or set(document) != {"schema_version", "supporters"}:
        raise SupporterError("must be an object with schema_version and supporters")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise SupporterError("schema_version: must be 1")
    return document["supporters"]


def _public(data, today, problems):
    """Valid public rows of ``data`` (sorted by id); every refusal is appended to ``problems`` by supporter id."""
    try:
        listed = _document(data, PUBLIC_LABEL)
        if not isinstance(listed, list):
            raise SupporterError("supporters: must be a list")
        if len(listed) > MAX_SUPPORTERS:
            raise SupporterError(f"supporters: at most {MAX_SUPPORTERS} organizations")
    except SupporterError as exc:
        problems.append(f"{PUBLIC_LABEL}: {exc}")
        return []
    rows, ids, names = [], set(), set()
    for index, raw in enumerate(listed):
        ident = _ident(raw.get("id") if isinstance(raw, dict) else None, index)
        try:
            row = _row(raw, today)
            if row["id"] in ids or name_key(row["name"]) in names:
                raise SupporterError("duplicate id or name")
        except SupporterError as exc:
            problems.append(f"{PUBLIC_LABEL}: {ident}: {exc}")
            continue
        ids.add(row["id"])
        names.add(name_key(row["name"]))
        rows.append(row)
    return sorted(rows, key=lambda row: row["id"])


def _consent(data, today, problems):
    """``{id: record}`` of valid consent records; refusals appended. None when the store is unusable as a whole."""
    try:
        listed = _document(data, PRIVATE_LABEL)
        if not isinstance(listed, dict):
            raise SupporterError("supporters: must be an object keyed by supporter id")
        if len(listed) > MAX_SUPPORTERS:
            raise SupporterError(f"supporters: at most {MAX_SUPPORTERS} records")
    except SupporterError as exc:
        problems.append(f"{PRIVATE_LABEL}: {exc}")
        return None
    records = {}
    for key in sorted(listed):
        ident = _ident(key, 0)
        try:
            if not SLUG.fullmatch(key):
                raise SupporterError("key: not a supporter id")
            records[key] = _record(listed[key], today)
        except SupporterError as exc:
            problems.append(f"{PRIVATE_LABEL}: {ident}: {exc}")
    return records


def _inspect(root, today, *, always_private=False):
    """``(rows, records, problems)``; rows None without a public file. The private store is read when the public
    file lists a row, or always with ``always_private``; records None when it is absent or unusable."""
    today = today or date.today()
    problems, rows, records = [], None, None
    try:
        data = _read(root, PUBLIC_REL, private=False)
    except SupporterError as exc:
        return [], None, [f"{PUBLIC_LABEL}: {exc}"]
    if data is not None:
        rows = _public(data, today, problems)
    if rows or always_private:
        try:
            private = _read(root, PRIVATE_REL, private=True)
        except SupporterError as exc:
            problems.append(f"{PRIVATE_LABEL}: {exc}")
            private, unusable = None, True
        else:
            unusable = False
        if private is not None:
            records = _consent(private, today, problems)
            unusable = records is None
        records = records if records is not None else {}
        for row in rows or []:
            record = records.get(row["id"])
            if unusable:
                problems.append(f"{PUBLIC_LABEL}: {row['id']}: consent store is not usable")
            elif record is None:
                problems.append(f"{PUBLIC_LABEL}: {row['id']}: no consent record")
            elif (record["public_sha256"] != row_hash(row) or record["confirmed_on"] != row["joined_on"]):
                problems.append(f"{PUBLIC_LABEL}: {row['id']}: consent record does not match the public row")
    return rows, records, problems


def load_supporters(root, *, today=None):
    """The validated public rows (sorted by id) when content/supporters.json exists, else None. Every row must have
    a matching consent record; otherwise ContentError. Only public fields are returned."""
    rows, _, problems = _inspect(Path(root), today)
    if problems:
        raise SupporterError(sorted(problems)[0])
    return rows


def supporter_problems(root, *, today=None):
    """Sorted problem lines naming each invalid or unconsented supporter id; no values. Read-only."""
    return sorted(set(_inspect(Path(root), today)[2]))


def read_consent(root, *, today=None):
    """``{id: record}`` from the private store; ``{}`` when it is missing. A malformed or insecure store is refused."""
    problems = []
    data = _read(Path(root), PRIVATE_REL, private=True)
    records = _consent(data, today or date.today(), problems) if data is not None else {}
    if problems:
        raise SupporterError(sorted(problems)[0])
    return records


def consent_for_supporter(root, supporter_id, *, today=None):
    """``{"public": ROW, "consent": RECORD}`` for trusted private callers; never part of the site model."""
    rows, records, problems = _inspect(Path(root), today, always_private=True)
    if problems:
        raise SupporterError(sorted(problems)[0])
    for row in rows or []:
        if row["id"] == supporter_id:
            return {"public": dict(row), "consent": dict(records[supporter_id])}
    raise SupporterError("unknown supporter id")


# --- writing ------------------------------------------------------------------------------------

def _dump(document):
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _state(root, today):
    """The current rows and records, refused unless both stores are valid, matching and free of orphans."""
    rows, records, problems = _inspect(root, today, always_private=True)
    if problems:
        shown = sorted(problems)
        raise SupporterError("fix or restore the saved pair of stores first: " + "; ".join(shown[:5])
                             + (f"; and {len(shown) - 5} more" if len(shown) > 5 else ""))
    rows = rows or []
    orphans = sorted(set(records) - {row["id"] for row in rows})
    if orphans:
        raise SupporterError("orphan consent record(s) with no public row (an interrupted change?): "
                             + ", ".join(orphans) + "; restore the saved pair of files before changing supporters")
    return rows, records


def _prepare(root, rows, records, today):
    """The two prospective stores as bytes, checked by the same readers the build uses."""
    ordered = sorted(rows, key=lambda row: row["id"])
    public = _dump({"schema_version": 1, "supporters": ordered})
    private = _dump({"schema_version": 1, "supporters": {row["id"]: records[row["id"]] for row in ordered}})
    if len(public) > MAX_BYTES or len(private) > MAX_BYTES or len(ordered) > MAX_SUPPORTERS:
        raise SupporterError(f"at most {MAX_SUPPORTERS} organizations and {MAX_BYTES // 1024} KiB per store")
    problems = []
    checked = _public(public, today, problems)
    kept = _consent(private, today, problems)
    if problems or checked != ordered or kept is None or any(
            kept[row["id"]]["public_sha256"] != row_hash(row) for row in ordered):
        raise SupporterError("the prospective stores do not validate; nothing was written")
    return public, private


def _snapshot(root, parts):
    target = _walk(root, parts)
    if target is None:
        return None
    info = os.lstat(target)
    return target.read_bytes(), stat.S_IMODE(info.st_mode)


def _ensure_private_dir(root):
    """Create kit/ 0700 or secure an existing one; returns True when created. Refuses if it cannot be secured."""
    kit = Path(root) / PRIVATE_REL[0]
    created = False
    try:
        if not os.path.lexists(kit):
            os.mkdir(kit, 0o700)
            created = True
        info = os.lstat(kit)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("not a real folder")
        if info.st_mode & 0o077:
            os.chmod(kit, 0o700)
        if os.lstat(kit).st_mode & 0o077:
            raise OSError("mode not secured")
    except OSError:
        if created:
            try:
                os.rmdir(kit)
            except OSError:
                pass
        raise SupporterError("kit: the private folder cannot be secured (mode 0700); nothing was written") from None
    return created


def _temporary(folder, data, mode):
    fd, name = tempfile.mkstemp(dir=folder, prefix=".supporters-", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        _discard(name)
        raise
    return name


def _discard(name):
    try:
        os.unlink(name)
    except OSError:
        pass


def _put(root, parts, previous):
    """Put the previous bytes and mode back, or remove the file when it did not exist."""
    target = Path(root).joinpath(*parts)
    if previous is None:
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
        return
    name = _temporary(target.parent, previous[0], previous[1])
    try:
        os.replace(name, target)
    finally:
        _discard(name)


def _install_pair(root, first, second, today):
    """Replace two stores one after the other. ``first``/``second`` are ``(parts, bytes, mode)``. Both temporary
    files are fully written before either target changes; a failure after the first replace restores the first."""
    root = Path(root)
    if not (root / PUBLIC_REL[0]).is_dir() or (root / PUBLIC_REL[0]).is_symlink():
        raise SupporterError("content/ does not exist: nothing was written")
    created = _ensure_private_dir(root)
    temps, done = [], []
    previous = {}
    try:
        for parts, _, _ in (first, second):
            previous[parts] = _snapshot(root, parts)
        for parts, data, mode in (first, second):
            temps.append(_temporary(root.joinpath(*parts).parent, data, mode))
        for (parts, _, _), name in zip((first, second), temps):
            os.replace(name, root.joinpath(*parts))
            done.append(parts)
    except BaseException as exc:
        failed = []
        for parts in reversed(done):
            try:
                _put(root, parts, previous[parts])
            except BaseException:  # keep going: report every file that could not be put back
                failed.append("/".join(parts))
        for name in temps:
            _discard(name)
        if created and not failed:
            try:
                os.rmdir(root / PRIVATE_REL[0])
            except OSError:
                pass
        if failed:
            raise SupporterError("write failed and could not be undone for " + ", ".join(failed)
                                 + "; restore the saved pair of files") from exc
        if isinstance(exc, SupporterError):
            raise
        if isinstance(exc, OSError):
            raise SupporterError("write failed; the previous files were restored") from exc
        raise
    for name in temps:
        _discard(name)  # already replaced: nothing left, but never leave a temporary behind


def _correspondence(root, prefix):
    if prefix is None:
        return None
    from .sources_cmd import resolve_hash  # local: sources_cmd imports content
    try:
        return resolve_hash(root, prefix)
    except ValueError as exc:
        raise SupporterError(f"correspondence: {exc}") from None


def add_supporter(root, *, name, kind, organization, confirmed_on, how, url=None, correspondence=None, today=None):
    """Record the consent privately and list the organization publicly; returns ``{"id", "action"}`` only."""
    root, today = Path(root), today or date.today()
    if organization is not True:
        raise SupporterError("organization: attest that this is an organization with --organization; "
                             "individuals are never listed")
    name = _name(name)
    row = _row({"id": derive_id(name), "name": name, "kind": kind, "url": url, "joined_on": confirmed_on,
                "organization": True}, today)
    record = _record({"organization": True, "confirmed_on": confirmed_on, "how": how,
                      "public_sha256": row_hash(row), "correspondence": None}, today)
    record["correspondence"] = _correspondence(root, correspondence)
    rows, records = _state(root, today)
    if any(old["id"] == row["id"] or name_key(old["name"]) == name_key(name) for old in rows):
        raise SupporterError("an organization with this name or id is already listed")
    if len(rows) >= MAX_SUPPORTERS:
        raise SupporterError(f"at most {MAX_SUPPORTERS} organizations")
    public, private = _prepare(root, rows + [row], {**records, row["id"]: record}, today)
    _install_pair(root, (PRIVATE_REL, private, 0o600), (PUBLIC_REL, public, 0o644), today)
    return {"id": row["id"], "action": "added"}


def remove_supporter(root, supporter_id, *, today=None):
    """Drop the public row and its private record; the order is public first, then private."""
    root, today = Path(root), today or date.today()
    rows, records = _state(root, today)
    if not isinstance(supporter_id, str) or supporter_id not in {row["id"] for row in rows}:
        raise SupporterError("unknown supporter id; nothing was changed")
    kept = [row for row in rows if row["id"] != supporter_id]
    public, private = _prepare(root, kept, {key: value for key, value in records.items() if key != supporter_id}, today)
    _install_pair(root, (PUBLIC_REL, public, 0o644), (PRIVATE_REL, private, 0o600), today)
    return {"id": supporter_id, "action": "removed"}


# --- co-sign grants and checks (DF2207-14) ------------------------------------------------------

def _install_private(root, data):
    """Replace only the private store (0600, atomic rename); the public file is never opened for writing."""
    root = Path(root)
    _ensure_private_dir(root)
    name = _temporary(root / PRIVATE_REL[0], data, 0o600)
    try:
        os.replace(name, root.joinpath(*PRIVATE_REL))
    except OSError:
        raise SupporterError("write failed; the previous private file was kept") from None
    finally:
        _discard(name)


def set_cosign_consent(root, supporter_id, *, cosign=None, clear=False, confirmed_on=None, how=None,
                       correspondence=None, today=None):
    """Grant or revoke one listed organization's permission to be named under a staged written comment.

    ``cosign`` is ``["any"]`` (alone) or 1-100 meeting ids; ``clear`` removes the whole optional grant group and needs
    no confirmation. The prospective private store is validated whole before the atomic private-only replacement.
    Returns ``{"id", "action"}`` only; nothing is restaged or published."""
    root, today = Path(root), today or date.today()
    if clear:
        if cosign is not None or confirmed_on is not None or how is not None or correspondence is not None:
            raise SupporterError("clearing a co-sign grant takes no scope or confirmation fields")
    elif cosign is None:
        raise SupporterError("give --cosign any|MEETING_ID ... or --clear-cosign")
    rows, records = _state(root, today)
    if not isinstance(supporter_id, str) or supporter_id not in {row["id"] for row in rows}:
        raise SupporterError("unknown supporter id; nothing was changed")
    record = {key: value for key, value in records[supporter_id].items() if key not in COSIGN_KEYS}
    if not clear:
        scope = list(cosign) if isinstance(cosign, (list, tuple)) else cosign
        if isinstance(scope, list) and "any" in scope:
            if scope != ["any"]:
                raise SupporterError("cosign: the word any must stand alone")
            scope = "any"
        if confirmed_on is None or how is None:
            raise SupporterError("a co-sign grant needs its own confirmation date and how it was confirmed")
        record.update({"cosign": cosign_scope(scope), "cosign_confirmed_on": _date(confirmed_on, "cosign_confirmed_on", today),
                       "cosign_how": _how(how), "cosign_correspondence": _correspondence(root, correspondence)})
    updated = {**records, supporter_id: _record(record, today)}
    _, private = _prepare(root, rows, updated, today)
    _install_private(root, private)
    return {"id": supporter_id, "action": "cosign cleared" if clear else "cosign granted"}


def _listing(root, today):
    """``({id: row}, {id: record})`` of the valid current rows and consent records (read-only; never raises)."""
    rows, records, _ = _inspect(Path(root), today, always_private=True)
    return {row["id"]: row for row in rows or []}, records or {}


def _grant_reason(listed, records, supporter_id, meeting_id):
    """Why ``supporter_id`` may not be named on ``meeting_id``, or None. Never repeats a stored value."""
    row = listed.get(supporter_id)
    if row is None:
        return "is not listed in content/supporters.json"
    record = records.get(supporter_id)
    if record is None or record["public_sha256"] != row_hash(row) or record["confirmed_on"] != row["joined_on"]:
        return "has no consent record matching the listed row"
    scope = record.get("cosign")
    if scope is None:
        return "has not consented to co-sign"
    if scope != "any" and meeting_id not in scope:
        return "has not consented to co-sign this meeting"
    return None


def resolve_cosigners(root, meeting_id, supporter_ids, *, today=None):
    """The ordered public rows for ``supporter_ids`` once each one's listing, attestation, binding and scope for
    exactly ``meeting_id`` validate. Only public fields leave this helper."""
    listed, records = _listing(root, today)
    for supporter_id in supporter_ids:
        reason = _grant_reason(listed, records, supporter_id, meeting_id)
        if reason:
            raise SupporterError(f"--cosign {supporter_id}: {reason}")
    return [dict(listed[supporter_id]) for supporter_id in supporter_ids]


def cosign_problems(root, meetings, *, today=None, skip_meeting_id=None):
    """Sorted problem lines for every staged meeting that names co-signers (loaded meeting dicts). Read-only: each
    id must still be listed, bound to its consent record and covered for that meeting, the meeting must still carry a
    verified written comment, and the kit must hold exactly one generated block with those names in that order."""
    from .meetings import COSIGN_HEADING, cosign_block_md  # local: meetings imports content
    problems, state = [], None
    for meeting in meetings:
        if meeting.get("id") == skip_meeting_id and skip_meeting_id is not None:
            continue
        label, kit = f"meetings/{meeting.get('id')}.json", meeting.get("comment_kit_md") or ""
        ids = meeting.get("cosigners")
        headings = sum(1 for line in kit.splitlines() if line == COSIGN_HEADING)
        if ids is None:
            if headings:
                problems.append(f"{label}: the comment kit has a co-signed block but the meeting lists no cosigners")
            continue
        if not (meeting.get("participation") or {}).get("written_comment"):
            problems.append(f"{label}.cosigners: the meeting has no verified written comment")
        state = state or _listing(root, today)
        names = []
        for supporter_id in ids:
            reason = _grant_reason(*state, supporter_id, meeting.get("id"))
            if reason:
                problems.append(f"{label}.cosigners: {supporter_id}: {reason}")
            else:
                names.append(state[0][supporter_id]["name"])
        if len(names) == len(ids):
            section = kit.split("## Written comment\n", 1)[1].split("\n\n## ", 1)[0] if "## Written comment\n" in kit else ""
            if headings != 1 or not section.endswith("\n\n" + cosign_block_md(names)):
                problems.append(f"{label}.cosigners: the written comment does not hold exactly one current "
                                "co-signed block for these organizations; stage it again with --replace")
    return sorted(set(problems))


# --- private inventory (DF1109B-06) -------------------------------------------------------------

ORPHAN = "private record without public entry"
MAX_MEETING_BYTES = 4 * 1024 * 1024


def _real_root(root):
    """The campaign folder; refused when absent, not a folder, or reached through a symbolic link."""
    here = Path(os.path.abspath(os.fspath(root)))
    for part in (here, *here.parents):
        if os.path.islink(part):
            raise SupporterError("the campaign folder must not be reached through a symbolic link")
    info = os.lstat(here)  # FileNotFoundError when absent: the caller refuses
    if not stat.S_ISDIR(info.st_mode):
        raise SupporterError("the campaign folder must be a folder")
    return here


def _inventory(root, today):
    """``({id: row}, {id: record})`` with both stores valid as a whole and every present pair bound; a missing record
    and an orphan record are kept as states. Any other problem refuses."""
    problems, rows, records = [], [], {}
    data = _read(root, PUBLIC_REL, private=False)
    if data is not None:
        rows = _public(data, today, problems)
    data = _read(root, PRIVATE_REL, private=True)
    if data is not None:
        records = _consent(data, today, problems)
    if problems or records is None:
        raise SupporterError("a supporter store is not valid")
    for row in rows:
        record = records.get(row["id"])
        if record is not None and (record["public_sha256"] != row_hash(row) or record["confirmed_on"] != row["joined_on"]):
            raise SupporterError("a consent record does not match its public row")
    return {row["id"]: row for row in rows}, records


def _staged(root, today):
    """``{supporter_id: {meeting_id}}`` from the structured ``cosigners`` of every valid content/meetings/*.json (no
    recursion; any unsafe, unreadable or invalid file refuses the whole index)."""
    folder = Path(root)
    for part in ("content", "meetings"):
        folder = folder / part
        try:
            info = os.lstat(folder)
        except FileNotFoundError:
            return {}
        if not stat.S_ISDIR(info.st_mode):
            raise SupporterError("content/meetings: must be a real folder")
    index, seen = {}, set()
    for name in sorted(entry for entry in os.listdir(folder) if entry.endswith(".json")):
        path = folder / name
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SupporterError("meetings: must be a regular file with a single link")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            stream = os.fdopen(fd, "rb")
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        with stream as handle:  # Only the stream owns the descriptor after handoff.
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SupporterError("meetings: must be a regular file with a single link")
            data = handle.read(MAX_MEETING_BYTES + 1)
        if len(data) > MAX_MEETING_BYTES:
            raise SupporterError("meetings: file too large")
        label = "meetings/" + name
        meeting = load_meeting(_parse(data), label, today=today)
        meeting_id = meeting["id"] or _slug(name[:-len(".json")], label + " filename")
        if meeting_id in seen:
            raise SupporterError("meetings: duplicate meeting id")
        seen.add(meeting_id)
        for supporter_id in meeting.get("cosigners") or ():
            index.setdefault(supporter_id, set()).add(meeting_id)
    return index


def list_supporters(root, *, today=None):
    """Private inventory rows (sorted by id) for every public row and every private record: public facts, whether a
    consent record exists, the confirmation dates, the recorded co-sign scope and the staged meetings naming the id.
    Read-only; never returns hashes, ``how`` values or correspondence. Raises on any broken or unsafe input."""
    today = today or date.today()
    root = _real_root(root)
    rows, records = _inventory(root, today)
    staged = _staged(root, today)
    listed = []
    for supporter_id in sorted(set(rows) | set(records)):
        row, record = rows.get(supporter_id), records.get(supporter_id) or {}
        scope = record.get("cosign")
        listed.append({
            "id": supporter_id, "status": "listed" if row else ORPHAN,
            "kind": row["kind"] if row else None, "name": row["name"] if row else None,
            "joined_on": row["joined_on"] if row else None,
            "consent_present": bool(record), "confirmed_on": record.get("confirmed_on"),
            "cosign": sorted(scope) if isinstance(scope, list) else scope,
            "cosign_confirmed_on": record.get("cosign_confirmed_on"),
            "staged_meetings": sorted(staged.get(supporter_id, ())),
        })
    return listed


def format_supporters_list(rows):
    """One labelled line per inventory row; the name is JSON-quoted so it cannot pass for a column."""
    if not rows:
        return "none recorded\n"
    lines = []
    for row in rows:
        scope = row["cosign"]
        tail = [f"co-sign {'none' if scope is None else scope if scope == 'any' else ','.join(scope)}",
                f"co-sign confirmed {row['cosign_confirmed_on'] or '-'}",
                f"staged {','.join(row['staged_meetings']) or 'none'}"]
        if row["status"] == ORPHAN:
            head = [row["id"], ORPHAN, f"confirmed {row['confirmed_on']}"]
        else:
            head = [row["id"], row["kind"], json.dumps(row["name"], ensure_ascii=False), f"joined {row['joined_on']}",
                    f"consent {'present' if row['consent_present'] else 'missing'}",
                    f"confirmed {row['confirmed_on'] or '-'}"]
        lines.append(" | ".join(head + tail))
    return "\n".join(lines) + "\n"
