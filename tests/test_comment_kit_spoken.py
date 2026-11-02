"""DF2022-14: the comment kit's spoken version is sized to the organizer-verified speaker limit only.

Synthetic only: fixture findings and temp copies of examples/fictional-campaign; nothing is fetched or sent.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from campaign_tool.content import ContentError, load_content, load_meeting
from campaign_tool.meeting_stage import stage
from campaign_tool.meetings import comment_kit_md
from tests.test_comment_kit_corrections import GOLDEN, MEETING, TWO, finding

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://campaign.example.invalid"
TODAY = date(2026, 10, 12)
PART = {"source_url": "https://example.invalid/council/rules", "verified_by": "Synthetic Organizer",
        "verified_at": "2026-10-10"}
TOO_SHORT = "The speaker limit is too short for this kit; use the first sentence and one ask."


def kit_for(minutes, findings, campaign="C"):
    meeting = dict(MEETING, participation=dict(PART, speaker_minutes=minutes))
    return comment_kit_md(meeting, findings, campaign, BASE)


def section(kit, heading):
    return kit.split(heading + "\n", 1)[1].split("\n\n", 1)[0].splitlines()


def spoken(kit):
    head = [line for line in kit.splitlines() if line.startswith("## Spoken comment (")]
    return head, (section(kit, head[0]) if head else [])


def recount(lines):
    return sum(len(line.split()) for line in lines if not line.startswith("Word count: "))


def words(n, tag):
    return " ".join(f"{tag}{i}" for i in range(n)) + "."


class SpokenSectionTests(unittest.TestCase):
    def assert_counted(self, lines, budget):
        self.assertTrue(lines[-1].startswith("Word count: "), lines)
        self.assertEqual(lines[-1], f"Word count: {recount(lines)} of {budget}.")
        self.assertLessEqual(recount(lines), budget)

    def test_two_minutes_three_short_findings_all_fit(self):
        findings = [finding(f"f{n}", summary=f"Short finding {n}.") for n in range(3)]
        kit = kit_for(2, findings)
        head, lines = spoken(kit)
        self.assertEqual(head, ["## Spoken comment (2 minutes, about 260 words)"])
        self.assertEqual([l for l in lines if l.startswith("- ")], [l for l in section(kit, "## Two-minute comment")
                                                                    if l.startswith("- ")])
        self.assertEqual(len([l for l in lines if l.startswith("- ")]), 3)
        self.assert_counted(lines, 260)
        self.assertEqual(lines[:-1], section(kit, "## Two-minute comment"))  # same greeting and closing
        order = [kit.index(h) for h in ("## Two-minute comment", "## Spoken comment", "## Three asks")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(kit.count("## Two-minute comment\n"), 1)

    def test_one_minute_drops_whole_bullets_from_the_end(self):
        findings = [finding(f"f{n}", summary=words(40, f"long{n}x")) for n in range(3)]
        kit = kit_for(1, findings)
        head, lines = spoken(kit)
        self.assertEqual(head, ["## Spoken comment (1 minute, about 130 words)"])
        full = [l for l in section(kit, "## Two-minute comment") if l.startswith("- ")]
        kept = [l for l in lines if l.startswith("- ")]
        self.assertEqual(len(full), 3)
        self.assertEqual(kept, full[:1])  # never cut mid-way: the first bullet whole, the rest dropped
        self.assert_counted(lines, 130)
        self.assertGreater(recount(lines) + len(full[1].split()), 130)

    def test_drop_is_from_the_end_not_the_longest(self):
        findings = [finding("a", summary=words(53, "a")), finding("b", summary="B."), finding("c", summary="C.")]
        kept = [l for l in spoken(kit_for(1, findings))[1] if l.startswith("- ")]
        self.assertEqual([l.split(" (")[0] for l in kept], ["- " + words(53, "a"), "- B."])  # 131 words with C

    def test_only_greeting_and_closing_fit_then_too_short(self):
        lines = spoken(kit_for(1, [finding("a", summary=words(70, "w"))]))[1]
        self.assertFalse([l for l in lines if l.startswith("- ")])
        self.assert_counted(lines, 130)
        self.assertEqual(lines[-1], "Word count: 64 of 130.")
        kit = kit_for(1, [finding("a")], campaign=words(120, "Name"))
        self.assertEqual(spoken(kit)[1], [TOO_SHORT])
        self.assertNotIn("Word count", kit)
        self.assertEqual(len([l for l in section(kit, "## Two-minute comment") if l.startswith("- ")]), 1)
        self.assert_counted(spoken(kit_for(10, [finding("a")], campaign=words(120, "Name")))[1], 1300)

    def test_corrected_mark_stays_and_counts(self):
        plain = spoken(kit_for(2, [finding("p")]))[1]
        fixed = spoken(kit_for(2, [finding("p", TWO)]))[1]
        self.assertIn("- No retention period is set. (documented fact, verified) (corrected 2026-10-15)", fixed)
        self.assert_counted(fixed, 260)
        self.assertEqual(recount(fixed), recount(plain) + 2)

    def test_zero_findings_keeps_the_no_findings_sentence(self):
        kit = kit_for(1, [])
        lines = spoken(kit)[1]
        self.assertEqual(lines[:-1], section(kit, "## Two-minute comment"))
        self.assertTrue(lines[1].startswith("We have requested the policy, the vendor agreement"))
        self.assert_counted(lines, 130)

    def test_without_a_verified_limit_nothing_is_guessed(self):
        self.assertEqual(comment_kit_md(MEETING, [finding("policy")], "Example Campaign", BASE), GOLDEN)
        self.assertEqual(comment_kit_md(dict(MEETING, participation=None), [finding("policy")],
                                        "Example Campaign", BASE), GOLDEN)
        for raw in (None, 0, 11, "2", 2.5, True, -1):  # unvalidated input reaching the kit adds nothing
            with self.subTest(raw=raw):
                kit = comment_kit_md(dict(MEETING, participation=dict(PART, speaker_minutes=raw)),
                                     [finding("policy")], "Example Campaign", BASE)
                self.assertNotIn("Spoken comment", kit)
                self.assertNotIn("Word count", kit)
        self.assertNotIn("Spoken comment", comment_kit_md(dict(MEETING, participation=PART), [], "C"))


class SpeakerMinutesValidationTests(unittest.TestCase):
    def load(self, value):
        document = {"body": "Example City Council", "starts_at": "2026-10-14T18:00:00-07:00",
                    "participation": dict(PART, speaker_minutes=value)}
        return load_meeting(document, "meetings/x.json", today=TODAY)

    def test_bad_values_refused_naming_the_field(self):
        for value in (0, 11, -1, "2", 2.5, 2.0, True, False, [2], {"minutes": 2}):
            with self.subTest(value=value):
                with self.assertRaises(ContentError) as caught:
                    self.load(value)
                self.assertIn("participation.speaker_minutes", str(caught.exception))

    def test_bounds_accepted_and_null_means_not_stated(self):
        self.assertEqual(self.load(1)["participation"]["speaker_minutes"], 1)
        self.assertEqual(self.load(10)["participation"]["speaker_minutes"], 10)
        self.assertNotIn("speaker_minutes", self.load(None)["participation"])


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


class StageAndBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-comment-kit-spoken-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.today = date.today()  # build validates verified_at against the real date
        self.mid = "legistar-examplecity-3001"
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": self.mid, "body": "Example City Council", "agenda_item": "Item 7: ALPR",
             "starts_at": f"{self.today + timedelta(days=5)}T18:00:00-07:00"}]}), encoding="utf-8")
        self.target = self.root / "content" / "meetings" / f"{self.mid}.json"

    def part_file(self, minutes):
        path = self.tmp / "participation.json"
        path.write_text(json.dumps(dict(PART, verified_at=str(self.today), speaker_minutes=minutes)), encoding="utf-8")
        return str(path)

    def test_staged_with_speaker_minutes_validates_and_builds(self):
        result = run("meetings-stage", "--directory", str(self.root), "--id", self.mid,
                     "--participation", self.part_file(3))
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(document["participation"]["speaker_minutes"], 3)
        self.assertIn("## Spoken comment (3 minutes, about 390 words)", document["comment_kit_md"])
        staged = [m for m in load_content(self.root)["meetings"] if m["id"] == self.mid]
        self.assertEqual(staged[0]["participation"]["speaker_minutes"], 3)
        build = run("build", "--directory", str(self.root))
        self.assertEqual(build.returncode, 0, build.stderr)
        page = (self.root / "public" / "meetings.html").read_text(encoding="utf-8")
        self.assertIn("Spoken comment (3 minutes, about 390 words)", page)
        self.assertIn("Word count: ", page)

    def test_bad_value_refused_before_anything_is_written(self):
        with self.assertRaises(ContentError) as caught:
            stage(self.root, self.mid, participation=self.part_file("3"))
        self.assertIn("participation.speaker_minutes", str(caught.exception))
        self.assertFalse(self.target.exists())
        result = run("meetings-stage", "--directory", str(self.root), "--id", self.mid,
                     "--participation", self.part_file(0))
        self.assertEqual(result.returncode, 1)
        self.assertIn("speaker_minutes", result.stderr)
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
