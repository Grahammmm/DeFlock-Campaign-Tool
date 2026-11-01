"""NB-DF-14: organizer-verified participation facts on staged meetings; the kit never guesses (synthetic only)."""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.content import ContentError, load_content, load_meeting
from campaign_tool.meeting_stage import MeetingStageError, stage

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
MID = "legistar-examplecity-1001"
STARTS = "2026-10-14T18:00:00-07:00"
TODAY = date(2026, 10, 12)
NOT_VERIFIED = "Check the posted agenda for how and when to comment; participation details were not verified."
FULL = {"in_person": True,
        "remote": {"available": True, "how": "Zoom link on the posted agenda"},
        "written_comment": {"how": "Email the city clerk", "deadline": "2026-10-14T12:00:00-07:00"},
        "source_url": "https://example.invalid/council/agenda-2026-10-14",
        "verified_by": "Synthetic Organizer", "verified_at": "2026-10-10"}
SOURCE_ONLY = {"in_person": None, "remote": None, "written_comment": None,
               "source_url": "https://example.invalid/council/agenda", "verified_by": "Synthetic Organizer",
               "verified_at": "2026-10-10"}


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


def snapshot(folder):
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())} if folder.is_dir() else None


def section(kit):
    return kit.split("## How to take part\n", 1)[1].split("\n\n## Sources", 1)[0].splitlines()


class MeetingParticipationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-participation-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.target = self.meetings / f"{MID}.json"
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Example City Council", "starts_at": STARTS, "agenda_item": "Item 7: ALPR",
             "agenda_url": "https://example.invalid/agenda"}]}), encoding="utf-8")

    def part_file(self, value, name="participation.json"):
        path = self.tmp / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def stage_with(self, value, today=TODAY):
        return stage(self.root, MID, participation=str(self.part_file(value)), today=today)

    def document(self):
        return json.loads(self.target.read_text(encoding="utf-8"))

    def assert_refused(self, value, needle, error=ContentError, today=TODAY):
        before = snapshot(self.meetings)
        with self.assertRaises(error) as caught:
            self.stage_with(value, today=today)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(snapshot(self.meetings), before, "nothing may be written")

    def test_full_object_lists_each_fact_and_round_trips(self):
        self.stage_with(FULL)
        document = self.document()
        self.assertEqual(document["participation"], FULL)
        self.assertEqual(section(document["comment_kit_md"]), [
            "- In person: yes", "- Remote: yes, Zoom link on the posted agenda",
            "- Written comment: Email the city clerk, by 2026-10-14 12:00 (UTC-07:00)",
            "Source: https://example.invalid/council/agenda-2026-10-14 (checked 2026-10-10)"])
        self.assertLess(document["comment_kit_md"].index("## Three asks"),
                        document["comment_kit_md"].index("## How to take part"))
        loaded = load_meeting(json.loads(json.dumps(document)), "meetings/x.json", today=TODAY)
        self.assertEqual(loaded["participation"], FULL)
        staged = [m for m in load_content(self.root)["meetings"] if m["id"] == MID][0]
        self.assertEqual(staged["participation"], FULL)

    def test_false_answers_render_no(self):
        value = dict(FULL, in_person=False, remote={"available": False, "how": "ignored when not offered"})
        self.stage_with(value)
        lines = section(self.document()["comment_kit_md"])
        self.assertEqual(lines[:2], ["- In person: no", "- Remote: no"])

    def test_source_only_has_only_the_source_line(self):
        self.stage_with(SOURCE_ONLY)
        kit = self.document()["comment_kit_md"]
        self.assertEqual(section(kit), ["Source: https://example.invalid/council/agenda (checked 2026-10-10)"])
        for invented in ("In person", "Remote", "Written comment", NOT_VERIFIED):
            self.assertNotIn(invented, kit)
        omitted = {k: v for k, v in SOURCE_ONLY.items() if v is not None}
        stage(self.root, MID, participation=str(self.part_file(omitted)), today=TODAY, replace=True)
        self.assertEqual(section(self.document()["comment_kit_md"]), section(kit))

    def test_without_flag_fixed_line_and_no_deadline(self):
        result = run("meetings-stage", "--directory", str(self.root), "--id", MID)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = self.document()
        self.assertNotIn("participation", document)
        self.assertEqual(section(document["comment_kit_md"]), [NOT_VERIFIED])
        self.assertNotIn(", by ", document["comment_kit_md"])
        self.assertNotIn("deadline", document["comment_kit_md"].lower())

    def test_restage_without_flag_drops_stale_participation(self):
        self.stage_with(FULL)
        stage(self.root, MID, replace=True)
        document = self.document()
        self.assertNotIn("participation", document)
        self.assertEqual(section(document["comment_kit_md"]), [NOT_VERIFIED])

    def test_cli_flag_writes_participation(self):
        path = self.part_file(dict(FULL, verified_at="2026-10-01"))
        result = run("meetings-stage", "--directory", str(self.root), "--id", MID, "--participation", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.document()["participation"]["verified_at"], "2026-10-01")
        bad = self.part_file(dict(FULL, phone="555-0100"), "bad.json")
        result = run("meetings-stage", "--directory", str(self.root), "--id", MID, "--participation", str(bad),
                     "--replace")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped:", result.stderr)
        self.assertIn("phone", result.stderr)

    def test_bad_deadlines_refused_naming_the_field(self):
        field = "participation.written_comment.deadline"
        for deadline in ("2026-10-14T19:00:00-07:00", STARTS, "2026-10-15T01:00:00Z", "2026-10-14T12:00:00",
                         "tomorrow", 20261014):
            with self.subTest(deadline=deadline):
                self.assert_refused(dict(FULL, written_comment={"how": "Email", "deadline": deadline}), field)
        self.assert_refused(dict(FULL, written_comment={"how": "Email"}), "participation.written_comment")
        self.stage_with(dict(FULL, written_comment={"how": "Email the clerk", "deadline": None}))
        self.assertIn("- Written comment: Email the clerk", section(self.document()["comment_kit_md"]))
        self.assertNotIn(", by ", self.document()["comment_kit_md"])

    def test_source_and_verification_rules(self):
        cases = [(dict(FULL, source_url="http://example.invalid/agenda"), "participation.source_url"),
                 ({k: v for k, v in FULL.items() if k != "source_url"}, "source_url"),
                 (dict(FULL, source_url="https://user@example.invalid/a"), "participation.source_url"),
                 (dict(FULL, verified_by=""), "participation.verified_by"),
                 (dict(FULL, verified_by="   "), "participation.verified_by"),
                 ({k: v for k, v in FULL.items() if k != "verified_by"}, "verified_by"),
                 (dict(FULL, verified_at="2026-10-13"), "participation.verified_at"),
                 (dict(FULL, verified_at="2026-09-13"), "participation.verified_at"),
                 (dict(FULL, verified_at="2026-10-10T09:00:00-07:00"), "participation.verified_at"),
                 ({k: v for k, v in FULL.items() if k != "verified_at"}, "verified_at")]
        for value, needle in cases:
            with self.subTest(needle=needle, value=value):
                self.assert_refused(value, needle)
        self.stage_with(dict(FULL, verified_at="2026-09-14"))  # exactly 30 days before is still fresh
        self.target.unlink()
        self.stage_with(dict(FULL, verified_at="2026-10-12"), today=TODAY)  # checked today
        self.assertTrue(self.target.exists())

    def test_unknown_keys_and_wrong_types_refused(self):
        cases = [(dict(FULL, phone="555-0100"), "phone"),
                 (dict(FULL, remote={"available": True, "how": "Zoom", "phone": "555-0100"}), "phone"),
                 (dict(FULL, in_person="yes"), "participation.in_person"),
                 (dict(FULL, in_person=1), "participation.in_person"),
                 (dict(FULL, remote={"how": "Zoom"}), "participation.remote"),
                 (dict(FULL, remote={"available": "true"}), "participation.remote.available"),
                 (dict(FULL, remote="Zoom"), "participation.remote"),
                 (dict(FULL, remote={"available": True, "how": "x" * 201}), "participation.remote.how"),
                 (dict(FULL, written_comment={"how": "y" * 201, "deadline": None}),
                  "participation.written_comment.how"),
                 (["not", "an", "object"], "participation")]
        for value, needle in cases:
            with self.subTest(needle=needle):
                self.assert_refused(value, needle)

    def test_oversized_symlinked_or_malformed_file_refused(self):
        before = snapshot(self.meetings)
        big = self.tmp / "big.json"
        big.write_text(json.dumps(dict(FULL, verified_by="z" * (64 * 1024))), encoding="utf-8")
        real = self.part_file(FULL, "real.json")
        link = self.tmp / "link.json"
        os.symlink(real, link)
        garbage = self.tmp / "garbage.json"
        garbage.write_bytes(b"\xff\xfe not json")
        for path in (big, link, garbage, self.tmp / "missing.json", self.tmp):
            with self.subTest(path=path.name):
                with self.assertRaises(MeetingStageError) as caught:
                    stage(self.root, MID, participation=str(path), today=TODAY)
                self.assertIn("--participation", str(caught.exception))
        self.assertEqual(snapshot(self.meetings), before)

    def test_cancelled_meeting_refused_even_with_participation(self):
        kit = self.root / "kit" / "meetings.json"
        document = json.loads(kit.read_text(encoding="utf-8"))
        document["meetings"][0]["status"] = "cancelled"
        kit.write_text(json.dumps(document), encoding="utf-8")
        self.assert_refused(FULL, "cancelled", error=MeetingStageError)

    def test_angle_brackets_stripped_in_kit_and_content(self):
        value = copy.deepcopy(FULL)
        value["remote"]["how"] = "Zoom <script>alert(1)</script> link"
        value["written_comment"]["how"] = "<b>Email</b> the clerk"
        self.stage_with(value)
        text = self.target.read_text(encoding="utf-8")
        self.assertNotIn("<", text)
        self.assertNotIn(">", text)
        participation = self.document()["participation"]
        self.assertEqual(participation["remote"]["how"], "Zoom scriptalert(1)/script link")
        self.assertEqual(participation["written_comment"]["how"], "bEmail/b the clerk")
        self.assertIn("- Remote: yes, Zoom scriptalert(1)/script link", section(self.document()["comment_kit_md"]))

    def test_hand_written_content_still_checked_at_build(self):
        self.meetings.mkdir(exist_ok=True)
        stale = {"body": "Example City Council", "starts_at": STARTS,
                 "participation": dict(FULL, verified_at="2026-09-01")}
        self.target.write_text(json.dumps(stale), encoding="utf-8")
        with self.assertRaises(ContentError) as caught:
            load_content(self.root)
        self.assertIn("participation.verified_at", str(caught.exception))
        brackets = copy.deepcopy(FULL)
        brackets["remote"]["how"] = "<script>x</script>"
        self.target.write_text(json.dumps(dict(stale, participation=brackets)), encoding="utf-8")
        with self.assertRaises(ContentError) as caught:
            load_content(self.root)
        self.assertIn("participation.remote.how", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
