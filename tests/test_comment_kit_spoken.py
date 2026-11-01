"""DF2022-14: the comment kit's spoken version fits the organizer-verified speaker limit (synthetic only)."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.content import ContentError, load_content, load_meeting
from campaign_tool.meeting_stage import stage
from campaign_tool.meetings import comment_kit_md
from tests.test_comment_kit_corrections import GOLDEN

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://campaign.example.invalid"
MID = "legistar-examplecity-1001"
STARTS = "2026-10-14T18:00:00-07:00"
TODAY = date(2026, 10, 12)
PART = {"source_url": "https://example.invalid/council/rules", "verified_by": "Synthetic Organizer",
        "verified_at": "2026-10-10"}
TOO_SHORT = "The speaker limit is too short for this kit; use the first sentence and one ask."
FIELD = "participation.speaker_minutes"


def meeting(minutes=None, **extra):
    part = dict(PART, speaker_minutes=minutes) if minutes is not None else None
    return {"body": "Example City Council", "starts_at": STARTS, "agenda_item": "Item 7", "relevance": "alpr_item",
            **({"participation": part} if part else {}), **extra}


def finding(slug, summary, corrections=()):
    return {"title": slug.title(), "summary": summary, "classification": "documented_fact", "confidence": "verified",
            "path": f"/findings/{slug}.html", "corrections": list(corrections),
            "sources": [{"title": "Policy", "locator": "page 3", "sha256": "ab" * 32}]}


def words(n, stem="word"):
    return " ".join(f"{stem}{i}" for i in range(n)) + "."


def two_minute(kit):
    return kit.split("## Two-minute comment\n", 1)[1].split("\n\n", 1)[0].splitlines()


def spoken(kit):
    """(heading, body lines, last line) of the spoken section."""
    lines = kit.split("## Spoken comment ", 1)[1].split("\n\n", 1)[0].splitlines()
    return lines[0], lines[1:-1], lines[-1]


def recount(lines):
    return len(" ".join(lines).split())


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


class SpokenSectionTests(unittest.TestCase):
    def kit(self, minutes, findings, name="Example Campaign"):
        return comment_kit_md(meeting(minutes), findings, name, BASE)

    def test_two_minutes_three_short_findings_all_fit_and_count_matches(self):
        kit = self.kit(2, [finding(f"f{n}", f"Short fact {n}.") for n in range(4)])
        heading, body, last = spoken(kit)
        self.assertEqual(heading, "(2 minutes, about 260 words)")
        self.assertEqual(body, two_minute(kit))
        self.assertEqual(sum(line.startswith("- ") for line in body), 3)
        self.assertEqual(last, f"Word count: {recount(body)} of 260.")
        self.assertLessEqual(recount(body), 260)
        order = [kit.index(h) for h in ("## Two-minute comment", "## Spoken comment", "## Three asks")]
        self.assertEqual(order, sorted(order))

    def test_one_minute_drops_whole_bullets_from_the_end(self):
        kit = self.kit(1, [finding(f"f{n}", words(40, f"w{n}x")) for n in range(3)])
        heading, body, last = spoken(kit)
        self.assertEqual(heading, "(1 minute, about 130 words)")
        full = two_minute(kit)
        kept = [line for line in body if line.startswith("- ")]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept, [line for line in full if line.startswith("- ")][:1])  # whole, never cut
        self.assertEqual(body, [line for line in full if line not in full[3:5]])
        self.assertLessEqual(recount(body), 130)
        self.assertGreater(recount(body) + recount(full[3:4]), 130)  # the next bullet would not have fit
        self.assertEqual(last, f"Word count: {recount(body)} of 130.")

    def test_long_first_bullet_drops_all_bullets_but_keeps_the_count(self):
        kit = self.kit(1, [finding("long", words(90)), finding("short", "Short.")])
        _, body, last = spoken(kit)
        self.assertFalse([line for line in body if line.startswith("- ")])
        self.assertEqual(body, [line for line in two_minute(kit) if not line.startswith("- ")])
        self.assertEqual(last, f"Word count: {recount(body)} of 130.")

    def test_greeting_and_closing_over_budget_gives_only_the_too_short_line(self):
        name = words(120, "Name")
        kit = self.kit(1, [finding("f", "Short.")], name=name)
        lines = kit.split("## Spoken comment ", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual(lines, ["(1 minute, about 130 words)", TOO_SHORT])
        self.assertNotIn("Word count", kit)
        self.assertIn(name, two_minute(kit)[0])  # the two-minute text is untouched
        self.assertIn("Word count:", self.kit(2, [finding("f", "Short.")], name=name))  # 260 words fit

    def test_exact_budget_fits_and_the_corrected_mark_counts(self):
        base = recount(spoken(self.kit(1, [finding("f", "x.")]))[1])
        summary = words(131 - base)  # "x." is one word: this summary makes exactly 130 words
        plain = spoken(self.kit(1, [finding("f", summary)]))
        self.assertEqual(plain[2], "Word count: 130 of 130.")
        corrected = self.kit(1, [finding("f", summary, [{"date": "2026-10-15", "note": "Fixed."}])])
        self.assertIn("(corrected 2026-10-15)", two_minute(corrected)[2])
        _, body, last = spoken(corrected)
        self.assertFalse([line for line in body if line.startswith("- ")])  # 132 words: the bullet is dropped
        roomy = self.kit(2, [finding("f", summary, [{"date": "2026-10-15", "note": "Fixed."}])])
        _, body, last = spoken(roomy)
        self.assertTrue(body[2].endswith("(documented fact, verified) (corrected 2026-10-15)"))
        self.assertEqual(last, "Word count: 132 of 260.")

    def test_zero_findings_holds_the_no_findings_sentence(self):
        kit = self.kit(3, [])
        heading, body, last = spoken(kit)
        self.assertEqual(heading, "(3 minutes, about 390 words)")
        self.assertEqual(body, two_minute(kit))
        self.assertTrue(body[1].startswith("We have requested the policy"))
        self.assertEqual(last, f"Word count: {recount(body)} of 390.")

    def test_without_speaker_minutes_the_kit_is_unchanged(self):
        one = [finding("policy", "No retention period is set.")]
        one[0]["title"] = "Policy lacks retention clause"
        self.assertEqual(comment_kit_md(meeting(), one, "Example Campaign", BASE), GOLDEN)
        stated = dict(meeting(), participation=dict(PART))
        without = comment_kit_md(stated, one, "Example Campaign", BASE)
        self.assertNotIn("Spoken comment", without)
        with_section = self.kit(2, one)
        cut, end = with_section.index("## Spoken comment"), with_section.index("## Three asks")
        self.assertEqual(with_section[:cut] + with_section[end:], without)


class SpeakerMinutesStagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-comment-kit-spoken-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.target = self.meetings / f"{MID}.json"
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Example City Council", "starts_at": STARTS, "agenda_item": "Item 7: ALPR"}]}),
            encoding="utf-8")

    def part_file(self, value):
        path = self.tmp / "participation.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def snapshot(self):
        return {p.name: p.read_bytes() for p in sorted(self.meetings.iterdir())} if self.meetings.is_dir() else None

    def test_bad_values_refused_naming_the_field_and_nothing_written(self):
        before = self.snapshot()
        for bad in (0, 11, -1, "2", 2.5, 2.0, True, False, [2], {"minutes": 2}):
            with self.subTest(value=bad):
                with self.assertRaises(ContentError) as caught:
                    stage(self.root, MID, participation=str(self.part_file(dict(PART, speaker_minutes=bad))),
                          today=TODAY)
                self.assertIn(FIELD, str(caught.exception))
                self.assertEqual(self.snapshot(), before)
                with self.assertRaises(ContentError) as caught:
                    load_meeting({"body": "B", "starts_at": STARTS, "participation": dict(PART, speaker_minutes=bad)},
                                 "meetings/x.json", today=TODAY)
                self.assertIn(FIELD, str(caught.exception))

    def test_null_means_not_stated_and_bounds_are_accepted(self):
        stage(self.root, MID, participation=str(self.part_file(dict(PART, speaker_minutes=None))), today=TODAY)
        document = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertNotIn("Spoken comment", document["comment_kit_md"])
        self.assertNotIn("speaker_minutes", document["participation"])
        for minutes, label in ((1, "(1 minute, about 130 words)"), (10, "(10 minutes, about 1300 words)")):
            stage(self.root, MID, participation=str(self.part_file(dict(PART, speaker_minutes=minutes))),
                  today=TODAY, replace=True)
            document = json.loads(self.target.read_text(encoding="utf-8"))
            self.assertEqual(document["participation"]["speaker_minutes"], minutes)
            self.assertEqual(spoken(document["comment_kit_md"])[0], label)

    def test_hand_written_bad_value_refused_at_build(self):
        self.meetings.mkdir(exist_ok=True)
        self.target.write_text(json.dumps({"body": "B", "starts_at": STARTS,
                                           "participation": dict(PART, speaker_minutes="two")}), encoding="utf-8")
        with self.assertRaises(ContentError) as caught:
            load_content(self.root)
        self.assertIn(FIELD, str(caught.exception))

    def test_cli_stage_then_build_shows_the_spoken_section(self):
        path = self.part_file(dict(PART, speaker_minutes=2))
        result = run("meetings-stage", "--directory", str(self.root), "--id", MID, "--participation", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(document["participation"]["speaker_minutes"], 2)
        loaded = load_meeting(document, f"meetings/{MID}.json")
        self.assertEqual(loaded["participation"]["speaker_minutes"], 2)
        _, body, last = spoken(document["comment_kit_md"])
        self.assertEqual(last, f"Word count: {recount(body)} of 260.")
        build = run("build", "--directory", str(self.root))
        self.assertEqual(build.returncode, 0, build.stderr)
        page = (self.root / "public" / "meetings.html").read_text(encoding="utf-8")
        self.assertIn("Spoken comment (2 minutes, about 260 words)", page)
        self.assertIn(f"Word count: {recount(body)} of 260.", page)


if __name__ == "__main__":
    unittest.main()
