"""DF1737-04: public/meetings.ics, the upcoming meetings as an RFC 5545 calendar, linked from the meetings page.

Synthetic content only (temp copies of the fictional example); no network.
"""
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.cli import check_public_tree
from campaign_tool.site import build_site, ics_text

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BUILD_DAY = date(2026, 10, 20)
HOST = "cedar-records.example.invalid"


def unfold(text):
    return text.replace("\r\n ", "")


def events(text):
    """One dict of property name -> value per VEVENT, read from the unfolded feed."""
    found = []
    for block in unfold(text).split("BEGIN:VEVENT\r\n")[1:]:
        body = block.split("END:VEVENT\r\n")[0]
        found.append(dict(line.split(":", 1) for line in body.split("\r\n") if line))
    return found


class MeetingsIcsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meetings-ics-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        for path in self.meetings.glob("*.json"):  # the example meeting is past on the build day
            path.unlink()

    def add(self, meeting_id, starts_at, **extra):
        record = {"id": meeting_id, "body": f"Synthetic council {meeting_id} (fictional)", "starts_at": starts_at,
                  "agency_id": "ca-cedar-city-council", **extra}
        (self.meetings / f"{meeting_id}.json").write_text(json.dumps(record), encoding="utf-8")

    def set_base_url(self, value):
        path = self.root / "content" / "site.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["base_url"] = value
        path.write_text(json.dumps(document), encoding="utf-8")

    def build(self, today=BUILD_DAY):
        build_site(self.root, today=today)
        return (self.root / "public" / "meetings.ics").read_bytes().decode("utf-8")

    def test_calendar_shape_and_exact_uid_dtstart_description(self):
        self.add("m1", "2026-10-28T09:30:00+00:00", agenda_url="https://example.invalid/agenda/m1",
                 agenda_item="Item 4: camera renewal", location="Cedar Civic Center (fictional)")
        self.add("m2", "2026-11-18T18:00:00-08:00")
        text = self.build()
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        self.assertEqual(text.count("\n"), text.count("\r\n"))  # every line ends CRLF
        for line in text.split("\r\n")[:-1]:
            self.assertLessEqual(len(line.encode("utf-8")), 75, line)
        for header in ("VERSION:2.0", "PRODID:-//DeFlock Campaign Tool//meetings//EN", "CALSCALE:GREGORIAN"):
            self.assertIn("\r\n" + header + "\r\n", text)
        self.assertEqual(text.count("BEGIN:VEVENT"), 2)
        first, second = events(text)
        self.assertEqual(first["UID"], f"meeting-m1@{HOST}")
        self.assertEqual(first["DTSTART"], "20261028T093000Z")
        self.assertEqual(first["DTSTAMP"], "20261020T000000Z")
        self.assertEqual(first["SUMMARY"], "Synthetic council m1 (fictional)")
        self.assertEqual(first["URL"], "https://example.invalid/agenda/m1")
        self.assertEqual(first["LOCATION"], "Cedar Civic Center (fictional)")
        self.assertEqual(first["DESCRIPTION"], f"Item 4: camera renewal\\nhttps://{HOST}/meetings.html#meeting-m1")
        self.assertEqual(second["UID"], f"meeting-m2@{HOST}")
        self.assertNotIn("DTEND", text)

    def test_offset_converted_across_the_utc_date_line(self):
        self.add("late", "2026-11-03T18:00:00-08:00")
        self.assertEqual(events(self.build())[0]["DTSTART"], "20261104T020000Z")

    def test_cancelled_and_past_meetings_absent_but_still_on_the_page(self):
        self.add("past", "2026-10-19T18:00:00-07:00")
        self.add("cancelled", "2026-11-05T18:00:00-08:00", status="cancelled")
        self.add("kept", "2026-11-06T18:00:00-08:00")
        text = self.build()
        self.assertEqual([e["UID"] for e in events(text)], [f"meeting-kept@{HOST}"])
        self.assertNotIn("past", text.replace("PRODID", ""))
        page = (self.root / "public" / "meetings.html").read_text(encoding="utf-8")
        self.assertIn('id="meeting-past"', page)
        self.assertIn('id="meeting-cancelled"', page)

    def test_meeting_on_the_build_day_is_included(self):
        self.add("today", "2026-10-20T18:00:00-07:00")
        self.assertEqual([e["UID"] for e in events(self.build())], [f"meeting-today@{HOST}"])

    def test_text_escaping_exact(self):
        self.add("esc", "2026-11-06T18:00:00-08:00", agenda_item="a, b; c\\d\ne")
        record = json.loads((self.meetings / "esc.json").read_text(encoding="utf-8"))
        record["body"] = "One, two; three\\four\nfive"
        (self.meetings / "esc.json").write_text(json.dumps(record), encoding="utf-8")
        event = events(self.build())[0]
        self.assertEqual(event["SUMMARY"], "One\\, two\\; three\\\\four\\nfive")
        self.assertTrue(event["DESCRIPTION"].startswith("a\\, b\\; c\\\\d\\ne\\nhttps://"))

    def test_control_characters_are_removed_from_text_values(self):
        self.assertEqual(ics_text("a\r\nb\x07c\x7fd\u0085e"), "a\\nbcde")

    def test_long_multibyte_body_folds_on_character_boundaries_and_unfolds_back(self):
        body = "Réunion du conseil — caméras ✓ " * 7  # well over 200 characters, multibyte
        body = body[:200]
        self.add("long", "2026-11-06T18:00:00-08:00")
        record = json.loads((self.meetings / "long.json").read_text(encoding="utf-8"))
        record["body"] = body
        (self.meetings / "long.json").write_text(json.dumps(record), encoding="utf-8")
        text = self.build()
        self.assertIn("\r\n ", text)  # it did fold
        for line in text.split("\r\n")[:-1]:
            self.assertLessEqual(len(line.encode("utf-8")), 75)
            line.encode("utf-8").decode("utf-8")  # no split sequence: each physical line is valid UTF-8 on its own
        self.assertEqual(events(text)[0]["SUMMARY"], body.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,"))

    def test_unset_optional_properties_are_omitted_not_empty(self):
        self.add("bare", "2026-11-06T18:00:00-08:00")
        text = self.build()
        for name in ("LOCATION", "URL"):
            self.assertNotIn("\r\n" + name, text)
        description = events(text)[0]["DESCRIPTION"]
        self.assertEqual(description, f"https://{HOST}/meetings.html#meeting-bare")  # no agenda item, no blank line

    def test_empty_base_url_uses_invalid_host_and_path_only_link(self):
        self.set_base_url(None)
        self.add("nobase", "2026-11-06T18:00:00-08:00")
        event = events(self.build())[0]
        self.assertEqual(event["UID"], "meeting-nobase@deflock-campaign.invalid")
        self.assertEqual(event["DESCRIPTION"], "/meetings.html#meeting-nobase")

    def test_no_upcoming_meetings_is_a_valid_empty_calendar(self):
        self.add("old", "2026-10-01T18:00:00-07:00")
        text = self.build()
        self.assertEqual(text.count("BEGIN:VEVENT"), 0)
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n"))

    def test_deterministic_leak_scan_clean_page_link_and_new_day_changes_stamp(self):
        self.add("m1", "2026-11-06T18:00:00-08:00")
        first = self.build()
        self.assertEqual(self.build(), first)  # same content, same day: byte-identical
        self.assertEqual(check_public_tree(self.root / "public"), [])
        page = (self.root / "public" / "meetings.html").read_text(encoding="utf-8")
        self.assertIn('<a href="/meetings.ics">Subscribe to the meetings calendar (.ics)</a>', page)
        self.assertIn("DTSTAMP:20261021T000000Z", self.build(date(2026, 10, 21)))

    def test_meeting_that_has_become_past_drops_out_on_a_later_build(self):
        self.add("m1", "2026-10-25T18:00:00-07:00")
        self.assertEqual(len(events(self.build())), 1)
        self.assertEqual(events(self.build(date(2026, 10, 26))), [])

    def test_starter_mode_writes_no_calendar(self):
        shutil.rmtree(self.root / "content")
        build_site(self.root, today=BUILD_DAY)
        self.assertFalse((self.root / "public" / "meetings.ics").exists())


if __name__ == "__main__":
    unittest.main()
