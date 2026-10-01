"""Exact-artifact, run-bound acceptance of a private catalog snapshot only.

Acceptance selects a private board snapshot. It grants no stage completion,
publication approval, server access, or permission to process additional data.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat

from . import ledger_catalog as catalog

SQL = """
CREATE TABLE IF NOT EXISTS snapshot_meta(version INTEGER PRIMARY KEY CHECK(version=1));
INSERT OR IGNORE INTO snapshot_meta VALUES(1);
CREATE TABLE IF NOT EXISTS snapshot_acceptances(
 snapshot_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, catalog_sha256 TEXT NOT NULL,
 manifest_sha256 TEXT NOT NULL, receipt_sha256 TEXT NOT NULL, receipt_json BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS accepted_pointer(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 snapshot_id TEXT NOT NULL REFERENCES snapshot_acceptances(snapshot_id));
CREATE TABLE IF NOT EXISTS snapshot_events(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, previous_snapshot_id TEXT,
 snapshot_id TEXT NOT NULL REFERENCES snapshot_acceptances(snapshot_id));
CREATE TRIGGER IF NOT EXISTS immutable_acceptances_update BEFORE UPDATE ON snapshot_acceptances
 BEGIN SELECT RAISE(ABORT,'immutable snapshot acceptance'); END;
CREATE TRIGGER IF NOT EXISTS immutable_acceptances_delete BEFORE DELETE ON snapshot_acceptances
 BEGIN SELECT RAISE(ABORT,'immutable snapshot acceptance'); END;
CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON snapshot_events
 BEGIN SELECT RAISE(ABORT,'immutable snapshot history'); END;
CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON snapshot_events
 BEGIN SELECT RAISE(ABORT,'immutable snapshot history'); END;
"""


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            catalog._require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError) as error:
        raise catalog.CatalogError("invalid_snapshot_json") from error


def _read(path):
    path = catalog._safe_path(path)
    catalog._require(path.stat().st_size <= catalog.MAX_BYTES, "artifact_size_limit")
    with path.open("rb") as source:
        raw = source.read(catalog.MAX_BYTES + 1)
    catalog._require(len(raw) <= catalog.MAX_BYTES, "artifact_size_limit")
    return raw


def _load(root, snapshot_id, expected_catalog, expected_manifest):
    catalog._require(type(snapshot_id) is str and catalog.HASH.fullmatch(snapshot_id), "invalid_snapshot_id")
    for value in (expected_catalog, expected_manifest):
        catalog._require(type(value) is str and catalog.HASH.fullmatch(value), "invalid_expected_hash")
    directory = catalog._safe_path(root / snapshot_id, directory=True)
    raw, manifest_raw = _read(directory / "catalog.json"), _read(directory / "artifact-hashes.json")
    catalog._require(catalog.digest(raw) == expected_catalog and
                     catalog.digest(manifest_raw) == expected_manifest, "artifact_hash_mismatch")
    value = _decode(raw)
    catalog._require(type(value) is dict and type(value.get("schema_version")) is int and
                     value["schema_version"] == catalog.SCHEMA_VERSION, "snapshot_schema_mismatch")
    catalog._require(value.get("snapshot_id") == snapshot_id and
                     catalog.digest(catalog.encoded({k: v for k, v in value.items() if k != "snapshot_id"})) == snapshot_id,
                     "stale_snapshot_binding")
    try:
        expected = catalog.artifact_payloads(value)
    except (KeyError, TypeError, AttributeError) as error:
        raise catalog.CatalogError("snapshot_schema_mismatch") from error
    catalog._require(raw == expected["catalog.json"] and manifest_raw == expected["artifact-hashes.json"],
                     "noncanonical_snapshot_bytes")
    catalog._require({p.name for p in directory.iterdir()} == set(expected), "artifact_inventory_mismatch")
    for name, content in expected.items():
        catalog._require(_read(directory / name) == content, "artifact_bytes_mismatch")
    return value


@contextmanager
def _locked(root):
    root = catalog._safe_path(root, directory=True)
    catalog._outside_repo(root)
    path = root / "catalog-export.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        catalog._require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                         not info.st_mode & 0o077, "unsafe_export_lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield root
    finally:
        os.close(fd)


def _connect(root):
    path = root / "accepted-snapshots.sqlite"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        catalog._safe_path(path)
    else:
        os.close(fd)
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA journal_mode=DELETE")
    con.executescript(SQL)
    catalog._require([tuple(row) for row in con.execute("SELECT version FROM snapshot_meta")] == [(1,)],
                     "acceptance_schema_mismatch")
    return con


def _receipt(row):
    raw = bytes(row["receipt_json"])
    catalog._require(catalog.digest(raw) == row["receipt_sha256"], "acceptance_receipt_hash_mismatch")
    receipt = _decode(raw)
    for key in ("snapshot_id", "run_id", "catalog_sha256", "manifest_sha256"):
        catalog._require(receipt.get(key) == row[key], "acceptance_receipt_binding_mismatch")
    return receipt


def _sync_root(root):
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _after_commit():
    """Crash-recovery fault injection boundary; no side effect in production."""


def accept_snapshot(database, private_scratch, private_output, snapshot_id, *,
                    run_id, expected_catalog_sha256, expected_manifest_sha256,
                    expected_current_snapshot_id=None):
    """Compare-and-swap a private pointer after exact-artifact and current-source checks.

    A replay of the current snapshot returns the original receipt. A superseded
    snapshot cannot silently reactivate. The source is a consistent read capture;
    subsequent ledger writes naturally require a new snapshot.
    """
    with _locked(private_output) as root:
        value = _load(root, snapshot_id, expected_catalog_sha256, expected_manifest_sha256)
        binding = value.get("run_binding")
        catalog._require(type(binding) is dict and binding.get("run_id") == run_id, "run_binding_mismatch")
        catalog._require(binding.get("status") in ("completed", "succeeded", "success") and
                         type(binding.get("engine_version")) is str and binding["engine_version"].strip() and
                         type(binding.get("config_sha256")) is str and catalog.HASH.fullmatch(binding["config_sha256"]),
                         "run_not_finalized_or_bound")
        try:
            timestamp = datetime.fromisoformat(binding["ended_at"])
            catalog._require(timestamp.tzinfo is not None, "run_timestamp_timezone_required")
        except (TypeError, ValueError, KeyError) as error:
            raise catalog.CatalogError("run_not_finalized_or_bound") from error
        con = _connect(root)
        try:
            con.execute("BEGIN IMMEDIATE")
            pointer = con.execute("SELECT snapshot_id FROM accepted_pointer WHERE singleton=1").fetchone()
            current = pointer[0] if pointer else None
            existing = con.execute("SELECT * FROM snapshot_acceptances WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            if current == snapshot_id:
                catalog._require(existing is not None and existing["run_id"] == run_id and
                                 existing["catalog_sha256"] == expected_catalog_sha256 and
                                 existing["manifest_sha256"] == expected_manifest_sha256, "replay_binding_mismatch")
                receipt = _receipt(existing)
                con.commit()
                _sync_root(root)
                return {"reused": True, "receipt": receipt, "receipt_sha256": existing["receipt_sha256"]}
            catalog._require(current == expected_current_snapshot_id, "accepted_pointer_changed")
            catalog._require(existing is None, "superseded_snapshot_cannot_reactivate")
            overlays = {card["sha256"]: card["metadata"] for card in value["cards"] if card["metadata"]}
            fresh = catalog.build(database, private_scratch, overlays=overlays,
                                  since=value["arrivals"]["since"], run_id=run_id)
            catalog._require(catalog.encoded(fresh) == catalog.encoded(value), "stale_source_snapshot")
            receipt = {"schema_version": 1, "snapshot_id": snapshot_id, "run_id": run_id,
                       "catalog_sha256": expected_catalog_sha256, "manifest_sha256": expected_manifest_sha256,
                       "accepted_at": datetime.now(timezone.utc).isoformat(),
                       "previous_snapshot_id": current, "source_check": "consistent_capture_at_acceptance",
                       "catalog_snapshot_accepted": True, "stage_promotions": 0,
                       "document_review_approval": False, "publication_ready": False}
            raw = catalog.encoded(receipt)
            receipt_hash = catalog.digest(raw)
            con.execute("INSERT INTO snapshot_acceptances VALUES(?,?,?,?,?,?)",
                        (snapshot_id, run_id, expected_catalog_sha256, expected_manifest_sha256, receipt_hash, raw))
            con.execute("INSERT INTO snapshot_events(previous_snapshot_id,snapshot_id) VALUES(?,?)", (current, snapshot_id))
            con.execute("INSERT INTO accepted_pointer VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET snapshot_id=excluded.snapshot_id",
                        (snapshot_id,))
            con.commit()
            # SQLite FULL makes the transaction durable; persist first DB directory entry too.
            _sync_root(root)
            _after_commit()
            return {"reused": False, "receipt": receipt, "receipt_sha256": receipt_hash}
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()


def read_accepted(private_output):
    """Read and revalidate the current pointer and immutable artifact bundle."""
    with _locked(private_output) as root:
        path = root / "accepted-snapshots.sqlite"
        if not path.exists():
            return None
        catalog._safe_path(path)
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT a.* FROM snapshot_acceptances a JOIN accepted_pointer p ON a.snapshot_id=p.snapshot_id WHERE p.singleton=1").fetchone()
            if row is None:
                return None
            receipt = _receipt(row)
            _load(root, row["snapshot_id"], row["catalog_sha256"], row["manifest_sha256"])
            return {"receipt": receipt, "receipt_sha256": row["receipt_sha256"]}
