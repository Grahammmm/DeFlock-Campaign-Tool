"""records mail-send: the console send path against a fake IMAP mailbox and an SMTP stub. No network, no real send."""
import contextlib
import io
import json
import multiprocessing
import os
import smtplib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from campaign_tool import outbox
from campaign_tool.records import approval_queue as aq
from campaign_tool.records import mail_drafts as md
from campaign_tool.records import mail_send as ms
from campaign_tool.records.outcomes import received_items
from tests.approval_signing import Signer
from tests.records.test_mail_drafts import AGENCY_MID, CANARY, DOC, EVIDENCE, FakeIMAP, NOW
from tests.records.test_mail_drafts import Base as DraftsBase

OWNER = "owner-test@example.invalid"  # an address the tester owns; never an agency


class MailboxIMAP(FakeIMAP):
    """Read-only after placement: SELECT must be readonly, SEARCH and FETCH only; any write is a failure."""

    def __init__(self):
        super().__init__()
        self.readonly = []

    def select(self, folder, readonly=False):
        self.readonly.append(readonly)
        return super().select(folder, readonly)

    def uid(self, command, *args):
        if command == "FETCH":
            self.calls.append("fetch")
            index = int(args[0]) - 1
            return "OK", [(b"%d (UID %d BODY[] {1}" % (index + 1, index + 1), self.messages[index]["raw"]), b")"]
        return super().uid(command, *args)


class SmtpStub:
    """Stands in for smtplib.SMTP: records connections and messages; optionally drops after DATA."""

    def __init__(self, record=None, drop=False):
        self.connections, self.messages, self.logins, self.drop, self.record = 0, [], [], drop, record

    def __call__(self):
        self.connections += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, message):
        self.messages.append(message)
        if self.record:
            with open(self.record, "a") as stream:
                stream.write("sent\n")
        if self.drop:
            raise smtplib.SMTPServerDisconnected("dropped after data")
        return {}


class SendBase(DraftsBase):
    def setUp(self):
        super().setUp()
        self.fake = MailboxIMAP()
        self.build_ledger()
        self.draft_row = self.draft()
        self.place()
        self.smtp = SmtpStub()
        self.send_path = self.project / "approvals" / "send.json"
        self.outbox_path = self.root / "outbox" / "outbox.sqlite"
        self.init_args = ["init", "--project", str(self.project), "--imap-config", str(self.config_path),
                          "--smtp-host", "smtp.example.invalid", "--smtp-user", "records@campaign.example",
                          "--smtp-password-file", str(self.pw), "--outbox-path", str(self.outbox_path)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ms.main(self.init_args), 0)

    def configure(self, **changes):
        data = json.loads(self.send_path.read_text())
        data.update(changes)
        self.send_path.write_text(json.dumps(data))
        self.send_path.chmod(0o600)

    def on(self, **changes):
        """Switch sending on the way G would: enabled true and SEND-STOP removed by hand."""
        self.configure(**{"enabled": True, "live": True, **changes})
        (self.project / "approvals" / ms.KILL).unlink()

    def imap(self):
        return self.fake.factory

    def prepared(self, ident=None, to=None, now=NOW):
        info = ms.prepare(self.project, self.send_path, ident or self.draft_row["id"], to=to,
                          imap_factory=self.imap(), now=now)
        self.assertIsNone(info["code"], info["code"])
        return info

    def click(self, ident=None, *, sign=True, signature=None, to="", confirm="", fingerprint=None, now=NOW, smtp=None):
        """One console POST: the page's payload signed by the trusted key, then send_one."""
        ident = ident or self.draft_row["id"]
        info = self.prepared(ident, to or None, now) if signature is None and sign else None
        if signature is None:
            signature = self.signer.sign(info["payload"].encode()) if info else "unsigned"
        if fingerprint is None:
            fingerprint = info["fingerprint"] if info else md.placements(self.project)[0]["fingerprint"]
        return ms.send_one(self.project, self.send_path, ident, actor="operator-1", page_fingerprint=fingerprint,
                           signature=signature, to=to, confirm=confirm, imap_factory=self.imap(),
                           smtp_factory=smtp or self.smtp, now=now)

    def log(self):
        path = self.project / "approvals" / ms.RESULTS
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def outbox_rows(self):
        box = outbox.Outbox(self.outbox_path)
        try:
            return box.rows()
        finally:
            box.close()

    def second_draft(self, item="1" * 64, agency="agency-2"):
        row = aq.add_draft(self.project, item_id=item, kind="reply-extension", agency_id=agency,
                           request_id="req-" + agency, body="Second follow-up.\n", evidence=EVIDENCE,
                           source_run_id="run-1")
        self.approve(row["id"])
        self.place()
        return row["id"]


