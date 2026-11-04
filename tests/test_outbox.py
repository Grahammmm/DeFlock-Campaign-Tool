"""Outbox journal: one send per key, caps, 402 blocked, ambiguous failures block resends."""
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from campaign_tool import outbox as ob

from tests.approval_signing import Approvers

APPROVERS = Approvers("original@example.invalid", "new@example.invalid", "o@example.invalid", "organizer@example.invalid")


def setUpModule():
    APPROVERS.start()


def tearDownModule():
    APPROVERS.stop()


class StubSmtpServer:
    """Minimal threaded SMTP stub: records DATA payloads; can drop the connection mid-DATA."""

    def __init__(self, drop_after_data=False, refused_recipients=()):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.messages = []
        self.drop_after_data = drop_after_data
        self.refused_recipients = frozenset(refused_recipients)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        f = conn.makefile("rb")
        def send(line):
            conn.sendall((line + "\r\n").encode())
        send("220 stub ESMTP")
        while True:
            line = f.readline()
            if not line:
                break
            cmd = line.decode(errors="replace").strip()
            upper = cmd.upper()
            if upper.startswith("EHLO") or upper.startswith("HELO"):
                send("250-stub"); send("250 8BITMIME")
            elif upper.startswith("RCPT TO"):
                recipient = cmd.split(":", 1)[1].strip().strip("<>")
                send("550 recipient refused" if recipient in self.refused_recipients else "250 OK")
            elif upper.startswith("MAIL FROM"):
                send("250 OK")
            elif upper == "DATA":
                send("354 End data with <CR><LF>.<CR><LF>")
                buf = io.BytesIO()
                while True:
                    l = f.readline()
                    if not l or l == b".\r\n":
                        break
                    buf.write(l)
                self.messages.append(buf.getvalue())
                if self.drop_after_data:
                    conn.close()
                    return
                send("250 queued")
            elif upper == "QUIT":
                send("221 bye"); conn.close(); return
            else:
                send("250 OK")
        conn.close()

    def close(self):
        self.sock.close()


def draft(request_id="req_0000000000000001", agency="ca-example-police", channel="email", fee=0, kind="send_request", to="records@example.invalid"):
    return ob.RequestDraft(request_id=request_id, agency_id=agency, scope_version=1, channel=channel,
                           subject="Synthetic records request", body="Synthetic body. No real content.",
                           to=to, from_addr="requests@campaign.example.invalid", fee_cap_cents=fee, kind=kind)


