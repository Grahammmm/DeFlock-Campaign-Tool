"""FC7 search query validation regressions using synthetic local FTS indexes."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import retained_units as ru


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def synthetic_result(text: str = "alpha café") -> dict:
    original = digest(b"synthetic-original")
    imported = digest(b"synthetic-import")
    artifact = digest(b"synthetic-artifact")
    text_hash = digest(text.encode("utf-8"))
    locator = {"page": 1}
    locator_text = json.dumps(locator, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    ordinal = 1
    unit_id = digest(json.dumps([imported, ordinal, artifact], separators=(",", ":")).encode())
    row = {
        "unit_id": unit_id,
        "original_sha256": original,
        "import_id": imported,
        "artifact_sha256": artifact,
        "text_sha256": text_hash,
        "locator_text": locator_text,
        "text": text,
        "ordinal": ordinal,
        "locator": locator,
        "data": {},
    }
    snapshot = digest(b"synthetic-snapshot")
    # Content binding is computed by the shipped index builder from sorted rows.
    return {"snapshot_sha256": snapshot, "units": [row], "unit_count": 1, "artifact_bytes": len(text.encode("utf-8"))}


class SearchUnicodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fc7-search-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        built = ru.build_search_index(self.root, synthetic_result())
        self.index = Path(built["path"])
        self.snapshot = built["snapshot_sha256"]
        self.content = built["content_sha256"]

    def search(self, query, *, path=None, limit=50):
        return ru.search_index(path or self.index, self.snapshot, query,
                               expected_content_sha256=self.content, limit=limit)

    def test_valid_ascii_and_multibyte_query(self):
        self.assertEqual(self.search("alpha")[0]["text"], "alpha café")
        self.assertEqual(self.search("café")[0]["text"], "alpha café")

    def test_high_low_and_embedded_surrogates(self):
        for query in ("\ud800", "\udfff", "a\ud800b", "x\udfffy", "\ud800\udfff"):
            with self.subTest(query=repr(query)):
                with self.assertRaises(ru.RetainedUnitError) as caught:
                    self.search(query)
                self.assertEqual(caught.exception.code, "search_query_invalid")

    def test_byte_limit_exact_and_plus_one(self):
        for exact in ("a" * 1024, "é" * 512, "😀" * 256):
            with self.subTest(exact_bytes=len(exact.encode("utf-8"))):
                self.assertEqual(self.search(exact), [])
                with self.assertRaises(ru.RetainedUnitError) as caught:
                    self.search(exact + "a")
                self.assertEqual(caught.exception.code, "search_query_invalid")

    def test_blank_nonstring_and_boolean_limit(self):
        for query in ("", " \t\n", "\u2003", None, 3, True, b"alpha", [], {}):
            with self.subTest(query=repr(query)):
                with self.assertRaises(ru.RetainedUnitError) as caught:
                    self.search(query)
                self.assertEqual(caught.exception.code, "search_query_invalid")
        for limit in (True, False, 0, 201, 1.0, None, "1"):
            with self.subTest(limit=repr(limit)):
                with self.assertRaises(ru.RetainedUnitError) as caught:
                    self.search("alpha", limit=limit)
                self.assertEqual(caught.exception.code, "search_query_invalid")
        self.assertEqual(len(self.search("alpha", limit=1)), 1)
        self.assertEqual(len(self.search("alpha", limit=200)), 1)

    def test_invalid_before_path_or_sqlite(self):
        with mock.patch.object(ru, "_path", side_effect=AssertionError("path inspected")), \
             mock.patch.object(ru.sqlite3, "connect", side_effect=AssertionError("sqlite opened")):
            for query in ("\ud800", "\udfff", "x\ud800y", "x\udfffy", "\ud800\udfff", "a" * 1025, "", None):
                with self.subTest(query=repr(query)):
                    with self.assertRaises(ru.RetainedUnitError) as caught:
                        ru.search_index(self.root / "nonexistent.sqlite", self.snapshot,
                                        query, expected_content_sha256=self.content)
                    self.assertEqual(caught.exception.code, "search_query_invalid")

    def test_valid_search_results_and_index_unchanged(self):
        before = self.index.read_bytes()
        stat_before = self.index.stat()
        entries_before = sorted(p.name for p in self.root.iterdir())
        matches = self.search("café")
        unit = synthetic_result()["units"][0]
        self.assertEqual(matches, [{"unit_id": unit["unit_id"], "original_sha256": unit["original_sha256"],
                                    "locator": '{"page":1}', "text": "alpha café"}])
        for query in ('"', "alpha OR missing", "*", "  café  "):
            with self.subTest(query=query):
                self.assertEqual(self.search(query), matches if query == "  café  " else [])
        for code, kwargs in (
            ("index_content_pin_mismatch", {"expected_content_sha256": digest(b"different-content")}),
            ("index_binding_mismatch", {"expected_snapshot": digest(b"different-snapshot")}),
        ):
            values = {"expected_snapshot": self.snapshot, "expected_content_sha256": self.content}
            values.update(kwargs)
            with self.assertRaises(ru.RetainedUnitError) as caught:
                ru.search_index(self.index, values.pop("expected_snapshot"), "café", **values)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), entries_before)
        self.assertEqual(self.index.read_bytes(), before)
        stat_after = self.index.stat()
        self.assertEqual((stat_before.st_ino, stat_before.st_size, stat_before.st_mtime_ns, stat_before.st_ctime_ns),
                         (stat_after.st_ino, stat_after.st_size, stat_after.st_mtime_ns, stat_after.st_ctime_ns))


if __name__ == "__main__":
    unittest.main()
