"""RF1835-4: a truncated or misnested iCalendar feed is an unreadable source, never a partial meeting list."""
import contextlib
import io
import json
import re
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool import cli, meetings_ics  # noqa: E402
from campaign_tool.meetings import MeetingsError, load_sources, resolve_zone, scan_sources, source_key  # noqa: E402
from campaign_tool.meetings_ics import IcsClient, parse_events  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "meetings_ics"
DOC = REPO_ROOT / "docs" / "ICS-INPUT-SAFETY.md"
FEED = "https://example.gov/calendar/council.ics"
SINCE = date(2026, 10, 1)
ZONE = resolve_zone("America/Los_Angeles")
REFUSAL = "feed has malformed iCalendar component structure; nothing was written"
A = "BEGIN:VEVENT\r\nUID:synthetic-a\r\nDTSTART:20261112T180000Z\r\nSUMMARY:ALPR policy\r\nEND:VEVENT\r\n"
B = "BEGIN:VEVENT\r\nUID:synthetic-b\r\nDTSTART:20261119T180000Z\r\nSUMMARY:ALPR contract\r\nEND:VEVENT\r\n"


def cal(*parts):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(parts) + "END:VCALENDAR\r\n"


def client_for(text):
    data = text.encode("utf-8") if isinstance(text, str) else text
    return IcsClient(FEED, "Example City Council", opener=lambda url: data)


def read(text):
    return client_for(text).relevant(since=SINCE, zone=ZONE)


def ids(text):
    return [m["event_id"] for m in read(text)]


class Refusing(unittest.TestCase):
    def refuses(self, text, msg=None):
        with self.assertRaises(MeetingsError, msg=msg or text) as caught:
            read(text)
        self.assertEqual(str(caught.exception), REFUSAL, msg or text)


class Structure(Refusing):
    def test_truncated_calendar_and_late_event_refuse_whole_feed(self):
        valid = cal(A, B)
        self.assertEqual(ids(valid), ["synthetic-a", "synthetic-b"])
        lines = valid.split("\r\n")
        for i, line in enumerate(lines):
            if line.startswith("END:"):
                self.refuses("\r\n".join(lines[:i] + lines[i + 1:]), f"without line {i} {line}")
        self.refuses(valid[:valid.rindex("END:VEVENT")])  # the file ends inside the second event
        for tail in (A, "X-TAIL:1\r\n", "END:VCALENDAR\r\n", "BEGIN:VCALENDAR\r\n", cal(B)):
            self.refuses(valid + tail, f"tail {tail!r}")

    def test_mismatched_names_and_nested_events_cannot_create_meetings(self):
        alarm = A.replace("END:VEVENT", "BEGIN:VALARM\r\nACTION:DISPLAY\r\nEND:VTODO\r\nEND:VEVENT")
        cases = {
            "mismatched alarm end": cal(alarm),
            "unmatched end": cal(A, "END:VTIMEZONE\r\n"),
            "event in timezone": cal("BEGIN:VTIMEZONE\r\nTZID:America/Los_Angeles\r\n", A, "END:VTIMEZONE\r\n"),
            "event in event": cal(A.replace("END:VEVENT", B + "END:VEVENT")),
            "event in alarm": cal(A.replace("END:VEVENT", "BEGIN:VALARM\r\n" + B + "END:VALARM\r\nEND:VEVENT")),
            "event in unknown": cal("BEGIN:X-WRAP\r\n", A, "END:X-WRAP\r\n"),
            "nested calendar": cal(cal(A)),
            "parameters on begin": cal(A.replace("BEGIN:VEVENT", "BEGIN;X-NOTE=1:VEVENT")),
            "parameters on end": cal(A.replace("END:VEVENT", "END;X-NOTE=1:VEVENT")),
            "empty name": cal(A, "BEGIN:\r\nEND:\r\n"),
            "non-token name": cal(A, "BEGIN:X_ODD\r\nEND:X_ODD\r\n"),
            "spaced name": cal(A, "BEGIN:X ODD\r\nEND:X ODD\r\n"),
            "non-ascii name that upper-cases to a token": cal(A, "BEGIN:X-\u00df\r\nEND:X-SS\r\n"),
            "concatenated calendars": cal(A) + cal(B),
            "event outside calendar": A + cal(B),
            "property before calendar": "X-PRE:1\r\n" + cal(A),
            "end before begin": "END:VCALENDAR\r\n" + cal(A),
        }
        for name, text in cases.items():
            self.refuses(text, name)
        # the old permissive reader still finds synthetic-a in each: the refusal is the admission step's
        self.assertTrue(parse_events(cases["mismatched alarm end"]) and parse_events(cases["event in timezone"]))

    def test_balanced_ignored_components_do_not_leak_properties(self):
        leak = "SUMMARY:ALPR leak\r\nUID:leak@example.gov\r\nDTSTART:20261201T180000Z\r\nSTATUS:CANCELLED\r\n"
        timezone = ("BEGIN:VTIMEZONE\r\nTZID:America/Los_Angeles\r\nBEGIN:STANDARD\r\n" + leak + "END:STANDARD\r\n"
                    "BEGIN:DAYLIGHT\r\nDTSTART:20260308T020000\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\n")
        alarm = A.replace("END:VEVENT", "BEGIN:VALARM\r\n" + leak + "BEGIN:X-INNER\r\nEND:X-INNER\r\nEND:VALARM\r\nEND:VEVENT")
        unknown = "BEGIN:X-THING\r\n" + leak + "BEGIN:x-deeper\r\nEND:X-DEEPER\r\nEND:X-THING\r\nBEGIN:VTODO\r\n" + leak + "END:VTODO\r\n"
        self.assertEqual(read(cal(timezone, alarm, unknown)), read(cal(A)))
        self.assertEqual(len(read(cal(A))), 1)

    def test_folded_and_case_varied_boundaries_and_depth_limit(self):
        varied = ("\ufeffbegin:vcalendar\n\nVERSION:2.0\nBegin:VEv\n ent\nUID:synthetic-a\nDTSTART:20261112T180000Z\n"
                  "SUMMARY:ALPR policy\nEnd:VEvent \n\nEND:VCAL\n\tENDAR\n\n")
        self.assertEqual(read(varied), read(cal(A)))
        self.assertEqual(read(varied.replace("\n", "\r\n")), read(cal(A)))

        def nested(levels):
            return cal(A, "BEGIN:X-N\r\n" * levels + "END:X-N\r\n" * levels)
        self.assertEqual(ids(nested(63)), ["synthetic-a"])  # 64 frames including VCALENDAR
        self.refuses(nested(64))
        self.refuses(nested(5000), "deep nesting is refused as a fixed message, not a RecursionError")
        self.refuses(cal(A, "BEGIN:<script>\r\n"))  # the bounded message never carries feed text

    def test_empty_complete_calendar_differs_from_empty_or_truncated_file(self):
        client = client_for(cal())
        self.assertEqual(client.relevant(since=SINCE, zone=ZONE), [])
        self.assertEqual(client.skipped, dict.fromkeys(meetings_ics.SKIP_KEYS, 0))
        for text in ("", "\ufeff", "\r\n\r\n", "garbage\r\n", "BEGIN:VCALENDAR\r\n", "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n", A):
            self.refuses(text)
        # standalone low-level parsing stays permissive, exactly as before
        self.assertEqual(parse_events(""), [])
        self.assertEqual(parse_events(A), [{"UID": [({}, "synthetic-a")], "DTSTART": [({}, "20261112T180000Z")],
                                            "SUMMARY": [({}, "ALPR policy")]}])
        self.assertEqual(parse_events(cal(A, B)[:cal(A, B).rindex("END:VEVENT")]), parse_events(cal(A)))


