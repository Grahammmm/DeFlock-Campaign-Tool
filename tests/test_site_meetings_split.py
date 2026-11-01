"""RF1230-3: upcoming meetings first (home and meetings page); past meetings in their own section.

Synthetic content only (temp copies of the fictional example); no network.
"""
import json
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import site as site_module
from campaign_tool.cli import check_public_tree
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
EXAMPLE_ID = "cedar-council-2026-10"  # starts_at 2026-10-14T18:00:00-07:00
BUILD_DAY = date(2026, 10, 20)
IDS = re.compile(r'<article id="meeting-([^"]+)"')


class MeetingsSplitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meetings-split-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"

    def add(self, meeting_id, starts_at):
        record = {"id": meeting_id, "body": f"Synthetic council {meeting_id} (fictional)", "starts_at": starts_at,
                  "agency_id": "ca-cedar-city-council"}
        (self.meetings / f"{meeting_id}.json").write_text(json.dumps(record), encoding="utf-8")

    def clear(self):
        for path in self.meetings.glob("*.json"):
            path.unlink()

    def build(self, today=BUILD_DAY):
        build_site(self.root, today=today)
        return [(self.root / "public" / name).read_text(encoding="utf-8") for name in ("index.html", "meetings.html")]

    @staticmethod
    def home_section(index):
        start = index.index('<section class="section" id="meetings">')
        return index[start:index.index("</section>", start)]

    @staticmethod
    def split(meetings):
        """(upcoming section, past section or "") of meetings.html."""
        start = meetings.index('<section class="section" id="content">')
        upcoming = meetings[start:meetings.index("</section>", start)]
        if 'id="past-meetings"' not in meetings:
            return upcoming, ""
        past_start = meetings.index('<section class="section" id="past-meetings">')
        return upcoming, meetings[past_start:meetings.index("</section>", past_start)]

    def add_mixed(self):
        for meeting_id, day in (("m1020", "2026-10-20"), ("m1028", "2026-10-28"),
                                ("m1104", "2026-11-04"), ("m1118", "2026-11-18")):
            self.add(meeting_id, day + "T18:00:00-07:00")

    def test_mixed_home_shows_next_three_and_page_splits(self):
        self.add_mixed()
        index, meetings = self.build()
        self.assertEqual(IDS.findall(self.home_section(index)), ["m1020", "m1028", "m1104"])
        upcoming, past = self.split(meetings)
        self.assertEqual(IDS.findall(upcoming), ["m1020", "m1028", "m1104", "m1118"])
        self.assertTrue(past)
        self.assertIn("<h2>Past meetings</h2>", past)
        self.assertEqual(IDS.findall(past), [EXAMPLE_ID])
        self.assertEqual(len(IDS.findall(meetings)), 5)  # nothing listed twice
        self.assertIn("Public meetings and comment kits", meetings)
        self.assertNotIn("Upcoming public meetings and comment kits", meetings)

    def test_leak_scan_still_passes(self):
        self.add_mixed()
        self.build()
        self.assertEqual(check_public_tree(self.root / "public"), [])

    def test_meeting_today_is_upcoming_until_the_next_day(self):
        self.clear()
        self.add("late", "2026-10-20T23:30:00-07:00")
        index, meetings = self.build(date(2026, 10, 20))
        self.assertEqual(IDS.findall(self.home_section(index)), ["late"])
        self.assertEqual(self.split(meetings)[1], "")
        index, meetings = self.build(date(2026, 10, 21))
        self.assertEqual(IDS.findall(self.home_section(index)), [])
        upcoming, past = self.split(meetings)
        self.assertEqual((IDS.findall(upcoming), IDS.findall(past)), ([], ["late"]))

    def test_utc_and_offset_forms_use_the_typed_date(self):
        self.clear()
        self.add("utc", "2026-10-21T01:00:00Z")  # 2026-10-20 in Pacific time; typed date wins
        self.add("local", "2026-10-21T18:00:00-07:00")
        for today, upcoming_ids, past_ids in ((date(2026, 10, 21), ["utc", "local"], []),
                                              (date(2026, 10, 22), [], ["local", "utc"])):
            _, meetings = self.build(today)
            upcoming, past = self.split(meetings)
            self.assertEqual(IDS.findall(upcoming), upcoming_ids, today)
            self.assertEqual(IDS.findall(past), past_ids, today)

    def test_only_past_meetings(self):
        index, meetings = self.build()  # the example's only meeting is 2026-10-14
        home = self.home_section(index)
        self.assertEqual(IDS.findall(home), [])
        self.assertIn("No upcoming meetings are listed.", home)
        self.assertIn('<a href="/meetings.html#past-meetings">', home)
        self.assertNotIn("No meetings are listed yet.", home)
        upcoming, past = self.split(meetings)
        self.assertIn("<p>No upcoming meetings are listed.</p>", upcoming)
        self.assertEqual(IDS.findall(past), [EXAMPLE_ID])

    def test_no_meetings(self):
        self.clear()
        index, meetings = self.build()
        home = self.home_section(index)
        self.assertIn("No meetings are listed yet.", home)
        self.assertNotIn("past-meetings", index)
        self.assertNotIn("past-meetings", meetings)
        self.assertIn("<p>No upcoming meetings are listed.</p>", self.split(meetings)[0])

    def test_past_meetings_most_recent_first(self):
        self.clear()
        for meeting_id, day in (("m1008", "2026-10-08"), ("m1014", "2026-10-14"), ("m1001", "2026-10-01")):
            self.add(meeting_id, day + "T18:00:00-07:00")
        _, meetings = self.build()
        upcoming, past = self.split(meetings)
        self.assertEqual(IDS.findall(upcoming), [])
        self.assertEqual(IDS.findall(past), ["m1014", "m1008", "m1001"])

    def test_comment_kit_kept_in_past_section(self):
        _, meetings = self.build()
        past = self.split(meetings)[1]
        start = past.index(f'<article id="meeting-{EXAMPLE_ID}"')
        article = past[start:past.index("</article>", start)]
        self.assertIn("<details><summary>Public comment kit</summary>", article)
        self.assertIn("Two minutes, one ask.", article)

    def test_default_date_is_the_build_day(self):
        class Day(date):
            value = date(2026, 10, 10)

            @classmethod
            def today(cls):
                return cls.value

        with mock.patch.object(site_module, "date", Day):
            build_site(self.root)  # the CLI path: no today argument
            meetings = (self.root / "public" / "meetings.html").read_text(encoding="utf-8")
            self.assertEqual(IDS.findall(self.split(meetings)[0]), [EXAMPLE_ID])
            self.assertNotIn("past-meetings", meetings)
            Day.value = date(2026, 10, 15)
            build_site(self.root)
            meetings = (self.root / "public" / "meetings.html").read_text(encoding="utf-8")
            self.assertEqual(IDS.findall(self.split(meetings)[1]), [EXAMPLE_ID])

    def test_default_date_runs_unpatched(self):
        written = build_site(self.root)
        self.assertIn(self.root / "public" / "meetings.html", written)
        meetings = (self.root / "public" / "meetings.html").read_text(encoding="utf-8")
        self.assertEqual(len(IDS.findall(meetings)), 1)


if __name__ == "__main__":
    unittest.main()
