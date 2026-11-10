"""RF0445-4: `law impact` refuses over-deeply nested package JSON as an input error (synthetic packages only).

Run from the repository root: python3 -B -m unittest tests.test_law_impact_depth -v
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law, law_impact  # noqa: E402

BASE_TEXT = (REPO_ROOT / "tests" / "fixtures" / "law_impact" / "base-package.json").read_text(encoding="utf-8")
DEPTH = 10000
SENTINEL = "SYNTHETIC-SENTINEL-7731"


def deep_array():
    return "[" * DEPTH + "]" * DEPTH


def with_extra(value_text):
    return BASE_TEXT.rstrip().rstrip("}").rstrip() + ',\n"extra": ' + value_text + "\n}"


def with_rules_value():
    package = json.loads(BASE_TEXT)
    key = next(k for k, v in package.items() if k != "jurisdiction" and isinstance(v, list))
    package[key] = "@@"
    return json.dumps(package).replace('"@@"', deep_array())


DEEP = {
    "top-array": deep_array,
    "extra-object": lambda: with_extra('{"a":' * DEPTH + "1" + "}" * DEPTH),
    "rules-value": with_rules_value,
}


class DepthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-law-depth-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.old, self.new = self.tmp / "old.json", self.tmp / "new.json"
        self.old.write_text(BASE_TEXT, encoding="utf-8")
        self.new.write_text(BASE_TEXT, encoding="utf-8")
        self.root = self.tmp / "campaign"
        (self.root / "content" / "findings").mkdir(parents=True)

    def snapshot(self):
        return sorted((str(p.relative_to(self.tmp)), p.stat().st_mode, p.stat().st_mtime_ns,
                       p.read_bytes() if p.is_file() else None) for p in self.tmp.rglob("*"))

    def run_main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = law.main(["impact", str(self.old), str(self.new), "--directory", str(self.root), *extra])
        return code, out.getvalue(), err.getvalue()

    def test_real_decoder_depth_failures_are_input_errors(self):
        for name, build in DEEP.items():
            with self.subTest(name):
                self.old.write_text(build(), encoding="utf-8")
                self.assertLess(self.old.stat().st_size, law_impact.MAX_PACKAGE_BYTES)
                with self.assertRaises(law_impact.ImpactError) as caught:
                    law_impact.load_file(self.old)
                self.assertEqual(str(caught.exception), f"{self.old}: JSON nesting is too deep")
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)

    def test_controls_keep_their_existing_behaviour(self):
        self.assertEqual(law_impact.load_file(self.old)["jurisdiction"], json.loads(BASE_TEXT)["jurisdiction"])
        self.old.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(law_impact.ImpactError, "missing required field 'schema_version'"):
            law_impact.load_file(self.old)
        self.old.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(law_impact.ImpactError, "invalid JSON"):
            law_impact.load_file(self.old)
        self.old.write_text(BASE_TEXT, encoding="utf-8")
        for flags in ((), ("--json",)):
            self.assertEqual(self.run_main(*flags)[0], 0)

    def test_each_operand_refuses_in_both_modes_before_reporting(self):
        for target, other in ((self.old, self.new), (self.new, self.old)):
            target.write_text(DEEP["extra-object"](), encoding="utf-8")
            other.write_text(BASE_TEXT, encoding="utf-8")
            before = self.snapshot()
            for flags in ((), ("--json",)):
                with self.subTest(target=target.name, flags=flags), \
                        mock.patch.object(law_impact, "report") as report:
                    code, out, err = self.run_main(*flags)
                    self.assertEqual((code, out), (2, ""))
                    self.assertEqual(err, f"Stopped: {target}: JSON nesting is too deep\n")
                    report.assert_not_called()
            self.assertEqual(self.snapshot(), before)

    def test_old_failure_does_not_read_new(self):
        self.old.write_text(deep_array(), encoding="utf-8")
        self.new.write_text(deep_array(), encoding="utf-8")
        real = law_impact._read_package_bytes
        seen = []

        def spy(path):
            seen.append(Path(path))
            return real(path)
        with mock.patch.object(law_impact, "_read_package_bytes", spy):
            code, _, err = self.run_main()
        self.assertEqual((code, seen), (2, [self.old]))
        self.assertIn(str(self.old), err)
        self.assertNotIn(str(self.new), err)

    def test_module_subprocess_returns_two_without_traceback(self):
        for target, other in ((self.old, self.new), (self.new, self.old)):
            target.write_text(deep_array(), encoding="utf-8")
            other.write_text(BASE_TEXT, encoding="utf-8")
            for flags in ([], ["--json"]):
                with self.subTest(target=target.name, flags=flags):
                    result = subprocess.run(
                        [sys.executable, "-B", "-m", "campaign_tool.law", "impact", str(self.old), str(self.new),
                         "--directory", str(self.root), *flags],
                        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, f"Stopped: {target}: JSON nesting is too deep\n")

    def test_decoder_translation_is_fixed_private_and_narrow(self):
        with mock.patch.object(law_impact.json, "loads", side_effect=RecursionError(SENTINEL)):
            with self.assertRaises(law_impact.ImpactError) as caught:
                law_impact.load_file(self.old)
        self.assertEqual(str(caught.exception), f"{self.old}: JSON nesting is too deep")
        self.assertNotIn(SENTINEL, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        # Recursion outside the decoder, and unrelated decoder errors, are not reclassified.
        with mock.patch.object(law, "validate", side_effect=RecursionError(SENTINEL)):
            with self.assertRaises(RecursionError):
                law_impact.load_file(self.old)
        with mock.patch.object(law_impact.json, "loads", side_effect=AssertionError("x")):
            with self.assertRaises(AssertionError):
                law_impact.load_file(self.old)

    def test_depth_refusal_closes_every_descriptor_once(self):
        self.old.write_text(deep_array(), encoding="utf-8")
        opened, closed = [], []
        real_open, real_close = os.open, os.close

        def spy_open(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if Path(path) == self.old:
                opened.append(fd)
            return fd

        def spy_close(fd):
            closed.append(fd)
            return real_close(fd)
        with mock.patch.object(law_impact.os, "open", spy_open), mock.patch.object(law_impact.os, "close", spy_close):
            self.assertEqual(self.run_main()[0], 2)
        self.assertEqual(len(opened), 1)
        self.assertEqual(closed, opened)


if __name__ == "__main__":
    unittest.main()