class SwitchTests(SendBase):
    def test_ships_switched_off(self):
        config = json.loads(self.send_path.read_text())
        self.assertIs(config["enabled"], False)
        self.assertIs(config["live"], False)
        self.assertTrue((self.project / "approvals" / ms.KILL).exists())
        self.assertEqual(oct(self.send_path.stat().st_mode & 0o777), "0o600")
        result = self.click(sign=False, signature="x", fingerprint="f")
        self.assertEqual((result["state"], result["code"]), ("refused", "send_disabled"))
        self.assertEqual(self.smtp.connections, 0)
        self.assertEqual(self.fake.calls.count("fetch"), 0)
        self.assertEqual(ms.switch_state(self.project, self.send_path)["reason"], "send_disabled")
        self.assertEqual([(r["state"], r["code"], r["actor"]) for r in self.log()], [("refused", "send_disabled", "operator-1")])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ms.main(["status", "--project", str(self.project)]), 0)
        self.assertIn('"kill_switch": true', out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ms.main(self.init_args), 1)  # init never overwrites
        self.assertIn("already_initialized", err.getvalue())

    def test_kill_switch_refuses_and_prepare_does_not_touch_the_mailbox(self):
        self.configure(enabled=True)  # enabled but SEND-STOP still present
        before = list(self.fake.calls)
        info = ms.prepare(self.project, self.send_path, self.draft_row["id"], imap_factory=self.imap(), now=NOW)
        self.assertEqual(info["code"], "kill_switch")
        self.assertEqual(self.fake.calls, before)
        result = self.click(sign=False, signature="x", fingerprint="f")
        self.assertEqual(result["code"], "kill_switch")
        self.assertEqual(self.smtp.connections, 0)
        # the operator's modes/ kill file and a configured kill_file stop it as well
        (self.project / "approvals" / ms.KILL).unlink()
        self.assertTrue(ms.switch_state(self.project, self.send_path, now=NOW)["can_send"])
        (self.project / "modes").mkdir()
        (self.project / "modes" / "RECORDS-SEND-STOP").write_text("")
        self.assertEqual(ms.switch_state(self.project, self.send_path, now=NOW)["reason"], "kill_switch")

    def test_kill_switch_wins_late(self):
        self.on()
        real = outbox.Outbox.approve
        stop = self.project / "approvals" / ms.KILL

        def approve_then_stop(box, key, evidence):
            row = real(box, key, evidence)
            stop.write_text("")
            return row
        with mock.patch.object(outbox.Outbox, "approve", approve_then_stop):
            result = self.click()
        self.assertEqual(result["code"], "kill_switch")
        self.assertEqual([r["state"] for r in self.outbox_rows()], ["approved"])
        self.assertEqual(self.smtp.connections, 0)

    def test_unknown_config_field_is_refused(self):
        self.on(surprise=True)
        self.assertEqual(self.click(sign=False, signature="x", fingerprint="f")["code"], "config_field_unknown")


