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


ACCEPTED = frozenset({"original_scope_unresolved", "unsupported_rfc822_part"})


def adopt_report(receipts, gaps):
    """An adopt-mail style payload for synthetic gap counts."""
    return {"receipts": receipts, "coded_gap_messages": sum(gaps.values()), "coded_gaps": dict(gaps),
            "unaccepted_coded_gaps": {k: v for k, v in gaps.items() if k not in ACCEPTED}}


def with_gaps(report, gaps, receipts=200, stage=2):
    """Record ``gaps`` on report.stages[stage] with the matching needs-G totals, as the stages do."""
    payload = adopt_report(receipts, gaps)
    entry = report["stages"][stage]
    entry["counts"]["input"] = receipts
    entry["outcomes"] = run_report.gap_partition(payload, ACCEPTED)
    items = [("ADOPTION_CODED_GAPS", "info", payload["coded_gap_messages"]),
             ("ADOPTION_UNACCEPTED_GAP", "blocking", sum(payload["unaccepted_coded_gaps"].values()))]
    report["needs_g"] += [{"code": code, "stage": entry["name"], "severity": sev, "count": n}
                          for code, sev, n in items if n]
    return entry


class GapPartitionTests(unittest.TestCase):
    """DF1117L-05: delta/adoption outcomes say which coded gaps hold messages back."""

    def setUp(self):
        self.schema = run_report.load_schema()
        self.report = _example()

    def assertInvalid(self, report, fragment):
        with self.assertRaises(ValueError) as caught:
            run_report.validate_run_report(report, self.schema)
        self.assertIn(fragment, str(caught.exception))

    def test_no_gaps_records_only_the_remainder(self):
        # Believable bug: a no-gap stage records nothing (unknown) instead of a measured zero.
        stage = with_gaps(self.report, {}, receipts=4)
        self.assertEqual(stage["outcomes"], [{"code": "GAP_NONE", "count": 4}])
        run_report.validate_run_report(self.report, self.schema)
        self.assertEqual(run_report.adoption_gap_breakdown(stage), [])
        self.assertEqual(run_report.gap_partition(adopt_report(0, {}), ACCEPTED), [])

    def test_accepted_only_is_not_blocking(self):
        gaps = {"unsupported_rfc822_part": 1, "original_scope_unresolved": 2}
        stage = with_gaps(self.report, gaps, receipts=5)
        run_report.validate_run_report(self.report, self.schema)
        self.assertEqual(run_report.adoption_gap_breakdown(stage), [
            {"code": "ORIGINAL_SCOPE_UNRESOLVED", "count": 2, "accepted": True},
            {"code": "UNSUPPORTED_RFC822_PART", "count": 1, "accepted": True}])
        self.assertNotIn("ADOPTION_UNACCEPTED_GAP", [i["code"] for i in self.report["needs_g"]])

    def test_mixed_accepted_and_blocking(self):
        gaps = {"original_scope_unresolved": 100, "new_unknown_class": 20}
        stage = with_gaps(self.report, gaps)
        run_report.validate_run_report(self.report, self.schema)
        self.assertEqual(stage["outcomes"], [{"code": "GA_ORIGINAL_SCOPE_UNRESOLVED", "count": 100},
                                             {"code": "GB_NEW_UNKNOWN_CLASS", "count": 20},
                                             {"code": "GAP_NONE", "count": 80}])
        rows = run_report.adoption_gap_breakdown(stage)
        self.assertEqual([(r["code"], r["accepted"]) for r in rows],
                         [("ORIGINAL_SCOPE_UNRESOLVED", True), ("NEW_UNKNOWN_CLASS", False)])

    def test_cap_keeps_32_codes_and_buckets_the_rest_without_losing_messages(self):
        gaps = {f"code_{i:02d}": 2 + i % 3 for i in range(40)}
        gaps["unsupported_rfc822_part"] = 9
        gaps["original_scope_unresolved"] = 1
        exact = {f"code_{i:02d}": 1 for i in range(32)}
        rows = run_report.gap_partition(adopt_report(200, exact), ACCEPTED)
        self.assertEqual(len([r for r in rows if r["code"].startswith("GB_")]), 32)
        self.assertFalse([r for r in rows if r["code"].endswith("_OTHER")])
        rows = run_report.gap_partition(adopt_report(500, gaps), ACCEPTED)
        reordered = dict(reversed(list(gaps.items())))
        self.assertEqual(rows, run_report.gap_partition(adopt_report(500, reordered), ACCEPTED))
        named = [r for r in rows if r["code"] not in ("GAP_NONE", "GA_OTHER", "GB_OTHER")]
        self.assertEqual(len(named), 32)
        self.assertEqual(named[0], {"code": "GA_UNSUPPORTED_RFC822_PART", "count": 9})
        counts = {r["code"]: r["count"] for r in rows}
        self.assertEqual(sum(counts.values()), 500)
        self.assertEqual(sum(v for k, v in counts.items() if k.startswith("GB_")),
                         sum(v for k, v in gaps.items() if k not in ACCEPTED))
        self.assertEqual(counts["GA_OTHER"], 1)  # the lone accepted code ranks last, so it overflows
        self.assertGreater(counts["GB_OTHER"], 0)
        stage = with_gaps(self.report, gaps, receipts=500)
        run_report.validate_run_report(self.report, self.schema)
        self.assertEqual(sum(r["count"] for r in run_report.adoption_gap_breakdown(stage)), sum(gaps.values()))

    def test_unsafe_producer_codes_never_reach_the_stage_result(self):
        # Believable bug: an unsafe or unexpected adopter code is echoed or truncated into the report.
        for bad in ("Bad-Code", "x\n", "has space", "UPPER", "/private/path", "1abc", ""):
            with self.subTest(code=bad):
                payload = adopt_report(10, {"fine_code": 1})
                payload["coded_gaps"][bad] = 1
                payload["coded_gap_messages"] = 2
                self.assertIsNone(run_report.gap_partition(payload, ACCEPTED))
        for mutate in (lambda p: p["coded_gaps"].update(fine_code=True), lambda p: p.update(coded_gap_messages=3),
                       lambda p: p["coded_gaps"].update(fine_code=-1), lambda p: p.update(receipts=0),
                       lambda p: p.pop("coded_gaps"), lambda p: p.update(unaccepted_coded_gaps={})):
            payload = adopt_report(10, {"fine_code": 1})
            mutate(payload)
            self.assertIsNone(run_report.gap_partition(payload, ACCEPTED))
        long_code, reserved = "a" + "b" * 62, "other"
        rows = run_report.gap_partition(adopt_report(10, {long_code: 2, reserved: 1}), ACCEPTED)
        self.assertEqual(rows, [{"code": "GB_OTHER", "count": 3}, {"code": "GAP_NONE", "count": 7}])

    def test_validator_rejects_malformed_partitions(self):
        cases = [
            ("not a gap code", lambda e: e["outcomes"].__setitem__(0, {"code": "EXTRACTED", "count": 100})),
            ("not a gap code", lambda e: e["outcomes"].__setitem__(0, {"code": "GA_X\n", "count": 100})),
            ("both accepted and blocking", lambda e: e.update(outcomes=[{"code": "GA_ORIGINAL_SCOPE_UNRESOLVED", "count": 100},
                                                                   {"code": "GB_ORIGINAL_SCOPE_UNRESOLVED", "count": 50},
                                                                   {"code": "GAP_NONE", "count": 50}])),
            ("disagrees", lambda e: e["outcomes"].__setitem__(0, {"code": "GB_ORIGINAL_SCOPE_UNRESOLVED", "count": 100})),
            ("more than 32", lambda e: e.update(outcomes=[{"code": f"GA_C{i:02d}", "count": 1} for i in range(33)],
                                               counts=dict(e["counts"], input=33))),
        ]
        for fragment, mutate in cases:
            with self.subTest(fragment=fragment):
                report = copy.deepcopy(self.report)
                mutate(with_gaps(report, {"original_scope_unresolved": 100}))
                self.assertInvalid(report, fragment)
        report = copy.deepcopy(self.report)
        with_gaps(report, {"new_unknown_class": 5})
        report["needs_g"] = [i for i in report["needs_g"] if i["code"] != "ADOPTION_UNACCEPTED_GAP"]
        self.assertInvalid(report, "disagrees")

    def test_old_reports_without_the_field_validate(self):
        # Compatibility: reports written before DF1117L-05 stay valid and decode to None, not [].
        run_report.validate_run_report(self.report, self.schema)
        for stage in self.report["stages"]:
            self.assertIsNone(run_report.adoption_gap_breakdown(stage))
        self.assertIsNone(run_report.adoption_gap_breakdown(None))


if __name__ == "__main__":
    unittest.main()
