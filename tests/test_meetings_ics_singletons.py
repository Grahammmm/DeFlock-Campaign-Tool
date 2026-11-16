"""RF0520-1: repeated UID, DTSTART or STATUS inside one imported VEVENT refuses the whole feed."""
import contextlib
import io
import json
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool import cli  # noqa: E402
from campaign_tool.meetings import MeetingsError, load_sources, resolve_zone, scan_sources, source_key  # noqa: E402
from campaign_tool.meetings_ics import IcsClient, parse_events  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
DOC = REPO_ROOT / "docs" / "ICS-INPUT-SAFETY.md"
FEED = "https://example.gov/calendar/council.ics"
SINCE = date(2026, 10, 1)
ZONE = resolve_zone("America/Los_Angeles")
REFUSAL = "feed has repeated UID, DTSTART or STATUS in one VEVENT; nothing was written"
CRLF = "\r\n"


def event(*lines):
    return CRLF.join(("BEGIN:VEVENT",) + lines + ("END:VEVENT",)) + CRLF


def cal(*parts):
    return "BEGIN:VCALENDAR" + CRLF + "VERSION:2.0" + CRLF + "".join(parts) + "END:VCALENDAR" + CRLF


GOOD = event("UID:synthetic-a", "DTSTART:20261112T180000Z", "SUMMARY:ALPR policy")
SIBLING = event("UID:synthetic-b", "DTSTART:20261119T180000Z", "SUMMARY:ALPR contract")
CANCELLED = event("UID:synthetic-a", "DTSTART:20261112T180000Z", "SUMMARY:ALPR policy", "STATUS:CANCELLED")


def client_for(text):
    return IcsClient(FEED, "Example City Council", opener=lambda url: text.encode("utf-8"))


def read(text):
    return client_for(text).relevant(since=SINCE, zone=ZONE)


class Refusal(unittest.TestCase):
    def refuses(self, text):
        with self.assertRaises(MeetingsError) as caught:
            read(text)
        self.assertEqual(str(caught.exception), REFUSAL)

    def test_duplicate_safety_properties_refuse_whole_feed(self):
        self.assertEqual([m["event_id"] for m in read(cal(GOOD, SIBLING))], ["synthetic-a", "synthetic-b"])
        bad = {
            "uid-conflict": ("UID:synthetic-a", "UID:synthetic-z"),
            "uid-identical": ("UID:synthetic-a", "UID:synthetic-a"),
            "start-conflict": ("DTSTART:20261112T180000Z", "DTSTART:20261113T180000Z"),
            "start-param": ("DTSTART:20261112T180000Z", "DTSTART;TZID=America/Los_Angeles:20261113T100000"),
            "status-conflict": ("STATUS:CONFIRMED", "STATUS:CANCELLED"),
            "status-reversed": ("STATUS:CANCELLED", "STATUS:CONFIRMED"),
            "status-identical": ("STATUS:CANCELLED", "STATUS:CANCELLED"),
            "status-param-lower": ("status;x-a=1:CONFIRMED", "Status:CANCELLED"),
        }
        for name, (first, second) in bad.items():
            base = ["UID:synthetic-a", "DTSTART:20261112T180000Z", "SUMMARY:ALPR policy"]
            key = first.split(":")[0].split(";")[0].upper()
            lines = [ln for ln in base if not ln.upper().startswith(key + ":")] + [first, second]
            self.refuses(cal(SIBLING, event(*lines), SIBLING.replace("synthetic-b", "synthetic-c")))
            self.assertTrue(name)
        folded = event("UID:synthetic-a", "DTSTART:20261112T180000Z", "SUMMARY:ALPR", "STAT", " US:CONFIRMED", "STATUS:CANCELLED")
        self.refuses(cal(folded))

    def test_preflight_precedes_revisions_and_semantic_skips(self):
        older = event("UID:synthetic-a", "SEQUENCE:1", "DTSTART:20261112T180000Z", "SUMMARY:ALPR", "STATUS:A", "STATUS:B")
        newer = event("UID:synthetic-a", "SEQUENCE:2", "DTSTART:20261112T180000Z", "SUMMARY:ALPR")
        self.refuses(cal(older, newer))
        self.refuses(cal(newer, older))
        self.refuses(cal(event("UID:r", "RRULE:FREQ=WEEKLY", "DTSTART:20261112T180000Z", "DTSTART:20261113T180000Z")))
        self.refuses(cal(event("UID:r", "RECURRENCE-ID:20261112T180000Z", "STATUS:X", "STATUS:X", "DTSTART:20261112T180000Z")))
        self.refuses(cal(event("UID:old", "DTSTART:20200101T000000Z", "DTSTART:20200102T000000Z", "SUMMARY:ALPR")))
        self.refuses(cal(event("UID:n", "DTSTART:20261112T180000Z", "STATUS:X", "STATUS:Y", "SUMMARY:Parks")))
        self.refuses(cal(event("UID:", "UID:b", "DTSTART:20261112T180000Z", "SUMMARY:ALPR")))
        self.refuses(cal(event("UID:" + "u" * 300, "UID:b", "DTSTART:20261112T180000Z", "SUMMARY:ALPR")))
        self.refuses(cal(event("UID:a", "DTSTART:garbage", "DTSTART:20261112T180000Z", "SUMMARY:ALPR")))


