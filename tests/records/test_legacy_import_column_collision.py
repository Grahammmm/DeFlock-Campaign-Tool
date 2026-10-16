"""Synthetic C17: source columns cannot collide with importer-only row IDs."""
import json
import sqlite3
import unittest

from campaign_tool.records.ledger import store
from tests.records import test_ledger as fixtures


class ImportColumnCollisionTests(unittest.TestCase):
    setUp = fixtures.LedgerTests.setUp
    tearDown = fixtures.LedgerTests.tearDown
    refresh_manifest = fixtures.LedgerTests.refresh_manifest

    def replace_meta(self, declaration, rows):
        with sqlite3.connect(self.snapshot / "intake.sqlite") as con:
            con.execute("DROP TABLE meta")
            con.execute("CREATE TABLE meta(" + declaration + ")")
            for values in rows:
                con.execute("INSERT INTO meta VALUES(" + ",".join("?" for _ in values) + ")", values)
        self.refresh_manifest()

    def captured_meta(self):
        with store.ledger(self.output, readonly=True) as con:
            return [dict(row) for row in con.execute(
                "SELECT source_rowid,identity_json,payload_json,payload_sha256 FROM legacy_rows "
                "WHERE source_table='meta' ORDER BY source_rowid")]

    def assert_payloads(self, expected):
        rows = self.captured_meta()
        self.assertEqual([json.loads(row["payload_json"]) for row in rows], expected)
        for row in rows:
            self.assertEqual(row["payload_sha256"], store.sha_bytes(row["payload_json"].encode()))
        return rows

    def test_reserved_source_column_preserved_in_each_position(self):
        for number, (columns, values) in enumerate((
            ("_source_rowid_ TEXT,key TEXT PRIMARY KEY,value TEXT", ("source", "a", "value")),
            ("key TEXT PRIMARY KEY,_source_rowid_ TEXT,value TEXT", ("a", "source", "value")),
            ("key TEXT PRIMARY KEY,value TEXT,_source_rowid_ TEXT", ("a", "value", "source")),
        )):
            with self.subTest(columns=columns):
                # Separate output preserves every preceding candidate unchanged.
                self.output = self.root / ("candidate-" + str(number)) / "ledger.sqlite"
                self.replace_meta(columns, [values])
                result = store.import_legacy(self.snapshot, self.output)
                self.assertEqual(result["status"], "captured")
                rows = self.assert_payloads([{"key": "a", "value": "value", "_source_rowid_": "source"}])
                self.assertEqual(rows[0]["source_rowid"], 1)
                self.assertEqual(json.loads(rows[0]["identity_json"]), {"key": "a"})

    def test_case_variant_column_is_not_replaced_with_internal_rowid(self):
        self.replace_meta("key TEXT PRIMARY KEY,_SOURCE_ROWID_ TEXT,value BLOB",
                          [("a", "source-owned Ω", b"\x00\xff")])
        store.import_legacy(self.snapshot, self.output)
        self.assert_payloads([{"key": "a", "_SOURCE_ROWID_": "source-owned Ω",
                               "value": store.json_value(b"\x00\xff")}])

    def test_reserved_primary_key_retains_source_identity(self):
        self.replace_meta("_source_rowid_ TEXT PRIMARY KEY,value TEXT", [("source-key", None)])
        store.import_legacy(self.snapshot, self.output)
        rows = self.assert_payloads([{"_source_rowid_": "source-key", "value": None}])
        self.assertEqual(rows[0]["source_rowid"], 1)
        self.assertEqual(json.loads(rows[0]["identity_json"]), {"_source_rowid_": "source-key"})

    def test_checkpoint_resume_and_replay_do_not_use_source_column_as_cursor(self):
        self.replace_meta("key TEXT PRIMARY KEY,_source_rowid_ TEXT,value TEXT",
                          [("a", "999999", "one"), ("b", "-999999", "two"), ("c", "not-a-rowid", "three")])
        for _ in range(20):
            result = store.import_legacy(self.snapshot, self.output, batch_size=1, max_batches=1)
            if result["status"] == "captured":
                break
        self.assertEqual(result["status"], "captured")
        expected = [{"key": "a", "_source_rowid_": "999999", "value": "one"},
                    {"key": "b", "_source_rowid_": "-999999", "value": "two"},
                    {"key": "c", "_source_rowid_": "not-a-rowid", "value": "three"}]
        before = self.assert_payloads(expected)
        self.assertEqual([row["source_rowid"] for row in before], [1, 2, 3])
        replay = store.import_legacy(self.snapshot, self.output, batch_size=1)
        self.assertTrue(replay["reused"])
        self.assertEqual(self.captured_meta(), before)
        with store.ledger(self.output, readonly=True) as con:
            checkpoint = con.execute("SELECT last_rowid,copied_rows FROM legacy_checkpoints WHERE source_table='meta'").fetchone()
        self.assertEqual(tuple(checkpoint), (3, 3))

    def test_non_primary_source_collision_keeps_internal_identity(self):
        self.replace_meta("_source_rowid_ TEXT,value TEXT", [("same-source-value", "one"), ("same-source-value", "two")])
        store.import_legacy(self.snapshot, self.output, batch_size=1)
        rows = self.assert_payloads([{"_source_rowid_": "same-source-value", "value": "one"},
                                    {"_source_rowid_": "same-source-value", "value": "two"}])
        self.assertEqual([json.loads(row["identity_json"]) for row in rows], [{"rowid": 1}, {"rowid": 2}])

    def test_generated_source_column_is_preserved(self):
        # table_info omits generated columns; SELECT * still includes this one.
        with sqlite3.connect(self.snapshot / "intake.sqlite") as con:
            con.execute("DROP TABLE meta")
            con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT, "
                        "_source_rowid_ TEXT GENERATED ALWAYS AS (key || ':' || value) VIRTUAL)")
            con.execute("INSERT INTO meta(key,value) VALUES('a','source')")
        self.refresh_manifest()
        store.import_legacy(self.snapshot, self.output)
        self.assert_payloads([{"key": "a", "value": "source", "_source_rowid_": "a:source"}])

    def test_original_declaration_collision_preserves_payload_without_promotion(self):
        with sqlite3.connect(self.snapshot / "intake.sqlite") as con:
            con.execute("ALTER TABLE docs ADD COLUMN _source_rowid_ TEXT")
            con.execute("UPDATE docs SET _source_rowid_='source-declaration'")
            source_row = con.execute("SELECT * FROM docs").fetchone()
            columns = [row[1] for row in con.execute("PRAGMA table_info(docs)")]
            expected = dict(zip(columns, source_row))
        self.refresh_manifest()
        source_hash = store.file_hash(self.snapshot / "intake.sqlite")
        result = store.import_legacy(self.snapshot, self.output)
        with store.ledger(self.output, readonly=True) as con:
            row = con.execute("SELECT payload_json FROM legacy_rows WHERE source_table='docs'").fetchone()
        self.assertEqual(json.loads(row[0]), expected)
        self.assertEqual(store.file_hash(self.snapshot / "intake.sqlite"), source_hash)
        self.assertEqual(result["stage_promotions"], 0)
        self.assertFalse(result["publication_ready"])
        self.assertEqual(store.counts(self.output)["stages"]["preserve"]["done"], 0)
