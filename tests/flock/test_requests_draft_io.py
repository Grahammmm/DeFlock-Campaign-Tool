"""RF1120-2: draft names a missing, unreadable or linked QUESTIONS.md and an unusable RUNDIR; nothing is written."""
import contextlib
import hashlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_requests.py"
FIXTURE = ROOT / "tests" / "fixtures" / "flock" / "requests-sample.csv"
QUESTIONS = FIXTURE.with_name("questions-sample.md")
DATES = ("--from", "2026-01-01", "--to", "2026-10-31")
MAIN_DRAFT_SHA256_HEAD = 6435140101118633223  # first 8 sha256 bytes (big-endian) of main's draft for these fixtures


def run(rundir, requests=FIXTURE):
    return subprocess.run([sys.executable, "-B", str(TOOL), "draft", str(rundir), "--requests", str(requests), *DATES],
                          capture_output=True, text=True, check=False)


def call(repo, rundir):  # in process, with the module's REPO patched to a temp "repository" root
    spec = importlib.util.spec_from_file_location("_requests_rf1120_2", TOOL)
    module, err, out = importlib.util.module_from_spec(spec), io.StringIO(), io.StringIO()
    spec.loader.exec_module(module)
    with mock.patch.object(module, "REPO", Path(repo)), contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        return module.main(["draft", str(rundir), "--requests", str(FIXTURE), *DATES]), err.getvalue(), out.getvalue()


class DraftInputAndFolderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.rundir = self.tmp / "agencies" / "sample-city-police-department" / "runs" / "2026-10-31"
        self.rundir.mkdir(parents=True)
        self.questions = self.rundir / "QUESTIONS.md"

    def listing(self):
        return sorted(p.name for p in self.rundir.iterdir())

    def assertRefused(self, done, message, before):
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", f"draft: {message}\n"))
        self.assertEqual(self.listing(), before)  # no draft and no temp left behind

    def test_happy_path_bytes_and_mode_unchanged(self):
        self.questions.write_bytes(QUESTIONS.read_bytes())
        done = run(self.rundir)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "items=2 held=2 written=FOLLOWUP-DRAFT.md\n", ""))
        out = self.rundir / "FOLLOWUP-DRAFT.md"
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertEqual(int.from_bytes(hashlib.sha256(out.read_bytes()).digest()[:8], "big"), MAIN_DRAFT_SHA256_HEAD)
        self.assertEqual(self.listing(), ["FOLLOWUP-DRAFT.md", "QUESTIONS.md"])

    def test_missing_questions_named(self):
        self.assertRefused(run(self.rundir), "QUESTIONS.md missing", [])

    def test_request_errors_still_win_over_question_problems(self):
        done = run(self.rundir, self.tmp / "no-such.csv")
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("QUESTIONS.md", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(self.listing(), [])

    def test_unreadable_questions_named(self):
        self.questions.mkdir()
        self.assertRefused(run(self.rundir), "QUESTIONS.md unreadable (IsADirectoryError)", ["QUESTIONS.md"])
        self.questions.rmdir()
        self.questions.write_bytes(QUESTIONS.read_bytes() + b"\xff\n")
        self.assertRefused(run(self.rundir), "QUESTIONS.md unreadable (UnicodeDecodeError)", ["QUESTIONS.md"])

    def test_symlinked_questions_refused_target_untouched(self):
        target = self.tmp / "outside-questions.md"
        target.write_bytes(QUESTIONS.read_bytes())
        before = (target.read_bytes(), target.stat().st_mtime_ns)
        self.questions.symlink_to(target)
        self.assertRefused(run(self.rundir), "QUESTIONS.md must not be a symlink", ["QUESTIONS.md"])
        self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)
        self.questions.unlink()
        self.questions.symlink_to(self.tmp / "dangling.md")  # a dangling link is a link, not "missing"
        self.assertRefused(run(self.rundir), "QUESTIONS.md must not be a symlink", ["QUESTIONS.md"])

    def test_rundir_missing_or_file_named_without_traceback(self):
        missing = self.rundir.parent / "2026-11-01"
        done = run(missing)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", "draft: RUNDIR missing or not a folder\n"))
        self.assertFalse(missing.exists())
        afile = self.rundir.parent / "2026-11-02"
        afile.write_text("not a folder\n", encoding="utf-8")
        done = run(afile)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", "draft: RUNDIR missing or not a folder\n"))
        self.assertEqual(afile.read_text(encoding="utf-8"), "not a folder\n")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_read_only_rundir_named_no_temp_left(self):
        self.questions.write_bytes(QUESTIONS.read_bytes())
        self.rundir.chmod(0o500)
        self.addCleanup(self.rundir.chmod, 0o700)
        self.assertRefused(run(self.rundir), "cannot write in RUNDIR (PermissionError)", ["QUESTIONS.md"])

    def test_failed_link_removes_temp(self):
        self.questions.write_bytes(QUESTIONS.read_bytes())
        with mock.patch("os.link", side_effect=OSError(18, "cross-device")):
            self.assertEqual(call(self.tmp / "repo", self.rundir), (1, "draft: cannot write in RUNDIR (OSError)\n", ""))
        self.assertEqual(self.listing(), ["QUESTIONS.md"])

    def test_existing_draft_still_refused_bytes_unchanged(self):
        self.questions.write_bytes(QUESTIONS.read_bytes())
        out = self.rundir / "FOLLOWUP-DRAFT.md"
        out.write_bytes(b"earlier draft\n")
        self.assertRefused(run(self.rundir), "FOLLOWUP-DRAFT.md already exists", ["FOLLOWUP-DRAFT.md", "QUESTIONS.md"])
        self.assertEqual(out.read_bytes(), b"earlier draft\n")

    def test_repo_refusal_precedes_new_checks(self):
        refused = (1, "flock-followup: RUNDIR is inside the repository\n", "")
        afile = self.tmp / "afile"
        afile.write_text("x\n", encoding="utf-8")
        for rundir in (self.tmp / "missing-run", afile, self.rundir):  # missing, a file, and no QUESTIONS.md
            with self.subTest(rundir=rundir.name):
                self.assertEqual(call(self.tmp, rundir), refused)
        self.assertEqual(self.listing(), [])


if __name__ == "__main__":
    unittest.main()
