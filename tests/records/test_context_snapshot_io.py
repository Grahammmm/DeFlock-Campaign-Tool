"""Synthetic SQLite context artifact bounds; no extraction, locators or runtime."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from campaign_tool.records import context_revision as cr, context_runtime as runtime
from campaign_tool.records.context_snapshot_io import read_artifact
from campaign_tool.records.ledger import stages
from campaign_tool.records.run import root_lock
from tests.records import test_context_revision as fixtures


class SnapshotReadTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.executescript("""
            CREATE TABLE ledger_meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE stage_artifacts(sha256 TEXT PRIMARY KEY,payload BLOB);
            CREATE TABLE receipts(sha256 TEXT,subject_sha256 TEXT);
            CREATE TABLE proposals(owner_approval TEXT,approved_content_sha256 TEXT,
                privacy_receipt_sha256 TEXT,independent_receipt_sha256 TEXT);
        """)
        self.subject = fixtures.ORIGINAL
        self.key = "context-revision-v1:" + self.subject
        self.payloads_returned = []
        def rows(cursor, row):
            if cursor.description[0][0].startswith("CASE WHEN"):
                self.payloads_returned.append(None if row[0] is None else len(row[0]))
            return row
        self.con.row_factory = rows

    @contextmanager
    def ledger(self, *args, **kwargs):
        yield self.con

    def install(self, raw, digest=None):
        digest = digest or hashlib.sha256(raw).hexdigest()
        self.con.execute("INSERT INTO stage_artifacts VALUES(?,?)", (digest, raw))
        self.con.execute("INSERT INTO ledger_meta VALUES(?,?)", (self.key, digest))
        self.con.commit()
        return digest

    def test_exact_existing_artifact_limit_is_returned_without_truncation(self):
        raw = b"x" * stages.MAX_CONTENT_BYTES
        digest = self.install(raw)
        self.assertEqual(read_artifact(self.con, digest), raw)
        self.assertEqual(self.payloads_returned, [stages.MAX_CONTENT_BYTES])

    def test_one_byte_over_limit_never_reaches_python_or_json_decoder(self):
        # Valid content hash reproduces the old path to the JSON decoder.
        raw = b" " * (stages.MAX_CONTENT_BYTES + 1)
        self.install(raw)
        with patch.object(runtime.store, "ledger", self.ledger), patch.object(runtime.json, "loads") as decode, \
                patch.object(runtime.hashlib, "sha256") as hash_payload:
            with self.assertRaisesRegex(ValueError, "^context_snapshot_bound$"):
                runtime.ledger_binding("synthetic", self.subject, "review")
        decode.assert_not_called()
        hash_payload.assert_not_called()
        self.assertEqual(self.payloads_returned, [None])

    def test_non_blob_and_null_rows_fail_closed(self):
        for value in ('{"synthetic":true}', None):
            with self.subTest(value=value):
                self.con.execute("DELETE FROM stage_artifacts")
                self.con.execute("INSERT INTO stage_artifacts VALUES(?,?)", ("b" * 64, value))
                with self.assertRaisesRegex(ValueError, "^context_snapshot_bound$"):
                    read_artifact(self.con, "b" * 64)
        self.assertEqual(self.payloads_returned, [None, None])

    def test_missing_artifact_keeps_corrupt_pointer_rejection(self):
        self.con.execute("INSERT INTO ledger_meta VALUES(?,?)", (self.key, "b" * 64))
        self.assertIsNone(read_artifact(self.con, "b" * 64))
        with patch.object(runtime.store, "ledger", self.ledger):
            with self.assertRaisesRegex(ValueError, "^context_snapshot_corrupt$"):
                runtime.ledger_binding("synthetic", self.subject, "review")

    def test_undersized_wrong_hash_still_fails_before_decoding(self):
        self.install(b"{}", digest="b" * 64)
        with patch.object(runtime.store, "ledger", self.ledger), patch.object(runtime.json, "loads") as decode:
            with self.assertRaisesRegex(ValueError, "^context_snapshot_corrupt$"):
                runtime.ledger_binding("synthetic", self.subject, "review")
        decode.assert_not_called()

    def test_valid_snapshot_retains_exact_revision_binding(self):
        revision = fixtures.revision()
        self.install(cr.canonical({"subject_sha256": self.subject, "revision": revision}))
        with patch.object(runtime.store, "ledger", self.ledger):
            bound = runtime.ledger_binding("synthetic", self.subject, "review")
        self.assertEqual(bound, {"context_stage_sha256": revision["stage_hashes"]["review"],
                                 "context_revision_sha256": revision["revision_sha256"]})

    def test_oversized_prior_snapshot_revokes_approval_without_advancing_pointer(self):
        digest = self.install(b" " * (stages.MAX_CONTENT_BYTES + 1))
        self.con.execute("INSERT INTO receipts VALUES(?,?)", ("c" * 64, self.subject))
        self.con.execute("INSERT INTO proposals VALUES(?,?,?,?)", ("approved", "d" * 64, "c" * 64, None))
        self.con.commit()
        with tempfile.TemporaryDirectory() as temporary:
            # Real resolved path and shared root lock; no simulated lock marker.
            pipeline = SimpleNamespace(root=SimpleNamespace(path=Path(temporary).resolve(), ledger="synthetic"))
            runner = Mock()
            with root_lock(pipeline.root.path), patch.object(runtime.store, "ledger", self.ledger), \
                    patch.object(runtime, "snapshot", return_value={}):
                with self.assertRaisesRegex(ValueError, "^context_snapshot_bound$"):
                    runtime.reconcile(pipeline, self.subject, runner)
        runner.invalidate.assert_not_called()
        self.assertEqual(self.con.execute("SELECT owner_approval,approved_content_sha256 FROM proposals").fetchone(),
                         ("none", None))
        self.assertEqual(self.con.execute("SELECT value FROM ledger_meta WHERE key=?", (self.key,)).fetchone()[0], digest)
        self.assertEqual(self.con.execute("SELECT count(*) FROM receipts").fetchone()[0], 1)
        self.assertEqual(self.con.execute("SELECT count(*) FROM stage_artifacts").fetchone()[0], 1)
