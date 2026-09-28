import copy
import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("gate", Path(__file__).parents[1] / "validate_findings.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ReviewGateTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name).resolve() / "original.txt"
        self.path.write_bytes(b"query row 1: purpose field empty\n")
        self.f = {
            "finding_id": "test-1", "agency": "Synthetic agency",
            "classification": "APPARENT_RULE_CONFLICT", "claim": "Synthetic bounded finding",
            "author_agent": "analyst", "next_action": "Ask for native notes",
            "counterevidence": [], "missing_evidence": ["native notes"],
            "primary_evidence": [{"path": str(self.path), "sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
                                  "locator": "line 1", "coverage": "full_text", "observation": "empty field"}],
            "rules": [], "privacy_status": "cleared", "publication_status": "draft",
        }

    def tearDown(self):
        self.directory.cleanup()

    def reviews(self, finding=None):
        f = finding or self.f
        return [{"finding_id": f["finding_id"], "finding_digest": m.digest(f),
                 "reviewer_agent": "reviewer-a" if role != "legal" else "reviewer-b",
                 "role": role, "verdict": "pass", "reviewed_primary": True,
                 "checked_locators": ["original line 1"], "rationale": "Synthetic source compared",
                 "reviewed_at": "2026-09-22T00:00:00Z"} for role in ("factual", "legal", "privacy")]

    def test_reviewed_apparent_finding_can_pass(self):
        self.assertTrue(m.gate(self.f, self.reviews(), True)["ready"])

    def test_content_change_invalidates_approvals(self):
        reviews = self.reviews()
        self.f["claim"] = "Changed claim"
        self.assertFalse(m.gate(self.f, reviews)["ready"])

    def test_source_bytes_change_blocks(self):
        self.path.write_text("changed")
        self.assertFalse(m.gate(self.f, self.reviews(), True)["ready"])

    def test_author_cannot_review_self(self):
        reviews = self.reviews()
        for r in reviews:
            r["reviewer_agent"] = "analyst"
        self.assertFalse(m.gate(self.f, reviews)["ready"])

    def test_single_reviewer_is_insufficient(self):
        reviews = self.reviews()
        for r in reviews:
            r["reviewer_agent"] = "only-reviewer"
        self.assertFalse(m.gate(self.f, reviews)["ready"])

    def test_challenge_blocks_even_with_passes(self):
        reviews = self.reviews()
        challenge = copy.deepcopy(reviews[0])
        challenge["verdict"] = "challenge"
        self.assertFalse(m.gate(self.f, reviews + [challenge])["ready"])

    def test_confirmed_claim_requires_dates_rules_and_exceptions(self):
        self.f["classification"] = "CONFIRMED_RULE_CONFLICT"
        self.assertFalse(m.gate(self.f, self.reviews())["ready"])

    def test_later_policy_cannot_prove_earlier_breach(self):
        self.f.update(classification="CONFIRMED_RULE_CONFLICT", event_date="2025-03-01",
                      exceptions_checked=True, missing_evidence=[])
        self.f["rules"] = [{"rule_id": "rule-1", "clause": "test section", "source_url": "https://official.example/rule",
                            "applicability": "yes", "version_verified": True,
                            "effective_from": "2026-01-01", "effective_to": None}]
        self.assertFalse(m.gate(self.f, self.reviews())["ready"])

    def test_empty_primary_evidence_blocks(self):
        self.f["primary_evidence"] = []
        self.assertFalse(m.gate(self.f, self.reviews())["ready"])

    def test_withholding_rule_uses_response_date(self):
        self.f.update(classification="CONFIRMED_RULE_CONFLICT", event_date="2020-03-01",
                      exceptions_checked=True, missing_evidence=[])
        self.f["rules"] = [{"rule_id": "disclosure-rule", "clause": "test section", "source_url": "https://official.example/rule",
                            "applicability": "yes", "version_verified": True,
                            "effective_from": "2026-01-01", "effective_to": None,
                            "applicability_event_date": "2026-09-01"}]
        self.assertTrue(m.gate(self.f, self.reviews(), True)["ready"])

    def test_missing_locator_blocks(self):
        self.f["primary_evidence"][0]["locator"] = " "
        self.assertFalse(m.gate(self.f, self.reviews())["ready"])

    def test_author_whitespace_aliases_block(self):
        reviews = self.reviews()
        for i, review in enumerate(reviews):
            review["reviewer_agent"] = "analyst " if i % 2 else " analyst"
        self.assertFalse(m.gate(self.f, reviews, True)["ready"])

    def test_author_case_alias_blocks(self):
        reviews = self.reviews()
        reviews[0]["reviewer_agent"] = "ANALYST"
        self.assertFalse(m.gate(self.f, reviews, True)["ready"])

    def test_reviewer_case_aliases_are_not_distinct(self):
        reviews = self.reviews()
        for i, review in enumerate(reviews):
            review["reviewer_agent"] = "REVIEWER-A" if i % 2 else "reviewer-a"
        self.assertFalse(m.gate(self.f, reviews, True)["ready"])

    def test_checked_locators_require_typed_nonempty_list(self):
        for invalid in (True, "page 1", {}, [], [True], [" "], [{"locator": "page 1"}]):
            with self.subTest(value=invalid):
                reviews = self.reviews()
                reviews[0]["checked_locators"] = invalid
                self.assertFalse(m.gate(self.f, reviews, True)["ready"])

    def test_coverage_requires_known_scope(self):
        for invalid in ("not_read", [], True, ""):
            with self.subTest(value=invalid):
                self.f["primary_evidence"][0]["coverage"] = invalid
                self.assertFalse(m.gate(self.f, self.reviews(), True)["ready"])

    def test_reviewed_at_requires_zoned_timestamp(self):
        for invalid in ("not-a-date", "2026-09-22", "2026-09-22T01:02:03", "2026-02-30T01:02:03Z"):
            with self.subTest(value=invalid):
                reviews = self.reviews()
                reviews[0]["reviewed_at"] = invalid
                self.assertFalse(m.gate(self.f, reviews, True)["ready"])

    def test_artifact_approval_binds_actual_bytes(self):
        artifact = self.path.with_name("public.txt")
        artifact.write_bytes(b"Sanitized publication copy\n")
        self.f["public_artifacts"] = [{"path": str(artifact), "sha256": m.sha256(artifact)}]
        reviews = self.reviews()
        reviews[-1]["checked_public_artifacts"] = copy.deepcopy(self.f["public_artifacts"])
        self.assertTrue(m.gate(self.f, reviews, True)["ready"])
        artifact.write_bytes(b"Changed after privacy approval\n")
        self.assertFalse(m.gate(self.f, reviews, True)["ready"])

    def test_public_artifact_requires_privacy_receipt_binding(self):
        self.f["public_artifacts"] = [{"path": str(self.path), "sha256": m.sha256(self.path)}]
        self.assertFalse(m.gate(self.f, self.reviews(), True)["ready"])

    def test_public_artifact_unavailable_or_malformed_blocks(self):
        for invalid in (True, [True], [{"path": "/unavailable/public.txt", "sha256": "a" * 64}]):
            with self.subTest(value=invalid):
                self.f["public_artifacts"] = invalid
                self.assertFalse(m.gate(self.f, self.reviews(), True)["ready"])

    def test_unverified_source_cannot_be_ready(self):
        self.assertFalse(m.gate(self.f, self.reviews(), False)["ready"])

    def test_symlink_ancestor_blocks(self):
        alias = self.path.parent / "alias"
        alias.symlink_to(self.path.parent, target_is_directory=True)
        self.f["primary_evidence"][0]["path"] = str(alias / self.path.name)
        self.assertFalse(m.gate(self.f, self.reviews(), True)["ready"])

    def test_bad_classification_or_role_blocks_without_crashing(self):
        self.f["classification"] = []
        self.assertFalse(m.gate(self.f, self.reviews(), True)["ready"])
        self.f["classification"] = "APPARENT_RULE_CONFLICT"
        reviews = self.reviews()
        reviews[0]["role"] = []
        self.assertFalse(m.gate(self.f, reviews, True)["ready"])


if __name__ == "__main__":
    unittest.main()
