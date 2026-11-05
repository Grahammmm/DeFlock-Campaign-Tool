"""DF1737-16: CivicClerk public events API import. Recorded synthetic JSON only (fictional client examplecounty); no network."""
import contextlib
import copy
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli  # noqa: E402
from campaign_tool.meetings import CivicClerkClient, MeetingsError, fixture_opener, relevant_events, resolve_zone  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "meetings" / "civicclerk-examplecounty.json"
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SINCE = date(2026, 12, 1)


def recorded():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def run(mapping, since=SINCE, zone="America/Los_Angeles"):
    client = CivicClerkClient("examplecounty", opener=fixture_opener(mapping))
    return relevant_events(client, since=since, zone=resolve_zone(zone))


class Import(unittest.TestCase):
    def test_value_envelope_match(self):
        meetings = run(recorded())
        self.assertEqual(len(meetings), 1)
        m = meetings[0]
        self.assertEqual((m["id"], m["source"], m["event_id"]), ("civicclerk-examplecounty-2001", "civicclerk", 2001))
        self.assertEqual((m["starts_at"], m["time_known"]), ("2026-12-07T17:00:00-08:00", True))  # UTC Z -> --tz
        self.assertEqual(m["agenda_url"], "https://examplecounty.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId=55,plainText=false)")
        self.assertIn("ALPR", m["agenda_item"])
        self.assertEqual(run(recorded(), zone="America/New_York")[0]["starts_at"], "2026-12-07T20:00:00-05:00")

    def test_bare_list_accepted_and_object_without_value_stops(self):
        mapping = recorded()
        mapping["/v1/Events"] = mapping["/v1/Events"]["value"]
        self.assertEqual(len(run(mapping)), 1)
        mapping["/v1/Events"] = {"unexpected": []}
        with self.assertRaisesRegex(MeetingsError, r"https://examplecounty\.api\.civicclerk\.com/v1/Events.*expected"):
            run(mapping)

    def test_non_matching_old_and_capped(self):
        mapping = recorded()
        mapping["/v1/Meetings/900"]["items"][0]["childItems"][0]["agendaObjectItemName"] = "Parks budget"
        self.assertEqual(run(mapping), [])
        self.assertEqual(run(recorded(), since=date(2026, 12, 9)), [])  # event 2001 starts before --since
        many = recorded()
        events = [dict(many["/v1/Events"]["value"][0], id=3000 + i, agendaId=900) for i in range(61)]
        many["/v1/Events"] = {"value": events}
        asked = []
        client = CivicClerkClient("examplecounty", opener=lambda u: (asked.append(u), fixture_opener(many)(u))[1])
        self.assertEqual(len(relevant_events(client, since=SINCE)), 60)
        self.assertEqual(sum("/Meetings/" in u for u in asked), 60)

    def test_missing_start_skipped_and_agenda_url_host(self):
        mapping = recorded()
        events = mapping["/v1/Events"]["value"]
        events.append(dict(events[0], id=2003, startDateTime=None))
        self.assertEqual([m["event_id"] for m in run(mapping)], [2001])
        for url in ("https://evil.example.invalid/a.pdf", "http://examplecounty.api.civicclerk.com/a.pdf",
                    "https://notcivicclerk.com/a.pdf"):
            events[0]["publishedFiles"] = [{"type": "Agenda", "fileId": 55, "url": url}]
            self.assertIsNone(run(mapping)[0]["agenda_url"], url)
        events[0]["publishedFiles"] = [{"type": "Agenda", "url": "https://examplecounty.api.civicclerk.com/v1/x"}]
        self.assertEqual(run(mapping)[0]["agenda_url"], "https://examplecounty.api.civicclerk.com/v1/x")

    def test_cancelled(self):
        mapping = recorded()
        mapping["/v1/Events"]["value"][0]["eventName"] = "Canceled: Board of Supervisors"
        self.assertEqual(run(mapping)[0]["status"], "cancelled")
        self.assertEqual(run(recorded())[0]["status"], "scheduled")

    def test_bad_slug_refused_before_fetch(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("fetched")) as fetch:
            with self.assertRaises(MeetingsError):
                CivicClerkClient("Example County")
            fetch.assert_not_called()


class Cli(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-civicclerk-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))

    def main(self, fixture, client="examplecounty", platform="civicclerk"):
        path = self.tmp / "rec.json"
        path.write_text(json.dumps(fixture))
        argv = ["campaign_tool", "meetings", "--directory", str(self.root), "--platform", platform, "--client", client,
                "--from-json", str(path), "--since", "2026-12-01"]
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_write_and_reschedule(self):
        code, out, err = self.main(recorded())
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        doc = json.loads((self.root / "kit" / "meetings.json").read_text())
        self.assertEqual((report["platform"], doc["source"], doc["meetings"][0]["id"]),
                         ("civicclerk", "civicclerk", "civicclerk-examplecounty-2001"))
        moved = copy.deepcopy(recorded())
        moved["/v1/Events"]["value"][0]["startDateTime"] = "2026-12-09T02:00:00Z"
        code, out, err = self.main(moved)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["changes"]["rescheduled"], 1)

    def test_stopped_writes_nothing_and_bad_slug_never_fetches(self):
        code, _, err = self.main({"/v1/Events": {"unexpected": 1}})
        self.assertNotEqual(code, 0)
        self.assertIn("Stopped:", err)
        self.assertFalse((self.root / "kit" / "meetings.json").exists())
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("fetched")):
            code, _, err = self.main(recorded(), client="Example County")
        self.assertNotEqual(code, 0)
        self.assertIn("Stopped:", err)
        self.assertFalse((self.root / "kit" / "meetings.json").exists())


if __name__ == "__main__":
    unittest.main()
