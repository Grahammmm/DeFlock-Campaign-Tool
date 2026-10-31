"""flock_requests.py due PATH --as-of DATE: provisional distance to or since each reminder date; totals (RF1300-1)."""
import hashlib
import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_requests.py"
FIXTURE = ROOT / "tests" / "fixtures" / "flock" / "requests-sample.csv"
LEGAL = re.compile(r"\b(late|overdue)\b|violat", re.I)
AS_OF = date(2026, 10, 31)
SHA = hashlib.sha256(b"synthetic receipt").hexdigest()
spec = importlib.util.spec_from_file_location("_requests_rf1300", TOOL)
requests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(requests)
law = requests.law
PKG = law.load_package("us-ca")


def run(*args):
    return subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)


def receipt_for(target, extension=False):
    """A receipt date whose provisional determination date is exactly target."""
    for back in range(0, 60):
        receipt = target - timedelta(days=back)
        if law.deadline(PKG, receipt.isoformat(), extension=extension) == target:
            return receipt
    raise AssertionError(f"no receipt date reaches {target}")


def known(rid, offset, extension=False):
    when = AS_OF + timedelta(days=offset)
    receipt = receipt_for(when, extension)
    sent = (receipt - timedelta(days=1)).isoformat()
    ext = receipt.isoformat() if extension else ""
    return [rid, "Sample City Police Department", sent, "agency acknowledgment message", receipt.isoformat(), "", ext,
            "open", SHA]


def unknown(rid, sent):
    return [rid, "Example Town Records Office", sent.isoformat(), "", "", "", "", "open", ""]


