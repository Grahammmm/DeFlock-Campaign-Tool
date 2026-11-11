"""RF2150-4: a UTC DTSTART whose requested-zone form leaves years 1-9999 is a bad_start, not a crash.

Synthetic offline feeds and the real ZoneInfo conversion; only the feed transport is replaced.
"""
import contextlib
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
from campaign_tool.meetings_ics import IcsClient, _start, parse_events

ROOT = Path(__file__).resolve().parents[1]
FEED = "https://example.gov/calendar/council.ics"
BODY = "Example City Council"
SINCE = date(2026, 10, 1)
KEYS = ("floating_time", "unknown_zone", "recurring", "uid_too_long", "no_uid", "bad_start")


def event(uid, start, extra=""):
    return f"UID:{uid}\r\nDTSTART{start}\r\nSUMMARY:ALPR\r\n{extra}"


def wrap(events):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(
        f"BEGIN:VEVENT\r\n{e}\r\nEND:VEVENT\r\n" for e in events) + "END:VCALENDAR\r\n")


HEALTHY = event("healthy", ":20261112T180000Z")
LOW_BAD = event("low", ":00010101T000000Z")      # year 0 in Etc/GMT+12
HIGH_BAD = event("high", ":99991231T235959Z")    # year 10000 in Etc/GMT-14


def parsed(start, zone):
    (ev,) = parse_events(wrap([event("x", start)]))
    return _start(ev, resolve_zone(zone))


