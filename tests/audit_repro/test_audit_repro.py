"""Audit reproductions (2026-10-22). Synthetic data only.

Each test asserts the CORRECT behaviour, so on current main it FAILS and that
failure is the proof of the defect. They are skipped by default so the normal
offline suite stays green; run them with:

    AUDIT_REPRO=1 python3 -B -m unittest tests.audit_repro.test_audit_repro -v
"""
import argparse
import io
import json
import os
import pathlib
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ENABLED = os.environ.get("AUDIT_REPRO") == "1"
REPO = Path(__file__).resolve().parents[2]


@unittest.skipUnless(ENABLED, "set AUDIT_REPRO=1 to run audit reproductions")
class ImapTlsVerification(unittest.TestCase):
    """Critical: IMAP over TLS is opened without a verifying SSL context."""

    def test_imap_ssl_client_verifies_certificate(self):
        from campaign_tool.records.intake import imap_intake
        seen = {}

        class Fake:
            def __init__(self, host, port, **kwargs):
                seen.update(kwargs)
                # Mirror imaplib: no ssl_context -> stdlib's unverified context.
                self.ssl_context = kwargs.get("ssl_context") or ssl._create_stdlib_context()

            def login(self, user, password):
                pass

        with mock.patch.object(imap_intake.imaplib, "IMAP4_SSL", Fake):
            client = imap_intake.default_client_factory(
                {"host": "imap.example.invalid", "port": 993, "ssl": True, "timeout": 5,
                 "username": "synthetic", "password": "synthetic"})
        self.assertEqual(client.ssl_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(client.ssl_context.check_hostname)


@unittest.skipUnless(ENABLED, "set AUDIT_REPRO=1 to run audit reproductions")
class QuarantineOverreach(unittest.TestCase):
    """High: quarantining one container also quarantines unrelated containers sharing a child."""

    def test_shared_child_does_not_pull_in_unrelated_container(self):
        intake = REPO / "campaign_tool/records/intake"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); root = base / "originals"; root.mkdir(); out = base / "out"
            logo = b"\x89PNG synthetic shared logo bytes"

            def mkzip(path, members):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as z:
                    for name, data in members.items():
                        z.writestr(name, data)
                path.write_bytes(buf.getvalue())

            mkzip(root / "personal.zip", {"logo.png": logo, "note.txt": b"synthetic personal note"})
            mkzip(root / "records.zip", {"logo.png": logo, "contract.txt": b"synthetic in-scope contract"})
            run = lambda *a: subprocess.run([sys.executable, *map(str, a)], capture_output=True, text=True, check=True)
            run(intake / "folder.py", "run", "--root", root, "--output", out)
            run(intake / "scope_quarantine.py", "--output", out, root / "personal.zip")
            db = sqlite3.connect(out / "intake.sqlite")
            try:
                (sha,), = db.execute("SELECT sha FROM occurrences WHERE path=?", (str(root / "records.zip"),)).fetchall()
                excluded = db.execute("SELECT 1 FROM scope_exclusions WHERE sha=?", (sha,)).fetchone()
            finally:
                db.close()
            self.assertIsNone(excluded, "unrelated in-scope container was quarantined")
            self.assertTrue((out / "blobs" / sha).exists(), "unrelated container blob was moved out")


@unittest.skipUnless(ENABLED, "set AUDIT_REPRO=1 to run audit reproductions")
class VisionOutputAcceptedAsText(unittest.TestCase):
    """High: any non-blank vision reply (refusal, truncated <think>) becomes resolved page text."""

    def run_case(self, vision_text):
        from campaign_tool.records import ocr_stage as stage
        from tests.records.ocr_stage_fixtures import FakeTools, FakeVision, scanned_pdf, noise_page
        from tests.records.test_ocr_stage import Workspace, fake_ocrmypdf, results
        ws = Workspace(self)
        doc = ws.add("doc.pdf", scanned_pdf([noise_page(0)]))
        ws.write_queue()
        cfg = ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp), vision={"endpoint": "http://127.0.0.1:8003/v1/x"})
        stage.run(ws.args(cfg), runner=FakeTools({doc: [""]}), transport=FakeVision(text=vision_text))
        return results(ws.out)[doc]

    def test_refusal_is_not_complete(self):
        rec = self.run_case("I'm sorry, I can't read this image.")
        self.assertNotEqual(rec["status"], "ocr_complete")

    def test_unterminated_think_is_not_complete(self):
        rec = self.run_case("<think>The user wants a transcription. Let me look at the image... it seems")
        self.assertNotEqual(rec["status"], "ocr_complete")


@unittest.skipUnless(ENABLED, "set AUDIT_REPRO=1 to run audit reproductions")
class InboxSymlinkAbortsRun(unittest.TestCase):
    """Medium: one symlinked .eml aborts the whole manual inbox ingest."""

    def test_symlink_is_recorded_as_failure_not_raised(self):
        from campaign_tool.records import run as R
        eml = (b"From: a@example.invalid\r\nTo: b@example.invalid\r\nSubject: synthetic\r\n"
               b"Message-ID: <x@example.invalid>\r\nDate: Mon, 1 Jan 2024 00:00:00 +0000\r\n\r\nhello\r\n")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); inbox = tmp / "inbox"; inbox.mkdir()
            (inbox / "a.eml").write_bytes(eml)
            (tmp / "elsewhere.eml").write_bytes(eml.replace(b"hello", b"other"))
            os.symlink(tmp / "elsewhere.eml", inbox / "b_link.eml")
            report = R.Pipeline(tmp / "root").ingest_inbox(inbox)
            self.assertEqual([f["file"] for f in report["failures"]], ["b_link.eml"])
            self.assertEqual(len(report["preserved"]), 1)


@unittest.skipUnless(ENABLED, "set AUDIT_REPRO=1 to run audit reproductions")
class DigestGatesDropsInProgress(unittest.TestCase):
    """Medium: in_progress gated slots vanish from the denominator and the stage says ok."""

    def test_in_progress_is_not_ok(self):
        from campaign_tool.records import run_stages as S
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "processing-v1"; proc.mkdir()
            con = sqlite3.connect(proc / "ledger.sqlite")
            con.execute("CREATE TABLE stage_state(original_sha256 TEXT, stage TEXT, status TEXT)")
            con.execute("CREATE TABLE proposals(id TEXT)")
            for i in range(3):
                for st in ("catalog", "detect", "review", "compare", "privacy"):
                    con.execute("INSERT INTO stage_state VALUES(?,?,?)",
                                ("%064x" % i, st, "in_progress" if i == 2 else "done"))
            con.commit(); con.close()
            line, code = S.stage_digest_gates(argparse.Namespace(project=tmp))
            self.assertEqual(line["counts"]["input"], 15)
            self.assertNotEqual(line["status"], "ok")


if __name__ == "__main__":
    unittest.main()
