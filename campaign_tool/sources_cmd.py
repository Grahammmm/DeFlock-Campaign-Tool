"""`sources add | list | edit | remove`: look after content/sources.json, the public source library.

`add` resolves the hash against the private ledger (read-only); `list` prints every entry with the slugs of the
published findings citing its record; `edit` changes title/note/public_url/pages of one entry chosen by >= 12 hex
of its library hash (+ `--fragment`); `remove` deletes one, refused while a published finding cites the record and
no other entry for it remains. Writers validate the whole new list with the content loader, then replace the file
atomically. The ledger and private/objects/ are never touched. Nothing is fetched, uploaded or published.
"""
import json
import os
import re
import sqlite3
import stat
import tempfile
from datetime import datetime
from pathlib import Path

from .content import (MAX_JSON_BYTES, ContentError, _read_json, default_agencies, load_agencies, load_finding,
                      load_sources)

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
    _rewrite(path, document[:index] + [updated_entry] + document[index + 1:])
    return {"edited": entry["sha256"][:12], "fragment": entry["fragment"], "changed": changed}


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