class StartRangeTests(unittest.TestCase):
    def read(self, events, zone, since=SINCE, delegate=False):
        client = IcsClient(FEED, BODY, opener=lambda url: wrap(events).encode())
        reader = relevant_events if delegate else lambda c, **kw: c.relevant(**kw)
        return reader(client, since=since, zone=resolve_zone(zone)), client

    def test_utc_requested_zone_range_failures_return_bad_start(self):
        for start, zone in [(":00010101T000000Z", "Etc/GMT+12"), (":99991231T235959Z", "Etc/GMT-14"),
                            (":00010101T000000Z", "America/Los_Angeles")]:
            with self.subTest(start=start, zone=zone):
                self.assertEqual(parsed(start, zone), "bad_start")

    def test_representable_boundary_controls_remain_exact(self):
        self.assertEqual(parsed(":00010101T000000Z", "UTC"), ("0001-01-01T00:00:00+00:00", True))
        self.assertEqual(parsed(":99991231T235959Z", "UTC"), ("9999-12-31T23:59:59+00:00", True))
        self.assertEqual(parsed(":00010101T120000Z", "Etc/GMT+12"), ("0001-01-01T00:00:00-12:00", True))
        self.assertEqual(parsed(":99991231T095959Z", "Etc/GMT-14"), ("9999-12-31T23:59:59+14:00", True))
        # accepted TZID-local endpoints stay accepted though UTC normalisation would overflow
        self.assertEqual(parsed(";TZID=Etc/GMT-14:00010101T000000", "UTC"), ("0001-01-01T00:00:00+14:00", True))
        self.assertEqual(parsed(";TZID=Etc/GMT+12:99991231T235959", "UTC"), ("9999-12-31T23:59:59-12:00", True))

    def test_mixed_feed_skips_one_event_without_losing_siblings(self):
        cases = [("Etc/GMT+12", LOW_BAD), ("Etc/GMT-14", HIGH_BAD)]
        for zone, bad in cases:
            for order in ([bad, HEALTHY], [HEALTHY, bad], [HEALTHY, bad, event("h2", ":20261113T180000Z")]):
                for delegate in (False, True):
                    with self.subTest(zone=zone, delegate=delegate, n=len(order)):
                        rows, client = self.read(order, zone, delegate=delegate)
                        self.assertEqual(sorted(m["event_id"] for m in rows),
                                         sorted(["healthy"] + (["h2"] if len(order) == 3 else [])))
                        self.assertEqual(client.skipped, dict.fromkeys(KEYS, 0) | {"bad_start": 1})
        both = [event("low2", ":00010101T000000Z"), LOW_BAD]
        rows, client = self.read(both, "Etc/GMT+12")
        self.assertEqual((rows, client.skipped["bad_start"]), ([], 2))

    def test_cutoff_and_earlier_skips_are_unchanged(self):
        rows, client = self.read([LOW_BAD], "Etc/GMT+12", since=date(2026, 10, 1))
        self.assertEqual((rows, client.skipped["bad_start"]), ([], 1))  # counted before the since cutoff
        recurring = event("rec", ":00010101T000000Z", "RRULE:FREQ=WEEKLY")
        rows, client = self.read([recurring], "Etc/GMT+12")
        self.assertEqual((client.skipped["recurring"], client.skipped["bad_start"]), (1, 0))

    def test_revision_winner_owns_range_validation(self):
        old_good = event("u", ":20261112T180000Z", "SEQUENCE:0")
        new_bad = event("u", ":00010101T000000Z", "SEQUENCE:1")
        for order in ([old_good, new_bad], [new_bad, old_good]):
            rows, client = self.read(order, "Etc/GMT+12")
            self.assertEqual((rows, client.skipped["bad_start"]), ([], 1))
        old_bad = event("u", ":00010101T000000Z", "SEQUENCE:0")
        new_good = event("u", ":20261112T180000Z", "SEQUENCE:1")
        for order in ([old_bad, new_good], [new_good, old_bad]):
            rows, client = self.read(order, "Etc/GMT+12")
            self.assertEqual(([m["event_id"] for m in rows], client.skipped["bad_start"]), (["u"], 0))
        with self.assertRaises(MeetingsError):  # malformed structure is still a source failure
            IcsClient(FEED, BODY, opener=lambda url: wrap([LOW_BAD, HEALTHY]).replace("END:VCALENDAR", "").encode()
                      ).relevant(since=SINCE, zone=resolve_zone("Etc/GMT+12"))

    def test_reused_client_counters_do_not_retain_prior_bad_starts(self):
        feeds = iter([wrap([LOW_BAD, HEALTHY]), wrap([HEALTHY]), wrap([LOW_BAD])])
        client = IcsClient(FEED, BODY, opener=lambda url: next(feeds).encode())
        zone = resolve_zone("Etc/GMT+12")
        counts = []
        for _ in range(3):
            client.relevant(since=SINCE, zone=zone)
            counts.append(client.skipped["bad_start"])
        self.assertEqual(counts, [1, 0, 1])

    def test_offline_cli_persists_healthy_siblings_and_reports_range_skips(self):
        for zone, bad in [("Etc/GMT+12", LOW_BAD), ("Etc/GMT-14", HIGH_BAD)]:
            with self.subTest(zone=zone), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "campaign"
                shutil.copytree(ROOT / "examples/fictional-campaign", root)
                before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                feed = Path(tmp) / "feed.ics"
                feed.write_text(wrap([bad, HEALTHY]), encoding="utf-8")
                argv = ["campaign_tool", "meetings", "--directory", str(root), "--platform", "ics",
                        "--feed-url", FEED, "--body", BODY, "--from-ics", str(feed), "--tz", zone,
                        "--since", "2026-10-01"]
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err), \
                        mock.patch("campaign_tool.meetings.default_opener", side_effect=AssertionError("network")):
                    cli.main()
                self.assertEqual(err.getvalue(), "")
                report = json.loads(out.getvalue())
                self.assertEqual(report["skipped"]["bad_start"], 1)
                doc = json.loads((root / "kit/meetings.json").read_text())
                self.assertEqual([m["event_id"] for m in doc["meetings"]], ["healthy"])
                after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                self.assertEqual(set(after) - set(before), {Path("kit/meetings.json")})
                self.assertEqual({p: d for p, d in after.items() if p != Path("kit/meetings.json")}, before)


if __name__ == "__main__":
    unittest.main()
