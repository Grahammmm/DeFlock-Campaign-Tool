import builtins
import contextlib
import csv
import datetime
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openpyxl import Workbook

from campaign_tool.records import cli
from campaign_tool.records.agency_run import cross_logs as xl

HEAD = "file\tformat\trows\tmatched\tunresolved\tdup_looking\tfirst_date\tlast_date\tcase_fill\tdate_basis\tstatus"
FOOT = "# counts and categories only; no plate, case number or user name is printed"
CANARIES = ("ZZT0001", "jdoe-test", "CASE-9917", "Reason", "Plate")
CSV_HEAD = ["Name", "Org Name", "Search Time", "Case #", "Reason"]
CSV_ROWS = [
    ["jdoe-test", "Pismo Test PD", "2026-03-02T07:30:00Z", "CASE-9917", "inv"],
    ["jdoe-test", "pismo test pd.", "3/5/2026 7:30 AM", "N/A", "inv"],
    ["jdoe-test", "Pismo Test PD", "2026-03-02T07:30:00Z", "CASE-9917", "inv"],
    ["jdoe-test", "Other PD", "2026-03-02T09:00:00-08:00", "CASE-1", "inv"],
    ["jdoe-test", "", "2026-03-02T09:00:00Z", "", "inv"],
]


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = xl.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.aliases = self.tmp / "aliases.txt"
        self.aliases.write_text("# agency\nPismo Test PD\n")

    def csv(self, name, header, rows):
        with open(self.root / name, "w", newline="") as handle:
            w = csv.writer(handle)
            w.writerow(header)
            w.writerows(rows)

    def xlsx(self, name, sheets):
        book = Workbook()
        book.remove(book.active)
        for title, rows in sheets.items():
            ws = book.create_sheet(title)
            for row in rows:
                ws.append(row)
        book.save(self.root / name)

    def index(self, *rows, header="path\tkind\trole"):
        path = self.tmp / "evidence-index.tsv"
        path.write_text(header + "\n" + "".join(r + "\n" for r in rows))
        return str(path)

    def scan(self, index, ori="CA0400100", extra=()):
        args = ["--index", index, "--root", str(self.root), "--aliases", str(self.aliases), *extra]
        return run(*args, "--ori", ori) if ori else run(*args)

    def standard(self):
        self.csv("flock-audit.csv", CSV_HEAD, CSV_ROWS)
        self.xlsx("vig.xlsx", {
            "Searches": [
                ["User", "Agency", "Date Time", "ORI", "Case", "Plate"],
                ["jdoe-test", "Pismo Test PD", datetime.datetime(2026, 4, 1, 10, 0), "", "CASE-9917", "ZZT0001"],
                ["jdoe-test", "PISMO TEST POLICE", "2026-04-02T01:00:00Z", "ca0400100", "N/A", "ZZT0001"],
                ["jdoe-test", "Other PD", "2026-04-02T01:00:00Z", "CA9", "CASE-2", "ZZT0001"],
                ["jdoe-test", "", "2026-04-02T01:00:00Z", "", "", "ZZT0001"],
            ],
            "Notes": [["Foo", "Bar"], [1, 2]],
        })
        return self.index("flock-audit.csv\tcsv\tthird-party", "vig.xlsx\txlsx\tthird-party")


