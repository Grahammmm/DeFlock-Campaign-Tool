"""DF1422-09: meetings --platform ics (synthetic fixtures, example.gov URLs, no network)."""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool import cli  # noqa: E402
from campaign_tool.meetings import MeetingsError, relevant_events, resolve_zone  # noqa: E402
from campaign_tool.meetings_ics import IcsClient  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "meetings_ics"
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FEED = "https://example.gov/calendar/council.ics"
SINCE = date(2026, 10, 1)


def feed_text(name="basic.ics"):
    return (FIXTURES / name).read_bytes().decode("utf-8").replace("LONGUID", "u" * 201 + "@example.gov")


def read(text, zone="America/Los_Angeles", opener_urls=None):
    """(by uid, client) from feed text; the opener serves only the feed URL."""
    def opener(url):
        if opener_urls is not None:
            opener_urls.append(url)
        if url != FEED:
            raise AssertionError(f"fetched {url}")
        return text.encode("utf-8") if isinstance(text, str) else text
    client = IcsClient(FEED, "Example City Council", opener=opener)
    meetings = relevant_events(client, since=SINCE, zone=resolve_zone(zone))
    return {m["event_id"]: m for m in meetings}, client


def wrap(*vevents):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(f"BEGIN:VEVENT\r\n{v}\r\nEND:VEVENT\r\n" for v in vevents) + "END:VCALENDAR\r\n"


