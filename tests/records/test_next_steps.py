"""REC-AUTO-10: recommended next steps (social post, site-update diff, follow-up probe) queued as pending drafts."""
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from campaign_tool import social
from campaign_tool.records import approval_queue as aq
from campaign_tool.records import next_steps as ns

RUN = "run_20260101T060000Z_0a1b2c3d"


def pid(n):
    return f"{n:064x}"


def proposal(n, *, agency="ca-caltrans", request="req-1", type="policy_gap", field="retention", confidence="high",
             quote="Camera logs are not enclosed.", page=2):
    return {"proposal_id": pid(n), "agency_id": agency, "request_id": request, "type": type, "detector_or_field": field,
            "confidence": confidence, "item_id": pid(100 + n), "page": page, "quote": quote,
            "rule_ref": {"label": "Gov. Code § 7922.535"}, "policy_section": None}


def doc(*groups, run_id=RUN):
    """groups: (agency_id, request_id, [proposals])"""
    agencies = {}
    for agency, request, props in groups:
        agencies.setdefault(agency, []).append({"request_id": request, "proposals": props})
    return {"schema": ns.SCHEMA, "run_id": run_id, "agencies": [{"agency_id": a, "requests": r} for a, r in agencies.items()]}


AGENCIES = [{"agency_id": "ca-caltrans", "name": "Caltrans", "profile": {"status": "Records requested", "summary": "s"}},
            {"agency_id": "ca-other", "name": "Other Agency"}]