class TestCrossLogs(Base):
    def test_golden_counts_csv_and_xlsx(self):
        code, out, err = self.scan(self.standard())
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            HEAD,
            "flock-audit.csv\tcsv\t5\t3\t1\t1\t2026-03-01\t2026-03-05\t66.7%\t2/1/0\tok",
            "vig.xlsx#Searches\txlsx\t4\t2\t1\t0\t2026-04-01\t2026-04-01\t50.0%\t1/1/0\tok",
            "vig.xlsx#Notes\txlsx\t\t\t\t\t\t\t\t\tnot_a_log",
            "TOTAL\t\t9\t5\t2\t1\t2026-03-01\t2026-04-01\t60.0%\t3/2/0\tok",
            FOOT,
        ])

    def test_duplicate_looking_rows_counted_not_removed(self):
        self.csv("a.csv", CSV_HEAD, CSV_ROWS[:1] * 3)
        _, out, _ = self.scan(self.index("a.csv\tcsv\tthird-party"))
        self.assertIn("a.csv\tcsv\t3\t3\t0\t2\t", out)

    def test_unresolved_rows_counted(self):
        _, out, _ = self.scan(self.standard())
        self.assertIn("flock-audit.csv\tcsv\t5\t3\t1\t", out)  # Other PD is in rows only

    def test_ori_match_without_org_name(self):
        self.csv("a.csv", ["Org Name", "ORI", "Search Time"], [["Different", "ca0400100", "2026-03-02T12:00:00Z"]])
        _, out, _ = self.scan(self.index("a.csv\tcsv\tthird-party"))
        self.assertIn("a.csv\tcsv\t1\t1\t0\t0\t2026-03-02\t2026-03-02\tn/a\t1/0/0\tok", out)
        _, out, _ = self.scan(self.index("a.csv\tcsv\tthird-party"), ori=None)
        self.assertIn("a.csv\tcsv\t1\t0\t0\t", out)

    def test_pacific_date_conversion(self):
        cases = {
            "2026-03-02T07:30:00Z": ("2026-03-01", "conv"),
            "2026-03-02T07:30:00-08:00": ("2026-03-02", "conv"),
            "2026-03-09T02:30:00Z": ("2026-03-08", "conv"),
            "2026-03-09T07:30:00-07:00": ("2026-03-09", "conv"),
            "2026-03-09T02:30:00-07:00": ("2026-03-09", "conv"),
            "3/2/2026 7:30 AM": ("2026-03-02", "stated"),
            "n/a": ("", "none"),
        }
        for raw, want in cases.items():
            self.assertEqual(xl.pacific(raw), want, raw)
        self.assertEqual(xl.pacific(datetime.datetime(2026, 3, 2, 7, 30)), ("2026-03-02", "stated"))

    def test_case_fill_rate_and_no_case_column(self):
        self.csv("a.csv", CSV_HEAD, [CSV_ROWS[0], ["u", "Pismo Test PD", "2026-03-02T07:30:00Z", "N/A", ""]])
        self.csv("b.csv", ["Org Name", "Search Time"], [["Pismo Test PD", "2026-03-02T07:30:00Z"]])
        _, out, _ = self.scan(self.index("a.csv\tcsv\tx", "b.csv\tcsv\tx"))
        self.assertIn("\t50.0%\t", out.splitlines()[1])
        self.assertIn("\tn/a\t", out.splitlines()[2])

    def test_no_plate_case_or_user_strings_in_output(self):
        code, out, err = self.scan(self.standard())
        self.assertEqual(code, 0)
        for canary in CANARIES + ("Org Name", "Search Time", "Date Time"):
            self.assertNotIn(canary, out + err)

    def test_outside_root_never_opened(self):
        outside = self.tmp / "outside.csv"
        outside.write_text("\n".join(",".join(r) for r in [CSV_HEAD] + CSV_ROWS))
        os.symlink(outside, self.root / "link.csv")
        idx = self.index("../outside.csv\tcsv\tx", f"{outside}\tcsv\tx", "link.csv\tcsv\tx")
        opened = []
        real_open, real_os_open = builtins.open, os.open

        def spy(path, *a, **k):
            opened.append(str(path))
            return real_open(path, *a, **k)

        def spy_os(path, *a, **k):
            opened.append(str(path))
            return real_os_open(path, *a, **k)

        with mock.patch("builtins.open", spy), mock.patch("os.open", spy_os):
            code, out, _ = self.scan(idx)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("refused_outside_root"), 3)
        self.assertFalse([p for p in opened if "outside.csv" in p or "link.csv" in p])
        self.assertIn("TOTAL\t\t0\t0\t0\t0\t", out)

    def test_pdf_listed_not_parsed_and_lead_skipped(self):
        (self.root / "x.pdf").write_bytes(b"%PDF-1.4 ZZT0001")
        self.csv("lead.csv", CSV_HEAD, CSV_ROWS)
        _, out, _ = self.scan(self.index("x.pdf\tpdf\tx", "lead.csv\tcsv\tlead", "note.txt\ttxt\tx"))
        self.assertIn("x.pdf\tpdf\t\t\t\t\t\t\t\t\tpdf_not_parsed", out)
        self.assertNotIn("lead.csv", out)
        self.assertNotIn("note.txt", out)

    def test_not_a_log_and_ambiguous_headers(self):
        self.csv("a.csv", ["Org Name", "Case #"], [["Pismo Test PD", "CASE-9917"]])
        self.csv("b.csv", ["Org Name", "Agency", "Search Time"], [["Pismo Test PD", "x", "2026-03-02T07:30:00Z"]])
        _, out, _ = self.scan(self.index("a.csv\tcsv\tx", "b.csv\tcsv\tx"))
        self.assertTrue(out.splitlines()[1].endswith("not_a_log"))
        self.assertTrue(out.splitlines()[2].endswith("ambiguous_columns"))
        self.assertNotIn("Org Name", out)

    def test_bad_arguments_refused(self):
        good = self.index("a.csv\tcsv\tx")
        empty = self.tmp / "empty.txt"
        empty.write_text("# nothing\n")
        nopath = self.tmp / "nopath.tsv"
        nopath.write_text("location\tkind\n")
        base = ["--index", good, "--root", str(self.root), "--aliases", str(self.aliases)]
        bad = {
            "index": ["--index", str(self.tmp / "missing.tsv"), "--root", str(self.root), "--aliases", str(self.aliases)],
            "index2": ["--index", str(nopath), "--root", str(self.root), "--aliases", str(self.aliases)],
            "aliases": ["--index", good, "--root", str(self.root), "--aliases", str(empty)],
            "ori": base + ["--ori", "CA 04"],
            "root": ["--index", good, "--root", str(self.tmp / "none"), "--aliases", str(self.aliases)],
        }
        for name, argv in bad.items():
            code, out, err = run(*argv)
            self.assertEqual((code, out), (2, ""), name)
            self.assertRegex(err.strip(), r"^refused agency_cross_logs_(index|root|aliases|ori)$", name)

    def test_inputs_unchanged(self):
        idx = self.standard()
        files = [Path(idx)] + sorted(self.root.iterdir()) + [self.aliases]
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in files]
        self.scan(idx)
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in files])

    def test_cli_registered(self):
        self.assertEqual(cli.COMMANDS["agency-cross-logs"], ("agency_run.cross_logs", "main"))


if __name__ == "__main__":
    unittest.main()
