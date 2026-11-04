"""records mail-poll against a local synthetic IMAP double: no network, no real mailbox or credential."""
import contextlib
import copy
import functools
import hashlib
import imaplib
import io
import json
import os
import ssl
import subprocess
import sys
import tempfile
import unittest
import uuid
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from campaign_tool.records import mail_poll

REPO = Path(__file__).resolve().parents[2]
# Built at run time so the synthetic secret is never a committed literal.
SYNTHETIC_CREDENTIAL = "synthetic-cred-" + uuid.uuid4().hex
SUBJECT, ADDRESS, PHONE, PLATE = "Records Request 4471", "clerk@agency.example", "805-555-0142", "7ABC123"


@functools.lru_cache(maxsize=None)
def message(n, *, mid=None, body=None, attachments=(("report.pdf", b"%PDF-1.4 synthetic"),), extra="", boundary=None):
    """Cached: the MIME boundary is random, and identical input must give identical bytes."""
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = ADDRESS, "records@campaign.example", SUBJECT + extra
    msg["Message-ID"] = mid or "<msg-%d@agency.example>" % n
    msg.set_content(body or "Responsive records attached. Call %s re plate %s. #%d" % (PHONE, PLATE, n))
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    if boundary is not None:
        msg.set_boundary(boundary)
    return msg.as_bytes()


def notice(n):
    return message(n, attachments=(), body="Your records are ready: https://portal.example/documents/%d" % n)


class Box:
    def __init__(self, messages, validity=7):
        self.messages, self.validity, self.flags, self.calls = dict(messages), validity, {}, []
        self.error, self.connects = None, 0

    def snapshot(self):
        return copy.deepcopy((self.messages, self.flags, self.validity))

    def factory(self, config, password):
        self.connects += 1
        assert password == SYNTHETIC_CREDENTIAL
        if self.error:
            raise self.error
        return FakeIMAP(self)


class FakeIMAP:
    def __init__(self, box):
        self.box = box

    def select(self, name, readonly=False):
        self.box.calls.append(("SELECT", name, readonly))
        return "OK", [str(len(self.box.messages)).encode()]

    def response(self, code):
        return code, [str(self.box.validity).encode()]

    def uid(self, command, *args):
        self.box.calls.append((command,) + args)
        if command == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in sorted(self.box.messages))]
        if command == "FETCH":
            uid = int(args[0])
            if "PEEK" not in args[1]:
                self.box.flags[uid] = "\\Seen"
            return "OK", [(b"%d (UID %d BODY[] {%d}" % (uid, uid, len(self.box.messages[uid])),
                           self.box.messages[uid]), b")"]
        raise AssertionError("unexpected IMAP command " + command)

    def logout(self):
        return "BYE", [b""]


def tree(root):
    root = Path(root)
    if not root.exists():
        return None
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns, oct(p.stat().st_mode))
            for p in sorted(root.rglob("*")) if p.is_file()}


class MailPollTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.store = self.root / "store"
        self.pw = self.private / "pw"
        self.pw.write_text(SYNTHETIC_CREDENTIAL + "\n")
        self.pw.chmod(0o600)
        self.config = self.write_config()
        self.outputs = []

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, name="mail.json", mode=0o600, **override):
        data = {"host": "imap.example.invalid", "port": 993, "user": ADDRESS, "password_file": str(self.pw)}
        data.update(override)
        data = {k: v for k, v in data.items() if v is not None}
        path = self.private / name
        path.write_text(json.dumps(data))
        path.chmod(mode)
        return path

    def run_poll(self, box, *extra, config=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mail_poll.main(["--config", str(config or self.config), "--store", str(self.store), *extra],
                                  factory=box.factory)
        self.outputs += [out.getvalue(), err.getvalue()]
        return code, out.getvalue().strip(), err.getvalue().strip()

    def state(self):
        return json.loads((self.store / "mail-poll" / "state.json").read_text())

    def receipts(self):
        return [json.loads(p.read_text()) for p in sorted((self.store / "mail-poll" / "receipts").glob("*.json"))]

    def assert_line(self, line, **counts):
        want = dict(new=0, already=0, conflict=0, notices=0, skipped=0)
        want.update(counts)
        self.assertEqual(line, mail_poll.status_line(want))

    # 1 + 2
    def test_three_new_then_rerun_rewrites_nothing(self):
        box = Box({1: message(1), 2: message(2), 3: message(3)})
        with mock.patch.object(mail_poll.folder, "atomic", wraps=mail_poll.folder.atomic) as atomic:
            code, line, _ = self.run_poll(box)
        self.assertEqual(code, 0)
        self.assert_line(line, new=3)
        self.assertEqual(atomic.call_count, 1)
        self.assertEqual(self.state()["cursor"], 3)
        self.assertEqual(sorted(r["uid"] for r in self.receipts()), [1, 2, 3])
        self.assertTrue(all(r["kind"] == "message" and r["received_files"] == 1 for r in self.receipts()))
        for r in self.receipts():
            intake = self.store / "mail-poll" / "mail" / r["intake_receipt"]
            self.assertEqual(json.loads(intake.read_text())["original_eml"]["sha256"], r["sha256"])
        before = tree(self.store / "mail-poll" / "mail"), tree(self.store / "mail-poll" / "receipts")
        code, line, _ = self.run_poll(box)
        self.assert_line(line, already=3)
        self.assertEqual(before, (tree(self.store / "mail-poll" / "mail"), tree(self.store / "mail-poll" / "receipts")))
        self.assertEqual([c for c in box.calls if c[0] == "FETCH"].__len__(), 3)

    def test_done_when_three_new_one_already_one_notice(self):
        box = Box({1: message(1)})
        self.assertEqual(self.run_poll(box)[0], 0)
        box.messages.update({2: message(2), 3: message(3), 4: message(4), 5: notice(5)})
        code, line, _ = self.run_poll(box)
        self.assert_line(line, new=3, already=1, notices=1)
        self.assertEqual(self.state()["cursor"], 5)
        self.assert_line(self.run_poll(box)[1], already=5)

    def test_same_bytes_at_new_uid_is_already(self):
        box = Box({1: message(1)})
        self.run_poll(box)
        box.messages[2] = message(1)
        self.assert_line(self.run_poll(box)[1], already=2)

    # 3
    def test_same_id_different_bytes_is_quarantined_conflict(self):
        box = Box({1: message(1)})
        self.run_poll(box)
        first = tree(self.store / "mail-poll" / "mail")
        changed = message(1, extra=" (amended)")
        box.messages[2] = changed
        code, line, _ = self.run_poll(box)
        self.assert_line(line, already=1, conflict=1)
        self.assertEqual(first, tree(self.store / "mail-poll" / "mail"))
        quarantined = self.store / "mail-poll" / "quarantine" / (hashlib.sha256(changed).hexdigest() + ".eml")
        self.assertEqual(quarantined.read_bytes(), changed)
        self.assertEqual(oct(quarantined.stat().st_mode & 0o777), "0o600")

    # 4
    def test_portal_notice_is_a_notice_not_a_received_file(self):
        code, line, _ = self.run_poll(Box({1: notice(1)}))
        self.assert_line(line, notices=1)
        [receipt] = self.receipts()
        self.assertEqual((receipt["kind"], receipt["received_files"]), ("notice", 0))

    # 5
    def test_connection_auth_and_tls_failures_are_named_refusals(self):
        cases = ((ConnectionRefusedError(111, "refused " + SYNTHETIC_CREDENTIAL), "connection_refused", 2),
                 (imaplib.IMAP4.error("LOGIN failed for " + ADDRESS + " " + SYNTHETIC_CREDENTIAL), "auth_refused", 1),
                 (ssl.SSLError("handshake " + SYNTHETIC_CREDENTIAL), "tls_unavailable", 1))
        for error, code_name, connects in cases:
            box = Box({1: message(1)})
            box.error = error
            code, line, err = self.run_poll(box)
            self.assertEqual((code, line), (1, ""))
            self.assertIn("refused: " + code_name, err)
            self.assertEqual(box.connects, connects)
            self.assertIsNone(tree(self.store))

    def test_plaintext_or_starttls_config_is_refused(self):
        for tls in ("starttls", "none", False):
            code, _, err = self.run_poll(Box({}), config=self.write_config(tls=tls))
            self.assertEqual(code, 1)
            self.assertIn("refused: tls_required", err)
        self.assertIsNone(tree(self.store))

    # 6
    def test_crash_between_fetch_and_confirm_refetches(self):
        box = Box({1: message(1), 2: message(2), 3: message(3)})
        config = mail_poll.load_config(self.config)

        def crash(uid):
            if uid == 2:
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            mail_poll.poll(config, self.store, factory=box.factory, crash_after_fetch=crash)
        self.assertFalse((self.store / "mail-poll" / "state.json").exists())
        code, line, _ = self.run_poll(box)
        self.assert_line(line, new=3)
        self.assertEqual(sorted(r["uid"] for r in self.receipts()), [1, 2, 3])
        self.assertEqual(self.state()["cursor"], 3)

    # 7
    def test_bad_config_files_refused_by_field_without_echo(self):
        loose = self.write_config("loose.json", mode=0o644)
        link = self.private / "link.json"
        link.symlink_to(self.config)
        nopw = self.write_config("nopw.json", password_file=None)
        loose_pw = self.private / "pw2"
        loose_pw.write_text(SYNTHETIC_CREDENTIAL)
        loose_pw.chmod(0o644)
        weak = self.write_config("weak.json", password_file=str(loose_pw))
        fd, inside = tempfile.mkstemp(dir=REPO, prefix=".mail-poll-test-")
        os.write(fd, self.config.read_bytes())
        os.close(fd)
        try:
            cases = ((loose, "config_mode_not_0600"), (link, "config_symlink"), (Path(inside), "config_inside_repo"),
                     (nopw, "config_field:password_file"), (weak, "password_file_mode_not_0600"))
            for path, expected in cases:
                code, line, err = self.run_poll(Box({}), config=path)
                self.assertEqual((code, line), (1, ""))
                self.assertIn("refused: " + expected, err)
                for value in ("imap.example.invalid", ADDRESS, str(self.private), SYNTHETIC_CREDENTIAL):
                    self.assertNotIn(value, err)
        finally:
            os.unlink(inside)
        self.assertIsNone(tree(self.store))

    # 8 + 10
    def test_secret_and_correspondence_never_leak(self):
        box = Box({1: message(1), 2: notice(2), 3: message(3)})
        self.run_poll(box)
        self.run_poll(box, "--dry-run")
        box.error = imaplib.IMAP4.error(SYNTHETIC_CREDENTIAL)
        self.run_poll(box)
        for text in self.outputs:
            for value in (SYNTHETIC_CREDENTIAL, SUBJECT, ADDRESS, PHONE, PLATE, "Responsive records", "report.pdf"):
                self.assertNotIn(value, text)
        poll_files = [self.store / "mail-poll" / "state.json", *(self.store / "mail-poll" / "receipts").glob("*")]
        intake_receipts = list((self.store / "mail-poll" / "mail").rglob("receipt*.json"))
        self.assertEqual(len(intake_receipts), 3)
        for path in poll_files + intake_receipts:
            data = path.read_text()
            for value in (SYNTHETIC_CREDENTIAL, SUBJECT, ADDRESS, PHONE, PLATE, "Responsive records"):
                self.assertNotIn(value, data)
        for path in poll_files:
            self.assertNotIn("report.pdf", path.read_text())
        for path in self.store.rglob("*"):
            if path.is_file():
                self.assertNotIn(SYNTHETIC_CREDENTIAL.encode(), path.read_bytes())
        for top in ("campaign_tool", "docs", "tests", "tools"):
            for path in (REPO / top).rglob("*"):
                if path.is_file() and path.suffix in {".py", ".md", ".json", ".txt"}:
                    self.assertNotIn(SYNTHETIC_CREDENTIAL.encode(), path.read_bytes(), str(path))

    # 9
    def test_unsafe_attachment_names_refused_message_still_recorded(self):
        # This fixed MIME boundary produces a legitimate SHA-256 containing "bad".
        raw = message(1, attachments=(("../../escape.txt", b"one"), ("a\\b.txt", b"two"), ("ok.txt", b"three")),
                      boundary="synthetic-unsafe-65")
        nul = raw.replace(b'filename="ok.txt"', b"filename*=utf-8''bad%00name.txt")
        before = sorted(p.name for p in self.root.iterdir())
        code, line, err = self.run_poll(Box({1: nul}))
        self.assertEqual(code, 0, err)
        self.assert_line(line, new=1, skipped=3)
        [receipt] = self.receipts()
        self.assertEqual((receipt["received_files"], receipt["intake_receipt"]), (0, None))
        self.assertEqual((self.store / "mail-poll" / receipt["held"]).read_bytes(), nul)
        self.assertEqual([r["reason"] for r in receipt["refused_attachments"]], ["attachment_name_unsafe"] * 3)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), sorted(before + ["store"]))
        poll_root = (self.store / "mail-poll").resolve()
        for path in poll_root.rglob("*"):
            self.assertTrue(path.resolve().is_relative_to(poll_root))
        sha = hashlib.sha256(nul).hexdigest()
        self.assertEqual(
            {p.relative_to(self.store).as_posix() for p in self.store.rglob("*") if p.is_file()},
            {"mail-poll/state.json", f"mail-poll/receipts/{sha}.json", f"mail-poll/quarantine/{sha}.eml"},
        )
        self.assertFalse([p for p in self.root.rglob("*")
                          if p.name in {"escape.txt", "b.txt", "a\\b.txt", "bad", "bad\x00name.txt", "ok.txt"}])
        self.assert_line(self.run_poll(Box({1: nul}))[1], already=1)

    # dry run
    def test_dry_run_leaves_store_cursor_and_mailbox_byte_identical(self):
        box = Box({1: message(1), 2: message(2), 3: message(3), 4: notice(4)})
        mailbox = box.snapshot()
        code, line, _ = self.run_poll(box, "--dry-run")
        self.assert_line(line, new=3, notices=1)
        self.assertIsNone(tree(self.store))
        self.run_poll(box)
        box.messages[5] = message(5)
        box.messages[6] = message(1, extra=" (amended)")
        mailbox, store = box.snapshot(), tree(self.store)
        code, line, _ = self.run_poll(box, "--dry-run")
        self.assert_line(line, new=1, already=4, conflict=1)
        self.assertEqual((mailbox, store), (box.snapshot(), tree(self.store)))
        self.assertEqual(box.flags, {})
        self.assertTrue(all(c[2] is True for c in box.calls if c[0] == "SELECT"))

    def test_uidvalidity_reset_refetches_without_duplicates(self):
        box = Box({1: message(1), 2: message(2)})
        self.run_poll(box)
        box.validity, box.messages = 8, {10: message(1), 11: message(2), 12: message(3)}
        self.assert_line(self.run_poll(box)[1], new=1, already=2)
        self.assertEqual((self.state()["uidvalidity"], self.state()["cursor"]), (8, 12))

    # 11
    def test_static_no_outbound_or_mailbox_mutation(self):
        source = Path(mail_poll.__file__).read_text()
        for token in ("smtplib", "sendmail", "send_message", '"STORE"', '"APPEND"', '"COPY"', '"MOVE"',
                      '"EXPUNGE"', "client.append", "client.store", "client.copy", "client.expunge", "IMAP4("):
            self.assertNotIn(token, source)
        self.assertIn("readonly=True", source)
        self.assertIn("(BODY.PEEK[])", source)
        self.assertLessEqual(len(source.splitlines()), 280)

    def test_cli_help_and_usage_exit(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "mail-poll", "--help"],
                                cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--dry-run", result.stdout)
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "mail-poll"],
                                cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
