import json, stat, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL, FIX = ROOT / "tools" / "flock_page.py", ROOT / "tests" / "fixtures" / "flock" / "page"
KEYS = ("title eyebrow headline dek firstRequest asOf programStart programNote programEvents keyDates overview admissions findings "
        "retention retentionCaption milestones extraSources").split()
HAPPY = "cards=3 admissions=2 sources=3 records_messages=2 dropped_outreach=1 errors=0"


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *map(str, args)], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


class FlockPageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.out = self.tmp / "sample-story.json"
        code, out = self.build()
        self.assertEqual(code, 0, out)
        self.story = json.loads(self.out.read_text(encoding="utf-8"))
        self.thread = json.loads((FIX / "thread.json").read_text(encoding="utf-8"))

    def build(self, *extra):
        return run("build", "--cards", FIX / "cards.json", "--sources", FIX / "sources.json", "--location", FIX / "location.json",
                   "--thread", FIX / "thread.json", "--base-sources", 8, "--out", self.out, *extra)

    def lint(self, story=None, thread=None, *extra):
        paths = []
        for name, data in (("story.json", story or self.story), ("thread.json", thread or self.thread)):
            (path := self.tmp / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            paths.append(path)
        return run("lint", "--story", paths[0], "--thread", paths[1], "--location", FIX / "location.json", "--base-sources", 8, *extra)

    def test_build_numbers_cites_hides_unposted_and_supersedes(self):
        self.assertEqual(list(self.story), KEYS)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
        self.assertEqual([a["cite"] for a in self.story["admissions"]], ["[9]", "[10]"])
        self.assertEqual([f["cite"] for f in self.story["findings"]], ["[10, 11]", "[9]", "[9, 10]"])
        self.assertEqual((self.story["retention"][0]["cite"], self.story["milestones"][0]["cite"]), ("[10]", ""))
        self.assertEqual(self.story["extraSources"][:2], [
            "[9] Request R-1, City records held by DeFlock SLO, not posted here.",
            "[10] Request R-1, Camera contract.pdf, page 2; inventory document 10000001."])
        self.assertTrue(self.story["extraSources"][2].startswith("[11] Sample City Council meeting"))
        text = self.out.read_text(encoding="utf-8")
        self.assertNotIn("10000002", text)
        self.assertNotIn("hidden audit log", text.lower())
        self.assertEqual(self.lint()[1].splitlines(), [HAPPY])
        self.assertEqual(self.build()[0], 1)
        self.assertEqual(self.out.read_text(encoding="utf-8"), text)
        self.story["dek"] = "changed"
        self.out.write_text(json.dumps(self.story), encoding="utf-8")
        code, out = self.build("--replace")
        self.assertEqual(code, 0, out)
        old = list(self.tmp.glob("sample-story.json.superseded-*"))
        self.assertEqual(len(old), 1)
        self.assertIn('"changed"', old[0].read_text(encoding="utf-8"))
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8"))["dek"], "One records request and the City files we have posted.")

    def mutations(self):  # -> (expected "<area> <index>: <rule>", story, thread, extra args)
        def story(edit):
            s = json.loads(json.dumps(self.story))
            edit(s)
            return s
        def card(i, key, value):
            return story(lambda s: s["findings"][i].__setitem__(key, value))
        f0 = self.story["findings"][0]["text"]
        thread_r9, thread_hidden = json.loads(json.dumps(self.thread)), json.loads(json.dumps(self.thread))
        thread_r9["messages"][0]["requests"] = ["R-9"]
        thread_hidden["messages"][1]["body"] += " See 10000003."
        return [
            ("findings 1: R4", card(1, "figure", self.story["findings"][0]["figure"]), None, ()),
            ("findings 1: R4", card(1, "text", f0.replace("lists", "shows")), None, ()),
            ("findings 0: R4", card(0, "figure", "5 cut"), None, ()),
            ("findings 0: R7", card(0, "cite", ""), None, ()),
            ("findings 0: R7", card(0, "cite", "[12]"), None, ()),
            ("extraSources 3: R7", story(lambda s: s["extraSources"].append("[12] Unused source.")), None, ()),
            ("findings 0: R1", card(0, "text", f0 + " Our outreach list grew."), None, ()),
            ("thread.messages 0: R1", None, thread_r9, ()),
            ("thread.messages 1: R8", None, thread_hidden, ("--not-posted", "10000003")),
            ("findings 0: R8", card(0, "text", f0 + " See inventory document 10000004."), None, ()),
            ("findings 0: R9", card(0, "text", f0 + " Write to name@city.example today."), None, ()),
            ("findings 0: tone", card(0, "text", f0 + " The City violated it."), None, ()),
            ("programEvents 0: R2", story(lambda s: s.__setitem__("programStart", "2025-02-01")), None, ()),
            ("keyDates 0: R3", story(lambda s: s["keyDates"][0].__setitem__("date", "2026-02-02")), None, ()),
        ]

    def test_lint_fails_closed_on_each_single_mutation(self):
        for want, story, thread, extra in self.mutations():
            with self.subTest(want=want):
                code, out = self.lint(story, thread, *extra)
                self.assertEqual(code, 1, out)
                self.assertTrue(any(line.startswith(want) for line in out.splitlines()), out)
                self.assertRegex(out.splitlines()[-1], r"^cards=\d+ admissions=\d+ sources=\d+ records_messages=\d+ "
                                                       r"dropped_outreach=\d+ errors=[1-9]\d*$")

    def test_lint_output_never_echoes_card_text(self):
        secrets = {v for c in self.story["findings"] + self.story["admissions"] + self.story["retention"] + self.story["milestones"]
                   for k, v in c.items() if k in ("text", "label")}
        seen = []
        for _want, story, thread, extra in self.mutations():
            seen += self.lint(story, thread, *extra)[1].splitlines()
            for c in (story or {}).get("findings", ()):
                secrets.add(c["text"]), secrets.add(c["label"])
        self.assertGreater(len(seen), 14)
        for line in seen:
            for value in secrets:
                self.assertNotIn(value, line)


if __name__ == "__main__":
    unittest.main()