class ParseTests(unittest.TestCase):
    def test_tzid_event_in_its_zone_with_legistar_shape(self):
        urls = []
        found, _ = read(feed_text(), opener_urls=urls)
        m = found["tz-1@example.gov"]
        self.assertEqual(m["starts_at"], "2026-11-12T18:00:00-08:00")
        self.assertEqual((m["time_known"], m["status"], m["source"], m["body"], m["relevance"]),
                         (True, "scheduled", "ics", "Example City Council", "alpr_item"))
        self.assertRegex(m["id"], r"^ics-example-city-council-[0-9a-f]{12}$")
        self.assertEqual(m["agenda_url"], "https://example.gov/agenda/1")
        self.assertEqual(m["attachments"], [{"name": "staff-report.pdf", "url": "https://example.gov/files/a.pdf"}])
        self.assertIn("Automated License Plate Readers", m["agenda_item"])
        self.assertEqual(urls, [FEED])  # one GET of the feed, no attachment or agenda link

    def test_utc_converted_to_tz_and_dst_repeated_hour_pinned(self):
        found, _ = read(feed_text())
        self.assertEqual(found["utc-1@example.gov"]["starts_at"], "2026-11-12T18:00:00-08:00")
        # 2026-11-01 01:30 happens twice in Los Angeles; zoneinfo fold=0 is the first (daylight) one
        self.assertEqual(found["dst-1@example.gov"]["starts_at"], "2026-11-01T01:30:00-07:00")
        east, _ = read(feed_text(), zone="America/New_York")
        self.assertEqual(east["utc-1@example.gov"]["starts_at"], "2026-11-12T21:00:00-05:00")

    def test_unplaceable_and_recurring_events_skipped_and_counted_others_kept(self):
        found, client = read(feed_text())
        self.assertEqual(client.skipped, {"floating_time": 1, "unknown_zone": 1, "recurring": 2, "uid_too_long": 1,
                                         "no_uid": 0, "bad_start": 0})
        for uid in ("floating-1@example.gov", "unknown-1@example.gov", "rrule-1@example.gov", "rdate-1@example.gov"):
            self.assertNotIn(uid, found)
        self.assertFalse(any(len(uid) > 200 for uid in found))
        self.assertIn("tz-1@example.gov", found)
        self.assertEqual(client.report_extra([])["skipped"]["recurring"], 2)

    def test_all_day_event_is_date_only(self):
        m = read(feed_text())[0]["allday-1@example.gov"]
        self.assertEqual((m["starts_at"], m["time_known"]), ("2026-11-15T00:00:00-08:00", False))

    def test_cancelled_status(self):
        found, _ = read(feed_text())
        self.assertEqual(found["cancelled-1@example.gov"]["status"], "cancelled")
        self.assertEqual(found["tz-1@example.gov"]["status"], "scheduled")

    def test_folded_lines_lf_and_escapes_exact(self):
        for text in (feed_text("folded.ics"), feed_text("folded.ics").replace("\n", "\r\n")):
            m = read(text)[0]["fold-1@example.gov"]
            self.assertEqual(m["agenda_item"], "ALPR contract, phase 2; approve\\return")
            self.assertEqual(m["matched_terms"], ["alpr"])

    def test_filter_is_summary_and_description_only(self):
        found, _ = read(feed_text())
        self.assertNotIn("nomatch-1@example.gov", found)
        self.assertNotIn("attachonly-1@example.gov", found)  # keyword only in an ATTACH URL
        self.assertNotIn("past-1@example.gov", found)  # before since

    def test_http_links_are_not_recorded_and_nothing_else_fetched(self):
        urls = []
        m = read(feed_text(), opener_urls=urls)[0]["http-1@example.gov"]
        self.assertEqual((m["agenda_url"], m["attachments"], m["attachments_dropped"]), (None, [], 1))
        self.assertEqual(urls, [FEED])

    def test_first_https_attach_is_agenda_url_without_url_property(self):
        text = wrap("UID:a1\r\nSUMMARY:ALPR\r\nATTACH:http://example.gov/x.pdf\r\nATTACH:https://example.gov/y.pdf\r\n"
                    "DTSTART;TZID=America/Los_Angeles:20261112T180000")
        m = read(text)[0]["a1"]
        self.assertEqual(m["agenda_url"], "https://example.gov/y.pdf")

    def test_missing_uid_and_bad_start_counted(self):
        text = wrap("SUMMARY:ALPR\r\nDTSTART;TZID=America/Los_Angeles:20261112T180000",
                    "UID:b1\r\nSUMMARY:ALPR\r\nDTSTART:2026-11-12", "UID:b2\r\nSUMMARY:ALPR")
        found, client = read(text)
        self.assertEqual((found, client.skipped["no_uid"], client.skipped["bad_start"]), ({}, 1, 2))

    def test_more_than_2000_vevents_refused(self):
        event = "UID:e{}\r\nSUMMARY:ALPR\r\nDTSTART;TZID=America/Los_Angeles:20261112T180000"
        self.assertEqual(len(read(wrap(*[event.format(i) for i in range(2000)]))[0]), 2000)
        with self.assertRaises(MeetingsError):
            read(wrap(*[event.format(i) for i in range(2001)]))

    def test_oversize_feed_refused(self):
        with self.assertRaises(MeetingsError):
            read(b"x" * (2 * 1024 * 1024 + 1))

    def test_feed_url_and_body_validation(self):
        for url in ("http://example.gov/c.ics", "https://user:pw@example.gov/c.ics", "https://example.gov/" + "a" * 500, "ftp://x/y", ""):
            with self.assertRaises(MeetingsError, msg=url):
                IcsClient(url, "Council")
        for body in ("", "x" * 121, "Bad\nname"):
            with self.assertRaises(MeetingsError, msg=body):
                IcsClient(FEED, body)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "c"
        shutil.copytree(EXAMPLE, self.root)
        self.kit = self.root / "kit" / "meetings.json"

    def run_cli(self, *extra, platform=True):
        argv = ["campaign_tool", "meetings", "--directory", str(self.root), *(["--platform", "ics"] if platform else []),
                "--since", "2026-10-01", *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "argv", argv):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
            except ValueError as exc:
                code = str(exc)
        return code, out.getvalue(), err.getvalue()

    def feed(self, text=None, name="feed.ics"):
        path = self.tmp / name
        path.write_bytes((feed_text() if text is None else text).encode("utf-8"))
        return str(path)

    def good(self, path):
        return self.run_cli("--feed-url", FEED, "--body", "Example City Council", "--from-ics", path)

    def test_writes_kit_with_skip_counts_and_source(self):
        code, out, _ = self.good(self.feed())
        self.assertEqual(code, 0)
        doc = json.loads(self.kit.read_text(encoding="utf-8"))
        self.assertEqual((doc["source"], doc["client"], doc["schema_version"]), ("ics", "Example City Council", 1))
        report = json.loads(out)
        self.assertEqual((report["platform"], report["skipped"]["recurring"], report["skipped"]["floating_time"]), ("ics", 2, 1))
        self.assertEqual(report["cancelled_meetings"], 1)

    def test_rerun_reports_cancelled_and_moved(self):
        before = feed_text().replace("STATUS:CANCELLED\r\n", "")
        self.assertEqual(self.good(self.feed(before))[0], 0)
        after = feed_text().replace("20261112T180000", "20261112T190000", 1)
        self.assertEqual(self.good(self.feed(after, "after.ics"))[0], 0)
        changes = json.loads(self.kit.read_text(encoding="utf-8"))["changes"]
        ids = {m["event_id"]: m["id"] for m in json.loads(self.kit.read_text(encoding="utf-8"))["meetings"]}
        self.assertEqual(changes["cancelled"], [ids["cancelled-1@example.gov"]])
        self.assertEqual([(r["id"], r["to"]) for r in changes["rescheduled"]],
                         [(ids["tz-1@example.gov"], "2026-11-12T19:00:00-08:00")])

    def test_invalid_flag_combinations_refused_nothing_written(self):
        path = self.feed()
        cases = [("--body", "B", "--from-ics", path),  # no --feed-url
                 ("--feed-url", FEED, "--from-ics", path),  # no --body
                 ("--feed-url", FEED, "--body", "B", "--client", "x", "--from-ics", path)]
        for extra in cases:
            code, _, _ = self.run_cli(*extra)
            self.assertEqual(code, 2, extra)
        for extra in (("--feed-url", "http://example.gov/c.ics", "--body", "B", "--from-ics", path),
                      ("--feed-url", FEED, "--body", "B"),  # neither --online nor --from-ics
                      ("--feed-url", FEED, "--body", "B", "--online", "--from-ics", path),
                      ("--feed-url", FEED, "--body", "B", "--from-ics", path, "--from-xml", path)):
            code, _, _ = self.run_cli(*extra)
            self.assertEqual(code, 1, extra)
        self.assertFalse(self.kit.exists())

    def test_other_platforms_refuse_ics_flags_and_still_need_client(self):
        code, _, _ = self.run_cli("--platform", "legistar", "--feed-url", FEED, "--client", "x", platform=False)
        self.assertEqual(code, 1)
        self.assertEqual(self.run_cli("--online", platform=False)[0], 2)

    def test_oversize_and_too_many_events_write_nothing(self):
        big = self.tmp / "big.ics"
        big.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        self.assertEqual(self.good(str(big))[0], 1)
        many = wrap(*["UID:e%d\r\nSUMMARY:ALPR\r\nDTSTART;TZID=America/Los_Angeles:20261112T180000" % i for i in range(2001)])
        self.assertEqual(self.good(self.feed(many, "many.ics"))[0], 1)
        self.assertFalse(self.kit.exists())

    def test_online_makes_one_get_of_the_feed_only(self):
        urls = []

        def opener(url):
            urls.append(url)
            return feed_text().encode("utf-8")
        with mock.patch("campaign_tool.meetings.default_opener", opener):
            code, _, _ = self.run_cli("--feed-url", FEED, "--body", "Example City Council", "--online")
        self.assertEqual((code, urls), (0, [FEED]))


if __name__ == "__main__":
    unittest.main()