class Clock:
    def __init__(self, stamp="2026-09-30T10:00:00Z"):
        self.stamp = stamp

    def __call__(self):
        return self.stamp


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.box = ob.Outbox(Path(self.tmp.name) / "private" / "outbox.sqlite", campaign_fee_cap_cents=5000, clock=self.clock)

    def tearDown(self):
        self.box.close()
        self.tmp.cleanup()

    def test_fresh_retry_preserves_failed_approval_and_still_applies_caps(self):
        row = self.box.propose(draft(request_id="req_failed"))
        key = row["idempotency_key"]
        APPROVERS.approve(self.box, key, "original@example.invalid")
        with self.assertRaises(ob.OutboxError):
            self.box.send(key, lambda d: (_ for _ in ()).throw(ob.OutboxError("definite refusal")))
        with self.assertRaises(ob.OutboxError):
            APPROVERS.approve_retry(self.box, key, "new@example.invalid", self.clock.stamp)
        self.assertFalse(self.box.db.in_transaction)
        self.clock.stamp = "2026-09-30T10:01:00Z"
        retried = APPROVERS.approve_retry(self.box, key, "new@example.invalid", self.clock.stamp)
        self.assertEqual(retried["state"], "approved")
        history = self.box.db.execute("SELECT * FROM outbox_retry_approval").fetchone()
        self.assertEqual(history["prior_approved_by"], "original@example.invalid")
        self.assertEqual(history["prior_error"], "definite refusal")
        self.assertEqual(history["approved_by"], "new@example.invalid")
        with self.assertRaises(ob.OutboxError):
            APPROVERS.approve_retry(self.box, key, "new@example.invalid", self.clock.stamp)
        # Consuming the daily cap elsewhere must still block the approved retry.
        other = self.box.propose(draft(request_id="req_other"))
        APPROVERS.approve(self.box, other["idempotency_key"], "new@example.invalid")
        self.box.send(other["idempotency_key"], lambda d: ob.ProviderReceipt("email", "synthetic-other"))
        with self.assertRaises(ob.Blocked) as caught:
            self.box.send(key, lambda d: self.fail("daily cap must apply to retry"))
        self.assertEqual(caught.exception.reason, "daily_agency_cap")

    def test_retry_cannot_clear_uncertainty_or_saved_provider_evidence(self):
        row = self.box.propose(draft()); key = row["idempotency_key"]
        APPROVERS.approve(self.box, key, "original@example.invalid")
        with self.assertRaises(ob.AmbiguousFailure):
            self.box.send(key, lambda d: (_ for _ in ()).throw(ob.AmbiguousFailure("unknown")))
        self.clock.stamp = "2026-09-30T10:01:00Z"
        with self.assertRaises(ob.OutboxError):
            APPROVERS.approve_retry(self.box, key, "new@example.invalid", self.clock.stamp)
        self.assertEqual(self.box.row(key)["state"], "sending")
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM outbox_retry_approval").fetchone()[0], 0)
        self.box.reconcile(key, "not_delivered", "owner@example.invalid")
        self.clock.stamp = "2026-09-30T10:02:00Z"
        APPROVERS.approve_retry(self.box, key, "new@example.invalid", self.clock.stamp)
        self.assertEqual(self.box.db.execute("SELECT resolved_by FROM outbox_retry_approval").fetchone()[0], "owner@example.invalid")
        self.box._set(key, state="failed", provider_receipt=ob.ProviderReceipt("email", "known-message").to_json())
        self.clock.stamp = "2026-09-30T10:03:00Z"
        with self.assertRaises(ob.OutboxError):
            APPROVERS.approve_retry(self.box, key, "new@example.invalid", self.clock.stamp)

    def test_retry_audit_and_transition_roll_back_together(self):
        row = self.box.propose(draft()); key = row["idempotency_key"]
        APPROVERS.approve(self.box, key, "original@example.invalid")
        self.box._set(key, state="failed", error="definite refusal")
        self.clock.stamp = "2026-09-30T10:01:00Z"
        self.box.db.execute("CREATE TRIGGER synthetic_abort_retry BEFORE UPDATE ON outbox WHEN NEW.state='approved' "
            "BEGIN SELECT RAISE(ABORT,'synthetic transition failure'); END")
        with self.assertRaises(Exception):
            APPROVERS.approve_retry(self.box, key, "new@example.invalid", self.clock.stamp)
        self.assertEqual(self.box.row(key)["state"], "failed")
        self.assertFalse(self.box.db.in_transaction)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM outbox_retry_approval").fetchone()[0], 0)

    def test_journal_permissions_and_key(self):
        self.assertEqual(os.stat(self.box.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.box.path.parent).st_mode & 0o777, 0o700)
        k = ob.idempotency_key("send_request", "req_x", 1)
        self.assertEqual(len(k), 64)
        self.assertNotEqual(k, ob.idempotency_key("send_request", "req_x", 2))
        self.assertNotEqual(k, ob.idempotency_key("send_followup", "req_x", 1))

    def test_smtp_stub_receives_one_message_and_second_send_is_noop(self):
        server = StubSmtpServer()
        try:
            settings = ob.SmtpSettings("127.0.0.1", server.port, starttls=False)
            row = self.box.propose(draft())
            self.assertEqual(row["state"], "proposed")
            with self.assertRaises(ob.Blocked) as ctx:
                self.box.send(row["idempotency_key"], lambda d: ob.send_email(d, settings))
            self.assertEqual(ctx.exception.reason, "not_approved")
            APPROVERS.approve(self.box, row["idempotency_key"], "organizer@example.invalid")
            sent = self.box.send(row["idempotency_key"], lambda d: ob.send_email(d, settings))
            self.assertEqual(sent["state"], "sent")
            receipt = json.loads(sent["provider_receipt"])
            self.assertEqual(receipt["channel"], "email")
            self.assertIn("@", receipt["provider_message_id"])
            # second send with the same key does not touch the transport
            again = self.box.send(row["idempotency_key"], lambda d: self.fail("transport must not run"))
            self.assertEqual(again["state"], "sent")
            self.assertEqual(len(server.messages), 1)
            self.assertIn(b"Message-ID:", server.messages[0])
            self.assertIn(b"Subject: Synthetic records request", server.messages[0])
            # proposing the same draft again returns the sent row
            self.assertEqual(self.box.propose(draft())["state"], "sent")
        finally:
            server.close()

    def test_partial_smtp_acceptance_stays_held_and_cannot_resend(self):
        server = StubSmtpServer(refused_recipients=("refused@example.invalid",))
        try:
            settings = ob.SmtpSettings("127.0.0.1", server.port, starttls=False)
            row = self.box.propose(draft(to="accepted@example.invalid, refused@example.invalid"))
            key = row["idempotency_key"]
            APPROVERS.approve(self.box, key, "organizer@example.invalid")
            with self.assertRaises(ob.AmbiguousFailure):
                self.box.send(key, lambda d: ob.send_email(d, settings))
            held = self.box.row(key)
            self.assertEqual(held["state"], "sending")
            self.assertIsNotNone(held["sending_at"])
            self.assertIsNone(held["sent_at"])
            self.assertIsNone(held["provider_receipt"])
            self.assertIn("partial recipient acceptance", held["error"])
            with self.assertRaises(ob.Blocked) as caught:
                self.box.send(key, lambda d: self.fail("held send must not retry"))
            self.assertEqual(caught.exception.reason, "ambiguous_send_unresolved")
            self.assertEqual(len(server.messages), 1)
            # A changed scope/key cannot escape the unresolved request-level hold.
            changed = draft(to="accepted@example.invalid, refused@example.invalid")
            changed.scope_version = 2
            next_row = self.box.propose(changed)
            APPROVERS.approve(self.box, next_row["idempotency_key"], "organizer@example.invalid")
            with self.assertRaises(ob.Blocked) as caught:
                self.box.send(next_row["idempotency_key"], lambda d: self.fail("new key must not bypass hold"))
            self.assertEqual(caught.exception.reason, "ambiguous_send_unresolved")
        finally:
            server.close()

    def test_all_smtp_recipients_refused_is_a_definite_failure_without_data(self):
        server = StubSmtpServer(refused_recipients=("refused@example.invalid",))
        try:
            settings = ob.SmtpSettings("127.0.0.1", server.port, starttls=False)
            row = self.box.propose(draft(to="refused@example.invalid"))
            key = row["idempotency_key"]
            APPROVERS.approve(self.box, key, "organizer@example.invalid")
            with self.assertRaises(ob.OutboxError) as caught:
                self.box.send(key, lambda d: ob.send_email(d, settings))
            self.assertNotIsInstance(caught.exception, ob.AmbiguousFailure)
            self.assertEqual(self.box.row(key)["state"], "failed")
            self.assertIsNone(self.box.row(key)["sending_at"])
            self.assertEqual(server.messages, [])
        finally:
            server.close()

    def test_daily_agency_cap(self):
        fake = lambda d: ob.ProviderReceipt("email", "id-" + d.request_id)
        a = self.box.propose(draft(request_id="req_a"))
        b = self.box.propose(draft(request_id="req_b"))
        APPROVERS.approve(self.box, a["idempotency_key"], "o@example.invalid")
        APPROVERS.approve(self.box, b["idempotency_key"], "o@example.invalid")
        self.assertEqual(self.box.send(a["idempotency_key"], fake)["state"], "sent")
        with self.assertRaises(ob.Blocked) as ctx:
            self.box.send(b["idempotency_key"], fake)
        self.assertEqual(ctx.exception.reason, "daily_agency_cap")
        # the cap is transient: the row stays approved for tomorrow
        self.assertEqual(self.box.row(b["idempotency_key"])["state"], "approved")
        # a different agency is unaffected
        c = self.box.propose(draft(request_id="req_c", agency="ca-other-sheriff"))
        APPROVERS.approve(self.box, c["idempotency_key"], "o@example.invalid")
        self.assertEqual(self.box.send(c["idempotency_key"], fake)["state"], "sent")

    def test_fee_cap(self):
        row = self.box.propose(draft(fee=9000))
        APPROVERS.approve(self.box, row["idempotency_key"], "o@example.invalid")
        with self.assertRaises(ob.Blocked) as ctx:
            self.box.send(row["idempotency_key"], lambda d: self.fail("must not send"))
        self.assertEqual(ctx.exception.reason, "fee_cap_exceeded")

    def test_default_fee_cap_blocks_any_fee_and_none_lifts_it(self):
        box = ob.Outbox(Path(self.tmp.name) / "default" / "outbox.sqlite", clock=self.clock)
        try:
            row = box.propose(draft(fee=1))
            APPROVERS.approve(box, row["idempotency_key"], "o@example.invalid")
            with self.assertRaises(ob.Blocked) as ctx:
                box.send(row["idempotency_key"], lambda d: self.fail("must not send"))
            self.assertEqual(ctx.exception.reason, "fee_cap_exceeded")
        finally:
            box.close()
        box = ob.Outbox(Path(self.tmp.name) / "unlimited" / "outbox.sqlite", campaign_fee_cap_cents=None, clock=self.clock)
        try:
            row = box.propose(draft(fee=999999))
            APPROVERS.approve(box, row["idempotency_key"], "o@example.invalid")
            sent = box.send(row["idempotency_key"], lambda d: ob.ProviderReceipt("email", "id-1"))
            self.assertEqual(sent["state"], "sent")
        finally:
            box.close()

    def test_concurrent_send_claims_the_row_once(self):
        # Two callers see the row 'approved' at the same time; the conditional claim lets
        # only one transport run and the other is told to reconcile instead of resending.
        row = self.box.propose(draft())
        key = row["idempotency_key"]
        APPROVERS.approve(self.box, key, "o@example.invalid")
        second = ob.Outbox(self.box.path, campaign_fee_cap_cents=5000, clock=self.clock)
        calls = []

        def transport(d):
            calls.append(d.subject)
            with self.assertRaises(ob.Blocked) as ctx:
                second.send(key, lambda dd: calls.append("second " + dd.subject))
            self.assertEqual(ctx.exception.reason, "ambiguous_send_unresolved")
            return ob.ProviderReceipt("email", "id-1")

        try:
            self.assertEqual(self.box.send(key, transport)["state"], "sent")
        finally:
            second.close()
        self.assertEqual(calls, ["Synthetic records request"])

    def test_smtp_connection_failure_before_data_is_a_plain_failure(self):
        row = self.box.propose(draft())
        key = row["idempotency_key"]
        APPROVERS.approve(self.box, key, "o@example.invalid")
        settings = ob.SmtpSettings(host="127.0.0.1", port=1, username="", password="", starttls=False, timeout=1)
        with self.assertRaises(ob.OutboxError) as ctx:
            self.box.send(key, lambda d: ob.send_email(d, settings))
        self.assertNotIsInstance(ctx.exception, ob.AmbiguousFailure)
        self.assertEqual(self.box.row(key)["state"], "failed")

    def test_starttls_uses_a_verifying_context(self):
        seen = {}

        class Smtp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def ehlo(self):
                pass

            def starttls(self, context=None):
                seen["context"] = context

            def login(self, u, p):
                pass

            def send_message(self, m):
                return {}

        settings = ob.SmtpSettings(host="smtp.example.invalid", port=587, username="", password="", starttls=True, timeout=1)
        ob.send_email(draft(), settings, smtp_factory=Smtp)
        self.assertIsNotNone(seen["context"])
        self.assertTrue(seen["context"].check_hostname)

    def test_muckrock_402_blocked_and_success(self):
        def opener_402(request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, 402, "Payment Required", {}, io.BytesIO(b"{}"))
        row = self.box.propose(draft(channel="muckrock", to="123"))
        APPROVERS.approve(self.box, row["idempotency_key"], "o@example.invalid")
        with self.assertRaises(ob.Blocked) as ctx:
            self.box.send(row["idempotency_key"], lambda d: ob.file_muckrock(d, "tok", opener=opener_402))
        self.assertEqual(ctx.exception.reason, "no_credits")
        self.assertEqual(self.box.row(row["idempotency_key"])["state"], "blocked")

        seen = {}
        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def opener_ok(request, timeout=0):
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data.decode())
            return Resp(json.dumps({"requests": [{"id": 4242, "absolute_url": "https://www.muckrock.com/foi/-/4242/", "status": "submitted"}]}).encode())
        row2 = self.box.propose(draft(request_id="req_mr2", agency="ca-mr", channel="muckrock", to="77"))
        APPROVERS.approve(self.box, row2["idempotency_key"], "o@example.invalid")
        sent = self.box.send(row2["idempotency_key"], lambda d: ob.file_muckrock(d, "tok", opener=opener_ok))
        self.assertEqual(sent["state"], "sent")
        self.assertEqual(json.loads(sent["provider_receipt"])["provider_message_id"], "4242")
        self.assertTrue(seen["url"].endswith("/foia/"))
        self.assertEqual(seen["auth"], "Token tok")
        self.assertEqual(seen["body"]["agencies"], [77])

    def test_ambiguous_failure_blocks_resend_until_reconciled(self):
        server = StubSmtpServer(drop_after_data=True)
        try:
            settings = ob.SmtpSettings("127.0.0.1", server.port, starttls=False)
            row = self.box.propose(draft())
            APPROVERS.approve(self.box, row["idempotency_key"], "o@example.invalid")
            with self.assertRaises(ob.AmbiguousFailure):
                self.box.send(row["idempotency_key"], lambda d: ob.send_email(d, settings))
            self.assertEqual(self.box.row(row["idempotency_key"])["state"], "sending")
            self.assertEqual(len(self.box.unresolved()), 1)
            with self.assertRaises(ob.Blocked) as ctx:
                self.box.send(row["idempotency_key"], lambda d: self.fail("must not resend"))
            self.assertEqual(ctx.exception.reason, "ambiguous_send_unresolved")
            # a follow-up on the same request is blocked too
            fu = self.box.propose(draft(kind="send_followup"))
            APPROVERS.approve(self.box, fu["idempotency_key"], "o@example.invalid")
            with self.assertRaises(ob.Blocked):
                self.box.send(fu["idempotency_key"], lambda d: self.fail("must not send"))
            resolved = self.box.reconcile(row["idempotency_key"], "delivered", "o@example.invalid")
            self.assertEqual(resolved["state"], "sent")
            self.assertEqual(self.box.unresolved(), [])
            with self.assertRaises(ob.OutboxError):
                self.box.reconcile(row["idempotency_key"], "delivered", "o@example.invalid")
        finally:
            server.close()

    def test_muckrock_5xx_is_ambiguous_and_auth_failure_is_failed(self):
        def opener_503(request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, 503, "Unavailable", {}, io.BytesIO(b""))
        row = self.box.propose(draft(request_id="req_mr5", agency="ca-mr5", channel="muckrock", to="5"))
        APPROVERS.approve(self.box, row["idempotency_key"], "o@example.invalid")
        with self.assertRaises(ob.AmbiguousFailure):
            self.box.send(row["idempotency_key"], lambda d: ob.file_muckrock(d, "tok", opener=opener_503))
        self.assertEqual(self.box.row(row["idempotency_key"])["state"], "sending")
        self.assertEqual(len(self.box.unresolved()), 1)
        self.assertEqual(self.box.reconcile(row["idempotency_key"], "not_delivered", "o@example.invalid")["state"], "failed")
        def opener_401(request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
        row2 = self.box.propose(draft(request_id="req_mr6", agency="ca-mr6", channel="muckrock", to="6"))
        APPROVERS.approve(self.box, row2["idempotency_key"], "o@example.invalid")
        with self.assertRaises(ob.OutboxError):
            self.box.send(row2["idempotency_key"], lambda d: ob.file_muckrock(d, "tok", opener=opener_401))
        self.assertEqual(self.box.row(row2["idempotency_key"])["state"], "failed")
        with self.assertRaises(ValueError):
            ob.file_muckrock(draft(channel="muckrock", to="not-an-id"), "tok", opener=opener_401)
        with self.assertRaises(ValueError):
            ob.file_muckrock(draft(channel="muckrock", to="7"), "", opener=opener_401)

    def test_followup_threads_in_reply_to_and_shares_the_daily_cap(self):
        server = StubSmtpServer()
        try:
            settings = ob.SmtpSettings("127.0.0.1", server.port, starttls=False)
            first = self.box.propose(draft())
            APPROVERS.approve(self.box, first["idempotency_key"], "o@example.invalid")
            self.box.send(first["idempotency_key"], lambda d: ob.send_email(d, settings))
            follow = draft(kind="send_followup")
            follow.in_reply_to = "<ack-0042@example.invalid>"
            row = self.box.propose(follow)
            self.assertNotEqual(row["idempotency_key"], first["idempotency_key"])
            APPROVERS.approve(self.box, row["idempotency_key"], "o@example.invalid")
            # same agency, same UTC day: the follow-up waits for tomorrow
            with self.assertRaises(ob.Blocked) as ctx:
                self.box.send(row["idempotency_key"], lambda d: ob.send_email(d, settings))
            self.assertEqual(ctx.exception.reason, "daily_agency_cap")
            self.clock.stamp = "2026-10-01T10:00:00Z"
            sent = self.box.send(row["idempotency_key"], lambda d: ob.send_email(d, settings))
            self.assertEqual(sent["state"], "sent")
            self.assertEqual(len(server.messages), 2)
            self.assertIn(b"In-Reply-To: <ack-0042@example.invalid>", server.messages[1])
            self.assertIn(b"References: <ack-0042@example.invalid>", server.messages[1])
        finally:
            server.close()

    def test_transport_crash_keeps_sending_and_approval_needs_identity(self):
        row = self.box.propose(draft(request_id="req_crash", agency="ca-crash"))
        with self.assertRaises(ob.OutboxError):
            APPROVERS.approve(self.box, row["idempotency_key"], "")
        APPROVERS.approve(self.box, row["idempotency_key"], "o@example.invalid")
        def boom(d):
            raise RuntimeError("socket vanished")
        with self.assertRaises(ob.AmbiguousFailure):
            self.box.send(row["idempotency_key"], boom)
        kept = self.box.row(row["idempotency_key"])
        self.assertEqual(kept["state"], "sending")
        self.assertIn("ambiguous", kept["error"])
        with self.assertRaises(ob.OutboxError):
            self.box.reconcile(row["idempotency_key"], "delivered", "")
        with self.assertRaises(ValueError):
            self.box.reconcile(row["idempotency_key"], "maybe", "o@example.invalid")
        with self.assertRaises(ob.OutboxError):
            APPROVERS.approve(self.box, row["idempotency_key"], "o@example.invalid")

    def test_cli_round_trip(self):
        draft_path = Path(self.tmp.name) / "draft.json"
        draft_path.write_text(draft().to_json())
        journal = str(Path(self.tmp.name) / "private" / "cli.sqlite")
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ob.main(["--journal", journal, "propose", str(draft_path)]), 0)
            key = json.loads(out.getvalue().splitlines()[-1])["idempotency_key"]
            payload = io.StringIO()
            with redirect_stdout(payload):
                self.assertEqual(ob.main(["--journal", journal, "payload", key]), 0)
            sig = Path(self.tmp.name) / "approval.sig"
            sig.write_text(APPROVERS.signers["o@example.invalid"].sign(payload.getvalue().encode("utf-8")))
            self.assertEqual(ob.main(["--journal", journal, "approve", key, "--signature", str(sig)]), 0)
            self.assertEqual(ob.main(["--journal", journal, "list"]), 0)
        self.assertIn('"state": "approved"', out.getvalue())
        self.assertIn('"approved_by": "o@example.invalid"', out.getvalue())


