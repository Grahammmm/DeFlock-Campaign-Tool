"""kit-calendar UTC timestamps: four-digit years at every width and a fixed refusal when UTC is unrepresentable.

Offline and synthetic. The expected strings are written out by hand, never produced by the formatter under test.
"""
import json
import re
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_calendar  # noqa: E402
from tests import test_kit_calendar as base  # noqa: E402

REFUSAL = "kit-calendar timestamp is outside the supported UTC calendar; nothing was written"
DATE_TIME = re.compile(r"(DTSTART|DTSTAMP):(\d+)T(\d+)Z")


def at(year, month, day, hour, minute, second, offset_hours=0, microsecond=0):
    return datetime(year, month, day, hour, minute, second, microsecond,
                    tzinfo=timezone(timedelta(hours=offset_hours)))


class KitCalendarUtcTests(unittest.TestCase):  # reuses only the fixture helpers, never the inherited suite
    setUp = base.KitCalendarTests.setUp
    cli = base.KitCalendarTests.cli
    stage = base.KitCalendarTests.stage
    events = base.KitCalendarTests.events

    def put(self, mid, starts_at, deadline, verified_at, status="scheduled"):
        document = base.meeting(mid, deadline=deadline, status=status)
        document["starts_at"] = starts_at
        document["participation"]["verified_at"] = verified_at
        self.stage(document)

    def low_year(self, mid="old-1"):  # year 0999, UTC 17:00, before the 18:00 start
        self.put(mid, "0999-11-10T18:00:00+00:00", "0999-11-10T17:00:00+00:00", "0999-11-01")

    def underflow(self, mid="early-1"):  # local 0001-01-01 00:30 at +01:00 is 0000-12-31 in UTC
        self.put(mid, "0001-01-01T02:00:00+01:00", "0001-01-01T00:30:00+01:00", "0001-01-01")

    def snapshot(self):
        return {str(p.relative_to(self.tmp)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in sorted(self.tmp.rglob("*")) if p.is_file()}

    def command(self, out, as_of="0001-01-01"):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit-calendar", "--directory",
                               str(self.root), "--out", str(out), "--as-of", as_of],
                              capture_output=True, text=True, timeout=120, cwd=REPO_ROOT)

    def test_low_year_deadline_is_admitted_and_exports_four_digit_utc_year(self):
        from campaign_tool.content import load_content
        self.low_year()
        self.assertEqual([m["participation"]["written_comment"]["deadline"] for m in load_content(self.root)["meetings"]],
                         ["0999-11-10T17:00:00+00:00"])  # the real validators admit it
        out = self.tmp / "low.ics"
        report = kit_calendar.calendar(self.root, out, "0001-01-01", now=base.NOW)
        self.assertEqual(report, {"written": str(out), "events": 1})
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o600")
        data = out.read_bytes()
        self.assertIn(b"DTSTART:09991110T170000Z\r\n", data)
        self.assertIn(b"DTSTAMP:20261101T120000Z\r\n", data)
        self.assertIn(b"UID:meeting-comment-old-1@campaign.invalid\r\n", data)
        self.assertIn(b"SUMMARY:Written comment deadline \xe2\x80\x94 Example City Council\r\n", data)
        self.assertIn(b"URL:https://example.invalid/agenda/old-1\r\n", data)
        for line in base.unfold(data):
            if DATE_TIME.fullmatch(line):
                self.assertRegex(line, r"(DTSTART|DTSTAMP):\d{8}T\d{6}Z")
        self.assertEqual(len([ln for ln in base.unfold(data) if DATE_TIME.fullmatch(ln)]), 2)

    def test_utc_formatter_and_renderer_pad_each_supported_year_width(self):
        cases = [
            (at(1, 2, 3, 4, 5, 6), "00010203T040506Z"),
            (at(9, 12, 11, 10, 9, 8), "00091211T100908Z"),
            (at(99, 7, 8, 9, 10, 11), "00990708T091011Z"),
            (at(999, 11, 10, 17, 0, 0), "09991110T170000Z"),
            (at(1000, 1, 2, 3, 4, 5), "10000102T030405Z"),
            (at(9999, 12, 31, 23, 59, 59), "99991231T235959Z"),
            (at(999, 12, 31, 23, 59, 58, 5), "09991231T185958Z"),  # positive offset moves earlier
            (at(998, 12, 31, 22, 0, 0, -3), "09990101T010000Z"),  # negative offset crosses into the next year
            (at(1000, 1, 1, 0, 30, 0, 2), "09991231T223000Z"),  # and across the four-digit boundary
            (at(1, 2, 3, 4, 5, 6, 3, 987654), "00010203T010506Z"),  # fractions are truncated
        ]
        for moment, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(kit_calendar._utc(moment), expected)
                event = {"uid": "meeting-comment-x@campaign.invalid", "day": "2026-11-11", "moment": moment,
                         "summary": "S"}
                text = kit_calendar.render([event], moment)
                self.assertIn(f"\r\nDTSTAMP:{expected}\r\n", text)
                self.assertIn(f"\r\nDTSTART:{expected}\r\n", text)

    def test_admitted_deadline_utc_underflow_refuses_before_output(self):
        self.underflow()
        self.stage(base.meeting("council-1"))  # a healthy neighbour: the whole export is refused, not part of it
        out = self.tmp / "early.ics"
        before = self.snapshot()
        with self.assertRaises(ValueError) as caught:
            kit_calendar.calendar(self.root, out, "0001-01-01", now=base.NOW)
        self.assertEqual(str(caught.exception), REFUSAL)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        done = self.command(out)
        self.assertEqual((done.returncode, done.stdout), (3, ""), done.stderr)
        self.assertEqual(done.stderr.splitlines()[0], "Stopped: " + REFUSAL)
        self.assertNotIn("Traceback", done.stderr)
        for value in ("0001", "early-1", "00:30", "OverflowError", "out of range"):
            self.assertNotIn(value, done.stderr)
        self.assertFalse(out.exists())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.tmp.glob(".kit-calendar.*")), [])

    def test_renderer_refuses_both_utc_endpoints_and_generation_time_overflow(self):
        def event(moment):
            return {"uid": "meeting-comment-x@campaign.invalid", "day": "2026-11-11", "moment": moment, "summary": "S"}
        low, high = at(1, 1, 1, 0, 30, 0, 1), at(9999, 12, 31, 23, 30, 0, -1)
        for moment in (low, high):
            with self.subTest(moment=moment):
                with self.assertRaises(ValueError) as caught:
                    kit_calendar._utc(moment)
                self.assertEqual(str(caught.exception), REFUSAL)
                with self.assertRaises(ValueError) as start:
                    kit_calendar.render([event(moment)], base.NOW)  # DTSTART
                self.assertEqual(str(start.exception), REFUSAL)
                with self.assertRaises(ValueError) as stamp:
                    kit_calendar.render([event(base.NOW)], moment)  # DTSTAMP (injected generation time)
                self.assertEqual(str(stamp.exception), REFUSAL)
        for moment, expected in ((at(1, 1, 1, 0, 0, 0), "00010101T000000Z"),
                                 (at(9999, 12, 31, 23, 59, 59), "99991231T235959Z"),
                                 (at(1, 1, 1, 1, 0, 0, 1), "00010101T000000Z"),
                                 (at(9999, 12, 31, 22, 59, 59, -1), "99991231T235959Z")):
            with self.subTest(control=expected):
                text = kit_calendar.render([event(moment)], moment)
                self.assertIn(f"DTSTART:{expected}\r\n", text)
                self.assertIn(f"DTSTAMP:{expected}\r\n", text)

    def test_cancelled_filtered_and_absent_deadlines_keep_existing_eligibility(self):
        self.put("cancelled-1", "0001-01-01T02:00:00+01:00", "0001-01-01T00:30:00+01:00", "0001-01-01", "cancelled")
        self.stage(base.meeting("bare-1", participation=False))
        self.stage(base.meeting("no-written-1", deadline=None))
        self.underflow("filtered-1")
        out = self.tmp / "skipped.ics"
        report = kit_calendar.calendar(self.root, out, "0001-01-02", now=base.NOW)  # past by its local date
        self.assertEqual(report, {"written": str(out), "events": 0})  # nothing converts, nothing is refused
        self.assertEqual(self.events(out.read_bytes()), [])
        # the as-of filter still uses the deadline's own local date, not its UTC date
        self.stage(base.meeting("council-1"))  # local 2026-11-10 17:00 at -08:00, UTC day 2026-11-11
        same = kit_calendar.calendar(self.root, self.tmp / "same.ics", "2026-11-10", now=base.NOW)
        later = kit_calendar.calendar(self.root, self.tmp / "later.ics", "2026-11-11", now=base.NOW)
        self.assertEqual((same["events"], later["events"]), (1, 0))
        self.low_year()
        kept = kit_calendar.calendar(self.root, self.tmp / "kept.ics", "0999-11-10", now=base.NOW)
        self.assertEqual(kept["events"], 2)

    def test_ordinary_calendar_bytes_projection_and_order_are_unchanged(self):
        events = [
            {"uid": "z-all-day@campaign.invalid", "day": "2026-11-10", "summary": "Request, due; today\nnext"},
            {"uid": "meeting-comment-b@campaign.invalid", "day": "2026-11-11", "summary": "Later",
             "moment": at(2026, 11, 10, 17, 0, 0, -8, 987654), "url": "https://example.invalid/agenda/b"},
            {"uid": "meeting-comment-a@campaign.invalid", "day": "2026-11-11", "summary": "Plazo — año",
             "moment": at(2026, 11, 10, 9, 30, 5, -8)},
        ]
        expected = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//DeFlock Campaign Tool//kit calendar//EN\r\n"
            "CALSCALE:GREGORIAN\r\n"
            "BEGIN:VEVENT\r\nUID:z-all-day@campaign.invalid\r\nDTSTAMP:20261101T120000Z\r\n"
            "DTSTART;VALUE=DATE:20261110\r\nSUMMARY:Request\\, due\\; today\\nnext\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:meeting-comment-a@campaign.invalid\r\nDTSTAMP:20261101T120000Z\r\n"
            "DTSTART:20261110T173005Z\r\nDURATION:PT0M\r\nSUMMARY:Plazo \u2014 a\u00f1o\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:meeting-comment-b@campaign.invalid\r\nDTSTAMP:20261101T120000Z\r\n"
            "DTSTART:20261111T010000Z\r\nDURATION:PT0M\r\nSUMMARY:Later\r\n"
            "URL:https://example.invalid/agenda/b\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n")
        self.assertEqual(kit_calendar.render(events, base.NOW), expected)

    def test_refusal_is_value_free_retryable_and_read_only(self):
        self.underflow()
        folder = self.tmp / "export"
        folder.mkdir()
        (folder / "other.txt").write_bytes(b"sentinel")
        out = folder / "dates.ics"
        before = self.snapshot()
        done = self.command(out)
        self.assertEqual((done.returncode, done.stdout), (3, ""), done.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["other.txt"])
        # an existing target keeps its own refusal before any event is built
        existing = folder / "mine.ics"
        existing.write_bytes(b"mine")
        refused = self.command(existing)
        self.assertEqual(refused.returncode, 3)
        self.assertIn("exists; nothing was overwritten", refused.stderr)
        self.assertNotIn(REFUSAL, refused.stderr)
        self.assertEqual(existing.read_bytes(), b"mine")
        # correcting the synthetic deadline into representable UTC lets the same output succeed
        self.put("early-1", "0001-01-01T08:00:00+01:00", "0001-01-01T05:00:00+01:00", "0001-01-01")
        retry = self.command(out)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout), {"written": str(out), "events": 1})
        self.assertIn(b"DTSTART:00010101T040000Z\r\n", out.read_bytes())
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["dates.ics", "mine.ics", "other.txt"])


if __name__ == "__main__":
    unittest.main()
