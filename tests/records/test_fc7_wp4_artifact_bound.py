"""Synthetic SQLite controls for WP5's installed WP4 support artifact bound."""
import hashlib
import sqlite3
import unittest
from pathlib import Path

from campaign_tool.records import catalog_wp4_support as support
from campaign_tool.records.catalog_stage import CatalogStageError


LIMIT = 8
PAYLOAD = b"123456789"


class ObservedCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.payload_selects = []
        self.fetched_payload_lengths = []

    def execute(self, sql, parameters=()):
        if "SELECT payload FROM stage_artifacts" in sql:
            self.payload_selects.append((sql, tuple(parameters)))
        self.cursor.execute(sql, parameters)
        return self

    def fetchone(self):
        row = self.cursor.fetchone()
        if self.payload_selects and row is not None:
            self.fetched_payload_lengths.append(len(row[0]) if type(row[0]) is bytes else None)
        return row


class ArtifactBoundTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE stage_artifacts (sha256 TEXT PRIMARY KEY, payload BLOB)")
        self.cursor = ObservedCursor(self.db.cursor())
        self.digest = hashlib.sha256(PAYLOAD).hexdigest()
        self.db.execute("INSERT INTO stage_artifacts VALUES (?, ?)", (self.digest, PAYLOAD))

    def tearDown(self):
        self.db.close()

    def artifact(self, digest=None):
        return support._artifact(self.cursor, self.digest if digest is None else digest, LIMIT)

    def test_below_exact_and_over_limit(self):
        for payload in (b"1234567", b"12345678"):
            digest = hashlib.sha256(payload).hexdigest()
            self.db.execute("INSERT OR REPLACE INTO stage_artifacts VALUES (?, ?)", (digest, payload))
            self.assertEqual(support._artifact(self.cursor, digest, LIMIT), payload)
        with self.assertRaisesRegex(CatalogStageError, "^wp4_stage_artifact_identity$"):
            self.artifact()
        self.assertEqual(self.cursor.fetched_payload_lengths, [7, 8])

    def test_missing_null_text_and_wrong_hash(self):
        with self.assertRaisesRegex(CatalogStageError, "^wp4_stage_artifact_identity$"):
            self.artifact("0" * 64)
        self.db.execute("INSERT INTO stage_artifacts VALUES (?, NULL)", ("n" * 64,))
        with self.assertRaisesRegex(CatalogStageError, "^wp4_stage_artifact_identity$"):
            self.artifact("n" * 64)
        self.db.execute("INSERT INTO stage_artifacts VALUES (?, ?)", ("t" * 64, "not a blob"))
        with self.assertRaisesRegex(CatalogStageError, "^wp4_stage_artifact_identity$"):
            self.artifact("t" * 64)
        wrong_hash = hashlib.sha256(b"other").hexdigest()
        self.db.execute("INSERT INTO stage_artifacts VALUES (?, ?)", (wrong_hash, b"payload"))
        with self.assertRaisesRegex(CatalogStageError, "^wp4_stage_artifact_identity$"):
            self.artifact(wrong_hash)

    def test_observed_fetch_never_contains_oversize_blob(self):
        with self.assertRaisesRegex(CatalogStageError, "^wp4_stage_artifact_identity$"):
            self.artifact()
        self.assertEqual(self.cursor.fetched_payload_lengths, [])

    def test_bound_remains_on_payload_select(self):
        try:
            self.artifact()
        except CatalogStageError:
            pass
        sql, parameters = self.cursor.payload_selects[-1]
        self.assertIn("length(payload)", sql.lower())
        self.assertIn("typeof(payload)", sql.lower())
        self.assertIn(LIMIT, parameters)

    def test_valid_artifact_fetch_and_consumer_bound_forwarding(self):
        payload = b"exactlim8"
        payload = payload[:LIMIT]
        digest = hashlib.sha256(payload).hexdigest()
        self.db.execute("INSERT INTO stage_artifacts VALUES (?, ?)", (digest, payload))
        self.assertEqual(support._artifact(self.cursor, digest, LIMIT), payload)
        source = Path(support.__file__).read_text()
        self.assertIn('_artifact(con, state["receipt_sha256"], adapter.stages.MAX_RECEIPT_BYTES)', source)
        self.assertIn('_artifact(con, head["content_sha256"], validation.MAX_CONTENT)', source)
        self.assertEqual(source.count('packet = _decode(_artifact(con, state["receipt_sha256"]'), 1)
        self.assertEqual(source.count('content_raw = _artifact(con, head["content_sha256"]'), 1)

    def test_database_and_authority_unchanged(self):
        before = self.db.execute("SELECT sha256, payload FROM stage_artifacts ORDER BY sha256").fetchall()
        schema = self.db.execute("SELECT sql FROM sqlite_master WHERE name='stage_artifacts'").fetchone()[0]
        self.assertEqual(before, [(self.digest, PAYLOAD)])
        self.assertIn("stage_artifacts", schema)
        self.assertEqual(self.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(),
                         [("stage_artifacts",)])


if __name__ == "__main__":
    unittest.main()
