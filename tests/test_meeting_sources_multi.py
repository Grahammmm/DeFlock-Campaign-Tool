"""MTG-MULTI-1: synthetic mixed-platform scans; no network or public output."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from campaign_tool import cli
from campaign_tool.meetings import client_for, load_sources, resolve_zone, scan_sources, source_key
from tests.test_meetings_sources import recorded
from tests.test_meetings_granicus import feed, item

SOURCES = [
    {"platform": "legistar", "client": "alpha", "label": "County Board"},
    {"platform": "civicclerk", "client": "alpha", "label": "Alpha Council"},
    {"platform": "granicus", "client": "gamma.granicus.com", "view_id": 7},
    {"platform": "agendacenter", "client": "delta.example.test"},
    {"platform": "ics", "feed_url": "https://epsilon.example.test/calendar.ics", "label": "Epsilon Council"},
]
SINCE = date(2026, 12, 1)
ZONE = resolve_zone("America/Los_Angeles")


def calendar(status="CONFIRMED", day="20261208"):
    event = (f"BEGIN:VEVENT\nUID:synthetic-1\nDTSTART:{day}T180000Z\n"
             f"SUMMARY:ALPR contract\nSTATUS:{status}\nEND:VEVENT\n")
    return "BEGIN:VCALENDAR\n" + event + event + "END:VCALENDAR\n"


def recordings():
    result = recorded("alpha", [(1, "2026-12-08")])
    result["/v1/Events"] = [{
        "id": 1, "startDateTime": "2026-12-08T18:00:00Z", "categoryName": "Alpha Council", "agendaId": 2,
    }]
    result["/Meetings/2"] = {"items": [{"agendaObjectItemName": "ALPR contract"}]}
    result["/ViewPublisherRSS.php"] = feed(item()).decode()
    result["/RSSFeed.aspx"] = (
        "<rss><channel><item><title>Council</title><category>Delta Council</category>"
        "<link>https://delta.example.test/AgendaCenter/ViewFile/Agenda/_12082026-1</link>"
        "<description>ALPR contract</description></item></channel></rss>"
    )
    result[SOURCES[4]["feed_url"]] = calendar()
    return result


class MixedSources(unittest.TestCase):
    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.object(sys, "argv", ["campaign_tool", *args]):
            try:
                code = cli.main() or 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_one_file_all_platforms_failure_stale_and_recovery(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-mtg-multi-") as tmp:
            root = Path(tmp) / "campaign"
            code, _, err = self.invoke("init", "--directory", str(root), "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
            self.assertEqual(code, 0, err)
            cfg_path = root / "campaign.json"
            cfg = json.loads(cfg_path.read_text())
            cfg["meeting_sources"] = SOURCES
            cfg_path.write_text(json.dumps(cfg))
            saved = Path(tmp) / "recorded.json"
            mapping = recordings()
            saved.write_text(json.dumps(mapping))
            args = ("meetings", "--directory", str(root), "--from-json", str(saved), "--since", SINCE.isoformat())
            with patch("campaign_tool.meetings.default_opener", side_effect=AssertionError("no network")):
                code, out, err = self.invoke(*args)
                self.assertEqual(code, 0, err)
                target = root / "kit/meetings.json"
                doc = json.loads(target.read_text())
                self.assertEqual(doc["client"], "multi")
                self.assertEqual([s["status"] for s in doc["sources"]], ["ok"] * 5)
                self.assertEqual(len(doc["meetings"]), 5)  # duplicate ICS UID stays one meeting
                self.assertEqual({m["source"] for m in doc["meetings"]}, {s["platform"] for s in SOURCES})
                self.assertTrue(all(m["id"].startswith(m["source"] + "-") for m in doc["meetings"]))
                self.assertEqual(set(doc["changes"]), {source_key(s) for s in SOURCES})
                self.assertEqual(next(m["body"] for m in doc["meetings"] if m["source"] == "ics"), "Epsilon Council")
                for failed in ("/ViewPublisherRSS.php", "/RSSFeed.aspx", SOURCES[4]["feed_url"],
                               "/v1/Events", "/v1/alpha/events"):
                    broken = dict(mapping)
                    del broken[failed]
                    saved.write_text(json.dumps(broken))
                    code, out, err = self.invoke(*args)
                    self.assertEqual(code, 3, err)
                    doc = json.loads(target.read_text())
                    self.assertEqual(len(doc["meetings"]), 5)
                    self.assertEqual(sum(s["status"] == "failed" for s in doc["sources"]), 1)
                    self.assertEqual(sum(bool(m.get("stale")) for m in doc["meetings"]), 1)
                    saved.write_text(json.dumps(mapping))
                    self.assertEqual(self.invoke(*args)[0], 0)
                    self.assertFalse(any(m.get("stale") for m in json.loads(target.read_text())["meetings"]))
                before = target.read_bytes()
                for flag in ("--all-events", "--matters"):
                    code, _, err = self.invoke(*args, flag)
                    self.assertEqual(code, 1)
                    self.assertIn(flag, err)
                    self.assertEqual(target.read_bytes(), before)
                self.assertFalse((root / "public").exists())

    def test_cancelled_expired_duplicate_and_first_failure(self):
        from campaign_tool.meetings import fixture_opener
        sources = load_sources({"meeting_sources": SOURCES})
        mapping = recordings()
        mapping[SOURCES[4]["feed_url"]] = calendar("CANCELLED")
        found, report, _ = scan_sources(sources, lambda key: fixture_opener(mapping), SINCE, ZONE)
        ics = [m for m in found if m["source"] == "ics"]
        self.assertEqual(len(ics), 1)
        self.assertEqual(ics[0]["status"], "cancelled")
        mapping[SOURCES[4]["feed_url"]] = calendar(day="20261101")
        found, report, _ = scan_sources(sources, lambda key: fixture_opener(mapping), SINCE, ZONE)
        self.assertFalse(any(m["source"] == "ics" for m in found))
        self.assertEqual(report[-1]["status"], "ok")
        del mapping[SOURCES[4]["feed_url"]]
        found, report, changes = scan_sources(sources, lambda key: fixture_opener(mapping), SINCE, ZONE)
        self.assertEqual(len(found), 4)
        self.assertEqual(report[-1]["kept_previous_meetings"], 0)
        self.assertNotIn(source_key(sources[-1]), changes)

    def test_entry_validation_names_index_before_io(self):
        cases = [None, [], {}, {"platform": "unknown"}, {"platform": []}]
        for entry in SOURCES:
            cases.extend({k: v for k, v in entry.items() if k != required}
                         for required in entry if required != "label" or entry["platform"] == "ics")
            cases.extend([dict(entry, extra="x"), dict(entry, label="<bad>"), dict(entry, label=" ")])
            if entry["platform"] != "ics":
                cases.append(dict(entry, feed_url="https://example.test/feed"))
        cases.extend([
            dict(SOURCES[0], client="Bad_slug"), dict(SOURCES[1], client="bad.example.test"),
            dict(SOURCES[2], client="gamma.example.test"), dict(SOURCES[2], view_id=True),
            dict(SOURCES[2], view_id=0), dict(SOURCES[2], view_id=10000), dict(SOURCES[2], view_id="7"),
            dict(SOURCES[3], client="https://delta.example.test"), dict(SOURCES[3], client="127.0.0.1"),
            dict(SOURCES[4], feed_url="http://epsilon.example.test/feed"),
            dict(SOURCES[4], feed_url="https://" + "user" + ":" + "synthetic" + "@example.test/feed"),
            dict(SOURCES[4], feed_url="https://example.test/" + "a" * 500),
            dict(SOURCES[4], client="epsilon"), dict(SOURCES[0], view_id=7),
        ])
        with patch("campaign_tool.meetings.default_opener", side_effect=AssertionError("no network")):
            for entry in cases:
                with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, r"meeting_sources\[1\]"):
                    load_sources({"meeting_sources": [SOURCES[0], entry]})
            for entry in SOURCES:
                with self.subTest(duplicate=entry), self.assertRaisesRegex(ValueError, r"meeting_sources\[1\].*duplicate"):
                    load_sources({"meeting_sources": [entry, entry]})
            views = load_sources({"meeting_sources": [SOURCES[2], dict(SOURCES[2], view_id=8)]})
            self.assertNotEqual(source_key(views[0]), source_key(views[1]))
            for entry in load_sources({"meeting_sources": SOURCES}):
                self.assertEqual(client_for(entry, None).source, entry["platform"])


if __name__ == "__main__":
    unittest.main()
