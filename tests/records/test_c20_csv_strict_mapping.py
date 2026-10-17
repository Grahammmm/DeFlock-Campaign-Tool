"""C20 synthetic CSV acceptance; no shared harness or external parsers."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from campaign_tool.records.intake import folder
from tools.check_secret_report import APPROVED_MANIFESTS

ROOT = Path(__file__).resolve().parents[2]


class C20CsvTests(unittest.TestCase):
    def extract(self, raw, form="csv"):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            source = base / ("synthetic." + form)
            source.write_bytes(raw)
            sha = hashlib.sha256(raw).hexdigest()
            out = base / "out"
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "campaign_tool/records/intake/folder.py"),
                 "_worker", "--source", str(source), "--sha", sha,
                 "--format", form, "--output", str(out)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            derived = out / "derived" / sha
            digest = json.loads((derived / "digest.json").read_text())
            units = [json.loads(line) for line in (derived / "units.jsonl").read_text().splitlines()]
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(digest["sha256"], sha)
            return digest, units

    def test_truncated_record_retains_prefix_and_exact_error_span(self):
        digest, units = self.extract(b'a,b\r\nok,value\r\n"unfinished\r\nnext,line')
        self.assertEqual(digest["stage"], "partial")
        self.assertEqual(len(units), 2)
        self.assertEqual(digest["issues"], [{"code": "csv_malformed_quoting_or_record", "detail": "",
                         "locator": {"record": 3, "line_start": 3, "line_end": 4}}])

    def test_truncated_header_has_header_locator(self):
        digest, units = self.extract(b'"unfinished\nheader')
        self.assertEqual(digest["stage"], "partial")
        self.assertEqual(units, [])
        self.assertEqual(digest["issues"][0]["locator"], {"record": 1, "line_start": 1, "line_end": 2})

    def test_invalid_text_after_closing_quote_is_held(self):
        digest, units = self.extract(b'a,b\n"value"garbage,x\n')
        self.assertEqual(digest["stage"], "partial")
        self.assertEqual(len(units), 1)
        self.assertEqual(digest["issues"][0]["locator"], {"record": 2, "line_start": 2, "line_end": 2})

    def test_valid_multiline_headers_values_escapes_duplicates_and_eof(self):
        digest, units = self.extract(b'"first\r\nheader",second\r\n"hello\r\nworld","say ""hi"""\r\nlast,value\r\nlast,value')
        self.assertEqual(digest["stage"], "complete")
        self.assertEqual(digest["issues"], [])
        self.assertEqual([unit["locator"] for unit in units], [
            {"record": 1, "line_start": 1, "line_end": 2},
            {"record": 2, "line_start": 3, "line_end": 4},
            {"record": 3, "line_start": 5, "line_end": 5},
            {"record": 4, "line_start": 6, "line_end": 6}])
        self.assertEqual(units[1]["data"]["values"], ['hello\r\nworld', 'say "hi"'])
        self.assertEqual(units[3]["data"]["duplicate_of_record"], 3)

    def test_ragged_short_long_blank_are_explicitly_ambiguous(self):
        digest, units = self.extract(b'a,b\none\ntwo,three,four\n\nnormal,value\n')
        self.assertEqual(digest["stage"], "partial")
        self.assertEqual(digest["counts"]["ragged_rows"], 3)
        self.assertEqual([issue["locator"]["record"] for issue in digest["issues"]], [2, 3, 4])
        for unit in units[1:4]:
            self.assertEqual(unit["data"]["column_mapping"], "ambiguous")
            self.assertIsNone(unit["data"]["presence"])
        self.assertEqual(units[4]["data"]["presence"], ["present", "present"])
        self.assertEqual(digest["counts"]["fields"][0]["counts"], {"present": 1})

    def test_tsv_multiline_uses_same_contract(self):
        digest, units = self.extract(b'a\tb\n"multi\nline"\tvalue\n', "tsv")
        self.assertEqual(digest["stage"], "complete")
        self.assertEqual(units[1]["locator"], {"record": 2, "line_start": 2, "line_end": 3})

    def test_csv_policy_cache_changes_only_csv_and_tsv(self):
        config = folder.load_config()
        for form in ("csv", "tsv", "txt"):
            legacy = folder.hid(folder.js(dict(format=form, paragraph_ordinals=None, excluded_path_fragments=[])))
            actual = folder.extraction_policy(form, config)
            if form == "txt":
                self.assertEqual(actual, legacy)
            else:
                self.assertNotEqual(actual, legacy)

    def test_exact_manifest_and_engine_pins(self):
        path = "docs/records-intake-import.json"
        raw = (ROOT / path).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), APPROVED_MANIFESTS[path])
        for entry in json.loads(raw)["files"]:
            self.assertEqual(hashlib.sha256((ROOT / entry["engine_path"]).read_bytes()).hexdigest(), entry["engine_sha256"])
