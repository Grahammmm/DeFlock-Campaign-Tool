"""REC-AUTO-9: gap and possible-misuse proposals (acceptance behaviours only)."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from campaign_tool.law import load_package
from campaign_tool.legal_claims import legal_claims
from campaign_tool.records import run_stages
from campaign_tool.records.extraction_ledger import ExtractionLedgerAdapter
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import gap_proposals as gp
from campaign_tool.records.outcomes import received_items as ri
from tests.records import test_outcome_factsheets as _fs

FactSheetsTests = _fs.FactSheetsTests
del _fs

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
PACKAGE = load_package("us-ca")
SHA = "a" * 64


def item(n, kind="records", agency="ag1", request="r1", findings=(), deadlines=(), received_at="2026-10-20T00:00:00Z",
         unreviewed=False, basis=("1", "x")):
    return {"item_id": f"{n:064x}", "original_sha256": f"{n:064x}", "agency_id": agency, "request_id": request, "kind": kind,
            "kind_basis": {"page": 1, "quote": basis[1]}, "findings": list(findings), "deadlines": list(deadlines),
            "received_at": received_at, "needs_visual_review_unresolved": unreviewed}


def finding(rating, field="retention", policy=True, quote="Data is kept 5 years."):
    return {"rating": rating, "rule_ref": {"label": "Civ. Code § 1798.90.51(b)"}, "page": 1, "quote": quote,
            "policy_ref": {"document_sha256": SHA, "page": 1, "quote": quote} if policy else None,
            "comparison_ref": {"field": field}, "comparison_reason": "reason"}


def run(items, pages=None, requests=(), scorer=None):
    received = {"schema": ri.SCHEMA, "run_id": "r", "items": items}
    text = {i["original_sha256"]: [{"page": 1, "text": (pages or {}).get(i["item_id"], "")}] for i in items}
    return gp.propose(received, text, PACKAGE, run_id="r", requests=requests, now=NOW, scorer=scorer)


def flat(report):
    return [p for a in report["agencies"] for r in a["requests"] for p in r["proposals"]]


class PureTests(unittest.TestCase):
    def test_gap_confidence_levels_and_not_proposed(self):
        report = run([item(1, findings=[finding("gap"), finding("meets"), finding("unknown"),
                                        finding("gap", "audit_process", policy=False, quote="No log.")]),
                      item(2, findings=[finding("gap", "custodian")], unreviewed=True)])
        by_quote = {(p["item_id"][-1], p["quote"]): p for p in flat(report)}
        self.assertEqual(by_quote[("1", "Data is kept 5 years.")]["confidence"], "high")
        self.assertEqual(by_quote[("1", "No log.")]["confidence"], "medium")
        self.assertIn("policy_section_unavailable", by_quote[("1", "No log.")]["flags"])
        unreviewed = by_quote[("2", "Data is kept 5 years.")]
        self.assertEqual((unreviewed["confidence"], "text_unreviewed" in unreviewed["flags"]), ("medium", True))
        self.assertEqual(report["counts"]["not_proposed"], 2)
        self.assertEqual(report["counts"]["policy_gap"], 3)

    def test_low_never_goes_below_low(self):
        report = run([item(1, unreviewed=True)], {f"{1:064x}": "Data goes to the FBI."})
        self.assertEqual(flat(report)[0]["confidence"], "low")
        self.assertIn("text_unreviewed", flat(report)[0]["flags"])

    def test_sharing_lead_levels_citation_question_and_kinds(self):
        pages = {f"{1:064x}": "The FBI may query the data.", f"{2:064x}": "Audit log of each search.\nShared with ICE.",
                 f"{3:064x}": "Civil Code 1798.90.55(b) prohibits sharing with federal agencies",
                 f"{4:064x}": "We deny your request. FBI."}
        report = run([item(1), item(2), item(3), item(4, kind="denial", basis=("1", "We deny your request. FBI."))], pages)
        leads = {p["item_id"][-1]: [x for x in flat(report) if x["item_id"] == p["item_id"] and x["type"] == "misuse_lead"]
                 for p in flat(report)}
        self.assertEqual({x["confidence"] for x in leads["1"]}, {"low"})
        self.assertEqual({x["detector_or_field"]: x["confidence"] for x in leads["2"]}, {"sharing": "medium", "immigration": "medium"})
        self.assertEqual(leads["3"][0]["confidence"], "low")
        self.assertTrue(leads["3"][0]["question"].endswith("?"))
        self.assertNotIn("4", {p["item_id"][-1] for p in flat(report) if p["type"] == "misuse_lead"})
        sharing = next(x for x in leads["2"] if x["detector_or_field"] == "sharing")
        immigration = next(x for x in leads["2"] if x["detector_or_field"] == "immigration")
        self.assertIn("1798.90.55(b)", sharing["rule_ref"]["label"])
        self.assertIn("7284.6", immigration["rule_ref"]["label"])
        self.assertTrue(all(p["legal_conclusion"] is False and p["review_required"] for p in flat(report)))

    def test_policy_section_only_from_same_agency_sharing_finding(self):
        own = item(1, agency="ag1", findings=[finding("meets", "sharing_limits", quote="Sharing is limited.")])
        other = item(2, agency="ag2", request="r2")
        texts = {own["item_id"]: "Sent to the FBI.", other["item_id"]: "Sent to the FBI."}
        report = run([own, other, item(3, agency="ag1")], texts)
        leads = {p["agency_id"]: p for p in flat(report) if p["type"] == "misuse_lead"}
        self.assertEqual(leads["ag2"]["policy_section"], None)
        self.assertIn("policy_section_unavailable", leads["ag2"]["flags"])
        self.assertEqual(leads["ag1"]["policy_section"]["quote"], "Sharing is limited.")

    def test_gaps_deadline_denial_fee_and_missing_fields(self):
        past = {"date": "2026-11-02", "date_text": "November 02, 2026", "page": 1, "quote": "We will respond by November 02, 2026.",
                "year_inferred": False}
        ext = item(1, kind="extension", deadlines=[past])
        answered = item(2, kind="extension", agency="ag2", request="r2", deadlines=[past])
        later = item(3, kind="records", agency="ag2", request="r2", received_at="2026-11-03T00:00:00Z")
        denial = item(4, kind="denial", basis=("1", "We deny your request."))
        cited = item(5, kind="denial", agency="ag3", request="r3", basis=("1", "We deny your request."))
        fee = item(6, kind="fee", agency="ag4", request="r4")
        norequest = item(7, kind="response", agency="ag5", request=None, basis=("1", "We have received your request."))
        pages = {denial["item_id"]: "We deny your request.", cited["item_id"]: "We deny your request under Gov. Code 7927.705.",
                 fee["item_id"]: "The fee is $40 for staff time."}
        report = run([ext, answered, later, denial, cited, fee, norequest], pages,
                     requests=[{"id": "r9", "agency_id": "ag6", "determination_due_at": "2026-10-30", "determination_at": None},
                               {"id": "r2", "agency_id": "ag2", "determination_due_at": "2026-10-30", "determination_at": None}])
        got = {(p["type"], p["agency_id"]) for p in flat(report)}
        self.assertIn(("promised_not_received", "ag1"), got)
        self.assertNotIn(("promised_not_received", "ag2"), got)
        self.assertIn(("exemption_without_citation", "ag1"), got)
        self.assertNotIn(("exemption_without_citation", "ag3"), got)
        self.assertIn(("fee_review", "ag4"), got)
        self.assertIn(("missing_field", "ag5"), got)
        self.assertIn(("deadline_passed_no_answer", "ag6"), got)
        self.assertNotIn(("deadline_passed_no_answer", "ag2"), got)
        ledger = next(p for p in flat(report) if p["type"] == "deadline_passed_no_answer")
        self.assertEqual((ledger["evidence_kind"], ledger["item_id"]), ("ledger_field", None))

    def test_later_extension_with_a_later_date_supersedes_the_passed_date(self):
        old = {"date": "2026-11-02", "date_text": "November 2", "page": 1, "quote": "reply by November 2", "year_inferred": False}
        new = dict(old, date="2026-11-30", date_text="November 30", quote="reply by November 30")
        report = run([item(1, kind="extension", deadlines=[old]),
                      item(2, kind="extension", deadlines=[new], received_at="2026-11-04T00:00:00Z")])
        self.assertEqual([p for p in flat(report) if p["type"] == "promised_not_received"], [])

    def test_unattributed_items_do_not_resolve_a_passed_date(self):
        old = {"date": "2026-11-02", "date_text": "November 2", "page": 1, "quote": "reply by November 2", "year_inferred": False}
        new = dict(old, date="2026-11-30", date_text="November 30", quote="reply by November 30")
        report = run([item(1, kind="extension", request=None, deadlines=[old]),
                      item(2, kind="extension", request=None, deadlines=[new], received_at="2026-11-04T00:00:00Z"),
                      item(3, kind="records", request=None, received_at="2026-11-04T00:00:00Z")])
        found = [p for p in flat(report) if p["type"] == "promised_not_received"]
        self.assertTrue(found)
        self.assertTrue(all(p["request_id"] is None for p in found))

    def test_future_stated_date_makes_no_proposal(self):
        future = {"date": "2026-12-01", "date_text": "December 1, 2026", "page": 1, "quote": "by December 1, 2026", "year_inferred": False}
        self.assertEqual(flat(run([item(1, kind="extension", deadlines=[future])])), [])

    def test_null_agency_unlinked_and_null_request_sorted_last(self):
        report = run([item(1, agency=None, findings=[finding("gap")]), item(2, request=None, findings=[finding("gap")]),
                      item(3, request="r1", findings=[finding("gap", "custodian")])])
        self.assertEqual(report["unlinked_items"], 1)
        self.assertEqual([r["request_id"] for r in report["agencies"][0]["requests"]], ["r1", None])
        self.assertEqual(report["agencies"][0]["requests"][1]["scope"], "agency_wide")

    def test_proposal_id_stable_and_changes_with_quote(self):
        a = flat(run([item(1, findings=[finding("gap")])]))[0]["proposal_id"]
        self.assertEqual(a, flat(run([item(1, findings=[finding("gap")])]))[0]["proposal_id"])
        self.assertNotEqual(a, flat(run([item(1, findings=[finding("gap", quote="Other.")])]))[0]["proposal_id"])

    def test_every_question_template_passes_the_claim_guard_and_guard_is_live(self):
        for template in gp.QUESTIONS.values():
            self.assertEqual(legal_claims(template.format(page=3)), [], template)
        self.assertNotEqual(legal_claims("The agency violated the law"), [])

    def test_quotes_are_exact_page_text(self):
        text = "Header\nThe FBI may query the data.\nFooter"
        got = [p for p in flat(run([item(1)], {f"{1:064x}": text})) if p["type"] == "misuse_lead"]
        self.assertTrue(got and all(p["quote"] in text for p in got))

    def test_quote_or_page_missing_is_omitted_and_recorded(self):
        report = run([item(1, findings=[finding("gap", quote="")])])
        self.assertEqual((flat(report), report["diagnostics"][0]["reason"]), ([], "quote_or_page_missing"))


class ScorerTests(unittest.TestCase):
    def setUp(self):
        self.items = [item(1, findings=[finding("gap")])]

    def test_no_scorer_logs_deterministic_probability(self):
        report = run(self.items)
        entry = report["model_log"][0]
        self.assertEqual((entry["source"], entry["p_yes"]), ("deterministic", 0.8))
        self.assertEqual(flat(report)[0]["review_bucket"], "proposal")

    def test_model_no_moves_to_review_list_and_logs_logprobs_but_keeps_rule_confidence(self):
        report = run(self.items, scorer=lambda q, quote: {"answer": "no", "p_yes": 0.1, "logprobs": [("no", -0.1)]})
        proposal = flat(report)[0]
        self.assertEqual((proposal["confidence"], proposal["review_bucket"]), ("high", "review_list"))
        self.assertIn("model_answered_no", proposal["review_reasons"])
        self.assertEqual(report["review_list"], [proposal["proposal_id"]])
        self.assertEqual(report["model_log"][0]["logprobs"], [["no", -0.1]])

    def test_model_failure_and_garbage_fall_back(self):
        def boom(q, quote):
            raise RuntimeError("down")
        for scorer in (boom, lambda q, quote: {"answer": "maybe", "p_yes": 0.5}, lambda q, quote: {"answer": "yes", "p_yes": 7}):
            self.assertEqual(run(self.items, scorer=scorer)["model_log"][0]["source"], "deterministic")

    def test_scorer_refuses_non_loopback(self):
        with self.assertRaises(ValueError):
            gp.h2o_scorer("http://example.com:8091")
        gp.h2o_scorer("http://127.0.0.1:8091")

    def test_h2o_scorer_reads_noul_probability_from_systemone(self):
        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        seen = {}

        def fake(request, timeout):
            seen["url"], seen["body"] = request.full_url, json.loads(request.data)
            return Reply(json.dumps({"answers": {"q": {"type": "noul", "noul": 0.9}}}).encode())
        with patch("urllib.request.urlopen", fake):
            got = gp.h2o_scorer("http://127.0.0.1:8091")("Q?", "quote text")
        self.assertEqual(seen["url"], "http://127.0.0.1:8091/v1/systemone")
        self.assertEqual(seen["body"]["state"], {"excerpt": "quote text"})
        self.assertEqual((got["answer"], got["p_yes"]), ("yes", 0.9))
        self.assertEqual([t for t, _ in got["logprobs"]], ["yes", "no"])


class StageTests(unittest.TestCase):
    private, document, enroll, tree = (FactSheetsTests.private, FactSheetsTests.document,
                                       FactSheetsTests.enroll, FactSheetsTests.tree)

    def setUp(self):
        workspaces = Path(tempfile.gettempdir()) / "gap-proposals" / "test-workspaces"
        workspaces.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="gap-", dir=workspaces)
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.processing = self.project / "processing-v1"
        self.processing.mkdir(mode=0o700)
        self.database = self.processing / "ledger.sqlite"
        store.initialize(self.database)
        self.evidence = self.processing / "artifacts" / "evidence"
        self.evidence.parent.mkdir(mode=0o700)
        self.adapter = ExtractionLedgerAdapter(database=self.database, evidence_root=self.evidence)
        self.serial = 0
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO runs VALUES('fixture','fixture','2041-01-01',NULL,'fixture',NULL,NULL,NULL,'fixture','{}')")
            for name in ("caltrans", "atascadero", "other"):
                con.execute("INSERT INTO agencies VALUES(?,?,NULL,NULL)", (name, name.title()))
            con.commit()
        portal = self.project / "portal-queue-v1"
        portal.mkdir(mode=0o700)
        with contextlib.closing(sqlite3.connect(portal / "portal.sqlite")) as con, con:
            con.execute("CREATE TABLE portal_items(id TEXT)")
        self.out = self.project / "run-outputs" / "test-run" / "outcomes"

    def stage(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = run_stages.main(["outputs", "--project", str(self.project), "--run-id", "test-run"])
        return code, json.loads(stdout.getvalue())

    def test_stage_writes_after_received_items_private_replay_noop_and_conflict(self):
        self.document(["Caltrans District 5\nWe are extending the response period. We will respond by October 30, 2026."], "caltrans")
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network")):
            code, _ = self.stage()
        self.assertEqual(code, 0)
        path = self.out / "gap-proposals.json"
        report = json.loads(path.read_text())
        self.assertEqual((report["schema"], report["publishable"], report["legal_conclusion"]), (gp.SCHEMA, False, False))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        self.assertEqual(report["unlinked_items"] + sum(len(a["requests"]) for a in report["agencies"]) > 0, True)
        inode = path.stat().st_ino
        self.assertEqual(self.stage()[0], 0)
        self.assertEqual(path.stat().st_ino, inode)
        with self.assertRaises(OSError) as ctx:
            path.write_bytes(path.read_bytes() + b" ")
            gp.write_run_gap_proposals(self.project, "test-run", now=NOW)
        self.assertEqual(str(ctx.exception), "gap_proposals_run_already_exists")

    def test_io_refusals_without_a_write(self):
        self.assertEqual(gp.write_run_gap_proposals(self.project, "test-run")["skipped"], "inputs_missing")
        self.assertFalse((self.out / "gap-proposals.json").exists())
        with self.assertRaises(OSError):
            gp.write_run_gap_proposals(self.project, "../evil")
        self.out.mkdir(parents=True)
        (self.out / "received-items.json").write_text("{" + " " * 8 + "}")
        with self.assertRaises(OSError):  # wrong schema
            gp.write_run_gap_proposals(self.project, "test-run")
        with patch.object(gp, "MAX_INPUT_BYTES", 4), self.assertRaises(OSError):
            gp.write_run_gap_proposals(self.project, "test-run")
        link = self.project / "run-outputs" / "linked"
        link.symlink_to(self.out.parent)
        with self.assertRaises(OSError):
            gp.write_run_gap_proposals(self.project, "linked")

    def test_seam_failure_keeps_earlier_outputs_and_timeout_propagates(self):
        self.document(["We acknowledge receipt of your request."], "other")
        with patch.object(gp, "write_run_gap_proposals", side_effect=RuntimeError("private")):
            code, result = self.stage()
        self.assertEqual(code, 0)
        self.assertIn("OUTPUTS_GAP_PROPOSALS_UNAVAILABLE", json.dumps(result))
        self.assertTrue((self.out / "received-items.json").exists())
        self.assertFalse((self.out / "gap-proposals.json").exists())
        with patch.object(gp, "write_run_gap_proposals", side_effect=run_stages.StageTimeout()):
            with self.assertRaises(run_stages.StageTimeout):
                run_stages.stage_outputs(argparse.Namespace(project=str(self.project), run_id="test-run"))


del FactSheetsTests  # helpers were copied into StageTests; do not collect the producer tests here


class NeverPublishedTests(unittest.TestCase):
    def test_nothing_outside_records_imports_gap_proposals(self):
        root = Path(__file__).resolve().parents[2] / "campaign_tool"
        offenders = [str(p) for p in root.rglob("*.py") if "records" not in p.relative_to(root).parts[:1]
                     and "gap_proposals" in p.read_text(encoding="utf-8", errors="ignore")]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
