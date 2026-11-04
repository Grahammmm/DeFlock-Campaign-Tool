"""DF-APPROVAL-BIND: finding review receipts are bound to the campaign that issued them.

Security regression test: receipts approved in campaign A and copied into campaign B's private/reviews/
must not approve the same finding in B. Synthetic fixtures only (no records, no network).
Run from the repository root: python3 -B -m unittest tests.test_findings_campaign_binding -v
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from campaign_tool import findings
from tests.approval_signing import Signer

NS = "deflock-finding-review"
REPO_ROOT = Path(__file__).resolve().parents[1]
APPROVALS = (("factual", "Synthetic Reviewer One"), ("legal", "Synthetic Reviewer Two"),
             ("privacy", "Synthetic Reviewer Two"))


def synthetic_draft():
    return {"id": "synthetic-finding", "author": "Synthetic Author", "classification": "documented_fact",
            "summary": "Synthetic record summary", "sources": [{"locator": "Synthetic page 1", "sha256": "a" * 64}],
            "limitations": "Synthetic only", "counterevidence": "Synthetic only"}


class CampaignBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-campaign-binding-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.a, self.b = self.base / "A", self.base / "B"
        for root in (self.a, self.b):
            root.mkdir()
            (root / "campaign.json").write_text(json.dumps(
                {"schema_version": 1, "name": root.name, "county": "Synthetic", "state": "CA"}), encoding="utf-8")
        self.draft = synthetic_draft()

    def approve_all(self, root):
        for role, reviewer in APPROVALS:
            findings.add_receipt(root, self.draft, role, "approve", reviewer, "Synthetic review")

    def receipts(self, root):
        return root / "private" / "reviews" / "synthetic-finding.json"

    def copy_receipts(self, source, destination):
        target = self.receipts(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.receipts(source).read_bytes())

    def write_legacy(self, root, decisions=("approve", "approve", "approve")):
        """Receipts exactly as main wrote them before binding: no campaign_id."""
        digest = findings.reviewed_hash(self.draft)
        legacy = [{"content_sha256": digest, "role": role, "decision": decision, "reviewer": reviewer,
                   "rationale": "Synthetic review", "reviewed_at": "2026-01-02T03:04:05Z"}
                  for (role, reviewer), decision in zip(APPROVALS, decisions)]
        self.receipts(root).parent.mkdir(parents=True, exist_ok=True)
        self.receipts(root).write_text(json.dumps(legacy), encoding="utf-8")
        return legacy

    def cli(self, *arguments, returncode=0):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    # Positive control: this is the original probe. On main before the fix, B's blockers were [] here.
    def test_receipts_copied_from_campaign_a_are_refused_in_campaign_b(self):
        self.approve_all(self.a)
        self.assertEqual(findings.review_status(self.a, self.draft)["review_blockers"], [])
        self.copy_receipts(self.a, self.b)
        status_b = findings.review_status(self.b, self.draft)
        self.assertIn("foreign_campaign_receipt", status_b["review_blockers"])
        for role in ("factual", "legal", "privacy"):
            self.assertIn(f"missing_{role}_review", status_b["review_blockers"])
        self.assertEqual(status_b["foreign_campaign_receipts"], 3)
        draft_path = self.b / "private" / "drafts" / "synthetic-finding.json"
        draft_path.parent.mkdir(parents=True)
        draft_path.write_text(json.dumps(self.draft), encoding="utf-8")
        (row,) = findings.status_rows(self.b)
        self.assertNotEqual(row["state"], "ready")
        self.assertIn("foreign_campaign_receipt", row["blockers"])
        with self.assertRaisesRegex(ValueError, "foreign_campaign_receipt"):
            findings.publish(self.b, draft_path, "Synthetic Approver")

    def test_signing_payloads_differ_between_campaigns(self):
        findings.add_receipt(self.a, self.draft, "factual", "approve", "Synthetic Reviewer One", "Synthetic review")
        findings.add_receipt(self.b, self.draft, "factual", "approve", "Synthetic Reviewer One", "Synthetic review")
        args = (self.draft, "factual", "approve", "Synthetic Reviewer One", "Synthetic review")
        payload_a, payload_b = findings.review_payload(self.a, *args), findings.review_payload(self.b, *args)
        self.assertNotEqual(payload_a, payload_b)
        self.assertEqual(json.loads(payload_a)["campaign_id"], findings.campaign_id(self.a))

    def test_review_payload_needs_a_bound_campaign_and_writes_nothing(self):
        with self.assertRaisesRegex(ValueError, "bind-receipts"):
            findings.review_payload(self.a, self.draft, "factual", "approve", "Synthetic Reviewer One", "Synthetic")
        self.assertFalse((self.a / "private").exists())

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen (OpenSSH 8.2+) is required")
    def test_a_signature_made_for_campaign_a_does_not_verify_in_campaign_b(self):
        """Same reviewer key on both rosters: a signed A receipt relabelled with B's id fails verification in B,
        and an A signature cannot be submitted to B's `finding review --signature` either."""
        signer = Signer("synthetic")
        self.addCleanup(signer.close)
        for root in (self.a, self.b):
            findings.add_reviewer(root, "Synthetic Reviewer One", ["factual"], ssh_key=str(signer.dir / "key.pub"))
        args = (self.draft, "factual", "approve", "Synthetic Reviewer One", "Synthetic review")
        signature = self.base / "a.sig"
        signature.write_text(signer.sign(findings.review_payload(self.a, *args), NS), encoding="utf-8")
        findings.add_receipt(self.a, *args, signature=str(signature))
        self.assertEqual(findings.review_status(self.a, self.draft)["signed"], 1)
        with self.assertRaisesRegex(ValueError, "bad signature"):
            findings.add_receipt(self.b, *args, signature=str(signature))
        (signed,) = json.loads(self.receipts(self.a).read_text(encoding="utf-8"))
        relabelled = dict(signed, campaign_id=findings.campaign_id(self.b))
        self.receipts(self.b).parent.mkdir(parents=True, exist_ok=True)
        self.receipts(self.b).write_text(json.dumps([relabelled]), encoding="utf-8")
        status = findings.review_status(self.b, self.draft)
        self.assertEqual((status["signed"], len(status["bad_signatures"])), (0, 1))

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen (OpenSSH 8.2+) is required")
    def test_a_legacy_signed_receipt_still_verifies_after_binding(self):
        signer = Signer("synthetic")
        self.addCleanup(signer.close)
        findings.add_reviewer(self.b, "Synthetic Reviewer One", ["factual"], ssh_key=str(signer.dir / "key.pub"))
        digest = findings.reviewed_hash(self.draft)
        legacy_payload = findings.receipt_payload("synthetic-finding", digest, "factual", "approve",
                                                  "Synthetic Reviewer One", "Synthetic review")  # pre-binding bytes
        legacy = {"content_sha256": digest, "role": "factual", "decision": "approve",
                  "reviewer": "Synthetic Reviewer One", "rationale": "Synthetic review",
                  "reviewed_at": "2026-01-02T03:04:05Z", "signature": signer.sign(legacy_payload, NS),
                  "signed_by": "Synthetic_Reviewer_One"}
        self.receipts(self.b).parent.mkdir(parents=True, exist_ok=True)
        self.receipts(self.b).write_text(json.dumps([legacy]), encoding="utf-8")
        self.assertEqual(findings.review_status(self.b, self.draft)["signed"], 0)  # unbound: not counted here
        findings.bind_receipts(self.b, confirm=True)
        status = findings.review_status(self.b, self.draft)
        self.assertEqual((status["signed"], status["bad_signatures"]), (1, []))

    # Negative control: B's own approvals still approve B.
    def test_campaign_b_receipts_are_accepted_in_campaign_b(self):
        self.approve_all(self.a)
        self.approve_all(self.b)
        self.assertEqual(findings.review_status(self.b, self.draft)["review_blockers"], [])
        self.assertEqual(findings.review_status(self.a, self.draft)["review_blockers"], [])
        stored = json.loads(self.receipts(self.b).read_text(encoding="utf-8"))
        self.assertEqual({r["campaign_id"] for r in stored}, {findings.campaign_id(self.b)})
        self.assertNotEqual(findings.campaign_id(self.a), findings.campaign_id(self.b))

    def test_copied_receipts_beside_valid_ones_are_never_silently_accepted(self):
        self.approve_all(self.b)
        copied = json.loads(self.receipts(self.b).read_text(encoding="utf-8"))
        self.approve_all(self.a)
        copied += json.loads(self.receipts(self.a).read_text(encoding="utf-8"))
        self.receipts(self.b).write_text(json.dumps(copied), encoding="utf-8")
        self.assertEqual(findings.review_status(self.b, self.draft)["review_blockers"], ["foreign_campaign_receipt"])

    # Legacy policy: unbound receipts are refused (loudly) until the organizer binds them with bind-receipts.
    def test_legacy_unbound_receipts_are_reported_not_silently_accepted_or_dropped(self):
        self.write_legacy(self.b)
        status = findings.review_status(self.b, self.draft)
        self.assertIn("unbound_legacy_receipt", status["review_blockers"])
        self.assertEqual(status["unbound_receipts"], 3)
        self.assertEqual(status["receipts_for_this_content"], 3)  # still listed, not dropped

    def test_bind_receipts_dry_run_writes_nothing_and_confirm_binds_once(self):
        legacy = self.write_legacy(self.b)
        before = self.receipts(self.b).read_bytes()
        plan = findings.bind_receipts(self.b)
        self.assertEqual(plan["confirmed"], False)
        self.assertEqual(plan["findings"], [{"id": "synthetic-finding", "unbound": 3, "foreign": 0}])
        self.assertEqual(self.receipts(self.b).read_bytes(), before)
        self.assertFalse((self.b / "private" / "campaign-id.json").exists())
        done = findings.bind_receipts(self.b, confirm=True)
        self.assertEqual(done["bound"], 3)
        stored = json.loads(self.receipts(self.b).read_text(encoding="utf-8"))
        self.assertEqual([{k: v for k, v in r.items() if k not in ("campaign_id", "campaign_bound")} for r in stored],
                         legacy)  # every original field kept
        self.assertTrue(all(r["campaign_id"] == findings.campaign_id(self.b)
                            and r["campaign_bound"]["by"] == "bind-receipts" for r in stored))
        self.assertEqual(findings.review_status(self.b, self.draft)["review_blockers"], [])
        log = [json.loads(line) for line in
               (self.b / "private" / "reviews" / "binding-log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(e["id"], e["bound"], len(e["receipt_sha256"])) for e in log], [("synthetic-finding", 3, 3)])
        self.assertEqual(findings.bind_receipts(self.b, confirm=True)["bound"], 0)  # idempotent

    def test_bind_receipts_never_stamps_another_campaigns_receipts(self):
        self.approve_all(self.a)
        self.copy_receipts(self.a, self.b)
        self.assertEqual(findings.bind_receipts(self.b, confirm=True)["bound"], 0)
        self.assertIn("foreign_campaign_receipt", findings.review_status(self.b, self.draft)["review_blockers"])

    def test_legacy_challenges_still_block_before_and_after_binding(self):
        self.write_legacy(self.b, decisions=("approve", "reject", "approve"))
        self.assertIn("unresolved_challenge", findings.review_status(self.b, self.draft)["review_blockers"])
        findings.bind_receipts(self.b, confirm=True)
        self.assertIn("unresolved_challenge", findings.review_status(self.b, self.draft)["review_blockers"])

    def test_a_bad_campaign_id_file_refuses(self):
        (self.b / "private").mkdir()
        (self.b / "private" / "campaign-id.json").write_text('{"schema_version": 1, "campaign_id": "x"}',
                                                            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "campaign-id.json"):
            findings.review_status(self.b, self.draft)

    def test_bind_receipts_cli_dry_run_then_confirm(self):
        root = self.base / "campaign"
        self.cli("init", "--directory", root, "--name", "Synthetic", "--county", "Synthetic County", "--state", "ca")
        self.write_legacy(root)
        dry = self.cli("finding", "bind-receipts", "--directory", root)
        self.assertIn("Nothing was changed", dry.stdout)
        self.assertEqual(json.loads(dry.stdout.splitlines()[0])["confirmed"], False)
        done = self.cli("finding", "bind-receipts", "--directory", root, "--confirm")
        self.assertEqual(json.loads(done.stdout.splitlines()[0])["bound"], 3)


if __name__ == "__main__":
    unittest.main()
