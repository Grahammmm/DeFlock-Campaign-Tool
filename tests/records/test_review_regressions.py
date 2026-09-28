"""Synthetic, positive-baseline regressions for independent review findings."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records.gates import validate_findings as gate
from campaign_tool.records.gates import safe_output
from campaign_tool.records.gates.tests import test_gate as legacy
from tools.check_public_tree import violations


class SpecificGateRegressions(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.ReviewGateTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.finding = self.fixture.f
        self.reviews = self.fixture.reviews()
        self.assertTrue(gate.gate(self.finding, self.reviews, True)["ready"])

    def blocked(self, message, refresh=False):
        reviews = self.fixture.reviews() if refresh else self.reviews
        result = gate.gate(self.finding, reviews, True)
        self.assertFalse(result["ready"])
        self.assertIn(message, result["blockers"])
        self.assertNotIn("source byte verification required for readiness", result["blockers"])

    def test_stale_receipt_specific_blocker(self):
        self.finding["claim"] = "Changed synthetic statement"
        self.blocked("stale or mismatched review")

    def test_self_review_specific_blocker(self):
        self.reviews[0]["reviewer_agent"] = "analyst"
        self.blocked("review must be independent of author")

    def test_single_reviewer_specific_blocker(self):
        for review in self.reviews:
            review["reviewer_agent"] = "one-reviewer"
        self.blocked("at least two distinct independent reviewers required")

    def test_challenge_specific_blocker(self):
        extra = copy.deepcopy(self.reviews[0])
        extra["verdict"] = "challenge"
        self.reviews.append(extra)
        self.blocked("unresolved reviewer challenge or block")

    def test_confirmed_rule_requirements_specific_blockers(self):
        self.finding["classification"] = "CONFIRMED_RULE_CONFLICT"
        self.blocked("confirmed conflict requires applicable rules", refresh=True)
        self.blocked("exceptions not checked", refresh=True)
        self.blocked("confirmed conflict still lists unresolved evidence", refresh=True)

    def test_event_date_specific_blocker(self):
        self.finding.update(classification="CONFIRMED_RULE_CONFLICT", event_date="2025-03-01",
                            exceptions_checked=True, missing_evidence=[])
        self.finding["rules"] = [dict(rule_id="example", clause="synthetic section",
            source_url="https://example.invalid/rule", applicability="yes", version_verified=True,
            effective_from="2025-01-01", effective_to=None)]
        self.assertTrue(gate.gate(self.finding, self.fixture.reviews(), True)["ready"])
        self.finding["rules"][0]["effective_from"] = "2026-01-01"
        self.blocked("rule does not cover event date", refresh=True)

    def test_missing_evidence_specific_blocker(self):
        self.finding["primary_evidence"] = []
        self.blocked("primary evidence required", refresh=True)

    def test_missing_locator_specific_blocker(self):
        self.finding["primary_evidence"][0]["locator"] = " "
        self.blocked("evidence[0] missing locator", refresh=True)

    def test_duplicate_ready_findings_are_blocked_across_agencies(self):
        root = self.fixture.path.parent
        for name in ("north", "south"):
            agency = root / "agencies" / name
            agency.mkdir(parents=True)
            (agency / "document-digests.jsonl").write_text("")
            (agency / "findings.json").write_text(json.dumps([self.finding]))
            (agency / "independent-reviews.json").write_text(json.dumps(self.reviews))
        output = root / "report.json"
        result = subprocess.run([sys.executable, "-m", "campaign_tool.records", "batch-report",
                                 str(root), "--check-files", "--output", str(output)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["publication_ready"], 0)
        for agency in report["agencies"]:
            self.assertEqual(agency["publication_ready"], 0)
            self.assertIn("duplicate finding_id", agency["gates"][0]["blockers"])

    def test_duplicate_ready_findings_in_one_collection(self):
        reports = gate.gate_many([self.finding, self.finding], self.reviews, True)
        self.assertEqual([r["ready"] for r in reports], [False, False])
        self.assertTrue(all("duplicate finding_id" in r["blockers"] for r in reports))


class PrivateReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_replaces_permissive_file_without_mutating_hardlink(self):
        target = self.root / "report.json"
        target.write_text("old")
        target.chmod(0o644)
        alias = self.root / "other.json"
        os.link(target, alias)
        safe_output.write_private(target, "new")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.read_text(), "new")
        self.assertEqual(alias.read_text(), "old")
        self.assertEqual(alias.stat().st_mode & 0o777, 0o644)

    def test_symlink_target_rejected_and_untouched(self):
        original = self.root / "original"
        original.write_text("unchanged")
        link = self.root / "report"
        link.symlink_to(original)
        with self.assertRaises(ValueError):
            safe_output.write_private(link, "bad")
        self.assertEqual(original.read_text(), "unchanged")
        self.assertTrue(link.is_symlink())

    def test_symlink_ancestor_rejected_even_when_creating_parents(self):
        link = self.root / "alias"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            safe_output.write_private(link / "new" / "report", "bad", create_parents=True)
        self.assertFalse((self.root / "new").exists())

    def test_temporary_collision_does_not_touch_existing_file(self):
        collision = self.root / ".records-report-collision"
        collision.write_text("untouched")
        with patch.object(safe_output.secrets, "token_hex", side_effect=["collision", "free"]):
            safe_output.write_private(self.root / "report", "new")
        self.assertEqual(collision.read_text(), "untouched")
        self.assertEqual((self.root / "report").read_text(), "new")

    def test_failed_replace_preserves_target_and_cleans_temporary(self):
        target = self.root / "report"
        target.write_text("old")
        with patch.object(safe_output.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                safe_output.write_private(target, "new")
        self.assertEqual(target.read_text(), "old")
        self.assertEqual(list(self.root.glob(".records-report-*")), [])

    def test_private_parent_creation(self):
        target = self.root / "new" / "nested" / "report"
        safe_output.write_private(target, "new", create_parents=True)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)

    def test_parent_traversal_rejected(self):
        with self.assertRaises(ValueError):
            safe_output.write_private(self.root / "missing" / ".." / "report", "bad")

    def test_all_output_commands_replace_existing_mode(self):
        (self.root / "intake").mkdir()
        (self.root / "intake/all-agency-document-queue.jsonl").write_text("")
        agency = self.root / "agencies/synthetic"
        agency.mkdir(parents=True)
        finding_file = agency / "findings.json"
        finding_file.write_text("[]")
        report = self.root / "report.json"
        receipt = agency / "independent-reviews.json"
        queue = self.root / "DOCUMENT-REVIEW-QUEUE.jsonl"
        calls = [(["validate-findings", finding_file, "--output", report], [report]),
                 (["batch-report", self.root, "--output", report], [report]),
                 (["reconcile-coverage", self.root, "--output", report], [report, queue]),
                 (["collect-reviews", self.root], [receipt])]
        for args, outputs in calls:
            with self.subTest(command=args[0]):
                for output in outputs:
                    output.write_text("old")
                    output.chmod(0o644)
                result = subprocess.run([sys.executable, "-m", "campaign_tool.records", *map(str, args)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                for output in outputs:
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)


class CredentialRegressions(unittest.TestCase):
    def test_compact_json_and_punctuation(self):
        for key in ("api_key", "password", "client_secret", "access_token"):
            value = "synthetic-" + "!#$%^&*()[]{}:+?" * 2
            payload = json.dumps({"nested": {key: value}}, separators=(",", ":")).encode()
            hits = violations("fixture.json", payload)
            self.assertEqual(hits, [("fixture.json", 1, "literal_credential")])
            self.assertNotIn(value, repr(hits))

    def test_python_assignment_punctuated_value(self):
        payload = b"pass" + b"word = '" + b"synthetic!@#$%^&*()" * 2 + b"'"
        self.assertEqual(violations("fixture.py", payload)[0][2], "literal_credential")


if __name__ == "__main__":
    unittest.main()
