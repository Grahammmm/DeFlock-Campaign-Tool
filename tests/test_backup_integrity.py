"""Campaign archive closure regressions, using generated SQLite and bytes only."""
import hashlib
import gc
import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from campaign_tool import backup
from tests.test_backup import FakeWorkspace


class BackupIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-backup-closure-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "campaign"
        self.root.mkdir()
        (self.root / "campaign.json").write_text('{"schema_version": 1}')
        self.sha = self.add_original(1)
        self.out = self.base / "backup.tar"

    def add_original(self, number):
        data = f"Synthetic original {number}\n".encode()
        sha = hashlib.sha256(data).hexdigest()
        objects = self.root / "private" / "objects"
        objects.mkdir(parents=True, exist_ok=True)
        (objects / sha).write_bytes(data)
        db = sqlite3.connect(self.root / "private" / "ledger.sqlite")
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS objects(sha256 TEXT PRIMARY KEY, byte_count INTEGER, stored_at TEXT);
                CREATE TABLE IF NOT EXISTS receipts(receipt_id TEXT PRIMARY KEY, object_sha256 TEXT,
                    source_id TEXT, received_at TEXT, original_name TEXT);
            """)
            db.execute("INSERT INTO objects VALUES(?,?,?)", (sha, len(data), "synthetic"))
            db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)", (str(number), sha, "fixture", "synthetic", "sample.txt"))
            db.commit()
        finally:
            db.close()
        return sha

    def mutate_ledger(self, sql, args=()):
        db = sqlite3.connect(self.root / "private" / "ledger.sqlite")
        try:
            db.execute(sql, args)
            db.commit()
        finally:
            db.close()

    def rewrite(self, *, drop=None, manifest_change=None, extra=None, replacements=None, raw_manifest=None):
        replacements = replacements or {}
        target = self.base / "rewritten.tar"
        with tarfile.open(self.out) as src, tarfile.open(target, "w") as dst:
            for member in src.getmembers():
                if member.name == drop:
                    continue
                data = src.extractfile(member).read()
                data = replacements.get(member.name, data)
                if member.name == backup.MANIFEST:
                    manifest = json.loads(data)
                    if drop:
                        manifest["files"].pop(drop)
                    for name, replacement in replacements.items():
                        manifest["files"][name] = {"sha256": hashlib.sha256(replacement).hexdigest(), "bytes": len(replacement)}
                    manifest["counts"]["files"] = len(manifest["files"])
                    manifest["counts"]["bytes"] = sum(e["bytes"] for e in manifest["files"].values())
                    if manifest_change:
                        manifest_change(manifest)
                    data = json.dumps(manifest).encode()
                    if raw_manifest:
                        data = raw_manifest(data)
                info = tarfile.TarInfo(member.name)
                info.size = len(data)
                dst.addfile(info, io.BytesIO(data))
            if extra:
                info = tarfile.TarInfo(extra[0])
                info.size = len(extra[1])
                dst.addfile(info, io.BytesIO(extra[1]))
        return target

    def test_missing_original_refuses_export(self):
        (self.root / "private" / "objects" / self.sha).unlink()
        with self.assertRaisesRegex(backup.BackupError, "missing_object"):
            backup.export(self.root, self.out)
        self.assertFalse(self.out.exists())

    def test_snapshot_precedes_enumeration(self):
        original_members = backup._members

        def interleaved_members(root):
            members = original_members(root)
            self.add_original(2)
            return members

        with patch.object(backup, "_members", interleaved_members):
            manifest = backup.export(self.root, self.out)
        self.assertEqual(manifest["counts"]["objects"], 1)
        self.assertTrue(backup.verify(self.out, manifest["manifest_sha256"])["ok"])
        target = self.base / "restored"
        backup.restore(self.out, target)
        db = sqlite3.connect(target / "private" / "ledger.sqlite")
        try:
            refs = list(db.execute("SELECT sha256 FROM objects"))
        finally:
            db.close()
        self.assertTrue(all((target / "private" / "objects" / sha).is_file() for sha, in refs))

    def test_ingest_before_snapshot_is_included(self):
        snapshot = backup._snapshot_ledger

        def interleaved_snapshot(ledger, tmp):
            self.add_original(2)
            return snapshot(ledger, tmp)

        with patch.object(backup, "_snapshot_ledger", interleaved_snapshot):
            manifest = backup.export(self.root, self.out)
        self.assertEqual(manifest["counts"]["objects"], 2)
        self.assertTrue(backup.verify(self.out)["ok"])

    def test_wal_snapshot_connections_close_before_hashing_without_gc(self):
        ledger = self.root / "private" / "ledger.sqlite"
        writer = sqlite3.connect(ledger)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE objects SET stored_at='committed'")
        writer.commit()
        reader = sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)
        reader.execute("SELECT * FROM objects").fetchall()
        writer.close()
        self.addCleanup(reader.close)
        self.assertTrue(Path(str(ledger) + "-wal").exists())
        connect, sha256 = sqlite3.connect, backup._sha256
        connections = []

        def retained_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connections.append(connection)
            return connection

        def checked_hash(path):
            if Path(path).name == "ledger.sqlite":
                for connection in connections:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        connection.execute("SELECT 1")
                self.assertFalse(Path(str(path) + "-wal").exists())
                self.assertFalse(Path(str(path) + "-shm").exists())
            return sha256(path)

        was_enabled = gc.isenabled()
        gc.disable()
        try:
            with patch.object(backup.sqlite3, "connect", retained_connect), patch.object(backup, "_sha256", checked_hash):
                manifest = backup.export(self.root, self.out)
                backup.verify(self.out, manifest["manifest_sha256"])
                backup.restore(self.out, self.base / "restored")
            for connection in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
        finally:
            if was_enabled:
                gc.enable()

    def test_initialized_campaign_without_ledger_still_round_trips(self):
        empty = self.base / "empty"
        empty.mkdir()
        (empty / "campaign.json").write_text("{}")
        result = backup.export(empty, self.out)
        self.assertEqual(result["counts"]["objects"], 0)
        self.assertTrue(backup.restore(self.out, self.base / "restored")["ok"])

    def test_missing_original_in_self_consistent_manifest_refuses_verify_and_restore(self):
        backup.export(self.root, self.out)
        broken = self.rewrite(drop="private/objects/" + self.sha)
        with self.assertRaisesRegex(backup.BackupError, "missing_object"):
            backup.verify(broken)
        target = self.base / "never-restored"
        with self.assertRaisesRegex(backup.BackupError, "missing_object"):
            backup.restore(broken, target)
        self.assertFalse(target.exists())

    def test_metadata_length_disagreement_refuses_export(self):
        self.mutate_ledger("UPDATE objects SET byte_count=byte_count+1")
        with self.assertRaisesRegex(backup.BackupError, "object_size_mismatch"):
            backup.export(self.root, self.out)
        self.assertFalse(self.out.exists())

    def test_verify_checks_ledger_length_and_readability(self):
        backup.export(self.root, self.out)
        self.mutate_ledger("UPDATE objects SET byte_count=byte_count+1")
        ledger = "private/ledger.sqlite"
        broken = self.rewrite(replacements={ledger: (self.root / ledger).read_bytes()})
        with self.assertRaisesRegex(backup.BackupError, "object_size_mismatch"):
            backup.verify(broken)
        broken = self.rewrite(replacements={ledger: b"not a database"})
        with self.assertRaisesRegex(backup.BackupError, "invalid_ledger"):
            backup.verify(broken)

    def test_invalid_local_object_metadata_refuses_export(self):
        for value in (-1, "bad", None):
            with self.subTest(byte_count=value):
                self.mutate_ledger("UPDATE objects SET byte_count=?", (value,))
                with self.assertRaisesRegex(backup.BackupError, "invalid_object_metadata"):
                    backup.export(self.root, self.out)

    def test_dangling_receipt_refuses_export(self):
        self.mutate_ledger("UPDATE receipts SET object_sha256=?", ("a" * 64,))
        with self.assertRaisesRegex(backup.BackupError, "dangling_object_reference"):
            backup.export(self.root, self.out)

    def test_unsupported_ledger_refuses_export(self):
        self.mutate_ledger("DROP TABLE receipts")
        with self.assertRaisesRegex(backup.BackupError, "invalid_ledger"):
            backup.export(self.root, self.out)

    def test_unreadable_ledger_refuses_export(self):
        (self.root / "private" / "ledger.sqlite").write_bytes(b"not sqlite")
        with self.assertRaisesRegex(backup.BackupError, "invalid_ledger"):
            backup.export(self.root, self.out)

    def test_duplicate_archive_member_refuses_verify(self):
        backup.export(self.root, self.out)
        duplicate = self.rewrite(extra=("private/objects/" + self.sha, (self.root / "private" / "objects" / self.sha).read_bytes()))
        with self.assertRaisesRegex(backup.BackupError, "duplicate_member"):
            backup.verify(duplicate)
        duplicate = self.rewrite(extra=(backup.MANIFEST, b"{}"))
        with self.assertRaisesRegex(backup.BackupError, "duplicate_member"):
            backup.verify(duplicate)

    def test_duplicate_manifest_json_key_refuses_verify(self):
        backup.export(self.root, self.out)
        broken = self.rewrite(raw_manifest=lambda data: data.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1'))
        with self.assertRaisesRegex(backup.BackupError, "duplicate_json_key"):
            backup.verify(broken)

    def test_malformed_manifest_entry_refuses_cleanly(self):
        backup.export(self.root, self.out)
        broken = self.rewrite(manifest_change=lambda m: m["files"].__setitem__("campaign.json", []))
        with self.assertRaisesRegex(backup.BackupError, "invalid_manifest"):
            backup.verify(broken)

    def test_manifest_counts_are_checked(self):
        backup.export(self.root, self.out)
        broken = self.rewrite(manifest_change=lambda m: m["counts"].__setitem__("objects", 19))
        with self.assertRaisesRegex(backup.BackupError, "count_mismatch"):
            backup.verify(broken)

    def test_member_limits_are_enforced_on_verify(self):
        backup.export(self.root, self.out)
        with patch.object(backup, "MAX_MEMBERS", 2), self.assertRaisesRegex(backup.BackupError, "member_limit_exceeded"):
            backup.verify(self.out)
        with patch.object(backup, "MAX_MEMBER_BYTES", 10), self.assertRaises(backup.BackupError):
            backup.verify(self.out)

    def test_restore_rechecks_archive_replaced_after_verification(self):
        backup.export(self.root, self.out)
        sentinel = self.base / "escape.txt"
        sentinel.write_bytes(b"keep synthetic sentinel")
        real_verify = backup.verify
        with tarfile.open(self.out) as tar:
            manifest_bytes = tar.extractfile(backup.MANIFEST).read()

        def replace_after_verify(path):
            report = real_verify(path)
            self.rewrite(extra=("../escape.txt", b"changed"),
                         raw_manifest=lambda data: manifest_bytes).replace(self.out)
            return report

        target = self.base / "never-restored"
        with patch.object(backup, "verify", replace_after_verify):
            with self.assertRaisesRegex(backup.BackupError, "unsafe_member_name"):
                backup.restore(self.out, target)
        self.assertFalse(target.exists())
        self.assertEqual(sentinel.read_bytes(), b"keep synthetic sentinel")

    def test_hosted_byte_count_must_match(self):
        data = b"synthetic hosted"
        client = FakeWorkspace({hashlib.sha256(data).hexdigest(): data})
        client.export["rows"]["original"][0]["byte_count"] += 1
        with self.assertRaisesRegex(backup.BackupError, "object_size_mismatch"):
            backup.export_hosted(client, self.out)
        self.assertFalse(self.out.exists())

    def test_hosted_receipt_requires_original_metadata(self):
        data = b"synthetic hosted"
        client = FakeWorkspace({hashlib.sha256(data).hexdigest(): data})
        client.export["rows"]["original"] = []
        with self.assertRaisesRegex(backup.BackupError, "dangling_object_reference"):
            backup.export_hosted(client, self.out)

    def test_hosted_verify_checks_archived_metadata(self):
        data = b"synthetic hosted"
        sha = hashlib.sha256(data).hexdigest()
        backup.export_hosted(FakeWorkspace({sha: data}), self.out)
        broken = self.rewrite(drop="private/objects/" + sha,
                              manifest_change=lambda m: m["source"].__setitem__("kind", "local"))
        with self.assertRaisesRegex(backup.BackupError, "missing_object"):
            backup.verify(broken)

    def test_hosted_duplicate_original_and_invalid_sizes_refuse_export(self):
        data = b"synthetic hosted"
        client = FakeWorkspace({hashlib.sha256(data).hexdigest(): data})
        row = client.export["rows"]["original"][0]
        client.export["rows"]["original"].append(dict(row))
        with self.assertRaisesRegex(backup.BackupError, "duplicate_object_metadata"):
            backup.export_hosted(client, self.out)
        client.export["rows"]["original"].pop()
        for size in (True, -1, "17", None):
            with self.subTest(byte_count=size):
                row["byte_count"] = size
                with self.assertRaisesRegex(backup.BackupError, "invalid_object_metadata"):
                    backup.export_hosted(client, self.out)

    def test_hosted_direct_original_references_are_checked(self):
        data = b"synthetic hosted"
        for table, row in (("correspondence", {"raw_sha256": "a" * 64}),
                           ("extraction", {"sha256": "a" * 64}),
                           ("digest", {"sha256": "a" * 64}),
                           ("finding", {"finding_json": json.dumps({"sources": [{"sha256": "a" * 64}]})})):
            with self.subTest(table=table):
                client = FakeWorkspace({hashlib.sha256(data).hexdigest(): data})
                client.export["tables"].append(table)
                client.export["rows"][table] = [row]
                with self.assertRaisesRegex(backup.BackupError, "dangling_object_reference"):
                    backup.export_hosted(client, self.out)

    def test_hosted_draft_without_sources_round_trips(self):
        client = FakeWorkspace({})
        tables = ("campaign", "setting", "agency", "request", "correspondence", "original",
                  "receipt_occurrence", "extraction", "digest", "finding", "review_receipt",
                  "publication", "correction", "subscriber_event", "meeting", "job",
                  "external_action", "incident", "run_receipt")
        client.export["tables"] = list(tables)
        for table in tables:
            client.export["rows"].setdefault(table, [])
        client.export["rows"]["receipt_occurrence"] = []
        finding = {"id": "synthetic-draft", "author": "synthetic", "classification": "documented_fact",
                   "confidence": "needs_attorney_review", "summary": "Draft in progress"}
        client.export["rows"]["finding"] = [{"finding_id": finding["id"], "state": "draft",
                                             "finding_json": json.dumps(finding)}]
        manifest = backup.export_hosted(client, self.out)
        self.assertEqual(manifest["counts"]["objects"], 0)
        self.assertTrue(backup.verify(self.out, manifest["manifest_sha256"])["ok"])
        target = self.base / "restored"
        backup.restore(self.out, target)
        restored = json.loads((target / "private" / "d1-export.json").read_bytes())
        self.assertEqual(restored, client.export)

    def test_restored_hosted_archive_preserves_metadata_when_exported_locally(self):
        data = b"synthetic hosted"
        manifest = backup.export_hosted(FakeWorkspace({hashlib.sha256(data).hexdigest(): data}), self.out)
        self.assertTrue(backup.verify(self.out, manifest["manifest_sha256"])["ok"])
        target = self.base / "restored"
        backup.restore(self.out, target)
        result = backup.export(target, self.base / "again.tar")
        self.assertIn("private/d1-export.json", result["files"])
        self.assertEqual(result["counts"]["objects"], 1)

    def test_hosted_reexport_refuses_symlinked_private_directory(self):
        client = FakeWorkspace({})
        client.export["rows"]["receipt_occurrence"] = []
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "d1-export.json").write_text(json.dumps(client.export))
        linked = self.base / "linked-campaign"
        linked.mkdir()
        (linked / "campaign.json").write_text("{}")
        (linked / "private").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(backup.BackupError, "unsafe_private_directory"):
            backup.export(linked, self.out)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
