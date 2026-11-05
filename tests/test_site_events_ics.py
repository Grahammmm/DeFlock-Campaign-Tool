"""DF1107-10: content/events.json -> public/events.ics, written with the meetings.ics writer. Offline, synthetic data."""
import json
import unittest
from datetime import date

from campaign_tool.content import ContentError
from campaign_tool.site import build_site
from tests.test_site_events import TODAY, EventsCase, event


def unfold(text):
    return text.replace("\r\n ", "")


class EventsIcsTests(EventsCase):
    def feed(self, events, today=TODAY):
        self.put(events)
        build_site(self.root, today=today)
        return (self.public / "events.ics").read_bytes().decode("utf-8")

    def set_base(self, base):
        site = self.content / "site.json"
        document = json.loads(site.read_text(encoding="utf-8"))
        document.pop("base_url", None)
        if base:
            document["base_url"] = base
        site.write_text(json.dumps(document), encoding="utf-8")

    def test_event_properties_in_order_and_utc(self):
        self.set_base("https://forum.example:8443/prefix")
        text = self.feed([event(1, starts_at="2026-10-10T23:30:00-07:00", ends_at="2026-10-11T01:00:00-07:00")])
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        lines = text.split("\r\n")[:-1]
        self.assertEqual(lines[:4], ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//DeFlock Campaign Tool//events//EN", "CALSCALE:GREGORIAN"])
        self.assertTrue(lines[4].startswith("X-WR-CALNAME:") and lines[4].endswith(" campaign events"))
        self.assertEqual(lines[5:], ["BEGIN:VEVENT", "UID:event-forum-1@forum.example", "DTSTAMP:20261001T000000Z",
                                     "DTSTART:20261011T063000Z", "DTEND:20261011T080000Z", "SUMMARY:Teach-in 1",
                                     "LOCATION:Cedar Library room 1",
                                     "URL:https://forum.example:8443/prefix/events.html#event-forum-1", "END:VEVENT", "END:VCALENDAR"])

    def test_no_base_uses_fallback_host_and_path_url_and_online_location(self):
        self.set_base(None)
        text = self.feed([event(1, venue=None, online=True)])
        self.assertIn("UID:event-forum-1@deflock-campaign.invalid\r\n", text)
        self.assertIn("LOCATION:Online\r\n", text)
        self.assertIn("URL:/events.html#event-forum-1\r\n", text)

    def test_differing_offsets_keep_elapsed_time(self):
        text = self.feed([event(1, starts_at="2026-11-01T00:30:00-07:00", ends_at="2026-11-01T01:30:00-08:00")])
        self.assertIn("DTSTART:20261101T073000Z\r\n", text)
        self.assertIn("DTEND:20261101T093000Z\r\n", text)

    def test_zero_duration_event_omits_dtend(self):
        text = self.feed([event(1, starts_at="2026-10-10T18:00:00Z", ends_at="2026-10-10T18:00:00Z")])
        self.assertIn("DTSTART:20261010T180000Z\r\n", text)
        self.assertNotIn("DTEND", text)

    def test_past_left_out_all_past_is_valid_empty_feed_with_links(self):
        mixed = self.feed([event(1, starts_at="2026-09-30T23:59:59Z", ends_at="2026-10-01T01:00:00Z"), event(2)])
        self.assertNotIn("event-forum-1@", mixed)
        self.assertIn("event-forum-2@", mixed)
        past = self.feed([event(1, starts_at="2026-09-30T10:00:00Z", ends_at="2026-09-30T11:00:00Z")])
        self.assertEqual(past.count("BEGIN:VEVENT"), 0)
        self.assertTrue(past.startswith("BEGIN:VCALENDAR\r\n") and past.endswith("END:VCALENDAR\r\n"))
        page = (self.public / "events.html").read_text(encoding="utf-8")
        self.assertIn('rel="alternate" type="text/calendar"', page)
        self.assertIn("Subscribe to the campaign events calendar (.ics)", page)

    def test_sorted_by_start_then_id_and_source_order_does_not_matter(self):
        a, b, c = event(1, id="b-one"), event(2, id="a-one"), event(3, id="c-early", starts_at="2026-10-09T00:00:00Z", ends_at="2026-10-09T01:00:00Z")
        first = self.feed([a, b, c])
        self.assertEqual(first, self.feed([c, b, a]))
        order = [line for line in first.split("\r\n") if line.startswith("UID:")]
        self.assertEqual([line.split("@")[0] for line in order], ["UID:event-c-early", "UID:event-a-one", "UID:event-b-one"])

    def test_same_day_builds_identical_next_day_changes_stamp(self):
        events = [event(1)]
        self.assertEqual(self.feed(events), self.feed(events))
        self.assertIn("DTSTAMP:20261002T000000Z", self.feed(events, today=date(2026, 10, 2)))

    def test_escaping_folding_and_no_injection(self):
        title = "Fish; chips, \\ end BEGIN:VEVENT" + "é" * 80
        text = self.feed([event(1, title=title, venue="Hall, Room; 5 " + "ü" * 80)])
        for physical in text.split("\r\n")[:-1]:
            self.assertLessEqual(len(physical.encode("utf-8")), 75)
        flat = unfold(text)
        self.assertIn("SUMMARY:Fish\\; chips\\, \\\\ end BEGIN:VEVENT" + "é" * 80 + "\r\n", flat)
        self.assertIn("LOCATION:Hall\\, Room\\; 5 " + "ü" * 80 + "\r\n", flat)
        self.assertEqual(len([l for l in flat.split("\r\n") if l == "BEGIN:VEVENT"]), 1)
        from campaign_tool.site import ics_text
        self.assertEqual(ics_text("a\x07b\r\nc"), "ab\\nc")

    def test_meetings_ics_unchanged_and_off_state_has_no_feed_or_links(self):
        build_site(self.root, today=TODAY)
        before = self.tree()
        self.assertNotIn("events.ics", before)
        self.assertNotIn(b"events.ics", b"".join(before.values()))
        self.put([event(1)])
        build_site(self.root, today=TODAY)
        on = self.tree()
        self.assertEqual(on["meetings.ics"], before["meetings.ics"])
        self.assertIn(b'<a href="/events.ics">Campaign events calendar (.ics)</a>', on["index.html"])
        self.assertNotIn(b"text/calendar", on["index.html"])
        self.assertIn(b"text/calendar", on["events.html"])
        self.path.unlink()
        build_site(self.root, today=TODAY)
        self.assertEqual(self.tree(), before)
        self.put([])
        build_site(self.root, today=TODAY)
        self.assertEqual(self.tree(), before)

    def test_reserved_name_and_privacy_gate(self):
        from campaign_tool.site import GENERATED_TOP_LEVEL
        self.assertIn("events.ics", GENERATED_TOP_LEVEL)
        for field, value in (("title", "Call 555-123-4567 now"), ("venue", "Write a@example.com")):
            self.put([event(1, **{field: value})])
            with self.assertRaises(ContentError) as raised:
                build_site(self.root, today=TODAY)
            self.assertNotIn(value, str(raised.exception))

    def test_malformed_events_keep_previous_tree(self):
        self.feed([event(1)])
        before = self.tree()
        self.put([event(1), event(1)])  # duplicate id
        with self.assertRaises(ContentError):
            build_site(self.root, today=TODAY)
        self.assertEqual(self.tree(), before)


if __name__ == "__main__":
    unittest.main()
