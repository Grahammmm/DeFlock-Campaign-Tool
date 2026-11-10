"""DF0137-05: `ingest --mbox MAILBOX --source-id ID` stores each message of a local mbox export like `--eml`.

The mailbox is stored first; each message gets a receipt `<mailbox>/message-<n>.eml` and a `contains` row to the
mailbox, and its attachments are linked to it exactly as `ingest --eml` links them. Every mailbox is synthetic
(example.org) and built in the test. Run from the repository root:
python3 -B -m unittest tests.test_ingest_mbox -v
"""

from email.message import EmailMessage
import base64
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


VALID_FIRST = b"first valid attachment bytes"


def bad_message():
    """A valid base64 attachment followed by one with a missing pad."""
    part = ("--B\r\nContent-Type: application/octet-stream\r\nContent-Transfer-Encoding: base64\r\n"
            'Content-Disposition: attachment; filename="{}.bin"\r\n\r\n{}\r\n')
    head = (f"Subject: {HEADER_VALUES[0]}\r\nFrom: {HEADER_VALUES[1]}\r\nTo: {HEADER_VALUES[2]}\r\nMIME-Version: 1.0\r\n"
            'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n')
    first = base64.b64encode(VALID_FIRST).decode()
    return (head + part.format("ok", first) + part.format("bad", "AP8BAgM") + "--B--\r\n").encode()


