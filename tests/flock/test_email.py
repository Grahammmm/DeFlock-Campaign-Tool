import ast
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.flock.approval_field import APPROVAL_LINE

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_email.py"
FIXTURES = ROOT / "tests" / "fixtures" / "flock"
SLUG = "sample-city-police-department"
FORBIDDEN = {"smtplib", "email", "campaign_tool", "runner", "socket", "http", "urllib", "subprocess", "requests"}


class EmailDraftTests(unittest.TestCase):
    def test_draft_writes_identical_files_once_and_needs_a_contact(self):
        with tempfile.TemporaryDirectory() as tmp:
            agency = Path(tmp) / "agencies" / SLUG
            rundir, drafts = agency / "runs" / "2026-10-31", Path(tmp) / "drafts"
            rundir.mkdir(parents=True)
            shutil.copy(FIXTURES / "followup-draft-sample.md", rundir / "FOLLOWUP-DRAFT.md")
            cmd = [sys.executable, "-B", str(TOOL), "draft", str(rundir), "--drafts", str(drafts)]
            self.assertEqual(subprocess.run(cmd, capture_output=True, text=True).returncode, 1)
            self.assertEqual([(rundir / "EMAIL-DRAFT.md").exists(), drafts.exists()], [False, False])
            shutil.copy(FIXTURES / "contact-sample.csv", agency / "CONTACT.csv")
            done = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual((done.returncode, done.stdout), (0, "written=2 to_domain=city.example\n"))
            first, second = rundir / "EMAIL-DRAFT.md", drafts / f"{SLUG}-2026-10-31-EMAIL-DRAFT.md"
            text = first.read_text(encoding="utf-8")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            lines = text.splitlines()
            self.assertEqual(lines[:2], ["DRAFT - not sent - G approval required", "To: records@city.example"])
            self.assertTrue(lines[2].startswith("Subject: CPRA request"))
            for want in ("Related prior requests: SAMPLE-4.", APPROVAL_LINE):
                self.assertIn(want, text)
            self.assertNotIn("G APPROVED", text)  # approval is a signed field, never a line to fill
            for gone in ("Held (internal", "END OF REQUEST DRAFT"):
                self.assertNotIn(gone, text)
            self.assertEqual(subprocess.run(cmd, capture_output=True, text=True).returncode, 1)

    def test_tool_has_no_send_path(self):
        source, imported = TOOL.read_text(encoding="utf-8"), set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imported |= {a.name.split(".")[0] for a in node.names} if isinstance(node, ast.Import) else {(node.module or "").split(".")[0]}
        self.assertEqual(imported & FORBIDDEN, set())
        for word in ("sendmail", "send_message", "outbox"):
            self.assertNotIn(word, source)
