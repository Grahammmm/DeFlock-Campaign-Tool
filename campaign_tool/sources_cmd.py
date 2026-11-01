"""`sources add`: register one ingested record in content/sources.json.

The hash is resolved against the private ledger (read-only), the whole new list
is validated by the content loader before anything is written, and the file is
replaced atomically. Nothing is fetched, uploaded or published.
"""
import json
import os
import re
import sqlite3
import stat
import tempfile
from datetime import datetime
from pathlib import Path

from .content import MAX_JSON_BYTES, ContentError, _read_json, default_agencies, load_agencies, load_sources

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
