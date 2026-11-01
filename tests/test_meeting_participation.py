"""NB-DF-14: organizer-verified participation fields for staged meetings; the comment kit never guesses.

Synthetic fixtures only (example.invalid hosts, fictional example campaign); no network.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.content import ContentError, load_content, load_meeting  # noqa: E402
from campaign_tool.meeting_stage import MeetingStageError, stage  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
MID = "legistar-examplecity-1001"
STARTS = "2026-10-14T18:00:00-07:00"
TODAY = date(2026, 10, 12)
NOT_VERIFIED = "Check the posted agenda for how and when to comment; participation details were not verified."
FULL = {"in_person": True, "remote": {"available": True, "how": "Zoom link on the posted agenda"},
        "written_comment": {"how": "Email the clerk at clerk@example.invalid", "deadline": "2026-10-14T12:00:00-07:00"},
        "source_url": "https://examplecity.example.invalid/agenda/1001", "verified_by": "Organizer A",
        "verified_at": "2026-10-10"}
SOURCE_LINE = "Source: https://examplecity.example.invalid/agenda/1001 (checked 2026-10-10)"


def section(kit):
    """The lines of the kit's '## How to take part' section."""
    after = kit.split("## How to take part\n", 1)[1]
    return [line for line in after.split("\n## Sources", 1)[0].splitlines() if line]


def snapshot(folder):
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())} if folder.is_dir() else None


class ParticipationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-participation-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        (self.root / "kit").mkdir(exist_ok=True)
        meeting = {"id": MID, "body": "Example City Council", "starts_at": STARTS, "agenda_item": "Item 7: ALPR",
                   "relevance": "alpr_item"}
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [meeting]}),
                                                         encoding="utf-8")
        self.meetings = self.root / "content" / "meetings"
        self.target = self.meetings / f"{MID}.json"

    def part_file(self, document, name="participation.json"):
        path = self.tmp / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def staged(self, document, today=TODAY):
        stage(self.root, MID, participation=self.part_file(document), today=today, replace=True)
        return json.loads(self.target.read_text(encoding="utf-8"))

    def assert_refused(self, document, needle, today=TODAY, error=ContentError):
        before = snapshot(self.meetings)
        with self.assertRaises(error) as caught:
            stage(self.root, MID, participation=self.part_file(document), today=today)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(snapshot(self.meetings), before)

    def test_full_object_lists_each_field_and_round_trips(self):
        document = self.staged(FULL)
        kit = document["comment_kit_md"]
        self.assertEqual(section(kit), ["- In person: yes", "- Remote: Zoom link on the posted agenda",
                                        "- Written comment: Email the clerk at clerk@example.invalid, "
                                        "by 2026-10-14 12:00 (UTC-07:00)", SOURCE_LINE])
        self.assertLess(kit.index("## Three asks"), kit.index("## How to take part"))
        self.assertLess(kit.index("## How to take part"), kit.index("## Sources"))
        self.assertEqual(document["participation"], FULL)
        staged = [m for m in load_content(self.root)["meetings"] if m["id"] == MID][0]
        self.assertEqual(staged["participation"], FULL)
        self.assertEqual(load_meeting(document, "m.json", today=TODAY)["participation"], FULL)

    def test_stated_no_and_remote_without_how(self):
        kit = self.staged(dict(FULL, in_person=False, remote={"available": False, "how": ""},
                               written_comment=None))["comment_kit_md"]
        self.assertEqual(section(kit), ["- In person: no", "- Remote: no", SOURCE_LINE])
        kit = self.staged(dict(FULL, remote={"available": True, "how": ""}, written_comment=None))["comment_kit_md"]
        self.assertIn("- Remote: yes", section(kit))

    def test_null_or_absent_fields_give_only_the_source_line(self):
        only = {k: FULL[k] for k in ("source_url", "verified_by", "verified_at")}
        for document in (dict(only, in_person=None, remote=None, written_comment=None), only):
            kit = self.staged(document)["comment_kit_md"]
            self.assertEqual(section(kit), [SOURCE_LINE])
            self.assertNotIn(" by 20", kit)
            self.assertNotIn("Remote", kit)
            self.assertNotIn("In person", kit)

    def test_without_participation_fixed_line_and_no_deadline(self):
        stage(self.root, MID)
        document = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertNotIn("participation", document)
        self.assertEqual(section(document["comment_kit_md"]), [NOT_VERIFIED])
        self.assertNotIn("deadline", document["comment_kit_md"].lower())
        self.assertNotIn(" by 20", document["comment_kit_md"])

    def test_kit_entry_cannot_smuggle_unverified_participation(self):
        meeting = {"id": MID, "body": "B", "starts_at": STARTS, "participation": FULL}
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [meeting]}),
                                                         encoding="utf-8")
        stage(self.root, MID, today=TODAY)
        document = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertNotIn("participation", document)
        self.assertEqual(section(document["comment_kit_md"]), [NOT_VERIFIED])

    def test_bad_deadlines_refused_naming_the_field(self):
        field = "participation.written_comment.deadline"
        for deadline in ("2026-10-14T19:00:00-07:00", STARTS, "2026-10-15T01:00:00Z", "2026-10-14T12:00:00",
                         "noon on the day", 20261014):
            with self.subTest(deadline=deadline):
                self.assert_refused(dict(FULL, written_comment={"how": "Email", "deadline": deadline}), field)
        self.assert_refused(dict(FULL, written_comment={"how": "Email"}), "participation.written_comment")

    def test_source_and_verification_refusals(self):
        self.assert_refused(dict(FULL, source_url="http://examplecity.example.invalid/a"), "participation.source_url")
        self.assert_refused({k: v for k, v in FULL.items() if k != "source_url"}, "missing field(s) ['source_url']")
        self.assert_refused({k: v for k, v in FULL.items() if k != "verified_at"}, "missing field(s) ['verified_at']")
        for empty in ("", "   "):
            self.assert_refused(dict(FULL, verified_by=empty), "participation.verified_by")
        self.assert_refused(dict(FULL, verified_at="2026-10-13"), "participation.verified_at")  # after today
        self.assert_refused(dict(FULL, verified_at="2026-09-13"), "participation.verified_at")  # 31 days before
        self.assert_refused(dict(FULL, verified_at="10/10/2026"), "participation.verified_at")
        self.assertEqual(self.staged(dict(FULL, verified_at="2026-09-14"))["participation"]["verified_at"], "2026-09-14")
        self.assertEqual(self.staged(FULL, today=date(2026, 10, 10))["participation"]["verified_at"], "2026-10-10")

    def test_type_and_unknown_key_refusals(self):
        self.assert_refused(dict(FULL, phone="555-0100"), "unknown field(s) ['phone']")
        self.assert_refused(dict(FULL, remote={"available": True, "how": "Zoom", "phone": "555-0100"}),
                            "participation.remote: unknown field(s) ['phone']")
        self.assert_refused(dict(FULL, in_person="yes"), "participation.in_person")
        self.assert_refused(dict(FULL, remote={"available": "yes", "how": "Zoom"}), "participation.remote.available")
        self.assert_refused(dict(FULL, remote={"available": True, "how": "x" * 201}), "participation.remote.how")
        self.assert_refused(["not", "an", "object"], "participation: expected an object")

    def test_file_limits_refused_before_any_write(self):
        before = snapshot(self.meetings)
        big = self.tmp / "big.json"
        big.write_text(json.dumps(dict(FULL, verified_by="A" + " " * (64 * 1024))), encoding="utf-8")
        real = self.part_file(FULL, "real.json")
        link = self.tmp / "link.json"
        os.symlink(real, link)
        bad = self.tmp / "bad.json"
        bad.write_bytes(b"\xff not json")
        for path, needle in ((big, "64 KB"), (link, "not a symlink"), (self.tmp / "missing.json", "regular file"),
                             (bad, "not UTF-8 JSON")):
            with self.subTest(path=path.name), self.assertRaises(MeetingStageError) as caught:
                stage(self.root, MID, participation=path, today=TODAY)
            self.assertIn(needle, str(caught.exception))
        self.assertEqual(snapshot(self.meetings), before)

    def test_angle_brackets_stripped_in_kit_and_content(self):
        document = self.staged(dict(FULL, remote={"available": True, "how": "<script>alert(1)</script>Zoom"},
                                    written_comment={"how": "<b>Email</b> the clerk", "deadline": FULL[
                                        "written_comment"]["deadline"]}))
        self.assertEqual(document["participation"]["remote"]["how"], "scriptalert(1)/scriptZoom")
        self.assertEqual(document["participation"]["written_comment"]["how"], "bEmail/b the clerk")
        text = self.target.read_text(encoding="utf-8")
        self.assertNotIn("<", text)
        self.assertNotIn(">", text)
        self.assertIn("- Remote: scriptalert(1)/scriptZoom", section(document["comment_kit_md"]))
        with self.assertRaises(ContentError):  # content files edited by hand are refused, never rendered
            load_meeting(dict(document, participation=dict(FULL, verified_by="<i>A</i>")), "m.json", today=TODAY)

    def test_cli_flag_stages_and_builds_and_refuses_naming_the_field(self):
        good = self.part_file(dict(FULL, verified_at="2026-09-20"), "good.json")
        base = [sys.executable, "-B", "-m", "campaign_tool", "meetings-stage", "--directory", str(self.root), "--id", MID]
        result = subprocess.run(base + ["--participation", str(good)], cwd=REPO_ROOT, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8"))["participation"]["verified_at"],
                         "2026-09-20")
        build = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root)],
                               cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(build.returncode, 0, build.stderr)
        before = self.target.read_bytes()
        late = self.part_file(dict(FULL, written_comment={"how": "Email", "deadline": "2026-10-15T09:00:00-07:00"}),
                              "late.json")
        result = subprocess.run(base + ["--participation", str(late), "--replace"], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("Stopped:", result.stderr)
        self.assertIn("participation.written_comment.deadline", result.stderr)
        self.assertEqual(self.target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
