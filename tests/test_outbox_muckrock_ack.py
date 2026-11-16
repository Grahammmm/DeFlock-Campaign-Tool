"""MuckRock acknowledgement validation: malformed replies stay ambiguous, never 'sent'."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from campaign_tool import outbox as ob
from tests.approval_signing import Approvers
from tests.test_outbox import Clock, draft

APPROVERS = Approvers("original@example.invalid", "new@example.invalid", "o@example.invalid", "organizer@example.invalid")
FIXED = "muckrock reply had no valid request id; filing state unknown"
CANARY = b"PRIVATE_REPLY_CANARY"


def setUpModule():
    APPROVERS.start()


def tearDownModule():
    APPROVERS.stop()


class Resp(io.BytesIO):
    closed_by_exit = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed_by_exit = True
        return False


class Opener:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.calls = 0
        self.responses = []

    def __call__(self, request, timeout=0):
        self.calls += 1
        resp = Resp(self.payload)
        self.responses.append(resp)
        return resp


def ack(rid, form="envelope", **extra):
    item = {"id": rid, **extra}
    return {"requests": [item]} if form == "envelope" else item


def file_with(payload):
    opener = Opener(payload)
    d = draft(channel="muckrock", to="77")
    return opener, lambda: ob.file_muckrock(d, "tok", opener=opener)


class DirectTests(unittest.TestCase):
    def assertFixed(self, payload):
        opener, call = file_with(payload)
        with self.assertRaises(ob.AmbiguousFailure) as ctx:
            call()
        self.assertEqual(str(ctx.exception), FIXED)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertEqual(opener.calls, 1)
        self.assertTrue(opener.responses[0].closed_by_exit)

    def test_malformed_request_ids_never_make_receipts(self):
        bad = [None, True, False, 0, -1, 1.0, 4242.5, float("nan"), float("inf"), "", "abc", "-5", "+5", " 5", "5 ",
               "1.5", "1e3", "000", "\u0664\u0662", [], [1], {}, {"a": 1}]
        for form in ("envelope", "flat"):
            for rid in bad:
                with self.subTest(form=form, rid=rid):
                    self.assertFixed(json.dumps(ack(rid, form)).encode())

    def test_response_shape_admission_has_no_fallback_bypass(self):
        cases = [b"", b"{}", b"null", b"true", b"7", b'"4242"', b"[]", b'[{"id": 5}]',
                 b'{"requests": null, "id": 5}', b'{"requests": [], "id": 5}', b'{"requests": 5, "id": 5}',
                 b'{"requests": {"id": 5}, "id": 5}', b'{"requests": "x", "id": 5}',
                 b'{"requests": [5], "id": 5}', b'{"requests": [{}], "id": 5}', b'{"requests": [{"id": null}], "id": 5}',
                 b'{"requests": [{"status": "x"}]}']
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertFixed(payload)

    def test_decode_and_parse_refusals_are_private(self):
        cases = [b"\xff" + CANARY, CANARY + b"\xff", b'{"requests": [{"id": 5, "x": "' + CANARY + b'\xff"}]}',
                 b'{"requests": [{"id": ' + CANARY, b"{" + CANARY + b"}", b"[" * 100000 + CANARY,
                 b'{"requests": [{"id": ' + b"9" * 6000 + b"}]}"]
        for payload in cases:
            with self.subTest(payload=payload[:30]):
                opener, call = file_with(payload)
                with self.assertRaises(ob.AmbiguousFailure) as ctx:
                    call()
                self.assertEqual(str(ctx.exception), FIXED)
                self.assertIsNone(ctx.exception.__cause__)
                self.assertNotIn("PRIVATE_REPLY_CANARY", repr(ctx.exception))

    def test_accepted_acknowledgements_preserve_receipt_bytes(self):
        long_digits = "7" * 6000
        for form in ("envelope", "flat"):
            for rid, expect in [(4242, "4242"), ("4242", "4242"), ("00042", "00042"), (long_digits, long_digits)]:
                with self.subTest(form=form, rid=rid[:10] if isinstance(rid, str) else rid):
                    payload = ack(rid, form, absolute_url="https://www.muckrock.com/foi/-/4242/", status="submitted", extra="ignored")
                    opener, call = file_with(json.dumps(payload).encode())
                    receipt = call()
                    want = ob.ProviderReceipt("muckrock", expect, url="https://www.muckrock.com/foi/-/4242/", raw={"status": "submitted"})
                    self.assertEqual(receipt.to_json(), want.to_json())
                    self.assertTrue(opener.responses[0].closed_by_exit)
        _, call = file_with(ack(9, url="https://example.invalid/u"))
        want = ob.ProviderReceipt("muckrock", "9", url="https://example.invalid/u", raw={"status": None})
        self.assertEqual(call().to_json(), want.to_json())
        _, call = file_with({"requests": [{"id": 11, "status": "a"}, {"id": 12, "status": "b"}]})
        self.assertEqual(json.loads(call().to_json())["provider_message_id"], "11")


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "private" / "outbox.sqlite"
        self.clock = Clock()
        self.box = ob.Outbox(self.path, campaign_fee_cap_cents=5000, clock=self.clock)

    def tearDown(self):
        self.box.close()
        self.tmp.cleanup()

    def approved(self, **kw):
        row = self.box.propose(draft(channel="muckrock", to="77", **kw))
        APPROVERS.approve(self.box, row["idempotency_key"], "o@example.invalid")
        return row["idempotency_key"]

    def send(self, key, payload):
        opener = Opener(payload)
        return opener, lambda: self.box.send(key, lambda d: ob.file_muckrock(d, "tok", opener=opener))

    def test_real_signed_send_holds_malformed_reply_after_reopen(self):
        for n, payload in enumerate([ack(True), b"\xff" + CANARY]):
            key = self.approved(request_id=f"req_hold{n}", agency=f"ca-hold{n}")
            opener, call = self.send(key, payload)
            with self.assertRaises(ob.AmbiguousFailure):
                call()
            row = self.box.row(key)
            self.assertEqual(row["state"], "sending")
            self.assertIsNone(row["sent_at"])
            self.assertIsNone(row["provider_receipt"])
            self.assertEqual(row["error"], "ambiguous: " + FIXED)
            self.assertNotIn("CANARY", row["error"])
            self.assertFalse(self.box.db.in_transaction)
            sending_at = row["sending_at"]
            self.box.close()
            self.box = ob.Outbox(self.path, campaign_fee_cap_cents=5000, clock=self.clock)
            again, call2 = self.send(key, ack(4242))
            with self.assertRaises(ob.Blocked) as ctx:
                call2()
            self.assertEqual(ctx.exception.reason, "ambiguous_send_unresolved")
            self.assertEqual(again.calls, 0)
            row = self.box.row(key)
            self.assertEqual((row["state"], row["sending_at"], row["error"]), ("sending", sending_at, "ambiguous: " + FIXED))

    def test_reconcile_remains_explicit_and_http_classes_unchanged(self):
        key = self.approved(request_id="req_rec", agency="ca-rec")
        _, call = self.send(key, ack(None))
        with self.assertRaises(ob.AmbiguousFailure):
            call()
        with self.assertRaises(ob.OutboxError):
            APPROVERS.approve_retry(self.box, key, "new@example.invalid", self.clock.stamp)
        self.assertEqual(self.box.reconcile(key, "delivered", "organizer@example.invalid")["state"], "sent")
        again, call2 = self.send(key, ack(4242))
        self.assertEqual(call2()["state"], "sent")
        self.assertEqual(again.calls, 0)

        key2 = self.approved(request_id="req_rec2", agency="ca-rec2")
        _, call = self.send(key2, ack("x"))
        with self.assertRaises(ob.AmbiguousFailure):
            call()
        self.assertEqual(self.box.reconcile(key2, "not_delivered", "organizer@example.invalid")["state"], "failed")
        self.clock.stamp = "2026-09-30T10:05:00Z"
        self.assertEqual(APPROVERS.approve_retry(self.box, key2, "new@example.invalid", self.clock.stamp)["state"], "approved")

        import urllib.error
        for code, exc_type in [(402, ob.Blocked), (401, ob.OutboxError), (403, ob.OutboxError), (503, ob.AmbiguousFailure)]:
            def opener(request, timeout=0, code=code):
                raise urllib.error.HTTPError(request.full_url, code, "x", {}, io.BytesIO(b"{}"))
            with self.subTest(code=code), self.assertRaises(exc_type):
                ob.file_muckrock(draft(channel="muckrock", to="77"), "tok", opener=opener)


class CliTests(unittest.TestCase):
    def run_cli(self, tmp, payload, key=None):
        opener = Opener(payload)
        out, err = io.StringIO(), io.StringIO()
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"MUCKROCK_TOKEN": "synthetic-token"}), \
                mock.patch("urllib.request.urlopen", opener), redirect_stdout(out), redirect_stderr(err):
            rc = ob.main(["--journal", str(Path(tmp) / "o.sqlite"), "send", key])
        return rc, out.getvalue(), err.getvalue(), opener

    def test_cli_uses_existing_ambiguous_contract(self):
        for n, (payload, healthy) in enumerate([(ack(None), False), (b"\xff" + CANARY, False), (ack(4242), True)]):
            with self.subTest(n=n), tempfile.TemporaryDirectory() as tmp:
                box = ob.Outbox(Path(tmp) / "o.sqlite", campaign_fee_cap_cents=5000)
                row = box.propose(draft(channel="muckrock", to="77"))
                APPROVERS.approve(box, row["idempotency_key"], "o@example.invalid")
                box.close()
                rc, out, err, opener = self.run_cli(tmp, payload, row["idempotency_key"])
                self.assertEqual(opener.calls, 1)
                self.assertEqual(err, "")
                self.assertNotIn("CANARY", out)
                if healthy:
                    self.assertEqual(rc, 0)
                    self.assertEqual(json.loads(out)["state"], "sent")
                else:
                    self.assertEqual(rc, 3)
                    self.assertEqual(json.loads(out), {"state": "sending", "error": FIXED, "next": "reconcile before any resend"})


if __name__ == "__main__":
    unittest.main()
