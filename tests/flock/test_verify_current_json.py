"""RF1240-13: `flock_verify.py current RUNDIR --json` prints one flock-verify-current-v1 document; text and exit codes unchanged."""
import json, os, unittest
from pathlib import Path
from tests.flock import test_verify as base
from tests.flock import test_verify_current as cur

GOLDEN, TAIL, NOTE = cur.GOLDEN, cur.TAIL, "outputs vs a fresh finish, not a review verdict"
CANARY = "Existing canary-7f3q search log field list"
EXTRA_ROW = "N99,extra-topic,record-gap,strong,F1,R1,P1\n"
FRESH = "current: PUBLIC-OK.csv current\ncurrent: QUESTIONS.md current\ncurrent=2 stale=0 missing=0" + TAIL  # origin/main text


def doc(outputs, counts, errors=()):
    return {"schema": "flock-verify-current-v1", "review_required": True, "note": NOTE, "outputs": outputs,
            "counts": counts, "errors": list(errors)}


def out(name, state, symlink=False, rows=None):
    return {"name": name, "state": state, "symlink": symlink, "rows": rows}


def counts(current, stale, missing):
    return {"current": current, "stale": stale, "missing": missing}


class VerifyCurrentJsonTest(unittest.TestCase):
    setUp, mutate, finished, snap, current = (base.FlockVerifyTest.setUp, base.FlockVerifyTest.mutate,
                                              cur.VerifyCurrentTest.finished, cur.VerifyCurrentTest.snap,
                                              cur.VerifyCurrentTest.current)

    def jcurrent(self, *extra):  # read-only, sorted keys with indent=1, no finding text, cell or path in stdout
        before = self.snap()
        code, text = base.run("flock_verify.py", "current", self.rd, "--json", *extra)
        self.assertEqual(self.snap(), before)
        got = json.loads(text)
        self.assertEqual(text, json.dumps(got, sort_keys=True, indent=1, ensure_ascii=False) + "\n")
        for leak in cur.LEAK_TEXT + [CANARY, self.rd, str(self.store), "N99", "extra-topic"]:
            self.assertNotIn(leak, text)
        return code, got

    def test_fresh_run_is_current(self):
        self.finished()
        self.assertEqual(self.jcurrent(), (0, doc([out("PUBLIC-OK.csv", "current"), out("QUESTIONS.md", "current")],
                                                  counts(2, 0, 0))))
        self.assertEqual(self.jcurrent("--golden", GOLDEN)[0], 0)
        self.assertEqual(self.current(), (0, FRESH))

    def test_row_removed_and_extra_row_added(self):
        self.finished()
        path = self.run_dir / "PUBLIC-OK.csv"
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        path.write_text("".join(lines[:2] + lines[3:]) + EXTRA_ROW, encoding="utf-8")
        code, text = self.current()
        self.assertEqual((code, text), (1, "current: PUBLIC-OK.csv stale\ncurrent: PUBLIC-OK.csv rows on_disk=4 expected=4 "
                                           "missing=1 extra=1\ncurrent: QUESTIONS.md current\ncurrent=1 stale=1 missing=0" + TAIL))
        rows = {"on_disk": 4, "expected": 4, "missing": 1, "extra": 1}
        self.assertEqual(self.jcurrent(), (1, doc([out("PUBLIC-OK.csv", "stale", rows=rows), out("QUESTIONS.md", "current")],
                                                  counts(1, 1, 0))))
        self.assertIn("current: PUBLIC-OK.csv rows on_disk=%(on_disk)d expected=%(expected)d missing=%(missing)d extra=%(extra)d\n"
                      % rows, text)

    def test_questions_missing_then_symlink(self):
        self.finished()
        (self.run_dir / "QUESTIONS.md").unlink()
        self.assertEqual(self.jcurrent(), (1, doc([out("PUBLIC-OK.csv", "current"), out("QUESTIONS.md", "missing")],
                                                  counts(1, 0, 1))))
        self.assertEqual(self.current(), (1, "current: PUBLIC-OK.csv current\ncurrent: QUESTIONS.md missing\n"
                                             "current=1 stale=0 missing=1" + TAIL))
        target = self.store.parent / "target.md"
        target.write_text("never read\n", encoding="utf-8")
        target.chmod(0o000)  # following the link as a non-root user would fail with io: PermissionError
        self.addCleanup(target.chmod, 0o600)
        (self.run_dir / "QUESTIONS.md").symlink_to(target)
        self.assertEqual(self.jcurrent(), (1, doc([out("PUBLIC-OK.csv", "current"), out("QUESTIONS.md", "missing", symlink=True)],
                                                  counts(1, 0, 1))))
        self.assertEqual(self.current(), (1, "current: PUBLIC-OK.csv current\ncurrent: QUESTIONS.md is a symlink (refused)\n"
                                             "current=1 stale=0 missing=1" + TAIL))

    def test_undecodable_public_ok_counts_zero_rows(self):
        self.finished()
        (self.run_dir / "PUBLIC-OK.csv").write_bytes(b"\xff\xfe not utf-8\n")
        rows = {"on_disk": 0, "expected": 4, "missing": 4, "extra": 0}
        self.assertEqual(self.jcurrent(), (1, doc([out("PUBLIC-OK.csv", "stale", rows=rows), out("QUESTIONS.md", "current")],
                                                  counts(1, 1, 0))))
        self.assertEqual(self.current()[1].splitlines()[1], "current: PUBLIC-OK.csv rows on_disk=0 expected=4 missing=4 extra=0")

    def test_family_refusal(self):
        self.finished()
        (self.run_dir / "VERIFY.json").write_text(json.dumps({"schema": 1, "verifier_provider": "Anthropic"}))
        self.assertEqual(self.jcurrent(), (3, doc([], None, ["refused: verifier family equals author family"])))
        self.assertEqual(self.current(), (3, "refused: verifier family equals author family\n"))
        (self.run_dir / "VERIFY.json").unlink()
        self.assertEqual(self.jcurrent(), (3, doc([], None, ["refused: family unknown"])))

    def test_malformed_verify_errors_match_text(self):
        self.finished()
        self.mutate(2, reread_page="3")
        code, text = self.current()
        self.assertEqual((code, text.splitlines()[-1]), (1, "current refused: errors=1"))
        self.assertEqual(self.jcurrent(), (1, doc([], None, text.splitlines()[:-1])))
        (self.run_dir / "VERIFY.csv").write_text("not,the,header\n", encoding="utf-8")
        code, text = self.current()
        self.assertEqual(self.jcurrent(), (1, doc([], None, text.splitlines()[:-1])))
        self.assertEqual(self.jcurrent("--golden", GOLDEN)[1]["errors"], text.splitlines()[:-1])

    def test_golden_refusal(self):
        self.finished()
        bad = self.store.parent / "bad-golden.csv"
        bad.write_text(Path(GOLDEN).read_text(encoding="utf-8") + "no-such-topic,record-gap,x,strong\n", encoding="utf-8")
        self.assertEqual(self.jcurrent("--golden", str(bad)), (1, doc([], None, ["golden: topic no-such-topic missing"])))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads mode-000 files")
    def test_io_error_is_class_name_only(self):
        self.finished()
        (self.run_dir / "QUESTIONS.md").chmod(0o000)
        self.addCleanup((self.run_dir / "QUESTIONS.md").chmod, 0o600)
        self.assertEqual(self.jcurrent(), (1, doc([], None, ["io: PermissionError"])))
        self.assertEqual(self.current(), (1, "current: PUBLIC-OK.csv current\nio: PermissionError\n"))

    def test_json_only_with_current(self):
        self.assertEqual(base.run("flock_verify.py", "start", self.rd, "--provider", "openai", "--model", "gpt-5-codex")[0], 0)
        self.mutate(1)
        before = self.snap()
        for argv in (("finish", self.rd, "--json"), ("--check", self.rd, "--json"), ("start", self.rd, "--provider", "openai",
                     "--model", "m", "--json"), ("current", "--json"), ("--json",)):
            with self.subTest(argv=argv):
                self.assertEqual(base.run("flock_verify.py", *argv), (2, ""))
                self.assertEqual(self.snap(), before)
        self.assertFalse((self.run_dir / "PUBLIC-OK.csv").exists())

    def test_canary_reason_never_in_json(self):
        self.assertEqual(base.run("flock_verify.py", "start", self.rd, "--provider", "openai", "--model", "gpt-5-codex")[0], 0)
        self.mutate(2, verdict="downgrade", new_rating="medium", records_sought=CANARY)
        self.assertEqual(base.run("flock_verify.py", "finish", self.rd)[0], 0)
        self.assertIn(CANARY, (self.run_dir / "QUESTIONS.md").read_text(encoding="utf-8"))
        (self.run_dir / "QUESTIONS.md").write_text("edited " + CANARY + "\n", encoding="utf-8")
        self.assertEqual(self.jcurrent(), (1, doc([out("PUBLIC-OK.csv", "current"), out("QUESTIONS.md", "stale")],
                                                  counts(1, 1, 0))))


if __name__ == "__main__":
    unittest.main()
