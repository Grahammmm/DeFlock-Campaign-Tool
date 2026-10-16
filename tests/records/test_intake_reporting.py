"""Synthetic report boundaries only; no native worker or extraction retries."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from campaign_tool.records.intake import folder


class IntakeReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name).resolve() / "out"
        self.out.mkdir()
        self.db = sqlite3.connect(self.out / "intake.sqlite")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript(folder.SCHEMA)
        self.db.execute("INSERT INTO meta VALUES('inventory_run','fixture')")

    def document(self, label, stage, issues):
        sha = folder.hid(label)
        digest = folder.js({"stage": stage, "issues": issues})
        self.db.execute("INSERT INTO docs VALUES(?,?,?,?,?,?,?,?)",
                        (sha, 1, "unknown", stage, "fixture-time", "not_reviewed", folder.VERSION, digest))
        self.db.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (label, sha, label, "synthetic-root", None, "null", "original", "{}", "fixture-time", "fixture-time"))
        self.db.execute("INSERT INTO seen VALUES('fixture',?,?)", (label, sha))
        artifact = self.out / "derived" / sha / "digest.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text(digest)
        self.db.commit()
        return sha, artifact

    def queue(self):
        return [json.loads(line) for line in (self.out / "all-agency-document-queue.jsonl").read_text().splitlines()]

    def test_cli_report_preserves_located_gaps_and_original_evidence(self):
        marker = "OSError: synthetic /private/synthetic-error-path"
        issue = {"code": "zip_member_error", "locator": {"member": "fixture.txt"}, "detail": marker}
        sha, artifact = self.document("partial", "partial", [issue])
        unsupported, _ = self.document("unsupported", "unsupported",
                                       [{"code": "unsupported_format", "locator": None, "detail": "unknown"}])
        before = artifact.read_bytes()
        database_before = [tuple(r) for r in self.db.execute("SELECT * FROM docs")]
        result = subprocess.run([sys.executable, "-B", str(Path(folder.__file__)), "report",
                                 "--output", str(self.out)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads((self.out / "coverage.json").read_text())
        gaps = {row["sha256"]: row for row in summary["extraction_gaps"]}
        self.assertEqual(summary["stages"], {"partial": 1, "unsupported": 1})
        self.assertEqual(summary["issues"], {"zip_member_error": 1, "unsupported_format": 1})
        self.assertEqual(gaps[sha]["document_id"], "sha256:" + sha)
        self.assertEqual((gaps[sha]["stage"], gaps[sha]["reason"], gaps[sha]["locator"]),
                         ("partial", "zip_member_error", issue["locator"]))
        self.assertEqual((gaps[unsupported]["stage"], gaps[unsupported]["reason"], gaps[unsupported]["locator"]),
                         ("unsupported", "unsupported_format", None))
        queue = {row["sha256"]: row for row in self.queue()}
        self.assertEqual(queue[sha]["issues"][0]["locator"], issue["locator"])
        self.assertEqual(queue[sha]["stage"], "partial")
        self.assertTrue(queue[sha]["issues"][0]["detail"])
        for path in [self.out / "coverage.json", self.out / "coverage.md",
                     self.out / "all-agency-document-queue.jsonl", *self.out.glob("reports/*.json")]:
            self.assertNotIn(marker, path.read_text())
        self.assertIn(sha, (self.out / "coverage.md").read_text())
        self.assertIn("fixture.txt", (self.out / "coverage.md").read_text())
        self.assertEqual(artifact.read_bytes(), before)
        self.assertEqual([tuple(r) for r in self.db.execute("SELECT * FROM docs")], database_before)

    def test_incomplete_without_issue_remains_actionable_and_complete_stays_complete(self):
        missing, _ = self.document("pending", "pending", [])
        complete, _ = self.document("complete", "complete", [])
        folder.report(self.db, self.out)
        summary = json.loads((self.out / "coverage.json").read_text())
        self.assertEqual(summary["extraction_gaps"], [{"document_id": "sha256:" + missing,
            "sha256": missing, "stage": "pending", "reason": "extraction_not_complete", "locator": None, "detail": ""}])
        self.assertEqual({r["sha256"]: r["stage"] for r in self.queue()}[complete], "complete")

    def test_multiple_locators_for_one_original_are_not_collapsed(self):
        issues = [{"code": "page_error", "locator": {"page": n}, "detail": "synthetic error"} for n in (2, 4)]
        sha, _ = self.document("pages", "partial", issues)
        folder.report(self.db, self.out)
        summary = json.loads((self.out / "coverage.json").read_text())
        self.assertEqual([r["locator"] for r in summary["extraction_gaps"]], [{"page": 2}, {"page": 4}])
        self.assertEqual({r["sha256"] for r in summary["extraction_gaps"]}, {sha})
        self.assertEqual(summary["issues"], {"page_error": 2})
