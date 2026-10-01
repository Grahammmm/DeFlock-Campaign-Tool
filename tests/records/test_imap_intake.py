"""IMAP intake over a fake in-memory server: new mail, replay, same bytes in two
folders, UIDVALIDITY reset, connection drop mid-folder, unconfigured and missing
folders, and credential hygiene. No network, no real mailbox.
"""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from campaign_tool.records.intake import imap_intake
from campaign_tool.records.run import Pipeline
from tests.records.test_run_slice import synthetic_email


class FakeIMAP:
    """Just enough of imaplib's surface for IMAPIntake: list/select/uid/response/logout."""

    def __init__(self, folders, *, fail_on=None, noselect=()):
        # folders: {name: {"uidvalidity": int, "messages": {uid: bytes}}}
        self.folders, self.fail_on, self.noselect = folders, fail_on or set(), set(noselect)
        self.selected = None
        self.logged_out = False

    def list(self):
        lines = []
        for name in sorted(self.folders) + sorted(self.noselect):
            flags = b"\\Noselect" if name in self.noselect else b"\\HasNoChildren"
            lines.append(b"(" + flags + b') "/" "' + name.encode() + b'"')
        return "OK", lines

    def select(self, quoted, readonly=False):
        name = quoted.strip('"')
        if name not in self.folders:
            return "NO", [b"nope"]
        assert readonly, "intake must select read-only"
        self.selected = name
        return "OK", [str(len(self.folders[name]["messages"])).encode()]

    def response(self, code):
        assert code == "UIDVALIDITY"
        return "OK", [str(self.folders[self.selected]["uidvalidity"]).encode()]

    def uid(self, command, *args):
        box = self.folders[self.selected]["messages"]
        if command == "SEARCH":
            start = int(args[1].split()[1].split(":")[0])
            hits = b" ".join(str(u).encode() for u in sorted(box) if u >= start)
            return "OK", [hits]
        if command == "FETCH":
            uid = int(args[0])
            if (self.selected, uid) in self.fail_on:
                raise OSError("connection dropped")
            return "OK", [(b"1 (UID %d BODY[] {%d}" % (uid, len(box[uid])), box[uid]), b")"]
        raise AssertionError(command)

    def logout(self):
        self.logged_out = True
        return "BYE", []


