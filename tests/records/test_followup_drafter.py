"""Follow-up drafts by playbook pattern: golden drafts from the three request-quality examples, tier order,
policy quote, citation flags, prior-asks check and the banned-phrase gate."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool.records import agency_policies as pol
from campaign_tool.records import followup_drafter as fd
from campaign_tool.records import playbook as pb

ROOT = Path(__file__).resolve().parents[2]
GOLD = Path(__file__).resolve().parent / "golden_followup"
REGISTER = ROOT / "data" / "slo-pilot" / "agency-policies.csv"
ROWS, _ = pol.load(REGISTER)


def case(name):
    return json.loads((GOLD / (name + ".case.json")).read_text(encoding="utf-8"))


def make(**over):
    base = {"schema": fd.SCHEMA, "agency": {"id": "example-pd", "name": "Example PD"}, "request_id": "R-1",
            "response_type": "no_records",
            "asks": [{"text": "Completed audits of ALPR use", "rating": "no_records_stated"}]}
    base.update(over)
    return base


class GoldenTests(unittest.TestCase):
    def setUp(self):
        self.book = pb.load()

    def test_three_quality_drafts_match_their_golden_text(self):
        for name in ("sheriff", "grover", "arroyo"):
            with self.subTest(name):
                out = fd.draft(self.book, case(name), ROWS)
                expected = (GOLD / (name + ".expected.md")).read_text(encoding="utf-8")
                self.assertEqual(out["body"], expected)
                self.assertEqual(out["status"], "needs edit")  # the recipient is never taken from the record
                self.assertEqual(out["unfilled"], ["recipient and contact; not taken from the record"])
                self.assertIs(out["sent"], False)
                self.assertTrue(out["review_required"])
                self.assertIn("DRAFT \u2014 NOT SENT", out["body"])

    def test_sheriff_is_blanket_denial_with_tier_one_first_and_policy_quote(self):
        out = fd.draft(self.book, case("sheriff"), ROWS)
        self.assertEqual(out["pattern"], "blanket_denial")
        self.assertEqual([(a["number"], a["tier"]) for a in out["open_asks"]], [(7, 1), (6, 1), (8, 2)])
        self.assertIn("Policy 466.5(e)", out["body"])
        self.assertIn("Audits of law enforcement requests for data will be conducted the first of each calendar year", out["body"])
        self.assertIn("Another county agency released", out["body"])
        self.assertNotIn("Grover", out["body"])  # G8: the precedent agency is never named

    def test_unverified_citation_is_flagged_and_kept_out_of_the_body(self):
        out = fd.draft(self.book, case("sheriff"), ROWS)
        flags = {c["ref"]: (c["status"], c["in_body"]) for c in out["citations"]}
        self.assertEqual(flags["Gov. Code 7922.600"], ("unverified", False))
        self.assertEqual(flags["Civ. Code 1798.90.52"], ("verified-in-package", True))
        letter = out["body"].split(fd.NOTES_RULE)[0]
        self.assertNotIn("7922.600", letter)
        self.assertIn("1798.90.52", letter)

    def test_prior_asks_check_never_repeats_produced_logs(self):
        out = fd.draft(self.book, case("grover"), ROWS)
        self.assertEqual(out["dropped"], [{"number": 5, "text": "Search logs for January to June 2026", "reason": "already produced"}])
        letter = out["body"].split(fd.NOTES_RULE)[0]
        self.assertNotIn("Item 5 asked for", letter)
        self.assertIn("not asking for those again", letter)

    def test_pending_earlier_ask_is_dropped_not_repeated(self):
        c = make(prior_asks=[{"text": "completed audits of alpr use", "status": "pending"}])
        out = fd.draft(self.book, c)
        self.assertEqual(out["dropped"][0]["reason"], "already asked, still pending")
        self.assertEqual(out["open_asks"], [])


class GateTests(unittest.TestCase):
    def setUp(self):
        self.book = pb.load()

    def test_banned_phrase_marks_needs_edit(self):
        out = fd.draft(self.book, make(subject="This was a violation"))
        self.assertEqual(out["status"], "needs edit")
        self.assertEqual([h["id"] for h in out["banned_phrases"]["hits"]], ["violation"])

    def test_open_placeholder_marks_needs_edit(self):
        out = fd.draft(self.book, make(response_type="partial"))
        self.assertEqual(out["status"], "needs edit")
        self.assertIn("[EDIT ME: n]", out["body"])
        filled = fd.draft(self.book, make(response_type="partial", recipient="Records Unit", contact="Ms. A", sender="Project", slots={"n": "6", "requested window": "1 Jan-30 Jun 2026",
                                                                           "produced window": "8 Jul-7 Aug 2026",
                                                                           "Template or substitute": "A blank template",
                                                                           "requested record": "the executed MOUs"}))
        self.assertEqual(filled["unfilled"], [])
        self.assertIn("Item 6 asked for 1 Jan-30 Jun 2026", filled["body"])

    def test_unresolved_recipient_contact_and_sender_mark_needs_edit(self):
        out = fd.draft(self.book, make())
        for inner in ("recipient and contact; not taken from the record", "contact", "name"):
            self.assertIn(inner, out["unfilled"])
        self.assertEqual(out["status"], "needs edit")
        ready = fd.draft(self.book, make(recipient="Records Unit", contact="Ms. A", sender="Project",
                                         slots={"policy_section": "Policy 1"}))
        self.assertEqual((ready["unfilled"], ready["status"]), ([], "ready for review"))

    def test_questions_about_dropped_audit_ask_are_not_repeated(self):
        c = make(asks=[{"text": "Completed audits of ALPR use", "rating": "answered", "produced": [{"file": "audits.pdf"}]},
                       {"text": "The vendor agreement", "rating": "no_records_stated"}],
                 prior_asks=[])
        out = fd.draft(self.book, c)
        letter = out["body"].split("\n---\n")[0]
        self.assertNotIn("audit exists", letter)
        self.assertNotIn("conducted regularly", letter)
        self.assertIn("Which systems were searched?", letter)
        pending = fd.draft(self.book, make(prior_asks=[{"text": "Completed audits of ALPR use", "status": "pending"}]))
        self.assertEqual([d["reason"] for d in pending["dropped"]], ["already asked, still pending"])
        self.assertNotIn("audit exists", pending["body"].split("\n---\n")[0])

    def test_dropped_audit_question_stays_out_when_open_ask_shares_a_word(self):
        open_ask = {"text": "Emails about the ALPR vendor agreement", "rating": "no_records_stated"}
        produced = make(asks=[{"text": "Completed audits of ALPR use", "rating": "answered", "produced": [{"file": "audits.pdf"}]},
                              open_ask], prior_asks=[])
        pending = make(asks=[{"text": "Completed audits of ALPR use"}, open_ask],
                       prior_asks=[{"text": "Completed audits of ALPR use", "status": "pending"}])
        for case in (produced, pending):
            out = fd.draft(self.book, case)
            letter = out["body"].split("\n---\n")[0]
            self.assertEqual(len(out["dropped"]), 1)
            self.assertNotIn("audit exists", letter)
            self.assertNotIn("conducted regularly", letter)

    def test_dropped_audit_question_stays_out_when_open_ask_shares_the_subject_word(self):
        open_ask = {"text": "ALPR audit trail access logs", "rating": "no_records_stated"}
        produced = make(asks=[{"text": "Completed audits of ALPR use", "rating": "answered", "produced": [{"file": "audits.pdf"}]},
                              open_ask], prior_asks=[])
        pending = make(asks=[{"text": "Completed audits of ALPR use"}, open_ask],
                       prior_asks=[{"text": "Completed audits of ALPR use", "status": "pending"}])
        for case in (produced, pending):
            out = fd.draft(self.book, case)
            letter = out["body"].split("\n---\n")[0]
            self.assertEqual(len(out["dropped"]), 1)
            self.assertNotIn("audit exists", letter)
            self.assertNotIn("conducted regularly", letter)

    def test_no_open_asks_left_asks_for_nothing_and_needs_edit(self):
        out = fd.draft(self.book, make(response_type="silence",
                                       prior_asks=[{"text": "Completed audits of ALPR use", "status": "produced"}]))
        letter = out["body"].split("\n---\n")[0]
        self.assertEqual(out["open_asks"], [])
        self.assertNotIn("Please:", letter)
        self.assertIn("Nothing further is asked", letter)
        self.assertEqual(out["status"], "needs edit")
        self.assertIn("no open asks remain; nothing to request, do not send", out["unfilled"])

    def test_policy_section_missing_from_register_needs_edit(self):
        out = fd.draft(self.book, make(policy={"section": "9.9"}), ROWS)
        self.assertEqual(out["status"], "needs edit")
        self.assertIsNone(out["policy"])

    def test_nothing_is_tied_to_one_agency(self):
        rows = [{"agency": "example-pd", "policy_id": "General Order 12", "section": "3(a)", "quote": "Audits happen yearly.",
                 "doc_sha256": "a" * 64, "page": "4", "adopted_date": ""}]
        out = fd.draft(self.book, make(policy={"policy_id": "General Order 12", "section": "3(a)"}), rows)
        self.assertIn("General Order 12 3(a)", out["body"])
        self.assertIn("Example PD", out["body"])

    def test_refuses_unknown_response_type_and_bad_case(self):
        for bad, code in ((make(response_type="applause"), "response_type_unknown"),
                          (make(asks=[]), "case_invalid"),
                          ({"schema": "nope"}, "case_invalid"),
                          (make(prior_asks=[{"text": "x", "status": "maybe"}]), "case_invalid")):
            with self.subTest(code=code), self.assertRaises(pb.PlaybookError) as raised:
                fd.draft(self.book, bad)
            self.assertEqual(raised.exception.code, code)

    def test_every_response_type_maps_to_a_playbook_pattern(self):
        ids = {p["id"] for p in self.book["followup_patterns"]}
        self.assertTrue(set(fd.RESPONSE_TYPES.values()) <= ids)
        self.assertEqual(set(fd.RESPONSE_TYPES.values()), ids)

    def test_input_case_is_not_changed(self):
        c = case("sheriff")
        before = copy.deepcopy(c)
        fd.draft(self.book, c, ROWS)
        self.assertEqual(c, before)


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["records followup-draft", *args]), redirect_stdout(out), redirect_stderr(err):
            code = fd.main()
        return code, out.getvalue(), err.getvalue()

    def test_prints_draft_and_exits_one_while_the_recipient_is_open(self):
        code, out, _ = self.run_cli(str(GOLD / "sheriff.case.json"), "--policies", str(REGISTER))
        self.assertEqual(code, 1)
        self.assertEqual(out, (GOLD / "sheriff.expected.md").read_text(encoding="utf-8"))

    def test_exits_zero_when_every_placeholder_is_resolved(self):
        done = case("sheriff")
        done["recipient"] = "Records Unit"
        path = Path(tempfile.mkdtemp(dir=ROOT / "tests" / "records")) / "done.case.json"
        try:
            path.write_text(json.dumps(done), encoding="utf-8")
            code, out, _ = self.run_cli(str(path), "--policies", str(REGISTER), "--json")
        finally:
            path.unlink()
            path.parent.rmdir()
        self.assertEqual((code, json.loads(out)["status"]), (0, "ready for review"))

    def test_without_register_the_policy_is_a_placeholder_and_exit_is_one(self):
        code, out, _ = self.run_cli(str(GOLD / "grover.case.json"), "--json")
        self.assertEqual(code, 1)  # policy named in the case but no register given
        self.assertEqual(json.loads(out)["status"], "needs edit")

    def test_bad_input_is_refused_with_exit_two(self):
        code, _, err = self.run_cli(str(GOLD / "missing.json"))
        self.assertEqual(code, 2)
        self.assertIn("refused unreadable", err)


if __name__ == "__main__":
    unittest.main()