class Base(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.tmp = tempfile.TemporaryDirectory(prefix="nextsteps-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.campaign = self.root / "campaign"
        self.outcomes = self.project / "run-outputs" / RUN / "outcomes"
        self.outcomes.mkdir(parents=True, mode=0o700)
        (self.campaign / "content").mkdir(parents=True)
        self.agencies_path = self.campaign / "content" / "agencies.json"
        self.agencies_path.write_text(json.dumps(AGENCIES, indent=2) + "\n", encoding="utf-8")
        self.write_proposals(doc(("ca-caltrans", "req-1", [proposal(1), proposal(2, type="misuse_lead", field="sharing",
                                                                         confidence="medium", quote='Shared with "partners".'),
                                                          proposal(3, type="misuse_lead", confidence="low")]),
                                 ("ca-other", "req-9", [proposal(4, agency="ca-other", request="req-9", confidence="low")])))
        (self.outcomes / "received-items.json").write_text(json.dumps({"items": [
            {"agency_id": "ca-caltrans", "kind": "records", "received_at": "2026-11-02T18:59:00Z"},
            {"agency_id": "ca-caltrans", "kind": "records", "received_at": "2026-10-01T00:00:00Z"}]}), encoding="utf-8")

    def write_proposals(self, document):
        (self.outcomes / "gap-proposals.json").write_text(json.dumps(document), encoding="utf-8")

    def run_ns(self, **kw):
        return ns.queue_run_next_steps(self.project, RUN, campaign_root=self.campaign, **kw)

    def rows(self):
        return {r["kind"]: r for r in aq.load_queue(self.project)}

    def journal(self):
        path = self.project / "approvals" / "queue.jsonl"
        return path.read_bytes() if path.exists() else b""


class GeneratorTests(Base):
    def test_three_drafts_for_eligible_agency_then_idempotent(self):
        report = self.run_ns()
        self.assertEqual((report["queued"], report["skipped_low"], report["diagnostics"]), (3, 2, []))
        rows = self.rows()
        self.assertEqual(set(rows), set(ns.KINDS))
        self.assertTrue(all(r["status"] == "pending" and r["agency_id"] == "ca-caltrans" for r in rows.values()))
        before = self.journal()
        self.assertEqual(self.run_ns()["queued"], 0)
        self.assertEqual(self.journal(), before)

    def test_no_receipts_or_denial_only_agency_never_claims_records_arrived(self):
        for items in ([], [{"agency_id": "ca-caltrans", "kind": "denial", "received_at": "2026-11-02T18:59:00Z"},
                           {"agency_id": "ca-caltrans", "kind": "extension", "received_at": "2026-11-03T00:00:00Z"}]):
            (self.outcomes / "received-items.json").write_text(json.dumps({"items": items}), encoding="utf-8")
            result = ns.recommend(json.loads((self.outcomes / "gap-proposals.json").read_text()), AGENCIES,
                                  run_id=RUN, received={"items": items})
            social_drafts = [d for d in result["drafts"] if d["kind"] == ns.SOCIAL]
            self.assertEqual(len(social_drafts), 1)
            self.assertNotIn("received records", social_drafts[0]["body"])
            self.assertIn("following up", social_drafts[0]["body"])
            self.assertNotIn("records arrived", social_drafts[0]["evidence"]["why"])

    def test_ledger_only_no_answer_probe_has_neutral_wording_and_ledger_citation(self):
        ledger = {"proposal_id": pid(7), "agency_id": "ca-caltrans", "request_id": "req-1",
                  "type": "deadline_passed_no_answer", "detector_or_field": "determination_due_at",
                  "confidence": "medium", "item_id": None, "page": None,
                  "quote": "determination_due_at=2026-10-01; no determination recorded",
                  "rule_ref": {"label": "Gov. Code § 7922.535"}, "policy_section": None}
        result = ns.recommend(doc(("ca-caltrans", "req-1", [ledger])), AGENCIES, run_id=RUN, received={"items": []})
        probes = [d for d in result["drafts"] if d["kind"] == ns.PROBE]
        self.assertEqual(len(probes), 1)
        body = probes[0]["body"]
        self.assertNotIn("Thank you for the records provided", body)
        self.assertNotIn("on page None", body)
        self.assertIn("Our request ledger shows", body)
        self.assertIn("determination_due_at=2026-10-01", body)

    def test_human_edit_and_rejection_survive_a_rerun(self):
        self.run_ns()
        rows = self.rows()
        aq.edit(self.project, rows[ns.SOCIAL]["id"], "op", "edited by a person")
        aq.reject(self.project, rows[ns.PROBE]["id"], "op", "not now")
        before = self.journal()
        self.run_ns()
        self.assertEqual(self.journal(), before)
        self.assertEqual(self.rows()[ns.SOCIAL]["body"], "edited by a person")
        self.assertEqual(self.rows()[ns.PROBE]["status"], "rejected")

    def test_social_is_short_and_has_no_quote_name_of_person_or_contact(self):
        self.run_ns()
        text = self.rows()[ns.SOCIAL]["body"]
        self.assertLessEqual(social.post_length(text, "x"), 280)
        self.assertIn("Caltrans", text)
        self.assertIn(pid(101)[:12], text)  # the source record id
        self.assertNotIn("Camera logs", text)

    def test_long_label_list_is_shortened_to_fit(self):
        props = [proposal(n, field="label_number_%02d_with_a_long_name" % n) for n in range(1, 30)]
        self.write_proposals(doc(("ca-caltrans", "req-1", props)))
        self.run_ns()
        text = self.rows()[ns.SOCIAL]["body"]
        self.assertLessEqual(social.post_length(text, "x"), 280)
        self.assertIn("29 questions", text)

    def test_privacy_and_legal_claim_refusals_name_category_not_text(self):
        agencies = [{"agency_id": "ca-caltrans", "name": "Caltrans jdoe@example.org"}]
        self.agencies_path.write_text(json.dumps(agencies), encoding="utf-8")
        report = self.run_ns()
        self.assertIn({"code": "social_privacy_refused", "agency_id": "ca-caltrans", "categories": ["email"]}, report["diagnostics"])
        self.assertNotIn("jdoe", json.dumps(report))
        self.assertNotIn(ns.SOCIAL, self.rows())
        self.agencies_path.write_text(json.dumps(AGENCIES), encoding="utf-8")
        self.write_proposals(doc(("ca-caltrans", "req-1", [proposal(1, field="agency violated the law")])))
        report = self.run_ns()
        self.assertIn("social_legal_claim_flagged", [d["code"] for d in report["diagnostics"]])
        self.assertNotIn(ns.SOCIAL, self.rows())

    def test_agency_absent_from_content(self):
        self.write_proposals(doc(("ca-ghost", "req-1", [proposal(1, agency="ca-ghost")])))
        report = self.run_ns()
        self.assertEqual([d["code"] for d in report["diagnostics"]], ["agency_not_in_content"])
        self.assertEqual(self.rows().get(ns.SOCIAL), None)

    def test_site_diff_changes_only_status_and_never_writes_content(self):
        path = self.agencies_path
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        self.run_ns()
        diff = self.rows()[ns.SITE]["body"]
        changed = [l for l in diff.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        self.assertEqual(changed, ['-      "status": "Records requested",',
                                   '+      "status": "Records received 2026-11-02; under review",'])
        self.assertTrue(diff.startswith("--- content/agencies.json\n+++ content/agencies.json (proposed)\n"))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (before, mtime))
        self.assertEqual([p.name for p in self.campaign.iterdir()], ["content"])

    def test_site_status_already_current_or_content_invalid(self):
        agencies = [dict(AGENCIES[0], profile={"status": "Records received 2026-11-02; under review"})]
        self.agencies_path.write_text(json.dumps(agencies, indent=2), encoding="utf-8")
        self.run_ns()
        self.assertNotIn(ns.SITE, self.rows())
        self.agencies_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.run_ns(), {"queued": 0, "skipped": "content_invalid"})

    def test_site_update_invalid_when_proposed_document_fails_validation(self):
        real = ns.content.load_agencies
        calls = []

        def second_call_fails(*args, **kw):
            calls.append(1)
            if len(calls) > 1:
                raise ns.content.ContentError("x")
            return real(*args, **kw)
        with mock.patch.object(ns.content, "load_agencies", side_effect=second_call_fails):
            report = self.run_ns()
        self.assertIn("site_update_invalid", [d["code"] for d in report["diagnostics"]])
        self.assertNotIn(ns.SITE, self.rows())

    def test_probe_has_numbered_exact_quotes_and_labels_agency_wide(self):
        self.write_proposals(doc(("ca-caltrans", None, [proposal(1, request=None), proposal(2, request=None, type="misuse_lead",
                                                                                          field="sharing", quote="Shared with partners.")])))
        self.run_ns()
        row = self.rows()[ns.PROBE]
        body = row["body"]
        self.assertEqual(row["request_id"], "agency-wide")
        self.assertIn("(agency-wide)", body)
        self.assertIn('1. Please provide', body)
        self.assertIn('2. Please provide', body)
        self.assertIn('on page 2: "Camera logs are not enclosed."', body)
        self.assertIn('"Shared with partners."', body)
        self.assertIn("Gov. Code § 7922.535", body)
        self.assertEqual([p["proposal_id"] for p in row["evidence"]["proposals"]], [pid(1), pid(2)])
        self.assertEqual(ns.legal_claims.legal_claims(body), [])

    def test_probe_with_accusatory_wording_is_not_queued(self):
        self.write_proposals(doc(("ca-caltrans", "req-1", [proposal(1, field="agency violated the law")])))
        report = self.run_ns()
        self.assertIn("probe_legal_claim_flagged", [d["code"] for d in report["diagnostics"]])
        self.assertNotIn(ns.PROBE, self.rows())

    def test_dry_run_writes_nothing_and_missing_or_stale_inputs_are_skips(self):
        report = self.run_ns(dry_run=True)
        self.assertEqual(len(report["drafts"]), 3)
        self.assertFalse((self.project / "approvals").exists())
        self.write_proposals(doc(run_id="other_run"))
        self.assertEqual(self.run_ns(), {"queued": 0, "skipped": "proposals_stale"})
        (self.outcomes / "gap-proposals.json").unlink()
        self.assertEqual(self.run_ns(), {"queued": 0, "skipped": "proposals_missing"})
        out = StringIO()
        with redirect_stdout(out):
            code = ns.main(["--project", str(self.project), "--run-id", RUN, "--campaign-root", str(self.campaign)])
        self.assertEqual((code, json.loads(out.getvalue())), (0, {"queued": 0, "skipped": "proposals_missing"}))

    def test_queue_error_exits_3_with_code_only(self):
        err, out = StringIO(), StringIO()
        with mock.patch.object(ns.approval_queue, "add_draft", side_effect=aq.ApprovalQueueError("queue_io")), \
                redirect_stderr(err), redirect_stdout(out):
            code = ns.main(["--project", str(self.project), "--run-id", RUN, "--campaign-root", str(self.campaign)])
        self.assertEqual((code, err.getvalue().strip(), out.getvalue()), (3, "queue_io", ""))

    def test_no_network_and_nothing_outside_the_queue(self):
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")):
            self.run_ns()
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["campaign", "project"])
        self.assertEqual(sorted(p.name for p in self.campaign.iterdir()), ["content"])
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), ["approvals", "run-outputs"])