class Preservation(unittest.TestCase):
    def unchecked(self, text):
        """The reader's result with the admission step switched off: what main returned for a well-formed feed."""
        client = client_for(text)
        with mock.patch.object(meetings_ics, "_check_components", lambda text: None):
            return client.relevant(since=SINCE, zone=ZONE), client.skipped

    def test_valid_feeds_revision_rules_and_raw_caps_remain_exact(self):
        basic = (FIXTURES / "basic.ics").read_text(encoding="utf-8").replace("LONGUID", "u" * 201 + "@example.gov")
        folded = (FIXTURES / "folded.ics").read_text(encoding="utf-8")
        older = A.replace("UID:", "SEQUENCE:1\r\nUID:").replace("ALPR policy", "ALPR draft")
        newer = A.replace("UID:", "SEQUENCE:2\r\nUID:")
        override = A.replace("UID:", "RECURRENCE-ID:20261112T180000Z\r\nUID:")
        master = A.replace("UID:", "RRULE:FREQ=WEEKLY\r\nUID:")
        allday = A.replace("DTSTART:20261112T180000Z", "DTSTART;VALUE=DATE:20261115")
        for text in (basic, folded, cal(older, newer), cal(newer, older), cal(master, override), cal(allday, B)):
            client = client_for(text)
            self.assertEqual((client.relevant(since=SINCE, zone=ZONE), client.skipped), self.unchecked(text))
        conflict = cal(newer, newer.replace("ALPR policy", "ALPR other"))
        with self.assertRaisesRegex(MeetingsError, "conflicting revisions"):
            read(conflict)
        event = "BEGIN:VEVENT\r\nUID:e{}\r\nSUMMARY:ALPR\r\nDTSTART:20261112T180000Z\r\nEND:VEVENT\r\n"
        self.assertEqual(len(read(cal(*[event.format(i) for i in range(2000)]))), 2000)
        for many in (cal(*[event.format(i) for i in range(2001)]), cal(*[event.format(0)] * 2001)):
            with self.assertRaisesRegex(MeetingsError, "more than 2000 VEVENTs"):
                read(many)
        limit = 2 * 1024 * 1024
        pad = cal(A, "X-PAD:", "\r\n")
        exact = pad.replace("X-PAD:", "X-PAD:" + "p" * (limit - len(pad)))
        self.assertEqual((len(exact.encode("utf-8")), ids(exact)), (limit, ["synthetic-a"]))
        with self.assertRaisesRegex(MeetingsError, "larger than 2 MiB"):
            read(exact + "\r\n")


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "c"
        shutil.copytree(EXAMPLE, self.root)
        self.kit = self.root / "kit" / "meetings.json"

    def run_cli(self, text, name):
        feed = self.tmp / name
        feed.write_bytes(text.encode("utf-8"))
        argv = ["campaign_tool", "meetings", "--directory", str(self.root), "--platform", "ics", "--since", "2026-10-01",
                "--feed-url", FEED, "--body", "Example City Council", "--from-ics", str(feed)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "argv", argv):
            try:
                code = cli.main() or 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def tree(self):
        return {str(p.relative_to(self.root)): (stat.S_IMODE(p.lstat().st_mode), p.read_bytes() if p.is_file() else None)
                for p in sorted(self.root.rglob("*"))}

    def assert_refused(self, result):
        code, out, err = result
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.splitlines(), [err.strip()])
        self.assertTrue(err.strip().endswith(REFUSAL), err)
        self.assertNotIn("Traceback", err)

    def test_single_source_refusal_preserves_existing_kit_bytes_modes_and_tree(self):
        truncated = cal(A, B)[:cal(A, B).rindex("END:VEVENT")]
        before = self.tree()
        self.assert_refused(self.run_cli(truncated, "truncated.ics"))
        self.assertEqual(self.tree(), before)  # no kit/meetings.json and no kit folder created
        self.assertFalse(self.kit.exists())
        self.assertEqual(self.run_cli(cal(A, B), "good.ics")[0], 0)
        self.kit.chmod(0o640)
        before = self.tree()
        for name, text in (("truncated.ics", truncated), ("nested.ics", cal("BEGIN:VTIMEZONE\r\n", A, "END:VTIMEZONE\r\n"))):
            self.assert_refused(self.run_cli(text, name))
            self.assertEqual(self.tree(), before, name)
        self.assertEqual([m["event_id"] for m in json.loads(self.kit.read_text(encoding="utf-8"))["meetings"]],
                         ["synthetic-a", "synthetic-b"])


