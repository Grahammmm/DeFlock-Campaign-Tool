"""Canonical ledger bootstrap and lossless, resumable legacy evidence import.

Imported declarations do not satisfy stage gates. This module intentionally has
no public promotion, scheduling, network, model, or publication operations.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from campaign_tool import __version__
from .migrations import v001

STAGES = ("preserve", "extract", "catalog", "detect", "review", "compare", "privacy")
STATUSES = ("done", "in_progress", "pending", "blocked", "inapplicable")
IMPORTER_VERSION = "legacy-bootstrap-1"
SOURCE_TABLES = ("docs", "occurrences", "units", "preservations", "extraction_attempts",
                 "edges", "edge_history", "document_history", "events", "runs",
                 "scope_exclusions", "seen", "meta")
SCHEMA_HASH = hashlib.sha256(v001.SQL.encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_path(value, *, existing=True):
    path = Path(os.path.abspath(value))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlink paths are not permitted")
    if existing and not path.is_file():
        raise ValueError("required file unavailable")
    return path


def private_parent(path):
    parent = path.parent
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if parent.stat().st_mode & 0o077 or parent.stat().st_uid != os.getuid():
        raise ValueError("ledger parent must be owner-only and owned by the current user")


def initialize(path):
    os.umask(0o077)
    path = checked_path(path, existing=False)
    private_parent(path)
    if path.exists():
        raise FileExistsError("existing ledger is never overwritten")
    fd, temporary_name = tempfile.mkstemp(prefix=".ledger-init-", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    con = None
    try:
        con = sqlite3.connect(temporary)
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA recursive_triggers=ON")
        con.execute("PRAGMA journal_mode=DELETE")
        con.executescript("BEGIN IMMEDIATE;\n" + v001.SQL)
        con.execute("INSERT INTO schema_migrations VALUES(?,?)", (v001.VERSION, SCHEMA_HASH))
        con.execute("INSERT INTO ledger_meta VALUES('acceptance','candidate_unreconciled')")
        con.execute("PRAGMA user_version=1")
        con.commit()
        con.close()
        con = None
        with temporary.open("rb") as stored:
            os.fsync(stored.fileno())
        # Publish only a committed database, without replacing any existing file.
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        with sqlite3.connect(path) as ready:
            ready.execute("PRAGMA journal_mode=WAL")
    finally:
        if con is not None:
            con.rollback()
            con.close()
        # These are only the temporary files allocated by this invocation.
        for owned in (temporary, Path(str(temporary) + "-journal")):
            if owned.exists():
                owned.unlink()
    return {"schema_version": v001.VERSION, "acceptance": "candidate_unreconciled"}


@contextmanager
def ledger(path, *, readonly=False):
    path = checked_path(path)
    private_parent(path)
    if path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
        raise ValueError("ledger file must be owner-only and owned by the current user")
    con = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA recursive_triggers=ON")
        if readonly:
            con.execute("PRAGMA query_only=ON")
        else:
            con.execute("PRAGMA journal_mode=WAL")
        if con.execute("PRAGMA user_version").fetchone()[0] != v001.VERSION:
            raise ValueError("unsupported ledger schema")
        row = con.execute("SELECT checksum FROM schema_migrations WHERE version=?", (v001.VERSION,)).fetchone()
        if row is None or row[0] != SCHEMA_HASH:
            raise ValueError("ledger migration identity mismatch")
        yield con
    finally:
        con.close()


def counts(path):
    with ledger(path, readonly=True) as con:
        total = con.execute("SELECT COUNT(*) FROM originals").fetchone()[0]
        states = {stage: {state: 0 for state in STATUSES} for stage in STAGES}
        for row in con.execute("SELECT stage,status,COUNT(*) FROM stage_state GROUP BY stage,status"):
            states[row[0]][row[1]] = row[2]
        accepted = con.execute("SELECT value FROM ledger_meta WHERE key='acceptance'").fetchone()[0]
        legacy = {row[0]: row[1] for row in con.execute("SELECT source_table,COUNT(*) FROM legacy_rows GROUP BY source_table")}
        pending_imports = con.execute("SELECT COUNT(*) FROM legacy_imports WHERE status!='captured'").fetchone()[0]
        return {"schema_version": v001.VERSION, "acceptance": accepted, "originals": total,
                "stages": states, "stage_slots_expected": total * len(STAGES),
                "stage_slots_observed": con.execute("SELECT COUNT(*) FROM stage_state").fetchone()[0],
                "canonical_occurrences": con.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0],
                "canonical_units": con.execute("SELECT COUNT(*) FROM units").fetchone()[0],
                "legacy_rows": legacy, "incomplete_imports": pending_imports,
                "end_to_end_complete": None,
                "limits": ["Candidate import has not passed receipt reconciliation or activation.",
                           "Legacy labels and captured rows are not accepted stage completion.",
                           "Mail identities, receipt gates and canonical unit/occurrence projection remain to integrate."]}


def capture_database(source, expected, directory):
    source = checked_path(source)
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("invalid source database identity")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("source must be an immutable captured snapshot, not a live database")
    directory = checked_path(directory, existing=False)
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise ValueError("captured input directory must be owner-only")
    target = directory / (expected + ".sqlite")
    if target.exists():
        checked_path(target)
        if file_hash(target) != expected:
            raise ValueError("captured source identity mismatch")
        return target
    fd, temporary = tempfile.mkstemp(prefix=".capture-", dir=directory)
    digest = hashlib.sha256()
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as out:
            for block in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        if digest.hexdigest() != expected:
            raise ValueError("source database hash mismatch")
        os.link(temporary, target)
    finally:
        os.unlink(temporary)
    return target


def json_value(value):
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    return value


def _seed_original(con, row, import_id, rowid, excluded):
    sha = row["sha"]
    if not isinstance(sha, str) or re.fullmatch(r"[a-f0-9]{64}", sha) is None:
        raise ValueError("invalid original identity in legacy source")
    size = row["bytes"]
    if type(size) is not int or size < 0:
        raise ValueError("invalid original byte count")
    existing = con.execute("SELECT bytes FROM originals WHERE sha256=?", (sha,)).fetchone()
    if existing is not None and existing[0] != size:
        raise ValueError("same original hash has contradictory byte lengths")
    if existing is not None:
        return
    provenance = canonical({"import_id": import_id, "table": "docs", "source_rowid": rowid,
                            "legacy_extraction_stage": row.get("stage"),
                            "legacy_review_status": row.get("review_status")})
    con.execute("INSERT INTO originals(sha256,bytes,mime_detected,first_seen_at,role,scope,storage_path,preservation_status,legacy_format,provenance_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (sha, size, None, row.get("first_seen"), "unresolved",
                 "out_of_scope" if sha in excluded else "unresolved", None,
                 "receipt_validation_pending", row.get("format"), provenance))
    stamp = now()
    con.executemany("INSERT INTO stage_state(original_sha256,stage,status,receipt_sha256,owner,updated_at,run_id,reason) VALUES(?,?,?,?,?,?,?,?)",
                    [(sha, stage, "pending", None, "receipt_reconciliation", stamp,
                      import_id, "legacy declaration retained; stage evidence not promoted") for stage in STAGES])


def import_legacy(snapshot, destination, *, batch_size=1000, max_batches=None):
    """Capture all legacy rows, seed original identities, and leave gates pending.

    max_batches is a bounded checkpoint/restart control, not an acceptance mode.
    No canonical occurrence or unit is synthesized from ambiguous legacy data.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 10000:
        raise ValueError("batch_size must be between 1 and 10000")
    if max_batches is not None and (type(max_batches) is not int or max_batches < 1):
        raise ValueError("max_batches must be a positive integer")
    snapshot = checked_path(snapshot, existing=False)
    manifest_path = checked_path(snapshot / "input-manifest.json")
    manifest_bytes = manifest_path.read_bytes()
    if len(manifest_bytes) > 16 * 1024 * 1024:
        raise ValueError("source manifest exceeds limit")
    manifest = json.loads(manifest_bytes)
    source_sha = manifest["database_snapshot_sha256"]
    destination = checked_path(destination, existing=False)
    private_parent(destination)
    source = checked_path(snapshot / "intake.sqlite")
    if destination == source or destination.parent == snapshot or snapshot in destination.parents:
        raise ValueError("candidate output must be outside the source snapshot")
    captured = capture_database(source, source_sha, destination.parent / "captured-inputs")
    source_manifest_sha = sha_bytes(manifest_bytes)
    importer_sha = file_hash(Path(__file__))
    config_sha = sha_bytes(canonical({"mode": "legacy_capture_without_stage_promotion", "schema": v001.VERSION}).encode())
    import_id = sha_bytes(canonical([source_sha, source_manifest_sha, IMPORTER_VERSION, importer_sha, config_sha]).encode())
    if not destination.exists():
        initialize(destination)
    src = sqlite3.connect(captured.as_uri() + "?mode=ro&immutable=1", uri=True)
    src.row_factory = sqlite3.Row
    src.execute("PRAGMA query_only=ON")
    try:
        names = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if names != set(SOURCE_TABLES):
            raise ValueError("unexpected legacy schema; classify all tables before importing")
        expected_counts = {name: src.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0] for name in SOURCE_TABLES}
        excluded = {r[0] for r in src.execute("SELECT sha FROM scope_exclusions WHERE sha!=''")}
        with ledger(destination) as con:
            con.execute("BEGIN IMMEDIATE")
            active = con.execute("SELECT id FROM legacy_imports WHERE status!='captured'").fetchall()
            if any(row[0] != import_id for row in active):
                raise ValueError("different unfinished input or importer; resume its exact identity first")
            prior = con.execute("SELECT status FROM legacy_imports WHERE id=?", (import_id,)).fetchone()
            if prior and prior[0] == "captured":
                con.rollback()
                return {"import_id": import_id, "status": "captured", "reused": True,
                        "stage_promotions": 0, "publication_ready": False}
            con.execute("INSERT OR IGNORE INTO legacy_imports VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (import_id, source_sha, source_manifest_sha, IMPORTER_VERSION, importer_sha,
                         str(captured), "in_progress", now(), None, canonical(expected_counts), config_sha))
            con.execute("INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (import_id, "legacy_capture_candidate", now(), None, __version__, None,
                         config_sha, None, "in_progress", canonical({"source_snapshot": source_manifest_sha})))
            con.commit()
            batches = 0
            for table in SOURCE_TABLES:
                columns = list(src.execute('PRAGMA table_info("' + table + '")'))
                pk = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
                prior = con.execute("SELECT last_rowid,copied_rows FROM legacy_checkpoints WHERE import_id=? AND source_table=?",
                                    (import_id, table)).fetchone()
                last, copied = (prior[0], prior[1]) if prior else (-9223372036854775808, 0)
                while True:
                    batch = src.execute('SELECT rowid AS _source_rowid_,* FROM "' + table + '" WHERE rowid>? ORDER BY rowid LIMIT ?',
                                        (last, batch_size)).fetchall()
                    if not batch:
                        if copied != expected_counts[table]:
                            raise ValueError("legacy table reconciliation failed")
                        break
                    con.execute("BEGIN IMMEDIATE")
                    try:
                        for item in batch:
                            row = dict(item)
                            rowid = row.pop("_source_rowid_")
                            identity = canonical({key: json_value(row[key]) for key in pk} if pk else {"rowid": rowid})
                            payload = canonical({key: json_value(value) for key, value in row.items()})
                            con.execute("INSERT INTO legacy_rows VALUES(?,?,?,?,?,?)",
                                        (import_id, table, rowid, identity, payload, sha_bytes(payload.encode())))
                            if table == "docs":
                                _seed_original(con, row, import_id, rowid, excluded)
                        last = batch[-1]["_source_rowid_"]
                        copied += len(batch)
                        con.execute("INSERT INTO legacy_checkpoints VALUES(?,?,?,?) ON CONFLICT(import_id,source_table) DO UPDATE SET last_rowid=excluded.last_rowid,copied_rows=excluded.copied_rows",
                                    (import_id, table, last, copied))
                        con.commit()
                    except Exception:
                        con.rollback()
                        raise
                    batches += 1
                    if max_batches is not None and batches >= max_batches:
                        return {"import_id": import_id, "status": "checkpointed", "last_table": table,
                                "batches_this_run": batches, "stage_promotions": 0, "publication_ready": False}
            con.execute("BEGIN IMMEDIATE")
            for table in SOURCE_TABLES:
                actual = con.execute("SELECT COUNT(*) FROM legacy_rows WHERE import_id=? AND source_table=?", (import_id, table)).fetchone()[0]
                if actual != expected_counts[table]:
                    raise ValueError("final legacy row reconciliation failed")
            summary = canonical({"source_rows": expected_counts, "stage_promotions": 0,
                                 "remaining": ["occurrence_identity_projection", "unit_parser_provenance", "digest_and_receipt_reconciliation"]})
            con.execute("UPDATE legacy_imports SET status='captured',finished_at=? WHERE id=?", (now(), import_id))
            con.execute("UPDATE runs SET status='captured_unreconciled',ended_at=?,summary=? WHERE run_id=?", (now(), summary, import_id))
            con.commit()
            return {"import_id": import_id, "status": "captured", "reused": False,
                    "source_rows": expected_counts, "stage_promotions": 0, "publication_ready": False}
    finally:
        src.close()
