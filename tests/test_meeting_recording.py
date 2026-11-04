"""DF1422-10: meetings-outcome --recording-url URL [--recording-start H:MM:SS] stores the body's own recording as a link.

Synthetic content only (temp copies of the fictional example, injected ``today``); nothing is fetched.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.meeting_outcome import MeetingOutcomeError, record_outcome
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
MID = "cedar-council-2026-10"  # starts_at 2026-10-14T18:00:00-07:00
TODAY = date(2026, 10, 20)
SOURCE = "https://example.invalid/cedar-council/minutes"
VIDEO = "https://video.example.invalid/cedar-council/2026-10-14.mp4"
OUTCOME = {"result": "approved", "on": "2026-10-14", "source_url": SOURCE, "verified_by": "Synthetic Organizer",
           "verified_at": "2026-10-20"}


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


class MeetingRecordingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-recording-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meeting = self.root / "content" / "meetings" / "2026-10-cedar-council.json"

    def record(self, url=VIDEO, start=None, **kwargs):
        return record_outcome(self.root, kwargs.pop("meeting_id", MID), "approved", "2026-10-14", SOURCE,
                              "Synthetic Organizer", today=TODAY, recording_url=url, recording_start=start, **kwargs)

    def refused(self, needle, error=ValueError, path=None, **kwargs):
        path = path or self.meeting
        before = path.read_bytes()
        with self.assertRaises(error) as caught:
            self.record(**kwargs)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(path.read_bytes(), before)

    def cli(self, *extra):
        return run("meetings-outcome", "--directory", str(self.root), "--id", MID, "--result", "approved",
                   "--on", "2026-10-14", "--source", SOURCE, "--verified-by", "Synthetic Organizer", *extra)

    def pages(self):
        build_site(self.root, today=TODAY)
        public = self.root / "public"
        return ((public / "meetings.html").read_text(encoding="utf-8").split('id="past-meetings"', 1)[1],
                (public / "meetings" / f"{MID}.html").read_text(encoding="utf-8"))

    def set_stored(self, **changes):
        document = json.loads(self.meeting.read_text(encoding="utf-8"))
        document["outcome"] = {**OUTCOME, **changes}
        self.meeting.write_text(json.dumps(document), encoding="utf-8")

    # 1. url + start: stored, and shown on the meetings list and the meeting page with the escaped URL
    def test_recording_with_start_is_stored_and_shown(self):
        url = VIDEO + "?a=1&b=2"
        outcome = self.record(url, "1:02:30")
        self.assertEqual(outcome, {**OUTCOME, "recording_url": url, "recording_start": "1:02:30"})
        self.assertEqual(json.loads(self.meeting.read_text(encoding="utf-8"))["outcome"], outcome)
        link = (f' · <a href="{VIDEO}?a=1&amp;b=2" rel="noopener noreferrer">Watch the discussion</a> (from 1:02:30)</p>')
        for page in self.pages():
            self.assertIn("verified by Synthetic Organizer" + link, page)
            self.assertNotIn("<iframe", page)
            self.assertNotIn("<video", page)
            self.assertNotIn("#t=", page)

    # 2. url without a start: link without "(from ...)"
    def test_recording_without_start_has_no_from_text(self):
        outcome = self.record()
        self.assertNotIn("recording_start", outcome)
        link = f' · <a href="{VIDEO}" rel="noopener noreferrer">Watch the discussion</a></p>'
        for page in self.pages():
            self.assertIn("verified by Synthetic Organizer" + link, page)
            self.assertNotIn("(from", page)

    # 2b. the wording comes from labels (translatable)
    def test_link_text_is_a_label(self):
        self.record(start="0:05:00")
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "meeting.recording": "Ver la discusi\u00f3n", "meeting.recording_from": "(desde {start})"}}), encoding="utf-8")
        for page in self.pages():
            self.assertIn(">Ver la discusi\u00f3n</a> (desde 0:05:00)</p>", page)
            self.assertNotIn("Watch the discussion", page)

    # 3. bad URLs: refused naming recording_url, nothing written
    def test_bad_urls_are_refused_naming_recording_url(self):
        long_url = "https://video.example.invalid/" + "a" * 471  # 501 characters
        self.assertEqual(len(long_url), 501)
        for url in ("http://video.example.invalid/x.mp4", "https://user:pw@video.example.invalid/x.mp4", long_url,  # pragma: allowlist secret (synthetic rejected URL)
                    "ftp://video.example.invalid/x.mp4", "javascript:alert(1)"):
            self.refused("recording_url", url=url)
        self.assertEqual(len(self.record("https://video.example.invalid/" + "a" * 470)["recording_url"]), 500)
        result = self.cli("--recording-url", "http://video.example.invalid/x.mp4", "--replace")
        self.assertEqual(result.returncode, 1)
        self.assertIn("recording_url", result.stderr)

    def test_failed_recording_writes_nothing(self):
        before = self.meeting.read_bytes()
        self.refused("recording_url", url="http://video.example.invalid/x.mp4")
        self.assertEqual(self.meeting.read_bytes(), before)
        self.assertFalse((self.root / "public").exists())

    # 4. start boundaries
    def test_recording_start_boundaries(self):
        for start in ("1:2:30", "12:00:01", "13:00:00", "0:60:00", "abc", "", "1:02:60", "1:02", "-1:00:00",
                      "1:02:30\n", "\u0661:02:30", "100:00:00"):
            self.refused("recording_start", start=start)
        for start in ("12:00:00", "0:00:00", "11:59:59", "01:02:30"):
            self.assertEqual(self.record(start=start, replace=True)["recording_start"], start)

    # 5. start alone; recording on a future / cancelled / unknown meeting: the existing refusals
    def test_start_alone_and_ineligible_meetings_are_refused(self):
        self.refused("recording_start needs recording_url", url=None, start="1:00:00", error=MeetingOutcomeError)
        result = self.cli("--recording-start", "1:00:00")
        self.assertEqual(result.returncode, 1)
        self.assertIn("recording_start needs recording_url", result.stderr)
        self.assertIn("Stopped:", result.stderr)
        meetings = self.root / "content" / "meetings"
        later = meetings / "synthetic-later.json"
        later.write_text(json.dumps({"id": "synthetic-later", "body": "Synthetic later (fictional)",
                                    "starts_at": "2026-10-25T18:00:00-07:00"}), encoding="utf-8")
        self.refused("has not happened yet", path=later, meeting_id="synthetic-later")
        cancelled = meetings / "synthetic-cancelled.json"
        cancelled.write_text(json.dumps({"id": "synthetic-cancelled", "body": "Synthetic cancelled (fictional)",
                                         "starts_at": "2026-10-15T18:00:00-07:00", "status": "cancelled"}), encoding="utf-8")
        self.refused("is cancelled", path=cancelled, meeting_id="synthetic-cancelled")
        self.refused("no staged meeting synthetic-missing", meeting_id="synthetic-missing")

    def test_existing_outcome_needs_replace_and_replace_drops_old_recording(self):
        self.record(start="0:10:00")
        self.refused("already has an outcome (approved)", url=VIDEO)
        outcome = record_outcome(self.root, MID, "approved", "2026-10-14", SOURCE, "Synthetic Organizer", replace=True,
                                 today=TODAY)
        self.assertEqual(outcome, OUTCOME)
        self.assertEqual(json.loads(self.meeting.read_text(encoding="utf-8"))["outcome"], OUTCOME)

    def test_cli_records_recording(self):
        result = self.cli("--recording-url", VIDEO, "--recording-start", "1:02:30")
        self.assertEqual(result.returncode, 0, result.stderr)
        printed, notice = result.stdout.rsplit("}", 1)
        self.assertEqual(json.loads(printed + "}")["recording_start"], "1:02:30")
        self.assertEqual(notice.strip(), "Recorded only. Nothing was published.")
        self.assertFalse((self.root / "public").exists())

    # 6. markup characters never reach the attribute: the URL rule refuses them
    def test_markup_characters_in_url_are_refused(self):
        for url in ('https://video.example.invalid/x"onmouseover="y', "https://video.example.invalid/<script>",
                    "https://video.example.invalid/x y", "https://video.example.invalid/x'y"):
            self.refused("recording_url", url=url)

    # 7. an outcome without the keys renders exactly as before; content-check is clean
    def test_outcome_without_recording_renders_unchanged(self):
        self.set_stored()
        before = self.pages()
        expected = ('<p class="meeting-outcome">Outcome: Approved on <time datetime="2026-10-14">2026-10-14</time>'
                    f' · <a href="{SOURCE}" rel="noopener noreferrer">Official record</a> · verified by Synthetic Organizer</p>')
        for page in before:
            self.assertIn(expected, page)
            self.assertNotIn("Watch the discussion", page)
        self.assertEqual(content_problems(self.root), [])
        self.assertEqual(load_content(self.root)["meetings"][0]["outcome"].keys(), OUTCOME.keys())

    # 8. bad stored values: content-check and build refuse with the field label
    def test_bad_stored_recording_is_refused_by_content_check_and_build(self):
        cases = [({"recording_url": VIDEO, "recording_start": "12:00:01"}, "outcome.recording_start: use H:MM:SS, at most 12:00:00"),
                 ({"recording_url": VIDEO, "recording_start": "1:2:30"}, "outcome.recording_start: use H:MM:SS"),
                 ({"recording_url": VIDEO, "recording_start": 90}, "outcome.recording_start: use H:MM:SS"),
                 ({"recording_start": "1:00:00"}, "outcome.recording_start: needs recording_url"),
                 ({"recording_url": "http://video.example.invalid/x"}, "outcome.recording_url: must be an https:// URL"),
                 ({"recording_url": "https://video.example.invalid/" + "a" * 480}, "outcome.recording_url: at most 500"),
                 ({"recording_url": None}, "outcome.recording_url: must be an https:// URL")]
        for changes, needle in cases:
            self.set_stored(**changes)
            self.assertTrue(any(needle in problem for problem in content_problems(self.root)), (changes, needle))
            with self.assertRaises(ContentError) as caught:
                load_content(self.root)
            self.assertIn(needle, str(caught.exception))
            self.assertIn("2026-10-cedar-council.json", str(caught.exception))
            built = run("build", "--directory", str(self.root))
            self.assertEqual(built.returncode, 1, built.stdout)
            self.assertIn(needle, built.stderr)
            self.assertFalse((self.root / "public").exists())
        self.assertEqual(self.cli("--recording-start", "1:00:00", "--replace").returncode, 1)

    def test_good_stored_recording_passes_content_check(self):
        self.set_stored(recording_url=VIDEO, recording_start="12:00:00")
        self.assertEqual(content_problems(self.root), [])
        checked = run("content-check", "--directory", str(self.root))
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)


if __name__ == "__main__":
    unittest.main()