class DueAsOfTests(unittest.TestCase):
    def write(self, rows, header=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "REQUESTS.csv"
        lines = [",".join(header or requests.HEADER)] + [",".join(r) for r in rows]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def due(self, rows, as_of=AS_OF.isoformat()):
        done = run("due", str(self.write(rows)), "--as-of", as_of)
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stderr)
        lines = done.stdout.splitlines()
        self.assertEqual([x for x in lines if LEGAL.search(x)], [])  # HOLIDAY_NOTE's "lateness" is not a match
        return lines

    def test_default_output_unchanged_without_as_of(self):
        rows, errs = requests.load(FIXTURE)
        self.assertEqual(errs, [])
        done = run("due", str(FIXTURE))
        self.assertEqual((done.returncode, done.stdout), (0, "\n".join(requests.due(rows)) + "\n"))
        self.assertNotIn("as of", done.stdout)
        self.assertNotIn("upcoming_7d", done.stdout)
        dated = run("due", str(FIXTURE), "--as-of", "2026-01-20").stdout.splitlines()
        plain = done.stdout.splitlines()
        self.assertEqual(len(dated), len(plain))
        for before, after in zip(plain[:-1], dated[:-1]):  # one cell appended, nothing else changed
            self.assertTrue(after.startswith(before + " | as of 2026-01-20: "), after)

    def test_upcoming_today_and_passed(self):
        lines = self.due([known("R-PLUS3", 3), known("R-TODAY", 0), known("R-MINUS2", -2)])
        self.assertEqual(len(lines), 4)
        order = [x.split(" | ")[1] for x in lines[:3]]
        self.assertEqual(order, ["R-MINUS2", "R-TODAY", "R-PLUS3"])  # sort order still by date
        self.assertTrue(lines[0].endswith(" | not a legal due date | as of 2026-10-31: reminder date passed 2 days ago"))
        self.assertTrue(lines[1].endswith(" | as of 2026-10-31: reminder date today"))
        self.assertTrue(lines[2].endswith(" | as of 2026-10-31: in 3 days"))
        self.assertIn(f"determination {AS_OF + timedelta(days=3)}", lines[2])
        self.assertTrue(lines[3].startswith(
            "provisional reminder | open=3 receipt_unknown=0 upcoming_7d=2 passed=1 as_of=2026-10-31 | "))
        self.assertTrue(lines[3].endswith(law.HOLIDAY_NOTE))

    def test_singular_day(self):
        lines = self.due([known("R-PLUS1", 1), known("R-MINUS1", -1)])
        self.assertTrue(lines[0].endswith(": reminder date passed 1 day ago"))
        self.assertTrue(lines[1].endswith(": in 1 day"))
        self.assertIn("upcoming_7d=1 passed=1", lines[2])

    def test_seven_day_boundary(self):
        self.assertIn("upcoming_7d=1 passed=0", self.due([known("R-PLUS7", 7)])[-1])
        lines = self.due([known("R-PLUS8", 8)])
        self.assertTrue(lines[0].endswith(": in 8 days"))
        self.assertIn("upcoming_7d=0 passed=0", lines[-1])

    def test_receipt_unknown_counts_from_sent_and_is_never_totalled(self):
        lines = self.due([unknown("U-BEFORE", AS_OF - timedelta(days=5)), unknown("U-AFTER", AS_OF + timedelta(days=2)),
                          unknown("U-SAME", AS_OF)])
        by_id = {x.split(" | ")[1]: x for x in lines[:-1]}
        self.assertTrue(by_id["U-BEFORE"].endswith(" | receipt unknown | no date computed | as of 2026-10-31: sent 5 days ago"))
        self.assertTrue(by_id["U-AFTER"].endswith(" | no date computed | as of 2026-10-31: sent after the as-of date"))
        self.assertTrue(by_id["U-SAME"].endswith(": sent 0 days ago"))
        self.assertIn("open=3 receipt_unknown=3 upcoming_7d=0 passed=0 as_of=2026-10-31", lines[-1])
        self.assertNotIn("determination", "\n".join(lines[:-1]))

    def test_extension_notice_uses_extended_date(self):
        row = known("R-EXT", 4, extension=True)
        extended = law.deadline(PKG, row[4], extension=True)
        self.assertNotEqual(extended, law.deadline(PKG, row[4]))
        lines = self.due([row])
        self.assertIn(f"determination {extended} | ", lines[0])
        self.assertIn("extension notice", lines[0])
        self.assertTrue(lines[0].endswith(": in 4 days"))
        self.assertIn("upcoming_7d=1 passed=0", lines[-1])

    def test_only_open_rows_print(self):
        done_row = known("R-DET", 2)[:5] + [(AS_OF - timedelta(days=1)).isoformat(), "", "determined", SHA]
        closed = known("R-CLOSED", -3)[:7] + ["closed", SHA]
        lines = self.due([done_row, closed, known("R-OPEN", 2)])
        self.assertEqual(len(lines), 2)
        self.assertNotIn("R-DET", "\n".join(lines))
        self.assertNotIn("R-CLOSED", "\n".join(lines))
        self.assertIn("open=1 receipt_unknown=0 upcoming_7d=1 passed=0", lines[-1])

    def test_bad_flag_use(self):
        path = str(self.write([known("R-1", 1)]))
        for bad in ("2026-13-01", "yesterday", "2026-1-01", "", "2026-10-31x"):
            with self.subTest(bad=bad):
                done = run("due", path, "--as-of", bad)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (2, "", "due: --as-of must be YYYY-MM-DD\n"))
        for args in (("summary", path, "--as-of", "2026-10-31"), ("due", "--as-of", "2026-10-31"),
                     ("--check", path, "--as-of", "2026-10-31"), ("due", path, "--as-of"),
                     ("due", "--as-of", "2026-10-31", path), ("due", path, "--from", "2026-10-31"),
                     ("due", path, "--as-of", "2026-10-31", "--as-of", "2026-10-30")):
            with self.subTest(args=args):
                done = run(*args)
                self.assertEqual((done.returncode, done.stdout), (2, ""))
                self.assertEqual(done.stderr, requests.USAGE + "\n")
        self.assertIn("due PATH [--as-of YYYY-MM-DD]", run("--help").stdout)

    def test_invalid_file_still_wins(self):
        bad = self.write([known("R-1", 1)], header=requests.HEADER[:-1] + ["sha"])
        done = run("due", str(bad), "--as-of", "2026-10-31")
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("row 1: header does not match REQUESTS.csv v1", done.stderr)
        row = known("R-2", 1)
        row[2] = "2026-99-01"
        done = run("due", str(self.write([row])), "--as-of", "2026-10-31")
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("row 2: sent must be YYYY-MM-DD", done.stderr)


if __name__ == "__main__":
    unittest.main()