class DraftChecks(SendBase):
    def setUp(self):
        super().setUp()
        self.on()

    def test_fingerprint_mismatch_refuses(self):
        stale = self.click(fingerprint="0" * 64)  # the page showed another text
        self.assertEqual(stale["code"], "fingerprint_stale")
        raw = self.fake.messages[0]["raw"]
        self.fake.messages[0]["raw"] = raw.replace(b"Please", b"Pleasf")  # one byte differs in the mailbox
        self.assertEqual(self.click(sign=False, signature="x")["code"], "draft_changed_in_mailbox")
        self.assertEqual(self.outbox_rows(), [])
        self.assertEqual(self.smtp.connections, 0)

    def test_missing_and_duplicate_draft(self):
        message = self.fake.messages.pop()
        self.assertEqual(self.click(sign=False, signature="x")["code"], "draft_missing")
        self.fake.messages.extend([message, dict(message)])
        self.assertEqual(self.click(sign=False, signature="x")["code"], "draft_ambiguous")
        self.assertEqual(self.smtp.connections, 0)

    def test_superseded_or_unapproved_refuses(self):
        ident, fingerprint = self.draft_row["id"], md.placements(self.project)[0]["fingerprint"]
        aq.edit(self.project, ident, "op", "Edited text.\n")  # back to pending
        self.assertEqual(self.click(sign=False, signature="x", fingerprint=fingerprint)["code"], "not_approved")
        self.approve(ident)  # approved again at a new revision, not yet placed
        self.assertEqual(self.click(sign=False, signature="x", fingerprint=fingerprint)["code"], "not_placed")
        self.place()  # placed: the old page's fingerprint is stale, the old revision is superseded
        self.assertEqual(self.click(sign=False, signature="x", fingerprint=fingerprint)["code"], "fingerprint_stale")
        other = self.second_draft()
        self.assertEqual(self.click("f" * 64, sign=False, signature="x", fingerprint="f")["code"], "unknown_draft")
        self.assertEqual(self.smtp.connections, 0)

    def test_only_an_outbox_signature_approves(self):
        info = self.prepared()
        queue_sig = self.signer.sign(aq.approval_payload(self.project, self.draft_row["id"]))
        untrusted = Signer("intruder@example.invalid", trusted=False)
        self.addCleanup(untrusted.close)
        for signature in (queue_sig, "G", untrusted.sign(info["payload"].encode()), "  "):
            result = self.click(signature=signature, fingerprint=info["fingerprint"])
            self.assertIn(result["code"], ("approval_refused", "signature_required"), signature)
        self.assertEqual([r["state"] for r in self.outbox_rows()], ["proposed"])
        self.assertEqual(self.smtp.connections, 0)


