"""`sources add | add-batch | list | edit | remove | verify-copy`: look after content/sources.json, the source library.

`verify-copy` hashes a local copy the organizer downloaded from an entry's public_url (nothing is fetched) and
records `public_copy` {matches, checked_on, reason?} on the entries of that record sharing that exact public_url
string (`--fragment` picks the URL when a record has several; no URL is normalized); `edit` drops that check when
public_url changes.

`add` resolves the hash against the private ledger (read-only); `add-batch` appends every not-yet-listed record
of one ingest source_id (read via `status --list`'s in-memory ledger copy); `add-batch --from-request AGENCY` instead takes the
production records linked on that request's produced steps (kit/sent.json, never its correspondence); `list` prints every entry with the slugs of the
published findings citing its record; `edit` changes title/note/public_url/pages of one entry chosen by >= 12 hex
of its library hash (+ `--fragment`); `remove` deletes one, refused while a published finding cites the record and
no other entry for it remains. Writers validate the whole new list with the content loader, then replace the file
atomically. The ledger and private/objects/ are never touched. Nothing is fetched, uploaded or published.
"""
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from datetime import date, datetime
from pathlib import Path

from .agency_cards import visible
from .content import (COPY_REASONS, MAX_JSON_BYTES, ContentError, _read_json, default_agencies, load_agencies,
                      load_finding, load_sources)

PREFIX = re.compile(r"[0-9a-f]{12,64}")


def resolve_hash(root, prefix):
    """Return the one ingested sha256 starting with ``prefix`` (>= 12 lowercase hex)."""
    if not isinstance(prefix, str) or not PREFIX.fullmatch(prefix):
        raise ValueError("--sha256: give at least 12 lowercase hex characters of an ingested hash")
    ledger = Path(root) / "private" / "ledger.sqlite"
    if ledger.is_symlink() or not ledger.is_file():
        raise ValueError("no ledger: run ingest first")
    db = sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT sha256 FROM objects WHERE substr(sha256, 1, ?) = ? ORDER BY sha256",
                          (len(prefix), prefix)).fetchall()
    finally:
        db.close()
    if not rows:
        raise ValueError("not ingested: run ingest first")
    if len(rows) > 1:
        raise ValueError(f"ambiguous prefix ({len(rows)} matches)")
    return rows[0][0]


def _read_existing(path):
    """Return (raw text or None, parsed list) for content/sources.json."""
    if path.is_symlink():
        raise ContentError("sources.json: symlinks are not read or replaced")
    if not path.exists():
        return None, []
    data = path.read_bytes()
    if len(data) > MAX_JSON_BYTES:
        raise ContentError("sources.json: file too large")
    try:
        return data.decode("utf-8"), json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContentError(f"sources.json: not valid UTF-8 JSON ({exc})") from None
    except RecursionError:
        raise ContentError("sources.json: not valid UTF-8 JSON (nested too deeply)") from None


def _known_agencies(root):
    path = Path(root) / "content" / "agencies.json"
    if path.is_file() or path.is_symlink():
        return load_agencies(_read_json(path), "content/agencies.json")
    return default_agencies(root)


def _append_text(raw, existing, entry):
    """Append one entry, keeping every existing byte up to the closing bracket."""
    if not existing:
        return json.dumps([entry], indent=2, ensure_ascii=False) + "\n"
    block = json.dumps(entry, indent=2, ensure_ascii=False).replace("\n", "\n  ")
    return raw.rstrip()[:-1].rstrip() + ",\n  " + block + "\n]\n"


def _write_atomic(path, text):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".sources-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def add_source(root, *, sha, title, agency_id, received_at, fragment, public_url=None, pages=None, note=""):
    """Append one validated library entry; return {"added", "fragment", "entries"}."""
    content = Path(root) / "content"
    if content.is_symlink() or not content.is_dir():
        raise ContentError("content/ is required")
    digest = resolve_hash(root, sha)
    if pages is not None and (type(pages) is not int or pages < 0):
        raise ContentError("--pages: must be a whole number of 0 or more")
    path = content / "sources.json"
    raw, document = _read_existing(path)
    entry = {"sha256": digest, "title": title, "agency_id": agency_id, "received_at": received_at,
             "public_url": public_url, "fragment": fragment}
    if pages is not None:
        entry["pages"] = pages
    if note:
        entry["note"] = note
    updated = document + [entry] if isinstance(document, list) else document
    load_sources(updated)
    if datetime.fromisoformat(received_at.replace("Z", "+00:00")).tzinfo is None:
        raise ContentError(f"sources.json[{len(updated) - 1}].received_at: must include a time zone offset")
    if agency_id not in {agency["agency_id"] for agency in _known_agencies(root)}:
        raise ContentError(f"agency_id {agency_id!r} is not one of the campaign's agencies")
    text = _append_text(raw, document, entry)
    if json.loads(text) != updated:
        raise ContentError("sources.json: could not append without changing existing entries")
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ContentError("sources.json: file too large")
    _write_atomic(path, text)
    return {"added": digest[:12], "fragment": fragment, "entries": len(updated)}