class Compatible(unittest.TestCase):
    def test_direct_properties_only_and_repeatable_fields_stay_compatible(self):
        alarm = event("UID:synthetic-a", "DTSTART:20261112T180000Z", "SUMMARY:ALPR policy",
                      "BEGIN:VALARM", "UID:alarm", "UID:alarm2", "STATUS:A", "STATUS:B", "DTSTART:1", "DTSTART:2", "END:VALARM")
        tz = ("BEGIN:VTIMEZONE" + CRLF + "DTSTART:19700101T000000" + CRLF + "DTSTART:19710101T000000" + CRLF
              + "END:VTIMEZONE" + CRLF)
        rows = read(cal(tz, alarm))
        self.assertEqual([m["event_id"] for m in rows], ["synthetic-a"])
        self.assertEqual(rows[0]["status"], "scheduled")
        multi = event("UID:synthetic-a", "DTSTART:20261112T180000Z", "SUMMARY:ALPR one", "SUMMARY:ALPR two",
                      "DESCRIPTION:x", "DESCRIPTION:y", "ATTACH:https://example.gov/a.pdf", "ATTACH:https://example.gov/b.pdf")
        self.assertEqual(len(read(cal(multi))), 1)
        old = event("UID:synthetic-a", "SEQUENCE:1", "DTSTART:20261112T180000Z", "SUMMARY:ALPR draft")
        new = event("UID:synthetic-a", "SEQUENCE:2", "DTSTART:20261113T180000Z", "SUMMARY:ALPR final", "STATUS:CANCELLED")
        for text in (cal(old, new), cal(new, old)):
            rows = read(text)
            self.assertEqual([(m["status"], m["starts_at"][:10]) for m in rows], [("cancelled", "2026-11-13")])

    def test_zero_or_one_occurrence_and_low_level_parser_compatibility(self):
        self.assertEqual(read(cal()), [])
        client = client_for(cal(event("DTSTART:20261112T180000Z", "SUMMARY:ALPR"), event("UID:x", "SUMMARY:ALPR"), GOOD))
        rows = client.relevant(since=SINCE, zone=ZONE)
        self.assertEqual([(m["event_id"], m["status"]) for m in rows], [("synthetic-a", "scheduled")])
        self.assertEqual(client.skipped["no_uid"], 1)
        self.assertEqual(len(read(cal(CANCELLED))), 1)
        self.assertEqual(read(cal(CANCELLED))[0]["status"], "cancelled")
        dup = parse_events(cal(event("UID:a", "STATUS:X", "STATUS:Y")))
        self.assertEqual(len(dup[0]["STATUS"]), 2)


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

    def test_cli_duplicate_refusal_preserves_private_tree(self):
        conflicting = cal(event("UID:synthetic-a", "DTSTART:20261112T180000Z", "SUMMARY:ALPR", "STATUS:CONFIRMED", "STATUS:CANCELLED"))
        dup_start = cal(event("UID:synthetic-a", "DTSTART:20261112T180000Z", "DTSTART:20261112T180000Z", "SUMMARY:ALPR"))
        dup_uid = cal(event("UID:synthetic-a", "UID:synthetic-a", "DTSTART:20261112T180000Z", "SUMMARY:ALPR"))
        before = self.tree()
        self.assert_refused(self.run_cli(conflicting, "bad0.ics"))
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.kit.exists())
        self.assertEqual(self.run_cli(cal(CANCELLED), "good.ics")[0], 0)
        self.kit.chmod(0o640)
        before = self.tree()
        for name, text in (("a.ics", conflicting), ("b.ics", dup_start), ("c.ics", dup_uid)):
            self.assert_refused(self.run_cli(text, name))
            self.assertEqual(self.tree(), before, name)
        stored = json.loads(self.kit.read_text(encoding="utf-8"))["meetings"]
        self.assertEqual([m["status"] for m in stored], ["cancelled"])
        self.assertEqual(self.run_cli(cal(GOOD), "healthy.ics")[0], 0)


