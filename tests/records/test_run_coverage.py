"""Coverage line projection (FL-1a, coverage contract v1) over synthetic run reports."""
import ast
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from campaign_tool.records import run_coverage, run_report  # noqa: E402

FIXTURE = ROOT / "examples" / "run-report" / "coverage-projection.synthetic.json"
MODULE = ROOT / "campaign_tool" / "records" / "run_coverage.py"
REQUIRED_CASES = {"a_live_shaped_scheduled_run", "b_adoption_skipped_not_configured",
                  "c_portal_and_extract_fail", "d_stub_run", "e_negative_adopted_is_unknown"}


def load_cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class CoverageFixtureTests(unittest.TestCase):
    def test_fixture_has_required_cases_and_every_report_is_v1_valid(self):
        cases = load_cases()
        self.assertEqual({case["name"] for case in cases}, REQUIRED_CASES)
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertEqual(set(case), {"name", "report", "expected"})
                run_report.validate_run_report(case["report"])

    def test_every_case_round_trips(self):
        for case in load_cases():
            with self.subTest(case=case["name"]):
                counts = run_coverage.coverage_counts(case["report"])
                self.assertEqual(counts, case["expected"]["counts"])
                self.assertEqual(run_coverage.format_coverage(counts), case["expected"]["line"])

    def test_unknown_is_none_never_zero(self):
        for case in load_cases():
            counts = run_coverage.coverage_counts(case["report"])
            line = run_coverage.format_coverage(counts)
            for key in ("adopted", "coded_gap", "awaiting_ocr", "portal_held"):
                value = counts[key]
                self.assertTrue(value is None or (isinstance(value, int) and value >= 0),
                                (case["name"], key, value))
            self.assertEqual(line.count("unknown"),
                             sum(counts[key] is None for key in
                                 ("adopted", "coded_gap", "awaiting_ocr", "portal_held")))

    def test_projection_does_not_mutate_report(self):
        for case in load_cases():
            before = copy.deepcopy(case["report"])
            run_coverage.coverage_counts(case["report"])
            self.assertEqual(case["report"], before)

    def test_warn_stage_counts_and_host_refusals_are_excluded(self):
        case = next(c for c in load_cases() if c["name"] == "a_live_shaped_scheduled_run")
        report = copy.deepcopy(case["report"])
        for item in report["needs_g"]:
            if item["code"] == "PORTAL_HOST_NOT_ALLOWLISTED":
                item["count"] = 50
        report["needs_g"].append({"code": "PORTAL_NOTICE_UNREADABLE", "stage": "portal",
                                  "severity": "action", "count": 9})
        run_report.validate_run_report(report)
        self.assertEqual(run_coverage.coverage_counts(report)["portal_held"], 7)

    def test_module_is_a_pure_consumer(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
        for banned in ("run_stages", "mail_adoption", "native_extract", "portal",
                       "run_orchestrator"):
            self.assertFalse(any(banned in name for name in imported), (banned, imported))
        source = MODULE.read_text(encoding="utf-8")
        for writer in ("write_text", "write_bytes", "open("):
            self.assertNotIn(writer, source)


class CoverageCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def run_cli(self, *args):
        # -B: no __pycache__ in the shared tree (tests/test_fa21_report_finite.py
        # snapshots it while parallel CI chunks run).
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records.run_coverage", *args],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)

    def write(self, payload, name="run-report.json"):
        path = self.dir / name
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                        encoding="utf-8")
        return path

    def test_cli_prints_case_a_line(self):
        case = next(c for c in load_cases() if c["name"] == "a_live_shaped_scheduled_run")
        result = self.run_cli("--report", str(self.write(case["report"])))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, case["expected"]["line"] + "\n")

    def test_cli_exits_2_on_invalid_report(self):
        case = next(c for c in load_cases() if c["name"] == "a_live_shaped_scheduled_run")
        invalid = copy.deepcopy(case["report"])
        invalid["overall_status"] = "ok"  # roll-up says warn
        for path in (self.write(invalid), self.write("{not json", "broken.json"),
                     self.dir / "missing.json"):
            with self.subTest(path=path.name):
                result = self.run_cli("--report", str(path))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn(str(self.dir), result.stderr)


if __name__ == "__main__":
    unittest.main()
