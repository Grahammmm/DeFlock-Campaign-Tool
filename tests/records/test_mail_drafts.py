"""records mail-drafts against a fake IMAP client: no network, no real mailbox, credential or send."""
import contextlib
import email
import io
import json
import os
import smtplib
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from email import policy
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import mail_drafts as md
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import received_items
from tests.approval_signing import Signer

REPO = Path(__file__).resolve().parents[2]
EML, DOC = "e" * 64, "d" * 64
USER = "records@campaign.example"
CANARY = "CANARY-BODY-TEXT-7731"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
EVIDENCE = {"findings": [], "deadlines": []}
AGENCY_MID = "<agency-1@agency.example>"


class FakeIMAP:
    """Allows exactly select, uid SEARCH, append and logout; anything else is a test failure."""

    def __init__(self):
        self.messages, self.calls, self.folders = [], [], []

    def __getattr__(self, name):
        raise AssertionError("unexpected IMAP call: " + name)

    def select(self, folder, readonly=False):
        self.calls.append("select")
        self.folders.append(folder)
        return "OK", [b"1"]

    def uid(self, command, *args):
        assert command == "SEARCH", command
        self.calls.append("search")
        wanted = args[-1].strip('"')
        hits = [str(i + 1).encode() for i, m in enumerate(self.messages) if wanted.encode() in m["raw"]]
        return "OK", [b" ".join(hits)]

    def append(self, folder, flags, date, raw):
        self.calls.append("append")
        self.messages.append({"folder": folder, "flags": flags, "raw": raw})
        return "OK", [b"done"]

    def logout(self):
        self.calls.append("logout")
        return "BYE", []

    def factory(self, config, password):
        return self


