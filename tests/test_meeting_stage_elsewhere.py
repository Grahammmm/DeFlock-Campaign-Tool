"""DF1107-06: meetings-stage --elsewhere N cites decisions elsewhere in the comment kit.

Synthetic only: temp copies of examples/fictional-campaign with synthetic elsewhere entries; nothing is fetched or sent.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.meeting_stage import MeetingStageError, stage
from campaign_tool.meetings import MeetingsError, comment_kit_md
from tests.test_comment_kit_corrections import GOLDEN, MEETING, finding

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
MID = "legistar-examplecity-3001"
PART = {"source_url": "https://example.invalid/council/rules", "verified_by": "Synthetic Organizer", "verified_at": "2026-10-10"}


def entry(n, **extra):
    row = {"place": f"Springfield {n}", "state": "OR", "body": "Springfield City Council", "decided_on": "2026-03-10",
           "kind": "paused", "as_stated": f"The council voted to pause contract {n}.",
           "source_url": f"https://records.example.test/minutes/{n}", "checked_on": "2026-09-15"}
    row.update(extra)
    return row


def line(e):  # the required line, then markup characters backslash-escaped
    return re.sub(r"([\\*_\[\]()`<>#!|~])", r"\\\1",
                  f"{e['place']}, {e['state']}: {e['body']}, {e['decided_on']}: {e['as_stated']} ({e['source_url']})")


class KitTests(unittest.TestCase):
    def test_lines_follow_findings_in_every_version_in_given_order(self):
        three, one = entry(3), entry(1)
        meeting = dict(MEETING, participation=dict(PART, speaker_minutes=3, written_comment={"how": "Email the clerk"}))
        kit = comment_kit_md(meeting, [finding("policy")], "C", elsewhere=[three, one])
        expected = ["Elsewhere:", line(three), line(one)]
        self.assertEqual(kit.count("Elsewhere:"), 3)  # two-minute, spoken, written
        for body in (kit.split("## Two-minute comment\n", 1)[1], kit.split("## Spoken comment", 1)[1],
                     kit.split("## Written comment\n", 1)[1]):
            rows = body.splitlines()
            at = rows.index("Elsewhere:")
            self.assertEqual(rows[at:at + 3], expected)
            self.assertTrue(any(r.startswith("- ") for r in rows[:at]), "findings come first")
        self.assertNotIn("records.example.test", kit.split("## Sources", 1)[1])

    def test_no_findings_and_no_option_is_byte_identical(self):
        kit = comment_kit_md(MEETING, [], "C", elsewhere=[entry(1)])
        self.assertLess(kit.index("will publish what we receive"), kit.index("Elsewhere:"))
        self.assertEqual(comment_kit_md(MEETING, [finding("policy")], "Example Campaign", "https://campaign.example.invalid"), GOLDEN)
        self.assertEqual(comment_kit_md(MEETING, [finding("policy")], "Example Campaign",
                                        "https://campaign.example.invalid", elsewhere=None), GOLDEN)

    def test_markup_is_escaped_not_injected_and_dash_line_refused(self):
        kit = comment_kit_md(MEETING, [], "C", elsewhere=[entry(1, as_stated="Voted [yes] *now* \\ done é")])
        self.assertIn("Voted \\[yes\\] \\*now\\* \\\\ done é", kit)
        with self.assertRaises(MeetingsError):
            comment_kit_md(MEETING, [], "C", elsewhere=[entry(1, place="- Springfield")])
        for bad in ([], [entry(1)] * 4, ["x"]):
            with self.assertRaises(MeetingsError):
                comment_kit_md(MEETING, [], "C", elsewhere=bad)

    def test_citations_are_never_dropped_to_fit_a_budget(self):
        long = entry(1, as_stated="word " * 39)
        meeting = dict(MEETING, participation=dict(PART, speaker_minutes=1))
        with self.assertRaisesRegex(MeetingsError, "--elsewhere.*spoken"):
            comment_kit_md(meeting, [], "C", elsewhere=[long, long, long])
        meeting = dict(MEETING, participation=dict(PART, written_comment={"how": "Email the clerk"}))
        with self.assertRaisesRegex(MeetingsError, "--elsewhere.*written"):
            comment_kit_md(meeting, [], "C", elsewhere=[entry(1, as_stated="word " * 39, body="b " * 90)] * 3)


class StageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-elsewhere-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Example City Council", "starts_at": "2026-11-18T18:00:00-08:00"}]}), encoding="utf-8")
        self.target = self.root / "content" / "meetings" / f"{MID}.json"
        self.entries = [entry(1, kind="adopted_ordinance", decided_on="2026-05-01"), entry(2), entry(3, decided_on="2026-01-02")]
        self.put(self.entries)

    def put(self, entries):
        (self.root / "content" / "elsewhere.json").write_text(json.dumps(entries), encoding="utf-8")

    def doc(self):
        return json.loads(self.target.read_text(encoding="utf-8"))

    def test_stored_order_indices_snapshot_and_report(self):
        report = stage(self.root, MID, elsewhere_indices=[3, 1])
        document = self.doc()
        self.assertEqual([e["place"] for e in document["cited_elsewhere"]], ["Springfield 3", "Springfield 1"])
        self.assertEqual(document["cited_elsewhere"], [self.entries[2], self.entries[0]])
        self.assertEqual(report["elsewhere"][0], {"place": "Springfield 3", "body": "Springfield City Council", "decided_on": "2026-01-02"})
        self.assertLess(document["comment_kit_md"].index("Springfield 3, OR"), document["comment_kit_md"].index("Springfield 1, OR"))

    def test_without_the_option_nothing_new_appears(self):
        report = stage(self.root, MID)
        self.assertNotIn("cited_elsewhere", self.doc())
        self.assertNotIn("Elsewhere", self.doc()["comment_kit_md"])
        self.assertNotIn("elsewhere", report)

    def test_refusals_by_name_write_nothing(self):
        for bad, pattern in (([0], "--elsewhere 0"), ([4], "--elsewhere 4: out of range"), ([-1], "--elsewhere -1"),
                             ([1, 1], "--elsewhere 1: given more than once"), ([1, 2, 3, 1], "1 to 3"), ([], "1 to 3"),
                             ([True], "whole numbers"), (["1"], "whole numbers")):
            with self.subTest(bad=bad), self.assertRaisesRegex(MeetingStageError, pattern):
                stage(self.root, MID, elsewhere_indices=bad)
        self.assertFalse(self.target.exists())
        (self.root / "content" / "elsewhere.json").unlink()
        with self.assertRaisesRegex(MeetingStageError, r"--elsewhere: content/elsewhere.json not found"):
            stage(self.root, MID, elsewhere_indices=[1])
        self.assertFalse(self.target.exists())

    def test_refused_replace_keeps_existing_bytes_and_replace_replaces_selection(self):
        stage(self.root, MID, elsewhere_indices=[1])
        saved = self.target.read_bytes()
        with self.assertRaisesRegex(MeetingStageError, "out of range"):
            stage(self.root, MID, replace=True, elsewhere_indices=[9])
        self.assertEqual(self.target.read_bytes(), saved)
        stage(self.root, MID, replace=True)  # no option: the key and the paragraph go
        self.assertNotIn("cited_elsewhere", self.doc())
        self.assertNotIn("Elsewhere", self.doc()["comment_kit_md"])

    def test_legal_wording_is_an_advisory_note_without_the_text(self):
        self.put([entry(1, as_stated="The county violated SB 34 by sharing plate data.")])
        report = stage(self.root, MID, elsewhere_indices=[1])
        notes = [n for n in report.get("review_flags", []) if "--elsewhere[1]" in n]
        self.assertTrue(notes, report)
        self.assertNotIn("violated", " ".join(notes))
        self.assertTrue(self.target.exists())

    def test_cli_flag_repeats_and_rejects_a_non_integer(self):
        base = [sys.executable, "-B", "-m", "campaign_tool", "meetings-stage", "--directory", str(self.root), "--id", MID]
        bad = subprocess.run([*base, "--elsewhere", "two"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("--elsewhere", bad.stderr)
        ok = subprocess.run([*base, "--elsewhere", "2", "--elsewhere", "01"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual([e["place"] for e in self.doc()["cited_elsewhere"]], ["Springfield 2", "Springfield 1"])
        dup = subprocess.run([*base, "--elsewhere", "1", "--elsewhere", "01", "--replace"], cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=60)
        self.assertNotEqual(dup.returncode, 0)


if __name__ == "__main__":
    unittest.main()
