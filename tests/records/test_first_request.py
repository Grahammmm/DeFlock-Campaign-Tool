import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from campaign_tool.records import agency_policies, approval_queue, first_request as fr, playbook as pb

SLO_CSV = Path(__file__).resolve().parents[2] / "data" / "slo-pilot" / "agency-policies.csv"
BASIS = "the California Public Records Act"


def make(rows=None, **over):
    book = pb.load()
    rows = agency_policies.load(SLO_CSV)[0] if rows is None else rows
    args = dict(agency="slo-county-sheriff", agency_name="Example Sheriff", start="2026-07-01", end="2026-09-30",
                basis=BASIS, requester="A. Requester")
    args.update(over)
    return fr.draft(book, rows, **args)


class DraftTests(unittest.TestCase):
    def test_exit_ramp_first_then_items_one_to_three_in_tier_order(self):
        body = make()["body"]
        marks = [body.index(s) for s in ("treat this request as complete", "1. A native access/search/audit-log export",
                                         "2. Completed periodic audits", "3. The data-sharing list")]
        self.assertEqual(marks, sorted(marks))
        self.assertIn("since 2026-07-01", body)
        self.assertNotIn("4. ", body)  # lead items only by default
        self.assertNotIn("Contracts", body)

    def test_subject_carries_no_date_window(self):
        for end in ("2026-09-30", None):
            body = make(end=end)["body"]
            self.assertIn("Subject: Public records request for access and audit records\n", body)
            self.assertNotIn("through 2026", body)

    def test_longest_period_fields_and_redaction_pre_offer(self):
        body = make()["body"]
        self.assertIn("with every recorded field, for the longest period held.", body)
        self.assertIn("Plate numbers and personal identifiers of members of the public may be redacted", body)
        self.assertIn("reason or purpose field", body)

    def test_agency_policy_section_quoted_from_register(self):
        result = make()
        self.assertIn("policy section Policy 466, section 466.5(e)", result["body"])
        self.assertIn("Audits of law enforcement requests for data", result["body"])
        self.assertEqual(result["evidence"]["policy_row"]["section"], "466.5(e)")
        other = make(agency="grover-beach-pd")
        self.assertIn("Policy 428, section 428.5(c)", other["body"])

    def test_no_policy_row_marks_needs_edit_with_placeholder(self):
        result = make(agency="agency-without-rows")
        self.assertEqual(result["status"], "needs edit")
        self.assertIn("no agency audit policy row", result["needs_edit"])
        self.assertIn("[EDIT ME: agency policy section]", result["body"])

    def test_access_log_row_alone_is_not_an_audit_policy(self):
        rows = [dict(r, quote="Access and search logs shall be kept for two years.", section="466.9")
                for r in agency_policies.load(SLO_CSV)[0] if r["agency"] == "slo-county-sheriff"][:1]
        self.assertIsNone(fr.audit_policy_row(pb.load(), rows, "slo-county-sheriff"))
        result = make(rows=rows)
        self.assertEqual(result["status"], "needs edit")
        self.assertIn("no agency audit policy row", result["needs_edit"])

    def test_audit_log_retention_is_not_an_audit_policy_and_search_continues(self):
        base = [r for r in agency_policies.load(SLO_CSV)[0] if r["agency"] == "slo-county-sheriff"]
        log_row = dict(base[0], quote="Access and search audit logs shall be retained for two years.", section="466.8")
        book = pb.load()
        self.assertIsNone(fr.audit_policy_row(book, [log_row], "slo-county-sheriff"))
        self.assertIn("no agency audit policy row", make(rows=[log_row])["needs_edit"])
        good = dict(base[0], quote="The unit shall audit searches quarterly and record the results.", section="466.9")
        self.assertEqual(fr.audit_policy_row(book, [log_row, good], "slo-county-sheriff")["section"], "466.9")

    def test_unverified_citations_left_out_and_verified_ones_used(self):
        result = make(all_items=True)
        self.assertNotIn("7922.600", result["body"])
        self.assertIn("Civ. Code 1798.90.52", result["body"])
        self.assertEqual(result["evidence"]["citations_omitted"], [])
        book = pb.load()
        book["citations"]["access_record"]["status"] = "unverified"
        rows = agency_policies.load(SLO_CSV)[0]
        out = fr.draft(book, rows, agency="slo-county-sheriff", agency_name="X", start="2026-07-01", end="2026-07-31", basis=BASIS)
        self.assertNotIn("1798.90.52", out["body"])
        self.assertIn("access_record", out["evidence"]["citations_omitted"])

    def test_all_items_ends_with_contracts_and_no_questions_anywhere(self):
        body = make(all_items=True)["body"]
        self.assertLess(body.index("Hotlist agreements"), body.index("Contracts, order forms"))
        self.assertNotIn("?", body)

    def test_clean_draft_passes_lint_and_is_marked_not_sent(self):
        result = make()
        self.assertEqual((result["lint"]["status"], result["status"], result["needs_edit"]), ("pass", "ready for approval", []))
        self.assertTrue(result["body"].startswith("DRAFT — NOT SENT"))

    def test_banned_phrase_or_missing_requester_marks_needs_edit(self):
        bad = make(basis="the Act, because you failed to answer")
        self.assertEqual(bad["status"], "needs edit")
        self.assertIn("banned phrase", bad["needs_edit"])
        missing = make(requester=None)
        self.assertIn("unfilled placeholder", missing["needs_edit"])


