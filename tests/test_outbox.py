"""Outbox journal: one send per key, caps, 402 blocked, ambiguous failures block resends."""
import io
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path

from campaign_tool import outbox as ob


class StubSmtpServer:
    """Minimal threaded SMTP stub: records DATA payloads; can drop the connection mid-DATA."""

    def __init__(self, drop_after_data=False):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.messages = []
        self.drop_after_data = drop_after_data
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
            elif upper.startswith("MAIL FROM") or upper.startswith("RCPT TO"):
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
            self.box.approve(row["idempotency_key"], "organizer@example.invalid")
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

    def test_daily_agency_cap(self):
        fake = lambda d: ob.ProviderReceipt("email", "id-" + d.request_id)
        a = self.box.propose(draft(request_id="req_a"))
        b = self.box.propose(draft(request_id="req_b"))
        self.box.approve(a["idempotency_key"], "o@example.invalid")
        self.box.approve(b["idempotency_key"], "o@example.invalid")
        self.assertEqual(self.box.send(a["idempotency_key"], fake)["state"], "sent")
        with self.assertRaises(ob.Blocked) as ctx:
            self.box.send(b["idempotency_key"], fake)
        self.assertEqual(ctx.exception.reason, "daily_agency_cap")
        # the cap is transient: the row stays approved for tomorrow
        self.assertEqual(self.box.row(b["idempotency_key"])["state"], "approved")
        # a different agency is unaffected
        c = self.box.propose(draft(request_id="req_c", agency="ca-other-sheriff"))
        self.box.approve(c["idempotency_key"], "o@example.invalid")
        self.assertEqual(self.box.send(c["idempotency_key"], fake)["state"], "sent")

    def test_fee_cap(self):
        row = self.box.propose(draft(fee=9000))
        self.box.approve(row["idempotency_key"], "o@example.invalid")
        with self.assertRaises(ob.Blocked) as ctx:
            self.box.send(row["idempotency_key"], lambda d: self.fail("must not send"))
        self.assertEqual(ctx.exception.reason, "fee_cap_exceeded")

    def test_muckrock_402_blocked_and_success(self):
        def opener_402(request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, 402, "Payment Required", {}, io.BytesIO(b"{}"))
        row = self.box.propose(draft(channel="muckrock", to="123"))
        self.box.approve(row["idempotency_key"], "o@example.invalid")
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
        self.box.approve(row2["idempotency_key"], "o@example.invalid")
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
            self.box.approve(row["idempotency_key"], "o@example.invalid")
            with self.assertRaises(ob.AmbiguousFailure):
                self.box.send(row["idempotency_key"], lambda d: ob.send_email(d, settings))
            self.assertEqual(self.box.row(row["idempotency_key"])["state"], "sending")
            self.assertEqual(len(self.box.unresolved()), 1)
            with self.assertRaises(ob.Blocked) as ctx:
                self.box.send(row["idempotency_key"], lambda d: self.fail("must not resend"))
            self.assertEqual(ctx.exception.reason, "ambiguous_send_unresolved")
            # a follow-up on the same request is blocked too
            fu = self.box.propose(draft(kind="send_followup"))
            self.box.approve(fu["idempotency_key"], "o@example.invalid")
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
        self.box.approve(row["idempotency_key"], "o@example.invalid")
        with self.assertRaises(ob.AmbiguousFailure):
            self.box.send(row["idempotency_key"], lambda d: ob.file_muckrock(d, "tok", opener=opener_503))
        self.assertEqual(self.box.row(row["idempotency_key"])["state"], "sending")
        self.assertEqual(len(self.box.unresolved()), 1)
        self.assertEqual(self.box.reconcile(row["idempotency_key"], "not_delivered", "o@example.invalid")["state"], "failed")
        def opener_401(request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
        row2 = self.box.propose(draft(request_id="req_mr6", agency="ca-mr6", channel="muckrock", to="6"))
        self.box.approve(row2["idempotency_key"], "o@example.invalid")
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
            self.box.approve(first["idempotency_key"], "o@example.invalid")
            self.box.send(first["idempotency_key"], lambda d: ob.send_email(d, settings))
            follow = draft(kind="send_followup")
            follow.in_reply_to = "<ack-0042@example.invalid>"
            row = self.box.propose(follow)
            self.assertNotEqual(row["idempotency_key"], first["idempotency_key"])
            self.box.approve(row["idempotency_key"], "o@example.invalid")
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
            self.box.approve(row["idempotency_key"], "")
        self.box.approve(row["idempotency_key"], "o@example.invalid")
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
            self.box.approve(row["idempotency_key"], "o@example.invalid")

    def test_cli_round_trip(self):
        draft_path = Path(self.tmp.name) / "draft.json"
        draft_path.write_text(draft().to_json())
        journal = str(Path(self.tmp.name) / "private" / "cli.sqlite")
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ob.main(["--journal", journal, "propose", str(draft_path)]), 0)
            key = json.loads(out.getvalue().splitlines()[-1])["idempotency_key"]
            self.assertEqual(ob.main(["--journal", journal, "approve", key, "--by", "o@example.invalid"]), 0)
            self.assertEqual(ob.main(["--journal", journal, "list"]), 0)
        self.assertIn('"state": "approved"', out.getvalue())


if __name__ == "__main__":
    unittest.main()
