"""Acceptance tests for tools/run_report_backlog.py (synthetic reports only)."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import run_report_backlog as rrb  # noqa: E402

SCRIPT = ROOT / "tools" / "run_report_backlog.py"
EXAMPLE = ROOT / "examples" / "run-report" / "records-run-report-v1.synthetic.json"


def backlog_report(error=18, refused=6, preserve=400, gaps=120, outcome_error=None):
    report = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    total = error + refused + preserve + 1
    for s in report["stages"]:
        if s["name"] == "extract":
            s["status"], s["error_code"] = "warn", "EXTRACT_DOCUMENT_FAILURES"
            s["counts"] = {"input": total, "output": 1, "failed": error + refused, "skipped": preserve}
            s["outcomes"] = [{"code": c, "count": n} for c, n in (
                ("EXTRACTED", 1),
                ("EXTRACT_NOT_ACCEPTED", refused),
                ("EXTRACT_ERROR", error if outcome_error is None else outcome_error),
                ("PRESERVE_PENDING", preserve)) if n]
            if outcome_error is not None:
                s["counts"]["input"] += outcome_error - error
        if s["name"] == "adoption":
            s["status"], s["error_code"] = "warn", "ADOPTION_CODED_GAPS"
    report["needs_g"] = [{"code": c, "stage": st, "severity": sev, "count": n} for c, st, sev, n in (
        ("ADOPTION_CODED_GAPS", "adoption", "info", gaps),
        ("EXTRACT_ERROR", "extract", "action", error),
        ("EXTRACT_NOT_ACCEPTED", "extract", "action", refused),
        ("EXTRACT_PRESERVE_PENDING", "extract", "info", preserve),
        ("PORTAL_FETCH_DRY_RUN_ONLY", "portal", "info", 139)) if n]
    report["overall_status"] = "warn"
    return report


class BacklogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def run_main(self, report, *extra, raw=None):
        path = self.tmp / "r.json"
        path.write_bytes(raw if raw is not None else json.dumps(report).encode())
        before = sorted(p.name for p in self.tmp.iterdir())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rrb.main([str(path), *extra])
        self.assertEqual(before, sorted(p.name for p in self.tmp.iterdir()))  # writes nothing
        self.assertNotIn(str(self.tmp), out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_four_held_classes_with_baseline_counts(self):
        code, out, err = self.run_main(backlog_report())
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        for text in ("EXTRACT_ERROR x18", "EXTRACT_NOT_ACCEPTED x6", "EXTRACT_PRESERVE_PENDING x400",
                     "ADOPTION_CODED_GAPS x120"):
            self.assertEqual(sum(text in line for line in lines), 1, text)
        self.assertIn("held total=544", lines[-1])
        self.assertNotIn("PORTAL_FETCH_DRY_RUN_ONLY", out)  # not one of the four held classes
        self.assertTrue(all(line.startswith("backlog | ") for line in lines))

    def test_counts_equal_report_and_json_matches(self):
        code, out, _ = self.run_main(backlog_report(error=3, refused=0, preserve=7, gaps=2), "--json")
        doc = json.loads(out)
        self.assertEqual(code, 0)
        counts = {h["code"]: h["count"] for h in doc["held"]}
        self.assertEqual(counts, {"EXTRACT_ERROR": 3, "EXTRACT_NOT_ACCEPTED": 0,
                                  "EXTRACT_PRESERVE_PENDING": 7, "ADOPTION_CODED_GAPS": 2})
        self.assertTrue(all(h["outcome_check"] in ("match", "n/a") for h in doc["held"]))

    def test_disagreeing_outcome_count_is_flagged(self):
        _, out, _ = self.run_main(backlog_report(outcome_error=17))
        row = next(line for line in out.splitlines() if "EXTRACT_ERROR x18" in line)
        self.assertIn("outcome differs", row)

    def test_every_line_names_clearing_and_live_write_and_invents_no_verb(self):
        _, out, _ = self.run_main(backlog_report())
        rows = [line for line in out.splitlines() if " x" in line and "clear:" in line]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all("writes live data: yes" in line for line in rows))
        self.assertIn("none on main", out)
        self.assertIn("adopt-mail --root ROOT --mail-history MAIL (no --apply)", out)

    def test_invalid_report_is_refused_without_listing(self):
        report = backlog_report()
        del report["needs_g"]
        code, out, _ = self.run_main(report)
        self.assertEqual(code, 1)
        self.assertIn("VALID | no", out)
        self.assertNotIn("EXTRACT_ERROR", out)

    def test_unreadable_and_missing_input(self):
        self.assertEqual(self.run_main(None, raw=b"not json")[0], 1)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(rrb.main([str(self.tmp / "absent.json")]), 2)
        self.assertNotIn(str(self.tmp), err.getvalue())

    def test_cli_on_shipped_example_lists_zero_held(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), str(EXAMPLE)],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertIn("held total=0", result.stdout)


if __name__ == "__main__":
    unittest.main()
