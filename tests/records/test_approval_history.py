"""Synthetic, read-only draft history from the existing approval journal."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq
from tests.approval_signing import Signer


def digest(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.body = "Original café\nKeep this line.\n"
        self.id = aq.add_draft(self.root, item_id="item-1", kind="reply-extension", agency_id="alpha-pd",
                               request_id="request-1", body=self.body, evidence={}, source_run_id="run-1")["id"]
        self.journal = self.root / "approvals/queue.jsonl"

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() if p.is_file() else None
                for p in self.root.rglob("*")}

    def history(self, ident=None, error=None):
        before = self.snapshot()
        try:
            if error:
                with self.assertRaises(aq.ApprovalQueueError) as caught:
                    aq.history(self.root, ident or self.id)
                self.assertEqual(caught.exception.code, error)
                return None
            return aq.history(self.root, ident or self.id)
        finally:
            self.assertEqual(self.snapshot(), before)

    def test_no_edit_and_duplicate_draft(self):
        aq.add_draft(self.root, item_id="item-1", kind="reply-extension", agency_id="alpha-pd",
                     request_id="request-1", body="ignored duplicate", evidence={}, source_run_id="run-2")
        h = self.history()
        self.assertFalse(h["changed"])
        self.assertEqual((h["diff"], h["edits"], h["diff_truncated"]), ([], [], False))
        for entry in (h["original"], h["current"]):
            self.assertEqual(entry["sha256"], digest(self.body))
            self.assertEqual(entry["body"], self.body)
            self.assertEqual(entry["bytes"], len(self.body.encode("utf-8")))
        self.assertEqual(h["original"]["actor"], aq.PIPELINE_ACTOR)
        self.assertEqual((h["revision"], h["current"]["revision"], h["status"]), (0, 0, "pending"))

    def test_two_edits_and_other_draft_isolation(self):
        other = aq.add_draft(self.root, item_id="item-2", kind="reply-extension", agency_id="alpha-pd",
                             request_id="request-2", body="Other draft", evidence={}, source_run_id="run-1")
        aq.edit(self.root, other["id"], "other", "Other edit")
        for n, body in enumerate(("First edit\nKeep this line.\n", "Second edit\nKeep this line.\n"), 1):
            with mock.patch.object(aq, "_now", return_value=f"2026-11-06T20:00:0{n}Z"):
                aq.edit(self.root, self.id, f"editor-{n}", body)
        h = self.history()
        self.assertTrue(h["changed"])
        self.assertEqual([e["revision"] for e in h["edits"]], [1, 2])
        for n, e in enumerate(h["edits"], 1):
            self.assertEqual(e["actor"], f"editor-{n}")
            self.assertEqual(e["at"], f"2026-11-06T20:00:0{n}Z")
            self.assertEqual(e["sha256"], digest(e["body"]))
        self.assertEqual(h["current"]["sha256"], digest("Second edit\nKeep this line.\n"))
        self.assertEqual(h["diff"], ["--- original", "+++ revision 2", "@@ -1,2 +1,2 @@",
                                     "-Original café", "+Second edit", " Keep this line."])

    def test_approval_rejection_and_stale_approval_do_not_become_edits(self):
        signer = Signer()
        self.addCleanup(signer.close)
        aq.edit(self.root, self.id, "editor", "Revised")
        with signer.trust():
            actor = {"kind": "ssh", "signature": signer.sign(aq.approval_payload(self.root, self.id))}
            aq.approve(self.root, self.id, actor)
            h = self.history()
            self.assertEqual((h["status"], h["revision"], h["current"]["revision"]), ("approved", 2, 1))
            self.assertEqual(len(h["edits"]), 1)
            aq.edit(self.root, self.id, "editor", self.body)
            self.assertFalse(self.history()["changed"])
            with self.assertRaises(aq.ApprovalQueueError):
                aq.approve(self.root, self.id, actor)
            aq.reject(self.root, self.id, "reviewer", "Cancelled draft")
            h = self.history()
            self.assertEqual((h["status"], h["revision"], h["current"]["revision"]), ("rejected", 4, 3))
            self.assertEqual([e["revision"] for e in h["edits"]], [1, 3])

    def test_diff_cap(self):
        aq.edit(self.root, self.id, "editor", "\n".join(f"New {n}" for n in range(1000)))
        h = self.history()
        self.assertEqual(len(h["diff"]), 400)
        self.assertTrue(h["diff_truncated"])

    def test_missing_store_unknown_id_and_malformed_journal(self):
        self.history("unknown", error="unknown_id")
        empty = self.root / "empty"
        empty.mkdir()
        before = self.snapshot()
        with self.assertRaises(aq.ApprovalQueueError) as caught:
            aq.history(empty, self.id)
        self.assertEqual(caught.exception.code, "unknown_id")
        self.assertEqual(self.snapshot(), before)
        aq.edit(self.root, self.id, "editor", "New")
        events = [json.loads(line) for line in self.journal.read_text().splitlines()]
        for bad in (123, "\ud800"):
            events[-1]["body"] = bad
            self.journal.write_text("".join(json.dumps(e) + "\n" for e in events))
            self.history(error="queue_invalid")
        self.journal.write_bytes(b'{"truncated":')
        self.history(error="queue_invalid")

    def test_cli_json_and_unknown_id(self):
        before = self.snapshot()
        command = [sys.executable, "-B", "-m", "campaign_tool.records", "approvals", "history",
                   "--project", str(self.root), "--id", self.id]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.history())
        result = subprocess.run(command[:-1] + ["unknown"], capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stderr.strip()), (1, "unknown_id"))
        self.assertEqual(self.snapshot(), before)
