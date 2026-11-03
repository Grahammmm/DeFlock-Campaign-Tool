"""kit-response --expected-on records the agency's own stated production date; kit-status shows the latest one and
kit-due lists answered, unproduced requests past it as stated_date_passed (exit 3). Offline, synthetic."""
import json
import re
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

RULE = "--expected-on is for the determination or a later step that restates the date"
WORDING = "stated date passed — the agency's own date; provisional, not a legal deadline"
OLD_DUE_KEYS = {"as_of", "law_package_status", "open", "answered", "past_due", "note"}


class ExpectedOnTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-expected-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        self.captured = []
        for arguments in (("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA"), ("kit", "--directory", self.root)):
            self.assertEqual(run(*arguments)[0], 0)
        self.sent = self.root / "kit" / "sent.json"
        self.agency = sorted((self.root / "kit" / "requests").glob("*.md"))[0].stem
        ready_drafts(self.root)
        self.assertEqual(self.cli("kit-sent", "--agency", self.agency, "--date", SENT_ON, "--channel", "email")[0], 0)

    def cli(self, command, *extra):
        code, out, err = run(command, "--directory", self.root, *extra)
        self.captured.append(out + err)
        return code, out, err

    def step(self, on, kind, *extra):
        return self.cli("kit-response", "--agency", self.agency, "--date", on, "--kind", kind, *extra)

    def ok(self, on, kind, *extra):
        code, out, err = self.step(on, kind, *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out.split("Recorded only.")[0])

    def refused_untouched(self, args, *words):
        before = self.sent.read_bytes()
        code, out, err = self.step(*args)
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        for word in words:
            self.assertIn(word, err)
        self.assertEqual(self.sent.read_bytes(), before)

    def row(self):
        code, out, err = self.cli("kit-status")
        self.assertEqual(code, 0, err)
        return json.loads(out)["requests"][0]

    def due(self, as_of, expect):
        code, out, err = self.cli("kit-due", "--as-of", as_of)
        self.assertEqual(code, expect, err)
        return json.loads(out)

    def edit_steps(self, steps):
        document = json.loads(self.sent.read_text())
        document["requests"][self.agency]["steps"] = steps
        self.sent.write_text(json.dumps(document), encoding="utf-8")

    def test_determination_stored_shown_and_listed_only_after_the_date(self):
        printed = self.ok("2026-03-10", "determination", "--expected-on", "2026-04-01")
        self.assertEqual(printed["steps"][-1]["expected_on"], "2026-04-01")
        stored = json.loads(self.sent.read_text())["requests"][self.agency]["steps"][-1]
        self.assertEqual((stored["kind"], stored["expected_on"]), ("determination", "2026-04-01"))
        self.assertEqual(self.row()["stated_production_date"], "2026-04-01")
        for as_of in ("2026-03-31", "2026-04-01"):  # the day before and the stated day itself: not passed
            report = self.due(as_of, 0)
            self.assertEqual((report["stated_passed"], report["stated_date_passed"]), ([], 0))
            self.assertEqual(report["note"], kit_track.NOTE)
        report = self.due("2026-04-02", 3)
        self.assertEqual(report["stated_passed"], [{"agency_id": self.agency, "request": "main", "name": mock.ANY,
                                                    "sent_on": SENT_ON, "state": "stated_date_passed",
                                                    "stated": "2026-04-01", "days": 1, "reference": None}])
        self.assertEqual((report["stated_date_passed"], report["past_due"], report["open"]), (1, 0, []))
        self.assertEqual([r["agency_id"] for r in report["answered"]], [self.agency])
        self.assertIn(WORDING[0].upper() + WORDING[1:], report["note"])
        self.assertEqual(kit_track.STATED, WORDING)
        self.assertEqual(self.due("2026-05-01", 3)["stated_passed"][0]["days"], 30)

    def test_same_day_allowed_one_day_before_or_not_a_date_refused(self):
        self.refused_untouched(("2026-03-10", "determination", "--expected-on", "2026-03-09"),
                               "stated date 2026-03-09 is before the determination on 2026-03-10")
        for value in ("2026-02-30", "soon", "", "2026-4-01"):
            self.refused_untouched(("2026-03-10", "determination", "--expected-on", value),
                                   "--expected-on must be a real YYYY-MM-DD date")
        self.assertEqual(self.ok("2026-03-10", "determination", "--expected-on", "2026-03-10")["steps"][-1]
                         ["expected_on"], "2026-03-10")
        self.ok("2026-03-12", "fee_estimate", "--amount", "10")
        self.refused_untouched(("2026-03-14", "fee_estimate", "--amount", "10", "--expected-on", "2026-03-13"),
                               "stated date 2026-03-13 is before the fee_estimate on 2026-03-14")

    def test_refused_on_kinds_that_do_not_state_a_date(self):
        flag = ("--expected-on", "2026-04-01")
        self.refused_untouched(("2026-03-03", "acknowledged", *flag), RULE)
        self.refused_untouched(("2026-03-03", "extension", *flag), RULE)
        self.refused_untouched(("2026-03-03", "fee_estimate", "--amount", "5", *flag), RULE)  # before any determination
        self.ok("2026-03-03", "fee_estimate", "--amount", "5")
        self.refused_untouched(("2026-03-04", "fee_estimate", "--amount", "5", *flag), RULE)  # still none
        self.ok("2026-03-05", "determination")
        for kind in ("produced", "denied", "closed"):
            self.refused_untouched(("2026-03-06", kind, *flag), RULE)
        self.assertIsNone(self.row()["stated_production_date"])

    def test_later_restated_date_wins_in_status_and_due(self):
        self.ok("2026-03-05", "determination", "--expected-on", "2026-03-20")
        self.assertEqual(self.due("2026-03-25", 3)["stated_passed"][0]["stated"], "2026-03-20")
        self.ok("2026-03-24", "fee_estimate", "--amount", "40", "--expected-on", "2026-04-15")
        self.assertEqual(self.row()["stated_production_date"], "2026-04-15")
        self.assertEqual(self.due("2026-03-25", 0)["stated_date_passed"], 0)
        report = self.due("2026-04-20", 3)
        self.assertEqual([(r["stated"], r["days"]) for r in report["stated_passed"]], [("2026-04-15", 5)])
        self.ok("2026-04-21", "fee_estimate", "--amount", "40")  # an estimate without a date keeps the last one
        self.assertEqual(self.row()["stated_production_date"], "2026-04-15")

    def test_production_clears_and_a_restated_date_rearms(self):
        self.ok("2026-03-05", "determination", "--expected-on", "2026-03-20")
        self.ok("2026-03-25", "produced", "--note", "partial set")
        self.assertEqual(self.due("2026-03-26", 0)["stated_passed"], [])
        self.assertEqual(self.row()["stated_production_date"], "2026-03-20")  # still shown, no longer listed
        self.ok("2026-03-26", "fee_estimate", "--amount", "0", "--expected-on", "2026-04-10")
        self.assertEqual(self.due("2026-04-10", 0)["stated_date_passed"], 0)
        self.assertEqual(self.due("2026-04-11", 3)["stated_passed"][0]["stated"], "2026-04-10")

    def test_production_before_the_stated_date_also_clears(self):
        self.ok("2026-03-05", "determination", "--expected-on", "2026-04-30")
        self.ok("2026-03-06", "produced")
        self.assertEqual(self.due("2026-05-15", 0)["stated_date_passed"], 0)

    def test_closed_request_is_never_listed(self):
        self.ok("2026-03-05", "determination", "--expected-on", "2026-03-20")
        self.ok("2026-03-06", "closed")
        report = self.due("2026-06-01", 0)
        self.assertEqual((report["stated_passed"], report["stated_date_passed"], report["answered"]), ([], 0, []))

    def test_hand_edited_expected_on_in_the_wrong_place_is_malformed(self):
        self.ok("2026-03-05", "determination")
        self.ok("2026-03-06", "produced")
        good = json.loads(self.sent.read_text())["requests"][self.agency]["steps"]
        determination, produced = good
        early_fee = {"on": "2026-03-04", "kind": "fee_estimate", "amount_cents": 1, "expected_on": "2026-04-01",
                     "recorded_at": produced["recorded_at"]}
        for steps in ([determination, {**produced, "expected_on": "2026-04-01"}],
                      [{**determination, "expected_on": "next month"}, produced],
                      [{**determination, "expected_on": 20260401}, produced],
                      [{**determination, "expected_on": "2026-03-04"}, produced],
                      [early_fee, determination, produced],
                      [{**determination, "kind": "denied", "expected_on": "2026-04-01"}]):
            with self.subTest(steps=steps):
                self.edit_steps(steps)
                for command in ("kit-status", "kit-due"):
                    code, out, err = self.cli(command)
                    self.assertEqual(code, 1, out)
                    self.assertIn("malformed steps", err)
                self.refused_untouched(("2026-03-07", "closed"), "malformed steps")

    def test_old_file_without_expected_on_adds_only_the_null_field_and_zero_count(self):
        self.ok("2026-03-05", "determination")
        row = self.row()
        self.assertIsNone(row["stated_production_date"])
        report = self.due("2026-06-01", 0)
        self.assertEqual(set(report), OLD_DUE_KEYS | {"stated_passed", "stated_date_passed", "waiting_on_you"})
        self.assertEqual((report["stated_passed"], report["stated_date_passed"], report["note"]),
                         ([], 0, kit_track.NOTE))
        self.assertNotIn("expected_on", self.sent.read_text())

    def test_no_legal_claim_wording_anywhere(self):
        self.ok("2026-03-05", "determination", "--expected-on", "2026-03-20")
        self.due("2026-04-01", 3)
        self.row()
        self.refused_untouched(("2026-03-06", "produced", "--expected-on", "2026-04-01"), RULE)
        self.captured.append(run("kit-response", "--help")[1])
        self.captured.append((REPO_ROOT / "docs" / "AGENCY-TIMELINE.md").read_text(encoding="utf-8")
                             .split("## Stated production date")[1])
        text = "\n".join(self.captured).lower()
        for word in ("violat", "illegal", "unlawful"):
            self.assertNotIn(word, text)
        self.assertIsNone(re.search(r"\blate\b", text))  # law.HOLIDAY_NOTE's "lateness" and the rule's "later" differ
        for added in (WORDING, kit_track.STATED):
            self.assertNotIn("late", added.lower())


if __name__ == "__main__":
    unittest.main()
