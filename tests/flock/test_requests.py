import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_requests.py"
FIXTURE = ROOT / "tests" / "fixtures" / "flock" / "requests-sample.csv"
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def run(*args):
    return subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)


class RequestsTrackerTests(unittest.TestCase):
    def test_happy_path_lists_only_open_rows_with_provisional_dates(self):
        self.assertEqual(run("--check", str(FIXTURE)).returncode, 0)
        done = run("due", str(FIXTURE))
        self.assertEqual(done.returncode, 0)
        lines = done.stdout.splitlines()
        self.assertTrue(all("provisional reminder" in line for line in lines))
        self.assertEqual(len(lines), 4)  # three open rows plus the summary line
        self.assertIn("SAMPLE-1", lines[0])
        self.assertIn("determination 2026-01-15", lines[0])
        self.assertIn("SAMPLE-2", lines[1])
        self.assertIn("determination 2026-01-29", lines[1])
        self.assertIn("extension notice", lines[1])
        self.assertIn("SAMPLE-3", lines[2])
        self.assertIn("receipt unknown", lines[2])
        self.assertIsNone(DATE.search(lines[2]))
        self.assertIn("open=3 receipt_unknown=1", lines[3])
        self.assertNotIn("SAMPLE-4", done.stdout)
        self.assertNotIn("SAMPLE-5", done.stdout)
        self.assertIsNone(re.search(r"\b(late|overdue)\b|violat", "\n".join(lines[:3]), re.I))

    def test_receipt_date_without_evidence_fails_closed(self):
        bad = "SAMPLE-6,Example Town Records Office,2026-01-03,,2026-01-05,,,open,\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "REQUESTS.csv"
            path.write_text(FIXTURE.read_text(encoding="utf-8") + bad, encoding="utf-8")
            checked = run("--check", str(path))
            self.assertEqual(checked.returncode, 1)
            self.assertIn("row 7: receipt_date", checked.stdout)
            self.assertIn("errors=1", checked.stdout)
            done = run("due", str(path))
            self.assertEqual(done.returncode, 1)
            self.assertEqual(done.stdout, "")
            self.assertIn("row 7", done.stderr)


if __name__ == "__main__":
    unittest.main()