class MultiSource(unittest.TestCase):
    def test_multi_source_bad_ics_keeps_prior_rows_stale_and_updates_good_source(self):
        bad_url, good_url = "https://bad.example.test/c.ics", "https://good.example.test/c.ics"
        sources = load_sources({"meeting_sources": [{"platform": "ics", "feed_url": bad_url, "label": "Bad Council"},
                                                    {"platform": "ics", "feed_url": good_url, "label": "Good Council"}]})
        mapping, fetched = {bad_url: cal(A), good_url: cal(B)}, []

        def opener_for(key):
            def opener(url):
                fetched.append(url)
                return mapping[url].encode("utf-8")
            return opener
        found, report, _ = scan_sources(sources, opener_for, SINCE, ZONE)
        self.assertEqual([r["status"] for r in report], ["ok", "ok"])
        previous = {"schema_version": 1, "client": "multi", "clients": [source_key(s) for s in sources], "meetings": found}
        mapping[bad_url] = cal(A, B)[:cal(A, B).rindex("END:VEVENT")]
        mapping[good_url] = cal(B.replace("20261119T180000Z", "20261120T180000Z"))
        found, report, changes = scan_sources(sources, opener_for, SINCE, ZONE, previous=previous)
        self.assertEqual(report[0]["status"], "failed")
        self.assertEqual(report[0]["kept_previous_meetings"], 1)
        self.assertIn(REFUSAL[:60], report[0]["error"])
        self.assertEqual((report[1]["status"], report[1]["meetings"]), ("ok", 1))
        rows = {m["event_id"]: m for m in found}
        self.assertEqual(set(rows), {"synthetic-a", "synthetic-b"})  # the bad feed's second event never appears
        self.assertTrue(rows["synthetic-a"]["stale"])
        self.assertNotIn("stale", rows["synthetic-b"])
        self.assertEqual(rows["synthetic-b"]["starts_at"], "2026-11-20T10:00:00-08:00")
        self.assertEqual(len(changes), 1)
        self.assertEqual(sorted(set(fetched)), [bad_url, good_url])


class Reference(Refusing):
    def test_structure_reference_names_preservation_and_limits(self):
        doc = DOC.read_text(encoding="utf-8")
        self.assertIn(REFUSAL, doc)
        self.assertIn("MEETINGS.md#icalendar-agenda-feed-import-cli", doc)
        self.assertIn("## iCalendar agenda feed import (CLI)", (REPO_ROOT / "docs" / "MEETINGS.md").read_text(encoding="utf-8"))
        for limit in ("64", "2 MiB", "2,000", "stale", "byte-for-byte"):
            self.assertIn(limit, doc)
        complete, truncated = re.findall(r"```ics\n(.*?)```", doc, re.S)
        self.assertEqual(ids(complete), ["example-1@example.gov"])
        self.refuses(truncated)


if __name__ == "__main__":
    unittest.main()