class MultiSource(unittest.TestCase):
    def test_multi_source_duplicate_is_failed_not_empty_or_success(self):
        bad_url, good_url = "https://bad.example.test/c.ics", "https://good.example.test/c.ics"
        sources = load_sources({"meeting_sources": [{"platform": "ics", "feed_url": bad_url, "label": "Bad Council"},
                                                    {"platform": "ics", "feed_url": good_url, "label": "Good Council"}]})
        mapping, fetched = {bad_url: cal(CANCELLED), good_url: cal(SIBLING)}, []

        def opener_for(key):
            def opener(url):
                fetched.append(url)
                return mapping[url].encode("utf-8")
            return opener
        found, report, _ = scan_sources(sources, opener_for, SINCE, ZONE)
        previous = {"schema_version": 1, "client": "multi", "clients": [source_key(s) for s in sources], "meetings": found}
        mapping[bad_url] = cal(event("UID:synthetic-a", "DTSTART:20261112T180000Z", "SUMMARY:ALPR", "STATUS:CONFIRMED", "STATUS:CANCELLED"))
        mapping[good_url] = cal(SIBLING.replace("20261119", "20261120"))
        found, report, changes = scan_sources(sources, opener_for, SINCE, ZONE, previous=previous)
        self.assertEqual(report[0]["status"], "failed")
        self.assertEqual(report[0]["kept_previous_meetings"], 1)
        self.assertIn(REFUSAL, report[0]["error"])
        self.assertEqual((report[1]["status"], report[1]["meetings"]), ("ok", 1))
        rows = {m["event_id"]: m for m in found}
        self.assertEqual(rows["synthetic-a"]["status"], "cancelled")
        self.assertTrue(rows["synthetic-a"]["stale"])
        self.assertEqual(rows["synthetic-b"]["starts_at"], "2026-11-20T10:00:00-08:00")
        self.assertEqual(len(changes), 1)
        self.assertEqual(sorted(set(fetched)), [bad_url, good_url])
        found, report, _ = scan_sources(sources, opener_for, SINCE, ZONE)
        self.assertEqual(report[0]["status"], "failed")
        self.assertEqual(report[0].get("kept_previous_meetings", 0), 0)
        self.assertEqual([m["event_id"] for m in found], ["synthetic-b"])


class Reference(unittest.TestCase):
    def test_reference_describes_both_admission_boundaries(self):
        doc = DOC.read_text(encoding="utf-8")
        for needle in (REFUSAL, "`UID`", "`DTSTART`", "`STATUS`", "identical", "VALARM", "stale", "byte-for-byte",
                       "feed has malformed iCalendar component structure; nothing was written"):
            self.assertIn(needle, doc)


if __name__ == "__main__":
    unittest.main()
