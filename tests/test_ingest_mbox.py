"""DF0137-05: `ingest --mbox MAILBOX --source-id ID` stores each message of a local mbox export like `--eml`.

The mailbox is stored first; each message gets a receipt `<mailbox>/message-<n>.eml` and a `contains` row to the
mailbox, and its attachments are linked to it exactly as `ingest --eml` links them. Every mailbox is synthetic
(example.org) and built in the test. Run from the repository root:
python3 -B -m unittest tests.test_ingest_mbox -v
"""

from email.message import EmailMessage
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
CANARY = "canary-mbox-5q"
HEADER_VALUES = (f"Synthetic reply subject {CANARY}", f"records-clerk-{CANARY}@example.org", "organizer-5q@example.org")
PDF, CSV = b"%PDF-1.4 synthetic\n\x00\xff binary\n", b"col_a,col_b\n1,2\n"
SEPARATOR = f"From records-clerk-{CANARY}@example.org Tue Nov  3 13:00:00 2026\n".encode()


def message(*attachments, body="Synthetic body text.\n"):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = HEADER_VALUES
    msg.set_content(body)
    for item in attachments:
        if isinstance(item, EmailMessage):
            msg.add_attachment(item)
        else:
            data, maintype, subtype, filename = item
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


def mbox(*messages):
    """Each message after its own `From ` separator line, followed by the blank line an mbox writer adds."""
    return b"".join(SEPARATOR + body + b"\n" for body in messages)


def three():
    inner = EmailMessage()
    inner["Subject"] = HEADER_VALUES[0]
    inner.set_content("Forwarded synthetic text.\n")
    inner.add_attachment(b"inner attachment bytes", maintype="application", subtype="pdf", filename="inner.pdf")
    return (message((PDF, "application", "pdf", "scan.pdf"), (CSV, "text", "csv", "scan.pdf")),
            message(body="Plain synthetic reply.\n"), message(inner))


def sha(data):
    return hashlib.sha256(data).hexdigest()


class IngestMboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-ingest-mbox-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root, self.work, self.inbox = self.base / "campaign", self.base / "work", self.base / "inbox"
        self.work.mkdir()
        self.inbox.mkdir()
        self.cli("init", "--name", "Synthetic Campaign", "--county", "Synthetic County", "--state", "ca")

    def cli(self, command, *arguments, returncode=0):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", command, "--directory", str(self.root), *map(str, arguments)],
            cwd=self.work, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, returncode, f"stdout={result.stdout[:2000]!r}; stderr={result.stderr!r}")
        self.assertNotIn("Traceback", result.stderr)
        for value in HEADER_VALUES + (CANARY,):  # edge 7: no header value on any path, success or refusal
            self.assertNotIn(value, result.stdout + result.stderr)
        return result

    def ingest(self, path, dry=False, returncode=0):
        result = self.cli("ingest", "--mbox", path, "--source-id", "synthetic-mbox-1", *(["--dry-run"] if dry else []),
                          returncode=returncode)
        if returncode:
            return result
        if dry:
            body, last = result.stdout.rstrip("\n").rsplit("\n", 1)
            self.assertEqual(last, "Dry run: nothing was written.")
            return json.loads(body)
        return json.loads(result.stdout)

    def write(self, data, name="replies.mbox"):
        path = self.inbox / name
        path.write_bytes(data)
        return path

    def files(self):
        return {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob("*") if p.is_file()}

    def rows(self, query):
        db = sqlite3.connect(self.root / "private" / "ledger.sqlite")
        try:
            return db.execute(query).fetchall()
        finally:
            db.close()

    def outcomes(self, report):
        return [report["mailbox"]["outcome"]] + [x for m in report["messages"] for x in
                                                 [m["outcome"]] + [a["outcome"] for a in m["attachments"]]]

    def test_three_messages_stored_with_attachments_linked_to_message_and_messages_to_mailbox(self):
        m1, m2, m3 = three()
        path = self.write(mbox(m1, m2, m3))
        before = self.files()
        report = self.ingest(path)
        box = sha(path.read_bytes())
        self.assertEqual(report["mailbox"]["sha256"], box)
        self.assertEqual([(m["number"], m["sha256"]) for m in report["messages"]], [(1, sha(m1)), (2, sha(m2)), (3, sha(m3))])
        self.assertEqual([[a["name"] for a in m["attachments"]] for m in report["messages"]],
                         [["scan.pdf", "scan-2.pdf"], [], ["attachment-1.eml"]])
        self.assertEqual((report["skipped"], report["count"], set(self.outcomes(report))), ([], 3, {"stored"}))
        forwarded = report["messages"][2]["attachments"][0]["sha256"]
        objects = self.root / "private" / "objects"
        self.assertEqual(len(os.listdir(objects)), 7)  # 1 mailbox + 3 messages + 3 attachments
        self.assertEqual([(objects / sha(m)).read_bytes() for m in (m1, m2, m3)], [m1, m2, m3])
        self.assertNotIn(sha(b"inner attachment bytes"), os.listdir(objects))
        self.assertIn(b'filename="inner.pdf"', (objects / forwarded).read_bytes())
        self.assertEqual(sorted(self.rows("SELECT * FROM contains")), sorted([
            (box, sha(m1), "message-1.eml"), (box, sha(m2), "message-2.eml"), (box, sha(m3), "message-3.eml"),
            (sha(m1), sha(PDF), "scan.pdf"), (sha(m1), sha(CSV), "scan-2.pdf"), (sha(m3), forwarded, "attachment-1.eml")]))
        self.assertEqual(sorted(r[0] for r in self.rows("SELECT original_name FROM receipts")), sorted([
            "replies.mbox", "replies.mbox/message-1.eml", "replies.mbox/message-1.eml/scan.pdf",
            "replies.mbox/message-1.eml/scan-2.pdf", "replies.mbox/message-2.eml", "replies.mbox/message-3.eml",
            "replies.mbox/message-3.eml/attachment-1.eml"]))
        created = {k for k in self.files() if k not in before}
        self.assertTrue(all(k.startswith("campaign/private/") for k in created), created)
        self.cli("status", "--check")

    def test_same_mailbox_again_is_all_present_and_adds_no_object(self):
        path = self.write(mbox(*three()))
        self.ingest(path)
        objects = sorted(os.listdir(self.root / "private" / "objects"))
        again = self.ingest(path)
        self.assertEqual(set(self.outcomes(again)), {"present"})
        self.assertEqual(sorted(os.listdir(self.root / "private" / "objects")), objects)

    def test_dry_run_same_shape_writes_nothing_and_matches_later_outcomes(self):
        path = self.write(mbox(*three()))
        before = self.files()
        preview = self.ingest(path, dry=True)
        self.assertEqual(self.files(), before)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(set(self.outcomes(preview)), {"would_store"})
        real = self.ingest(path)
        self.assertEqual(set(preview), set(real) | {"dry_run"})
        self.assertEqual([set(m) for m in preview["messages"]], [set(m) for m in real["messages"]])
        self.assertEqual([m["sha256"] for m in preview["messages"]], [m["sha256"] for m in real["messages"]])
        after = self.files()
        again = self.ingest(path, dry=True)
        self.assertEqual(self.files(), after)
        self.assertEqual(set(self.outcomes(again)), {"present"})

    def test_repeated_message_in_one_mailbox_is_present_in_dry_run_and_real_run(self):
        m1 = message(body="Repeated synthetic reply.\n")
        path = self.write(mbox(m1, m1))
        self.assertEqual([m["outcome"] for m in self.ingest(path, dry=True)["messages"]], ["would_store", "present"])
        self.assertEqual([m["outcome"] for m in self.ingest(path)["messages"]], ["stored", "present"])

    def test_symlink_directory_empty_file_and_non_mbox_refused_with_nothing_written(self):
        target = self.write(mbox(*three()))
        (self.inbox / "link.mbox").symlink_to(target)
        (self.inbox / "folder.mbox").mkdir()
        cases = {"link.mbox": "--mbox must be a regular, non-symlink file",
                 "folder.mbox": "--mbox must be a regular, non-symlink file",
                 "empty.mbox": "not an mbox file (no From line at the start); nothing was imported",
                 "hello.mbox": "not an mbox file (no From line at the start); nothing was imported"}
        self.write(b"", "empty.mbox")
        self.write(b"Hello\n" + mbox(*three()), "hello.mbox")
        for name, text in cases.items():
            with self.subTest(name):
                before = self.files()
                result = self.ingest(self.inbox / name, returncode=1)
                self.assertIn(f"Stopped: {text}", result.stderr)
                self.assertEqual(self.files(), before)
                self.assertFalse((self.root / "private" / "ledger.sqlite").exists())

    def test_mailbox_over_200_mib_refused_before_any_write(self):
        from campaign_tool import ingest_mbox
        self.assertEqual(ingest_mbox.MAX_MBOX_BYTES, 200 * 1024 * 1024)
        path = self.write(mbox(*three()))
        args = types.SimpleNamespace(mbox=str(path), source_id="synthetic-mbox-1", dry_run=False)
        before = self.files()
        with mock.patch.object(ingest_mbox, "MAX_MBOX_BYTES", len(path.read_bytes()) - 1):
            with self.assertRaisesRegex(ValueError, r"^Alpha limit: 200 MiB per mailbox; nothing was imported$"):
                ingest_mbox.ingest_mbox(args, self.root)
        self.assertEqual(self.files(), before)

    def test_501_messages_refused_whole_and_500_accepted(self):
        bodies = [message(body=f"Synthetic reply {n}.\n") for n in range(501)]
        path = self.write(mbox(*bodies), "many.mbox")
        before = self.files()
        result = self.ingest(path, returncode=1)
        self.assertIn("Stopped: many.mbox: more than 500 messages; nothing was imported", result.stderr)
        self.assertEqual(self.files(), before)
        fits = self.ingest(self.write(mbox(*bodies[:500]), "fits.mbox"), dry=True)
        self.assertEqual((fits["count"], len(fits["messages"]), fits["skipped"]), (500, 500, []))

    def test_oversized_empty_and_malformed_messages_skipped_by_number_others_stored(self):
        m1, m2, _ = three()
        big = b"Subject: synthetic\n\n" + b"x" * (50 * 1024 * 1024 + 1 - 20)
        self.assertEqual(len(big), 50 * 1024 * 1024 + 1)
        bad = b'Content-Type: multipart/mixed\r\n\r\n--B\r\nbody\r\n--B--\r\n'
        path = self.write(mbox(m1, big, m2) + SEPARATOR + b"\n" + mbox(bad))
        report = self.ingest(path)
        self.assertEqual(report["skipped"], [{"number": 2, "code": "too_large"}, {"number": 4, "code": "empty"},
                                             {"number": 5, "code": "malformed"}])
        self.assertEqual([(m["number"], m["sha256"], m["outcome"]) for m in report["messages"]],
                         [(1, sha(m1), "stored"), (3, sha(m2), "stored")])
        self.assertEqual(report["count"], 5)
        self.assertEqual(self.rows("SELECT count(*) FROM objects"), [(5,)])  # mailbox, 2 messages, 2 attachments

    def test_mboxo_ambiguity_only_line_start_from_splits_and_escaped_from_kept_raw(self):
        escaped = message(body="Reply follows.\n>From the clerk, with records.\nSent From the clerk office.\n")
        self.assertIn(b"\n>From the clerk", escaped)
        report = self.ingest(self.write(mbox(escaped, message(body="Second reply.\n"))))
        self.assertEqual(report["count"], 2)
        self.assertEqual((self.root / "private" / "objects" / report["messages"][0]["sha256"]).read_bytes(), escaped)
        # mboxo: an unescaped body line starting `From ` cannot be told from a separator, so it splits.
        unescaped = message(body="Reply follows.\nFrom the clerk, with records.\n")
        self.assertEqual(self.ingest(self.write(mbox(unescaped), "unescaped.mbox"), dry=True)["count"], 2)

    def test_header_values_never_in_receipt_names(self):
        self.ingest(self.write(mbox(*three())))
        names = " ".join(r[0] for r in self.rows("SELECT original_name FROM receipts"))
        for value in HEADER_VALUES + (CANARY,):
            self.assertNotIn(value, names)
        self.assertNotIn(CANARY, " ".join(r[0] for r in self.rows("SELECT member_path FROM contains")))


if __name__ == "__main__":
    unittest.main()
