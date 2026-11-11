"""RF0010-1: a saved finding summary holding an overflowing JSON number (1e999) is refused like literal Infinity/NaN.

Synthetic data only; offline. Run from the repository root: python3 -B -m unittest tests.test_finding_search_log_numbers
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import finding_search_log  # noqa: E402
from tests import test_finding_search_log as base  # noqa: E402

REFUSAL = "--search-log file is not valid UTF-8 JSON"
OVERFLOW = ("1e999", "-1e999", "1E+9999", "1.7976931348623159e308", "-123.456e400")


class NumberTests(unittest.TestCase):
    def setUp(self):
        # Compose the existing fixture (init/ingest/register/summary) without inheriting its discoverable tests.
        self.fx = base.FindingSearchLogTests("setUp")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def raw_with_agency(self, token):
        text = json.dumps(self.fx.summary(), indent=2)
        old = json.dumps(self.fx.summary()["agency_name"])
        self.assertIn(old, text)
        return text.replace(old, token, 1)

    def plant(self, raw, name="numbers.json"):
        path = self.fx.base / name
        path.write_text(raw)
        return path

    def snapshot(self):
        out = {}
        for p in sorted(self.fx.root.rglob("*")):
            s = p.lstat()
            out[str(p)] = (p.read_bytes() if p.is_file() else None, s.st_mode, s.st_ino, s.st_mtime_ns)
        return out

    def test_overflow_refused_at_every_depth(self):
        for token in OVERFLOW:
            for wrapped in (token, "[%s]" % token, '{"a": [{"b": %s}]}' % token):
                path = self.plant(self.raw_with_agency(wrapped))
                with self.assertRaises(ValueError) as caught:
                    finding_search_log._load_summary(path)
                self.assertEqual(str(caught.exception), REFUSAL)
        # also an unknown top-level field
        summary = self.fx.summary()
        raw = json.dumps(summary)[:-1] + ', "extra": [1e999]}'
        with self.assertRaises(ValueError) as caught:
            finding_search_log._load_summary(self.plant(raw))
        self.assertEqual(str(caught.exception), REFUSAL)

    def test_public_composer_and_actual_cli_refuse_ignored_field_overflow(self):
        path = self.plant(self.raw_with_agency("1e999"))
        before = self.snapshot()
        with self.assertRaises(ValueError) as caught:
            self.fx.draft(path)
        self.assertEqual(str(caught.exception), REFUSAL)
        result = self.fx.cli("finding", "new", "--directory", self.fx.root, "--agency", base.AID, "--search-log", path,
                             returncode=2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "Stopped: %s\n" % REFUSAL)
        self.assertFalse(self.fx.drafts.exists())
        self.assertEqual(self.snapshot(), before)

    def test_overflow_refuses_before_structure_replay_and_writes(self):
        path = self.plant(self.raw_with_agency("1e999"))
        names = ("_structure", "_verified_object", "_body", "_write_draft")
        patches = [mock.patch.object(finding_search_log, n, side_effect=AssertionError(n)) for n in names]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with self.assertRaises(ValueError) as caught:
            self.fx.draft(path)
        self.assertEqual(str(caught.exception), REFUSAL)

    def test_finite_floats_and_display_name_remain_compatible(self):
        for token in ("1e308", "1.7976931348623157e308", "-0.0", "1e-999", "12.5", '"1e999"', "null", "7"):
            path = self.plant(self.raw_with_agency(token))
            result = self.fx.draft(path)
            self.assertTrue(Path(result["written"]).is_file(), token)
            self.fx.drafts and [p.unlink() for p in self.fx.drafts.glob("*")]
        values = json.loads('[1e308, -0.0, 1e-999, 3, true, 2.0]', parse_float=finding_search_log._finite_float)
        self.assertEqual([type(v) for v in values], [float, float, float, int, bool, float])
        self.assertEqual(values[0], 1e308)

    def test_finite_count_float_still_fails_exact_type_comparison(self):
        summary = self.fx.summary()
        summary["rows"]["read"] = float(summary["rows"]["read"])
        path = self.fx.write_summary(summary, "float-count.json")
        with self.assertRaises(ValueError) as caught:
            self.fx.draft(path)
        self.assertNotEqual(str(caught.exception), REFUSAL)

    def test_literal_constants_still_refused(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.assertRaises(ValueError) as caught:
                finding_search_log._load_summary(self.plant(self.raw_with_agency(token)))
            self.assertEqual(str(caught.exception), REFUSAL)

    def test_unrelated_exceptions_propagate(self):
        path = self.plant(self.raw_with_agency("1.5"))
        for exc in (RuntimeError("x"), MemoryError(), KeyboardInterrupt()):
            with mock.patch.object(finding_search_log, "_finite_float", side_effect=exc):
                with self.assertRaises(type(exc)):
                    finding_search_log._load_summary(path)


if __name__ == "__main__":
    unittest.main()
