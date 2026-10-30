import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_requests.py"
FIXTURE = ROOT / "tests" / "fixtures" / "flock" / "requests-sample.csv"
QUESTIONS = FIXTURE.with_name("questions-sample.md")
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

    def test_matching_request_numbers_from_different_agencies_are_valid(self):
        header = FIXTURE.read_text(encoding="utf-8").splitlines()[0] + "\n"
        agencies = ("Example City Police", "Sample County Sheriff")
        rows = [f"26-001,{agency},2026-01-03,,,,,open,\n" for agency in agencies]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            combined = root / "REQUESTS.csv"
            combined.write_text(header + "".join(rows), encoding="utf-8")
            for folder, row in zip(("example-city-police", "sample-county-sheriff"), rows):
                path = root / "agencies" / folder / "REQUESTS.csv"
                path.parent.mkdir(parents=True)
                path.write_text(header + row, encoding="utf-8")
            for path in (combined, root):
                with self.subTest(path=path.name):
                    checked = run("--check", str(path))
                    self.assertEqual(checked.returncode, 0, checked.stdout)
                    self.assertIn("requests=2 errors=0", checked.stdout)
                    done = run("due", str(path))
                    self.assertEqual(done.returncode, 0, done.stderr)
                    lines = done.stdout.splitlines()
                    self.assertEqual(len(lines), 3)
                    for agency in agencies:
                        self.assertTrue(any(f"26-001 | {agency} | receipt unknown" in line for line in lines))
                    self.assertIn("open=2 receipt_unknown=2", lines[-1])

    def test_matching_request_numbers_within_one_agency_fail_closed(self):
        header = FIXTURE.read_text(encoding="utf-8").splitlines()[0] + "\n"
        row = "26-001,Example City Police,2026-01-03,,,,,open,\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            combined = root / "REQUESTS.csv"
            combined.write_text(header + row * 2, encoding="utf-8")
            agency_file = root / "agencies" / "example-city-police" / "REQUESTS.csv"
            agency_file.parent.mkdir(parents=True)
            agency_file.write_text(header + row * 2, encoding="utf-8")
            for path in (combined, root):
                with self.subTest(path=path.name):
                    checked = run("--check", str(path))
                    self.assertEqual(checked.returncode, 1)
                    self.assertIn("row 3: request_id", checked.stdout)
                    self.assertIn("requests=2 errors=1", checked.stdout)
                    done = run("due", str(path))
                    self.assertEqual(done.returncode, 1)
                    self.assertEqual(done.stdout, "")
                    self.assertIn("row 3: request_id", done.stderr)

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

    def draft(self, root, text=None):
        rundir = Path(root) / "agencies" / "sample-city-police-department" / "runs" / "2026-10-31"
        rundir.mkdir(parents=True, exist_ok=True)
        (rundir / "QUESTIONS.md").write_text(QUESTIONS.read_text(encoding="utf-8") if text is None else text, encoding="utf-8")
        done = run("draft", str(rundir), "--requests", str(FIXTURE), "--from", "2026-01-01", "--to", "2026-10-31")
        return done, rundir / "FOLLOWUP-DRAFT.md"

    def test_draft_asks_existing_records_and_holds_open_prior_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            done, out = self.draft(tmp)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.strip(), "items=2 held=2 written=FOLLOWUP-DRAFT.md")
            text = out.read_text(encoding="utf-8")
            lines = text.splitlines()
            self.assertEqual(lines[0], "DRAFT - not sent - G approval required")
            for want in ("existing records", "related to requests SAMPLE-4", "the period is 2026-01-01 through 2026-10-31",
                         "native format", "identify records withheld and the basis"):
                self.assertIn(want, text)
            self.assertEqual([x[:2] for x in lines if re.match(r"\d+\. ", x)], ["1.", "2."])
            end = next(n for n, x in enumerate(lines) if x.startswith("END OF REQUEST DRAFT"))
            self.assertNotIn("?", "\n".join(lines[:end]))
            self.assertIn("F-002 open SAMPLE-1; F-003 open SAMPLE-1", lines[-1])

    def test_draft_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = QUESTIONS.read_text(encoding="utf-8").splitlines()
            bad, out = self.draft(tmp, "\n".join(lines[:1] + ["- Q F-009 | Policy | prior - | Existing records showing why it was used."]) + "\n")
            self.assertEqual(bad.returncode, 1)
            self.assertFalse(out.exists())
            held, out = self.draft(tmp, "\n".join(lines[:1] + lines[2:4]) + "\n")
            self.assertEqual(held.returncode, 1)
            self.assertIn("no items to draft", held.stderr)
            self.assertFalse(out.exists())
            self.assertEqual(self.draft(tmp)[0].returncode, 0)
            before = out.read_bytes()
            self.assertEqual(self.draft(tmp)[0].returncode, 1)
            self.assertEqual(out.read_bytes(), before)
            self.assertEqual([p.name for p in out.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_draft_is_private_despite_stale_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            rundir = Path(tmp) / "agencies" / "sample-city-police-department" / "runs" / "2026-10-31"
            rundir.mkdir(parents=True)
            stale = rundir / ".FOLLOWUP-DRAFT.md.tmp"
            stale.write_text("stale", encoding="utf-8")
            stale.chmod(0o644)
            done, out = self.draft(tmp)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            self.assertEqual(stale.read_text(encoding="utf-8"), "stale")
            self.assertNotEqual(out.stat().st_ino, stale.stat().st_ino)


if __name__ == "__main__":
    unittest.main()
