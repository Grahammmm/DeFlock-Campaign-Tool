"""`status --list`: ingested ledger receipts with 12-char hash prefixes, read-only.

The ledger bytes are read once into an in-memory SQLite copy (SQLite never opens the file, so no -wal/-shm
sidecar is created) and only queried with bound parameters; nothing is written, fetched or published.
Only the public 12-character prefix of each hash is printed; give `sources add` more characters if it reports an
ambiguous prefix.
"""
import os
import re
import sqlite3
import stat
from datetime import date, datetime, timezone

from .content import load_sources
from .evidence import PREFIX
from .sources_cmd import _read_existing

MAX_ROWS = 5000
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
SINCE_ERROR = "--since: use YYYY-MM-DD or an ISO timestamp with offset"
SIDECARS = ("journal", "wal", "shm")
SIDECAR_ERROR = "private/ledger.sqlite has a journal or WAL file beside it; list again when no ingest is running"


def _since(value):
    """Return the lower bound compared against UTC isoformat received_at strings."""
    if DATE.fullmatch(value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise ValueError(SINCE_ERROR) from None
    try:
        moment = datetime.fromisoformat(value) if len(value) <= 40 else None
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise ValueError(SINCE_ERROR)
    return moment.astimezone(timezone.utc).isoformat()


def _registered(root):
    """Full hashes in content/sources.json; absent file -> empty set, invalid -> its ContentError."""
    document = _read_existing(root / "content" / "sources.json")[1]
    load_sources(document)
    return {entry["sha256"] for entry in document}


def _snapshot(path):
    """In-memory copy of the ledger; SQLite never opens the file itself.

    Even mode=ro creates -wal/-shm sidecars beside a WAL-mode database, so the bytes are read once (no link
    followed) and deserialized. A journal or WAL sidecar means the file alone may not hold the committed state:
    refused. A WAL header with no sidecar is a complete, checkpointed file and is read as a rollback database."""
    if any(os.path.lexists(f"{path}-{suffix}") for suffix in SIDECARS):
        raise ValueError(SIDECAR_ERROR)
    db = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError
            with os.fdopen(os.dup(fd), "rb") as handle:
                data = bytearray(handle.read())
        finally:
            os.close(fd)
        if any(os.path.lexists(f"{path}-{suffix}") for suffix in SIDECARS):
            raise ValueError(SIDECAR_ERROR)  # a writer started while the bytes were read
        if data[:16] == b"SQLite format 3\0" and data[18:20] == b"\2\2":
            data[18:20] = b"\1\1"
        db = sqlite3.connect(":memory:")
        db.deserialize(bytes(data))
        db.execute("SELECT count(*) FROM objects JOIN receipts ON 0").fetchone()
    except (OSError, sqlite3.Error):
        if db is not None:
            db.close()
        raise ValueError("private/ledger.sqlite is not a readable ledger") from None
    return db


def run_cli(args, root):
    """`status --list` flags: filters need --list, and --list never runs with --check/--fix-modes."""
    if not args.list:
        raise ValueError("--source-id/--since/--not-in-sources need --list")
    if args.check or args.fix_modes:
        raise ValueError("--list is read-only; run --check separately")
    return list_receipts(root, args.source_id, args.since, args.not_in_sources)


def list_receipts(root, source_id=None, since=None, not_in_sources=False):
    """Return {"receipts": [...], "count": n[, "more": k]} from an in-memory copy of private/ledger.sqlite."""
    clauses, params = [], []
    if source_id is not None:
        if not 1 <= len(source_id) <= 200:
            raise ValueError("--source-id: use 1-200 characters")
        clauses.append("r.source_id = ?")
        params.append(source_id)
    if since is not None:
        clauses.append("r.received_at >= ?")
        params.append(_since(since))
    registered = _registered(root) if not_in_sources else set()
    path = root / "private" / "ledger.sqlite"
    if not os.path.lexists(path) and not path.parent.is_symlink():
        return {"receipts": [], "count": 0}
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError("private/ledger.sqlite must be a regular file, not a symlink")
    db = _snapshot(path)
    try:
        rows = db.execute(
            "SELECT r.object_sha256, o.byte_count, r.source_id, r.original_name, r.received_at FROM receipts r "
            "JOIN objects o ON o.sha256 = r.object_sha256"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY r.received_at, r.receipt_id", params).fetchall()
    finally:
        db.close()
    receipts = [{"sha256_prefix": sha[:PREFIX], "bytes": size, "source_id": source,
                 "original_name": CONTROL.sub("?", str(name or ""))[:200], "received_at": received}
                for sha, size, source, name, received in rows if sha not in registered]
    report = {"receipts": receipts[:MAX_ROWS], "count": min(len(receipts), MAX_ROWS)}
    if len(receipts) > MAX_ROWS:
        report["more"] = len(receipts) - MAX_ROWS
    return report
