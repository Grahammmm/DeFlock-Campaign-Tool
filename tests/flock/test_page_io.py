"""RF1130-4: flock_page build/lint name each bad input by flag and kind; a linked or folder-less --out is refused."""
import json, os, re, shutil, stat, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL, FIX = ROOT / "tools" / "flock_page.py", ROOT / "tests" / "fixtures" / "flock" / "page"
HAPPY = "cards=3 admissions=2 sources=3 records_messages=2 dropped_outreach=1 errors=0"
LINE = re.compile(r"^(cards|sources|location|thread|story|out|lint) 0: (missing|unreadable|not JSON|not an object|not a list|"
                  r"is a link; refused|folder missing|refused inside the repository|shape [A-Za-z]+)$")
BUILD = ("cards", "sources", "location", "thread")


class FlockPageIoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.fix = self.tmp / "in"
        shutil.copytree(FIX, self.fix)
        self.paths = {k: self.fix / (k + ".json") for k in BUILD}
        self.outdir = self.tmp / "out"
        self.outdir.mkdir()
        self.out = self.outdir / "sample-story.json"
        self.seen = []

    def run_tool(self, *args):
        done = subprocess.run([sys.executable, "-B", str(TOOL), *map(str, args)], capture_output=True, text=True, check=False)
        self.assertEqual(done.stderr, "", "no traceback")
        self.seen += done.stdout.splitlines()
        return done.returncode, done.stdout.splitlines()

    def build(self, *extra, out=None, **paths):
        p = dict(self.paths, **paths)
        return self.run_tool("build", "--cards", p["cards"], "--sources", p["sources"], "--location", p["location"],
                             "--thread", p["thread"], "--base-sources", 8, "--out", out or self.out, *extra)

    def lint(self, story, **paths):
        p = dict(self.paths, **paths)
        return self.run_tool("lint", "--story", story, "--thread", p["thread"], "--location", p["location"], "--base-sources", 8)

    def assert_refused(self, result, *want):
        code, lines = result
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[:-1], list(want))
        self.assertTrue(lines[-1].endswith(" errors=%d" % len(want)), lines)

    def write(self, name, data):
        (path := self.fix / name).write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
        return path

    def assert_empty_outdir(self):
        self.assertEqual(list(self.outdir.iterdir()), [])

    def tearDown(self):  # 9: output hygiene - only fixed labels and kinds, never a path, card text or fixture string
        def values(v):
            if isinstance(v, (dict, list)):
                yield from (s for x in (v.values() if isinstance(v, dict) else v) for s in values(x))
            elif isinstance(v, str) and len(v) >= 4:
                yield v
        fixture_text = {s for f in FIX.iterdir() for s in values(json.loads(f.read_text(encoding="utf-8")))}
        self.assertIn("The City found no training roster for 2025.", fixture_text)
        for line in self.seen:
            if line.startswith("cards="):  # the counts line is fixed: numbers only
                self.assertRegex(line, r"^cards=\d+ admissions=\d+ sources=\d+ records_messages=\d+ dropped_outreach=\d+ errors=\d+$")
                continue
            self.assertRegex(line, LINE)
            self.assertNotIn(str(self.tmp), line)
            self.assertNotIn("/", line)
            for s in fixture_text:
                self.assertNotIn(s, line)

    def test_each_missing_input_is_named(self):  # 1
        for label in BUILD:
            with self.subTest(label=label):
                self.assert_refused(self.build(**{label: self.fix / "absent.json"}), "%s 0: missing" % label)
                self.assert_empty_outdir()
        self.assert_refused(self.lint(self.fix / "absent.json"), "story 0: missing")
        self.assert_refused(self.lint(self.fix / "absent.json", thread=self.fix / "absent.json"),
                            "story 0: missing", "thread 0: missing")

    def test_every_bad_input_is_reported_in_flag_order(self):  # 2
        bad = self.write("bad.json", b"{not json")
        self.assert_refused(self.build(thread=self.fix / "absent.json", cards=bad), "cards 0: not JSON", "thread 0: missing")
        self.assert_empty_outdir()

    def test_unreadable_undecodable_and_wrong_top_level(self):  # 3
        self.assert_refused(self.build(cards=self.write("latin.json", b'{"slug": "caf\xe9"}')), "cards 0: unreadable")
        self.assert_refused(self.build(location=self.fix), "location 0: unreadable")  # a folder, not a file
        self.assert_refused(self.build(cards=self.write("list.json", [])), "cards 0: not an object")
        self.assert_refused(self.build(sources=self.write("obj.json", {})), "sources 0: not a list")
        self.assert_refused(self.build(thread=self.write("str.json", "text")), "thread 0: not an object")
        self.assert_refused(self.lint(self.write("story.json", [])), "story 0: not an object")
        self.assert_refused(self.build(cards=self.write("empty.json", b"")), "cards 0: not JSON")
        self.assert_empty_outdir()
        if os.geteuid() == 0:
            self.skipTest("root reads mode 000 files")
        locked = self.write("locked.json", {})
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        self.assert_refused(self.build(cards=locked), "cards 0: unreadable")
        self.assert_refused(self.lint(locked), "story 0: unreadable")

    def test_dangling_out_link_is_refused_and_target_not_created(self):  # 4
        target = self.tmp / "elsewhere" / "planted.json"
        target.parent.mkdir()
        self.out.symlink_to(target)
        self.assert_refused(self.build(), "out 0: is a link; refused")
        self.assert_refused(self.build("--replace"), "out 0: is a link; refused")
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.iterdir()), [])
        self.assertEqual(list(self.outdir.iterdir()), [self.out])

    def test_out_link_to_existing_story_is_refused_with_replace(self):  # 5
        real = self.tmp / "real-story.json"
        self.assertEqual(self.build(out=real)[0], 0)
        real.chmod(0o640)
        before = (real.read_bytes(), real.stat().st_ino, stat.S_IMODE(real.stat().st_mode))
        self.out.symlink_to(real)
        self.assert_refused(self.build("--replace"), "out 0: is a link; refused")
        self.assertEqual((real.read_bytes(), real.stat().st_ino, stat.S_IMODE(real.stat().st_mode)), before)
        self.assertEqual(sorted(p.name for p in self.tmp.rglob("*superseded*")), [])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["in", "out", "real-story.json"])

    def test_out_in_missing_folder_is_refused_and_not_created(self):  # 6
        gone = self.tmp / "gone"
        self.assert_refused(self.build(out=gone / "sample-story.json"), "out 0: folder missing")
        self.assert_refused(self.build(out=gone / "deeper" / "sample-story.json"), "out 0: folder missing")
        self.assertFalse(gone.exists())
        afile = self.tmp / "afile"
        afile.write_text("x", encoding="utf-8")
        self.assert_refused(self.build(out=afile / "sample-story.json"), "out 0: folder missing")
        self.assertEqual(afile.read_text(encoding="utf-8"), "x")

    def test_link_above_out_still_resolves_and_repo_refusal_holds(self):
        into_repo = self.tmp / "repo-link"
        into_repo.symlink_to(ROOT / "tests" / "fixtures", target_is_directory=True)
        self.assert_refused(self.build(out=into_repo / "sample-story.json"), "out 0: refused inside the repository")
        self.assertFalse((ROOT / "tests" / "fixtures" / "sample-story.json").exists())
        outside = self.tmp / "out-link"
        outside.symlink_to(self.outdir, target_is_directory=True)
        self.assertEqual(self.build(out=outside / "sample-story.json")[1], [HAPPY])
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)

    def test_wrong_shape_names_the_stage_without_traceback_or_temp_file(self):  # 7
        cards = json.loads((FIX / "cards.json").read_text(encoding="utf-8"))
        cards["admissions"] = "a string"
        self.assert_refused(self.build(cards=self.write("shape.json", cards)), "cards 0: shape AttributeError")
        self.assert_refused(self.build(sources=self.write("rows.json", ["row"])), "cards 0: shape TypeError")
        self.assert_refused(self.build(thread=self.write("nothread.json", {})), "lint 0: shape KeyError")
        self.assert_empty_outdir()
        self.assertEqual(self.build()[1], [HAPPY])
        self.assert_refused(self.lint(self.out, thread=self.write("nothread.json", {})), "lint 0: shape KeyError")
        self.assertEqual(sorted(p.name for p in self.outdir.iterdir()), ["sample-story.json"])

    def test_happy_build_then_lint_prints_the_same_line(self):  # 8
        self.assertEqual(self.build(), (0, [HAPPY]))
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
        self.assertEqual(self.lint(self.out), (0, [HAPPY]))


if __name__ == "__main__":
    unittest.main()
