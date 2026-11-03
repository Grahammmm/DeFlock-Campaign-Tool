"""RF0310-7: `stage RUNDIR --copy COPY.json --dry-run` runs every stage check and plan, prints what would be staged, writes nothing."""
import json, os, subprocess, sys, unittest
from unittest import mock
from tests.flock import test_publish as base
from tests.flock import test_publish_stage as real

DRY = "stage (dry run) | approved=%d would_stage=%d skipped_pending=%d | nothing written\n"
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


class PublishStageDryRunTest(real.PublishStageTest):  # setUp: N1 approved (duty-concern), copy.json with its block
    def cli(self, *argv):  # every run is checked for output hygiene, as the real stage tests do
        done = subprocess.run([sys.executable, "-B", str(base.TOOLS / "flock_publish.py"), *argv],
                              capture_output=True, text=True, check=False)
        self.assertNotRegex(done.stdout + done.stderr, r"[0-9a-fA-F]{64}")
        for f in self.doc["findings"]:
            for secret in [f["text"]] + [c["label"] for c in f["cites"]]:
                self.assertNotIn(secret, done.stdout + done.stderr)
        return done.returncode, done.stdout

    def dry(self, *extra, copy=None):
        return self.cli("stage", self.rd, "--copy", str(copy or self.copy), "--dry-run", *extra)

    def snapshot(self):  # paths, mtimes and sizes of the temp parent (run folder, COPY.json folder, store)
        return {str(p.relative_to(self.tmp)): (p.stat().st_mtime_ns, p.stat().st_size, p.is_dir())
                for p in [self.tmp, *self.tmp.rglob("*")]}

    def same_as_real(self, code, *lines):  # dry and real refusals are byte-identical; neither writes
        dry, done = self.dry(), self.stage()
        self.assertEqual(dry, done)
        self.assert_refused(dry, *lines, code=code)

    def test_would_stage_lines_match_a_real_stage(self):
        self.approve("O1")
        self.write_copy({"N1": real.block("search-purpose", classification="documented_fact"), "O1": real.block("outside-access")})
        before = self.snapshot()
        code, text = self.dry()
        self.assertEqual((code, text), (0, "would stage N1 -> content/findings/search-purpose.json\n"
                                           "would stage O1 -> content/findings/outside-access.json\n" + DRY % (2, 2, 2)))
        self.assertEqual(self.snapshot(), before)
        code, staged = self.stage()
        self.assertEqual((code, staged.splitlines()[-1] + "\n"), (0, real.FINAL % (2, 2, 2)))
        self.assertEqual([line.replace("would stage", "staged") for line in text.splitlines()[:-1]], staged.splitlines()[:-1])
        manifest = json.loads((self.staging / "STAGE-MANIFEST.json").read_text())
        self.assertEqual([(r["finding_id"], r["slug"]) for r in manifest["staged"]], [("N1", "search-purpose"), ("O1", "outside-access")])
        self.assertEqual(sorted(p.name for p in (self.staging / "content" / "findings").iterdir()),
                         ["outside-access.json", "search-purpose.json"])

    def test_nothing_written_and_no_write_open(self):
        before, opened, real_open = self.snapshot(), [], os.open

        def spy(path, flags, *rest, **kw):
            opened.append(flags)
            return real_open(path, flags, *rest, **kw)
        with mock.patch("os.open", side_effect=spy):
            code, text = self.call(self.tmp / "repo", "stage", self.rd, "--copy", str(self.copy), "--dry-run")
        self.assertEqual((code, text), (0, "would stage N1 -> content/findings/search-purpose.json\n" + DRY % (1, 1, 3)), text)
        self.assertEqual([f for f in opened if f & WRITE_FLAGS], [])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.staging.exists())
        with mock.patch("os.open", side_effect=spy):  # control: the same spy sees a real stage's write opens
            self.assertEqual(self.call(self.tmp / "repo", "stage", self.rd, "--copy", str(self.copy), "--out", str(self.staging))[0], 0)
        self.assertEqual(len([f for f in opened if f & os.O_CREAT]), 2)

    def test_direct_call_takes_no_out(self):
        stage = real.load_stage()
        with mock.patch.object(stage, "write", side_effect=AssertionError("write called")), mock.patch("builtins.print") as out:
            self.assertEqual(stage.stage(self.run_dir, self.copy, None, self.tmp / "repo", dry_run=True), 0)
        self.assertEqual(out.call_args_list[-1].args[0] + "\n", DRY % (1, 1, 3))

    def test_missing_limitations_same_refusal(self):
        self.approve("O1")
        self.write_copy({"N1": real.block("search-purpose", classification="documented_fact"),
                         "O1": {k: v for k, v in real.block("outside-access").items() if k != "limitations"}})
        self.same_as_real(1, "copy O1: keys differ from the copy schema")
        self.write_copy({"N1": real.block("search-purpose", classification="documented_fact", limitations=[]), "O1": real.block("o")})
        self.same_as_real(1, "copy N1: limitations needs at least one non-empty text")

    def test_publish_check_failure_same_refusal(self):
        self.doc["findings"][0]["text"] = "Changed."
        self.out.write_text(json.dumps(self.doc))
        self.same_as_real(1, "finding N1: text differs from the run", "PUBLISH.json: check failed")
        self.out.unlink()
        self.same_as_real(1, "PUBLISH.json: missing")

    def test_same_family_exit_3(self):
        (self.run_dir / "VERIFY.json").write_text(json.dumps({"schema": 1, "verifier_provider": "anthropic"}))
        self.same_as_real(3, "refused: verifier family equals author family")

    def test_nothing_approved_same_outcome(self):
        self.doc["findings"][0]["review"] = {"status": "pending", "approved_by": "", "approved_on": "", "approval_ref": ""}
        self.out.write_text(json.dumps(self.doc))
        self.same_as_real(1, "PUBLISH.json: nothing approved")

    def test_copy_inside_repository_refused(self):
        code, text = self.dry(copy=base.ROOT / "README.md")
        self.assertEqual((code, "--copy is inside the repository\n" in text), (1, True), text)
        self.assertRegex(text, r"stage refused \| errors=1 \| nothing written\n$")
        fake = self.tmp / "fake-repo"
        fake.mkdir()
        (fake / "copy.json").write_text(self.copy.read_text())
        code, text = self.call(fake, "stage", self.rd, "--copy", str(fake / "copy.json"), "--dry-run")
        self.assertEqual(code, 1, text)
        self.assertIn("--copy is inside the repository\n", text)

    def test_usage_errors_create_nothing(self):
        before = self.snapshot()
        for argv in (("stage", self.rd, "--copy", str(self.copy), "--dry-run", "--out", str(self.staging)),
                     ("stage", self.rd, "--dry-run"), ("stage", "--copy", str(self.copy), "--dry-run"),
                     ("stage", self.rd, "--copy", str(self.copy), "--dry-run", "--today", "2026-10-31"),
                     ("stage", self.rd, "--copy", str(self.copy), "--dry-run", "--site-slug", "sample"),
                     ("status", str(self.store), "--dry-run"), ("brief", str(self.store), "--agency", base.AGENCY, "--out",
                                                                str(self.tmp / "b.md"), "--dry-run"),
                     ("export", self.rd, "--site-slug", "sample", "--dry-run")):
            with self.subTest(argv=argv):
                self.assertEqual(base.run("flock_publish.py", *argv), (2, ""))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.staging.exists())

    def test_recheck_dry_run_unchanged(self):
        before = self.out.read_bytes()
        code, text = base.run("flock_publish.py", "recheck", self.rd, "--today", "2026-10-31", "--dry-run")
        self.assertEqual(code, 0, text)
        self.assertTrue(text.startswith("recheck"), text)
        self.assertEqual(self.out.read_bytes(), before)


for _name in [n for n in dir(real.PublishStageTest) if n.startswith("test_")]:
    setattr(PublishStageDryRunTest, _name, None)  # the real stage tests run once, in tests/flock/test_publish_stage.py

if __name__ == "__main__":
    unittest.main()