BATCH_MAX = 500
TAG = re.compile(r"<[^>]*>?")


def _batch_title(name):
    """Plain-text title from an original file name stem (<= 120 chars), marked for an organizer edit."""
    stem = re.split(r"[\\/]", TAG.sub(" ", name))[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem.lstrip(".") else stem
    stem = "".join(char if visible(char) and char not in "<>\t\n" else " " for char in stem)
    stem = " ".join(stem.split())[:120].rstrip() or "Untitled record"
    return stem + " [EDIT ME: title]"


def _full_hashes(root, prefixes):
    """Map each 12-char prefix to its one full ledger hash, from DF2237-17's in-memory ledger copy."""
    from .ledger_list import _snapshot
    db = _snapshot(Path(root) / "private" / "ledger.sqlite")
    try:
        found = {}
        for (sha,) in db.execute("SELECT sha256 FROM objects ORDER BY sha256"):
            if sha[:12] in prefixes:
                found.setdefault(sha[:12], []).append(sha)
    finally:
        db.close()
    if any(len(found.get(prefix, [])) != 1 for prefix in prefixes):
        raise ContentError("ledger changed or a 12-char prefix is ambiguous; use sources add for those records")
    return {prefix: hashes[0] for prefix, hashes in found.items()}


def add_batch(root, source_id, agency_id, received_at, fragment=None, dry_run=False):
    """Add every receipt of ``source_id`` whose record is not yet in the library, in one validated write."""
    from .ledger_list import list_receipts
    content = Path(root) / "content"
    if content.is_symlink() or not content.is_dir():
        raise ContentError("content/ is required")
    try:
        moment = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    except ValueError:
        raise ContentError("--received-at: use an ISO timestamp with a UTC offset") from None
    if moment.tzinfo is None:
        raise ContentError("--received-at: must include a time zone offset")
    path, raw, document, fragment = _batch_target(root, agency_id, fragment)
    listing = list_receipts(root, source_id)
    if listing.get("more"):
        raise ContentError(f"more than {listing['count']} receipts for source_id; nothing written")
    if not listing["receipts"]:
        raise ContentError(f"no receipts for source_id {source_id!r}")
    full = _full_hashes(root, {receipt["sha256_prefix"] for receipt in listing["receipts"]})
    listed = {entry["sha256"] for entry in document}
    new, already = {}, set()
    for receipt in listing["receipts"]:
        sha = full[receipt["sha256_prefix"]]
        if sha in listed:
            already.add(sha)
        elif sha not in new:
            new[sha] = {"sha256": sha, "title": _batch_title(receipt["original_name"]), "agency_id": agency_id,
                        "received_at": received_at, "public_url": None, "fragment": fragment, "pages": None,
                        "note": f"added in batch from {source_id}"}
    _batch_write(path, raw, document, new, dry_run)
    return {"added": [sha[:12] for sha in new], "already_listed": len(already), "dry_run": dry_run}


def _batch_target(root, agency_id, fragment):
    """Shared head of the batch writers: (sources.json path, raw text, validated list, fragment to use)."""
    content = Path(root) / "content"
    if content.is_symlink() or not content.is_dir():
        raise ContentError("content/ is required")
    if agency_id not in {agency["agency_id"] for agency in _known_agencies(root)}:
        raise ContentError(f"agency_id {agency_id!r} is not one of the campaign's agencies")
    path = content / "sources.json"
    raw, document = _read_existing(path)
    load_sources(document)
    if fragment is None:
        fragments = sorted({entry["fragment"] for entry in document if entry["agency_id"] == agency_id})
        if len(fragments) != 1:
            raise ContentError(f"agency {agency_id!r} has {len(fragments)} existing fragments; give --fragment")
        fragment = fragments[0]
    if fragment == "library":
        raise ContentError("--fragment: 'library' is the page anchor; choose another slug")
    return path, raw, document, fragment


def _batch_write(path, raw, document, new, dry_run):
    """Shared tail of the batch writers: cap, validate the whole list, append in memory, write once."""
    if len(new) > BATCH_MAX:
        raise ContentError(f"{len(new)} new records; at most {BATCH_MAX} per run, nothing written")
    updated, text, existing = document + list(new.values()), raw or "", document
    load_sources(updated)
    for entry in new.values():  # the one-entry append path, applied in memory; one write below
        text, existing = _append_text(text, existing, entry), True
    if new and json.loads(text) != updated:
        raise ContentError("sources.json: could not append without changing existing entries")
    if new and len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ContentError("sources.json: file too large")
    if new and not dry_run:
        _write_atomic(path, text)


def _production_dates(root, agency_id, request):
    """{full hash: earliest produced-step date} of one request, in step order; correspondence is never read."""
    from . import kit_track
    request = kit_track._request(request)
    if "#" in agency_id:
        raise ValueError(f"no hand-sent request recorded for {agency_id}; run kit-sent first")
    entry = kit_track.read_sent(root).get(kit_track._key(agency_id, request))
    if entry is None and request != "main":
        raise ValueError(f"no sent request {agency_id}/{request}; run kit-sent --request {request} first")
    if entry is None:
        raise ValueError(f"no hand-sent request recorded for {agency_id}; run kit-sent first")
    dates = {}
    for step in entry["steps"]:
        day = kit_track._day(step.get("on")) if isinstance(step, dict) and step.get("kind") == "produced" else None
        for sha in kit_track._step_records(step) if day else ():
            if sha not in dates or day < dates[sha]:
                dates[sha] = day
    if not dates:
        raise ContentError(f"request {kit_track._label(agency_id, request)} has no linked production records")
    return dates


def _original_names(root, wanted):
    """{full hash: first non-empty original file name} from the ledger copy; {} without a ledger."""
    path = Path(root) / "private" / "ledger.sqlite"
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        return {}
    from .ledger_list import _snapshot
    db = _snapshot(path)
    try:
        rows = db.execute("SELECT object_sha256, original_name FROM receipts ORDER BY received_at, receipt_id")
        names = {}
        for sha, name in rows:
            if sha in wanted and sha not in names and name and str(name).strip():
                names[sha] = str(name)
    finally:
        db.close()
    return names


def add_from_request(root, agency_id, *, request="main", tz="America/Los_Angeles", fragment=None, dry_run=False):
    """Add the production records linked on one request's produced steps that the library does not list yet."""
    from .meetings import resolve_zone
    zone = resolve_zone(tz)
    if zone is None:
        raise ContentError(f"unknown time zone {tz}")
    dates = _production_dates(root, agency_id, request)
    path, raw, document, fragment = _batch_target(root, agency_id, fragment)
    listed = {entry["sha256"] for entry in document}
    names = _original_names(root, set(dates) - listed)
    label = f"{agency_id}/{request}"
    new = {}
    for sha, day in dates.items():
        if sha not in listed:
            title = _batch_title(names[sha]) if sha in names else f"Record {sha[:12]} [EDIT ME: title]"
            moment = datetime(day.year, day.month, day.day, tzinfo=zone)  # the step date at 00:00 in --tz
            new[sha] = {"sha256": sha, "title": title, "agency_id": agency_id, "received_at": moment.isoformat(),
                        "public_url": None, "fragment": fragment, "pages": None,
                        "note": f"added in batch from request {label}"}
    _batch_write(path, raw, document, new, dry_run)
    return {"added": [sha[:12] for sha in new], "already_listed": len(dates) - len(new), "dry_run": dry_run}


def _library(root):
    """Return (path, validated raw list) of content/sources.json; content/ must exist, nothing is created."""
    content = Path(root) / "content"
    if content.is_symlink() or not content.is_dir():
        raise ContentError("content/ is required")
    path = content / "sources.json"
    document = _read_existing(path)[1]
    load_sources(document)
    return path, document


def _citations(root, strict):
    """Map full sha256 -> sorted citing finding slugs; ``strict`` (remove) stops on an unreadable finding."""
    cited, folder = {}, Path(root) / "content" / "findings"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            finding = load_finding(_read_json(path), "findings/" + path.name)
        except (OSError, ValueError, TypeError, AttributeError, LookupError, RecursionError) as exc:
            # Malformed field types (e.g. classification=[]) fail inside the loader with TypeError & co.
            if strict:
                raise ContentError(f"findings/{path.name} cannot be read, so its citations cannot be checked "
                                   f"({exc})") from None
            continue
        for source in finding["sources"]:
            cited.setdefault(source["sha256"], set()).add(finding["slug"])
    return {sha: sorted(slugs) for sha, slugs in cited.items()}


def _select(document, prefix, fragment):
    """Index of the one library entry whose sha256 starts with ``prefix`` (narrowed by ``fragment``)."""
    if not isinstance(prefix, str) or not PREFIX.fullmatch(prefix):
        raise ValueError("--sha256: give at least 12 lowercase hex characters of a library entry's hash")
    hits = [index for index, entry in enumerate(document)
            if entry["sha256"].startswith(prefix) and fragment in (None, entry["fragment"])]
    if not hits:
        raise ContentError(f"no library entry for {prefix}" + (f" in fragment {fragment!r}" if fragment else ""))
    if len({document[index]["sha256"] for index in hits}) > 1:
        raise ContentError(f"ambiguous prefix ({len(hits)} entries); give more hex characters")
    if len(hits) > 1:
        names = ", ".join(document[index]["fragment"] for index in hits)
        raise ContentError(f"ambiguous: {len(hits)} entries; add --fragment (one of: {names})")
    return hits[0]


def _rewrite(path, updated):
    load_sources(updated)
    text = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ContentError("sources.json: file too large")
    _write_atomic(path, text)


def list_sources(root):
    """Every library entry with the published findings citing its record; read-only."""
    document = _library(root)[1]
    cited = _citations(root, strict=False)
    entries = [{"sha256": entry["sha256"][:12], "fragment": entry["fragment"], "title": entry["title"],
                "agency_id": entry["agency_id"], "received_at": entry["received_at"],
                "public_url": entry["public_url"], "pages": entry["pages"], "cited_by": cited.get(entry["sha256"], [])}
               for entry in load_sources(document)]
    entries.sort(key=lambda entry: (entry["received_at"], entry["fragment"]))
    return {"entries": entries, "count": len(entries)}


def edit_source(root, prefix, fragment=None, *, title=None, note=None, public_url=None, pages=None,
                clear_public_url=False):
    """Change title / note / public_url / pages of one entry; return {"edited", "fragment", "changed"}."""
    path, document = _library(root)
    if clear_public_url and public_url is not None:
        raise ContentError("--public-url and --clear-public-url cannot be combined")
    if pages is not None and (type(pages) is not int or pages < 0):
        raise ContentError("--pages: must be a whole number of 0 or more")
    index = _select(document, prefix, fragment)
    entry = document[index]
    changes = {key: value for key, value in (("title", title), ("note", note), ("public_url", public_url),
                                             ("pages", pages)) if value is not None}
    if clear_public_url:
        changes["public_url"] = None
    updated_entry = {**entry, **changes}
    if updated_entry.get("note") == "":
        del updated_entry["note"]
    changed = sorted(key for key in changes if updated_entry.get(key) != entry.get(key))
    if not changed:
        raise ContentError("nothing to change")
    dropped = "public_url" in changed and updated_entry.pop("public_copy", None) is not None  # old check is stale
    _rewrite(path, document[:index] + [updated_entry] + document[index + 1:])
    return {"edited": entry["sha256"][:12], "fragment": entry["fragment"], "changed": changed,
            **({"public_copy_removed": True} if dropped else {})}


COPY_MAX = 200 * 1024 * 1024


def _hash_copy(file):
    """sha256 of a regular local file (no symlink, <= COPY_MAX), read in <= 1 MiB chunks; never copied or stored.

    The limit is enforced on the bytes actually read, not only on the size seen at open: a file that grows past
    COPY_MAX after that check is refused before any digest is returned (at most COPY_MAX + 1 bytes are consumed and
    only COPY_MAX are hashed).
    """
    try:
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # a FIFO never blocks the run
    except FileNotFoundError:
        raise ValueError(f"--file: {file} does not exist") from None
    except OSError as exc:
        reason = "is a symlink; give the downloaded file itself" if Path(file).is_symlink() else f"cannot be read ({exc})"
        raise ValueError(f"--file: {file} {reason}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > COPY_MAX:
            raise ValueError(f"--file: {file} is not a regular file" if not stat.S_ISREG(info.st_mode)
                             else f"--file: larger than {COPY_MAX // (1024 * 1024)} MiB; nothing hashed")
        handle = os.fdopen(fd, "rb", buffering=0)  # unbuffered: no read-ahead beyond the bytes counted below
    except BaseException:
        os.close(fd)
        raise
    with handle:
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = handle.read(min(1024 * 1024, COPY_MAX - total + 1))  # at most one byte past the limit
            if not chunk:
                break
            total += len(chunk)
            if total > COPY_MAX:
                raise ValueError(f"--file: larger than {COPY_MAX // (1024 * 1024)} MiB; verification not recorded")
            digest.update(chunk)
    return digest.hexdigest()


def _copy_targets(document, sha, fragment=None):
    """Indices of the entries one verification covers: entries of ``sha`` sharing one exact public_url string.

    Without ``fragment`` the record must have a single distinct URL; with it, that library fragment's URL chooses the group.
    """
    entries = [(index, entry) for index, entry in enumerate(document) if entry["sha256"] == sha]
    if fragment is None:
        urls = {entry["public_url"] for _, entry in entries if entry["public_url"]}
        if not urls:
            raise ValueError("entry has no public_url; nothing to compare")
        if len(urls) > 1:
            raise ValueError("record has multiple public URLs; add --fragment to choose the copy")
        return [index for index, entry in entries if entry["public_url"]]
    if not isinstance(fragment, str) or not fragment:
        raise ValueError("--fragment: give a non-empty library fragment")
    chosen = [entry for _, entry in entries if entry["fragment"] == fragment]
    if not chosen:
        raise ValueError("no library entry for this record in the selected fragment")
    url = chosen[0]["public_url"]
    if not url:
        raise ValueError("selected entry has no public_url; nothing to compare")
    return [index for index, entry in entries if entry["public_url"] == url]


def verify_copy(root, prefix, file, checked_on, differs_because=None, today=None, *, fragment=None):
    """Hash a copy the organizer downloaded from a public_url; record public_copy on the entries sharing that URL."""
    path, document = _library(root)
    if not isinstance(prefix, str) or not PREFIX.fullmatch(prefix):
        raise ValueError("--sha256: give 12 to 64 lowercase hex characters of a library entry's hash")
    records = {entry["sha256"] for entry in document if entry["sha256"].startswith(prefix)}
    if not records:
        raise ValueError(f"no library entry starts with {prefix}")
    if len(records) > 1:
        raise ValueError(f"prefix matches {len(records)} entries; give more characters")
    sha = records.pop()
    targets = _copy_targets(document, sha, fragment)
    if differs_because is not None and differs_because not in COPY_REASONS:
        raise ValueError("--differs-because: one of " + ", ".join(COPY_REASONS))
    try:
        day = datetime.strptime(checked_on, "%Y-%m-%d").date() if isinstance(checked_on, str) else None
    except ValueError:
        day = None
    if day is None or day.isoformat() != checked_on:
        raise ValueError("--checked-on: a real YYYY-MM-DD date")
    if day > (today or date.today()):
        raise ValueError("--checked-on: must not be in the future")
    received = max(document[index]["received_at"][:10] for index in targets)
    if checked_on < received:
        raise ValueError(f"--checked-on: must not be before the record was received ({received})")
    digest = _hash_copy(file)
    matches = digest == sha
    if not matches and differs_because is None:
        raise ValueError("copy differs from the record; pass --differs-because redacted|converted|unknown")
    if matches and differs_because is not None:
        raise ValueError("copy matches; --differs-because is only for a differing copy")
    check = {"matches": matches, "checked_on": checked_on, **({} if matches else {"reason": differs_because})}
    _rewrite(path, [{**entry, "public_copy": check} if index in targets else entry
                    for index, entry in enumerate(document)])
    return {"sha256_prefix": sha[:12], **check, **({} if matches else {"copy_sha256_prefix": digest[:12]})}


def remove_source(root, prefix, fragment=None):
    """Delete one entry unless a published finding cites a record no other entry keeps; ledger untouched."""
    path, document = _library(root)
    index = _select(document, prefix, fragment)
    entry, remaining = document[index], document[:index] + document[index + 1:]
    if not any(other["sha256"] == entry["sha256"] for other in remaining):
        slugs = _citations(root, strict=True).get(entry["sha256"])
        if slugs:
            raise ContentError(f"cited by published finding(s): {', '.join(slugs)}; correct or withdraw them first")
    _rewrite(path, remaining)
    return {"removed": entry["sha256"][:12], "fragment": entry["fragment"], "entries": len(remaining)}