if __name__ == "__main__":
    unittest.main()


class ConcurrentCapTests(unittest.TestCase):
    def test_different_keys_cannot_both_pass_one_agency_daily_cap(self):
        self._race_caps()

    def test_unresolved_request_hold_is_atomic_across_scope_keys(self):
        self._race_caps(same_request=True)

    def _race_caps(self, same_request=False):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "outbox.sqlite"
            setup = ob.Outbox(path)
            keys = []
            for request in ["req_race_first", "req_race_second"]:
                item = draft(request_id="req_same" if same_request else request)
                item.scope_version = len(keys) + 1
                row = setup.propose(item)
                APPROVERS.approve(setup, row["idempotency_key"], "organizer@example.invalid")
                keys.append(row["idempotency_key"])
            setup.close()
            ready = [threading.Event(), threading.Event()]
            begin = [threading.Event(), threading.Event()]
            first_checked = threading.Event(); release_first = threading.Event()
            second_sent = threading.Event(); calls = []; errors = []
            def worker(index):
                box = ob.Outbox(path, daily_agency_cap=100 if same_request else 1)
                original = box._check
                if index == 0:
                    def pause_after_check(row):
                        result = original(row)
                        first_checked.set()
                        if not release_first.wait(5): raise RuntimeError("synthetic synchronization timeout")
                        return result
                    box._check = pause_after_check
                ready[index].set()
                try:
                    if not begin[index].wait(5): raise RuntimeError("synthetic start timeout")
                    def transport(item):
                        calls.append(item.request_id)
                        if index == 1: second_sent.set()
                        if same_request and index == 0: raise ob.AmbiguousFailure("synthetic unknown delivery")
                        return ob.ProviderReceipt("email", "receipt-" + item.request_id)
                    box.send(keys[index], transport)
                except Exception as exc: errors.append(exc)
                finally: box.close()
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
            for thread in threads: thread.start()
            try:
                for event in ready: self.assertTrue(event.wait(5))
                begin[0].set(); self.assertTrue(first_checked.wait(5)); begin[1].set()
                # Old code can send the second key in this gap; atomic code waits for the first claim.
                second_sent.wait(1)
            finally:
                release_first.set()
                for event in begin: event.set()
                for thread in threads: thread.join(6)
            self.assertFalse(any(t.is_alive() for t in threads))
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(errors), 2 if same_request else 1)
            blocked = [e for e in errors if isinstance(e, ob.Blocked)]
            self.assertEqual(len(blocked), 1)
            self.assertEqual(blocked[0].reason, "ambiguous_send_unresolved" if same_request else "daily_agency_cap")


    def test_transaction_ends_before_transport_and_guard_refusals_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "outbox.sqlite"
            box = ob.Outbox(path)
            key = box.propose(draft())["idempotency_key"]
            with self.assertRaises(ob.Blocked): box.send(key, lambda _: self.fail("not approved"))
            self.assertFalse(box.db.in_transaction)
            APPROVERS.approve(box, key, "organizer@example.invalid")
            box.db.execute("CREATE TABLE synthetic_probe (value TEXT)")
            def transport(_):
                self.assertFalse(box.db.in_transaction)
                writer = ob.sqlite3.connect(path, timeout=0)
                try:
                    writer.execute("INSERT INTO synthetic_probe VALUES ('transport-write')")
                    writer.commit()
                finally: writer.close()
                return ob.ProviderReceipt("email", "synthetic-unlocked")
            try:
                self.assertEqual(box.send(key, transport)["state"], "sent")
                self.assertFalse(box.db.in_transaction)
                self.assertEqual(box.send(key, lambda _: self.fail("replayed sent row"))["state"], "sent")
                self.assertFalse(box.db.in_transaction)
            finally: box.close()


