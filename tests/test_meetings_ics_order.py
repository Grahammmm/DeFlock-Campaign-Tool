"""RF0325-5: single-feed instant order, not scan_sources' separate aggregate sort.

Synthetic offline feeds; expected orders and timestamp spellings are independent
of the production key. Only the feed transport is replaced.
"""
import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import cli
from campaign_tool.meetings import MeetingsError, relevant_events, resolve_zone
from campaign_tool.meetings_ics import IcsClient

ROOT = Path(__file__).resolve().parents[1]
FEED = "https://example.gov/calendar/council.ics"
BODY = "Example City Council"
SINCE = date(2026, 10, 1)
ZERO = {"floating_time": 0, "unknown_zone": 0, "recurring": 0,
        "uid_too_long": 0, "no_uid": 0, "bad_start": 0}


def event(uid, start, extra="", summary="ALPR"):
    return f"UID:{uid}\r\nDTSTART{start}\r\nSUMMARY:{summary}\r\n{extra}"


def wrap(events):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(
        f"BEGIN:VEVENT\r\n{e}\r\nEND:VEVENT\r\n" for e in events) + "END:VCALENDAR\r\n")


def expected(uid, start, *, known=True, summary="ALPR", terms=None,
             status="scheduled", agenda=None, attachments=None, dropped=0):
    return {"id": "ics-example-city-council-" + hashlib.sha256(uid.encode()).hexdigest()[:12],
            "body": BODY, "starts_at": start, "time_known": known, "agenda_url": agenda,
            "agenda_item": summary, "relevance": "alpr_item", "source": "ics", "event_id": uid,
            "matched_terms": terms if terms is not None else ["alpr"], "matched_items": 1,
            "status": status, "attachments": attachments or [], "attachments_dropped": dropped}


WEST = event("west", ";TZID=America/Los_Angeles:20261112T090000")
EAST = event("east", ";TZID=America/New_York:20261112T100000")
WEST_ROW = expected("west", "2026-11-12T09:00:00-08:00")
EAST_ROW = expected("east", "2026-11-12T10:00:00-05:00")


