"""Policy-presence checks only; these do not prove lane test execution."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
POLICY = "docs/RUN-STAGES-PRE-REVIEW.md"


class RunStagesReviewContractTests(unittest.TestCase):
    def policy(self):
        return (ROOT / POLICY).read_text(encoding="utf-8")

    def assert_phrases(self, *phrases):
        text = " ".join(self.policy().split())
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_contributor_link_and_target(self):
        self.assertIn(f"]({POLICY})", (ROOT / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / POLICY).is_file())

    def test_each_lane_tests_exact_composed_candidate_before_review(self):
        self.assert_phrases(
            "Each lane must", "before requesting review", "single-lane candidates",
            "campaign_tool/records/run_stages.py", "stage-contract tests",
            "stacked interacting stage behavior", "isolated-green head is not a substitute",
            "isolated clean worktree", "explicitly admitted prerequisite lane heads",
            "composition order", "git rev-parse HEAD^{tree}",
            "Do not infer sibling admission from an open PR",
        )

    def test_relevant_commands_and_coverage_are_not_replaced_by_examples(self):
        self.assert_phrases(
            "added/changed test modules", "impacted stage-contract consumers",
            "not a substitute for the lane's full relevant set", "working directory",
            "interpreter/dependency availability", "zero failures does not mean full coverage",
            "Known environment skips", "incomplete coverage",
        )

    def test_stops_and_drift_invalidate_receipts(self):
        self.assert_phrases(
            "do not request review as passing", "missing admitted constituent",
            "unresolved conflict", "unrun relevant tests", "test failure",
            "unexplained skip", "head/tree drift", "cancelled or expired test run",
            "Never remove assertions", "weaken expectations", "drop a constituent",
            "Any constituent or candidate change invalidates the prior receipt",
            "recomposition, rerun and fresh review context",
            "Old approvals and duplicate receipts are not transferable",
        )

    def test_receipt_retains_identities_and_results(self):
        self.assert_phrases(*(f"{field}:" for field in (
            "lane", "base_sha", "constituent_heads", "candidate_head", "candidate_tree",
            "commands", "return_codes", "test_counts", "skips", "limitations",
        )))

    def test_offline_limits_and_no_new_authority(self):
        self.assert_phrases(
            "offline", "no shared-main checkout", "no force update", "no implicit rebase",
            "no queue mutation", "no live run", "no send", "no publication",
            "no secret/private-record access", "Synthetic placeholders only",
            "not a new executable gate", "Documentation tests prove policy presence only",
        )


if __name__ == "__main__":
    unittest.main()
