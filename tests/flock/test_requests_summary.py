"""flock_requests.py summary: one request-status line per agency, then totals; stdout only (RF1120-1)."""
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_requests.py"
FIXTURE = ROOT / "tests" / "fixtures" / "flock" / "requests-sample.csv"
HEADER = FIXTURE.read_text(encoding="utf-8").splitlines()[0]
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
BANNED = re.compile(r"\b(late|overdue)\b|violat|\bdue\b", re.I)
TAIL = " | not a legal status; the canonical ledger wins"
ZERO = "open=0 determined=0 produced=0 closed=0 withdrawn=0"
HEX = "c" * 64


def run(*args, cwd=None):
    return subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False, cwd=cwd)


def row(rid, agency, sent, status="open", receipt=False):
    ev, rdate, sha = ("agency acknowledgment message", sent, HEX) if receipt else ("", "", "")
    det = sent if status == "determined" else ""
    return ",".join((rid, agency, sent, ev, rdate, det, "", status, sha))


def counts(**n):
    return " ".join(f"{s}={n.get(s, 0)}" for s in ("open", "determined", "produced", "closed", "withdrawn"))


class RequestsSummaryTests(unittest.TestCase):
    def tmp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        return Path(t.name)

    def csv(self, *rows, path=None):
        path = path or self.tmp() / "REQUESTS.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join((HEADER, *rows)) + "\n", encoding="utf-8")
        return path

    def ok(self, path, cwd=None):
        done = run("summary", str(path), cwd=cwd)
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stderr)
        lines = done.stdout.splitlines()
        for line in lines:
            self.assertTrue(line.startswith("request status | "), line)
            self.assertIsNone(BANNED.search(line), line)
        self.assertTrue(lines[-1].endswith(TAIL), lines[-1])
        return lines

    def refused(self, path, message):
        done = run("summary", str(path))
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn(message, done.stderr)

    def test_golden_sample_fixture_sorted_by_slug(self):
        lines = self.ok(FIXTURE)
        self.assertEqual(lines, [
            f"request status | Example State Office | {counts(closed=1)} | receipt_unknown=0 | oldest_open_sent -",
            f"request status | Example Town Records Office | {counts(open=1)} | receipt_unknown=1 | oldest_open_sent 2026-01-03",
            f"request status | Sample City Police Department | {counts(open=1, determined=1)} | receipt_unknown=0"
            " | oldest_open_sent 2026-01-02",
            f"request status | Sample County Sheriff | {counts(open=1)} | receipt_unknown=0 | oldest_open_sent 2026-01-02",
            "request status | agencies=4 requests=5 open=3 receipt_unknown=1" + TAIL])
        self.assertNotIn("Gov. Code", "\n".join(lines))

    def test_store_dir_one_line_per_folder_and_oldest_open_sent_only(self):
        root = self.tmp()
        self.csv(row("26-001", "Alpha City Police", "2026-03-09", receipt=True),
                 row("26-002", "Alpha City Police", "2026-02-01"),  # oldest open, listed after a newer one
                 row("25-009", "Alpha City Police", "2025-01-01", "determined", receipt=True),  # older, not open
                 row("25-010", "Alpha City Police", "2024-06-01", "withdrawn"),
                 path=root / "agencies" / "alpha-city-police" / "REQUESTS.csv")
        self.csv(row("26-001", "Beta County Sheriff", "2026-01-20", "produced"),
                 row("26-003", "Beta County Sheriff", "2026-04-02"),
                 path=root / "agencies" / "beta-county-sheriff" / "REQUESTS.csv")
        before = sorted((str(p.relative_to(root)), p.stat().st_mtime_ns) for p in root.rglob("*"))
        lines = self.ok(root, cwd=root)
        self.assertEqual(lines, [
            f"request status | Alpha City Police | {counts(open=2, determined=1, withdrawn=1)} | receipt_unknown=1"
            " | oldest_open_sent 2026-02-01",
            f"request status | Beta County Sheriff | {counts(open=1, produced=1)} | receipt_unknown=1 | oldest_open_sent 2026-04-02",
            "request status | agencies=2 requests=6 open=3 receipt_unknown=2" + TAIL])
        after = sorted((str(p.relative_to(root)), p.stat().st_mtime_ns) for p in root.rglob("*"))
        self.assertEqual(after, before)  # no file written, none touched, not even in the working directory
        sent = {"2026-03-09", "2026-02-01", "2025-01-01", "2024-06-01", "2026-01-20", "2026-04-02"}
        self.assertLessEqual(set(DATE.findall("\n".join(lines))), sent)  # no computed date, only sent values

    def test_agency_with_no_open_rows(self):
        lines = self.ok(self.csv(row("1", "Gamma Office", "2026-01-02", "determined", receipt=True),
                                 row("2", "Gamma Office", "2026-01-03", "closed")))
        self.assertEqual(lines[0], f"request status | Gamma Office | {counts(determined=1, closed=1)} | receipt_unknown=0"
                                   " | oldest_open_sent -")
        self.assertIsNone(DATE.search("\n".join(lines)))
        self.assertEqual(lines[1], "request status | agencies=1 requests=2 open=0 receipt_unknown=0" + TAIL)

    def test_empty_store_refused(self):
        root = self.tmp()
        (root / "agencies" / "empty-folder").mkdir(parents=True)
        self.refused(root, "store: no agencies/*/REQUESTS.csv found")

    def test_one_bad_row_anywhere_prints_no_partial_table(self):
        root = self.tmp()
        self.csv(row("1", "Alpha City Police", "2026-01-02"), path=root / "agencies" / "alpha-city-police" / "REQUESTS.csv")
        self.csv(row("2", "Beta County Sheriff", "2026-01-02"), row("3", "Beta County Sheriff", "2026-01-03", "pending"),
                 path=root / "agencies" / "beta-county-sheriff" / "REQUESTS.csv")
        self.refused(root, "beta-county-sheriff: row 3: status must be one of")
        self.refused(self.csv(row("1", "Alpha City Police", "2026-01-02", "pending"), row("2", "Alpha City Police", "2026-01-03")),
                     "row 2: status must be one of")

    def test_header_only_file_prints_zero_totals(self):
        self.assertEqual(self.ok(self.csv()), ["request status | agencies=0 requests=0 open=0 receipt_unknown=0" + TAIL])

    def test_mixed_case_and_spacing_names_merge_with_first_seen_label(self):
        lines = self.ok(self.csv(row("1", "Sample City Police", "2026-02-02"), row("2", "SAMPLE  city-POLICE", "2026-01-05"),
                                 row("3", "sample city police", "2026-01-09", "closed")))
        self.assertEqual(lines, [
            f"request status | Sample City Police | {counts(open=2, closed=1)} | receipt_unknown=2 | oldest_open_sent 2026-01-05",
            "request status | agencies=1 requests=3 open=2 receipt_unknown=2" + TAIL])

    def test_help_lists_summary_and_bad_arguments_refused(self):
        done = run("--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("| summary PATH |", done.stdout)
        for args in (["summary"], ["summary", str(FIXTURE), "extra"], ["summary", "--from", str(FIXTURE)]):
            done = run(*args)
            self.assertEqual((done.returncode, done.stdout), (2, ""), args)
            self.assertTrue(done.stderr.startswith("usage: flock_requests.py"), args)


if __name__ == "__main__":
    unittest.main()
