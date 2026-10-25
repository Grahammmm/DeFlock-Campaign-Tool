"""Regression coverage for detached law summary reviewer metadata."""

import copy
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fc16_law_summary_snapshot" / "cases.json"


class LawSummarySnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.package = law.load_package("us-ca")
        cls.package["reviewed_by"] = list(cls.fixture["reviewer_values"])
        cls.package["status"] = "reviewed"
        cls.package["reviewed_at"] = "2026-01-02"

    def test_two_way_mutation_isolation(self):
        package = copy.deepcopy(self.package)
        summary = law.summary(package)
        original = list(package["reviewed_by"])
        package["reviewed_by"].append("caller-added")
        self.assertEqual(summary["reviewed_by"], original)
        package["reviewed_by"][0] = "caller-replaced"
        self.assertEqual(summary["reviewed_by"], original)
        package["reviewed_by"].pop()
        self.assertEqual(summary["reviewed_by"], original)
        summary["reviewed_by"].append("summary-added")
        self.assertEqual(package["reviewed_by"], ["caller-replaced", *original[1:]])
        summary["reviewed_by"][0] = "summary-replaced"
        self.assertEqual(package["reviewed_by"][0], "caller-replaced")
        summary["reviewed_by"].pop()
        self.assertEqual(package["reviewed_by"], ["caller-replaced", *original[1:]])

    def test_repeated_summaries_are_equal_but_independent(self):
        package = copy.deepcopy(self.package)
        first = law.summary(package)
        second = law.summary(package)
        self.assertEqual(first, second)
        self.assertIsNot(first["reviewed_by"], second["reviewed_by"])
        first["reviewed_by"].append("first-only")
        self.assertEqual(second["reviewed_by"], self.fixture["reviewer_values"])
        self.assertEqual(law.summary(package)["reviewed_by"], self.fixture["reviewer_values"])

    def test_empty_and_order_preserve_exact_values(self):
        package = copy.deepcopy(self.package)
        package["reviewed_by"] = list(self.fixture["empty_reviewers"])
        result = law.summary(package)
        self.assertEqual(result["reviewed_by"], [])
        self.assertEqual(package["reviewed_by"], [])
        package["reviewed_by"] = list(self.fixture["reviewer_values"])
        result = law.summary(package)
        self.assertEqual(result["reviewed_by"], self.fixture["reviewer_values"])
        self.assertEqual(result["reviewed_at"], "2026-01-02")
        self.assertEqual(result["status"], "reviewed")

    def test_projection_preserves_all_other_summary_fields(self):
        package = copy.deepcopy(self.package)
        actual = law.summary(package)
        expected = law.summary(package)
        expected["reviewed_by"] = list(package["reviewed_by"])
        self.assertEqual(set(actual), set(expected))
        self.assertEqual({key: value for key, value in actual.items() if key != "reviewed_by"},
                         {key: value for key, value in expected.items() if key != "reviewed_by"})
        self.assertEqual(actual["reviewed_by"], package["reviewed_by"])
        self.assertEqual(actual["review_counts"]["verified"] + actual["review_counts"]["likely"] +
                         actual["review_counts"]["needs_attorney_review"], actual["rule_count"])
        self.assertEqual(actual["request_scopes"], [scope["scope_id"] for scope in package["request_scopes"]])
        self.assertEqual(actual["note"], law.HOLIDAY_NOTE)
        self.assertEqual(actual["status"], self.fixture["expected_status"])
        self.assertEqual(actual["reviewed_at"], self.fixture["expected_reviewed_at"] if
                         self.fixture["expected_status"] == "draft" else package["reviewed_at"])

    def test_baseline_mutation_control_detects_direct_reference(self):
        package = copy.deepcopy(self.package)
        aliased = law.summary(package)
        equality_control = copy.deepcopy(aliased)
        self.assertEqual(aliased, equality_control)
        package["reviewed_by"].append("control-caller-change")
        self.assertEqual(aliased["reviewed_by"], self.fixture["reviewer_values"])

    def test_isolated_direct_reference_mutant_is_detected(self):
        package = copy.deepcopy(self.package)
        isolated_summary = law.summary(package)
        isolated_summary["reviewed_by"] = package["reviewed_by"]
        package["reviewed_by"].append("mutant-change")
        self.assertNotEqual(isolated_summary["reviewed_by"], self.fixture["reviewer_values"])


if __name__ == "__main__":
    unittest.main()
