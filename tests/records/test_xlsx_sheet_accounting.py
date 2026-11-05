"""RF0210-4: XLSX chartsheets are accounted for as located unsupported gaps.

Synthetic openpyxl workbooks only; the real intake CLI runs in subprocesses and
the test process itself may not open a network socket.
"""
import hashlib
import importlib.util
import json
import pathlib
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "campaign_tool" / "records" / "intake" / "folder.py"
spec = importlib.util.spec_from_file_location("intake_xlsx_accounting", SCRIPT)
folder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(folder)

CHART_CODE = "workbook_chartsheet_not_extracted"
HOLD_CODE = "workbook_cached_values_and_objects_not_reviewed"


def refuse_network(*args, **kwargs):
    raise AssertionError("network access is not allowed in this test")


def add_chart(chartsheet):
    from openpyxl.chart import BarChart
    chartsheet.add_chart(BarChart())


class XlsxAccountingBase(unittest.TestCase):
    def setUp(self):
        for name in ("connect", "bind"):
            patcher = patch.object(socket.socket, name, refuse_network)
            patcher.start(); self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.root = self.base / "originals"; self.root.mkdir()
        self.out = self.base / "out"

    def cli(self, command="run", *extra):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), command, "--root", str(self.root),
                                 "--output", str(self.out), *extra], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def db(self):
        db = sqlite3.connect(self.out / "intake.sqlite"); db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        return db

    def doc(self, db=None):
        row = (db or self.db()).execute("SELECT * FROM docs WHERE format='xlsx'").fetchone()
        return row, json.loads(row["digest"])

    def sheet_units(self, db=None):
        return [(json.loads(r["locator"])["sheet"], json.loads(r["data"]))
                for r in (db or self.db()).execute("SELECT locator,data FROM units WHERE kind='workbook_sheet' ORDER BY ordinal")]

    def mixed_workbook(self, name="book.xlsx"):
        from openpyxl import Workbook
        wb = Workbook(); data = wb.active; data.title = "Data"; data["A1"] = "first"; data["B1"] = "=1+1"
        add_chart(wb.create_chartsheet("Chart only"))
        wb.create_sheet("After")["A1"] = "trailing"
        wb.save(self.root / name)
        return self.root / name