CITE = "Gov. Code § 7922.535"
LEDGER = {"proposal_id": pid(7), "agency_id": "ca-caltrans", "request_id": "req-1", "type": "deadline_passed_no_answer",
          "detector_or_field": "determination_due_at", "confidence": "medium", "item_id": None, "page": None,
          "quote": "determination_due_at=2026-10-01; no determination recorded", "rule_ref": {"label": CITE}}
PINNED = {  # one pinned ask per proposal type: the record, the window (misuse leads) and the clause the answer would test
    "policy_gap": (proposal(1), "Please provide the current ALPR usage and privacy policy section governing retention, and any "
                                "records showing how it was applied. This would show whether the material received on retention "
                                f"meets the policy section or comparison point cited ({CITE})."),
    "misuse_lead/sharing": (proposal(2, type="misuse_lead", field="sharing"),
                            "Please provide the ALPR data-sharing agreements in effect and the audit log entries for searches made "
                            "by or on behalf of agencies outside California for the period covered by the record on page 2. This "
                            f"would show whether the sharing described on page 2 is consistent with {CITE}."),
    "misuse_lead/immigration": (proposal(3, type="misuse_lead", field="immigration"),
                                "Please provide the audit log entries and any records of access to or sharing of ALPR data for "
                                "immigration-related purposes for the period covered by the record on page 2. This would show "
                                f"whether the access or sharing described on page 2 is consistent with {CITE}."),
    "promised_not_received": (proposal(4, type="promised_not_received", field="stated_date"),
                              "Please provide the further response or records the agency stated on page 2 it would provide. This "
                              "would show whether the response promised on page 2 has been provided within the time the cited "
                              f"section allows ({CITE})."),
    "deadline_passed_no_answer": (LEDGER, "Please provide the agency's determination on this request, or the date it will issue "
                                          "one. This would show whether a determination was issued within the time the cited "
                                          f"section allows ({CITE})."),
    "exemption_without_citation": (proposal(5, type="exemption_without_citation", field=None),
                                   "Please provide the provision or exemption the agency relies on for the denial on page 2. This "
                                   "would show whether the denial on page 2 names the provision it relies on, as the cited "
                                   f"section contemplates ({CITE})."),
    "fee_review": (proposal(6, type="fee_review", field=None),
                   "Please provide the basis and an itemization of the fee stated on page 2. This would show whether the fee "
                   f"on page 2 is limited to the direct cost of duplication ({CITE})."),
    "missing_field/request_id": (proposal(8, type="missing_field", field="request_id"),
                                 "Please provide confirmation of which request the item on page 2 answers. This would show "
                                 "whether the item on page 2 can be matched to the request it answers under the cited section "
                                 f"({CITE})."),
    "missing_field/received_at": (proposal(9, type="missing_field", field="received_at"),
                                  "Please provide the date the item on page 2 was received. This would show whether the receipt "
                                  "date of the item on page 2 can be set against the response time the cited section allows "
                                  f"({CITE})."),
}


