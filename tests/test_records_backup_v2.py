"""Entirely synthetic, offline acceptance tests; no production interfaces."""
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from campaign_tool.records.recovery import backup as v1
from campaign_tool.records.recovery import backup_v2 as v2

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def schema_hash(con):
    rows = [list(r) for r in con.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
    return v1.sha(v1.canonical(rows))

class RecoveryV2(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deflock-v2-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"; self.source.mkdir(mode=0o700)
        self.blobs = self.source / "blobs"; self.blobs.mkdir(mode=0o700)
        self.raw = b"SYNTHETIC ORIGINAL ONLY\n"
        self.sha = digest(self.raw)
        self.blob = self.blobs / self.sha
        self.blob.write_bytes(self.raw); self.blob.chmod(0o400)
        self.receipt = self.source / "receipt.json"
        self.receipt_raw = json.dumps({"source": str(self.blob), "sha256": self.sha,
                                      "scope": "synthetic", "status": "blocked"}).encode()
        self.receipt.write_bytes(self.receipt_raw); self.receipt.chmod(0o400)
        self.review = self.source / "review.json"
        self.review_raw = json.dumps({"historical_absolute_path": str(self.blob),
                                     "signature": "opaque-synthetic-envelope-no-signing-key",
                                     "review_set_sha256": digest(self.receipt_raw)}).encode()
        self.review.write_bytes(self.review_raw); self.review.chmod(0o400)
        self.secret = self.source / ".env"
        self.secret.write_bytes(b"SYNTHETIC_CREDENTIAL_CANARY"); self.secret.chmod(0o600)
        self.canonical = self.source / "canonical.sqlite"
        self.intake = self.source / "intake.sqlite"
        refs = [("original_blob", str(self.blob), self.sha),
                ("receipt", str(self.receipt), digest(self.receipt_raw)),
                ("review_bundle", str(self.review), digest(self.review_raw))]
        with closing(sqlite3.connect(self.canonical)) as con, con:
            con.executescript("""
              CREATE TABLE documents(sha TEXT PRIMARY KEY, blob_path TEXT NOT NULL);
              CREATE TABLE backup_refs(role TEXT, source_path TEXT, sha256 TEXT);
              CREATE TABLE mail_checkpoints(account TEXT, folder TEXT, uidvalidity INTEGER, highest_uid INTEGER);
              CREATE TABLE work_leases(item TEXT, state TEXT);
              CREATE TABLE legacy_checkpoints(source TEXT, last_row INTEGER);
            """)
            con.execute("INSERT INTO documents VALUES(?,?)", (self.sha, str(self.blob)))
            con.executemany("INSERT INTO backup_refs VALUES(?,?,?)", refs)
            con.execute("INSERT INTO mail_checkpoints VALUES('synthetic','folder',101,7)")
            con.execute("INSERT INTO work_leases VALUES('synthetic-item','in_progress')")
            con.execute("INSERT INTO legacy_checkpoints VALUES('synthetic-input',3)")
            canonical_schema = schema_hash(con)
        self.canonical.chmod(0o600)
        with closing(sqlite3.connect(self.intake)) as con, con:
            con.executescript("""
              CREATE TABLE occurrences(source TEXT PRIMARY KEY, sha TEXT NOT NULL);
              CREATE TABLE backup_refs(role TEXT, source_path TEXT, sha256 TEXT);
            """)
            # replay same source is idempotent, second source preserves occurrence.
            con.execute("INSERT OR IGNORE INTO occurrences VALUES('source-one',?)", (self.sha,))
            con.execute("INSERT OR IGNORE INTO occurrences VALUES('source-one',?)", (self.sha,))
            con.execute("INSERT OR IGNORE INTO occurrences VALUES('source-two',?)", (self.sha,))
            con.executemany("INSERT INTO backup_refs VALUES(?,?,?)", refs)
            intake_schema = schema_hash(con)
        self.intake.chmod(0o600)
        query = ("SELECT role,source_path,sha256 FROM backup_refs",)
        self.profile = v2.Profile((str(self.source),), (
            v2.Store("canonical", str(self.canonical), ("canonical_ledger","cursor"), "sqlite",
                     required_tables=("documents","backup_refs","mail_checkpoints","work_leases","legacy_checkpoints"),
                     schema_sha256=canonical_schema, reference_sql=query),
            v2.Store("intake", str(self.intake), ("intake_ledger",), "sqlite",
                     required_tables=("occurrences","backup_refs"), schema_sha256=intake_schema, reference_sql=query),
            v2.Store("original", str(self.blob), ("original_blob",), expected_sha256=self.sha),
            v2.Store("receipt", str(self.receipt), ("receipt",), expected_sha256=digest(self.receipt_raw)),
            v2.Store("review", str(self.review), ("review_bundle",), expected_sha256=digest(self.review_raw)),
            v2.Store("credential", str(self.secret), included=False, exclusion="credential"),
        ))
        self.q = v2.CallerQuiescence(self.profile.roots, datetime.now(timezone.utc).isoformat(), "e"*64,
                                    all_writers_stopped=True)

    def backup(self, profile=None, q=None, name="backup"):
        return v2.backup_v2(str(self.base/name), profile or self.profile, quiescence=q or self.q)

    def ready(self):
        report = self.backup()
        return self.base/"backup", report["manifest_sha256"]

    def changed(self, name, **changes):
        return replace(self.profile, stores=tuple(replace(s, **changes) if s.name == name else s for s in self.profile.stores))

    @contextmanager
    def retained_connections(self, *, factory=sqlite3.Connection, fail_connect_at=None):
        """Keep strong references and disable GC to expose missed explicit closes."""
        connect = sqlite3.connect
        connections = []
        calls = 0
        gc_enabled = gc.isenabled()
        gc.disable()
        def retained(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == fail_connect_at:
                raise sqlite3.OperationalError("synthetic connect failure")
            con = connect(*args, factory=factory, **kwargs)
            connections.append(con)
            return con
        try:
            with patch.object(sqlite3, "connect", retained):
                yield connections
        finally:
            for con in connections:
                con.close()
            if gc_enabled:
                gc.enable()

    def assert_connections_closed(self, connections):
        self.assertTrue(connections)
        for con in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                con.execute("SELECT 1")

    def quiescent_wal_profile(self):
        """A read-only survivor keeps committed WAL; every writer is closed."""
        with closing(sqlite3.connect(self.intake)) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
            writer.execute("INSERT INTO occurrences VALUES('wal-committed',?)", (self.sha,))
            writer.commit()
            reader = sqlite3.connect(self.intake.as_uri() + "?mode=ro", uri=True)
            self.addCleanup(reader.close)
            reader.execute("PRAGMA query_only=ON")
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM occurrences").fetchone(), (3,))
        extra = []
        for suffix in ("-wal", "-shm"):
            path = Path(str(self.intake) + suffix)
            self.assertTrue(path.is_file())
            path.chmod(0o600)
            extra.append(v2.Store("intake" + suffix, str(path), included=False, exclusion="sqlite_sidecar"))
        return replace(self.profile, stores=self.profile.stores + tuple(extra))

    def test_complete_two_database_roundtrip_and_explicit_synthetic_replay(self):
        source, pin = self.ready()
        report = v2.restore_v2(str(source), str(self.base/"restored"), manifest_sha256=pin)
        self.assertEqual(report["sqlite_snapshots"], 2)
        self.assertFalse(report["activation"]); self.assertEqual(report["sends"], 0); self.assertEqual(report["replay"], 0)
        restored = self.base/"restored"
        self.assertEqual((restored/"manifest.json").read_bytes(), (source/"manifest.json").read_bytes())
        self.assertEqual((restored/"payload/review").read_bytes(), self.review_raw)
        self.assertEqual((restored/"payload/receipt").read_bytes(), self.receipt_raw)
        self.assertEqual((restored/"payload/original").read_bytes(), self.raw)
        for name in ("canonical","intake","original","receipt","review"):
            self.assertEqual((restored/"payload"/name).stat().st_mode & 0o777, 0o400)
        with closing(sqlite3.connect((restored/"payload/canonical").as_uri()+"?mode=ro&immutable=1", uri=True)) as con:
            self.assertEqual(con.execute("SELECT highest_uid FROM mail_checkpoints").fetchone(), (7,))
            self.assertEqual(con.execute("SELECT state FROM work_leases").fetchone(), ("in_progress",))
            self.assertEqual(con.execute("SELECT last_row FROM legacy_checkpoints").fetchone(), (3,))
        # Runnable reconciliation is explicitly outside restore. This clone is synthetic only.
        clone = self.base/"synthetic-replay.sqlite"; shutil.copyfile(restored/"payload/intake", clone); clone.chmod(0o600)
        with closing(sqlite3.connect(clone)) as con, con:
            con.execute("INSERT OR IGNORE INTO occurrences VALUES('source-one',?)", (self.sha,))
            con.execute("INSERT OR IGNORE INTO occurrences VALUES('source-two',?)", (self.sha,))
            self.assertEqual(con.execute("SELECT COUNT(*),COUNT(DISTINCT sha) FROM occurrences").fetchone(), (2,1))
        self.assertNotIn(b"SYNTHETIC_CREDENTIAL_CANARY", b"".join(p.read_bytes() for p in (source/"payload").iterdir()))

    def resolution_profile(self, *, local=False):
        """Use the real S1 append/schema and canonical import fixtures, not fake SQL."""
        from campaign_tool.records.ledger import original_resolution as resolution
        from tests.records import test_original_reconciliation as local_fixtures
        from tests.records import test_original_resolution_mail as mail_fixtures
        case = (local_fixtures.OriginalReconciliationTests() if local else
                mail_fixtures.OriginalResolutionMailTests())
        case.setUp()
        self.addCleanup(case.doCleanups)
        output = case.backend.output if local else case.output
        policy = case.policy
        if not local:
            blob = output / "blobs" / case.sha
            blob.write_bytes(case.fixture.source.read_bytes()); blob.chmod(0o400)
            case.receipt_path.chmod(0o400)
            directory = output / "resolution-policies"; directory.mkdir(mode=0o700)
            policy_path = directory / policy.sha256
            policy_path.write_bytes(policy.raw); policy_path.chmod(0o400)
        case.enroll()
        path = self.source / "resolution.sqlite"
        v1.snapshot_ledger(case.database, path)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as con:
            self.assertTrue(resolution.installed(con))
            schema = schema_hash(con)
            tables = tuple(row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            row = con.execute("SELECT policy_sha256,capture_receipt_sha256 FROM original_resolutions").fetchone()
            self.assertEqual(row[0], policy.sha256)
        directories = (output / "blobs", output / "resolution-policies",
                       output / ("local-capture-receipts" if local else "verification-receipts"))
        stores = [v2.Store("resolution-ledger", str(path), ("canonical_ledger",), "sqlite",
                           required_tables=tables, schema_sha256=schema,
                           # Intentionally does not mention policy, CAS or receipt:
                           # fixed reader must enforce these independently.
                           reference_sql=("SELECT 'receipt','unused','unused' WHERE 0",))]
        for name, directory, key, role in (
                ("resolution-blob", directories[0], case.sha, "original_blob"),
                ("resolution-policy", directories[1], row[0], "resolution_policy"),
                ("resolution-receipt", directories[2], row[1], "receipt")):
            stores.append(v2.Store(name, str(directory / key), (role,), expected_sha256=key))
        self.profile = replace(self.profile, roots=self.profile.roots + tuple(map(str, directories)),
                               stores=self.profile.stores + tuple(stores))
        self.q = replace(self.q, roots=self.profile.roots)
        return policy, Path(stores[2].source)

    def rewrite_manifest(self, source, mutate):
        value = json.loads((source / "manifest.json").read_bytes())
        mutate(value)
        value["total_bytes"] = sum(row["bytes"] for row in value["files"])
        raw = v1.canonical(value)
        target = source / "manifest.json"; target.chmod(0o600)
        target.write_bytes(raw); target.chmod(0o400)
        return digest(raw)

    def assert_restore_refused(self, source, pin, reason, name="refused-restore"):
        before = v2._inventory(self.source)
        target = self.base / name
        with self.assertRaisesRegex(v1.RecoveryError, reason):
            v2.restore_v2(str(source), str(target), manifest_sha256=pin)
        self.assertFalse(target.exists(), "invalid closure must refuse before target creation")
        self.assertEqual(v2._inventory(self.source), before)

    def test_resolution_policy_roundtrip_mail_and_local_unchanged_pin(self):
        # Each variant gets its own complete synthetic fixture and fresh target.
        from campaign_tool.records.ledger import original_resolution as resolution
        from campaign_tool.records.ledger.migrations import original_resolution_v001 as migration
        for local in (False, True):
            with self.subTest(local=local):
                case = RecoveryV2("runTest"); case.setUp()
                self.addCleanup(case.doCleanups)
                policy, _ = case.resolution_profile(local=local)
                source, pin = case.ready()
                restored = case.base / "restored"
                result = v2.restore_v2(str(source), str(restored), manifest_sha256=pin)
                self.assertTrue(result["declared_scope_closed"])
                self.assertFalse(result["runnable_reconciliation_proven"])
                self.assertEqual((restored / "payload/resolution-policy").read_bytes(), policy.raw)
                for row in json.loads((source / "manifest.json").read_bytes())["files"]:
                    self.assertEqual((restored / row["path"]).read_bytes(), (source / row["path"]).read_bytes())
                    self.assertEqual((restored / row["path"]).stat().st_mode & 0o777, 0o400)
                with closing(sqlite3.connect((restored / "payload/resolution-ledger").as_uri() +
                                             "?mode=ro&immutable=1", uri=True)) as con:
                    self.assertTrue(resolution.installed(con))
                    self.assertEqual(con.execute("SELECT value FROM ledger_meta WHERE key=?", (resolution.KEY,)).fetchone(),
                                     (digest(migration.SQL.encode()),))

    def test_resolution_policy_configured_without_rows_and_no_policy_compatibility(self):
        # Configuration can precede the first resolution; an unreferenced but
        # explicitly pinned policy must still be included and roundtrip.
        directory = self.source / "resolution-policies"; directory.mkdir(mode=0o700)
        raw = b"synthetic configured policy"; key = digest(raw)
        path = directory / key; path.write_bytes(raw); path.chmod(0o400)
        profile = replace(self.profile, stores=self.profile.stores + (
            v2.Store("configured-policy", str(path), ("resolution_policy",), expected_sha256=key),))
        result = self.backup(profile=profile)
        self.assertTrue(v2.verify_v2(str(self.base / "backup"), manifest_sha256=result["manifest_sha256"])["declared_scope_closed"])
        v2.restore_v2(str(self.base / "backup"), str(self.base / "restored"), manifest_sha256=result["manifest_sha256"])
        self.assertEqual((self.base / "restored/payload/configured-policy").read_bytes(), raw)
        # The pre-existing no-policy roundtrip remains a separate positive control.

    def test_resolution_policy_source_missing_tampered_wrong_hash_links_and_permissions(self):
        for mode, reason in (("missing", "unknown_or_missing"), ("tampered", "expected_hash_mismatch"),
                             ("wrong-pin", "resolution_policy_store_invalid"),
                             ("symlink", "symlink_rejected"), ("parent-symlink", "backup_v2_refused"),
                             ("hardlink", "hardlink_rejected"), ("shared", "owner_only_permissions")):
            with self.subTest(mode=mode):
                case = RecoveryV2("runTest"); case.setUp(); self.addCleanup(case.doCleanups)
                _, path = case.resolution_profile()
                before = case.canonical.read_bytes()
                if mode == "missing": path.unlink()
                if mode == "tampered": path.chmod(0o600); path.write_bytes(b"tampered")
                if mode == "wrong-pin": case.profile = case.changed("resolution-policy", expected_sha256="f" * 64)
                if mode in ("symlink", "hardlink"):
                    other = case.base / "other-policy"; other.write_bytes(path.read_bytes()); other.chmod(0o400)
                    path.unlink()
                    if mode == "symlink": path.symlink_to(other)
                    else: os.link(other, path)
                if mode == "parent-symlink":
                    other = case.base / "policy-directory"; path.parent.rename(other)
                    path.parent.symlink_to(other, target_is_directory=True)
                if mode == "shared": path.chmod(0o644)
                with self.assertRaisesRegex(v1.RecoveryError, reason): case.backup()
                self.assertEqual(case.canonical.read_bytes(), before)
                self.assertFalse((case.base / "backup/manifest.json").exists())

    def test_resolution_policy_cannot_be_excluded_relabelled_or_unpinned(self):
        self.resolution_profile()
        for index, changes in enumerate((dict(included=False, roles=(), exclusion="rebuildable", expected_sha256=""),
                                        dict(roles=("configuration",)), dict(expected_sha256=""))):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(v1.RecoveryError, "resolution_policy_store_invalid"):
                    self.backup(profile=self.changed("resolution-policy", **changes), name="invalid-policy-" + str(index))
                self.assertFalse((self.base / ("invalid-policy-" + str(index))).exists())

    def test_policy_directory_cannot_be_excluded_without_resolution_rows(self):
        # Isolate the directory gate: no resolution row or closure dependency
        # can mask a missing check, and each attempted backup uses a new target.
        directory = self.source / "resolution-policies"; directory.mkdir(mode=0o700)
        raw = b"synthetic configured policy"; key = digest(raw)
        path = directory / key; path.write_bytes(raw); path.chmod(0o400)
        item = v2.Store("configured-policy", str(path), ("resolution_policy",), expected_sha256=key)
        for index, changes in enumerate((dict(included=False, roles=(), exclusion="rebuildable", expected_sha256=""),
                                        dict(roles=("configuration",)), dict(expected_sha256=""))):
            profile = replace(self.profile, stores=self.profile.stores + (replace(item, **changes),))
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(v1.RecoveryError, "resolution_policy_store_invalid"):
                    self.backup(profile=profile, name="configured-" + str(index))

    def test_fixed_resolution_reader_closes_omitted_policy_receipt_and_cas(self):
        self.resolution_profile()
        # Keep every file declared/included and all ordinary roles/closure valid;
        # only the resolution dependency role is removed (no allowlist masking).
        for name, role in (("resolution-blob", "configuration"), ("resolution-receipt", "configuration")):
            with self.subTest(name=name):
                with self.assertRaisesRegex(v1.RecoveryError, "database_reference_not_closed"):
                    self.backup(profile=self.changed(name, roles=(role,)), name=name)
        # Omit policy physically AND from profile: ordinary inventories are valid.
        policy = next(s for s in self.profile.stores if s.name == "resolution-policy")
        Path(policy.source).unlink()
        Path(policy.source).parent.rmdir()
        profile = replace(self.profile, roots=tuple(r for r in self.profile.roots if r != str(Path(policy.source).parent)),
                          stores=tuple(s for s in self.profile.stores if s != policy))
        with self.assertRaisesRegex(v1.RecoveryError, "database_reference_not_closed"):
            self.backup(profile=profile, q=replace(self.q, roots=profile.roots), name="omitted-policy")

    def test_resolution_backup_policy_corruption_refuses_restore_before_mutation(self):
        self.resolution_profile()
        source, pin = self.ready()
        path = source / "payload/resolution-policy"; raw = path.read_bytes()
        for mode, reason in (("missing", "unclassified_backup_member"), ("tampered", "member_hash_or_size_mismatch"),
                             ("symlink", "symlink_rejected"), ("shared", "owner_only_permissions")):
            with self.subTest(mode=mode):
                path.unlink(missing_ok=True)
                if mode == "symlink": path.symlink_to(self.receipt)
                elif mode != "missing":
                    path.write_bytes(b"changed" if mode == "tampered" else raw)
                    path.chmod(0o644 if mode == "shared" else 0o400)
                self.assert_restore_refused(source, pin, reason, name=mode)

    def test_resolution_resealed_manifest_omission_exclusion_wrong_path_and_role_refused(self):
        for mode, reason in (("omission", "database_reference_not_closed"),
                             ("exclusion", "resolution_policy_store_invalid"),
                             ("path", "database_reference_not_closed"),
                             ("role", "resolution_policy_store_invalid"),
                             ("wrong-hash", "resolution_policy_store_invalid")):
            with self.subTest(mode=mode):
                case = RecoveryV2("runTest"); case.setUp(); self.addCleanup(case.doCleanups)
                case.resolution_profile(); source, _ = case.ready()
                def mutate(m):
                    row = next(r for r in m["files"] if r["name"] == "resolution-policy")
                    if mode in ("omission", "exclusion"):
                        m["files"].remove(row); (source / row["path"]).unlink()
                        if mode == "exclusion":
                            m["excluded"].append(dict(name=row["name"], source=row["source"], reason="rebuildable"))
                    if mode == "path":
                        # Same digest/basename but another root: not row-bound.
                        row["source"] = str(case.source / "resolution-policies" / row["sha256"])
                    if mode == "role": row["roles"] = ["configuration"]
                    if mode == "wrong-hash": row["sha256"] = "f" * 64
                pin = case.rewrite_manifest(source, mutate)
                case.assert_restore_refused(source, pin, reason)

    def test_missing_quiescence_rejected(self):
        with self.assertRaises(v1.RecoveryError):
            self.backup(q=replace(self.q, all_writers_stopped=False))

    def test_root_lock_alone_rejected(self):
        with self.assertRaises(v1.RecoveryError):
            self.backup(q=replace(self.q, basis="run_root_lock_only"))

    def test_quiescence_wrong_scope_and_expiry_rejected(self):
        for q in (replace(self.q, roots=()), replace(self.q, observed_utc=(datetime.now(timezone.utc)-timedelta(seconds=121)).isoformat()),
                  replace(self.q, evidence_sha256="not-a-hash")):
            with self.assertRaises(v1.RecoveryError):
                self.backup(q=q)

    def test_unknown_store_fails_closed(self):
        (self.source/"unclassified.sqlite").write_bytes(b"unknown"); (self.source/"unclassified.sqlite").chmod(0o600)
        with self.assertRaisesRegex(v1.RecoveryError, "unknown_or_missing"):
            self.backup()

    def test_unknown_empty_store_directory_fails_closed(self):
        (self.source/"unknown-store").mkdir(mode=0o700)
        with self.assertRaises(v1.RecoveryError):
            self.backup()

    def test_duplicate_and_missing_roles_rejected(self):
        for p in (replace(self.profile, stores=self.profile.stores+(self.profile.stores[0],)),
                  self.changed("intake", roles=("configuration",))):
            with self.assertRaises(v1.RecoveryError):
                self.backup(profile=p)

    def test_two_independent_databases_required(self):
        with self.assertRaises(v1.RecoveryError):
            self.backup(profile=self.changed("intake", kind="file", required_tables=(), schema_sha256="", reference_sql=()))

    def test_credential_inclusion_forbidden(self):
        with self.assertRaises(v1.RecoveryError):
            self.backup(profile=self.changed("credential", included=True, roles=("configuration",), exclusion=""))

    def test_source_file_permissions_rejected_without_repair(self):
        self.receipt.chmod(0o644)
        with self.assertRaises(v1.RecoveryError):
            self.backup()
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o644)

    def test_source_directory_permissions_rejected(self):
        self.blobs.chmod(0o755)
        with self.assertRaises(v1.RecoveryError):
            self.backup()

    def test_source_symlink_rejected(self):
        link = self.source/"linked"; link.symlink_to(self.blob)
        with self.assertRaises(v1.RecoveryError):
            self.backup()

    def test_source_hardlink_rejected(self):
        os.link(self.blob, self.source/"hardlinked")
        with self.assertRaises(v1.RecoveryError):
            self.backup()

    def test_symlink_ancestor_rejected(self):
        link = self.base/"alias"; link.symlink_to(self.source, target_is_directory=True)
        p = replace(self.profile, roots=(str(link),), stores=tuple(
            replace(s, source=str(link/Path(s.source).relative_to(self.source))) for s in self.profile.stores))
        with self.assertRaises(v1.RecoveryError):
            self.backup(profile=p, q=replace(self.q, roots=p.roots))

    def test_corrupt_second_database_rejected(self):
        self.intake.write_bytes(b"not SQLite")
        with self.assertRaises(v1.RecoveryError):
            self.backup()
        self.assertFalse((self.base/"backup/manifest.json").exists())

    def test_schema_identity_mismatch_rejected(self):
        with self.assertRaisesRegex(v1.RecoveryError, "schema_identity"):
            self.backup(profile=self.changed("intake", schema_sha256="f"*64))

    def test_foreign_key_integrity_checked_independently(self):
        with closing(sqlite3.connect(self.intake)) as con, con:
            con.executescript("CREATE TABLE parent(id INTEGER PRIMARY KEY); CREATE TABLE child(id INTEGER REFERENCES parent(id));")
            con.execute("INSERT INTO child VALUES(99)")
            schema = schema_hash(con)
        with self.assertRaisesRegex(v1.RecoveryError, "foreign_key"):
            self.backup(profile=self.changed("intake", schema_sha256=schema))

    def test_database_reference_omission_rejected(self):
        with closing(sqlite3.connect(self.intake)) as con, con:
            con.execute("INSERT INTO backup_refs VALUES('receipt',? ,?)", (str(self.source/"missing.json"), "a"*64))
        with self.assertRaisesRegex(v1.RecoveryError, "reference_not_closed"):
            self.backup()

    def test_declared_expected_receipt_hash_required(self):
        with self.assertRaises(v1.RecoveryError):
            self.backup(profile=self.changed("receipt", expected_sha256=""))

    def test_review_and_receipt_hash_mismatch_rejected(self):
        with self.assertRaises(v1.RecoveryError):
            self.backup(profile=self.changed("review", expected_sha256="f"*64))

    def test_original_content_address_required(self):
        self.blob.chmod(0o600); self.blob.write_bytes(b"altered synthetic original")
        with self.assertRaises(v1.RecoveryError):
            self.backup()

    def test_committed_wal_included_uncommitted_wal_excluded(self):
        con = sqlite3.connect(self.intake)
        self.addCleanup(con.close)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("INSERT INTO occurrences VALUES('wal-committed',?)", (self.sha,)); con.commit()
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO occurrences VALUES('wal-uncommitted',?)", (self.sha,))
        extra = []
        for suffix in ("-wal","-shm"):
            p = Path(str(self.intake)+suffix)
            if p.exists():
                p.chmod(0o600)
                extra.append(v2.Store("intake"+suffix, str(p), included=False, exclusion="sqlite_sidecar"))
        # Uncommitted writer is not actually quiescent: this is an explicit snapshot
        # mechanics test with a synthetic attestation, NOT proof of the caller boundary.
        profile = replace(self.profile, stores=self.profile.stores+tuple(extra))
        result = self.backup(profile=profile)
        with closing(sqlite3.connect((self.base/"backup/payload/intake").as_uri()+"?mode=ro&immutable=1", uri=True)) as snap:
            self.assertEqual(snap.execute("SELECT COUNT(*) FROM occurrences").fetchone(), (3,))
        self.assertFalse(result["live_pipeline_recovery_proven"])
        con.rollback()

    def test_quiescent_wal_verifies_and_restores_without_connection_finalization(self):
        profile = self.quiescent_wal_profile()
        source_before = v2._inventory(self.source)
        with self.retained_connections() as connections:
            result = self.backup(profile=profile)
            source = self.base / "backup"
            pin = result["manifest_sha256"]
            self.assertTrue(v2.verify_v2(str(source), manifest_sha256=pin)["declared_scope_closed"])
            restored = self.base / "restored"
            self.assertTrue(v2.restore_v2(str(source), str(restored), manifest_sha256=pin)["isolated"])
            manifest = json.loads((source / "manifest.json").read_bytes())
            for root in (source, restored):
                self.assertEqual({p.name for p in (root / "payload").iterdir()},
                                 {row["name"] for row in manifest["files"]})
                for row in manifest["files"]:
                    raw = (root / row["path"]).read_bytes()
                    self.assertEqual((digest(raw), len(raw)), (row["sha256"], row["bytes"]))
                    if row["name"] == "intake":
                        self.assertEqual(raw[18:20], b"\x01\x01")  # SQLite rollback-journal header.
                with closing(sqlite3.connect((root / "payload/intake").as_uri() +
                                             "?mode=ro&immutable=1", uri=True)) as con:
                    self.assertEqual(con.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                    self.assertEqual(con.execute("PRAGMA journal_mode").fetchone(), ("delete",))
                    self.assertEqual(con.execute("SELECT source FROM occurrences ORDER BY source").fetchall(),
                                     [("source-one",), ("source-two",), ("wal-committed",)])
            self.assert_connections_closed(connections)
        self.assertEqual(v2._inventory(self.source), source_before)
        with closing(sqlite3.connect(self.intake.as_uri() + "?mode=ro", uri=True)) as con:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone(), ("wal",))

    def test_v1_wal_snapshot_is_standalone_without_connection_finalization(self):
        self.quiescent_wal_profile()
        target = self.base / "snapshot.sqlite"
        with self.retained_connections() as connections:
            result = v1.snapshot_ledger(self.intake, target)
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(str(target) + suffix).exists())
            raw = target.read_bytes()
            self.assertEqual(result, {"sha256": digest(raw), "bytes": len(raw)})
            self.assertEqual(raw[18:20], b"\x01\x01")
            with closing(sqlite3.connect(target.as_uri() + "?mode=ro&immutable=1", uri=True)) as con:
                self.assertEqual(con.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                self.assertEqual(con.execute("PRAGMA journal_mode").fetchone(), ("delete",))
                self.assertEqual(con.execute("SELECT COUNT(*) FROM occurrences").fetchone(), (3,))
            self.assert_connections_closed(connections)

    def test_snapshot_connections_close_on_query_connect_and_backup_errors(self):
        for method in ("v1", "v2"):
            for failure in ("query", "connect", "backup"):
                with self.subTest(method=method, failure=failure):
                    class FailingConnection(sqlite3.Connection):
                        def execute(self, sql, *args, **kwargs):
                            if failure == "query" and sql == "PRAGMA query_only=ON":
                                raise sqlite3.OperationalError("synthetic query failure")
                            return super().execute(sql, *args, **kwargs)

                        def backup(self, *args, **kwargs):
                            if failure == "backup":
                                raise sqlite3.OperationalError("synthetic backup failure")
                            return super().backup(*args, **kwargs)

                    target = self.base / (method + "-" + failure + ".sqlite")
                    with self.retained_connections(factory=FailingConnection,
                                                   fail_connect_at=2 if failure == "connect" else None) as connections:
                        with self.assertRaisesRegex(sqlite3.OperationalError, "synthetic"):
                            if method == "v1":
                                v1.snapshot_ledger(self.intake, target)
                            else:
                                v2._snapshot_quiescent(self.intake, target, time.monotonic() + 60)
                        self.assert_connections_closed(connections)

    def test_database_validation_failure_closes_connections_without_finalization(self):
        with self.retained_connections() as connections:
            with self.assertRaisesRegex(v1.RecoveryError, "schema_identity"):
                self.backup(profile=self.changed("intake", schema_sha256="f" * 64))
            self.assert_connections_closed(connections)
            self.assertFalse((self.base / "backup/manifest.json").exists())

    def test_wal_normalization_and_integrity_failures_close_connections_without_manifest(self):
        profile = self.quiescent_wal_profile()
        for failure, reason in (("normalization", "ledger_snapshot_journal_mode"),
                                ("integrity", "backup_v2_refused")):
            with self.subTest(failure=failure):
                class FailingCheckConnection(sqlite3.Connection):
                    def execute(self, sql, *args, **kwargs):
                        if failure == "normalization" and sql == "PRAGMA journal_mode=DELETE":
                            return super().execute("PRAGMA journal_mode")
                        cursor = super().execute(sql, *args, **kwargs)
                        if failure == "integrity" and sql == "PRAGMA quick_check":
                            cursor.close()
                            raise sqlite3.OperationalError("synthetic integrity query failure")
                        return cursor

                with self.retained_connections(factory=FailingCheckConnection) as connections:
                    with self.assertRaisesRegex(v1.RecoveryError, reason):
                        self.backup(profile=profile, name=failure)
                    self.assert_connections_closed(connections)
                    self.assertFalse((self.base / failure / "manifest.json").exists())

    def test_leftover_snapshot_sidecars_refuse_completion_without_deleting_files(self):
        for mode in ("immutable", "wal"):
            profile = self.quiescent_wal_profile() if mode == "wal" else self.profile
            source_before = v2._inventory(self.source)
            for suffix in ("-wal", "-shm", "-journal"):
                with self.subTest(mode=mode, suffix=suffix):
                    name = "leftover-" + mode + suffix
                    target = self.base / name / "payload" / ("intake" if mode == "wal" else "canonical")
                    sidecar = Path(str(target) + suffix)
                    raw = b"SYNTHETIC LEFTOVER SIDECAR ONLY"

                    class LeftoverConnection(sqlite3.Connection):
                        def __init__(self, database, *args, **kwargs):
                            super().__init__(database, *args, **kwargs)
                            self.is_target = database == target
                            self.injected = False

                        def close(self):
                            super().close()
                            if self.is_target and not self.injected:
                                sidecar.write_bytes(raw)
                                self.injected = True

                    with self.retained_connections(factory=LeftoverConnection) as connections:
                        with self.assertRaisesRegex(v1.RecoveryError, "ledger_snapshot_sidecar_remaining"):
                            self.backup(profile=profile, name=name)
                        self.assert_connections_closed(connections)
                        self.assertFalse((self.base / name / "manifest.json").exists())
                        self.assertEqual(sidecar.read_bytes(), raw)
                        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(v2._inventory(self.source), source_before)

    def test_closed_quiescent_wal_without_sidecars_is_snapshotted(self):
        for path in (self.canonical, self.intake):
            con = sqlite3.connect(path)
            try:
                self.assertEqual(con.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
            finally:
                con.close()
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(str(path) + suffix).exists())
        source, pin = self.ready()
        self.assertEqual(v2.verify_v2(str(source), manifest_sha256=pin)["sqlite_snapshots"], 2)
        for path in (self.canonical, self.intake):
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(str(path) + suffix).exists())

    def test_unpaired_wal_exclusion_rejected(self):
        p = self.source/"unpaired-wal"; p.write_bytes(b"synthetic"); p.chmod(0o600)
        profile = replace(self.profile, stores=self.profile.stores+(v2.Store("unpaired", str(p), included=False, exclusion="sqlite_sidecar"),))
        with self.assertRaises(v1.RecoveryError):
            self.backup(profile=profile)

    def test_source_change_prevents_completion_manifest(self):
        real_copy = v2.copy_exact
        def changing(source, destination, **kw):
            result = real_copy(source, destination, **kw)
            if Path(source) == self.receipt:
                self.secret.write_bytes(b"changed synthetic credential only")
            return result
        with patch.object(v2, "copy_exact", changing):
            with self.assertRaisesRegex(v1.RecoveryError, "scope_changed"):
                self.backup()
        self.assertFalse((self.base/"backup/manifest.json").exists())

    def test_backup_and_restore_no_overwrite_even_empty_target(self):
        self.backup()
        with self.assertRaises(v1.RecoveryError):
            self.backup()
        target = self.base/"existing"; target.mkdir(mode=0o700)
        source = self.base/"backup"; pin = digest((source/"manifest.json").read_bytes())
        with self.assertRaises(v1.RecoveryError):
            v2.restore_v2(str(source), str(target), manifest_sha256=pin)
        self.assertEqual(list(target.iterdir()), [])

    def test_restore_nonempty_symlink_and_nested_target_rejected(self):
        source, pin = self.ready()
        existing = self.base/"existing"; existing.mkdir(mode=0o700); (existing/"keep").write_bytes(b"keep")
        link = self.base/"link"; link.symlink_to(existing, target_is_directory=True)
        for target in (existing, link, source/"nested"):
            with self.assertRaises(v1.RecoveryError):
                v2.restore_v2(str(source), str(target), manifest_sha256=pin)
        self.assertEqual((existing/"keep").read_bytes(), b"keep")

    def test_final_backup_copy_deadline_cannot_return_success(self):
        real_copy = v2.copy_exact
        clock = [0.0]
        final_payload_copied = []
        def late_final_copy(source, destination, **kw):
            result = real_copy(source, destination, **kw)
            if Path(source) == self.review:
                final_payload_copied.append(Path(destination))
                clock[0] = v2.OPERATION_SECONDS + 1
            return result
        target = self.base / "backup"
        with patch.object(v2.time, "monotonic", lambda: clock[0]), patch.object(v2, "copy_exact", late_final_copy):
            with self.assertRaisesRegex(v1.RecoveryError, "operation_timeout"):
                self.backup()
        self.assertEqual(clock[0], v2.OPERATION_SECONDS + 1)
        self.assertEqual(final_payload_copied, [target / "payload" / "review"])
        self.assertTrue((target / "payload" / "review").exists())
        self.assertFalse((target / "manifest.json").exists())
        self.assertEqual(self.review.read_bytes(), self.review_raw)

    def test_restore_copy_elapsed_deadline_cannot_return_success(self):
        source, pin = self.ready()
        real_copy = v2.copy_exact
        clock = [1000.0]
        def late_copy(*args, **kw):
            result = real_copy(*args, **kw)
            clock[0] += v2.OPERATION_SECONDS + 1
            return result
        target = self.base / "late-restore"
        with patch.object(v2.time, "monotonic", lambda: clock[0]), patch.object(v2, "copy_exact", late_copy):
            with self.assertRaisesRegex(v1.RecoveryError, "operation_timeout"):
                v2.restore_v2(str(source), str(target), manifest_sha256=pin)
        self.assertFalse((target / "manifest.json").exists())
        self.assertEqual(digest((source / "manifest.json").read_bytes()), pin)

    def test_restore_inside_original_profile_root_rejected(self):
        source, pin = self.ready()
        target = self.source / "restored"
        with self.assertRaisesRegex(v1.RecoveryError, "destination_must_be_isolated"):
            v2.restore_v2(str(source), str(target), manifest_sha256=pin)
        self.assertFalse(target.exists())
        self.assertEqual(digest((source / "manifest.json").read_bytes()), pin)

    def test_exact_trusted_manifest_digest_required(self):
        source, pin = self.ready()
        for wrong in ("", "A"*64, "0"*64):
            with self.assertRaises(v1.RecoveryError):
                v2.verify_v2(str(source), manifest_sha256=wrong)

    def test_extra_archive_member_and_unsafe_permissions_rejected(self):
        source, pin = self.ready()
        extra = source/"payload/extra"; extra.write_bytes(b"extra"); extra.chmod(0o400)
        with self.assertRaises(v1.RecoveryError):
            v2.verify_v2(str(source), manifest_sha256=pin)
        extra.unlink()
        (source/"payload/review").chmod(0o644)
        with self.assertRaises(v1.RecoveryError):
            v2.verify_v2(str(source), manifest_sha256=pin)

    def test_corrupt_archive_blob_rejected(self):
        source, pin = self.ready()
        blob = source/"payload/original"; blob.chmod(0o600); blob.write_bytes(b"corruption")
        with self.assertRaises(v1.RecoveryError):
            v2.verify_v2(str(source), manifest_sha256=pin)

    def test_manifest_omission_duplicate_and_traversal_rejected_with_new_pin(self):
        source, old_pin = self.ready()
        original = json.loads((source/"manifest.json").read_bytes())
        for mutate in ("omission","duplicate","traversal"):
            m = json.loads(json.dumps(original))
            if mutate == "omission":
                m["files"].pop()
            elif mutate == "duplicate":
                m["files"].append(m["files"][0])
            else:
                m["files"][0]["path"] = "../../escape"
            raw = v1.canonical(m); manifest = source/"manifest.json"; manifest.chmod(0o600); manifest.write_bytes(raw); manifest.chmod(0o400)
            with self.assertRaises(v1.RecoveryError):
                v2.verify_v2(str(source), manifest_sha256=digest(raw))

    def test_duplicate_json_manifest_keys_rejected(self):
        source, pin = self.ready()
        raw = (source/"manifest.json").read_bytes()
        raw = raw.replace(b'"schema":"records-backup-v2"', b'"schema":"records-backup-v2","schema":"records-backup-v2"')
        m = source/"manifest.json"; m.chmod(0o600); m.write_bytes(raw); m.chmod(0o400)
        with self.assertRaises(v1.RecoveryError):
            v2.verify_v2(str(source), manifest_sha256=digest(raw))

    def test_interrupted_restore_never_returns_success(self):
        source, pin = self.ready()
        def interrupted(*args, **kw):
            raise OSError("synthetic interruption only")
        with patch.object(v2, "copy_exact", interrupted):
            with self.assertRaises(v1.RecoveryError):
                v2.restore_v2(str(source), str(self.base/"interrupted"), manifest_sha256=pin)
        self.assertFalse((self.base/"interrupted/manifest.json").exists())
        self.assertEqual((source/"manifest.json").read_bytes().__class__, bytes)

    def test_v1_selected_scope_remains_one_ledger_and_roundtrips(self):
        entries = [
            {"category":"ledger","name":"ledger.sqlite","source":str(self.canonical)},
            {"category":"originals","name":"original","source":str(self.blob)},
            {"category":"rules","name":"synthetic-rule","source":str(self.receipt)},
            {"category":"receipts","name":"synthetic-receipt","source":str(self.review)},
        ]
        result = v1.backup(str(self.base/"v1"), entries)
        self.assertEqual(result["scope"], "explicit_selected_files")
        restored = v1.restore(str(self.base/"v1"), str(self.base/"v1-restored"), manifest_sha256=result["manifest_sha256"])
        self.assertEqual(restored["scope"], "explicit_selected_files")
        self.assertFalse(restored["activation"])
        entries.append({"category":"ledger","name":"second.sqlite","source":str(self.intake)})
        with self.assertRaises(v1.RecoveryError):
            v1.backup(str(self.base/"v1-two"), entries)

if __name__ == "__main__":
    # Prevent any accidentally introduced Python socket call in this test process.
    with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
        unittest.main(verbosity=2)

