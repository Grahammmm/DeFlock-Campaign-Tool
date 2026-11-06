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
            {"agency_id": "ca-caltrans", "received_at": "2026-11-02T18:59:00Z"},
            {"agency_id": "ca-caltrans", "received_at": "2026-10-01T00:00:00Z"}]}), encoding="utf-8")

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


if __name__ == "__main__":
    unittest.main()
