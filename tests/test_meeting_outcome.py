"""DF2237-11: meetings-outcome records an organizer-verified result on a past staged meeting.

Synthetic content only (temp copies of the fictional example, injected ``today``); no network.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.cli import read_config
from campaign_tool.content import ContentError, load_content
from campaign_tool.meeting_outcome import MeetingOutcomeError, record_outcome
from campaign_tool.meeting_stage import stage
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "legistar-events.json"
MID = "cedar-council-2026-10"  # starts_at 2026-10-14T18:00:00-07:00
STAGED = "legistar-examplecity-1001"  # fixture meeting, 2026-10-14 18:00
TODAY = date(2026, 10, 20)
SOURCE = "https://example.invalid/cedar-council/minutes"
LINE = ('<p class="meeting-outcome">Outcome: Continued to a later meeting on <time datetime="2026-10-14">2026-10-14</time>'
        f' (4-1) · <a href="{SOURCE}" rel="noopener noreferrer">Official record</a> · verified by Synthetic Organizer</p>')


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


class MeetingOutcomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-outcome-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.example = self.meetings / "2026-10-cedar-council.json"

    def add(self, meeting_id, starts_at, **extra):
        record = {"id": meeting_id, "body": f"Synthetic board {meeting_id} (fictional)", "starts_at": starts_at, **extra}
        path = self.meetings / f"{meeting_id}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def record(self, meeting_id=MID, result="continued", on="2026-10-14", source=SOURCE, by="Synthetic Organizer",
               vote="4-1", replace=False, today=TODAY):
        return record_outcome(self.root, meeting_id, result, on, source, by, vote, replace, today=today)

    def refused(self, needle, path=None, **kwargs):
        path = path or self.example
        before = path.read_bytes()
        with self.assertRaises(ValueError) as caught:
            self.record(**kwargs)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(path.read_bytes(), before)

    def cli(self, *extra, result="continued", source=SOURCE, by="Synthetic Organizer"):
        return run("meetings-outcome", "--directory", str(self.root), "--id", MID, "--result", result,
                   "--on", "2026-10-14", "--source", source, "--verified-by", by, *extra)

    # 1. a past staged meeting gains the outcome; the past list and the meeting page show it; load_content accepts it
    def test_past_meeting_outcome_is_recorded_and_shown(self):
        outcome = self.record()
        self.assertEqual(outcome, {"result": "continued", "on": "2026-10-14", "source_url": SOURCE,
                                   "verified_by": "Synthetic Organizer", "verified_at": "2026-10-20", "vote": "4-1"})
        self.assertEqual(json.loads(self.example.read_text(encoding="utf-8"))["outcome"], outcome)
        meeting = [m for m in load_content(self.root)["meetings"] if m["id"] == MID][0]
        self.assertEqual(meeting["outcome"], outcome)
        build_site(self.root, today=TODAY)
        listing = (self.root / "public" / "meetings.html").read_text(encoding="utf-8")
        self.assertIn(LINE, listing.split('id="past-meetings"', 1)[1])
        self.assertIn(LINE, (self.root / "public" / "meetings" / f"{MID}.html").read_text(encoding="utf-8"))

    def test_cli_prints_outcome_and_publishes_nothing(self):
        result = self.cli("--vote", "4-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        printed, notice = result.stdout.rsplit("}", 1)
        self.assertEqual(json.loads(printed + "}")["result"], "continued")
        self.assertEqual(notice.strip(), "Recorded only. Nothing was published.")
        self.assertFalse((self.root / "public").exists())

    # 2. not yet happened, --on before the meeting or after today: refused, bytes unchanged
    def test_future_meeting_and_out_of_range_dates_are_refused(self):
        later = self.add("synthetic-later", "2026-10-25T18:00:00-07:00")
        self.refused("has not happened yet", later, meeting_id="synthetic-later", on="2026-10-25")
        same_day = self.add("synthetic-today", "2026-10-20T09:00:00-07:00")
        self.refused("from 2026-10-20 to 2026-10-20", same_day, meeting_id="synthetic-today", on="2026-10-21")
        self.refused("from 2026-10-14 to 2026-10-20", on="2026-10-13")
        self.refused("from 2026-10-14 to 2026-10-20", on="2026-10-21")
        self.refused("YYYY-MM-DD", on="2026-10-1")
        self.refused("invalid ISO-8601", on="14/10/2026")

    # 3. cancelled, unknown id, symlinked file
    def test_cancelled_unknown_and_symlinked_meetings_are_refused(self):
        cancelled = self.add("synthetic-cancelled", "2026-10-15T18:00:00-07:00", status="cancelled")
        self.refused("is cancelled", cancelled, meeting_id="synthetic-cancelled", on="2026-10-15")
        with self.assertRaises(MeetingOutcomeError) as caught:
            self.record(meeting_id="synthetic-missing")
        self.assertIn("no staged meeting synthetic-missing", str(caught.exception))
        outside = self.tmp / "outside.json"
        outside.write_text(json.dumps({"id": "synthetic-link", "body": "Synthetic link (fictional)",
                                       "starts_at": "2026-10-14T18:00:00-07:00"}), encoding="utf-8")
        (self.meetings / "synthetic-link.json").symlink_to(outside)
        self.refused("must not be a symlink", outside, meeting_id="synthetic-link")

    # 4. an existing outcome is kept without --replace and replaced (verified_at updated) with it
    def test_existing_outcome_needs_replace(self):
        self.record(today=date(2026, 10, 15))
        self.refused("already has an outcome (continued)", result="approved")
        outcome = self.record(result="approved", vote=None, replace=True)
        self.assertEqual(outcome["verified_at"], "2026-10-20")
        stored = json.loads(self.example.read_text(encoding="utf-8"))["outcome"]
        self.assertEqual((stored["result"], stored["verified_at"], "vote" in stored), ("approved", "2026-10-20", False))
        result = self.cli("--vote", "5-0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("already has an outcome (approved)", result.stderr)

    # 5. bad source, verified-by, vote or result: Stopped, exit 1, nothing written
    def test_cli_refuses_bad_fields(self):
        before = self.example.read_bytes()
        cases = [({"source": "http://example.invalid/minutes"}, (), "must be an https:// URL"),
                 ({"source": "javascript:alert(1)"}, (), "must be an https:// URL"),
                 ({"by": ""}, (), "verified_by: must be 1 to 120"),
                 ({"by": "x" * 121}, (), "verified_by: must be 1 to 120"),
                 ({}, ("--vote", "<b>4-1</b>"), "vote: must be 1 to 120"),
                 ({}, ("--vote", "4-1\n2"), "vote: must be 1 to 120"),
                 ({}, ("--vote", "4-1\x07"), "vote: must be 1 to 120"),
                 ({"result": "passed"}, (), "result: must be one of approved, denied")]
        for kwargs, extra, needle in cases:
            with self.subTest(kwargs=kwargs, extra=extra):
                result = self.cli(*extra, **kwargs)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("Stopped:", result.stderr)
                self.assertIn(needle, result.stderr)
                self.assertEqual(self.example.read_bytes(), before)

    # 6. a hand-edited outcome on an upcoming or cancelled meeting fails load_content and the build
    def test_hand_edited_outcome_on_upcoming_meeting_fails_build(self):
        build_site(self.root, today=TODAY)
        public = self.root / "public"
        before = {p: p.read_bytes() for p in sorted(public.rglob("*")) if p.is_file()}
        outcome = {"result": "approved", "on": "2099-01-05", "source_url": SOURCE, "verified_by": "Synthetic Organizer",
                   "verified_at": "2099-01-05"}
        self.add("synthetic-upcoming", "2099-01-05T18:00:00-08:00", outcome=outcome)
        with self.assertRaises(ContentError):
            load_content(self.root)
        result = run("build", "--directory", str(self.root))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("synthetic-upcoming.json.outcome.on", result.stderr)
        self.assertEqual({p: p.read_bytes() for p in sorted(public.rglob("*")) if p.is_file()}, before)
        self.add("synthetic-upcoming", "2026-10-14T18:00:00-07:00", status="cancelled",
                 outcome={**outcome, "on": "2026-10-14", "verified_at": "2026-10-14"})
        with self.assertRaisesRegex(ContentError, "cancelled meeting has no outcome"):
            load_content(self.root)

    # 7. meetings-stage --replace keeps an outcome; without one the output is unchanged
    def test_stage_replace_keeps_outcome(self):
        kit = run("meetings", "--directory", str(self.root), "--client", "examplecity", "--from-json", str(FIXTURE),
                  "--since", "2026-09-30")
        self.assertEqual(kit.returncode, 0, kit.stderr)
        target = self.meetings / f"{STAGED}.json"
        stage(self.root, STAGED, today=TODAY)
        first = target.read_bytes()
        stage(self.root, STAGED, replace=True, today=TODAY)
        self.assertEqual(target.read_bytes(), first)
        outcome = self.record(meeting_id=STAGED, result="denied", vote=None)
        stage(self.root, STAGED, replace=True, today=TODAY)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["outcome"], outcome)

    # 8. upcoming meetings never show an outcome; meetings without one render as before
    def test_upcoming_and_plain_meetings_render_unchanged(self):
        self.add("synthetic-today", "2026-10-20T09:00:00-07:00")
        self.record(meeting_id="synthetic-today", on="2026-10-20")
        site = Site(self.root, read_config(self.root), load_content(self.root), today=TODAY)
        today_meeting = [m for m in site.upcoming if m["id"] == "synthetic-today"][0]
        bare = {**today_meeting, "outcome": None}
        self.assertEqual(site.meeting_item(today_meeting), site.meeting_item(bare))
        self.assertEqual(site.meeting_html(today_meeting), site.meeting_html(bare))
        past = [m for m in site.past if m["id"] == MID][0]
        self.assertIsNone(past["outcome"])
        without_key = {k: v for k, v in past.items() if k != "outcome"}
        self.assertEqual(site.meeting_item(past), site.meeting_item(without_key))
        self.assertEqual(site.meeting_html(past), site.meeting_html(without_key))
        self.assertNotIn("meeting-outcome", site.meetings_html()["html"])


if __name__ == "__main__":
    unittest.main()
