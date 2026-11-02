"""Acceptance tests for tools/run_report_review.py (synthetic reports only)."""
import ast
import contextlib
import copy
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import run_report_review as rrr  # noqa: E402

SCRIPT = ROOT / "tools" / "run_report_review.py"
EXAMPLE = ROOT / "examples" / "run-report" / "records-run-report-v1.synthetic.json"
BANNED = re.compile(r"\b(late|overdue)\b|violat|unlawful|illegal|non-?complian|breach", re.I)
REVIEWED = ("REVIEWED | none by this tool | machine-valid is review-required, not semantic acceptance; "
            "extracted and OCR text stay unreviewed; not a legal status")
FIELDS = ["VALID", "MODE", "WORK", "NOTIFY", "OWED", "REVIEWED"]


def example(**top):
    report = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    report.update(top)
    return report


def stage(report, name):
    return next(s for s in report["stages"] if s["name"] == name)


def set_status(report, name, status, code):
    s = stage(report, name)
    s["status"], s["error_code"] = status, code
    if status == "skipped":
        s["duration_seconds"] = None
    statuses = {x["status"] for x in report["stages"]}
    report["overall_status"] = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "ok"
    return report


def shifted(report, run_id, hour):
    report["run_id"] = run_id
    report["started_at"], report["finished_at"] = f"2026-01-01T{hour:02d}:00:00Z", f"2026-01-01T{hour:02d}:07:30Z"
    return report


class RunReportReviewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, report, name="r.json", directory=None, raw=None):
        directory = directory or self.tmp
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_bytes(raw if raw is not None else json.dumps(report).encode())
        return path

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rrr.main(["summary", *map(str, argv)])
        self.check_hygiene(out.getvalue() + err.getvalue())
        return code, out.getvalue().splitlines(), err.getvalue()

    def summary(self, report, *extra):
        return self.run_main(self.write(report), *extra)

    def check_hygiene(self, text):
        self.assertNotIn(str(self.tmp), text)
        self.assertNotIn("Traceback", text)
        self.assertIsNone(BANNED.search(text), text)
        for line in text.splitlines():
            self.assertTrue(line.startswith("review | "), line)

    def field(self, lines, name):
        return [line.split(" | ", 3)[3] if line.count(" | ") >= 3 else "" for line in lines
                if line.split(" | ")[2:3] == [name]]

    def assert_order(self, lines):
        seen = [line.split(" | ")[2] for line in lines]
        firsts = [seen.index(name) for name in FIELDS]
        self.assertEqual(firsts, sorted(firsts), seen)
        self.assertEqual(lines[-1].split(" | ", 2)[2], REVIEWED)

    # 1
    def test_valid_scheduled_report_prints_six_blocks_in_order(self):
        code, lines, err = self.summary(example(mode="scheduled"))
        self.assertEqual((code, err), (0, ""))
        self.assert_order(lines)
        self.assertEqual(self.field(lines, "VALID"), ["yes"])
        self.assertEqual(self.field(lines, "MODE"), ["scheduled"])
        self.assertNotIn("TEST evidence only; not live", "\n".join(lines))
        self.assertIn("sync=ok out=3 delta=ok out=4 adoption=ok out=4 portal=warn out=1 extract=ok out=5 "
                      "digest_gates=warn out=3 outputs=ok out=3", self.field(lines, "WORK"))
        owed = self.field(lines, "OWED")
        self.assertIn("review_required=2 -> gate 2 approve what gets published", owed)
        self.assertIn("GATE_REVIEW_REQUIRED x2 [action] -> gate 2 approve what gets published", owed)
        self.assertIn("PORTAL_RETRY_EXHAUSTED x1 [info] -> operator (no gate)", owed)
        self.assertIn("ocr unknown", owed)

    def test_cli_on_shipped_example(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "summary", str(EXAMPLE)],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertTrue(result.stdout.rstrip().endswith(REVIEWED))

    # 2
    def test_unknown_is_not_zero(self):
        report = set_status(set_status(example(), "extract", "skipped", None), "portal", "fail", "PORTAL_FETCH_BLOCKED")
        code, lines, _ = self.summary(report)
        work = self.field(lines, "WORK")[0]
        self.assertIn("portal=fail out=unknown", work)
        self.assertIn("extract=skipped out=unknown", work)
        self.assertNotIn("out=0", work)
        self.assertIn("counts | unknown", self.field(lines, "WORK"))
        self.assertIn("AWAITING_OCR=unknown", self.field(lines, "OWED"))
        self.assertEqual(code, 1)

    def test_stub_backend_marks_stage_output_unknown(self):
        report = example()
        report["needs_g"].append({"code": "STUB_STAGE_BACKEND", "stage": "extract", "severity": "info", "count": 1})
        code, lines, _ = self.summary(report)
        self.assertEqual(self.field(lines, "STUB"), ["yes | output of this run is not real data"])
        self.assertIn("extract=ok out=unknown", self.field(lines, "WORK")[0])
        self.assertIn("sync=ok out=3", self.field(lines, "WORK")[0])
        self.assertEqual(code, 0)

    def test_extract_outcomes_and_unmapped_codes(self):
        report = example()
        stage(report, "extract")["outcomes"] = [{"code": "EXTRACTED", "count": 3}, {"code": "AWAITING_OCR", "count": 1},
                                                {"code": "NEW_OUTCOME_CODE", "count": 1}]
        code, lines, _ = self.summary(report)
        self.assertIn("counts | EXTRACTED=3 AWAITING_OCR=1 NEW_OUTCOME_CODE=1 unmapped=1", self.field(lines, "WORK"))
        self.assertEqual(code, 0)

    # 3
    def test_invalid_reports_are_not_interpreted(self):
        def order(r):
            r["stages"][0], r["stages"][1] = r["stages"][1], r["stages"][0]
        def out_of_order(r):
            r["started_at"], r["finished_at"] = r["finished_at"], r["started_at"]
        cases = {"stage order": order, "finished before started": out_of_order,
                 "rollup mismatch": lambda r: r.update(overall_status="ok"),
                 "extra key": lambda r: r.update(free_text_key_zz="call me at home"),
                 "non-finite": lambda r: r.update(duration_seconds=float("nan"))}
        for name, mutate in cases.items():
            with self.subTest(name):
                report = example()
                mutate(report)
                code, lines, _ = self.summary(report)
                self.assertEqual(code, 1)
                self.assertEqual(self.field(lines, "VALID")[0].split(" | ")[0], "no")
                self.assertEqual(self.field(lines, "WORK"), ["not interpreted"])
                self.assertEqual(self.field(lines, "OWED"), ["not interpreted"])
                text = "\n".join(lines)
                self.assertNotIn("ok", re.sub(r"REVIEWED.*", "", text).replace("not interpreted", ""))
                self.assertNotIn("free_text_key_zz", text)
                self.assertNotIn("call me", text)
                self.assertEqual(lines[-1].split(" | ", 2)[2], REVIEWED)

    def test_unreadable_json_is_invalid(self):
        code, lines, _ = self.run_main(self.write(None, raw=b"{not json"))
        self.assertEqual((code, self.field(lines, "VALID")), (1, ["no | unreadable JSON"]))

    # 4
    def test_notify_only_failure(self):
        report = set_status(example(notify_failed=True), "notify", "fail", "NOTIFY_FAILED")
        code, lines, _ = self.summary(report)
        self.assertEqual(code, 0)
        self.assertEqual(self.field(lines, "NOTIFY"), ["failed | does not affect WORK | rollup=fail (notify only)"])
        self.assertNotIn("=fail", self.field(lines, "WORK")[0])
        report = set_status(report, "outputs", "fail", "OUTPUTS_FAILED")
        code, lines, _ = self.summary(report)
        self.assertEqual(code, 1)
        self.assertEqual(self.field(lines, "NOTIFY"), ["failed | does not affect WORK | rollup=fail"])

    # 5
    def test_bypass_by_wording_stays_unmapped_and_unreviewed(self):
        report = example()
        for code in ("REVIEW_COMPLETE", "ALL_CLEAR", "APPROVED"):
            report["needs_g"].append({"code": code, "stage": "outputs", "severity": "info", "count": 1})
        report["needs_g"].append({"code": "LEGAL_REVIEW_REQUIRED", "stage": "digest_gates", "severity": "blocking", "count": 4})
        report["needs_g"].append({"code": "SOURCE_BREACH_FOUND", "stage": "outputs", "severity": "info", "count": 1})
        report["ocr"] = {"pages_ocr": 6, "pages_conf_ge80": 4, "pages_conf_lt80": 1, "pages_conf_unknown": 1,
                         "pages_reviewed": 6, "awaiting_ocr_images": 0}
        code, lines, _ = self.summary(report)
        owed = self.field(lines, "OWED")
        for name in ("REVIEW_COMPLETE", "ALL_CLEAR", "APPROVED"):
            self.assertIn(f"{name} x1 [info] unmapped -> operator", owed)
        self.assertIn("LEGAL_REVIEW_REQUIRED x4 [blocking] -> gate 3 approve the law and any legal claim", owed)
        self.assertIn("WITHHELD_CODE x1 [info] unmapped -> operator", owed)
        self.assertIn("ocr pages_reviewed=6/6 | pages unreviewed=0", owed)
        self.assertEqual(lines[-1].split(" | ", 2)[2], REVIEWED)
        self.assertEqual(code, 0)

    # 6
    def test_mode_comes_from_report_only(self):
        code, lines, _ = self.summary(example(mode="test"))
        self.assertEqual(self.field(lines, "MODE"), ["test | TEST evidence only; not live"])
        named = self.write(example(mode="manual"), name="test-run.json", directory=self.tmp / "test")
        code, lines, _ = self.run_main(named)
        self.assertEqual(self.field(lines, "MODE"), ["manual"])
        self.assertNotIn("TEST evidence", "\n".join(lines))
        code, lines, _ = self.run_main("--dir", self.tmp / "test")
        self.assertEqual(self.field(lines, "MODE"), ["manual"])

    # 7
    def test_duplicates(self):
        report = example()
        a = self.write(report, "a.json", self.tmp / "d")
        self.write(report, "b.json", self.tmp / "d")
        code, lines, _ = self.run_main("--dir", self.tmp / "d")
        self.assertEqual(code, 0)
        self.assertEqual(self.field(lines, "DUPLICATE"), ["identical"])
        self.assertEqual(self.field(lines, "VALID"), ["yes"])
        self.assertEqual(lines[-1], "review | reports=1 valid=1 invalid=0 duplicates=1 | machine-valid is not reviewed")
        a.write_text(json.dumps(report, indent=2))
        code, lines, _ = self.run_main("--dir", self.tmp / "d")
        self.assertEqual(code, 1)
        self.assertEqual(self.field(lines, "DUPLICATE"), ["run_id | contents differ"] * 2)
        self.assertEqual(lines[-1], "review | reports=2 valid=2 invalid=0 duplicates=2 | machine-valid is not reviewed")

    # 8
    def test_age_and_future(self):
        report = example()
        _, base, _ = self.summary(report)
        code, lines, _ = self.summary(report, "--now", "2026-01-02T22:07:30Z")
        self.assertEqual(code, 0)
        self.assertEqual(self.field(lines, "AGE"), ["40h since finished_at"])
        self.assertEqual([line for line in lines if " | AGE | " not in line], base)
        code, lines, _ = self.summary(report, "--now", "2026-01-01T06:07:29Z")
        self.assertEqual(code, 1)
        self.assertEqual(self.field(lines, "AGE"), ["in the future | do not trust"])
        self.assertEqual([line for line in lines if " | AGE | " not in line], base)
        code, lines, err = self.summary(report, "--now", "2026-01-02 22:07")
        self.assertEqual((code, lines), (2, []))
        self.assertIn("review | refused | --now must be", err)
        # Shape-valid but calendar/time-invalid: refused with exit 2, no traceback, value not echoed.
        for bad in ("2026-02-30T00:00:00Z", "2026-13-01T00:00:00Z", "2026-01-02T25:00:00Z", "2026-01-02T22:61:00Z"):
            code, lines, err = self.summary(report, "--now", bad)
            self.assertEqual((code, lines), (2, []), bad)
            self.assertEqual(err.strip(), "review | refused | --now is not a real UTC date and time", bad)

    # 9
    def test_interrupted_stage(self):
        code, lines, _ = self.summary(set_status(example(), "outputs", "fail", "RUN_INTERRUPTED"))
        self.assertEqual(code, 1)
        self.assertIn("outputs=fail out=unknown", self.field(lines, "WORK")[0])
        self.assertIn("rollup=fail", self.field(lines, "NOTIFY")[0])
        self.assertNotIn("notify only", self.field(lines, "NOTIFY")[0])

    # 10
    def test_dir_orders_by_started_at_not_name(self):
        d = self.tmp / "order"
        self.write(shifted(example(), "run_20260101T050000Z_00000001", 5), "z.json", d)
        self.write(shifted(example(), "run_20260101T090000Z_00000002", 9), "a.json", d)
        self.write(shifted(example(), "run_20260101T070000Z_00000003", 7), "m.json", d)
        code, lines, _ = self.run_main("--dir", d)
        ids = [line.split(" | ")[1] for line in lines if " | VALID | " in line]
        self.assertEqual(ids, ["run_20260101T050000Z_00000001", "run_20260101T070000Z_00000003",
                               "run_20260101T090000Z_00000002"])
        self.assertEqual(code, 0)

    def test_refusals_echo_no_path(self):
        target = self.write(example(), "real.json", self.tmp / "src")
        os.symlink(target, self.tmp / "link.json")
        os.symlink(self.tmp / "src", self.tmp / "linkdir")
        big = self.tmp / "big"
        self.write(None, "big.json", big, raw=b" " * (1024 * 1024 + 1))
        many = self.tmp / "many"
        many.mkdir()
        for index in range(501):
            (many / f"{index}.json").write_text("{}")
        lnk = self.tmp / "hasln"
        lnk.mkdir()
        os.symlink(target, lnk / "x.json")
        for argv in ([self.tmp / "link.json"], ["--dir", self.tmp / "linkdir"], [big / "big.json"],
                     ["--dir", big], ["--dir", many], ["--dir", lnk], [self.tmp / "missing.json"], []):
            with self.subTest(argv=[Path(a).name for a in argv]):
                code, lines, err = self.run_main(*argv)
                self.assertEqual((code, lines), (2, []))
                self.assertTrue(err.startswith("review | refused | "), err)

    # 11
    def test_writes_nothing_and_imports_no_network_or_store(self):
        d = self.tmp / "ro"
        self.write(example(), "a.json", d)
        self.write(None, "b.json", d, raw=b"[]")
        before = {p: p.read_bytes() for p in d.rglob("*")}
        code, lines, _ = self.run_main("--dir", d)
        self.assertEqual(code, 1)
        self.assertEqual({p: p.read_bytes() for p in d.rglob("*")}, before)
        self.assertEqual(lines[-1], "review | reports=2 valid=1 invalid=1 duplicates=0 | machine-valid is not reviewed")
        banned = {"smtplib", "imaplib", "socket", "ssl", "http", "urllib", "requests", "subprocess", "sqlite3"}
        found = set()
        for node in ast.walk(ast.parse(SCRIPT.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                found.add((node.module or "").split(".")[0])
        self.assertFalse(found & banned, found)
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertFalse(re.search(r"\bopen\(|\.(write_text|write_bytes|mkdir|unlink|rename|touch|rmdir)\(", source))

    def test_no_review_recording_verb(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rrr.main(["mark-reviewed", str(EXAMPLE)])


if __name__ == "__main__":
    unittest.main()