class Base(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.tmp = tempfile.TemporaryDirectory(prefix="maildrafts-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.project = root / "project"
        self.project.mkdir(mode=0o700)
        private = root / "private"
        private.mkdir(mode=0o700)
        self.pw = private / "pw"
        self.pw.write_text("synthetic-secret\n")
        self.pw.chmod(0o600)
        self.config_path = self.write_config(private)
        self.config = md.load_config(self.config_path)
        self.fake = FakeIMAP()
        self.signer = Signer()
        self.addCleanup(self.signer.close)
        trust = self.signer.trust()
        trust.__enter__()
        self.addCleanup(trust.__exit__, None, None, None)
        self.root = root
        self.ledger = self.project / "processing-v1" / "ledger.sqlite"
        for tripwire in (mock.patch.object(smtplib, "SMTP", side_effect=AssertionError("smtp")),
                         mock.patch.object(socket, "create_connection", side_effect=AssertionError("socket")),
                         mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("http"))):
            tripwire.start()
            self.addCleanup(tripwire.stop)

    def write_config(self, private, name="mail.json", **override):
        data = {"host": "imap.example.invalid", "port": 993, "user": USER, "password_file": str(self.pw),
                "folder": "Drafts"}
        data.update(override)
        path = private / name
        path.write_text(json.dumps({k: v for k, v in data.items() if v is not None}))
        path.chmod(0o600)
        return path

    def build_ledger(self, eml_headers=None, store_file=True, link=True):
        """One agency message (EML) with one attached document (DOC); the item is DOC's received item."""
        store.initialize(self.ledger)
        blob = self.root / "blobs" / EML
        blob.parent.mkdir(exist_ok=True, mode=0o700)
        headers = eml_headers if eml_headers is not None else (
            f"From: Clerk <clerk@agency.example>\r\nReply-To: records-desk@agency.example\r\n"
            f"Subject: Your request 4471\r\nMessage-ID: {AGENCY_MID}\r\n"
            "References: <earlier@agency.example>\r\n\r\nbody\r\n")
        blob.write_text(headers)
        with store.ledger(self.ledger) as con:
            for digest in (EML, DOC):
                con.execute("INSERT INTO originals(sha256,bytes,mime_detected,first_seen_at,role,scope,"
                            "preservation_status,provenance_json,storage_path) VALUES(?,?,?,?,?,?,?,?,?)",
                            (digest, 10, "x/y", "2026-11-02T18:59:00Z", "agency_original", "in_scope", "verified",
                             "{}", str(blob) if digest == EML and store_file else None))
            if link:
                con.execute("INSERT INTO occurrences VALUES('o1',?,'mail','m',NULL,NULL,'t','{}')", (EML,))
                con.execute("INSERT INTO occurrences VALUES('o2',?,'attachment','p','o1',NULL,'t','{}')", (DOC,))
                con.execute("INSERT INTO mail_messages VALUES('msg-1','acct','INBOX',1,1,?,?,?,?,'x')",
                            (AGENCY_MID, EML, "{}", "2026-11-02T18:59:00Z"))
            con.commit()

    def draft(self, item=None, body=None, approve=True):
        row = aq.add_draft(self.project, item_id=item or received_items.item_id(DOC), kind="reply-extension",
                           agency_id="agency-1", request_id="req-1", body=body or f"Please extend. {CANARY}\n",
                           evidence=EVIDENCE, source_run_id="run-1")
        return self.approve(row["id"]) if approve else row

    def approve(self, ident):
        sig = self.signer.sign(aq.approval_payload(self.project, ident))
        return aq.approve(self.project, ident, {"kind": "ssh", "signature": sig})

    def place(self, **kw):
        kw.setdefault("now", NOW)
        return md.place(self.project, self.config, factory=self.fake.factory, **kw)

    def tearDown_project(self):
        """Fresh project and fake mailbox inside one test."""
        self.project = self.root / ("project-%d" % len(list(self.root.glob("project*"))))
        self.project.mkdir(mode=0o700)
        self.ledger = self.project / "processing-v1" / "ledger.sqlite"
        self.fake = FakeIMAP()

    def journal_path(self):
        return self.project / "approvals" / "mail-drafts.jsonl"

    def parsed(self, index=-1):
        return email.message_from_bytes(self.fake.messages[index]["raw"], policy=policy.default)


class PlaybookPlaceTests(Base):
    def test_invalid_or_unresolved_provenance_is_journaled_without_append(self):
        cases = [({}, "thread_provenance_invalid"),
                 ({"vehicle": "reply_in_thread", "thread_item_id": None}, "thread_provenance_invalid"),
                 ({"vehicle": "reply_in_thread", "thread_item_id": "bad"}, "thread_provenance_invalid"),
                 ({"vehicle": "new_request", "thread_item_id": "a" * 64}, "thread_provenance_invalid"),
                 ({"vehicle": "reply_in_thread", "thread_item_id": "a" * 64}, "thread_provenance_unresolved")]
        for evidence, code in cases:
            with self.subTest(evidence=evidence):
                self.tearDown_project()
                row = aq.add_draft(self.project, item_id="route:synthetic", kind="followup-playbook",
                                   agency_id="alpha-pd", request_id="OLD-1", body="Please supply the logs.\n",
                                   evidence=evidence, source_run_id="r1")
                self.approve(row["id"])
                self.assertEqual(self.place()["failed"], 1)
                self.assertEqual(md.placements(self.project)[0]["code"], code)
                self.assertEqual(self.fake.messages, [])

    def test_received_items_denial_keeps_original_threading(self):
        self.build_ledger()
        row = aq.add_draft(self.project, item_id=received_items.item_id(DOC), kind="appeal-denial",
                           agency_id="alpha-pd", request_id="OLD-1", body="Please reconsider.\n",
                           evidence={}, source_run_id="r1")
        self.approve(row["id"])
        self.assertEqual(self.place()["placed"], 1)
        self.assertEqual(self.parsed()["In-Reply-To"], AGENCY_MID)


class PlaceTests(Base):
    def test_only_approved_rows_are_placed_once(self):
        self.build_ledger()
        approved = self.draft()
        pending = self.draft(item="1" * 64, approve=False)
        rejected = self.draft(item="2" * 64, approve=False)
        aq.reject(self.project, rejected["id"], "op", "no")
        edited = self.draft(item="3" * 64)
        aq.edit(self.project, edited["id"], "op", "edited back to pending")
        self.assertEqual(self.place(), {"placed": 1, "existing": 0, "failed": 0, "skipped": 0})
        self.assertEqual(len(self.fake.messages), 1)
        self.assertEqual((self.fake.messages[0]["folder"], self.fake.messages[0]["flags"]), ('"Drafts"', "(\\Draft)"))
        self.assertEqual(self.parsed()["X-Records-Queue-Id"].strip(), approved["id"])
        # a second run makes no second draft
        self.assertEqual(self.place(), {"placed": 0, "existing": 0, "failed": 0, "skipped": 1})
        self.assertEqual(len(self.fake.messages), 1)
        self.assertNotIn(pending["id"], b"".join(m["raw"] for m in self.fake.messages).decode())
        (row,) = md.placements(self.project)
        self.assertEqual((row["state"], row["threaded"], row["folder"]), ("placed", True, "Drafts"))
        self.assertEqual(row["fingerprint"], md.fingerprint(self.fake.messages[0]["raw"]))
        self.assertEqual(row["message_id"], self.parsed()["Message-ID"])

    def test_threaded_headers_from_source_message(self):
        self.build_ledger()
        self.draft()
        self.place()
        msg = self.parsed()
        self.assertEqual(msg["In-Reply-To"], AGENCY_MID)
        self.assertEqual(msg["References"], "<earlier@agency.example> " + AGENCY_MID)
        self.assertEqual(msg["To"], "records-desk@agency.example")
        self.assertEqual(msg["Subject"], "Re: Your request 4471")
        self.assertEqual(msg["From"], USER)
        self.assertEqual(msg.get_content().replace("\r\n", "\n"), f"Please extend. {CANARY}\n")
        # an existing "Re:" is not doubled
        raw = md.build_message({"id": "a" * 64, "revision": 2, "request_id": "r", "body": "x"},
                               {"message_id": AGENCY_MID, "references": [], "subject": "RE: Hello",
                                "reply_to": "a@b.example"}, USER, now=NOW)
        self.assertEqual(email.message_from_bytes(raw, policy=policy.default)["Subject"], "RE: Hello")

    def test_unthreaded_when_source_missing(self):
        cases = {
            "no_link": (dict(link=False), "thread_not_found"),
            "no_file": (dict(store_file=False), "eml_missing"),
            "no_message_id": (dict(eml_headers="From: a@b.example\r\nSubject: Re: x\r\n\r\nbody\r\n"),
                              "message_id_missing"),
        }
        for name, (kw, reason) in cases.items():
            with self.subTest(name):
                self.tearDown_project()
                self.build_ledger(**kw)
                self.draft()
                self.place()
                msg = self.parsed()
                self.assertIsNone(msg["To"])
                self.assertIsNone(msg["In-Reply-To"])
                self.assertEqual(msg["Subject"], "Records request req-1 follow-up")  # never guessed from a subject
                (row,) = md.placements(self.project)
                self.assertEqual((row["threaded"], row["thread_reason"]), (False, reason))

    def test_crash_after_append_does_not_duplicate(self):
        self.build_ledger()
        self.draft()
        self.place()
        self.journal_path().unlink()  # the crash: APPEND happened, the journal write did not
        self.fake.calls.clear()
        self.assertEqual(self.place(), {"placed": 0, "existing": 1, "failed": 0, "skipped": 0})
        self.assertNotIn("append", self.fake.calls)
        self.assertEqual(len(self.fake.messages), 1)
        self.assertEqual(md.placements(self.project)[0]["state"], "found_existing")

    def test_crash_recovery_at_a_later_clock_records_the_stored_fingerprint(self):
        self.build_ledger()
        self.draft()
        self.place()
        self.journal_path().unlink()
        later = NOW + timedelta(days=3)
        self.assertEqual(self.place(now=later), {"placed": 0, "existing": 1, "failed": 0, "skipped": 0})
        self.assertEqual(len(self.fake.messages), 1)
        (row,) = md.placements(self.project)
        self.assertEqual(row["state"], "found_existing")
        self.assertEqual(row["fingerprint"], md.fingerprint(self.fake.messages[0]["raw"]))

    def test_edit_after_placement_supersedes(self):
        self.build_ledger()
        row = self.draft()
        self.place()
        first_mid = self.parsed()["Message-ID"]
        aq.edit(self.project, row["id"], "op", "second text")
        self.assertEqual(self.place(), {"placed": 0, "existing": 0, "failed": 0, "skipped": 0})  # pending: not placed
        self.approve(row["id"])
        self.fake.calls.clear()
        self.assertEqual(self.place()["placed"], 1)
        self.assertEqual(len(self.fake.messages), 2)
        self.assertNotEqual(self.parsed()["Message-ID"], first_mid)
        self.assertEqual(self.parsed().get_content().replace("\r\n", "\n"), "second text\n")
        states = {r["revision"]: r["state"] for r in md.placements(self.project)}
        self.assertEqual(sorted(states.values()), ["placed", "superseded"])
        superseded = [r for r in md.placements(self.project) if r["state"] == "superseded"][0]
        self.assertEqual(superseded["message_id"], first_mid)
        self.assertEqual(set(self.fake.calls), {"select", "search", "append", "logout"})  # no store/expunge/copy

    def test_connection_failure_is_journaled_and_retried(self):
        self.build_ledger()
        self.draft()

        def refuse(config, password):
            raise OSError("down")
        summary = md.place(self.project, self.config, factory=refuse, now=NOW)
        self.assertEqual(summary["failed"], 1)
        (row,) = md.placements(self.project)
        self.assertEqual((row["state"], row["code"]), ("failed", "connection_refused"))
        self.assertEqual(self.place()["placed"], 1)
        self.assertEqual(md.placements(self.project)[0]["state"], "placed")

    def test_dry_run_connects_nowhere_and_writes_nothing(self):
        self.build_ledger()
        self.draft()
        summary = md.place(self.project, self.config, factory=None, dry_run=True)
        self.assertEqual((summary["would_place"], summary["placed"]), (1, 0))
        self.assertFalse(self.journal_path().exists())


class RefusalTests(Base):
    def test_config_refusals(self):
        private = self.pw.parent
        for name, override, code in (("nofolder.json", {"folder": None}, "folder_required"),
                                     ("inbox.json", {"folder": "INBOX"}, "folder_required"),
                                     ("noat.json", {"user": "records"}, "from_address_unknown"),
                                     ("twoat.json", {"user": "a@b@c"}, "from_address_unknown")):
            path = self.write_config(private, name, **override)
            with self.subTest(name), self.assertRaises(md.MailDraftError) as ctx:
                md.load_config(path)
            self.assertEqual(ctx.exception.code, code)
        loose = self.write_config(private, "loose.json")
        loose.chmod(0o644)
        link = private / "link.json"
        link.symlink_to(self.config_path)
        for path in (loose, link):
            with self.subTest(path.name), self.assertRaises(md.MailDraftError):
                md.load_config(path)

    def test_header_injection_refused(self):
        for thread in ({"message_id": AGENCY_MID, "references": [], "subject": "x\r\nBcc: z@z.example",
                        "reply_to": "a@b.example"},
                       {"message_id": AGENCY_MID, "references": [], "subject": "x", "reply_to": "a@b.example\r\nBcc: z"}):
            with self.subTest(thread["subject"]), mock.patch.object(md, "find_thread", return_value=(thread, None)):
                self.tearDown_project()
                self.build_ledger()
                self.draft()
                self.assertEqual(self.place()["failed"], 1)
                self.assertEqual(self.fake.messages, [])
                self.assertEqual(md.placements(self.project)[0]["code"], "header_unsafe")

    def test_journal_private_and_append_only(self):
        self.build_ledger()
        self.draft()
        self.place()
        path = self.journal_path()
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), "0o700")
        before = path.read_bytes()
        self.draft(item="4" * 64)
        self.place()
        self.assertTrue(path.read_bytes().startswith(before))
        # a truncated tail, a loose mode and a symlink are refused and left unchanged
        good = path.read_bytes()
        path.write_bytes(good + b'{"schema":')
        with self.assertRaises(md.MailDraftError):
            md.placements(self.project)
        with self.assertRaises(md.MailDraftError):
            self.place()
        self.assertEqual(path.read_bytes(), good + b'{"schema":')
        path.write_bytes(good)
        path.chmod(0o644)
        with self.assertRaises(md.MailDraftError):
            md.placements(self.project)
        path.chmod(0o600)
        moved = path.with_name("moved.jsonl")
        path.rename(moved)
        path.symlink_to(moved)
        with self.assertRaises(md.MailDraftError):
            md.placements(self.project)

    def test_cli_dry_run_and_list_are_content_free(self):
        self.build_ledger()
        self.draft()

        def cli(*args):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = md.main(list(args))
            return code, out.getvalue(), err.getvalue()
        code, out, err = cli("place", "--project", str(self.project), "--config", str(self.config_path), "--dry-run")
        self.assertEqual((code, json.loads(out)["would_place"]), (0, 1))
        self.assertFalse(self.journal_path().exists())
        self.place()
        code, listing, err2 = cli("list", "--project", str(self.project))
        self.assertEqual((code, json.loads(listing.splitlines()[0])["state"]), (0, "placed"))
        code, out3, err3 = cli("place", "--project", str(self.project), "--config", str(self.pw.parent / "nope.json"))
        self.assertEqual((code, out3, err3.strip()), (1, "", "config_unavailable"))
        for text in (out, err, listing, err2, out3, err3):
            self.assertNotIn(CANARY, text)
            self.assertNotIn("synthetic-secret", text)
        # the real entry point is wired and refuses without touching a network
        run = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "mail-drafts", "place",
                              "--project", str(self.project), "--config", str(self.pw.parent / "nope.json")],
                             cwd=REPO, capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 1)

    def test_connect_uses_only_mail_poll_secret_path(self):
        self.build_ledger()
        self.draft()
        seen = []
        self.fake.factory = lambda config, password: (seen.append(password) or self.fake)
        self.place()
        self.assertEqual(seen, ["synthetic-secret"])


if __name__ == "__main__":
    unittest.main()