class SendTests(SendBase):
    def setUp(self):
        super().setUp()
        self.on()

    def test_one_click_one_send(self):
        result = self.click()
        self.assertEqual((result["state"], result["code"]), ("sent", None))
        self.assertEqual(len(self.smtp.messages), 1)
        message = self.smtp.messages[0]
        self.assertEqual(message["In-Reply-To"], AGENCY_MID)
        self.assertEqual(message["To"], "records-desk@agency.example")
        self.assertEqual(message["Subject"], "Re: Your request 4471")
        self.assertIn(CANARY, message.get_content())
        repeat = self.click()
        self.assertEqual((repeat["state"], repeat["code"], repeat["message_id"]),
                         ("sent", "already_sent", result["message_id"]))
        self.assertEqual((len(self.smtp.messages), self.smtp.connections), (1, 1))
        sent = [r for r in self.log() if r["state"] == "sent"][0]
        self.assertEqual((sent["message_id"], sent["actor"], sent["approver"]),
                         (result["message_id"], "operator-1", self.signer.principal))
        self.assertEqual(sent["fingerprint"], md.placements(self.project)[0]["fingerprint"])
        self.assertEqual(sent["schema"], ms.SCHEMA)

    def test_late_kill_then_changed_recipient_is_refused(self):
        other = self.second_draft()  # unthreaded: the recipient comes from the form
        real, stop = outbox.Outbox.approve, self.project / "approvals" / ms.KILL

        def approve_then_stop(box, key, evidence):
            row = real(box, key, evidence)
            stop.write_text("")
            return row
        with mock.patch.object(outbox.Outbox, "approve", approve_then_stop):
            self.assertEqual(self.click(other, to=OWNER, confirm=OWNER)["code"], "kill_switch")
        stop.unlink()
        changed = "someone-else@agency.example"
        result = self.click(other, to=changed, confirm=changed)
        self.assertEqual((result["state"], result["code"]), ("refused", "draft_payload_mismatch"))
        self.assertEqual(self.smtp.connections, 0)
        self.assertEqual([r["state"] for r in self.outbox_rows()], ["approved"])
        self.assertEqual(self.click(other, to=OWNER, confirm=OWNER)["state"], "sent")  # same reviewed payload still sends
        self.assertEqual(self.smtp.messages[0]["To"], OWNER)

    def test_unwritable_or_full_log_sends_nothing(self):
        with mock.patch.object(ms, "MAX_RESULTS", 1):  # the results log cannot take another row
            result = self.click()
        self.assertEqual((result["state"], result["code"]), ("refused", "audit_log_unwritable"))
        self.assertEqual(self.smtp.connections, 0)
        self.assertEqual([r["state"] for r in self.outbox_rows()], ["proposed"])  # never approved

    def test_attempt_row_precedes_the_send(self):
        self.assertEqual(self.click()["state"], "sent")
        states = [r["state"] for r in self.log()]
        self.assertEqual(states, ["attempt", "sent"])
        self.assertEqual(self.log()[0]["actor"], "operator-1")

    def test_daily_cap_and_agency_cap(self):
        self.configure(daily_cap=1)
        self.assertEqual(self.click()["state"], "sent")
        other = self.second_draft(agency="agency-2")
        fingerprint = [r for r in md.placements(self.project) if r["queue_id"] == other][0]["fingerprint"]
        denied = self.click(other, sign=False, signature="x", fingerprint=fingerprint, to=OWNER, confirm=OWNER)
        self.assertEqual(denied["code"], "daily_cap_reached")
        self.assertEqual(self.smtp.connections, 1)
        later = self.click(other, to=OWNER, confirm=OWNER, now=NOW + timedelta(days=1))  # next local day
        self.assertEqual(later["state"], "sent")
        # the outbox's own one-per-agency-per-UTC-day cap still holds beside the console cap
        self.configure(daily_cap=5)
        same_agency = self.second_draft(item="2" * 64, agency="agency-1")
        self.assertEqual(self.click(same_agency, to=OWNER, confirm=OWNER)["code"], "outbox_daily_agency_cap")

    def test_ambiguous_failure_left_for_reconcile(self):
        drop = SmtpStub(drop=True)
        result = self.click(smtp=drop)
        self.assertEqual((result["state"], result["code"]), ("unknown", "delivery_unknown"))
        self.assertEqual([r["state"] for r in self.outbox_rows()], ["sending"])
        again = self.click(smtp=drop)
        self.assertEqual((again["state"], again["code"]), ("unknown", "delivery_unknown"))
        self.assertEqual(drop.connections, 1)

    def test_recipient_outside_the_thread_needs_typed_confirmation(self):
        other = self.second_draft()  # no thread: the draft has no To
        self.assertEqual(self.click(other, sign=False, signature="x", fingerprint="f")["code"], "fingerprint_stale")
        fingerprint = [r for r in md.placements(self.project) if r["queue_id"] == other][0]["fingerprint"]
        self.assertEqual(self.click(other, sign=False, signature="x", fingerprint=fingerprint)["code"], "recipient_missing")
        bad = self.click(other, sign=False, signature="x", fingerprint=fingerprint, to="a@b.example\r\nBcc: c@d.example")
        self.assertEqual(bad["code"], "recipient_invalid")
        self.assertEqual(self.click(other, to=OWNER)["code"], "confirmation_required")
        self.assertEqual(self.click(other, to=OWNER, confirm="someone-else@example.invalid")["code"], "confirmation_required")
        self.assertEqual(self.smtp.connections, 0)
        self.assertEqual(self.click(other, to=OWNER, confirm=OWNER)["state"], "sent")
        self.assertEqual(self.smtp.messages[0]["To"], OWNER)
        self.assertNotIn("In-Reply-To", self.smtp.messages[0])

    def test_agency_list_address_needs_no_confirmation(self):
        self.configure(agency_addresses=[OWNER.upper()])
        other = self.second_draft()
        self.assertEqual(self.click(other, to=OWNER)["state"], "sent")

    def test_mailbox_untouched(self):
        self.fake.calls.clear()
        self.fake.readonly.clear()
        self.assertEqual(self.click()["state"], "sent")
        self.assertEqual(set(self.fake.calls) - {"select", "search", "fetch", "logout"}, set())  # no STORE/APPEND/...
        self.assertTrue(self.fake.readonly and all(self.fake.readonly))
        self.assertEqual(len(self.fake.messages), 1)

    def test_no_secret_in_output(self):
        result = self.click()
        info_page = ms.render_result(result)
        log = (self.project / "approvals" / ms.RESULTS).read_text()
        for text in (json.dumps(result), info_page, log, json.dumps(ms.switch_state(self.project, self.send_path))):
            self.assertNotIn("synthetic-secret", text)
        self.assertEqual(self.smtp.logins, [("records@campaign.example", "synthetic-secret")])  # it was used, only there


