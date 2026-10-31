import contextlib, importlib.util, io, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOLS, FIXTURES = ROOT / "tools", ROOT / "tests" / "fixtures" / "flock"
AGENCY, GOLDEN = "sample-city-police-department", str(FIXTURES / "morrobay-golden.csv")
REFUSED = "finish refused: PUBLIC-OK.csv or QUESTIONS.md already exists\nerrors=1\n"


def tool(name, *args):
    done = subprocess.run([sys.executable, "-B", str(TOOLS / name), *args], capture_output=True, text=True, check=False)
    return done.returncode


class FlockVerifyIoTest(unittest.TestCase):
    def setUp(self):  # the run folder exactly as FlockVerifyTest.setUp builds it
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.assertEqual(tool("flock_facts.py", "import", str(FIXTURES / "compare-facts.csv"), "--store", str(self.store),
                              "--added-by", "tester", "--date", "2026-10-30"), 0)
        shutil.copy(FIXTURES / "law-sample.csv", self.store / "LAW.csv")
        shutil.copy(FIXTURES / "policy-sample.csv", self.store / "agencies" / AGENCY / "POLICY.csv")
        self.run_dir = self.store / "agencies" / AGENCY / "runs" / "2026-10-30"
        self.assertEqual(tool("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", AGENCY,
                              "--date", "2026-10-30", "--provider", "anthropic", "--model", "claude-opus-5-5",
                              "--applicability", "Sample city police; local ALPR policy applies"), 0)
        shutil.copy(FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")
        self.rd = str(self.run_dir)
        spec = importlib.util.spec_from_file_location("_verify_io", TOOLS / "flock_verify.py")
        assert spec and spec.loader
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.real_write = self.mod.write

    def call(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            return self.mod.main(list(argv)), out.getvalue()

    def start(self, provider="openai"):
        return self.call("start", self.rd, "--provider", provider, "--model", "gpt-5-codex")

    def verified(self):
        self.assertEqual(self.start(), (0, "started verify run\n"))
        shutil.copy(FIXTURES / "verify-sample.csv", self.run_dir / "VERIFY.csv")
        return sorted(os.listdir(self.run_dir))

    def failing(self, name, exc):  # module write() that fails only for the named output file
        def write(path, text):
            if path.name == name:
                raise exc
            return self.real_write(path, text)
        return mock.patch.object(self.mod, "write", write)

    def test_happy_path_unchanged(self):
        before = self.verified()
        self.assertEqual(self.call("finish", self.rd, "--golden", GOLDEN), (0, "public_ok=4 questions=2 rejected=1\n"))
        self.assertEqual(sorted(os.listdir(self.run_dir)), sorted(before + ["PUBLIC-OK.csv", "QUESTIONS.md"]))

    def test_second_write_fails_leaves_no_half_pair_and_rerun_works(self):
        before = self.verified()
        with self.failing("QUESTIONS.md", OSError("disk full")):
            self.assertEqual(self.call("finish", self.rd, "--golden", GOLDEN), (1, "io: OSError\n"))
        self.assertEqual(sorted(os.listdir(self.run_dir)), before)
        with self.failing("QUESTIONS.md", OSError("disk full")), mock.patch.object(self.mod.os, "unlink", side_effect=OSError):
            self.assertEqual(self.call("finish", self.rd), (1, "io: OSError\n"))  # cleanup failure keeps the first error
        (self.run_dir / "PUBLIC-OK.csv").unlink()
        self.assertEqual(self.call("finish", self.rd, "--golden", GOLDEN), (0, "public_ok=4 questions=2 rejected=1\n"))
        self.assertTrue((self.run_dir / "QUESTIONS.md").is_file())

    def test_interrupt_on_second_write_reraised_and_cleaned(self):
        before = self.verified()
        with self.failing("QUESTIONS.md", KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            self.call("finish", self.rd)
        self.assertEqual(sorted(os.listdir(self.run_dir)), before)

    def test_first_write_fails_leaves_nothing_new(self):
        before = self.verified()
        with mock.patch.object(self.mod.os, "replace", side_effect=OSError("boom")):
            self.assertEqual(self.call("finish", self.rd, "--golden", GOLDEN), (1, "io: OSError\n"))
        self.assertEqual(sorted(os.listdir(self.run_dir)), before)  # no PUBLIC-OK.csv, QUESTIONS.md or stray .tmp

    def test_existing_output_refused_and_never_cleaned(self):
        before = self.verified()
        for name in ("PUBLIC-OK.csv", "QUESTIONS.md"):
            with self.subTest(name=name):
                (self.run_dir / name).write_bytes(b"keep\n")
                with self.failing("QUESTIONS.md", OSError("disk full")):
                    self.assertEqual(self.call("finish", self.rd, "--golden", GOLDEN), (1, REFUSED))
                self.assertEqual(sorted(os.listdir(self.run_dir)), sorted(before + [name]))
                self.assertEqual((self.run_dir / name).read_bytes(), b"keep\n")
                (self.run_dir / name).unlink()

    def test_missing_golden_named_without_path(self):
        before = self.verified()
        self.assertEqual(self.call("finish", self.rd, "--golden", str(self.store / "no-such.csv")),
                         (1, "io: FileNotFoundError\n"))
        self.assertEqual(sorted(os.listdir(self.run_dir)), before)

    def test_unreadable_verify_csv_named(self):
        before = self.verified()
        for body, name in ((b"finding_id,verdict\n\xff\xfe\n", "UnicodeDecodeError"), (b"a," + b"x" * 200000 + b"\n", "Error")):
            with self.subTest(name=name):
                (self.run_dir / "VERIFY.csv").write_bytes(body)
                self.assertEqual(self.call("--check", self.rd), (1, "io: %s\n" % name))
                self.assertEqual(self.call("finish", self.rd), (1, "io: %s\n" % name))
        self.assertEqual(sorted(os.listdir(self.run_dir)), before)

    def test_start_second_write_fails_then_rerun_works(self):
        with self.failing("VERIFY.csv", OSError("disk full")):
            self.assertEqual(self.start(), (1, "io: OSError\n"))
        self.assertTrue((self.run_dir / "VERIFY.json").is_file())
        self.assertFalse((self.run_dir / "VERIFY.csv").exists())
        self.assertEqual(self.start(), (0, "started verify run\n"))
        self.assertTrue((self.run_dir / "VERIFY.csv").is_file())

    def test_repository_and_family_refusals_unchanged(self):
        with mock.patch.object(self.mod, "REPO", self.store):
            for argv in (("start", self.rd, "--provider", "openai", "--model", "m"), ("finish", self.rd, "--golden", GOLDEN)):
                self.assertEqual(self.call(*argv), (1, "flock-verify: RUNDIR is inside the repository\n"))
        self.assertEqual(self.start("anthropic"), (3, "refused: verifier family equals author family\n"))
        self.assertFalse((self.run_dir / "VERIFY.json").exists())


if __name__ == "__main__":
    unittest.main()
