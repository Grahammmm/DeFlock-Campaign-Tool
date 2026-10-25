"""Synthetic SQLite controls for WP5's installed WP4 support artifact bound.

No process-wide guards or filesystem fixtures are installed by importing this
module: ordinary unittest discovery remains safe alongside peer suites.
"""
import ast
import hashlib
from pathlib import Path
import sqlite3
import unittest

from campaign_tool.records import catalog_wp4_support as support
from campaign_tool.records.catalog_stage import CatalogStageError
from campaign_tool.records.ledger import stages

LIMIT = 8
PAYLOAD = b"123456789"
REASON = "wp4_stage_artifact_identity"


class ObservedCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.payload_selects = []
        self.fetched_payload_lengths = []
        self.fetched_payload_types = []

    def execute(self, sql, parameters=()):
        self.cursor.execute(sql, parameters)
        self.payload_columns = [index for index, column in enumerate(self.cursor.description or ())
                                if column[0] == "payload"]
        if self.payload_columns:
            self.payload_selects.append((sql, tuple(parameters)))
        return self

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is not None:
            for index in self.payload_columns:
                value = row[index]
                self.fetched_payload_types.append(type(value).__name__)
                if type(value) is bytes:
                    self.fetched_payload_lengths.append(len(value))
        return row


class ArtifactBoundTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE stage_artifacts (sha256 TEXT PRIMARY KEY, payload BLOB)")
        self.cursor = ObservedCursor(self.db.cursor())
        self.digest = hashlib.sha256(PAYLOAD).hexdigest()
        self.db.execute("INSERT INTO stage_artifacts VALUES (?, ?)", (self.digest, PAYLOAD))

    def reject(self, digest, limit=LIMIT):
        with self.assertRaises(CatalogStageError) as raised:
            support._artifact(self.cursor, digest, limit)
        self.assertEqual(str(raised.exception), REASON)

    def test_below_exact_and_over_limit(self):
        for payload in (b"", b"1234567", b"12345678"):
            with self.subTest(size=len(payload)):
                digest = hashlib.sha256(payload).hexdigest()
                self.db.execute("INSERT OR REPLACE INTO stage_artifacts VALUES (?, ?)", (digest, payload))
                self.assertEqual(support._artifact(self.cursor, digest, LIMIT), payload)
        self.reject(self.digest)
        self.assertEqual(self.cursor.fetched_payload_lengths, [0, 7, 8])
        self.assertEqual(support._artifact(self.cursor, hashlib.sha256(b"").hexdigest(), 0), b"")

    def test_missing_null_text_and_wrong_hash(self):
        self.reject(hashlib.sha256(b"missing").hexdigest())
        for name, value, expected_bytes in (
                ("null", None, b""), ("text", "small", b"small"),
                ("integer", 7, b"7"), ("real", 1.5, b"1.5")):
            with self.subTest(kind=name):
                # Text is otherwise within the byte bound and has a matching
                # encoded hash: a size/hash failure cannot conceal the type gate.
                digest = hashlib.sha256(expected_bytes).hexdigest()
                self.db.execute("INSERT OR REPLACE INTO stage_artifacts VALUES (?, ?)", (digest, value))
                self.reject(digest)
        wrong_hash = hashlib.sha256(b"other").hexdigest()
        self.db.execute("INSERT INTO stage_artifacts VALUES (?, ?)", (wrong_hash, b"payload"))
        self.reject(wrong_hash)
        self.assertEqual(self.cursor.fetched_payload_types, ["bytes"])
        for invalid in (None, 7, "short"):
            self.reject(invalid)

    def test_observed_fetch_never_contains_oversize_blob(self):
        # Matching digest and real SQLite; only the size prerequisite fails.
        self.reject(self.digest)
        self.assertEqual(self.cursor.fetched_payload_lengths, [], "OVERSIZE_PAYLOAD_TRANSFER")

    def test_bound_remains_on_payload_select(self):
        self.reject(self.digest)
        self.assertEqual(len(self.cursor.payload_selects), 1)
        sql, parameters = self.cursor.payload_selects[0]
        self.assertIn("length(payload)<=?", sql.lower())
        self.assertIn("typeof(payload)='blob'", sql.lower())
        self.assertEqual(parameters, (self.digest, LIMIT))

    def test_valid_artifact_fetch_and_consumer_bound_forwarding(self):
        payload = b"12345678"
        digest = hashlib.sha256(payload).hexdigest()
        self.db.execute("INSERT INTO stage_artifacts VALUES (?, ?)", (digest, payload))
        self.assertEqual(support._artifact(self.cursor, digest, LIMIT), payload)
        source = Path(support.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        consumer = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "verified_text_line")
        calls = [node for node in ast.walk(consumer) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "_artifact"]
        self.assertEqual([ast.unparse(call) for call in sorted(calls, key=lambda call: call.lineno)], [
            "_artifact(con, state['receipt_sha256'], adapter.stages.MAX_RECEIPT_BYTES)",
            "_artifact(con, head['content_sha256'], validation.MAX_CONTENT)"])
        # All stage_artifacts SQL in this module must remain in the one bounded
        # helper; no second unbounded payload read may bypass those fixed limits.
        selections = [node for node in ast.walk(tree) if isinstance(node, ast.Constant)
                      and isinstance(node.value, str) and "stage_artifacts" in node.value.lower()]
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_artifact")
        self.assertEqual(len(selections), 1)
        self.assertIsNotNone(helper.end_lineno)
        self.assertTrue(helper.lineno <= selections[0].lineno <= (helper.end_lineno or 0))

    def test_database_and_authority_unchanged(self):
        valid = b"12345678"
        digest = hashlib.sha256(valid).hexdigest()
        self.db.execute("INSERT INTO stage_artifacts VALUES (?, ?)", (digest, valid))
        before = list(self.db.iterdump())
        changes = self.db.total_changes
        validators = dict(stages._INSTALLED_VALIDATORS)
        profiles = dict(stages._INSTALLED_PROFILES)
        self.assertEqual(support._artifact(self.cursor, digest, LIMIT), valid)
        self.reject(self.digest)
        self.reject(hashlib.sha256(b"absent").hexdigest())
        self.assertEqual(list(self.db.iterdump()), before)
        self.assertEqual(self.db.total_changes, changes)
        self.assertEqual(stages._INSTALLED_VALIDATORS, validators)
        self.assertEqual(stages._INSTALLED_PROFILES, profiles)
        self.assertEqual(self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                         [("stage_artifacts",)])


if __name__ == "__main__":
    unittest.main()
