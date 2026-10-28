"""Legistar client and comment-kit tests against a recorded synthetic fixture. No network."""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.meetings import (  # noqa: E402
    LegistarClient, MeetingsError, comment_kit_md, fixture_opener, match_items, relevant_events,
)

FIXTURE = json.loads((REPO_ROOT / "tests" / "fixtures" / "legistar-events.json").read_text())


class RecordingOpener:
    def __init__(self):
        self.urls = []
        self.inner = fixture_opener(FIXTURE)

    def __call__(self, url):
        self.urls.append(url)
        return self.inner(url)


class LegistarClientTests(unittest.TestCase):
    def test_events_query_uses_odata_date_filter(self):
        opener = RecordingOpener()
        client = LegistarClient("examplecity", opener=opener)
        events = client.events(since=date(2026, 9, 30))
        self.assertEqual(len(events), 4)
        self.assertEqual(len(opener.urls), 1)
        url = opener.urls[0]
        self.assertTrue(url.startswith("https://webapi.legistar.com/v1/examplecity/events?"))
        self.assertIn("EventDate%20ge%20datetime%272026-09-30%27", url)
        self.assertIn("%24orderby=EventDate", url)

    def test_relevant_events_filters_on_alpr_keywords(self):
        opener = RecordingOpener()
        client = LegistarClient("examplecity", opener=opener)
        meetings = relevant_events(client, since=date(2026, 9, 30))
        self.assertEqual([m["event_id"] for m in meetings], [1001, 1003])
        first = meetings[0]
        self.assertEqual(first["body"], "Example City Council")
        self.assertEqual(first["starts_at"], "2026-10-14T18:00:00")
        self.assertEqual(first["matched_terms"], ["alpr", "flock", "license plate"])
        self.assertEqual(first["matched_items"], 2)
        self.assertEqual(first["relevance"], "alpr_item")
        self.assertEqual(first["source"], "legistar")
        self.assertNotIn("<", first["agenda_item"])
        # insecure agenda URL dropped; unparseable date skipped (1004); zoning meeting excluded (1002)
        self.assertIsNone(meetings[1]["agenda_url"])
        self.assertEqual(meetings[1]["starts_at"], "2026-10-21T00:00:00")
        self.assertEqual(len([u for u in opener.urls if "eventitems" in u]), 4)

    def test_keyword_matching(self):
        hits = match_items([{"EventItemTitle": "Parking enforcement"},
                            {"EventItemTitle": "License-plate readers", "EventItemMatterName": None},
                            {"EventItemTitle": "ALPRS renewal"}, "not a dict"])
        self.assertEqual([terms for _, terms in hits], [["license-plate"], ["alprs"]])

    def test_client_validation_and_missing_fixture(self):
        with self.assertRaises(MeetingsError):
            LegistarClient("Bad Client!")
        client = LegistarClient("examplecity", opener=fixture_opener({}))
        with self.assertRaises(MeetingsError):
            client.events(since=date(2026, 1, 1))
        client = LegistarClient("examplecity", opener=lambda url: b"{not json")
        with self.assertRaises(MeetingsError):
            client.events(since=date(2026, 1, 1))
        with self.assertRaises(MeetingsError):
            client.event_items("1001")


class CommentKitTests(unittest.TestCase):
    def test_kit_from_published_findings(self):
        meeting = {"body": "Example City Council", "starts_at": "2026-10-14T18:00:00", "agenda_item": "Item 7", "relevance": "alpr_item"}
        findings = [{"title": "Policy lacks retention clause", "summary": "No retention period is set.",
                     "classification": "documented_fact", "confidence": "verified", "path": "/findings/policy.html",
                     "sources": [{"title": "Policy", "locator": "page 3", "sha256": "ab" * 32}]}]
        kit = comment_kit_md(meeting, findings, "Example Campaign", "https://campaign.example.invalid")
        self.assertIn("## Two-minute comment", kit)
        self.assertIn("## Three asks", kit)
        self.assertIn("Continue this item", kit)
        self.assertIn("published 1 reviewed finding,", kit)
        self.assertIn("https://campaign.example.invalid/findings/policy.html", kit)
        self.assertIn("sha256 abababababababab", kit)
        self.assertNotIn("<", kit)
        self.assertIn("not legal advice", kit)
        empty = comment_kit_md({"body": "B", "starts_at": "2026-10-14", "relevance": "budget"}, [], "C")
        self.assertIn("No published findings yet", empty)
        self.assertIn("consent calendar", empty)


class MeetingsCliTests(unittest.TestCase):
    def test_cli_writes_kit_meetings_json_offline(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-meetings-") as tmp:
            root = Path(tmp) / "campaign"
            subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root), "--name",
                            "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                           capture_output=True, timeout=20)
            result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "meetings", "--directory", str(root),
                                     "--client", "examplecity"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 1)
            self.assertIn("--online", result.stderr)
            result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "meetings", "--directory", str(root),
                                     "--client", "examplecity", "--from-json",
                                     str(REPO_ROOT / "tests" / "fixtures" / "legistar-events.json"), "--since", "2026-09-30"],
                                    cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["matched_meetings"], 2)
            document = json.loads((root / "kit" / "meetings.json").read_text())
            self.assertEqual(document["schema_version"], 1)
            self.assertEqual([m["id"] for m in document["meetings"]], ["legistar-examplecity-1001", "legistar-examplecity-1003"])


if __name__ == "__main__":
    unittest.main()
