"""NB-DF-12: cancelled Legistar events are marked, out of upcoming, and get no comment kit.

Synthetic fixtures only (example.invalid hosts, fictional example campaign); no network.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.cli import check_public_tree  # noqa: E402
from campaign_tool.content import ContentError, load_meeting  # noqa: E402
from campaign_tool.meeting_stage import MeetingStageError, stage  # noqa: E402
from campaign_tool.meetings import compare_meetings, event_status, fixture_opener, LegistarClient, relevant_events  # noqa: E402
from campaign_tool.site import build_site  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "legistar-events-cancelled.json"
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SINCE = date(2026, 10, 1)
CANCELLED = ["legistar-examplecity-2001", "legistar-examplecity-2002", "legistar-examplecity-2003",
             "legistar-examplecity-2006"]
SCHEDULED = ["legistar-examplecity-2004", "legistar-examplecity-2005", "legistar-examplecity-2007"]
IDS = re.compile(r'<article id="meeting-([^"]+)"')


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


def snapshot(folder):
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())} if folder.is_dir() else None


def meeting(mid, status=None, item="Item 7: ALPR"):
    record = {"id": mid, "starts_at": "2026-11-02T18:00:00", "agenda_item": item,
              "agenda_url": "https://agenda.example.invalid/1"}
    if status is not None:
        record["status"] = status
    return record


def doc(meetings):
    return {"schema_version": 1, "client": "examplecity", "generated": "2026-10-01T00:00:00+00:00",
            "meetings": meetings}


class EventStatusTests(unittest.TestCase):
    def test_agenda_status_values(self):
        for value in ("Cancelled", "canceled", " CANCELLED ", "\tCanceled\n"):
            self.assertEqual(event_status({"EventAgendaStatusName": value}), "cancelled", value)
        for value in ("Final", "Draft", "", "Cancelled meeting", "Not cancelled", None, 7, ["Cancelled"], {"x": 1}):
            self.assertEqual(event_status({"EventAgendaStatusName": value}), "scheduled", value)
        self.assertEqual(event_status({}), "scheduled")

    def test_comment_word_rule(self):
        for value in ("Meeting cancelled", "CANCELED due to lack of quorum", "This meeting has been canceled."):
            self.assertEqual(event_status({"EventComment": value}), "cancelled", value)
        for value in ("Cancellation policy discussed", "Cancel the contract?", "uncancelledness", "cancelledx",
                      "", None, ["Meeting cancelled"], 3):
            self.assertEqual(event_status({"EventComment": value}), "scheduled", value)

    def test_relevant_events_from_fixture(self):
        client = LegistarClient("examplecity", opener=fixture_opener(json.loads(FIXTURE.read_text())))
        meetings = relevant_events(client, since=SINCE)
        status = {m["id"]: m["status"] for m in meetings}
        self.assertEqual(sorted(status), sorted(CANCELLED + SCHEDULED))  # 2008 has no ALPR item
        self.assertEqual(sorted(i for i, s in status.items() if s == "cancelled"), CANCELLED)
        self.assertEqual(sorted(i for i, s in status.items() if s == "scheduled"), SCHEDULED)


class CompareCancelledTests(unittest.TestCase):
    def test_newly_cancelled_listed_not_agenda_changed(self):
        old = [meeting("a", "scheduled"), meeting("b", "scheduled")]
        new = [meeting("a", "cancelled", item="Item 7: ALPR (meeting cancelled)"), meeting("b", "scheduled", item="x")]
        out = compare_meetings(doc(old), new, "examplecity", SINCE)
        self.assertEqual(out["previous"], "compared")
        self.assertEqual(out["cancelled"], ["a"])
        self.assertEqual(out["agenda_changed"], ["b"])

    def test_still_cancelled_not_listed_again(self):
        old = [meeting("a", "cancelled")]
        out = compare_meetings(doc(old), [meeting("a", "cancelled", item="changed")], "examplecity", SINCE)
        self.assertEqual((out["cancelled"], out["agenda_changed"]), ([], []))

    def test_old_file_without_status_reads_as_scheduled(self):
        old = [meeting("a"), meeting("b"), meeting("c")]
        new = [meeting("a", "cancelled"), meeting("b", "scheduled", item="other"), meeting("c", "scheduled")]
        out = compare_meetings(doc(old), new, "examplecity", SINCE)
        self.assertEqual(out["previous"], "compared")
        self.assertEqual(out["cancelled"], ["a"])
        self.assertEqual(out["agenda_changed"], ["b"])

    def test_added_cancelled_is_added_and_uncancelled_is_not_listed(self):
        out = compare_meetings(doc([meeting("b", "cancelled")]), [meeting("a", "cancelled"), meeting("b", "scheduled")],
                               "examplecity", SINCE)
        self.assertEqual((out["added"], out["cancelled"]), (["a"], []))


class CampaignTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meetings-cancelled-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.kit = self.root / "kit" / "meetings.json"
        self.content = self.root / "content" / "meetings"

    def run_meetings(self):
        result = run("meetings", "--directory", str(self.root), "--client", "examplecity",
                     "--from-json", str(FIXTURE), "--since", "2026-10-01")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout), json.loads(self.kit.read_text(encoding="utf-8"))

    def test_cli_marks_counts_and_lists_cancelled(self):
        out, document = self.run_meetings()
        self.assertEqual(out["cancelled_meetings"], 4)
        self.assertEqual({m["id"]: m["status"] for m in document["meetings"]},
                         dict({i: "cancelled" for i in CANCELLED}, **{i: "scheduled" for i in SCHEDULED}))
        previous = json.loads(self.kit.read_text(encoding="utf-8"))
        for entry in previous["meetings"]:
            if entry["id"] == CANCELLED[0]:
                entry.pop("status")  # an old file without status
            elif entry["id"] in CANCELLED[1:3]:
                entry["status"] = "scheduled"
        self.kit.write_text(json.dumps(previous), encoding="utf-8")
        out, document = self.run_meetings()
        self.assertEqual(out["previous"], "compared")
        self.assertEqual(out["cancelled_meetings"], 4)
        self.assertEqual(document["changes"]["cancelled"], CANCELLED[:3])  # 2006 was already cancelled
        self.assertEqual(document["changes"]["agenda_changed"], [])
        out, document = self.run_meetings()
        self.assertEqual(document["changes"]["cancelled"], [])

    def test_stage_refuses_cancelled_and_writes_nothing(self):
        self.run_meetings()
        before = snapshot(self.content)
        for extra in ((), ("--replace",)):
            result = run("meetings-stage", "--directory", str(self.root), "--id", CANCELLED[0], *extra)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("Stopped:", result.stderr)
            self.assertIn(f"meeting {CANCELLED[0]} is cancelled; no comment kit", result.stderr)
            self.assertEqual(snapshot(self.content), before)
        (self.content / f"{CANCELLED[1]}.json").write_text("{}", encoding="utf-8")  # an earlier staged copy
        before = snapshot(self.content)
        with self.assertRaisesRegex(MeetingStageError, "is cancelled; no comment kit"):
            stage(self.root, CANCELLED[1], replace=True)
        self.assertEqual(snapshot(self.content), before)

    def test_stage_scheduled_works_as_before(self):
        self.run_meetings()
        result = run("meetings-stage", "--directory", str(self.root), "--id", SCHEDULED[0])
        self.assertEqual(result.returncode, 0, result.stderr)
        staged = json.loads((self.content / f"{SCHEDULED[0]}.json").read_text(encoding="utf-8"))
        self.assertIn("## Two-minute comment", staged["comment_kit_md"])

    def add(self, meeting_id, starts_at, status=None):
        record = {"id": meeting_id, "body": f"Synthetic council {meeting_id} (fictional)", "starts_at": starts_at}
        if status is not None:
            record["status"] = status
        (self.content / f"{meeting_id}.json").write_text(json.dumps(record), encoding="utf-8")

    def test_site_never_lists_cancelled_as_upcoming(self):
        self.add("future-off", "2026-11-04T18:00:00-08:00", "cancelled")
        self.add("future-on", "2026-11-05T18:00:00-08:00", "scheduled")
        self.add("future-default", "2026-11-06T18:00:00-08:00")
        self.add("past-off", "2026-10-01T18:00:00-07:00", "cancelled")
        build_site(self.root, today=date(2026, 10, 20))
        index, page = ((self.root / "public" / n).read_text(encoding="utf-8") for n in ("index.html", "meetings.html"))
        home = index[index.index('<section class="section" id="meetings">'):]
        home = home[:home.index("</section>")]
        self.assertEqual(IDS.findall(home), ["future-on", "future-default"])
        upcoming = page[page.index('id="content"'):page.index('id="past-meetings"')]
        past = page[page.index('id="past-meetings"'):]
        self.assertEqual(IDS.findall(upcoming), ["future-on", "future-default"])
        self.assertEqual(sorted(IDS.findall(past)), sorted(["future-off", "past-off", "cedar-council-2026-10"]))
        for mid in ("future-off", "past-off"):
            self.assertIn(f"<h3>Cancelled: Synthetic council {mid} (fictional)</h3>", past)
        self.assertNotIn("Cancelled:", upcoming)
        self.assertNotIn("<h3>Cancelled: Cedar", past)
        self.assertEqual(check_public_tree(self.root / "public"), [])


class ContentStatusTests(unittest.TestCase):
    BASE = {"id": "m1", "body": "Synthetic council (fictional)", "starts_at": "2026-11-04T18:00:00-08:00"}

    def test_status_values(self):
        self.assertEqual(load_meeting(dict(self.BASE), "meetings/m1.json")["status"], "scheduled")
        for value in ("scheduled", "cancelled"):
            self.assertEqual(load_meeting(dict(self.BASE, status=value), "meetings/m1.json")["status"], value)
        for value in ("postponed", "Cancelled", "canceled", " cancelled", "", None, 1, ["cancelled"]):
            with self.assertRaisesRegex(ContentError, r"meetings/m1\.json\.status"):
                load_meeting(dict(self.BASE, status=value), "meetings/m1.json")


if __name__ == "__main__":
    unittest.main()
