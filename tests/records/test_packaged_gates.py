"""Synthetic integration checks for reusable, offline gate entry points."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from campaign_tool.records.gates.collect_reviews import extract_receipts
from campaign_tool.records.gates.reconcile_coverage import reconcile


class PackagedGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sha = hashlib.sha256(b"synthetic document").hexdigest()

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "campaign_tool.records", *map(str, args)],
                              capture_output=True, text=True)

    def test_all_commands_have_help(self):
        for cmd in ("validate-findings", "reconcile-coverage", "collect-reviews", "batch-report"):
            with self.subTest(command=cmd):
                result = self.cli(cmd, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_empty_findings_are_not_ready(self):
        path = self.root / "findings.json"
        path.write_text("[]")
        result = self.cli("validate-findings", path, "--require-ready")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["ready"], 0)

    def test_duplicate_finding_ids_block(self):
        path = self.root / "findings.json"
        path.write_text(json.dumps([{"finding_id": "duplicate"}] * 2))
        result = self.cli("validate-findings", path, "--require-ready")
        self.assertEqual(result.returncode, 1)
        for finding in json.loads(result.stdout)["findings"]:
            self.assertIn("duplicate finding_id", finding["blockers"])

    def test_output_is_private_under_permissive_parent_umask(self):
        path = self.root / "findings.json"
        path.write_text("[]")
        output = self.root / "report.json"
        old = os.umask(0o022)
        try:
            result = self.cli("validate-findings", path, "--output", output)
        finally:
            os.umask(old)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_reconcile_keeps_extraction_separate_from_review(self):
        summary, details = reconcile([{"sha256": self.sha, "stage": "complete"}], [])
        self.assertEqual(summary["intake_unique_hashes"], 1)
        self.assertEqual(summary["coverage_states"], {"no_analyst_digest": 1})
        self.assertEqual(details[0]["independent_legal_or_publication_approval"],
                         "not_established_by_this_report")

    def test_reconcile_rejects_duplicate_inventory(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            reconcile([{"sha256": self.sha}] * 2, [])

    def test_reconcile_preserves_prior_full_partial_and_conflicts(self):
        inventory = [{"sha256": str(i) * 64} for i in range(1, 4)]
        digests = [{"sha256": str(i) * 64, "review_status": status,
                    "coverage": "page 1", "key_points": ["synthetic"]}
                   for i, status in ((1, "full"), (2, "partial"), (3, "full"), (3, "partial"))]
        summary, _ = reconcile(inventory, digests)
        self.assertEqual(summary["coverage_states"], {
            "author_declared_full": 1, "author_declared_partial": 1,
            "conflicting_coverage_declarations": 1})

    def test_reconcile_cli_uses_configured_root(self):
        (self.root / "intake").mkdir()
        (self.root / "intake/all-agency-document-queue.jsonl").write_text(
            json.dumps({"sha256": self.sha}) + "\n")
        result = self.cli("reconcile-coverage", self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["intake_unique_hashes"], 1)
        self.assertTrue((self.root / "DOCUMENT-REVIEW-QUEUE.jsonl").exists())

    def test_collection_does_not_invent_approvals(self):
        receipts, errors = extract_receipts({"reviewer": "someone", "approved": True})
        self.assertEqual(receipts, [])
        self.assertTrue(errors)

    def test_canonical_receipt_is_preserved_without_promotion(self):
        receipt = dict(finding_id="example", finding_digest="a" * 64,
                       reviewer_agent="independent", role="factual", verdict="blocked",
                       rationale="Missing source", reviewed_at="2026-01-01T00:00:00Z")
        receipts, errors = extract_receipts([receipt])
        self.assertEqual(receipts, [receipt])
        self.assertEqual(errors, [])

    def test_collect_cli_records_missing_canonical_review(self):
        agency = self.root / "agencies/synthetic"
        agency.mkdir(parents=True)
        (agency / "findings.json").write_text("[]")
        (agency / "factual-review.json").write_text('{"approved": true}')
        result = self.cli("collect-reviews", self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads((agency / "independent-reviews.json").read_text())
        self.assertEqual(data["reviews"], [])
        self.assertEqual(len(data["collection_errors"]), 1)
        self.assertEqual(len(data["receipt_sources"]), 1)

    def test_batch_expected_agencies_are_configuration_not_pilot_constants(self):
        output = self.root / "report.json"
        result = self.cli("batch-report", self.root, "--output", output,
                          "--require-agency", "synthetic-north", "--require-agency", "synthetic-south")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text())["missing_agency_packages"],
                         ["synthetic-north", "synthetic-south"])

    def test_batch_has_no_implied_campaign(self):
        output = self.root / "report.json"
        result = self.cli("batch-report", self.root, "--output", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text())["missing_agency_packages"], [])


if __name__ == "__main__":
    unittest.main()