class OrderTests(unittest.TestCase):
    def read(self, events=(), *, text=None, zone="UTC", since=SINCE, delegate=False):
        urls = []

        def opener(url):
            urls.append(url)
            self.assertEqual(url, FEED)  # no agenda/attachment requests
            return (wrap(events) if text is None else text).encode("utf-8")

        client = IcsClient(FEED, BODY, opener=opener)
        reader = relevant_events if delegate else lambda c, **kw: c.relevant(**kw)
        rows = reader(client, since=since, zone=resolve_zone(zone))
        self.assertEqual(urls, [FEED])
        return rows, client

    def assertRows(self, events, rows, **kwargs):
        for delegate in (False, True):
            with self.subTest(delegate=delegate, **kwargs):
                found, client = self.read(events, delegate=delegate, **kwargs)
                self.assertEqual([m["event_id"] for m in found], [m["event_id"] for m in rows])
                self.assertEqual({m["event_id"]: m for m in found}, {m["event_id"]: m for m in rows})
                self.assertEqual(client.skipped, ZERO)

    def test_single_feed_mixed_zone_wall_clock_inversions_sort_by_instant(self):
        tokyo = event("tokyo", ";TZID=Asia/Tokyo:20261113T080000")
        late = event("late", ";TZID=America/Los_Angeles:20261112T230000")
        cases = [([WEST, EAST], [EAST_ROW, WEST_ROW]),
                 ([late, tokyo], [expected("tokyo", "2026-11-13T08:00:00+09:00"),
                                  expected("late", "2026-11-12T23:00:00-08:00")])]
        for events, rows in cases:
            for order in (events, events[::-1]):
                self.assertRows(order, rows)

    def test_utc_and_tzid_mixed_offsets_across_dst_keep_actual_order(self):
        cases = [
            ([event("pacific", ";TZID=America/Los_Angeles:20261101T011500"),
              event("utc", ":20261101T080000Z")],
             [expected("utc", "2026-11-01T08:00:00+00:00"),
              expected("pacific", "2026-11-01T01:15:00-07:00")], "UTC"),
            ([event("west", ";TZID=America/Los_Angeles:20260712T090000"),
              event("utc", ":20260712T150000Z"),
              event("nepal", ";TZID=Asia/Kathmandu:20260712T203000")],
             [expected("nepal", "2026-07-12T20:30:00+05:45"),
              expected("utc", "2026-07-12T11:00:00-04:00"),
              expected("west", "2026-07-12T09:00:00-07:00")], "America/New_York"),
            ([WEST, event("utc", ":20261112T150000Z")],
             [expected("utc", "2026-11-12T10:00:00-05:00"), WEST_ROW], "America/New_York")]
        for events, rows, zone in cases:
            for order in (events, events[::-1]):
                self.assertRows(order, rows, zone=zone, since=date(2026, 1, 1))

    def test_equal_instant_legacy_lexical_ties_and_identical_key_stability(self):
        east_tie = event("east", ";TZID=America/New_York:20261112T120000")
        tie_row = expected("east", "2026-11-12T12:00:00-05:00")
        for order in ([WEST, east_tie], [east_tie, WEST]):
            self.assertRows(order, [WEST_ROW, tie_row])
        a = event("z-first", ":20261112T160000Z")
        b = event("a-second", ":20261112T160000Z")
        for middle in ([a, b], [b, a]):
            self.assertRows([WEST, middle[0], EAST, middle[1]], [EAST_ROW, *[
                expected(uid, "2026-11-12T16:00:00+00:00") for uid in
                (["z-first", "a-second"] if middle[0] == a else ["a-second", "z-first"])], WEST_ROW])

    def test_date_only_anchor_and_existing_since_admission_are_preserved(self):
        events = [event("day", ";VALUE=DATE:20261112"),
                  event("before", ";TZID=Etc/GMT+12:20261111T235900"),
                  event("on", ";TZID=Asia/Tokyo:20261112T003000"),
                  event("after", ";VALUE=DATE:20261113")]
        for zone, anchor in [("UTC", "+00:00"), ("America/Los_Angeles", "-08:00")]:
            self.assertRows(events, [expected("on", "2026-11-12T00:30:00+09:00"),
                                    expected("day", "2026-11-12T00:00:00" + anchor, known=False),
                                    expected("after", "2026-11-13T00:00:00" + anchor, known=False)],
                            zone=zone, since=date(2026, 11, 12))
        self.assertRows([], [])

    def test_supported_year_endpoints_sort_without_utc_overflow(self):
        events = [event("last", ";TZID=Etc/GMT+12:99991231T235959"),
                  event("middle-b", ":20261112T160000Z"),
                  event("first", ";TZID=Etc/GMT-14:00010101T000000"),
                  event("middle-a", ":20261112T150000Z"),
                  event("first-tie", ";TZID=Etc/GMT-14:00010101T000000")]
        for order in (events, events[::-1]):
            first = ["first", "first-tie"] if order == events else ["first-tie", "first"]
            self.assertRows(order, [*[expected(uid, "0001-01-01T00:00:00+14:00") for uid in first],
                                    expected("middle-a", "2026-11-12T15:00:00+00:00"),
                                    expected("middle-b", "2026-11-12T16:00:00+00:00"),
                                    expected("last", "9999-12-31T23:59:59-12:00")], since=date.min)

    def test_revision_selection_and_skip_accounting_precede_ordering(self):
        old = event("changed", ";TZID=America/Los_Angeles:20261112T070000", "SEQUENCE:0")
        winner = event("changed", ";TZID=America/Los_Angeles:20261112T090000",
                       "SEQUENCE:1\r\nSTATUS:CANCELLED")
        expired = event("expired", ":20261112T200000Z", "SEQUENCE:0")
        expired_new = event("expired", ":20260901T200000Z", "SEQUENCE:1")
        skipped = [event("floating", ":20261112T080000"),
                   event("unknown", ";TZID=Mars/Base:20261112T080000"),
                   event("recurring", ":20261112T080000Z", "RRULE:FREQ=WEEKLY")]
        for order in ([old, EAST, winner, winner, expired, expired_new, *skipped],
                      [*skipped, expired_new, expired, winner, EAST, old]):
            rows, client = self.read(order)
            self.assertEqual(rows, [EAST_ROW, expected("changed", "2026-11-12T09:00:00-08:00", status="cancelled")])
            self.assertEqual(client.skipped, dict(ZERO, floating_time=1, unknown_zone=1, recurring=1))
        for text in (wrap([old, winner]) .replace("END:VCALENDAR", ""),
                     wrap([winner, winner.replace("STATUS:CANCELLED", "STATUS:CONFIRMED")])):
            with self.assertRaises(MeetingsError):
                self.read(text=text)

    def test_legacy_fixture_rows_and_network_bound_remain_exact(self):
        specs = [
            ("dst-1@example.gov", "2026-11-01T01:30:00-07:00", {"summary": "ALPR late session"}),
            ("tz-1@example.gov", "2026-11-12T18:00:00-08:00",
             {"summary": "Item 7: Automated License Plate Readers contract", "terms": ["license plate"],
              "agenda": "https://example.gov/agenda/1",
              "attachments": [{"name": "staff-report.pdf", "url": "https://example.gov/files/a.pdf"}]}),
            ("utc-1@example.gov", "2026-11-12T18:00:00-08:00", {"summary": "Flock camera review", "terms": ["flock"]}),
            ("allday-1@example.gov", "2026-11-15T00:00:00-08:00", {"summary": "ALPR community day", "known": False}),
            ("cancelled-1@example.gov", "2026-11-19T18:00:00-08:00", {"summary": "ALPR policy hearing", "status": "cancelled"}),
            ("http-1@example.gov", "2026-11-22T18:00:00-08:00", {"summary": "ALPR over http", "dropped": 1})]
        text = (ROOT / "tests/fixtures/meetings_ics/basic.ics").read_text().replace("LONGUID", "u" * 201 + "@example.gov")
        rows, client = self.read(text=text, zone="America/Los_Angeles")
        self.assertEqual(rows, [expected(uid, start, **fields) for uid, start, fields in specs])
        self.assertEqual(client.skipped, dict(ZERO, floating_time=1, unknown_zone=1, recurring=2, uid_too_long=1))
        folded = (ROOT / "tests/fixtures/meetings_ics/folded.ics").read_text()
        rows, client = self.read(text=folded, zone="America/Los_Angeles")
        self.assertEqual(rows, [expected("fold-1@example.gov", "2026-11-12T18:00:00-08:00",
                                         summary="ALPR contract, phase 2; approve\\return")])
        self.assertEqual(client.skipped, ZERO)

    def test_single_source_cli_persists_actual_order_without_false_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            shutil.copytree(ROOT / "examples/fictional-campaign", root)
            target = root / "kit/meetings.json"
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            feed = Path(tmp) / "feed.ics"
            previous_rows = None
            for events in ([WEST, EAST], [EAST, WEST]):
                feed.write_text(wrap(events), encoding="utf-8")
                argv = ["campaign_tool", "meetings", "--directory", str(root), "--platform", "ics",
                        "--feed-url", FEED, "--body", BODY, "--from-ics", str(feed), "--tz", "UTC",
                        "--since", "2026-10-01"]
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                        mock.patch("campaign_tool.meetings.default_opener", side_effect=AssertionError("network")):
                    cli.main()
                self.assertEqual(err.getvalue(), "")
                report = json.loads(out.getvalue())
                self.assertEqual((report["platform"], report["skipped"], report["cancelled_meetings"]), ("ics", ZERO, 0))
                doc = json.loads(target.read_text())
                self.assertEqual((doc["schema_version"], doc["source"], doc["client"], doc["time_zone"]), (1, "ics", BODY, "UTC"))
                self.assertEqual(doc["meetings"], [EAST_ROW, WEST_ROW])
                if previous_rows is None:
                    self.assertNotIn("changes", doc)
                    self.assertEqual(report["previous"], "none")
                else:
                    self.assertEqual(doc["meetings"], previous_rows)
                    self.assertEqual(set(doc["changes"]), {"previous", "previous_generated", "added", "removed",
                                                           "rescheduled", "agenda_changed", "past", "cancelled"})
                    self.assertEqual(doc["changes"]["previous"], "compared")
                    for key in ("added", "removed", "rescheduled", "agenda_changed", "past", "cancelled"):
                        self.assertEqual(doc["changes"][key], [])
                previous_rows = doc["meetings"]
            after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(set(after) - set(before), {Path("kit/meetings.json")})
            self.assertEqual({p: data for p, data in after.items() if p != Path("kit/meetings.json")}, before)


if __name__ == "__main__":
    unittest.main()