class ProbeWordingTests(Base):
    def test_one_pinned_ask_per_proposal_type_and_it_passes_the_guard(self):
        for name, (p, expected) in PINNED.items():
            with self.subTest(name):
                self.assertEqual(ns._ask(p), expected)
                self.assertNotIn("None", expected)
                for bad in ("()", "[]", "held by .", " for .", "  ", " ,"):
                    self.assertNotIn(bad, expected)
                self.assertEqual(ns.legal_claims.legal_claims(expected), [])

    def test_probe_body_numbers_each_ask_and_keeps_the_quote_sentence(self):
        result = ns.recommend(doc(("ca-caltrans", "req-1", [PINNED["fee_review"][0], PINNED["misuse_lead/sharing"][0]])),
                              AGENCIES, run_id=RUN, received={"items": []})
        body = next(d["body"] for d in result["drafts"] if d["kind"] == ns.PROBE)
        quote = 'The record states, on page 2: "Camera logs are not enclosed."'
        self.assertIn(f"1. {PINNED['fee_review'][1]} {quote}", body)
        self.assertIn(f"2. {PINNED['misuse_lead/sharing'][1]} {quote}", body)

    def test_custodian_and_dates_are_named_only_when_the_proposal_carries_them(self):
        p = dict(PINNED["misuse_lead/sharing"][0], custodian="the Records Unit", date_from="2025-01-01", date_to="2025-06-30")
        self.assertIn("agencies outside California held by the Records Unit for 2025-01-01 to 2025-06-30. This would show whether",
                      ns._ask(p))
        only_unit = dict(PINNED["fee_review"][0], custodian="the Records Unit")
        self.assertIn("the fee stated on page 2 held by the Records Unit. This would show", ns._ask(only_unit))
        bad = dict(p, custodian='x "y"', date_from="yesterday", date_to=None)  # unusable values are left out, not guessed
        self.assertNotIn("held by", ns._ask(bad))
        self.assertIn("California for the period covered by the record on page 2.", ns._ask(bad))
        cited = dict(PINNED["fee_review"][0], policy_section={"page": 4})
        self.assertIn(f"({CITE}; the agency's policy, page 4).", ns._ask(cited))

    def test_flagged_custodian_or_claim_still_drops_the_probe(self):
        for p in (dict(PINNED["fee_review"][0], custodian="the unit that violated the law"),
                  proposal(1, field="agency violated the law")):
            result = ns.recommend(doc(("ca-caltrans", "req-1", [p])), AGENCIES, run_id=RUN, received={"items": []})
            self.assertEqual([d["kind"] for d in result["drafts"] if d["kind"] == ns.PROBE], [])
            self.assertIn("probe_legal_claim_flagged", [d["code"] for d in result["diagnostics"]])

    def test_low_confidence_stays_skipped_and_item_id_is_unchanged(self):
        report = self.run_ns(dry_run=True)
        self.assertEqual(report["skipped_low"], 2)
        probe = next(d for d in report["drafts"] if d["kind"] == ns.PROBE)
        self.assertEqual(probe["item_id"], "8699d1b68f36fc6d6896caf72e78f35420a54c2f48a53dd657af9809a13fe446")  # value before this change


if __name__ == "__main__":
    unittest.main()