def _child(args):
    project, send_path, ident, signature, fingerprint, to, record, queue = args
    fake = MailboxIMAP()
    fake.messages = [dict(m) for m in json.loads(Path(record + ".mailbox").read_text(), object_hook=_decode)]
    result = ms.send_one(project, send_path, ident, actor="operator-1", page_fingerprint=fingerprint,
                         signature=signature, to=to, confirm=to, imap_factory=fake.factory, smtp_factory=SmtpStub(record=record), now=NOW)
    queue.put(result["state"])


def _decode(obj):
    return {k: bytes.fromhex(v[4:]) if isinstance(v, str) and v.startswith("hex:") else v for k, v in obj.items()}


class ConcurrencyTests(SendBase):
    def test_concurrent_clicks_serialized(self):
        self.on(daily_cap=1)
        other = self.second_draft(agency="agency-2")
        rows = {r["queue_id"]: r for r in md.placements(self.project)}
        record = str(self.root / "smtp-record")
        mailbox = [{**m, "raw": "hex:" + m["raw"].hex()} for m in self.fake.messages]
        Path(record + ".mailbox").write_text(json.dumps(mailbox))
        jobs = []
        for ident in (self.draft_row["id"], other):
            to = "" if ident == self.draft_row["id"] else OWNER
            info = ms.prepare(self.project, self.send_path, ident, to=to or None, imap_factory=self.imap(), now=NOW)
            jobs.append((str(self.project), self.send_path, ident, self.signer.sign(info["payload"].encode()),
                         rows[ident]["fingerprint"], to, record))
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        procs = [context.Process(target=_child, args=(job + (queue,),)) for job in jobs]
        for proc in procs:
            proc.start()
        states = sorted(queue.get(timeout=60) for _ in procs)
        for proc in procs:
            proc.join(30)
        self.assertEqual(states, ["refused", "sent"])
        self.assertEqual(Path(record).read_text().count("sent"), 1)