class IMAPIntakeTests(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.tmp = tempfile.TemporaryDirectory(prefix="records-imap-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        os.chmod(self.base, 0o700)
        self.root = self.base / "root"
        self.config_path = self.base / "mail.json"
        self.write_config({"host": "imap.example.invalid", "username": "records", "password": "not-a-real-secret",  # pragma: allowlist secret - synthetic fixture
                           "account_id": "synthetic-account", "folders": ["INBOX", "Agencies"]})

    def write_config(self, config, mode=0o600):
        self.config_path.write_bytes(json.dumps(config).encode())
        os.chmod(self.config_path, mode)

    def ledger(self, sql, values=()):
        con = sqlite3.connect(self.root / "ledger.sqlite")
        try:
            return con.execute(sql, values).fetchall()
        finally:
            con.close()

    def checkpoints(self):
        return {row[0]: (row[1], row[2]) for row in self.ledger("SELECT folder,uidvalidity,highest_uid FROM mail_checkpoints")}

    def run_with(self, server):
        pipeline = Pipeline(self.root, account="synthetic-account")
        report = pipeline.run(None, self.config_path, client_factory=lambda config: server)
        self.assertTrue(server.logged_out)
        return report

    def test_new_mail_is_preserved_and_replay_adds_nothing(self):
        mail = synthetic_email()
        server = FakeIMAP({"INBOX": {"uidvalidity": 7, "messages": {1: mail, 2: synthetic_email(message_id="<b@x.invalid>", filename="other.txt")}},
                           "Agencies": {"uidvalidity": 3, "messages": {}}})
        report = self.run_with(server)
        mailbox = report["mailbox"]
        self.assertEqual(mailbox["preserved"], 2)
        self.assertEqual(mailbox["failures"], 0)
        self.assertEqual(self.checkpoints(), {"INBOX": (7, 2), "Agencies": (3, 0)})
        originals = self.ledger("SELECT count(*) FROM originals")[0][0]
        self.assertEqual(self.ledger("SELECT count(*) FROM stage_state WHERE stage='preserve' AND status='done'")[0][0], originals)
        occurrences = self.ledger("SELECT count(*) FROM occurrences")[0][0]
        # Replay: nothing new on the server, nothing new in the ledger.
        again = self.run_with(FakeIMAP(server.folders))
        self.assertEqual(again["mailbox"]["preserved"], 0)
        self.assertEqual(self.ledger("SELECT count(*) FROM originals")[0][0], originals)
        self.assertEqual(self.ledger("SELECT count(*) FROM occurrences")[0][0], occurrences)
        # The password never reaches any report, run record or checkpoint.
        for path in (self.root / "runs").iterdir():
            self.assertNotIn("not-a-real-secret", path.read_text())
        for row in self.ledger("SELECT summary FROM runs"):
            self.assertNotIn("not-a-real-secret", row[0])
            self.assertNotIn("records@", row[0])

    def test_same_bytes_in_two_folders_is_one_original_with_two_occurrences(self):
        mail = synthetic_email()
        server = FakeIMAP({"INBOX": {"uidvalidity": 1, "messages": {5: mail}},
                           "Agencies": {"uidvalidity": 1, "messages": {9: mail}}})
        report = self.run_with(server)
        self.assertEqual(report["mailbox"]["preserved"], 2)
        eml_sha = self.ledger("SELECT eml_sha256 FROM mail_messages")[0][0]
        self.assertEqual(self.ledger("SELECT count(DISTINCT eml_sha256) FROM mail_messages")[0][0], 1)
        self.assertEqual(self.ledger("SELECT count(*) FROM mail_messages WHERE eml_sha256=?", (eml_sha,))[0][0], 2)
        self.assertEqual(self.ledger("SELECT count(*) FROM originals WHERE sha256=?", (eml_sha,))[0][0], 1)

    def test_uidvalidity_reset_reenumerates_without_duplicating_bytes(self):
        mail = synthetic_email()
        first = FakeIMAP({"INBOX": {"uidvalidity": 1, "messages": {1: mail}}, "Agencies": {"uidvalidity": 1, "messages": {}}})
        self.run_with(first)
        originals = self.ledger("SELECT count(*) FROM originals")[0][0]
        # Server rebuilt the folder: new UIDVALIDITY, same message under a new UID plus one new message.
        reset = FakeIMAP({"INBOX": {"uidvalidity": 2, "messages": {1: mail, 2: synthetic_email(message_id="<new@x.invalid>")}},
                          "Agencies": {"uidvalidity": 1, "messages": {}}})
        report = self.run_with(reset)
        entry = next(f for f in report["mailbox"]["folders"] if f["folder"] == "INBOX")
        self.assertTrue(entry["uidvalidity_reset"])
        self.assertEqual(entry["new"], 2)
        self.assertEqual(self.checkpoints()["INBOX"], (2, 2))
        # The re-seen message adds an occurrence, not an original; the new message adds one
        # original (its attachment bytes are identical to the first message's attachment).
        self.assertEqual(self.ledger("SELECT count(*) FROM originals")[0][0], originals + 1)
        self.assertEqual(self.ledger("SELECT count(*) FROM mail_messages WHERE folder='INBOX' AND uidvalidity=2")[0][0], 2)
        self.assertEqual([r[0] for r in self.ledger("SELECT key FROM alerts WHERE key LIKE 'mail:uidvalidity_reset%'")],
                         ["mail:uidvalidity_reset:INBOX"])

    def test_drop_mid_folder_keeps_checkpoint_before_failure_and_other_folders_continue(self):
        messages = {1: synthetic_email(message_id="<1@x.invalid>"), 2: synthetic_email(message_id="<2@x.invalid>"),
                    3: synthetic_email(message_id="<3@x.invalid>")}
        server = FakeIMAP({"INBOX": {"uidvalidity": 1, "messages": messages},
                           "Agencies": {"uidvalidity": 4, "messages": {11: synthetic_email(message_id="<a@x.invalid>")}}},
                          fail_on={("INBOX", 2)})
        report = self.run_with(server)
        inbox = next(f for f in report["mailbox"]["folders"] if f["folder"] == "INBOX")
        self.assertEqual(inbox["preserved"], 1)
        self.assertIn("uid 2", inbox["failed"])
        self.assertEqual(self.checkpoints(), {"INBOX": (1, 1), "Agencies": (4, 11)})
        self.assertEqual(report["mailbox"]["failures"], 1)
        # Next run resumes at uid 2 and finishes the folder; no duplicate occurrences for uid 1.
        report = self.run_with(FakeIMAP(server.folders))
        inbox = next(f for f in report["mailbox"]["folders"] if f["folder"] == "INBOX")
        self.assertEqual(inbox["preserved"], 2)
        self.assertEqual(self.checkpoints()["INBOX"], (1, 3))
        self.assertEqual(self.ledger("SELECT count(*) FROM mail_messages WHERE folder='INBOX'")[0][0], 3)

    def test_unconfigured_and_missing_folders_raise_keyed_alerts(self):
        server = FakeIMAP({"INBOX": {"uidvalidity": 1, "messages": {}}, "Junk": {"uidvalidity": 1, "messages": {}}},
                          noselect=["[Gmail]"])
        report = self.run_with(server)
        self.assertEqual(report["mailbox"]["unconfigured_folders"], ["Junk"])
        agencies = next(f for f in report["mailbox"]["folders"] if f["folder"] == "Agencies")
        self.assertEqual(agencies["failed"], "folder_missing")
        keys = sorted(r[0] for r in self.ledger("SELECT key FROM alerts"))
        self.assertEqual(keys, ["mail:missing_folder:Agencies", "mail:unconfigured_folder:Junk"])
        self.assertEqual(self.ledger("SELECT count FROM alerts WHERE key='mail:unconfigured_folder:Junk'")[0][0], 1)
        self.run_with(FakeIMAP(server.folders, noselect=["[Gmail]"]))
        self.assertEqual(self.ledger("SELECT count FROM alerts WHERE key='mail:unconfigured_folder:Junk'")[0][0], 2)

    def test_config_must_be_owner_only_and_hold_a_password(self):
        self.write_config({"host": "h", "username": "u", "password": "p", "account_id": "a"}, mode=0o644)
        with self.assertRaisesRegex(imap_intake.IntakeError, "owner_only"):
            imap_intake.load_config(self.config_path)
        self.write_config({"host": "h", "username": "u", "account_id": "a"})
        with self.assertRaisesRegex(imap_intake.IntakeError, "password_unavailable"):
            imap_intake.load_config(self.config_path)
        secret = self.base / "pw"
        secret.write_text("from-file\n")
        os.chmod(secret, 0o600)
        self.write_config({"host": "h", "username": "u", "account_id": "a", "password_file": "pw"})
        config = imap_intake.load_config(self.config_path)
        self.assertEqual(config["password"], "from-file")
        self.assertNotIn("password", imap_intake.redacted(config))
        self.assertNotIn("from-file", json.dumps(imap_intake.redacted(config)))

    def test_oversized_message_is_a_failure_not_a_crash(self):
        big = b"From: a@x.invalid\r\nSubject: big\r\n\r\n" + b"x" * (imap_intake.MAX_MESSAGE + 1)
        server = FakeIMAP({"INBOX": {"uidvalidity": 1, "messages": {1: big, 2: synthetic_email()}},
                           "Agencies": {"uidvalidity": 1, "messages": {}}})
        report = self.run_with(server)
        inbox = next(f for f in report["mailbox"]["folders"] if f["folder"] == "INBOX")
        self.assertIn("message_size_limit", inbox["failed"])
        self.assertEqual(self.checkpoints()["INBOX"], (1, 0))

    def test_list_line_parsing_handles_quoted_and_literal_forms(self):
        self.assertEqual(imap_intake.parse_list_line(b'(\\HasNoChildren) "/" "Sent Items"'), "Sent Items")
        self.assertEqual(imap_intake.parse_list_line(b'(\\HasNoChildren) "/" INBOX'), "INBOX")
        self.assertIsNone(imap_intake.parse_list_line(b'(\\Noselect \\HasChildren) "/" "[Gmail]"'))
        self.assertEqual(imap_intake.parse_list_line((b'(\\HasNoChildren) "/" {5}', b"Caf\xc3\xa9")), "Café")


if __name__ == "__main__":
    unittest.main()
