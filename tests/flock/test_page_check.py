"""RF1700-1: flock_page check - the story on disk against a fresh in-memory build (read-only; stale/missing/link/invalid)."""
import json, os, re, shutil, tempfile, unittest
from pathlib import Path

from tests.flock.test_page import FIX, KEYS, ROOT, run

BASE = "cards=3 admissions=2 sources=3 records_messages=2 dropped_outreach=1"
SENTINELS = ("ZQXCARDSENTINEL", "ZQXSOURCESENTINEL", "ZQXTHREADSENTINEL")
LAST = re.compile(r"^cards=\d+ admissions=\d+ sources=\d+ records_messages=\d+ dropped_outreach=\d+ errors=\d+ superseded=\d+ "
                  r"state=(current|stale|missing|link|invalid)$")


def tail(state, errors=0, superseded=0, counts=BASE):
    return "%s errors=%d superseded=%d state=%s" % (counts, errors, superseded, state)


ZERO = "cards=0 admissions=0 sources=0 records_messages=0 dropped_outreach=0"


class FlockPageCheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.fix = self.tmp / "in"
        shutil.copytree(FIX, self.fix)
        self.data = {k: json.loads((self.fix / (k + ".json")).read_text(encoding="utf-8"))
                     for k in ("cards", "sources", "location", "thread")}
        # 11: sentinels planted in card text, a source line and the thread; all still lint clean
        self.data["cards"]["findings"][1]["text"] = self.data["cards"]["findings"][1]["text"].replace("produced", SENTINELS[0])
        self.data["cards"]["extra_sources"][0]["text"] += " " + SENTINELS[1]
        self.data["thread"]["messages"][2]["body"] += " " + SENTINELS[2]
        self.save()
        self.outdir = self.tmp / "out"
        self.outdir.mkdir()
        self.story = self.outdir / "sample-story.json"
        self.seen = []

    def save(self):
        for k, v in self.data.items():
            (self.fix / (k + ".json")).write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")

    def flags(self, extra):
        return ["--cards", self.fix / "cards.json", "--sources", self.fix / "sources.json", "--location", self.fix / "location.json",
                "--thread", self.fix / "thread.json", "--base-sources", 8, *extra]

    def build(self, *extra, out=None):
        code, out = run("build", *self.flags(extra), "--out", out or self.story)
        self.assertTrue(out.endswith("errors=0\n"), out)
        return code

    def snapshot(self):  # 10: every name, link target, byte and mtime under the temp root
        snap = {}
        for folder, dirs, files in os.walk(self.tmp):
            for name in dirs + files:
                p = Path(folder) / name
                st = p.lstat()
                snap[str(p)] = (st.st_mode, st.st_mtime_ns, os.readlink(p) if p.is_symlink() else
                                p.read_bytes() if p.is_file() else None)
        return snap

    def check(self, *extra, story=None):
        before = self.snapshot()
        code, out = run("check", *self.flags(extra), "--story", story or self.story)
        self.assertEqual(self.snapshot(), before, "check wrote, touched or created something")
        self.seen += out.splitlines()
        return code, out.splitlines()

    def tearDown(self):  # 11: output never carries a sentinel, a path or a card/source/thread string
        for line in self.seen:
            self.assertTrue(LAST.match(line) or re.fullmatch(r"[a-zA-Z.]+ \d+: [^/]+", line), line)
            self.assertNotIn(str(self.tmp), line)
            for s in SENTINELS:
                self.assertNotIn(s, line)

    def test_build_then_check_is_current(self):  # 1
        self.assertEqual(self.build(), 0)
        self.assertIn(SENTINELS[0], self.story.read_text(encoding="utf-8"))
        self.assertEqual(self.check(), (0, [tail("current")]))

    def test_changed_card_meta_or_admission_is_stale_naming_the_key(self):  # 2
        self.assertEqual(self.build(), 0)
        cards = self.data["cards"]
        for key, edit in (("findings", lambda: cards["findings"][0].__setitem__("text", "The contract names 120 partners. "
                                                                                              "The council heard 95, then 90.")),
                          ("admissions", lambda: cards["admissions"][0].__setitem__("date", "2026-09-02")),
                          ("dek", lambda: cards["meta"].__setitem__("dek", "A changed dek."))):
            with self.subTest(key=key):
                old = json.dumps(cards)
                edit()
                self.save()
                msg = "story 0: differs from a fresh build (keys: %s)" % key
                self.assertEqual(self.check(), (1, [msg, tail("stale", 1)]))
                cards.update(json.loads(old))
                self.save()
        self.assertEqual(self.check(), (0, [tail("current")]))

    def test_sources_numbering_change_is_stale_in_extra_sources(self):  # 3
        self.assertEqual(self.build(), 0)
        rows = self.data["sources"]
        rows[0]["n"], rows[1]["n"] = 2, 1
        self.save()
        self.assertEqual(self.check(), (1, ["story 0: differs from a fresh build (keys: extraSources)", tail("stale", 1)]))

    def test_absent_story_is_missing_and_not_created(self):  # 4
        self.assertEqual(self.check(), (1, [tail("missing")]))
        self.assertEqual(list(self.outdir.iterdir()), [])
        gone = self.tmp / "gone" / "sample-story.json"
        self.assertEqual(self.check(story=gone), (1, [tail("missing")]))
        self.assertFalse(gone.parent.exists())

    def test_story_link_is_never_followed(self):  # 5
        real = self.tmp / "real-story.json"
        self.assertEqual(self.build(out=real), 0)
        self.story.symlink_to(real)
        self.assertEqual(self.check(), (1, [tail("link", counts=ZERO)]))
        self.story.unlink()
        self.story.symlink_to(self.tmp / "nowhere.json")  # dangling
        self.assertEqual(self.check(), (1, [tail("link", counts=ZERO)]))
        self.assertFalse((self.tmp / "nowhere.json").exists())

    def test_story_inside_the_repository_is_refused(self):  # 5 (order: link, then repository)
        into = self.tmp / "repo-link"
        into.symlink_to(ROOT / "tests" / "fixtures", target_is_directory=True)
        for story in (ROOT / "tests" / "fixtures" / "sample-story.json", into / "sample-story.json"):
            with self.subTest(story=story.name):
                self.assertEqual(self.check(story=story),
                                 (1, ["out 0: refused inside the repository", tail("invalid", 1, counts=ZERO)]))
        self.assertFalse((ROOT / "tests" / "fixtures" / "sample-story.json").exists())

    def test_unparseable_story_is_stale_unreadable_json(self):  # 6
        self.assertEqual(self.build(), 0)
        good = self.story.read_bytes()
        for bad in (b"{not json", good[: len(good) // 2], b"", b'{"title": "caf\xe9"}'):
            with self.subTest(bad=bad[:12]):
                self.story.write_bytes(bad)
                self.assertEqual(self.check(), (1, ["story 0: unreadable JSON", tail("stale", 1)]))

    def test_same_values_other_bytes_or_shape_is_stale(self):  # 6 (bypass by formatting / wrong top level)
        self.assertEqual(self.build(), 0)
        story = json.loads(self.story.read_text(encoding="utf-8"))
        self.story.write_text(json.dumps(story), encoding="utf-8")
        self.assertEqual(self.check(), (1, ["story 0: differs from a fresh build (keys: none)", tail("stale", 1)]))
        self.story.write_text(json.dumps([story]), encoding="utf-8")
        self.assertEqual(self.check(), (1, ["story 0: differs from a fresh build (keys: %s)" % ", ".join(KEYS), tail("stale", 1)]))
        self.story.unlink()
        self.story.mkdir()
        self.assertEqual(self.check(), (1, ["io 0: IsADirectoryError", tail("invalid", 1)]))

    def test_cards_that_fail_lint_or_load_are_invalid(self):  # 7
        self.assertEqual(self.build(), 0)
        self.data["cards"]["findings"][0]["sources"].append(99)
        self.save()
        code, lines = self.check()
        self.assertEqual((code, lines[0], lines[-1]), (1, "findings 0: R7 unknown source reference", tail("invalid", len(lines) - 1)))
        (self.fix / "cards.json").unlink()
        self.assertEqual(self.check(), (1, ["cards 0: missing", tail("invalid", 1, counts=ZERO)]))
        (self.fix / "thread.json").write_bytes(b"{not json")
        self.assertEqual(self.check(), (1, ["cards 0: missing", "thread 0: not JSON", tail("invalid", 2, counts=ZERO)]))

    def test_not_posted_list_gates_check_like_build(self):  # 8
        self.assertEqual(self.build(), 0)  # built without --not-posted; the story names inventory document 10000001
        code, lines = self.check("--not-posted", "10000001")
        self.assertEqual((code, lines[-1]), (1, tail("invalid", len(lines) - 1)))
        self.assertTrue(all(" R8 not-posted record named" in line for line in lines[:-1]), lines)
        self.assertEqual(self.check("--not-posted-title", "Camera contract")[1][-1].split()[-1], "state=invalid")
        # --not-posted only gates lint and never changes the bytes, so a story built with a list it passes is current without it
        self.story.unlink()
        self.assertEqual(self.build("--not-posted", "10000099"), 0)
        self.assertEqual(self.check(), (0, [tail("current")]))

    def test_superseded_counts_regular_files_only(self):  # 9
        self.assertEqual(self.build(), 0)
        self.data["cards"]["meta"]["dek"] = "A changed dek."
        self.save()
        self.assertEqual(self.check()[1][-1], tail("stale", 1))
        self.assertEqual(self.build("--replace"), 0)
        (self.outdir / "sample-story.json.superseded-fake").symlink_to(self.story)
        (self.outdir / "sample-story.json.superseded-dir").mkdir()
        (self.outdir / "other-story.json.superseded-1").write_text("{}", encoding="utf-8")
        self.assertEqual(self.check(), (0, [tail("current", superseded=1)]))
        self.assertFalse([p for p in self.outdir.iterdir() if p.name.endswith(".tmp")])

    def test_check_takes_no_replace_or_out(self):  # usage is argparse's exit 2
        for extra in (("--replace",), ("--out", self.story)):
            with self.subTest(extra=extra[0]):
                code, out = run("check", *self.flags(extra), "--story", self.story)
                self.assertEqual((code, out), (2, ""))


if __name__ == "__main__":
    unittest.main()