def deep(depth, leaf="leaf"):
    """A complete message of `depth` nested multiparts around one text leaf; each level has its own boundary `D<n>`,
    opened and closed (RF1355-1: deeper than the stdlib email parser can follow at depth 1200)."""
    part = f"Content-Type: text/plain\r\n\r\n{leaf}\r\n"
    for n in range(depth):
        part = f'Content-Type: multipart/mixed; boundary="D{n}"\r\n\r\n--D{n}\r\n{part}\r\n--D{n}--\r\n'
    return (f"Subject: {HEADER_VALUES[0]}\r\nFrom: {HEADER_VALUES[1]}\r\nMIME-Version: 1.0\r\n" + part).encode()


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

    # RF0200-1: a malformed attachment transfer encoding skips only its own message
    def counts(self):
        return self.rows("SELECT (SELECT count(*) FROM objects), (SELECT count(*) FROM receipts), "
                         "(SELECT count(*) FROM contains)")

    def test_mbox_bad_encoding_skips_only_its_message(self):
        m1, m3 = message((PDF, "application", "pdf", "a.pdf")), message((CSV, "text", "csv", "b.csv"))
        data = mbox(m1, bad_message(), m3)
        path = self.write(data)
        report = self.ingest(path)
        self.assertEqual(report["skipped"], [{"number": 2, "code": "malformed"}])
        self.assertEqual([(m["number"], m["sha256"], m["outcome"]) for m in report["messages"]],
                         [(1, sha(m1), "stored"), (3, sha(m3), "stored")])
        self.assertEqual(report["count"], 3)
        self.assertEqual((self.root / "private" / "objects" / report["mailbox"]["sha256"]).read_bytes(), data)
        self.assertNotIn(sha(VALID_FIRST), set(os.listdir(self.root / "private" / "objects")))
        # mailbox + 2 messages + 2 attachments; 2 message rows + 2 attachment rows
        self.assertEqual(self.counts(), [(5, 5, 4)])
        self.assertEqual(sorted(r[0] for r in self.rows("SELECT original_name FROM receipts")),
                         sorted(["replies.mbox", "replies.mbox/message-1.eml", "replies.mbox/message-1.eml/a.pdf",
                                 "replies.mbox/message-3.eml", "replies.mbox/message-3.eml/b.csv"]))
        every = self.write(mbox(bad_message(), bad_message()), "every.mbox")
        report = self.ingest(every)
        self.assertEqual((report["skipped"], report["messages"], report["count"]),
                         ([{"number": 1, "code": "malformed"}, {"number": 2, "code": "malformed"}], [], 2))
        self.assertEqual((self.root / "private" / "objects" / report["mailbox"]["sha256"]).read_bytes(), every.read_bytes())

    def test_mbox_bad_encoding_dry_run_and_reingest_match(self):
        path = self.write(mbox(message((PDF, "application", "pdf", "a.pdf")), bad_message(), bad_message(),
                               message((CSV, "text", "csv", "b.csv"))))
        before = self.files()
        preview = self.ingest(path, dry=True)
        self.assertEqual(self.files(), before)
        real = self.ingest(path)
        shape = lambda r: (r["skipped"], [(m["number"], m["sha256"]) for m in r["messages"]], r["count"])
        self.assertEqual(shape(preview), shape(real))
        self.assertEqual(real["skipped"], [{"number": 2, "code": "malformed"}, {"number": 3, "code": "malformed"}])
        stored = self.counts()
        again = self.ingest(path)
        self.assertEqual(shape(again), shape(real))
        self.assertEqual(set(self.outcomes(again)), {"present"})
        self.assertEqual(self.counts(), stored)

    # RF1410-13: LF and CRLF framing; expected bytes are written out, never produced by the splitter
    def test_split_lf_crlf_and_mixed_framing_preserves_exact_messages(self):
        from campaign_tool.ingest_mbox import _split
        lf_sep, crlf_sep = b"From a@example.org Tue\n", b"From a@example.org Tue\r\n"

        def cut(data):
            return [data[s:e] for s, e in _split(data, "x.mbox")]
        self.assertEqual(cut(lf_sep + b"H: 1\n\nbody\n\n"), [b"H: 1\n\nbody\n"])
        self.assertEqual(cut(crlf_sep + b"H: 1\r\n\r\nbody\r\n\r\n"), [b"H: 1\r\n\r\nbody\r\n"])
        self.assertEqual(cut(crlf_sep + b"A\r\n\r\n" + crlf_sep + b"B\r\n\r\n"), [b"A\r\n", b"B\r\n"])
        # mixed: CRLF separator with LF message and LF framing, LF separator with CRLF framing
        self.assertEqual(cut(crlf_sep + b"A\nB\r\nC\n\n" + lf_sep + b"D\r\n\r\n"), [b"A\nB\r\nC\n", b"D\r\n"])
        # only one framing ending is removed; earlier blank lines stay
        self.assertEqual(cut(crlf_sep + b"A\r\n\r\n\r\n"), [b"A\r\n\r\n"])
        self.assertEqual(cut(lf_sep + b"A\n\n\n"), [b"A\n\n"])
        # no framing newline kept; a bare CR is never removed; a final single CRLF is the framing
        self.assertEqual(cut(crlf_sep + b"A"), [b"A"])
        self.assertEqual(cut(crlf_sep + b"A\r"), [b"A\r"])
        self.assertEqual(cut(crlf_sep + b"A\r\n"), [b"A"])
        self.assertEqual(cut(lf_sep + b"A\r\n\r"), [b"A\r\n\r"])
        # >From kept; inline From does not split; line-start From (mboxo) does
        self.assertEqual(cut(crlf_sep + b">From x\r\nSent From y\r\n\r\n"), [b">From x\r\nSent From y\r\n"])
        self.assertEqual(len(cut(crlf_sep + b"A\r\nFrom z\r\nB\r\n\r\n")), 2)

    def test_empty_crlf_message_skipped_without_derived_object(self):
        good = message(body="Synthetic CRLF body.\n").replace(b"\n", b"\r\n")
        for sep, frame in ((b"\n", b"\n"), (b"\r\n", b"\r\n")):
            with self.subTest(frame=frame):
                head = b"From records@example.org Tue" + sep
                data = head + frame + head + good + frame + head + frame + head + good + frame + head + frame
                path = self.write(data, f"empty{len(frame)}.mbox")
                report = self.ingest(path)
                self.assertEqual(report["skipped"], [{"number": n, "code": "empty"} for n in (1, 3, 5)])
                self.assertEqual((report["count"], [m["number"] for m in report["messages"]]), (5, [2, 4]))
                self.assertEqual([m["sha256"] for m in report["messages"]], [sha(good)] * 2)
        objects = os.listdir(self.root / "private" / "objects")
        self.assertNotIn(sha(b"\r"), objects)
        self.assertNotIn(sha(b""), objects)
        self.assertEqual(len(objects), 3)  # two mailboxes and one message
        self.assertEqual(self.rows("SELECT count(*) FROM contains"), [(4,)])

    def crlf_three(self):
        return [m.replace(b"\n", b"\r\n") for m in three()]

    def crlf_mbox(self, *messages):
        sep = SEPARATOR[:-1] + b"\r\n"
        return b"".join(sep + body + b"\r\n" for body in messages)

    def test_crlf_cli_hash_receipt_contains_and_replay(self):
        m1, m2, m3 = self.crlf_three()
        data = self.crlf_mbox(m1, m2, m3)
        path = self.write(data)
        report = self.ingest(path)
        box = sha(data)
        self.assertEqual(report["mailbox"]["sha256"], box)
        self.assertEqual([(m["number"], m["sha256"]) for m in report["messages"]], [(1, sha(m1)), (2, sha(m2)), (3, sha(m3))])
        objects = self.root / "private" / "objects"
        self.assertEqual((objects / box).read_bytes(), data)
        self.assertEqual([(objects / sha(m)).read_bytes() for m in (m1, m2, m3)], [m1, m2, m3])
        forwarded = report["messages"][2]["attachments"][0]["sha256"]
        self.assertEqual(sorted(self.rows("SELECT * FROM contains")), sorted([
            (box, sha(m1), "message-1.eml"), (box, sha(m2), "message-2.eml"), (box, sha(m3), "message-3.eml"),
            (sha(m1), sha(PDF), "scan.pdf"), (sha(m1), sha(CSV), "scan-2.pdf"), (sha(m3), forwarded, "attachment-1.eml")]))
        stored = (self.counts(), sorted(os.listdir(objects)))
        again = self.ingest(path)
        self.assertEqual(set(self.outcomes(again)), {"present"})
        self.assertEqual((self.counts(), sorted(os.listdir(objects))), stored)

    def test_crlf_dry_run_and_refusals_preserve_private_tree(self):
        m1, m2, _ = self.crlf_three()
        data = self.crlf_mbox(m1, bad_message(), m2)
        path = self.write(data)
        before = self.files()
        preview = self.ingest(path, dry=True)
        self.assertEqual(self.files(), before)
        self.assertEqual(preview["skipped"], [{"number": 2, "code": "malformed"}])
        real = self.ingest(path)
        self.assertEqual([m["sha256"] for m in preview["messages"]], [m["sha256"] for m in real["messages"]])
        self.assertEqual([m["sha256"] for m in real["messages"]], [sha(m1), sha(m2)])
        self.assertEqual(real["skipped"], preview["skipped"])
        after = self.files()
        self.assertEqual(self.ingest(path, dry=True)["count"], 3)
        self.assertEqual(self.files(), after)
        refused = self.write(b"Hello\r\n" + data, "hello-crlf.mbox")
        self.ingest(refused, returncode=1)
        self.assertEqual(self.files(), after | {str(refused.relative_to(self.base)): refused.read_bytes()})

    # RF0824-2: an ambiguous forwarded-message transfer encoding skips only its own message
    def forwarded(self, header, named=True, valid_first=False):
        """A message with a forwarded message/rfc822 part whose own Content-Transfer-Encoding header lines are
        `header` (a string or a list, one line each); an optional valid base64 attachment comes before it."""
        lines = [header] if isinstance(header, str) else header
        first = ("--B\r\nContent-Type: application/octet-stream\r\nContent-Transfer-Encoding: base64\r\n"
                 'Content-Disposition: attachment; filename="ok.bin"\r\n\r\n'
                 f"{base64.b64encode(VALID_FIRST).decode()}\r\n") if valid_first else ""
        wrapper = ("--B\r\nContent-Type: message/rfc822\r\n" + "".join(f"Content-Transfer-Encoding: {v}\r\n" for v in lines)
                   + ('Content-Disposition: attachment; filename="fwd.eml"\r\n' if named else "")
                   + "\r\nSubject: Fictional forward\r\n\r\nforwarded body\r\n")
        head = (f"Subject: {HEADER_VALUES[0]}\r\nFrom: {HEADER_VALUES[1]}\r\nTo: {HEADER_VALUES[2]}\r\nMIME-Version: 1.0\r\n"
                'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n')
        return (head + first + wrapper + "--B--\r\n").encode()

    def unique(self, label):
        """A valid message with one attachment whose bytes exist nowhere else in this test's campaign."""
        return message((PDF + label.encode(), "application", "pdf", f"{len(label)}.pdf"))

    def test_mbox_forwarded_wrapper_skips_only_bad_messages(self):
        bad_headers = {"unknown": "x-uuencode", "empty": "", "duplicate-equal": ["7bit", "7bit"],
                       "duplicate-conflicting": ["7bit", "base64"], "base64": "base64", "quoted-printable": "quoted-printable"}
        self.ingest(self.write(mbox(self.unique("seed")), "seed.mbox"))  # a populated campaign
        for crlf in (False, True):
            for label, header in bad_headers.items():
                for named in (True, False):
                    with self.subTest(label=label, named=named, crlf=crlf):
                        tag = f"{label}-{named}-{crlf}"
                        m1, m3 = self.unique(f"a-{tag}"), self.unique(f"c-{tag}")
                        bad = self.forwarded(header, named, valid_first=True)
                        if crlf:
                            m1, m3 = m1.replace(b"\n", b"\r\n"), m3.replace(b"\n", b"\r\n")
                            data = self.crlf_mbox(m1, bad, m3)
                        else:
                            data = mbox(m1, bad, m3)
                        path = self.write(data, f"{tag}.mbox")
                        before, counts = self.files(), self.counts()
                        report = self.ingest(path)
                        self.assertEqual(report["skipped"], [{"number": 2, "code": "malformed"}])
                        self.assertEqual([(m["number"], m["sha256"]) for m in report["messages"]],
                                         [(1, sha(m1)), (3, sha(m3))])
                        self.assertEqual(report["count"], 3)
                        objects = self.root / "private" / "objects"
                        self.assertEqual((objects / report["mailbox"]["sha256"]).read_bytes(), data)
                        self.assertEqual(path.read_bytes(), data)
                        self.assertNotIn(sha(bad), os.listdir(objects))
                        self.assertNotIn(sha(VALID_FIRST), os.listdir(objects))
                        # mailbox + 2 messages + 2 attachments; 2 message rows + 2 attachment rows
                        self.assertEqual([b - a for a, b in zip(counts[0], self.counts()[0])], [5, 5, 4])
                        names = [r[0] for r in self.rows("SELECT original_name FROM receipts")]
                        self.assertFalse([n for n in names if n.startswith(f"{tag}.mbox/message-2")], names)
                        self.assertTrue(set(before) <= set(self.files()))
        every = self.write(mbox(self.forwarded("x-uuencode"), self.forwarded(["7bit", "7bit"], named=False)), "all-bad.mbox")
        report = self.ingest(every)
        self.assertEqual((report["skipped"], report["messages"], report["count"]),
                         ([{"number": 1, "code": "malformed"}, {"number": 2, "code": "malformed"}], [], 2))
        self.assertEqual((self.root / "private" / "objects" / report["mailbox"]["sha256"]).read_bytes(), every.read_bytes())

    def test_mbox_forwarded_wrapper_dry_run_and_replay(self):
        good_forward = self.forwarded("8bit", valid_first=True)
        path = self.write(mbox(self.unique("x"), self.forwarded("x-uuencode", valid_first=True), good_forward,
                               self.forwarded("base64", named=False), self.unique("y")))
        before = self.files()
        preview = self.ingest(path, dry=True)
        self.assertEqual(self.files(), before)
        self.assertEqual(preview["skipped"], [{"number": 2, "code": "malformed"}, {"number": 4, "code": "malformed"}])
        real = self.ingest(path)
        shape = lambda r: (r["skipped"], [(m["number"], m["sha256"]) for m in r["messages"]], r["count"])
        self.assertEqual(shape(preview), shape(real))
        self.assertEqual([n for n, _ in shape(real)[1]], [1, 3, 5])
        self.assertEqual([a["name"] for a in real["messages"][1]["attachments"]], ["ok.bin", "fwd.eml"])
        self.assertEqual(self.counts(), [(8, 8, 7)])  # mailbox, 3 messages, 4 attachments; 3 + 4 contains rows
        stored, objects = self.counts(), sorted(os.listdir(self.root / "private" / "objects"))
        again = self.ingest(path)
        self.assertEqual(shape(again), shape(real))
        self.assertEqual(set(self.outcomes(again)), {"present"})
        self.assertEqual((self.counts(), sorted(os.listdir(self.root / "private" / "objects"))), (stored, objects))

    def test_derived_size_boundary_excludes_complete_frame(self):
        import contextlib
        import io
        from campaign_tool import ingest_mbox
        limit = 40
        head = b"Subject: s\r\n\r\n"
        for eol, frame in ((b"\n", b"\n"), (b"\r\n", b"\r\n")):
            fit = head.replace(b"\r\n", eol) + b"x" * (limit - len(head.replace(b"\r\n", eol)))
            for payload, code in ((fit, None), (fit + b"y", "too_large")):
                with self.subTest(eol=eol, size=len(payload)):
                    sep = b"From a@example.org Tue" + eol
                    path = self.write(sep + payload + frame, "size.mbox")
                    args = types.SimpleNamespace(mbox=str(path), source_id="synthetic-mbox-1", dry_run=True)
                    out = io.StringIO()
                    with mock.patch.object(ingest_mbox, "MAX_BYTES", limit), contextlib.redirect_stdout(out):
                        ingest_mbox.ingest_mbox(args, self.root)
                    report = json.loads(out.getvalue().rsplit("Dry run", 1)[0])
                    self.assertEqual(report["skipped"], [{"number": 1, "code": code}] if code else [])
                    self.assertEqual([m["sha256"] for m in report["messages"]], [] if code else [sha(payload)])

    # RF1355-1: a MIME tree nested deeper than the stdlib parser can follow skips only its own message
    def snapshot(self):
        return {str(p.relative_to(self.base)): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes())
                for p in self.base.rglob("*") if p.is_file()}

    def test_deep_mbox_skips_only_offending_message_preserves_links(self):
        m1, m3 = message((PDF, "application", "pdf", "a.pdf")), message((CSV, "text", "csv", "b.csv"))
        data = mbox(m1, deep(1200), m3)
        path = self.write(data)
        report = self.ingest(path)
        box = sha(data)
        self.assertEqual((report["mailbox"]["sha256"], report["skipped"], report["count"]),
                         (box, [{"number": 2, "code": "malformed"}], 3))
        self.assertEqual([(m["number"], m["sha256"], m["outcome"], m["attachments"]) for m in report["messages"]], [
            (1, sha(m1), "stored", [{"name": "a.pdf", "sha256": sha(PDF), "outcome": "stored"}]),
            (3, sha(m3), "stored", [{"name": "b.csv", "sha256": sha(CSV), "outcome": "stored"}])])
        objects = self.root / "private" / "objects"
        self.assertEqual(set(os.listdir(objects)), {box, sha(m1), sha(m3), sha(PDF), sha(CSV)})
        self.assertEqual([(objects / h).read_bytes() for h in (box, sha(m1), sha(m3))], [data, m1, m3])
        self.assertEqual(self.counts(), [(5, 5, 4)])
        self.assertEqual(sorted(self.rows("SELECT * FROM contains")), sorted([
            (box, sha(m1), "message-1.eml"), (box, sha(m3), "message-3.eml"),
            (sha(m1), sha(PDF), "a.pdf"), (sha(m3), sha(CSV), "b.csv")]))
        self.assertEqual(sorted(r[0] for r in self.rows("SELECT original_name FROM receipts")),
                         sorted(["replies.mbox", "replies.mbox/message-1.eml", "replies.mbox/message-1.eml/a.pdf",
                                 "replies.mbox/message-3.eml", "replies.mbox/message-3.eml/b.csv"]))
        self.assertEqual(path.read_bytes(), data)
        self.cli("status", "--check")

    def test_all_deep_mbox_preserves_only_mailbox(self):
        data = mbox(deep(1200), deep(1200, "other leaf"))
        report = self.ingest(self.write(data, "every.mbox"))
        self.assertEqual((report["skipped"], report["messages"], report["count"]),
                         ([{"number": 1, "code": "malformed"}, {"number": 2, "code": "malformed"}], [], 2))
        objects = self.root / "private" / "objects"
        self.assertEqual(os.listdir(objects), [sha(data)])
        self.assertEqual((objects / sha(data)).read_bytes(), data)
        self.assertEqual((self.counts(), self.rows("SELECT * FROM contains")), ([(1, 1, 0)], []))

    def test_deep_mbox_preview_and_replay_preserve_accounting(self):
        bad = deep(1200)
        cases = {"mixed": (mbox(message((PDF, "application", "pdf", "a.pdf")), bad, message(body="Plain reply.\n")),
                           [{"number": 2, "code": "malformed"}]),
                 "all-deep": (mbox(bad, bad), [{"number": 1, "code": "malformed"}, {"number": 2, "code": "malformed"}])}
        shape = lambda r: (r["mailbox"]["sha256"], r["skipped"], r["count"], [
            (m["number"], m["sha256"], [(a["name"], a["sha256"]) for a in m["attachments"]]) for m in r["messages"]])
        for case, (data, skipped) in cases.items():
            for populated in (False, True):
                with self.subTest(case=case, populated=populated):
                    self.root = self.base / f"campaign-{case}-{populated}"
                    self.cli("init", "--name", "Synthetic Campaign", "--county", "Synthetic County", "--state", "ca")
                    if populated:
                        self.ingest(self.write(mbox(message((CSV, "text", "csv", "seed.csv"))), "seed.mbox"))
                    path = self.write(data, f"{case}.mbox")
                    before = self.snapshot()
                    preview = self.ingest(path, dry=True)
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(preview["skipped"], skipped)
                    real = self.ingest(path)
                    self.assertEqual(shape(preview), shape(real))
                    stored, objects = self.counts(), sorted(os.listdir(self.root / "private" / "objects"))
                    self.assertNotIn(sha(bad), objects)
                    self.assertIn(sha(data), objects)
                    again = self.ingest(path)
                    self.assertEqual(shape(again), shape(real))
                    self.assertEqual(set(self.outcomes(again)), {"present"})
                    self.assertEqual((self.counts(), sorted(os.listdir(self.root / "private" / "objects"))),
                                     (stored, objects))

    # RF0345-2: a mailbox basename that is not strict UTF-8 is refused before anything is preserved.
    REFUSAL = "Mailbox filename cannot be represented as UTF-8; nothing was imported"
    RAW_NAMES = (b"raw-\xff.mbox", b"raw-\xc3\x28-\xe2\x82.mbox")  # lone FF; invalid two- and three-byte sequences

    def raw_write(self, data, raw_name, parent=None):
        """A real file whose basename is exactly `raw_name` bytes (POSIX); skipped where the platform cannot."""
        path = os.path.join(os.fsencode(parent or self.inbox), raw_name)
        try:
            with open(path, "wb") as handle:
                handle.write(data)
        except (OSError, UnicodeError):
            self.skipTest("this platform cannot create a raw non-UTF-8 file name")
        return os.fsdecode(path)

    def assert_refused(self, path, dry):
        result = self.ingest(path, dry=dry, returncode=1)
        self.assertEqual((result.stdout, result.stderr), ("", f"Stopped: {self.REFUSAL}\n"))
        for part in ("codec", "surrogate", "\\udcff", "raw-", ".mbox", "message-"):
            self.assertNotIn(part, result.stderr)

    def test_raw_mailbox_basename_refuses_before_preview_or_persistence(self):
        valid = mbox(*three())
        paths = [self.raw_write(valid, raw_name) for raw_name in self.RAW_NAMES]
        for populated in (False, True):
            if populated:
                self.ingest(self.write(mbox(message(body="Populated synthetic.\n")), "seed.mbox"))
            for path in paths:
                for dry in (True, False):
                    with self.subTest(name=path, populated=populated, dry=dry):
                        before = self.files()
                        self.assert_refused(path, dry)
                        self.assertEqual(self.files(), before)
                        with open(os.fsencode(path), "rb") as handle:
                            self.assertEqual(handle.read(), valid)
                        if not populated:
                            self.assertFalse((self.root / "private" / "ledger.sqlite").exists())
                            self.assertEqual(os.listdir(self.root / "private" / "objects") if
                                             (self.root / "private" / "objects").exists() else [], [])
        self.cli("status", "--check")
        self.assertEqual(self.rows("SELECT count(*) FROM receipts"), [(2,)])

    def test_direct_refusal_precedes_split_extract_lookup_database_and_store(self):
        from campaign_tool import ingest_mbox
        path = self.raw_write(mbox(*three()), self.RAW_NAMES[0])
        trips = {n: mock.Mock(side_effect=AssertionError(n)) for n in
                 ("_split", "_extract", "_ledger_lookup", "database", "_store_bytes", "_store_message")}
        with mock.patch.multiple(ingest_mbox, **trips):
            for dry in (True, False):
                args = types.SimpleNamespace(mbox=path, source_id="synthetic-mbox-1", dry_run=dry)
                with self.assertRaises(ValueError) as caught:
                    ingest_mbox.ingest_mbox(args, self.root)
                self.assertEqual(str(caught.exception), self.REFUSAL)
                self.assertIsNone(caught.exception.__cause__)
        for trip in trips.values():
            trip.assert_not_called()

    def test_preview_and_apply_share_filename_admission(self):
        empty = SEPARATOR + b"\n"  # one separator, no message bytes
        malformed = mbox(b'Content-Type: multipart/mixed\r\n\r\n--B\r\nbody\r\n--B--\r\n')
        for index, data in enumerate((mbox(*three()), empty, malformed)):
            path = self.raw_write(data, self.RAW_NAMES[0] + str(index).encode())
            for dry in (True, False):
                with self.subTest(index=index, dry=dry):
                    before = self.files()
                    self.assert_refused(path, dry)
                    self.assertEqual(self.files(), before)
        self.assertFalse((self.root / "private" / "ledger.sqlite").exists())

    def test_bad_name_retry_does_not_create_or_repair_orphans(self):
        path = self.raw_write(mbox(*three()), self.RAW_NAMES[0])
        self.assert_refused(path, False)
        first = self.files()
        self.assert_refused(path, False)
        self.assertEqual(self.files(), first)
        # A historical orphan object and same-digest preexisting state are left exactly as found.
        data = mbox(*three())
        self.ingest(self.write(data, "other.mbox"))
        objects = self.root / "private" / "objects"
        orphan = objects / sha(b"historical orphan")
        orphan.write_bytes(b"historical orphan")
        seeded, rows = self.files(), self.rows("SELECT original_name FROM receipts ORDER BY 1")
        self.assert_refused(self.raw_write(data, self.RAW_NAMES[1]), False)
        self.assertEqual({k: v for k, v in self.files().items() if "raw-" not in k},
                         {k: v for k, v in seeded.items() if "raw-" not in k})
        self.assertEqual(self.rows("SELECT original_name FROM receipts ORDER BY 1"), rows)
        self.assertEqual(orphan.read_bytes(), b"historical orphan")
        status = self.cli("status", "--check", returncode=3)  # only the deliberately seeded orphan is reported
        self.assertEqual((status.stdout.count("no_ledger_row"), status.stdout.count(sha(b"historical orphan"))), (1, 1))

    def test_manual_rename_allows_normal_mailbox_import(self):
        data = mbox(*three())
        path = self.raw_write(data, self.RAW_NAMES[0])
        self.assert_refused(path, False)
        renamed = self.inbox / "ordinary.mbox"
        os.rename(os.fsencode(path), renamed)
        preview, real = self.ingest(renamed, dry=True), self.ingest(renamed)
        self.assertEqual([m["sha256"] for m in preview["messages"]], [m["sha256"] for m in real["messages"]])
        self.assertEqual((real["mailbox"]["sha256"], set(self.outcomes(real))), (sha(data), {"stored"}))
        self.assertEqual(sorted(r[0] for r in self.rows("SELECT original_name FROM receipts"))[:2],
                         ["ordinary.mbox", "ordinary.mbox/message-1.eml"])
        self.assertTrue(all(not name.startswith("raw") for (name,) in self.rows("SELECT original_name FROM receipts")))
        counts = self.rows("SELECT (SELECT count(*) FROM objects), (SELECT count(*) FROM contains)")
        self.assertEqual(set(self.outcomes(self.ingest(renamed))), {"present"})
        self.assertEqual(self.rows("SELECT (SELECT count(*) FROM objects), (SELECT count(*) FROM contains)"), counts)

    def test_valid_unicode_basename_and_undecodable_parent_remain_supported(self):
        names = ("caf\u00e9.mbox", "cafe\u0301.mbox", "\u90f5\u4ef6.mbox", "mail-\U0001f4ec.mbox")
        for number, name in enumerate(names):
            data = mbox(message(body=f"Unicode name synthetic {number}.\n"))  # distinct bytes: one receipt per name
            with self.subTest(name=name):
                path = self.write(data, name)
                preview, real = self.ingest(path, dry=True), self.ingest(path)
                self.assertEqual(preview["mailbox"]["sha256"], real["mailbox"]["sha256"])
                self.assertIn((name,), self.rows("SELECT original_name FROM receipts"))
                self.assertIn((f"{name}/message-1.eml",), self.rows("SELECT original_name FROM receipts"))
                self.assertEqual(set(self.outcomes(self.ingest(path))), {"present"})
        high = os.path.join(os.fsdecode(os.fsencode(self.inbox)), os.fsdecode(b"up-\xfe"))
        parent = os.path.join(high, os.fsdecode(b"under-\xff"))
        try:
            os.makedirs(os.fsencode(parent))
        except OSError:
            self.skipTest("this platform cannot create a raw non-UTF-8 directory name")
        path = os.path.join(parent, "ordinary.mbox")
        with open(os.fsencode(path), "wb") as handle:
            handle.write(mbox(message(body="Raw ancestor synthetic.\n")))
        args = types.SimpleNamespace(mbox=path, source_id="synthetic-mbox-1", dry_run=False)
        result = self.cli("ingest", "--mbox", path, "--source-id", "synthetic-mbox-1")
        self.assertEqual(json.loads(result.stdout)["mailbox"]["outcome"], "stored")
        names_now = [r[0] for r in self.rows("SELECT original_name FROM receipts")]
        self.assertIn("ordinary.mbox", names_now)
        self.assertFalse(any("under-" in n or "up-" in n for n in names_now))
        self.assertIsNotNone(args)

    def test_filename_refusal_precedence_is_explicit(self):
        raw = self.RAW_NAMES[0]
        junk = self.raw_write(b"Hello\n" + mbox(*three()), raw)  # not an mbox, readable: name refusal wins
        self.assert_refused(junk, True)
        many = self.raw_write(mbox(*[message(body=f"Synthetic reply {n}.\n") for n in range(501)]), raw + b"2")
        self.assert_refused(many, False)
        # A representable name keeps the existing content-specific errors.
        result = self.ingest(self.write(b"Hello\n" + mbox(*three()), "hello.mbox"), returncode=1)
        self.assertIn("Stopped: not an mbox file (no From line at the start); nothing was imported", result.stderr)
        # Reader refusals keep precedence over the name check for a raw-named non-regular file.
        os.symlink(self.write(mbox(*three())), os.fsencode(os.path.join(os.fsdecode(os.fsencode(self.inbox)),
                                                                         os.fsdecode(b"link-\xff.mbox"))))
        os.mkdir(os.fsencode(os.path.join(os.fsdecode(os.fsencode(self.inbox)), os.fsdecode(b"dir-\xff.mbox"))))
        for leaf in (b"link-\xff.mbox", b"dir-\xff.mbox"):
            result = self.ingest(os.path.join(os.fsdecode(os.fsencode(self.inbox)), os.fsdecode(leaf)), returncode=1)
            self.assertEqual(result.stderr, "Stopped: --mbox must be a regular, non-symlink file\n")
        from campaign_tool import ingest_mbox
        small = self.raw_write(mbox(*three()), raw + b"3")
        args = types.SimpleNamespace(mbox=small, source_id="synthetic-mbox-1", dry_run=False)
        with mock.patch.object(ingest_mbox, "MAX_MBOX_BYTES", 10):
            with self.assertRaisesRegex(ValueError, r"^Alpha limit: 200 MiB per mailbox; nothing was imported$"):
                ingest_mbox.ingest_mbox(args, self.root)

    def test_refusal_diagnostic_is_fixed_and_value_free(self):
        from campaign_tool import ingest_mbox
        data = mbox(message(body="Canary name synthetic.\n"))
        parent = os.path.join(os.fsdecode(os.fsencode(self.inbox)), f"parent-{CANARY}-\u00e9")
        os.mkdir(parent)
        path = self.raw_write(data, f"name-{CANARY}-".encode() + b"\xff.mbox", parent)
        for dry in (True, False):
            result = self.ingest(path, dry=dry, returncode=1)  # self.cli() already rejects any canary in output
            self.assertEqual(result.stderr, f"Stopped: {self.REFUSAL}\n")
        for value in ("a\udcffb", "\ud800", "x\udfffy"):
            with self.assertRaises(ValueError) as caught:
                ingest_mbox._require_receipt_name(value)
            self.assertEqual(str(caught.exception), self.REFUSAL)
        ingest_mbox._require_receipt_name("plain-\u00e9\U0001f4ec.mbox")


if __name__ == "__main__":
    unittest.main()