class ChartsheetAccountingTests(XlsxAccountingBase):
    def test_mixed_chartsheet_preserves_sheet_order_and_trailing_rows(self):
        from openpyxl import load_workbook
        path = self.mixed_workbook(); before = path.read_bytes()
        probe = load_workbook(path)
        self.assertEqual(probe.sheetnames, ["Data", "Chart only", "After"])
        self.assertEqual([s.title for s in probe.worksheets], ["Data", "After"])
        self.cli(); db = self.db(); row, digest = self.doc(db)
        sheets = self.sheet_units(db)
        self.assertEqual([name for name, _ in sheets], ["Data", "Chart only", "After"])
        self.assertEqual(sheets[1][1], {"state": "visible", "declared_rows": None, "declared_columns": None,
                                        "sheet_type": "chartsheet", "extraction_status": "unsupported"})
        self.assertNotIn("sheet_type", sheets[0][1]); self.assertNotIn("sheet_type", sheets[2][1])
        self.assertEqual(digest["counts"]["sheets_expected"], 3)
        self.assertEqual(digest["counts"]["workbook_sheet"], 3)
        self.assertEqual(digest["counts"]["chartsheets_unsupported"], 1)
        self.assertEqual(digest["counts"]["formulas"], 1)
        gaps = [i for i in digest["issues"] if i["code"] == CHART_CODE]
        self.assertEqual(gaps, [{"code": CHART_CODE, "locator": {"sheet": "Chart only"},
                                 "detail": "Chartsheet content is not extracted; visual review required."}])
        self.assertEqual(sum(i["code"] == HOLD_CODE for i in digest["issues"]), 1)
        self.assertEqual(digest["stage"], "partial"); self.assertEqual(digest["review_status"], "not_reviewed")
        rows = [(json.loads(r["locator"]), r["text"]) for r in db.execute("SELECT locator,text FROM units WHERE kind='workbook_row' ORDER BY ordinal")]
        self.assertEqual([loc for loc, _ in rows], [{"sheet": "Data", "row": 1}, {"sheet": "After", "row": 1}])
        self.assertIn("trailing", rows[1][1]); self.assertIn("=1+1", rows[0][1])
        self.assertFalse([r for r in db.execute("SELECT 1 FROM units WHERE locator LIKE '%Chart only%' AND kind!='workbook_sheet'")])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((self.out / "blobs" / row["sha"]).read_bytes(), before)

    def test_chartsheet_only_is_accounted_but_never_extracted(self):
        from openpyxl import Workbook
        wb = Workbook(); add_chart(wb.create_chartsheet("Only chart")); wb.remove(wb["Sheet"])
        wb.save(self.root / "charts.xlsx")
        self.cli(); db = self.db(); _, digest = self.doc(db)
        self.assertEqual(digest["stage"], "partial")
        self.assertEqual((digest["counts"]["sheets_expected"], digest["counts"]["workbook_sheet"],
                          digest["counts"]["chartsheets_unsupported"]), (1, 1, 1))
        self.assertFalse(digest["counts"].get("workbook_row"))
        self.assertEqual(sorted(i["code"] for i in digest["issues"]), sorted([CHART_CODE, HOLD_CODE]))
        self.assertEqual([i["locator"] for i in digest["issues"] if i["code"] == CHART_CODE], [{"sheet": "Only chart"}])
        self.assertEqual(db.execute("SELECT count(*) FROM units WHERE kind='workbook_row'").fetchone()[0], 0)

    def test_multiple_hidden_unicode_and_empty_sheets(self):
        from openpyxl import Workbook
        wb = Workbook(); first = wb.active; first.title = "Données"; first["A1"] = "visible"
        for title, state in (("图表 visible", "visible"), ("Ñandú hidden", "hidden"), ("Ωmega very", "veryHidden")):
            chart = wb.create_chartsheet(title); add_chart(chart); chart.sheet_state = state
        wb.create_sheet("Empty")
        hidden = wb.create_sheet("Hidden data"); hidden["A1"] = "kept"; hidden.sheet_state = "hidden"
        wb.save(self.root / "many.xlsx")
        self.cli(); db = self.db(); _, digest = self.doc(db)
        sheets = self.sheet_units(db)
        self.assertEqual([n for n, _ in sheets], ["Données", "图表 visible", "Ñandú hidden", "Ωmega very", "Empty", "Hidden data"])
        self.assertEqual([d["state"] for _, d in sheets], ["visible", "visible", "hidden", "veryHidden", "visible", "hidden"])
        self.assertEqual([d.get("sheet_type") for _, d in sheets], [None, "chartsheet", "chartsheet", "chartsheet", None, None])
        self.assertEqual(digest["counts"]["sheets_expected"], 6)
        self.assertEqual(digest["counts"]["workbook_sheet"], 6)
        self.assertEqual(digest["counts"]["chartsheets_unsupported"], 3)
        gaps = [i["locator"] for i in digest["issues"] if i["code"] == CHART_CODE]
        self.assertEqual(gaps, [{"sheet": "图表 visible"}, {"sheet": "Ñandú hidden"}, {"sheet": "Ωmega very"}])

    def test_plain_workbook_rows_and_formula_holds_unchanged(self):
        from openpyxl import Workbook
        wb = Workbook(); a = wb.active; a.title = "A"; a["A1"] = "one"; a["B2"] = "=SUM(1,2)"
        wb.create_sheet("Blank"); hid = wb.create_sheet("H"); hid["C3"] = 7; hid.sheet_state = "hidden"
        wb.save(self.root / "plain.xlsx")
        self.cli(); db = self.db(); _, digest = self.doc(db)
        sheets = self.sheet_units(db)
        self.assertEqual([n for n, _ in sheets], ["A", "Blank", "H"])
        self.assertEqual([d["state"] for _, d in sheets], ["visible", "visible", "hidden"])
        for _, data in sheets:
            self.assertEqual(set(data), {"state", "declared_rows", "declared_columns"})
        self.assertEqual(sheets[0][1]["declared_rows"], 2)
        self.assertEqual(digest["counts"]["formulas"], 1)
        self.assertEqual(digest["counts"]["sheets_expected"], 3)
        self.assertNotIn("chartsheets_unsupported", digest["counts"])
        self.assertEqual([i["code"] for i in digest["issues"]], [HOLD_CODE])
        self.assertEqual(digest["issues"][0]["detail"], "Formula text retained; no recalculation, macro execution, object rendering or cached-value comparison.")
        text = "\n".join(r[0] for r in db.execute("SELECT text FROM units WHERE kind='workbook_row' ORDER BY ordinal"))
        self.assertIn('"value":"=SUM(1,2)"', text.replace(" ", "")); self.assertIn('"type":"f"', text.replace(" ", ""))
        self.assertEqual(digest["stage"], "partial")

    def test_corrupt_workbook_still_reports_failure(self):
        (self.root / "broken.xlsx").write_bytes(b"PK\x03\x04 not a workbook at all")
        self.cli(); db = self.db(); row = db.execute("SELECT * FROM docs").fetchone()
        digest = json.loads(row["digest"])
        self.assertEqual(row["stage"], "failed")
        self.assertIn("extraction_error", [i["code"] for i in digest["issues"]])
        self.assertNotIn("sheets_expected", digest["counts"])
        self.assertEqual(db.execute("SELECT count(*) FROM units WHERE kind='workbook_sheet'").fetchone()[0], 0)

    def test_chartsheet_gap_reaches_private_coverage_and_queue(self):
        path = self.mixed_workbook(); before = path.read_bytes()
        self.cli(); db = self.db(); row, digest = self.doc(db)
        blob = self.out / "blobs" / row["sha"]; blob_bytes = blob.read_bytes()
        artifacts = {n: (self.out / "derived" / row["sha"] / n).read_bytes() for n in ("digest.json", "units.jsonl", "text.txt")}
        database_before = [tuple(r) for r in db.execute("SELECT * FROM docs")]
        self.cli("report")
        summary = json.loads((self.out / "coverage.json").read_text())
        gaps = [g for g in summary["extraction_gaps"] if g["reason"] == CHART_CODE]
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0]["sha256"], gaps[0]["stage"], gaps[0]["locator"]),
                         (row["sha"], "partial", {"sheet": "Chart only"}))
        self.assertEqual(summary["issues"][CHART_CODE], 1)
        queue = [json.loads(line) for line in (self.out / "all-agency-document-queue.jsonl").read_text().splitlines()]
        entry = [q for q in queue if q["sha256"] == row["sha"]][0]
        self.assertEqual(entry["stage"], "partial")
        queued = [i for i in entry["issues"] if i["code"] == CHART_CODE]
        self.assertEqual([i["locator"] for i in queued], [{"sheet": "Chart only"}])
        markdown = (self.out / "coverage.md").read_text()
        self.assertIn(CHART_CODE, markdown); self.assertIn("Chart only", markdown)
        self.assertEqual(digest["counts"]["workbook_sheet"], 3)
        for name in ("coverage.json", "coverage.md", "all-agency-document-queue.jsonl"):
            self.assertEqual((self.out / name).stat().st_mode & 0o077, 0, name)
        self.assertEqual(blob.read_bytes(), blob_bytes); self.assertEqual(blob_bytes, before)
        self.assertEqual(hashlib.sha256(blob_bytes).hexdigest(), row["sha"])
        for name, content in artifacts.items():
            self.assertEqual((self.out / "derived" / row["sha"] / name).read_bytes(), content)
        self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM docs")], database_before)


