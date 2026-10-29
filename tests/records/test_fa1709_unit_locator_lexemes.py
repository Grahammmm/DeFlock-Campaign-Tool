"""Fictional captured locator spellings are observations, never parser proof.

Portable unittest entry point; no operator manifest or real data is required.
"""
from contextlib import ExitStack
import functools
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import traceback
import unittest
from unittest import mock
from urllib.parse import unquote


FIXTURE_ROOT = None  # The evidence launcher may supply its retained run child.
TEXT = "fictional café observation\r\nsecond line"
LOCATORS = (
    '{"line":1,"label":"café"}',
    '{"label":"café","line":1}',
    '{ "line" : 1, "label" : "café" }',
    '{"line":1,"label":"caf\\u00e9"}',
)


def checked_directory(path):
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("synthetic root has a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError("synthetic root is not a directory")
    return path


def offline(stack, root):
    """Scoped side-effect sentinels, not a hostile-code sandbox.

    The inspected import closure is store + three inert SQL migrations. There
    are no reachable data-path constants or bound real-data defaults to patch.
    All application database paths must instead be caller-owned fixture paths.
    """
    def denied(*args, **kwargs):
        raise RuntimeError("FA1709 network/model/child execution forbidden")

    connect = sqlite3.connect

    def synthetic_connect(database, *args, **kwargs):
        name = os.fspath(database)
        # SQLite's exact private-memory target cannot access real data. Do not
        # exempt file: URIs, empty targets, or filesystem paths from confinement.
        if name == ":memory:":
            return connect(database, *args, **kwargs)
        if name.startswith("file:"):
            name = unquote(name[5:].split("?", 1)[0])
        path = Path(os.path.abspath(name))
        if root not in path.parents or any(p.is_symlink() for p in (path, *path.parents)):
            raise RuntimeError("FA1709 nonfixture database forbidden")
        return connect(database, *args, **kwargs)

    stack.enter_context(mock.patch.object(socket, "socket", side_effect=denied))
    stack.enter_context(mock.patch.object(socket, "create_connection", side_effect=denied))
    stack.enter_context(mock.patch.object(socket, "getaddrinfo", side_effect=denied))
    stack.enter_context(mock.patch.object(subprocess, "Popen", side_effect=denied))
    stack.enter_context(mock.patch.object(os, "system", side_effect=denied))
    stack.enter_context(mock.patch.object(sqlite3, "connect", side_effect=synthetic_connect))


def assert_source_spelling(test, observed, original):
    test.assertEqual(observed.encode("utf-8"), original.encode("utf-8"),
                     "exact source spelling must survive projection")


class LocatorLexemeTests(unittest.TestCase):
    def setUp(self):
        root = checked_directory(tempfile.gettempdir() if FIXTURE_ROOT is None
                                 else FIXTURE_ROOT)
        self.tmp = tempfile.TemporaryDirectory(prefix="lexemes-", dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        old_mask = os.umask(0o077)
        self.addCleanup(os.umask, old_mask)
        offline(self.stack, self.root)
        # Install sentinels BEFORE application or existing fixture imports.
        from campaign_tool.records.ledger import store, unit_projection
        from tests.records import test_ledger
        self.store, self.projection = store, unit_projection
        # Exercise reference-schema construction under the guard every time;
        # correctness must not depend on another module warming this cache.
        store._base_schema.cache_clear()
        self.fixture_class = test_ledger.LedgerTests
        self.database = self.root / "ledger" / "ledger.sqlite"

    def rows(self, table):
        with self.store.ledger(self.database, readonly=True) as con:
            return [dict(row) for row in con.execute("SELECT * FROM " + table)]

    def capture(self, locator):
        fixture = self.fixture_class()
        # The existing factory's otherwise-unbound temp default is redirected
        # before setUp. This is synthetic source construction, not ledger edits.
        factory = tempfile.TemporaryDirectory
        with mock.patch.object(tempfile, "TemporaryDirectory",
                               functools.partial(factory, dir=self.root)):
            fixture.setUp()
        self.addCleanup(fixture.tearDown)
        with sqlite3.connect(fixture.snapshot / "intake.sqlite") as con:
            con.execute("UPDATE units SET locator=?,text=?", (locator, TEXT))
        fixture.refresh_manifest()
        result = self.store.import_legacy(fixture.snapshot, self.database)
        self.assertEqual(result["status"], "captured")
        self.assertEqual(result["stage_promotions"], 0)
        return result["import_id"]

    def project_and_check(self, locator):
        capture = self.capture(locator)
        before = {table: self.rows(table) for table in ("stage_state", "stage_events")}
        acceptance = self.store.counts(self.database)["acceptance"]
        result = self.projection.project_units(self.database, capture)
        self.assertEqual(result["projected_source_rows"], 1)
        self.assertEqual(result["blocked_source_rows"], 0)
        self.assertEqual(result["unresolved_reasons"], {"missing_parser_provenance": 1})
        self.assertEqual(result["stage_promotions"], 0)
        self.assertIs(result["original_parser_verified"], False)
        self.assertIs(result["publication_ready"], False)
        self.assertIsNone(result["end_to_end_complete"])
        for table, prior in before.items():
            self.assertEqual(self.rows(table), prior)
        self.assertEqual(self.store.counts(self.database)["acceptance"], acceptance)
        evidence = [row for row in self.rows("unit_projection_rows")
                    if row["import_id"] == capture]
        self.assertEqual(len(evidence), 1)
        evidence = evidence[0]
        unit = next(row for row in self.rows("units") if row["id"] == evidence["unit_id"])
        source = next(row for row in self.rows("legacy_rows")
                      if row["import_id"] == capture and row["source_table"] == "units")
        self.assertEqual(evidence["payload_sha256"], source["payload_sha256"])
        self.assertEqual(json.loads(source["payload_json"])["locator"], locator)
        assert_source_spelling(self, unit["locator"], locator)
        assert_source_spelling(self, evidence["observed_locator"], locator)
        self.assertEqual(evidence["observed_text"], TEXT)
        self.assertEqual(evidence["observed_text_sha256"], hashlib.sha256(TEXT.encode()).hexdigest())
        self.assertEqual(unit["text_sha256"], evidence["observed_text_sha256"])
        self.assertEqual(unit["status"], "captured_unverified")
        self.assertIsNone(unit["parser"])
        self.assertIsNone(unit["parser_version"])
        provenance = json.loads(unit["provenance_json"])
        self.assertIs(provenance["original_parser_verified"], False)
        self.assertEqual(provenance["parser_provenance"], "missing")
        self.assertEqual(provenance["source_payload_sha256"], source["payload_sha256"])
        self.assertEqual(evidence["reason"], "missing_parser_provenance")
        self.assertEqual(evidence["integrity_checked"], 1)
        tables = ("units", "unit_projection_rows", "unit_projection_checkpoints", "import_gaps",
                  "stage_state", "stage_events")
        prior = {table: self.rows(table) for table in tables}
        replay = self.projection.project_units(self.database, capture)
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["projection_id"], result["projection_id"])
        for table, values in prior.items():
            self.assertEqual(self.rows(table), values)
        return unit, evidence, capture

    def test_equivalent_lexemes_preserve_bytes_and_distinct_evidence(self):
        records = []
        for locator in LOCATORS:
            with self.subTest(locator=locator):
                self.assertEqual(json.loads(locator), json.loads(LOCATORS[0]))
                records.append(self.project_and_check(locator))
        self.assertEqual(len({row[0]["id"] for row in records}), len(LOCATORS))
        self.assertEqual(len({row[1]["payload_sha256"] for row in records}), len(LOCATORS))
        self.assertEqual(len({row[2] for row in records}), len(LOCATORS))
        for key in ("original_sha256", "legacy_ordinal", "unit_type", "text_sha256"):
            self.assertEqual(len({row[0][key] for row in records}), 1)

    def test_semantic_value_change_changes_evidence_not_observations(self):
        first = self.project_and_check(LOCATORS[0])
        changed = LOCATORS[0].replace('"line":1', '"line":2')
        self.assertNotEqual(json.loads(LOCATORS[0]), json.loads(changed))
        second = self.project_and_check(changed)
        self.assertNotEqual(first[0]["id"], second[0]["id"])
        self.assertNotEqual(first[1]["payload_sha256"], second[1]["payload_sha256"])
        for key in ("original_sha256", "legacy_ordinal", "unit_type", "text_sha256"):
            self.assertEqual(first[0][key], second[0][key])
        self.assertEqual(first[1]["observed_text"], second[1]["observed_text"])

    def test_canonicalizing_result_transformer_is_detected(self):
        locator = '{ "label" : "café", "line" : 1 }'
        unit, evidence, capture = self.project_and_check(locator)
        faulty = dict(unit, locator=json.dumps(json.loads(unit["locator"]),
                                              sort_keys=True, separators=(",", ":")))
        self.assertEqual(json.loads(faulty["locator"]), json.loads(locator))
        try:
            assert_source_spelling(self, faulty["locator"], locator)
        except AssertionError as error:
            self.assertIn("exact source spelling", str(error))
            print("EXPECTED SOURCE-SPELLING ASSERTION FAILURE\n" + traceback.format_exc())
        else:
            self.fail("canonicalizing transformer escaped the deciding assertion")
        assert_source_spelling(self, unit["locator"], locator)
        assert_source_spelling(self, evidence["observed_locator"], locator)
        self.assertTrue(self.projection.project_units(self.database, capture)["reused"])
        actual = next(row for row in self.rows("units") if row["id"] == unit["id"])
        assert_source_spelling(self, actual["locator"], locator)


if __name__ == "__main__":
    unittest.main()
