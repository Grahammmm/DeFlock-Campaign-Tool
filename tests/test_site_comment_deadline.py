"""DF1822-08: a verified written-comment deadline on the meetings list, home and the meeting page.

Synthetic content only (temp copies of the fictional example); no network.
"""
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
STARTS = "2026-11-10T18:00:00-08:00"
BUILD_DAY = date(2026, 11, 1)  # build instant 2026-11-01T00:00:00Z
DUE = "2026-11-09T17:00:00-08:00"
DUE_TEXT = "Written comments due Monday, November 9, 2026, 5:00 PM (UTC-08:00)"
CLOSED = "Written comment period closed"
LINE = re.compile(r'<p class="comment-deadline"><time datetime="([^"]+)">([^<]+)</time></p>')
# Captured from main 20121328 (before this change) by building BASELINE with BUILD_DAY; not candidate-derived.
# Each meeting's list/home article and page body; only the id differs between the three, so {m} stands for it.
GOLDEN_ARTICLE = (
    '<article id="meeting-{m}"><time datetime="2026-11-10T18:00:00-08:00">2026-11-10 18:00</time>'
    ' · <a href="/meetings/{m}.html">Meeting page</a><h3>Synthetic council {m} (fictional)</h3>'
    '<p>Agenda item: Item 7: ALPR contract</p></article>')
GOLDEN_PAGE = (
    '<article class="section finding-body" id="content"><p class="eyebrow">Meeting</p>'
    '<h2>Synthetic council {m} (fictional)</h2><p><time datetime="2026-11-10T18:00:00-08:00">2026-11-10 18:00</time></p>'
    '<p>Agenda item: Item 7: ALPR contract</p><h3>Public comment kit</h3>'
    '<p>No comment kit has been staged for this meeting.</p><p class="notice">This is not legal advice. Findings describe'
    ' records and published rules as of the event date; consult counsel before relying on them.</p>'
    '<p><a href="/meetings.html">All meetings</a></p></article>')


def participation(written):
    return {"in_person": True, "remote": None, "written_comment": written,
            "source_url": "https://example.invalid/council/agenda", "verified_by": "Synthetic Organizer",
            "verified_at": "2026-10-25"}


# Meetings whose output must stay byte-identical to main: no participation, no written_comment, null deadline.
BASELINE = {"none": None, "nowritten": participation(None),
            "nulldeadline": participation({"how": "Email the city clerk", "deadline": None})}


class CommentDeadlineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-comment-deadline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        for path in self.meetings.glob("*.json"):
            path.unlink()

    def add(self, meeting_id, part, status="scheduled", starts_at=STARTS, kit=None):
        record = {"id": meeting_id, "body": f"Synthetic council {meeting_id} (fictional)", "starts_at": starts_at,
                  "agenda_item": "Item 7: ALPR contract", "agency_id": "ca-cedar-city-council", "status": status}
        if kit is not None:
            record["comment_kit_md"] = kit
        if part is not None:
            record["participation"] = part
        (self.meetings / f"{meeting_id}.json").write_text(json.dumps(record), encoding="utf-8")

    def add_due(self, meeting_id, deadline, **kwargs):
        self.add(meeting_id, participation({"how": "Email the city clerk", "deadline": deadline}), **kwargs)

    def build(self, today=BUILD_DAY):
        build_site(self.root, today=today)
        public = self.root / "public"
        return {"list": (public / "meetings.html").read_text(encoding="utf-8"),
                "home": (public / "index.html").read_text(encoding="utf-8"),
                **{p.stem: p.read_text(encoding="utf-8") for p in (public / "meetings").glob("*.html")}}

    @staticmethod
    def article(html, meeting_id):
        start = html.index(f'<article id="meeting-{meeting_id}">')
        return html[start:html.index("</article>", start) + 10]

    @staticmethod
    def page_body(html):
        start = html.index('<article class="section finding-body" id="content">')
        return html[start:html.index("</article>", start) + 10]

    def test_future_deadline_on_list_home_and_page_after_agenda_item_before_kit(self):
        self.add_due("due", DUE, kit="Synthetic kit text")
        out = self.build()
        for html in (self.article(out["list"], "due"), self.article(out["home"], "due"), self.page_body(out["due"])):
            self.assertEqual(LINE.findall(html), [(DUE, DUE_TEXT)])
            self.assertLess(html.index("Item 7: ALPR contract"), html.index("comment-deadline"))
            self.assertLess(html.index("comment-deadline"), html.index("Synthetic kit text"))
        page = self.page_body(out["due"])
        self.assertLess(page.index("comment-deadline"), page.index("<h3>"))

    def test_passed_or_equal_deadline_reads_closed(self):
        # Build day 2026-11-10: the build instant is 2026-11-10T00:00:00Z; the meeting itself is still upcoming.
        self.add_due("before", "2026-11-09T12:00:00-08:00")  # 2026-11-09T20:00Z, before
        self.add_due("equal", "2026-11-09T16:00:00-08:00")  # exactly the build instant: closed
        self.add_due("after", "2026-11-09T17:00:00-08:00")  # 2026-11-10T01:00Z: still open on the same calendar day
        out = self.build(today=date(2026, 11, 10))
        for meeting_id, deadline in (("before", "2026-11-09T12:00:00-08:00"), ("equal", "2026-11-09T16:00:00-08:00")):
            for html in (self.article(out["list"], meeting_id), self.article(out["home"], meeting_id),
                         self.page_body(out[meeting_id])):
                self.assertEqual(LINE.findall(html), [(deadline, CLOSED)])
        self.assertEqual(LINE.findall(self.page_body(out["after"])), [(DUE, DUE_TEXT)])
        self.assertNotIn("Written comments due", self.page_body(out["before"]))

    def test_past_meeting_keeps_closed_line(self):
        self.add_due("held", "2026-10-09T17:00:00-07:00", starts_at="2026-10-10T18:00:00-07:00")
        out = self.build(today=date(2026, 10, 20))
        self.assertEqual(LINE.findall(self.article(out["list"], "held"))[0][1], CLOSED)
        self.assertNotIn("comment-deadline", out["home"])

    def test_no_participation_no_written_comment_or_null_deadline_no_line(self):
        for meeting_id, part in BASELINE.items():
            self.add(meeting_id, part)
        out = self.build()
        for name, html in out.items():
            self.assertNotIn("comment-deadline", html, name)

    def test_cancelled_meeting_with_deadline_has_no_line(self):
        self.add_due("off", DUE, status="cancelled")
        out = self.build()
        self.assertIn('<article id="meeting-off">', out["list"])
        for name, html in out.items():
            self.assertNotIn("comment-deadline", html, name)
            self.assertNotIn("Written comment", html, name)

    def test_own_offset_midnight_noon_and_no_host_time_zone(self):
        cases = {"west": ("2026-11-09T00:00:00-07:00", "Monday, November 9, 2026, 12:00 AM (UTC-07:00)"),
                 "utc": ("2026-11-09T12:00:00+00:00", "Monday, November 9, 2026, 12:00 PM (UTC+00:00)"),
                 "late": ("2026-11-08T23:59:00+00:00", "Sunday, November 8, 2026, 11:59 PM (UTC+00:00)")}
        for meeting_id, (deadline, _) in cases.items():
            self.add_due(meeting_id, deadline)
        expected = {k: [(d, "Written comments due " + text)] for k, (d, text) in cases.items()}
        self.assertEqual({k: LINE.findall(self.build()[k]) for k in cases}, expected)
        if not hasattr(time, "tzset"):
            self.skipTest("time.tzset is not available on this platform")
        old = os.environ.get("TZ")
        self.addCleanup(time.tzset)
        self.addCleanup(lambda: os.environ.__setitem__("TZ", old) if old is not None else os.environ.pop("TZ", None))
        os.environ["TZ"] = "Pacific/Kiritimati"
        time.tzset()
        self.assertEqual({k: LINE.findall(self.build()[k]) for k in cases}, expected)

    def test_meetings_without_deadline_byte_identical_to_main(self):
        for meeting_id, part in BASELINE.items():
            self.add(meeting_id, part)
        out = self.build()
        for m in BASELINE:
            self.assertEqual(self.article(out["list"], m), GOLDEN_ARTICLE.format(m=m), m)
            self.assertEqual(self.article(out["home"], m), GOLDEN_ARTICLE.format(m=m), m)
            self.assertEqual(self.page_body(out[m]), GOLDEN_PAGE.format(m=m), m)


if __name__ == "__main__":
    unittest.main()