class TestModeTests(SendBase):
    def test_default_mode_refuses_other_recipient_before_send_steps(self):
        self.on(test_recipient=OWNER)
        data = json.loads(self.send_path.read_text())
        del data["live"]
        self.send_path.write_text(json.dumps(data))
        self.assertIs(ms.load_config(self.send_path)["live"], False)
        # Unthreaded form recipient is also constrained, before typed confirmation.
        other = self.second_draft()
        for ident, to in ((self.draft_row["id"], ""), (other, "other@example.invalid")):
            with self.subTest(ident=ident):
                info = ms.prepare(self.project, self.send_path, ident, to=to or None,
                                  imap_factory=self.imap(), now=NOW)
                self.assertEqual(info["code"], "test_mode_recipient")
                self.assertNotIn("payload", info)
                with mock.patch.object(ms._Dir, "_flock", side_effect=AssertionError("lock reached")), \
                        mock.patch.object(ms, "sent_today", side_effect=AssertionError("cap reached")):
                    fingerprint = next(r["fingerprint"] for r in md.placements(self.project) if r["queue_id"] == ident)
                    result = self.click(ident, sign=False, signature="x", to=to, fingerprint=fingerprint)
                self.assertEqual(result, {"state": "refused", "code": "test_mode_recipient"})
        self.assertEqual([(r["state"], r["code"]) for r in self.log()],
                         [("refused", "test_mode_recipient")] * 2)
        self.assertNotIn(OWNER, json.dumps(self.log()))
        self.assertNotIn("other@example.invalid", json.dumps(self.log()))
        self.assertEqual(aq.load_queue(self.project)[0]["status"], "approved")
        self.assertEqual(self.smtp.connections, 0)
        self.assertFalse(self.outbox_path.exists())

    def test_test_recipient_case_insensitive_and_existing_confirmation_still_required(self):
        self.on(live=False, test_recipient=OWNER.upper())
        data = json.loads(self.send_path.read_text())
        del data["live"]
        self.send_path.write_text(json.dumps(data))
        self.assertEqual(ms.load_config(self.send_path)["test_recipient"], OWNER)
        other = self.second_draft()
        self.assertEqual(self.click(other, to=OWNER)["code"], "confirmation_required")
        self.assertEqual(self.smtp.connections, 0)
        result = self.click(other, to=OWNER, confirm=OWNER)
        self.assertEqual(result["state"], "sent")
        self.assertEqual(self.smtp.connections, 1)
        self.assertEqual(self.smtp.messages[0]["To"], OWNER)
        self.assertEqual(self.click(other, to=OWNER, confirm=OWNER)["code"], "already_sent")
        self.assertEqual(self.smtp.connections, 1)

    def test_missing_test_recipient_refuses_preview_and_send(self):
        self.on(live=False)
        info = ms.prepare(self.project, self.send_path, self.draft_row["id"],
                          imap_factory=self.imap(), now=NOW)
        self.assertEqual(info["code"], "test_recipient_missing")
        self.assertNotIn("payload", info)
        self.assertEqual(self.click(sign=False, signature="x")["code"], "test_recipient_missing")
        self.assertEqual([r["state"] for r in self.log()], ["refused"])
        self.assertEqual(self.smtp.connections, 0)
        self.assertFalse(self.outbox_path.exists())

    def test_original_stops_win_in_test_mode(self):
        self.on(live=False, test_recipient="records-desk@agency.example")
        for stop in ms.kill_files(self.project, {"kill_file": str(self.root / "custom-stop")}):
            with self.subTest(stop=stop):
                path = Path(stop)
                path.parent.mkdir(exist_ok=True)
                self.configure(kill_file=str(self.root / "custom-stop"))
                path.write_text("")
                self.assertEqual(self.click(sign=False, signature="x")["code"], "kill_switch")
                path.unlink()
        self.configure(enabled=False)
        self.assertEqual(self.click(sign=False, signature="x")["code"], "send_disabled")
        self.assertEqual(self.smtp.connections, 0)
        self.assertFalse(self.outbox_path.exists())

    def test_config_rejects_bad_mode_and_recipient(self):
        for field, values, valid in (("live", ("yes", None, 1, []), False),
                                     ("test_recipient", ("bad", "", None, 1, [], "A <a@example.invalid>"), OWNER)):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.configure(**{field: value})
                    with self.assertRaises(ms.MailSendError) as error:
                        ms.load_config(self.send_path)
                    self.assertEqual(error.exception.code, "config_field:" + field)
            self.configure(**{field: valid})

    def test_init_test_recipient_and_switch_state(self):
        project = self.root / "fresh-project"
        project.mkdir(mode=0o700)
        args = list(self.init_args)
        args[2] = str(project)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ms.main(args + ["--test-recipient", OWNER.upper()]), 0)
        config = ms.load_config(ms.config_path(project))
        self.assertIs(config["live"], False)
        self.assertEqual(config["test_recipient"], OWNER)
        self.on(live=False, test_recipient=OWNER)
        for live in (False, True):
            self.configure(live=live)
            state = ms.switch_state(self.project, self.send_path, now=NOW)
            self.assertEqual(state["mode"], "live" if live else "test")
            self.assertEqual(state["test_recipient"], OWNER)
            self.assertTrue(state["can_send"])


