"""kit-response --kind clarification_requested|clarified: while the agency waits on the organizer, kit-status and kit due
say "waiting on you since D" (not past due) and kit-followup refuses with exit 3. Offline, synthetic."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_track  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402
from tests.test_kit_response import SENT_ON, run  # noqa: E402

LATE = "2026-04-30"  # well after the provisional determination date for SENT_ON
SINCE = "waiting on you since 2026-03-05 (the agency asked to clarify or narrow the request)"
NO_OPEN = "no open clarification request to answer"


class ClarificationTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-clarify-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        for arguments in (("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA"), ("kit", "--directory", self.root)):
            self.assertEqual(run(*arguments)[0], 0)
        self.sent = self.root / "kit" / "sent.json"
        self.followups = self.root / "kit" / "followups"
        self.agency = sorted((self.root / "kit" / "requests").glob("*.md"))[0].stem
        ready_drafts(self.root)
        self.assertEqual(self.cli("kit-sent", "--agency", self.agency, "--date", SENT_ON, "--channel", "email")[0], 0)

    def cli(self, command, *extra):
        return run(command, "--directory", self.root, *extra)

    def step(self, on, kind, *extra):
        return self.cli("kit-response", "--agency", self.agency, "--date", on, "--kind", kind, *extra)

    def ok(self, on, kind, *extra):
        code, out, err = self.step(on, kind, *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out.split("Recorded only.")[0])

    def refused_untouched(self, on, kind, words, code=1):
        before = self.sent.read_bytes()
        got, out, err = self.step(on, kind)
        self.assertEqual(got, code, out)
        self.assertIn(words, err)
        self.assertEqual(self.sent.read_bytes(), before)

    def row(self):
        code, out, err = self.cli("kit-status")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        return report["requests"][0], report["note"]

    def due(self, expect, as_of=LATE):
        code, out, err = self.cli("kit-due", "--as-of", as_of)
        self.assertEqual(code, expect, err)
        return json.loads(out)

    def edit_steps(self, steps):
        document = json.loads(self.sent.read_text())
        document["requests"][self.agency]["steps"] = steps
        self.sent.write_text(json.dumps(document), encoding="utf-8")

    def test_waiting_shown_not_past_due_then_clarified_is_past_due_again(self):
        printed = self.ok("2026-03-05", "clarification_requested", "--note", "asked to narrow the date range")
        self.assertEqual(printed["steps"][-1]["kind"], "clarification_requested")
        row, note = self.row()
        self.assertEqual(row["waiting_on_you_since"], "2026-03-05")
        self.assertIn("the agency asked to clarify or narrow the request", note)
        self.assertLess(row["determination_due"], LATE)
        report = self.due(0)
        self.assertEqual((report["past_due"], report["waiting_on_you"], report["stated_date_passed"]), (0, 1, 0))
        [waiting] = report["open"]
        self.assertEqual((waiting["state"], waiting["since"], waiting["due"], waiting["note"]),
                         ("waiting_on_you", "2026-03-05", row["determination_due"], SINCE))
        self.assertNotIn("legal deadline", waiting["note"])
        self.ok("2026-03-08", "clarified", "--note", "narrowed to 2025")
        self.assertIsNone(self.row()[0]["waiting_on_you_since"])
        report = self.due(3)
        self.assertEqual((report["past_due"], report["waiting_on_you"], report["open"][0]["state"]), (1, 0, "past_due"))

    def test_clarified_without_open_request_refused(self):
        self.refused_untouched("2026-03-05", "clarified", NO_OPEN)
        self.ok("2026-03-05", "acknowledged")
        self.refused_untouched("2026-03-06", "clarified", NO_OPEN)
        self.ok("2026-03-07", "clarification_requested")
        self.refused_untouched("2026-03-06", "clarified", "is before the previous step")  # out of order
        self.ok("2026-03-08", "clarified")
        self.refused_untouched("2026-03-09", "clarified", NO_OPEN)  # a second reply
        self.refused_untouched("2026-03-09", "Clarified", "invalid choice", code=2)  # wording is not a bypass

    def test_one_open_request_at_a_time_and_a_new_one_after_clarified(self):
        self.ok("2026-03-05", "clarification_requested")
        self.refused_untouched("2026-03-06", "clarification_requested",
                               "a clarification request is already open since 2026-03-05")
        self.ok("2026-03-06", "extension")
        self.refused_untouched("2026-03-07", "clarification_requested", "already open since 2026-03-05")
        self.ok("2026-03-07", "clarified")
        self.ok("2026-03-09", "clarification_requested")
        self.assertEqual(self.row()[0]["waiting_on_you_since"], "2026-03-09")

    def test_not_after_produced_denied_or_closed(self):
        for prior, words in (([{"on": "2026-03-05", "kind": "produced"}], "not allowed after a produced or denied"),
                             ([{"on": "2026-03-05", "kind": "denied"}], "not allowed after a produced or denied"),
                             ([{"on": "2026-03-04", "kind": "acknowledged"}, {"on": "2026-03-05", "kind": "closed"}],
                              "is closed")):
            with self.subTest(prior=prior[-1]["kind"]):
                self.edit_steps(prior)
                self.refused_untouched("2026-03-06", "clarification_requested", words)

    def test_closed_ends_the_wait(self):
        self.ok("2026-03-05", "clarification_requested")
        self.ok("2026-03-06", "closed")
        self.assertIsNone(self.row()[0]["waiting_on_you_since"])
        report = self.due(0)
        self.assertEqual((report["open"], report["waiting_on_you"]), ([], 0))
        self.refused_untouched("2026-03-07", "clarified", "is closed")

    def test_followup_refused_while_waiting_and_drafted_after_clarified(self):
        self.ok("2026-03-05", "clarification_requested")
        before = self.sent.read_bytes()
        code, out, err = self.cli("kit-followup", "--agency", self.agency, "--as-of", LATE)
        self.assertEqual(code, 3, out)
        self.assertEqual(err.splitlines()[0],
                         "Stopped: waiting on you since 2026-03-05 — reply to the agency's clarification first")
        self.assertEqual(out, "")
        self.assertEqual(sorted(self.followups.glob("*")) if self.followups.exists() else [], [])
        self.assertEqual(self.sent.read_bytes(), before)
        self.ok("2026-03-08", "clarified")
        code, out, err = self.cli("kit-followup", "--agency", self.agency, "--as-of", LATE)
        self.assertEqual(code, 0, err)
        self.assertEqual([p.name for p in self.followups.glob("*.md")], [f"{self.agency}-{LATE}.md"])

    def test_stated_date_request_waiting_is_not_stated_date_passed(self):
        self.ok("2026-03-10", "determination", "--expected-on", "2026-03-20")
        self.ok("2026-03-21", "clarification_requested")
        report = self.due(0)
        self.assertEqual((report["stated_date_passed"], report["stated_passed"], report["waiting_on_you"]), (0, [], 1))
        self.assertEqual([(r["state"], r["since"]) for r in report["open"]], [("waiting_on_you", "2026-03-21")])
        self.assertNotIn("Stated date passed", report["note"])
        self.ok("2026-03-22", "clarified")
        report = self.due(3)
        self.assertEqual((report["stated_date_passed"], report["waiting_on_you"]), (1, 0))

    def test_old_file_without_new_kinds_reads_as_before(self):
        self.edit_steps([{"on": "2026-03-04", "kind": "acknowledged", "recorded_at": "2026-03-04T10:00:00+00:00"}])
        row, note = self.row()
        self.assertIsNone(row["waiting_on_you_since"])
        self.assertEqual(note, kit_track.NOTE)
        report = self.due(3)
        self.assertEqual((report["waiting_on_you"], report["past_due"], report["note"]), (0, 1, kit_track.NOTE))
        [open_row] = report["open"]
        self.assertEqual(set(open_row), {"agency_id", "request", "name", "sent_on", "due", "days", "state",
                                         "reference"})
        self.assertEqual(open_row["state"], "past_due")


if __name__ == "__main__":
    unittest.main()
