"""DF1407-09: inert schema.org Event data on staged meeting pages (synthetic content only)."""
import json
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.content import ContentError
from campaign_tool.site import build_site

from tests.test_site_build import EXAMPLE

TODAY = date(2026, 10, 1)
MEETING = "meetings/cedar-council-2026-10.html"
BLOCK = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
KEYS = {"@context", "@type", "name", "startDate", "eventStatus", "location", "url", "organizer"}
BASE = "https://cedar-records.example.invalid"
MODES = {"mixed": "https://schema.org/MixedEventAttendanceMode", "offline": "https://schema.org/OfflineEventAttendanceMode",
         "online": "https://schema.org/OnlineEventAttendanceMode"}
SENTINEL = "SENTINEL-kit-outcome-vote-verifier-contact"


def participation(in_person=None, remote=None):
    return {"in_person": in_person, "remote": None if remote is None else {"available": remote, "how": "SENTINEL-how"},
            "written_comment": None, "speaker_minutes": None, "source_url": "https://example.invalid/notice",
            "verified_by": "SENTINEL-verifier", "verified_at": "2026-09-20"}


class Case(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-event-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"

    def edit(self, name, change):
        path = self.root / "content" / name
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))

    def meeting(self, **fields):
        self.edit("meetings/2026-10-cedar-council.json", lambda d: d.update(fields))

    def build(self, today=TODAY):
        build_site(self.root, today=today)
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}

    def event(self, **fields):
        if fields:
            self.meeting(**fields)
        files = self.build()
        (raw,) = BLOCK.findall(files[MEETING].decode())
        return json.loads(raw)


class EventTests(Case):
    def test_exact_fields_and_scope(self):
        files = self.build()
        (raw,) = BLOCK.findall(files[MEETING].decode())
        event = json.loads(raw)
        self.assertEqual(event, {
            "@context": "https://schema.org", "@type": "Event",
            "name": "Cedar City Council (fictional) \u2014 Item 7: consent calendar, technology renewals",
            "startDate": "2026-10-14T18:00:00-07:00", "eventStatus": "https://schema.org/EventScheduled",
            "location": "Cedar Civic Center (fictional)", "url": BASE + "/meetings/cedar-council-2026-10.html",
            "organizer": "Cedar City Council (fictional)"})
        for name, data in files.items():
            if name.endswith(".html") and name not in (MEETING, "index.html", "findings/cedar-policy-posted.html"):
                self.assertNotIn(b"ld+json", data, name)

    def test_name_without_agenda_and_empty_location(self):
        event = self.event(agenda_item="", location="")
        self.assertEqual(event["name"], "Cedar City Council (fictional)")
        self.assertEqual(event["location"], "")
        self.assertEqual(set(event), KEYS)

    def test_cancelled_and_past_meetings_keep_the_block(self):
        self.assertEqual(self.event(status="cancelled")["eventStatus"], "https://schema.org/EventCancelled")
        self.meeting(status="scheduled")
        shutil.rmtree(self.public)
        files = self.build(today=date(2026, 12, 1))  # the meeting is now past
        (raw,) = BLOCK.findall(files[MEETING].decode())
        self.assertEqual(json.loads(raw)["eventStatus"], "https://schema.org/EventScheduled")

    def test_start_date_keeps_the_stored_offset(self):
        for value in ("2026-10-14T23:30:00+14:00", "2026-10-14T00:15:00-07:00", "2026-10-14T18:00:00Z"):
            shutil.rmtree(self.public, ignore_errors=True)
            self.assertEqual(self.event(starts_at=value)["startDate"], value)

    def test_attendance_mode_only_from_verified_positive_availability(self):
        table = [(None, None), (participation(None, None), None), (participation(True, True), "mixed"),
                 (participation(True, None), "offline"), (participation(None, True), "online"),
                 (participation(True, False), "offline"), (participation(False, True), "online"),
                 (participation(False, False), None), (participation(False, None), None), (participation(None, False), None)]
        for value, mode in table:
            with self.subTest(participation=value and (value["in_person"], value["remote"] and value["remote"]["available"])):
                shutil.rmtree(self.public, ignore_errors=True)
                event = self.event(participation=value)
                if mode is None:
                    self.assertNotIn("eventAttendanceMode", event)
                    self.assertEqual(set(event), KEYS)
                else:
                    self.assertEqual(event["eventAttendanceMode"], MODES[mode])
                    self.assertEqual(set(event), KEYS | {"eventAttendanceMode"})

    def test_location_text_mentioning_zoom_is_not_evidence(self):
        event = self.event(location="Zoom or Cedar Civic Center")
        self.assertNotIn("eventAttendanceMode", event)

    def test_cancelled_with_participation_has_both_status_and_mode(self):
        event = self.event(status="cancelled", participation=participation(True, True))
        self.assertEqual(event["eventStatus"], "https://schema.org/EventCancelled")
        self.assertEqual(event["eventAttendanceMode"], MODES["mixed"])

    def test_private_fields_never_enter_the_block(self):
        self.meeting(comment_kit_md=SENTINEL + " kit", participation=participation(True, True))
        raw = BLOCK.findall(self.build()[MEETING].decode())[0]
        self.assertNotIn("SENTINEL", raw)
        self.assertNotIn("example.invalid/notice", raw)

    def test_raw_text_cannot_close_the_element(self):
        body = 'A &lt;/script&gt; & "q" \\ {x} \u00e9'
        self.meeting(body=body)
        files = self.build()
        html = files[MEETING].decode()
        blocks = BLOCK.findall(html)
        self.assertEqual(len(blocks), 1)
        for char in "<>&":
            self.assertNotIn(char, blocks[0])
        event = json.loads(blocks[0])
        self.assertEqual(event["organizer"], body)
        self.assertEqual(event["name"].startswith(body), True)
        self.assertEqual(html.count("<script"), html.count("</script>"))


class RefusalAndNoBaseTests(Case):
    def test_invalid_status_refuses_and_keeps_the_previous_tree(self):
        before = self.build()
        self.meeting(status="postponed")
        with self.assertRaises(ContentError):
            self.build()
        after = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}
        self.assertEqual(before, after)

    def test_without_base_url_no_event_and_builds_repeat(self):
        for omit in (False, True):
            shutil.rmtree(self.public, ignore_errors=True)
            self.edit("site.json", (lambda d: d.pop("base_url", None)) if omit else (lambda d: d.update(base_url=None)))
            first = self.build()
            self.assertNotIn(b"ld+json", first[MEETING])
            shutil.rmtree(self.public)
            self.assertEqual(first, self.build())


if __name__ == "__main__":
    unittest.main()