class RefusalTests(unittest.TestCase):
    def refused(self, code, **over):
        with self.assertRaises(fr.FirstRequestError) as ctx:
            make(**over)
        self.assertEqual(ctx.exception.code, code)

    def test_reversed_or_malformed_window_refused_and_long_or_open_window_allowed(self):
        self.refused("window_invalid", start="2026-09-30", end="2026-07-01")
        self.refused("window_invalid", start="July 1", end="2026-07-31")
        self.refused("window_invalid", start="2026-02-30", end="2026-03-01")
        self.refused("window_invalid", start="2026-07-01", end="2026/09/30")
        make(start="2026-01-01", end="2027-12-31")  # no one-quarter cap
        self.assertEqual(make(end=None)["evidence"]["window"], ["2026-07-01", None])

    def test_bad_agency_or_text_fields_refused(self):
        self.refused("agency_invalid", agency="Not A Slug")
        self.refused("field_invalid", agency_name="")
        self.refused("field_invalid", basis="two\nlines")


class QueueAndCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir(mode=0o700)

    def test_queue_adds_pending_draft_once(self):
        result = make()
        row = fr.queue(self.root, result, agency="slo-county-sheriff", request_id="first-1", run_id="r1")
        again = fr.queue(self.root, result, agency="slo-county-sheriff", request_id="first-1", run_id="r2")
        self.assertEqual((row["status"], row["kind"]), ("pending", "first-request"))
        self.assertEqual(row["id"], again["id"])
        self.assertEqual(len(approval_queue.load_queue(self.root)), 1)

    def test_cli_prints_draft_and_status_and_refuses_bad_window(self):
        argv = ["--agency", "slo-county-sheriff", "--agency-name", "Example Sheriff", "--policies", str(SLO_CSV),
                "--window-start", "2026-07-01", "--window-end", "2026-09-30", "--basis", BASIS, "--requester", "A. R."]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fr.main(argv)
        self.assertEqual(code, 0)
        self.assertIn("status: ready for approval", err.getvalue())
        self.assertIn("treat this request as complete", out.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fr.main(argv[:8] + argv[10:])  # --window-end omitted
        self.assertEqual(code, 0)
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = fr.main(argv[:8] + ["--window-end", "2026-06-30"] + argv[10:])
        self.assertEqual(code, 2)
        self.assertIn("window_invalid", err.getvalue())


if __name__ == "__main__":
    unittest.main()
