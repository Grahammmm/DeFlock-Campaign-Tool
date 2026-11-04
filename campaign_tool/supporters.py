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

from .content import HOST, SLUG, SHA256, ContentError

KINDS = ("civil_liberties", "community", "faith", "labor", "neighborhood", "business", "other")
HOWS = ("email", "letter", "meeting", "form")
PUBLIC_REL, PRIVATE_REL = ("content", "supporters.json"), ("kit", "supporters-consent.json")
PUBLIC_LABEL, PRIVATE_LABEL = "content/supporters.json", "kit/supporters-consent.json"
ROW_KEYS = ("id", "name", "kind", "url", "joined_on", "organization")
CONSENT_KEYS = ("organization", "confirmed_on", "how", "public_sha256", "correspondence")
MAX_SUPPORTERS, MAX_BYTES, MAX_NAME, MAX_URL = 200, 1024 * 1024, 120, 300
EARLIEST = date(2000, 1, 1)
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
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


def _record(record, today):
    if not isinstance(record, dict) or set(record) != set(CONSENT_KEYS):
        raise SupporterError("record must have exactly the fields " + ", ".join(CONSENT_KEYS))
    if record["organization"] is not True:
        raise SupporterError("organization: must be the attestation true")
    for key in ("public_sha256", "correspondence"):
        value = record[key]
        if not (value is None and key == "correspondence") and not (isinstance(value, str) and SHA256.fullmatch(value)):
            raise SupporterError(f"{key}: must be a lowercase SHA-256 hex digest" + (" or null" if key == "correspondence" else ""))
    return {"organization": True, "confirmed_on": _date(record["confirmed_on"], "confirmed_on", today),
            "how": _how(record["how"]), "public_sha256": record["public_sha256"],
            "correspondence": record["correspondence"]}


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
    with os.fdopen(fd, "rb") as handle:
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
        if os.lstat(kit).st_mode & 0o077:
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
