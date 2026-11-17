"""RF1810-4: `recent` refuses repeated JSON members in optional campaign files. Synthetic data only; no network.

Run from the repository root: python3 -B -m unittest tests.test_recent_duplicate_members
"""
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import recent  # noqa: E402

PROBLEM = "content/sources.json unreadable"


class DuplicateMemberTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-recent-dup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def put(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_store_refuses_top_level_repeat(self):
        self.put("x.json", '{"a": 1, "a": 2}')
        with self.assertRaises(ValueError):
            recent._store(self.root, "x.json")

    def test_store_refuses_nested_and_escaped_repeat(self):
        for text in ('[{"k": 1, "k": 2}]', '{"a": 1, "\\u0061": 2}'):
            self.put("x.json", text)
            with self.assertRaises(ValueError, msg=text):
                recent._store(self.root, "x.json")

    def test_report_marks_repeated_member_sources_file_unreadable(self):
        self.put("campaign.json", json.dumps({"schema_version": 1, "name": "Synthetic",
                                              "county": "San Luis Obispo", "state": "CA"}))
        self.put("content/sources.json", '{"sources": [], "sources": []}')
        out, err = io.StringIO(), io.StringIO()
        recent.run_cli(self.root, "2026-11-01", None, today=date(2026, 11, 4), out=out, err=err)
        self.assertEqual(out.getvalue().count(PROBLEM), 1)
        self.assertNotIn("Traceback", err.getvalue())

    def test_store_null_and_absent_unchanged(self):
        self.put("x.json", "null")
        self.assertIsNone(recent._store(self.root, "x.json"))
        self.assertIs(recent._store(self.root, "missing.json"), recent.ABSENT)

    def test_store_accepts_sibling_same_name_and_case_variants(self):
        self.put("x.json", '[{"a": 1}, {"a": 2}]')
        self.assertEqual(recent._store(self.root, "x.json"), [{"a": 1}, {"a": 2}])
        self.put("x.json", '{"a": 1, "A": 2}')
        self.assertEqual(recent._store(self.root, "x.json"), {"a": 1, "A": 2})


if __name__ == "__main__":
    unittest.main()