class ChartsheetPolicyReuseTests(XlsxAccountingBase):
    def seed(self, mutate):
        """Run once, then rewrite the stored digest as an older extraction would have left it."""
        self.mixed_workbook(); self.cli()
        db = self.db(); row, digest = self.doc(db)
        mutate(digest)
        db.execute("UPDATE docs SET digest=? WHERE sha=?", (folder.js(digest), row["sha"])); db.commit()
        return row["sha"]

    def attempts(self, sha):
        return self.db().execute("SELECT count(*) FROM extraction_attempts WHERE sha=?", (sha,)).fetchone()[0]

    def old_policy(self):
        return folder.hid(folder.js(dict(format="xlsx", paragraph_ordinals=None, excluded_path_fragments=[])))

    def check_reparse_once(self, mutate):
        sha = self.seed(mutate); original = (self.out / "blobs" / sha).read_bytes()
        before = self.attempts(sha); history_before = list((self.out / "history").glob("*/*")) if (self.out / "history").exists() else []
        self.cli("extract")
        db = self.db(); _, digest = self.doc(db)
        self.assertEqual(self.attempts(sha), before + 1)
        self.assertEqual(digest["extraction_policy_sha256"], folder.extraction_policy("xlsx", folder.load_config(output=str(self.out))))
        self.assertEqual(digest["counts"]["chartsheets_unsupported"], 1)
        self.assertGreater(len(list((self.out / "history" / sha).iterdir())), len(history_before))
        self.assertEqual((self.out / "blobs" / sha).read_bytes(), original)
        self.cli("extract")
        self.assertEqual(self.attempts(sha), before + 1)

    def test_old_xlsx_policy_and_missing_policy_reparse_once(self):
        def old(digest):
            digest["extraction_policy_sha256"] = self.old_policy()
        def missing(digest):
            digest.pop("extraction_policy_sha256")
        self.assertNotEqual(self.old_policy(), folder.extraction_policy("xlsx", {"excluded_path_fragments": []}))
        with self.subTest("old policy hash"):
            self.check_reparse_once(old)
        self.out = self.base / "second-output"
        with self.subTest("missing policy"):
            self.check_reparse_once(missing)

    def test_unchanged_current_policy_is_reused_and_other_formats_keep_their_hash(self):
        self.mixed_workbook(); self.cli(); db = self.db(); row, _ = self.doc(db)
        count = self.attempts(row["sha"]); self.cli("extract")
        self.assertEqual(self.attempts(row["sha"]), count)
        config = {"excluded_path_fragments": ["x", "a"]}
        formulas = {
            "txt": dict(format="txt", paragraph_ordinals=None, excluded_path_fragments=[]),
            "csv": dict(format="csv", paragraph_ordinals=None, csv_strict_records=1, excluded_path_fragments=[]),
            "docx": dict(format="docx", paragraph_ordinals=2, docx_text_separators=1, docx_altchunk_unsupported=1, excluded_path_fragments=[]),
            "zip": dict(format="zip", paragraph_ordinals=None, excluded_path_fragments=["a", "x"]),
            "pdf": dict(format="pdf", paragraph_ordinals=None, excluded_path_fragments=[]),
        }
        for form, expected in formulas.items():
            self.assertEqual(folder.extraction_policy(form, config), folder.hid(folder.js(expected)), form)
        self.assertNotIn("xlsx_sheet_inventory", folder.js(dict(format="docx")))

    def test_missing_policy_non_xlsx_is_still_grandfathered(self):
        (self.root / "a.txt").write_text("plain\n"); self.cli()
        db = self.db(); row = db.execute("SELECT * FROM docs WHERE format='txt'").fetchone()
        digest = json.loads(row["digest"]); digest.pop("extraction_policy_sha256")
        db.execute("UPDATE docs SET digest=? WHERE sha=?", (folder.js(digest), row["sha"])); db.commit()
        count = self.attempts(row["sha"]); self.cli("extract")
        self.assertEqual(self.attempts(row["sha"]), count)


if __name__ == "__main__":
    unittest.main()
