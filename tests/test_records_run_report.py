"""Run report v1 schema, synthetic example and cross-field validator."""

import copy
import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.records import run_report  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "run-report" / "records-run-report-v1.synthetic.json"


def _example():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


class RunReportSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = run_report.load_schema()
        self.report = _example()

    def assertInvalid(self, report, fragment):
        with self.assertRaises(ValueError) as caught:
            run_report.validate_run_report(report, self.schema)
        self.assertIn(fragment, str(caught.exception))

    def test_schema_top_level(self):
        self.assertEqual(self.schema["type"], "object")
        self.assertIs(self.schema["additionalProperties"], False)
        self.assertEqual(self.schema["properties"]["schema_version"], {"const": 1})
        self.assertEqual(tuple(self.schema["$defs"]["stage_name"]["enum"]), run_report.STAGE_ORDER)

    def test_synthetic_example_validates(self):
        self.assertIs(run_report.validate_run_report(self.report, self.schema), self.report)

    def test_example_is_metadata_only(self):
        text = EXAMPLE.read_text(encoding="utf-8")
        for pattern in (r"https?://", r"@", r"[\\/]", r"\.(pdf|docx?|xlsx?|msg|eml|png|jpe?g)\b"):
            self.assertIsNone(re.search(pattern, text, re.I), pattern)

    def test_every_object_rejects_extra_fields(self):
        cases = [
            lambda r: r.__setitem__("note", "free text"),
            lambda r: r["stages"][0].__setitem__("message", "x"),
            lambda r: r["stages"][0]["counts"].__setitem__("bytes", 1),
            lambda r: r["new_data"].__setitem__("filenames", []),
            lambda r: r["needs_g"][0].__setitem__("url", "x"),
        ]
        for mutate in cases:
            report = copy.deepcopy(self.report)
            mutate(report)
            self.assertInvalid(report, "unexpected field")

    def test_free_text_rejected_in_codes(self):
        report = copy.deepcopy(self.report)
        report["needs_g"][0]["code"] = "see scan_0001.pdf"
        self.assertInvalid(report, "pattern")
        report = copy.deepcopy(self.report)
        report["stages"][3]["error_code"] = "Timeout at https://portal.invalid/x"
        self.assertInvalid(report, "pattern")

    def test_shape_errors(self):
        cases = [
            (lambda r: r.__setitem__("schema_version", 2), "constant"),
            (lambda r: r.__setitem__("run_id", "run-1"), "pattern"),
            (lambda r: r.__setitem__("started_at", "2026-01-01 06:00"), "pattern"),
            (lambda r: r["stages"][0].__setitem__("status", "done"), "not one of"),
            (lambda r: r["stages"][0]["counts"].__setitem__("input", -1), "minimum"),
            (lambda r: r["new_data"].__setitem__("pages", 1.5), "expected type"),
            (lambda r: r.__setitem__("notify_failed", "no"), "expected type"),
            (lambda r: r["needs_g"][0].__setitem__("count", 0), "minimum"),
            (lambda r: r["needs_g"][0].pop("severity"), "missing required"),
            (lambda r: r.__setitem__("stages", r["stages"][:7]), "minItems"),
        ]
        for mutate, fragment in cases:
            with self.subTest(fragment=fragment):
                report = copy.deepcopy(self.report)
                mutate(report)
                self.assertInvalid(report, fragment)

    def test_stage_order_is_exact(self):
        report = copy.deepcopy(self.report)
        report["stages"][0], report["stages"][1] = report["stages"][1], report["stages"][0]
        self.assertInvalid(report, "stage order")
        report = copy.deepcopy(self.report)
        report["stages"].append(copy.deepcopy(report["stages"][-1]))
        self.assertInvalid(report, "stage order")

    def test_timing_rules(self):
        report = copy.deepcopy(self.report)
        report["finished_at"] = "2026-01-01T05:59:59Z"
        self.assertInvalid(report, "earlier than started_at")
        report = copy.deepcopy(self.report)
        report["duration_seconds"] = 10
        self.assertInvalid(report, "disagrees")
        report = copy.deepcopy(self.report)
        report["stages"][0]["duration_seconds"] = None
        self.assertInvalid(report, "skipped")

    def test_skipped_stage_may_have_null_duration(self):
        report = copy.deepcopy(self.report)
        report["stages"][1].update(status="skipped", duration_seconds=None)
        run_report.validate_run_report(report, self.schema)

    def test_error_code_follows_status(self):
        report = copy.deepcopy(self.report)
        report["stages"][3]["error_code"] = None
        self.assertInvalid(report, "required when status is warn")
        report = copy.deepcopy(self.report)
        report["stages"][0]["error_code"] = "SYNC_SLOW"
        self.assertInvalid(report, "must be null")

    def test_overall_status_rolls_up(self):
        report = copy.deepcopy(self.report)
        report["overall_status"] = "ok"
        self.assertInvalid(report, "overall_status")
        report["stages"][4].update(status="fail", error_code="EXTRACT_TIMEOUT")
        report["overall_status"] = "warn"
        self.assertInvalid(report, "expected 'fail'")
        report["overall_status"] = "fail"
        run_report.validate_run_report(report, self.schema)

    def test_notify_failed_matches_notify_stage(self):
        report = copy.deepcopy(self.report)
        report["notify_failed"] = True
        self.assertInvalid(report, "notify_failed")
        report["stages"][7].update(status="fail", error_code="NOTIFY_SEND_FAILED")
        report["overall_status"] = "fail"
        run_report.validate_run_report(report, self.schema)
        report["notify_failed"] = False
        self.assertInvalid(report, "must be true")

    def test_needs_g_duplicates_rejected(self):
        report = copy.deepcopy(self.report)
        report["needs_g"].append(copy.deepcopy(report["needs_g"][0]))
        self.assertInvalid(report, "duplicate")

    def test_empty_needs_g_allowed(self):
        report = copy.deepcopy(self.report)
        report["needs_g"] = []
        run_report.validate_run_report(report, self.schema)


if __name__ == "__main__":
    unittest.main()
