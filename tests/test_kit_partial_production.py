"""kit-response --kind produced --partial records a rolling production: the request stays open in kit-status, kit-due,
kit-followup and today (with the agency's next installment date) until a final produced or closed step; files without
"partial" read and report as before. Offline, synthetic."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from tests.test_kit_check import ready_drafts  # noqa: E402
from tests.test_kit_response import SENT_ON, run  # noqa: E402

STAMP = "2026-03-25T12:00:00+00:00"
LEGACY_STEPS = [{"on": "2026-03-05", "kind": "determination", "expected_on": "2026-03-20", "recorded_at": STAMP},
                {"on": "2026-03-25", "kind": "produced", "note": "partial set", "recorded_at": STAMP}]
NEW_ROW_KEYS = {"producing": False, "next_installment": None}
MALFORMED = "kit/sent.json unreadable: malformed steps"


class PartialProductionTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-partial-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        for arguments in (("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA"), ("kit", "--directory", self.root)):
            self.assertEqual(run(*arguments)[0], 0)
        self.sent = self.root / "kit" / "sent.json"
        self.agency, self.other = [p.stem for p in sorted((self.root / "kit" / "requests").glob("*.md"))][:2]
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

    def refused_untouched(self, args, message):
        before = self.sent.read_bytes()
        code, out, err = self.step(*args)
        self.assertEqual((code, err), (1, f"Stopped: {message}\n"), out)
        self.assertEqual(self.sent.read_bytes(), before)

    def row(self):
        code, out, err = self.cli("kit-status")
        self.assertEqual(code, 0, err)
        return json.loads(out)["requests"][0]

    def due(self, as_of, expect):
        code, out, err = self.cli("kit-due", "--as-of", as_of)
        self.assertEqual(code, expect, err)
        return json.loads(out)

    def today(self, as_of):
        code, out, err = self.cli("today", "--as-of", as_of)
        self.assertEqual(code, 0, err)
        return out.replace(str(self.root), "DIR")

    def followup(self, as_of):
        return self.cli("kit-followup", "--agency", self.agency, "--as-of", as_of)

    def edit_steps(self, steps):
        document = json.loads(self.sent.read_text())
        document["requests"][self.agency]["steps"] = steps
        self.sent.write_text(json.dumps(document), encoding="utf-8")

    def test_legacy_file_reads_and_reports_as_on_main(self):
        self.assertEqual(self.cli("kit-sent", "--agency", self.other, "--date", SENT_ON, "--channel", "mail")[0], 0)
        self.edit_steps(LEGACY_STEPS)
        before = self.sent.read_bytes()
        code, out, err = self.cli("kit-status")
        self.assertEqual((code, err), (0, ""))
        rows = json.loads(out)["requests"]
        self.assertEqual([{k: r.pop(k) for k in NEW_ROW_KEYS} for r in rows], [NEW_ROW_KEYS, NEW_ROW_KEYS])
        self.assertEqual(json.dumps(rows), GOLDEN_STATUS_ROWS.replace("AGENCY", self.agency)
                         .replace("OTHER", self.other))
        report = self.due("2026-04-01", 3)
        self.assertEqual(report.pop("installment_passed"), 0)
        self.assertEqual(json.dumps(report), GOLDEN_DUE.replace("AGENCY", self.agency).replace("OTHER", self.other))
        self.assertEqual(self.today("2026-04-01"), GOLDEN_TODAY.replace("OTHER", self.other))
        self.assertEqual(self.followup("2026-04-01"), (1, "", f"Stopped: request for {self.agency} was answered "
                                                           "(determination on 2026-03-05); no follow-up drafted\n"))
        code, _, err = self.cli("kit-followup", "--agency", self.other, "--as-of", "2026-04-01")
        self.assertEqual(code, 0, err)
        draft = (self.root / "kit" / "followups" / f"{self.other}-2026-04-01.md").read_text(encoding="utf-8")
        self.assertEqual(draft, GOLDEN_FOLLOWUP.replace("OTHER", self.other))
        self.assertEqual(self.sent.read_bytes(), before)

    def test_partial_with_date_is_producing_then_installment_passed(self):
        printed = self.ok("2026-03-10", "produced", "--partial", "--expected-on", "2026-03-20", "--note", "batch 1")
        self.assertEqual({k: printed["steps"][-1][k] for k in ("kind", "partial", "expected_on")},
                         {"kind": "produced", "partial": True, "expected_on": "2026-03-20"})
        row = self.row()
        self.assertEqual((row["producing"], row["next_installment"], row["stated_production_date"]),
                         (True, "2026-03-20", None))
        report = self.due("2026-03-10", 0)
        self.assertEqual([(r["state"], r["due"], r["days"]) for r in report["open"]], [("producing", "2026-03-20", 10)])
        self.assertEqual((report["answered"], report["installment_passed"]), ([], 0))
        self.assertEqual(self.today("2026-03-10"), "Nothing due on 2026-03-10.\n")
        self.assertEqual(self.due("2026-03-20", 0)["open"][0]["state"], "producing")  # the stated day itself
        report = self.due("2026-03-21", 3)
        self.assertEqual([(r["state"], r["days"]) for r in report["open"]], [("installment_passed", -1)])
        self.assertEqual((report["past_due"], report["installment_passed"], report["stated_date_passed"]), (0, 1, 0))
        code, out, err = self.cli("today", "--as-of", "2026-03-21", "--json")
        self.assertEqual(code, 0, err)
        items = [i for i in json.loads(out) if i["kind"].startswith("request_")]
        self.assertEqual([(i["kind"], i["due"]) for i in items], [("request_installment_passed", "2026-03-20")])
        self.assertIn("the next installment the agency stated for 2026-03-20 is not recorded yet", items[0]["what"])

    def test_installment_due_today_item(self):
        self.ok("2026-03-10", "produced", "--partial", "--expected-on", "2026-03-20")
        self.assertIn("[request_installment_due] Check on ", self.today("2026-03-20"))
        self.assertIn("for 2026-03-20 is due today", self.today("2026-03-20"))

    def test_latest_partial_date_wins_and_an_undated_partial_clears_it(self):
        self.ok("2026-03-10", "produced", "--partial", "--expected-on", "2026-03-20")
        self.ok("2026-03-20", "produced", "--partial", "--expected-on", "2026-04-15")
        self.assertEqual(self.row()["next_installment"], "2026-04-15")
        self.assertEqual(self.due("2026-04-01", 0)["open"][0]["state"], "producing")
        self.ok("2026-04-02", "produced", "--partial")
        row = self.row()
        self.assertEqual((row["producing"], row["next_installment"]), (True, None))
        report = self.due("2026-06-01", 0)
        self.assertEqual([(r["state"], r["due"], r["days"]) for r in report["open"]], [("producing", None, None)])

    def test_final_produced_ends_production_and_clears_the_stated_date(self):
        self.ok("2026-03-05", "determination", "--expected-on", "2026-03-20")
        self.ok("2026-03-10", "produced", "--partial", "--expected-on", "2026-03-15")
        self.assertEqual(self.row()["stated_production_date"], "2026-03-20")  # the partial's date is not the stated one
        report = self.due("2026-03-25", 3)
        self.assertEqual((report["installment_passed"], report["stated_date_passed"]), (1, 0))
        self.ok("2026-03-26", "produced")
        row = self.row()
        self.assertEqual((row["producing"], row["next_installment"]), (False, None))
        report = self.due("2026-04-30", 0)
        self.assertEqual((report["open"], report["stated_passed"], report["installment_passed"]), ([], [], 0))
        self.assertEqual([r["agency_id"] for r in report["answered"]], [self.agency])
        self.assertEqual(self.followup("2026-04-30"), (1, "", f"Stopped: request for {self.agency} was answered "
                                                           "(determination on 2026-03-05); no follow-up drafted\n"))

    def test_partial_does_not_clear_a_stated_date_but_denied_or_closed_ends_production(self):
        self.ok("2026-03-05", "determination", "--expected-on", "2026-03-20")
        self.ok("2026-03-10", "produced", "--partial")
        self.ok("2026-03-11", "denied")
        report = self.due("2026-03-25", 3)
        self.assertEqual((report["open"], report["stated_date_passed"]), ([], 1))
        self.ok("2026-03-26", "closed")
        report = self.due("2026-03-27", 0)
        self.assertEqual((report["open"], report["answered"], report["stated_date_passed"]), ([], [], 0))

    def test_refusals_write_nothing(self):
        self.refused_untouched(("2026-03-05", "acknowledged", "--partial"), "--partial is only for produced steps")
        self.refused_untouched(("2026-03-05", "denied", "--partial"), "--partial is only for produced steps")
        self.ok("2026-03-05", "produced", "--partial")
        self.ok("2026-03-06", "produced", "--partial")  # partials may repeat
        self.refused_untouched(("2026-03-07", "extension"),
                               "extension is not allowed after a determination, produced or denied step")
        self.refused_untouched(("2026-03-07", "clarification_requested"),
                               "clarification_requested is not allowed after a produced or denied step")
        self.ok("2026-03-08", "produced")
        self.refused_untouched(("2026-03-09", "produced", "--partial"), "production already final on 2026-03-08")
        self.refused_untouched(("2026-03-09", "produced", "--partial", "--expected-on", "2026-04-01"),
                               "production already final on 2026-03-08")

    def test_expected_on_rules(self):
        rule = "--expected-on is for the determination or a later step that restates the date"
        self.refused_untouched(("2026-03-10", "produced", "--partial", "--expected-on", "2026-03-09"),
                               "stated date 2026-03-09 is before the produced on 2026-03-10")
        self.refused_untouched(("2026-03-10", "produced", "--expected-on", "2026-03-20"), rule)
        self.ok("2026-03-10", "produced", "--partial", "--expected-on", "2026-03-10")  # same day allowed
        self.refused_untouched(("2026-03-11", "denied", "--expected-on", "2026-03-20"), rule)
        self.refused_untouched(("2026-03-11", "closed", "--expected-on", "2026-03-20"), rule)

    def test_hand_edited_partial_is_malformed(self):
        self.ok("2026-03-05", "produced", "--partial")
        good = json.loads(self.sent.read_text())["requests"][self.agency]["steps"][0]
        for steps in ([{**good, "partial": "yes"}], [{**good, "partial": False}],
                      [{**good, "kind": "denied"}], [{**good, "kind": "determination"}],
                      [{**{k: v for k, v in good.items() if k != "partial"}, "expected_on": "2026-03-20"}]):
            with self.subTest(steps=steps):
                self.edit_steps(steps)
                for command in ("kit-status", "kit-due"):
                    code, out, err = self.cli(command)
                    self.assertEqual(code, 1, out)
                    self.assertIn(MALFORMED, err)
                self.refused_untouched(("2026-03-07", "closed"), f"{MALFORMED} for {self.agency!r}")

    def test_waiting_on_you_wins_over_producing(self):
        self.ok("2026-03-04", "clarification_requested")
        self.ok("2026-03-05", "produced", "--partial", "--expected-on", "2026-03-06")
        report = self.due("2026-03-20", 0)
        self.assertEqual([r["state"] for r in report["open"]], ["waiting_on_you"])
        self.assertEqual(report["installment_passed"], 0)
        self.assertEqual(self.followup("2026-03-20")[0], 3)

    def test_followup_drafts_only_after_the_installment_date(self):
        self.ok("2026-03-10", "produced", "--partial", "--expected-on", "2026-03-20")
        self.assertEqual(self.followup("2026-03-20"), (1, "", "Stopped: records still coming: next installment "
                                                           "2026-03-20; not passed\n"))
        self.assertFalse((self.root / "kit" / "followups" / f"{self.agency}-2026-03-20.md").exists())
        code, out, err = self.followup("2026-03-25")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out.split("Draft only.")[0])["days_past"], 5)
        draft = (self.root / "kit" / "followups" / f"{self.agency}-2026-03-25.md").read_text(encoding="utf-8")
        self.assertIn("you said the next installment would come on 2026-03-20. As of 2026-03-25 we have no record of "
                      "that installment.", draft)
        self.assertNotIn("determination date", draft)
        self.ok("2026-03-26", "produced", "--partial")  # no next date: nothing to follow up on
        self.assertEqual(self.followup("2026-03-27")[2], "Stopped: records still coming: next installment not stated; "
                                                        "not passed\n")


# Goldens: main a977fde4's kit-status rows, kit-due report, today text and follow-up draft for the same legacy file.
def _row(agency, name, channel, steps, last_step, stated):
    return {"agency_id": agency, "request": "main", "name": name, "sent_on": "2026-03-02", "channel": channel,
            "draft_changed_since_sent": False, "determination_due": "2026-03-12", "with_extension": "2026-03-26",
            "steps": steps, "extended": False, "closed": False, "last_step": last_step, "records": 0,
            "record_prefixes": [], "fee_estimate": None, "over_fee_cap": None, "stated_production_date": stated,
            "waiting_on_you_since": None, "reference": None}


A_NAME, B_NAME = "Arroyo Grande Police Department", "Atascadero Police Department"
LAST = {"on": "2026-03-25", "kind": "produced"}
GOLDEN_STATUS_ROWS = json.dumps([_row("AGENCY", A_NAME, "email", 2, LAST, "2026-03-20"),
                                 _row("OTHER", B_NAME, "mail", 0, None, None)])
GOLDEN_DUE = json.dumps({
    "as_of": "2026-04-01", "law_package_status": "draft",
    "open": [{"agency_id": "OTHER", "request": "main", "name": B_NAME, "sent_on": "2026-03-02", "reference": None,
              "due": "2026-03-12", "days": -20, "state": "past_due"}],
    "answered": [{"agency_id": "AGENCY", "request": "main", "name": A_NAME, "sent_on": "2026-03-02",
                  "reference": None, "last_step": LAST}],
    "past_due": 1, "stated_passed": [], "stated_date_passed": 0, "waiting_on_you": 0,
    "note": "Provisional reminders only; the period runs from the agency's receipt. Provisional reminders only, not "
            "legal due dates. Business-day counts skip Saturday and Sunday only; public holidays are not modeled. "
            "Confirm actual receipt, applicable last-day holiday exclusions and lawful extension notice before any "
            "lateness conclusion."})
GOLDEN_TODAY = (f"1. [request_past_due] Follow up with {B_NAME} (OTHER): request sent 2026-03-02 passed its "
                "provisional determination date 2026-03-12 20 day(s) ago — kit-status --directory DIR\n")
GOLDEN_FOLLOWUP = (
    f"# Follow-up draft: {B_NAME}\n\nDRAFT — NOT SENT. Review before sending it yourself.\n\n"
    f"To: {B_NAME}, records custodian\n\nOn 2026-03-02 we sent a records request by mail to {B_NAME}. Its "
    "provisional determination date was 2026-03-12; the period runs from the agency's receipt of the request. As of "
    "2026-04-01 we have no record of a determination, records produced or a denial.\n\nPlease tell us the status of "
    "the request under the California Public Records Act, Gov. Code § 7920.000 et seq. (recodified by AB 473, Stats. "
    "2021, ch. 614; operative 2023-01-01), and when you expect to respond.\n\n[requester-approved contact details]"
    "\n\n---\n\nLaw package status: draft (not independently reviewed)\nProvisional reminders only, not legal due "
    "dates. Business-day counts skip Saturday and Sunday only; public holidays are not modeled. Confirm actual "
    "receipt, applicable last-day holiday exclusions and lawful extension notice before any lateness conclusion.\n")

if __name__ == "__main__":
    unittest.main()