BAD_FEES = [-1, True, False, 0.9, 1.0, float("nan"), float("inf"), float("-inf"), "0", None, [], {}, [1], 2 ** 63]
FEE_MESSAGE = "fee_cap_cents must be an integer between 0 and 9223372036854775807"
CAMPAIGN_FEE_MESSAGE = "campaign_fee_cap_cents must be an integer between 0 and 9223372036854775807"
MAX_FEE = 2 ** 63 - 1
REPO_ROOT = Path(__file__).resolve().parents[1]


def snapshot(box):
    return ([tuple(r) for r in box.db.execute("SELECT *, typeof(fee_cap_cents) FROM outbox ORDER BY idempotency_key")],
            [tuple(r) for r in box.db.execute("SELECT * FROM outbox_retry_approval ORDER BY event_id")])


class OutboxFeeTests(unittest.TestCase):
    """RF0210-3: fees are exact integer cents and the cap reads the fee that was signed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.n = 0
        refuse = unittest.mock.patch.object(socket.socket, "connect", side_effect=AssertionError("outbound socket"))
        refuse.start()
        self.addCleanup(refuse.stop)

    def new_box(self, cap=5000, name=None):
        self.n += 1
        box = ob.Outbox(Path(self.tmp.name) / (name or f"j{self.n}") / "outbox.sqlite", campaign_fee_cap_cents=cap, clock=self.clock)
        self.addCleanup(box.close)
        return box

    def approved(self, box, **kw):
        key = box.propose(draft(**kw))["idempotency_key"]
        APPROVERS.approve(box, key, "o@example.invalid")
        return key

    def test_propose_fee_requires_exact_nonnegative_sqlite_integer(self):
        box = self.new_box()
        for bad in BAD_FEES:
            with self.subTest(fee=repr(bad)):
                item = draft(request_id="req_fresh")
                item.fee_cap_cents = bad
                with self.assertRaises(ob.OutboxError) as ctx:
                    box.propose(item)
                self.assertNotIsInstance(ctx.exception, ob.Blocked)
                self.assertEqual(str(ctx.exception), FEE_MESSAGE)
                self.assertEqual(snapshot(box), ([], []))
        # An existing key: the invalid replay is refused (not returned as the old row) and nothing changes.
        key = box.propose(draft(request_id="req_dup", fee=7))["idempotency_key"]
        other = box.propose(draft(request_id="req_other", agency="ca-other", fee=3))
        before = snapshot(box)
        for bad in BAD_FEES:
            with self.subTest(replay=repr(bad)):
                item = draft(request_id="req_dup")
                item.fee_cap_cents = bad
                with self.assertRaises(ob.OutboxError):
                    box.propose(item)
                self.assertEqual(snapshot(box), before)
        self.assertEqual(box.propose(draft(request_id="req_dup", fee=7))["idempotency_key"], key)  # valid replay unchanged
        self.assertEqual(box.row(other["idempotency_key"]), other)

    def test_fee_boundaries_preserve_exact_draft_json(self):
        for fee in (0, 1, MAX_FEE):
            with self.subTest(fee=fee):
                box = self.new_box(None)
                row = box.propose(draft(request_id=f"req_edge_{fee}", fee=fee))
                self.assertEqual((row["fee_cap_cents"], json.loads(row["draft_json"])["fee_cap_cents"]), (fee, fee))
                self.assertIs(type(json.loads(row["draft_json"])["fee_cap_cents"]), int)
                self.assertEqual(box.db.execute("SELECT typeof(fee_cap_cents) FROM outbox").fetchone()[0], "integer")
        # (campaign cap, fee, expected reason or None); every case uses its own journal and agency.
        cases = [(5000, 4999, None), (5000, 5000, None), (5000, 5001, "fee_cap_exceeded"), (0, 0, None), (0, 1, "fee_cap_exceeded"),
                 (None, MAX_FEE, None), (MAX_FEE, MAX_FEE, None), (MAX_FEE - 1, MAX_FEE, "fee_cap_exceeded"), (MAX_FEE, 0, None)]
        for cap, fee, reason in cases:
            with self.subTest(cap=cap, fee=fee):
                box = self.new_box(cap)
                key = self.approved(box, fee=fee)
                sent = []
                transport = lambda d: sent.append(d.fee_cap_cents) or ob.ProviderReceipt("email", "id")
                if reason:
                    with self.assertRaises(ob.Blocked) as ctx:
                        box.send(key, transport)
                    self.assertEqual((ctx.exception.reason, ctx.exception.detail, sent), (reason, f"{fee} > campaign cap {cap}", []))
                else:
                    self.assertEqual(box.send(key, transport)["state"], "sent")
                    self.assertEqual(sent, [fee])

    def test_campaign_fee_validation_precedes_filesystem_effects(self):
        root = Path(self.tmp.name)
        for bad in (-1, True, False, 1.5, 0.0, float("nan"), float("inf"), "5", "", [], {}, 2 ** 63):
            with self.subTest(cap=repr(bad)):
                with unittest.mock.patch.object(ob.sqlite3, "connect") as connect:
                    with self.assertRaises(ob.OutboxError) as ctx:
                        ob.Outbox(root / "absent" / "outbox.sqlite", campaign_fee_cap_cents=bad)
                self.assertEqual(str(ctx.exception), CAMPAIGN_FEE_MESSAGE)
                connect.assert_not_called()
                self.assertFalse((root / "absent").exists())
        # An existing directory and database keep their bytes and modes.
        existing = root / "existing"
        existing.mkdir(mode=0o755)
        os.chmod(existing, 0o755)
        db_path = existing / "outbox.sqlite"
        db_path.write_bytes(b"synthetic not-a-database bytes")
        os.chmod(db_path, 0o644)
        for bad in (-1, 1.5, "5", True, 2 ** 63):
            with self.assertRaises(ob.OutboxError):
                ob.Outbox(db_path, campaign_fee_cap_cents=bad)
        self.assertEqual((db_path.read_bytes(), os.stat(db_path).st_mode & 0o777, os.stat(existing).st_mode & 0o777),
                         (b"synthetic not-a-database bytes", 0o644, 0o755))
        self.assertEqual(sorted(p.name for p in existing.iterdir()), ["outbox.sqlite"])
        for ok in (0, 1, MAX_FEE, None):
            with self.subTest(accepted=ok):
                box = ob.Outbox(root / f"ok{ok}" / "outbox.sqlite", campaign_fee_cap_cents=ok)
                self.assertEqual(box.campaign_fee_cap_cents, ok)
                box.close()
        default = ob.Outbox(root / "default" / "outbox.sqlite")
        self.addCleanup(default.close)
        self.assertEqual(default.campaign_fee_cap_cents, 0)

    def test_invalid_fee_guard_preserves_other_state_rules(self):
        box = self.new_box(None)
        # Signed text mutation still reports not_approved even when the fee column also disagrees.
        key = self.approved(box, request_id="req_text", agency="ca-text", fee=5000)
        changed = box.row(key)["draft_json"].replace("Synthetic body", "Changed body")
        box.db.execute("UPDATE outbox SET draft_json=?, fee_cap_cents=0 WHERE idempotency_key=?", (changed, key))
        with self.assertRaises(ob.Blocked) as ctx:
            box.send(key, lambda d: self.fail("must not send"))
        self.assertEqual(ctx.exception.reason, "not_approved")
        # A sending row with a malformed fee stays an ambiguity hold.
        hold = self.approved(box, request_id="req_hold", agency="ca-hold", fee=10)
        box.db.execute("UPDATE outbox SET state='sending', sending_at=?, fee_cap_cents=0 WHERE idempotency_key=?", (self.clock.stamp, hold))
        with self.assertRaises(ob.Blocked) as ctx:
            box.send(hold, lambda d: self.fail("must not send"))
        self.assertEqual(ctx.exception.reason, "ambiguous_send_unresolved")
        # An already-sent row with malformed data stays a no-transport no-op, unrewritten.
        done = self.approved(box, request_id="req_done", agency="ca-done", fee=20)
        box.send(done, lambda d: ob.ProviderReceipt("email", "id-done"))
        box.db.execute("UPDATE outbox SET fee_cap_cents=-5 WHERE idempotency_key=?", (done,))
        before = snapshot(box)
        self.assertEqual(box.send(done, lambda d: self.fail("sent twice"))["state"], "sent")
        self.assertEqual(snapshot(box), before)
        # A refusal consumes no daily slot: a valid different request to the same agency still sends.
        bad = self.approved(box, request_id="req_bad", agency="ca-slot", fee=5)
        box.db.execute("UPDATE outbox SET fee_cap_cents=6 WHERE idempotency_key=?", (bad,))
        with self.assertRaises(ob.Blocked) as ctx:
            box.send(bad, lambda d: self.fail("must not send"))
        self.assertEqual(ctx.exception.reason, "invalid_fee_cap")
        good = self.approved(box, request_id="req_good", agency="ca-slot", fee=5)
        self.assertEqual(box.send(good, lambda d: ob.ProviderReceipt("email", "id-good"))["state"], "sent")
        # Daily cap, follow-up and over-cap rows keep their ordinary reasons.
        again = self.approved(box, request_id="req_again", agency="ca-slot")
        with self.assertRaises(ob.Blocked) as ctx:
            box.send(again, lambda d: self.fail("must not send"))
        self.assertEqual(ctx.exception.reason, "daily_agency_cap")
        capped = self.new_box(5000)
        follow = self.approved(capped, request_id="req_follow", agency="ca-follow", kind="send_followup", fee=9000)
        with self.assertRaises(ob.Blocked) as ctx:
            capped.send(follow, lambda d: self.fail("must not send"))
        self.assertEqual((ctx.exception.reason, ctx.exception.detail), ("fee_cap_exceeded", "9000 > campaign cap 5000"))
        ok = self.approved(capped, request_id="req_follow_ok", agency="ca-follow-ok", kind="send_followup", fee=5000)
        self.assertEqual(capped.send(ok, lambda d: ob.ProviderReceipt("email", "id-follow"))["state"], "sent")

    def test_retry_round_is_refused_unchanged_when_fee_column_disagrees(self):
        box = self.new_box(5000)
        key = self.approved(box, request_id="req_retry", agency="ca-retry", fee=100)
        with self.assertRaises(ob.OutboxError):
            box.send(key, lambda d: (_ for _ in ()).throw(ob.OutboxError("definite refusal")))
        self.clock.stamp = "2026-09-30T10:01:00Z"
        APPROVERS.approve_retry(box, key, "new@example.invalid", self.clock.stamp)
        box.db.execute("UPDATE outbox SET fee_cap_cents=0 WHERE idempotency_key=?", (key,))
        before = snapshot(box)
        self.assertEqual(len(before[1]), 1)
        with self.assertRaises(ob.Blocked) as ctx:
            box.send(key, lambda d: self.fail("must not send"))
        self.assertEqual(ctx.exception.reason, "invalid_fee_cap")
        self.assertEqual(snapshot(box), before)

    # -- CLI -------------------------------------------------------------------------------------
    def write_draft(self, **changes):
        fields = json.loads(draft().to_json())
        fields.update(subject="SENTINEL-PRIVATE-SUBJECT", body="SENTINEL-PRIVATE-BODY", **changes)
        path = Path(self.tmp.name) / f"draft{self.n}.json"
        self.n += 1
        path.write_text(json.dumps(fields), encoding="utf-8")
        return path

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = ob.main(list(argv))
        except SystemExit as exc:
            code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_cli_fee_proposal_refusal_is_structured_and_private(self):
        journal = str(Path(self.tmp.name) / "cli" / "outbox.sqlite")
        expected = {"state": "refused", "reason": FEE_MESSAGE}
        for bad in (-1, 0.9, 1.0, "7", True, None, [1], {"a": 1}, 2 ** 63):
            path = self.write_draft(fee_cap_cents=bad)
            with self.subTest(fee=repr(bad)):
                code, out, err = self.run_main("--journal", journal, "propose", str(path))
                self.assertEqual((code, json.loads(out), err), (2, expected, ""))
                self.assertNotIn("SENTINEL", out + err)
                proc = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.outbox", "--journal", journal, "propose", str(path)],
                                      cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
                self.assertEqual((proc.returncode, json.loads(proc.stdout), proc.stderr), (2, expected, ""))
        box = ob.Outbox(journal)
        self.addCleanup(box.close)
        self.assertEqual(box.rows(), [])
        # An omitted fee defaults to 0 and a valid one is stored as given.
        fields = json.loads(self.write_draft().read_text(encoding="utf-8"))
        del fields["fee_cap_cents"]
        path = Path(self.tmp.name) / "omitted.json"
        path.write_text(json.dumps(fields), encoding="utf-8")
        code, out, err = self.run_main("--journal", journal, "propose", str(path))
        self.assertEqual((code, json.loads(out)["state"], err), (0, "proposed", ""))
        self.assertEqual(box.rows()[0]["fee_cap_cents"], 0)
        proc = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.outbox", "--journal", journal, "propose",
                               str(self.write_draft(request_id="req_cli_fee", fee_cap_cents=250))],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(sorted(r["fee_cap_cents"] for r in box.rows()), [0, 250])

    def test_cli_campaign_fee_parser_refuses_before_journal(self):
        path = self.write_draft()
        for token in ("-1", "1.5", "+5", "nan", "inf", "-0", "zz12abc34", "", " 5", "5 ", "1e3", "\u0663", "\u00b2",
                      "9223372036854775808", "9" * 5000):
            journal = Path(self.tmp.name) / f"refused{self.n}" / "outbox.sqlite"
            self.n += 1
            with self.subTest(token=token[:12]):
                code, out, err = self.run_main("--journal", str(journal), "--fee-cap-cents", token, "propose", str(path))
                self.assertEqual((code, out), (2, ""))
                self.assertIn("campaign fee cap must be an integer between 0 and 9223372036854775807", err)
                self.assertNotIn("zz12abc34", err)
                self.assertNotIn("Traceback", err)
                self.assertFalse(journal.parent.exists())
        seen = []
        real = ob.Outbox

        def spy(*args, **kwargs):
            seen.append(kwargs["campaign_fee_cap_cents"])
            return real(*args, **kwargs)

        for token, value in (("0", 0), ("007", 7), ("9223372036854775807", MAX_FEE), ("000000000000000000000000005", 5),
                             ("0" * 5000, 0), ("0" * 5000 + "7", 7)):
            with unittest.mock.patch.object(ob, "Outbox", spy):
                code, out, err = self.run_main("--journal", str(Path(self.tmp.name) / f"ok{self.n}" / "outbox.sqlite"),
                                               "--fee-cap-cents", token, "list")
            self.n += 1
            self.assertEqual((code, err), (0, ""))
        self.assertEqual(seen, [0, 7, MAX_FEE, 5, 0, 7])
        code, out, err = self.run_main("--journal", str(Path(self.tmp.name) / "default" / "outbox.sqlite"), "list")
        self.assertEqual(code, 0)

    def test_cli_send_reports_invalid_fee_cap(self):
        box = self.new_box(0, name="clisend")
        key = self.approved(box, fee=5000)
        box.db.execute("UPDATE outbox SET fee_cap_cents=0 WHERE idempotency_key=?", (key,))
        before = snapshot(box)
        with unittest.mock.patch.object(ob, "send_email", side_effect=AssertionError("transport")) as email, \
                unittest.mock.patch.object(ob, "file_muckrock", side_effect=AssertionError("transport")) as muckrock:
            code, out, err = self.run_main("--journal", str(box.path), "--fee-cap-cents", "0", "send", key,
                                           "--smtp-host", "smtp.example.invalid")
        self.assertEqual((code, json.loads(out), err), (2, {"state": "blocked", "reason": "invalid_fee_cap"}, ""))
        email.assert_not_called()
        muckrock.assert_not_called()
        self.assertEqual(snapshot(box), before)
        self.assertFalse(box.db.in_transaction)
