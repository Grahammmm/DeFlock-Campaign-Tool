"""RF1035-3: flock_compare names io errors, names a missing golden and refuses a symlinked or non-folder runs/run path."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "flock"
AGENCY = "sample-city-police-department"
DATE = "2026-10-30"
SPEC = importlib.util.spec_from_file_location("_rf1035_3_compare", ROOT / "tools" / "flock_compare.py")
compare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compare)


def call(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = compare.main(list(argv))
    return code, out.getvalue() + err.getvalue()


class ScaffoldFolderTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = self.tmp / "store"
        self.agency = self.store / "agencies" / AGENCY
        self.agency.mkdir(parents=True)
        (self.agency / "FACTS.csv").write_text("fact_id\n", encoding="utf-8")
        self.target = self.tmp / "elsewhere"
        self.target.mkdir()

    def scaffold(self):
        return call("scaffold", "--store", str(self.store), "--agency", AGENCY, "--date", DATE,
                    "--provider", "anthropic", "--model", "claude-opus-5-5", "--applicability", "Sample city")

    def assert_refused(self, reason):
        code, out = self.scaffold()
        self.assertEqual((code, out.strip()), (1, "scaffold refused: " + reason))
        self.assertNotIn("Traceback", out)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_clean_store_scaffolds_three_files(self):
        code, out = self.scaffold()
        self.assertEqual(code, 0, out)
        run = self.agency / "runs" / DATE
        self.assertEqual(sorted(p.name for p in run.iterdir()), ["FINDINGS.csv", "FINDINGS.md", "RUN.json"])

    def test_symlinked_run_folder_refused(self):
        (self.agency / "runs").mkdir()
        os.symlink(self.target, self.agency / "runs" / DATE)
        self.assert_refused("run folder is a symlink")

    def test_dangling_run_symlink_refused(self):
        (self.agency / "runs").mkdir()
        os.symlink(self.tmp / "gone", self.agency / "runs" / DATE)
        self.assert_refused("run folder is a symlink")
        self.assertFalse((self.tmp / "gone").exists())

    def test_symlinked_runs_folder_refused(self):
        os.symlink(self.target, self.agency / "runs")
        self.assert_refused("runs folder is a symlink")

    def test_runs_regular_file_refused(self):
        (self.agency / "runs").write_text("x", encoding="utf-8")
        self.assert_refused("runs folder is not a folder")

    def test_run_regular_file_refused(self):
        (self.agency / "runs").mkdir()
        (self.agency / "runs" / DATE).write_text("x", encoding="utf-8")
        self.assert_refused("run folder is not a folder")


class CheckIoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = self.tmp / "store"
        done = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "flock_facts.py"), "import",
                               str(FIXTURES / "compare-facts.csv"), "--store", str(self.store), "--added-by", "tester",
                               "--date", DATE], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stdout)
        shutil.copy(FIXTURES / "law-sample.csv", self.store / "LAW.csv")
        shutil.copy(FIXTURES / "policy-sample.csv", self.store / "agencies" / AGENCY / "POLICY.csv")
        code, out = call("scaffold", "--store", str(self.store), "--agency", AGENCY, "--date", DATE, "--provider",
                         "anthropic", "--model", "claude-opus-5-5", "--applicability", "Sample city")
        self.assertEqual(code, 0, out)
        self.run_dir = self.store / "agencies" / AGENCY / "runs" / DATE
        shutil.copy(FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")

    def check(self, *extra):
        return call("--check", str(self.run_dir), *extra)

    def assert_golden_missing(self, golden):
        code, out = self.check("--golden", str(golden))
        lines = out.splitlines()
        self.assertEqual(code, 1, out)
        self.assertIn("golden: missing or not a regular file", lines)
        self.assertRegex(lines[-1], r"^findings=\d+ errors=\d+$")
        self.assertNotIn("Traceback", out)

    def test_missing_golden_is_named(self):
        self.assert_golden_missing(self.tmp / "nonexistent.csv")

    def test_golden_directory_is_named(self):
        self.assert_golden_missing(self.tmp)

    def test_dangling_golden_link_is_named(self):
        os.symlink(self.tmp / "gone.csv", self.tmp / "golden.csv")
        self.assert_golden_missing(self.tmp / "golden.csv")

    def test_non_utf8_findings_md_named(self):
        (self.run_dir / "FINDINGS.md").write_bytes(b"\xff\xfe")
        self.assertEqual(self.check(), (1, "io: UnicodeDecodeError\n"))

    def test_unreadable_findings_csv_named(self):
        with patch.object(compare, "read_csv", side_effect=PermissionError("/secret/path")):
            code, out = self.check()
        self.assertEqual((code, out), (1, "io: PermissionError\n"))
        self.assertNotIn("secret", out)

    def test_bad_csv_named(self):
        with patch.object(compare, "read_csv", side_effect=compare.csv.Error("row text")):
            self.assertEqual(self.check(), (1, "io: Error\n"))


if __name__ == "__main__":
    unittest.main()
