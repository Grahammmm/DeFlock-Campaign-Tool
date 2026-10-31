"""RF1150-2: `flock_verify.py current RUNDIR` compares PUBLIC-OK.csv/QUESTIONS.md on disk with a fresh finish, read-only."""
import contextlib, csv, importlib.util, io, json, os, shutil, unittest
from pathlib import Path
from tests.flock import test_verify as base

FIX, GOLDEN = base.FIXTURES, base.GOLDEN
TAIL = " | outputs vs a fresh finish, not a review verdict\n"
_spec = importlib.util.spec_from_file_location("_rf1150_verify", base.TOOLS / "flock_verify.py")
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)
with open(FIX / "verify-sample.csv", newline="") as _h:
    _table = list(csv.reader(_h))
SECRET_TEXT = [r[i] for r in _table[1:] for i in (5, 6) if r[i]]  # records_sought and reason values


class VerifyCurrentTest(unittest.TestCase):
    setUp, mutate = base.FlockVerifyTest.setUp, base.FlockVerifyTest.mutate

    def finished(self):
        self.assertEqual(base.run("flock_verify.py", "start", self.rd, "--provider", "openai", "--model", "gpt-5-codex")[0], 0)
        shutil.copy(FIX / "verify-sample.csv", self.run_dir / "VERIFY.csv")
        self.assertEqual(base.run("flock_verify.py", "finish", self.rd, "--golden", GOLDEN)[0], 0)

    def snap(self):
        return sorted((str(p.relative_to(self.run_dir)), p.lstat().st_size, p.lstat().st_mtime_ns) for p in self.run_dir.rglob("*"))

    def current(self, *extra):  # every call: read-only and no records_sought/reason text in stdout
        before = self.snap()
        code, out = base.run("flock_verify.py", "current", self.rd, *extra)
        self.assertEqual(self.snap(), before)
        for text in SECRET_TEXT:
            self.assertNotIn(text, out)
        return code, out

    def test_fresh_is_current_and_repeatable(self):
        self.finished()
        expected = "current: PUBLIC-OK.csv current\ncurrent: QUESTIONS.md current\ncurrent=2 stale=0 missing=0" + TAIL
        self.assertEqual(self.current(), (0, expected))
        self.assertEqual(self.current(), (0, expected))
        self.assertEqual(self.current("--golden", GOLDEN), (0, expected))

    def test_finding_downgraded_after_finish(self):
        self.finished()
        self.mutate(2, verdict="downgrade", new_rating="medium", records_sought="Existing search log field list")
        self.assertEqual(base.run("flock_verify.py", "--check", self.rd)[0], 0)
        code, out = self.current()
        self.assertEqual((code, out.splitlines()), (1, [
            "current: PUBLIC-OK.csv stale", "current: PUBLIC-OK.csv rows on_disk=4 expected=3 missing=0 extra=1",
            "current: QUESTIONS.md stale", "current=0 stale=2 missing=0" + TAIL.rstrip("\n")]))

    def test_row_deleted_by_hand(self):
        self.finished()
        path = self.run_dir / "PUBLIC-OK.csv"
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        path.write_text("".join(lines[:2] + lines[3:]), encoding="utf-8")
        code, out = self.current()
        self.assertEqual(code, 1)
        self.assertIn("current: PUBLIC-OK.csv stale\ncurrent: PUBLIC-OK.csv rows on_disk=3 expected=4 missing=1 extra=0\n", out)
        self.assertIn("current: QUESTIONS.md current\n", out)

    def test_unreadable_or_wrong_header_counts_zero_rows(self):
        self.finished()
        path = self.run_dir / "PUBLIC-OK.csv"
        for data in (b"\xff\xfe not utf-8\n", b"finding_id,topic\nN1,x\n", b""):
            with self.subTest(data=data):
                path.write_bytes(data)
                code, out = self.current()
                self.assertEqual(code, 1)
                self.assertIn("current: PUBLIC-OK.csv stale\ncurrent: PUBLIC-OK.csv rows on_disk=0 expected=4 missing=4 extra=0\n", out)

    def test_trailing_bytes_are_stale(self):
        self.finished()
        with open(self.run_dir / "QUESTIONS.md", "ab") as handle:
            handle.write(b"\n")
        code, out = self.current()
        self.assertEqual(code, 1)
        self.assertTrue(out.endswith("current: QUESTIONS.md stale\ncurrent=1 stale=1 missing=0" + TAIL), out)

    def test_missing_file_and_symlink(self):
        self.finished()
        (self.run_dir / "QUESTIONS.md").unlink()
        self.assertEqual(self.current(), (1, "current: PUBLIC-OK.csv current\ncurrent: QUESTIONS.md missing\n"
                                             "current=1 stale=0 missing=1" + TAIL))
        target = self.store.parent / "target.csv"
        os.replace(self.run_dir / "PUBLIC-OK.csv", target)
        target.chmod(0o000)  # opening it as a non-root user would fail with io: PermissionError
        self.addCleanup(target.chmod, 0o600)
        (self.run_dir / "PUBLIC-OK.csv").symlink_to(target)
        self.assertEqual(self.current(), (1, "current: PUBLIC-OK.csv is a symlink (refused)\ncurrent: QUESTIONS.md missing\n"
                                             "current=0 stale=0 missing=2" + TAIL))
        (self.run_dir / "PUBLIC-OK.csv").unlink()
        (self.run_dir / "PUBLIC-OK.csv").mkdir()  # not a regular file: missing, never read
        self.assertEqual(self.current()[1].splitlines()[0], "current: PUBLIC-OK.csv missing")

    def test_errors_refuse(self):
        self.finished()
        self.mutate(2, reread_page="3")
        code, out = self.current()
        self.assertEqual(code, 1)
        self.assertIn("row 2: reread_page", out)
        self.assertTrue(out.endswith("current refused: errors=1\n"), out)
        self.assertNotIn(" current\n", out)
        (self.run_dir / "VERIFY.csv").unlink()
        self.assertTrue(self.current()[1].endswith("current refused: errors=1\n"))

    def test_family_refusal(self):
        self.finished()
        (self.run_dir / "VERIFY.json").write_text(json.dumps({"schema": 1, "verifier_provider": "Anthropic"}))
        self.assertEqual(self.current(), (3, "refused: verifier family equals author family\n"))
        (self.run_dir / "VERIFY.json").unlink()
        self.assertEqual(self.current(), (3, "refused: family unknown\n"))

    def test_golden(self):
        self.finished()
        bad = self.store.parent / "bad-golden.csv"
        bad.write_text(Path(GOLDEN).read_text(encoding="utf-8") + "no-such-topic,record-gap,x,strong\n", encoding="utf-8")
        self.assertEqual(self.current("--golden", str(bad)),
                         (1, "golden: topic no-such-topic missing\ncurrent refused: errors=1\n"))
        self.assertEqual(self.current("--golden", GOLDEN)[0], 0)

    def test_usage(self):
        self.finished()
        for argv in (("current", self.rd, "--provider", "openai"), ("current", self.rd, "--model", "m"), ("current",),
                     ("current", "--golden", GOLDEN), ("--check", self.rd, "current")):
            with self.subTest(argv=argv):
                self.assertEqual(base.run("flock_verify.py", *argv)[0], 2)

    def test_current_is_not_guarded_inside_repository(self):  # read-only, like --check
        self.finished()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            from unittest import mock
            with mock.patch.object(verify, "REPO", self.store):
                self.assertEqual(verify.main(["current", self.rd]), 0)
        self.assertTrue(out.getvalue().startswith("current: PUBLIC-OK.csv current\n"))

    def test_finish_writes_exactly_the_outputs_texts(self):
        self.assertEqual(base.run("flock_verify.py", "start", self.rd, "--provider", "openai", "--model", "gpt-5-codex")[0], 0)
        shutil.copy(FIX / "verify-sample.csv", self.run_dir / "VERIFY.csv")
        errs, public, questions, counts = verify.outputs(self.run_dir, GOLDEN)
        self.assertEqual((errs, counts), ([], (4, 2, 1)))
        self.assertEqual(base.run("flock_verify.py", "finish", self.rd), (0, "public_ok=4 questions=2 rejected=1\n"))
        self.assertEqual((self.run_dir / "PUBLIC-OK.csv").read_bytes(), public.encode("utf-8"))
        self.assertEqual((self.run_dir / "QUESTIONS.md").read_bytes(), questions.encode("utf-8"))
        self.assertEqual(verify.outputs(self.run_dir, None)[1:], (public, questions, counts))


if __name__ == "__main__":
    unittest.main()