if __name__ == "__main__":
    unittest.main()


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class CampaignDayTests(SendBase):
    """The console's daily budget counts the campaign's own local day (campaign.json timezone)."""
    PRIOR = "2026-11-04T23:00:00Z"
    BOUNDARY = datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc)  # Nov 4 17:00 Pacific, Nov 5 01:00 UTC
    PACIFIC = "America/Los_Angeles"

    def campaign(self, project=None, **doc):
        path = (project or self.project) / "campaign.json"
        path.write_text(json.dumps({"schema_version": 1, **doc}))
        path.chmod(0o600)

    def seed(self, rows, db=None):
        db = db or self.outbox_path
        outbox.Outbox(db).close()  # creates the real schema
        con = sqlite3.connect(db)
        try:
            for index, (stamp, state, kind, intent, agency) in enumerate(rows):
                column = "sending_at" if state == "sending" else "sent_at"
                con.execute("INSERT INTO outbox(idempotency_key, kind, request_id, agency_id, scope_version, channel, "
                            "draft_json, state, %s, created_at, updated_at) VALUES(?,?,?,?,1,'email',?,?,?,?,?)" % column,
                            ("seed-%s-%d" % (agency, index), kind, "req-" + agency, agency,
                             json.dumps({"intent_id": intent}), state, stamp, stamp, stamp))
            con.commit()
        finally:
            con.close()

    def prior(self, agency="agency-prior", stamp=None, state="sent"):
        self.seed([(stamp or self.PRIOR, state, "send_followup", "recq-" + agency, agency)])

    def test_second_fictional_campaign_cap_midnight(self):
        campaigns = {}
        for name, zone in (("Harbor Pilot", self.PACIFIC), ("Cedar Civic Records", "UTC")):
            root = self.root / name.split()[0].lower()
            project = root / "project"
            (project / "approvals").mkdir(parents=True, mode=0o700)
            self.campaign(project, timezone=zone, name=name)
            self.seed([(self.PRIOR, "sent", "send_followup", "recq-1", "agency-prior")], root / "outbox.sqlite")
            send = project / "approvals" / "send.json"
            send.write_text(json.dumps({**json.loads(self.send_path.read_text()), "enabled": True, "daily_cap": 1,
                                        "outbox_path": str(root / "outbox.sqlite")}))
            send.chmod(0o600)
            campaigns[name] = (project, send)
        expected = {"Harbor Pilot": (1, "daily_cap_reached", self.PACIFIC), "Cedar Civic Records": (0, None, "UTC")}
        for name in ("Harbor Pilot", "Cedar Civic Records", "Harbor Pilot"):  # A/B/A in one process
            state = ms.switch_state(*campaigns[name], now=self.BOUNDARY)
            self.assertEqual((state["count"], state["reason"], state["timezone"]), expected[name], name)

    def test_locked_cap_uses_campaign_timezone(self):
        self.on(daily_cap=1)
        self.prior()
        fingerprint = md.placements(self.project)[0]["fingerprint"]
        # Pacific (no campaign.json): the prior receipt is today's, so the locked check refuses.
        refused = self.click(now=self.BOUNDARY, sign=False, signature="x", fingerprint=fingerprint)
        self.assertEqual((refused["state"], refused["code"]), ("refused", "daily_cap_reached"))
        self.assertEqual(self.smtp.connections, 0)
        # UTC: the same receipt is yesterday's; preview and the locked check both allow it.
        self.campaign(timezone="UTC")
        info = self.prepared(now=self.BOUNDARY)
        self.assertEqual((info["switch"]["count"], info["switch"]["timezone"]), (0, "UTC"))
        sent = self.click(now=self.BOUNDARY)
        self.assertEqual((sent["state"], sent["code"]), ("sent", None))
        self.assertEqual(self.smtp.connections, 1)

    def test_locked_check_refuses_a_receipt_added_after_the_preview(self):
        self.on(daily_cap=1)
        self.campaign(timezone="UTC")
        self.prior()
        info = self.prepared(now=self.BOUNDARY)
        self.assertEqual(info["switch"]["count"], 0)
        self.prior(agency="agency-late", stamp="2026-11-05T00:30:00Z")  # lands between preview and click
        result = ms.send_one(self.project, self.send_path, self.draft_row["id"], actor="operator-1",
                             page_fingerprint=info["fingerprint"], signature=self.signer.sign(info["payload"].encode()),
                             imap_factory=self.imap(), smtp_factory=self.smtp, now=self.BOUNDARY)
        self.assertEqual((result["state"], result["code"]), ("refused", "daily_cap_reached"))
        self.assertEqual(self.smtp.connections, 0)
        self.assertEqual([r["state"] for r in self.outbox_rows() if r["agency_id"] == "agency-1"], ["proposed"])

    def test_campaign_day_dst_and_sending_rows(self):
        rows = [("2027-03-14T07:59:00Z", "sent"), ("2027-03-14T08:00:00Z", "sent"),
                ("2027-03-15T06:59:00Z", "sending"), ("2027-03-15T07:00:00Z", "sent"),
                ("2026-11-01T06:59:00Z", "sent"), ("2026-11-01T07:00:00Z", "sent"),
                ("2026-11-02T07:59:00Z", "sent"), ("2026-11-02T08:00:00Z", "sending"),
                ("2026-11-05T18:29:00Z", "sent"), ("2026-11-05T18:30:00Z", "sent")]
        self.seed([(stamp, state, "send_followup", "recq-%d" % i, "a%d" % i) for i, (stamp, state) in enumerate(rows)])
        # other kinds, non-recq intents, failed rows and malformed stamps never count
        self.seed([("2027-03-14T12:00:00Z", "sent", "send_followup", "other-1", "x1"),
                   ("2027-03-14T12:00:00Z", "sent", "send_email", "recq-x", "x2"),
                   ("2027-03-14T12:00:00Z", "failed", "send_followup", "recq-y", "x3"),
                   ("not-a-time", "sent", "send_followup", "recq-z", "x4")])
        for zone, now, expected in (
                (self.PACIFIC, "2027-03-14T09:00:00Z", 2),  # spring-forward day (23 h): 08:00Z .. 07:00Z next day
                (self.PACIFIC, "2027-03-14T20:00:00Z", 2),  # includes the still-sending row at 06:59Z
                (self.PACIFIC, "2027-03-15T07:30:00Z", 1),
                (self.PACIFIC, "2026-11-01T12:00:00Z", 2),  # fall-back day (25 h): 07:00Z .. 08:00Z next day
                (self.PACIFIC, "2026-11-02T09:00:00Z", 1),  # includes the still-sending row at 08:00Z
                ("UTC", "2027-03-14T12:00:00Z", 2),
                ("Asia/Kolkata", "2026-11-05T19:00:00Z", 1),  # local midnight is 18:30Z
                ("Asia/Kolkata", "2026-11-05T12:00:00Z", 1)):
            with self.subTest(zone=zone, now=now):
                self.assertEqual(ms.sent_today(str(self.outbox_path), _utc(now), timezone_name=zone), expected)
        legacy = ms.sent_today(str(self.outbox_path), _utc("2027-03-14T20:00:00Z"))
        self.assertEqual(legacy, 2)  # direct callers keep the Pacific default

    def test_invalid_campaign_timezone_never_contacts_transport(self):
        self.on(test_recipient=OWNER)
        path = self.project / "campaign.json"
        for raw in ('{"schema_version":1,"timezone":"No/Such_Zone"}', '{"schema_version":1,"timezone":null}', "{"):
            with self.subTest(raw=raw):
                path.write_text(raw)
                path.chmod(0o600)
                before = (path.read_bytes(), self.send_path.read_bytes())
                state = ms.switch_state(self.project, self.send_path, now=NOW)
                self.assertEqual((state["can_send"], state["reason"], state["cap"], state["count"]),
                                 (False, "campaign_timezone_invalid", None, None))
                info = ms.prepare(self.project, self.send_path, self.draft_row["id"], imap_factory=self.imap(), now=NOW)
                self.assertEqual(info["code"], "campaign_timezone_invalid")
                self.assertNotIn("payload", info)
                logged = len(self.log())
                self.fake.calls.clear()
                result = ms.send_one(self.project, self.send_path, self.draft_row["id"], actor="operator-1",
                                     page_fingerprint=md.placements(self.project)[0]["fingerprint"], signature="x",
                                     imap_factory=self.imap(), smtp_factory=self.smtp, now=NOW)
                self.assertEqual(result, {"state": "refused", "code": "campaign_timezone_invalid"})
                self.assertEqual(len(self.log()), logged + 1)
                self.assertEqual(self.log()[-1]["code"], "campaign_timezone_invalid")
                self.assertEqual((self.smtp.connections, self.fake.calls), (0, []))
                self.assertFalse(self.outbox_path.exists())
                self.assertEqual((path.read_bytes(), self.send_path.read_bytes()), before)
                self.assertEqual(aq.load_queue(self.project)[0]["status"], "approved")
        path.unlink()  # a missing file, or a file without the key, keeps the legacy Pacific day
        self.assertEqual(ms.switch_state(self.project, self.send_path, now=NOW)["timezone"], self.PACIFIC)
        self.campaign(name="No zone key")
        self.assertEqual(ms.switch_state(self.project, self.send_path, now=NOW)["timezone"], self.PACIFIC)
