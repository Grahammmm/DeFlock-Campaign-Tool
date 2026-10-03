"""Signed gateway acceptance, using synthetic drafts and injected transports only."""
import copy
import datetime as dt
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from campaign_tool.outbox import AmbiguousFailure, Outbox, OutboxError, ProviderReceipt, idempotency_key
from runner.outbox_gateway import ApprovedOutboxGateway, MAX_FRAME_BYTES, make_handler, signature


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "private" / "outbox.sqlite"
        self.secret = "synthetic-signing-key-for-tests-only-0000"
        self.calls = []
        self.transport = lambda draft: self.deliver(draft)
        self.gateway = ApprovedOutboxGateway("synthetic", self.secret, self.path,
            "sender@example.invalid", {"email": self.transport},
            clock=lambda: dt.datetime(2026, 10, 3, 10, tzinfo=dt.timezone.utc))
        self.frame = {"v": 1, "campaign_id": "synthetic", "action_id": "action-1",
            "approved_by": "organizer@example.invalid", "approved_at": "2026-10-03T09:59:00Z",
            "issued_at": "2026-10-03T10:00:00Z",
            "canonical_key": idempotency_key("send_request", "request-1", 1),
            "draft": {"request_id": "request-1", "agency_id": "agency-synthetic", "scope_version": 1,
                "channel": "email", "subject": "Synthetic request", "body": "Synthetic body.\nNo real records.",
                "to": "records@example.invalid", "fee_cap_cents": 0, "kind": "send_request"}}

    def tearDown(self):
        self.tmp.cleanup()

    def deliver(self, draft):
        self.calls.append(draft)
        return ProviderReceipt("email", "synthetic-message-1")

    def dispatch(self, frame=None, raw=None):
        raw = raw if raw is not None else json.dumps(frame or self.frame).encode()
        return self.gateway.dispatch(raw, signature(self.secret, raw))

    def test_unsigned_oversized_and_mutated_frames_do_not_open_journal(self):
        raw = json.dumps(self.frame).encode()
        self.assertEqual(self.gateway.dispatch(raw, "0" * 64)[0], 401)
        self.assertEqual(self.gateway.dispatch(raw + b" ", signature(self.secret, raw))[0], 401)
        self.assertEqual(self.gateway.dispatch(b"x" * (MAX_FRAME_BYTES + 1), "0" * 64)[0], 413)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.calls, [])

    def test_strict_signed_schema_and_freshness_before_journal(self):
        mutations = [("campaign_id", "other"), ("v", True), ("canonical_key", "wrong"),
            ("issued_at", "2026-10-03T09:54:59Z"), ("approved_at", "2026-10-03T10:01:00Z"),
            ("approved_by", "bad\nheader"), ("action_id", "")]
        for field, value in mutations:
            with self.subTest(field=field):
                frame = copy.deepcopy(self.frame); frame[field] = value
                self.assertEqual(self.dispatch(frame)[0], 400)
        for field, value in [("scope_version", True), ("fee_cap_cents", -1), ("in_reply_to", None),
                ("subject", "bad\rheader"), ("to", "bad\nheader"), ("channel", "portal_manual")]:
            with self.subTest(field=field):
                frame = copy.deepcopy(self.frame); frame["draft"][field] = value
                self.assertEqual(self.dispatch(frame)[0], 400)
        raw = json.dumps(self.frame).replace('"v": 1', '"v": 1, "v": 1').encode()
        self.assertEqual(self.dispatch(raw=raw)[0], 400)
        self.assertFalse(self.path.exists())

    def test_missing_provider_refuses_before_journal(self):
        self.gateway.transports = {}
        self.assertEqual(self.dispatch(), (503, {"code": "transport_not_configured"}))
        self.assertFalse(self.path.exists())

    def test_send_and_replay_bind_receipt_to_action_and_canonical_key(self):
        status, receipt = self.dispatch()
        self.assertEqual(status, 200)
        self.assertEqual(receipt["action_id"], self.frame["action_id"])
        self.assertEqual(receipt["canonical_key"], self.frame["canonical_key"])
        self.assertEqual(receipt["provider_message_id"], "synthetic-message-1")
        self.assertEqual(self.dispatch(), (status, receipt))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0].from_addr, "sender@example.invalid")
        changed = copy.deepcopy(self.frame); changed["draft"]["body"] = "Changed draft"
        self.assertEqual(self.dispatch(changed), (409, {"code": "draft_conflict"}))
        self.assertEqual(len(self.calls), 1)

    def test_fee_and_daily_caps_are_enforced_by_existing_outbox(self):
        frame = copy.deepcopy(self.frame); frame["draft"]["fee_cap_cents"] = 1
        self.assertEqual(self.dispatch(frame)[1]["reason"], "fee_cap_exceeded")
        self.assertEqual(self.calls, [])
        # A different canonical request avoids the original fee-held row, not its guard.
        frame = copy.deepcopy(self.frame); frame["draft"]["request_id"] = "request-2"
        frame["canonical_key"] = idempotency_key("send_request", "request-2", 1)
        self.assertEqual(self.dispatch(frame)[0], 200)
        frame["draft"]["request_id"] = "request-3"
        frame["canonical_key"] = idempotency_key("send_request", "request-3", 1)
        self.assertEqual(self.dispatch(frame)[1]["reason"], "daily_agency_cap")
        self.assertEqual(len(self.calls), 1)

    def test_ambiguous_send_holds_retries_and_reconciliation_replays_receipt(self):
        def uncertain(draft):
            self.calls.append(draft)
            raise AmbiguousFailure("private provider error must not escape")
        self.gateway.transports["email"] = uncertain
        self.assertEqual(self.dispatch(), (503, {"code": "outbox_ambiguous"}))
        self.assertEqual(self.dispatch()[1]["code"], "outbox_ambiguous")
        self.assertEqual(len(self.calls), 1)
        box = Outbox(self.path)
        try:
            self.assertEqual(box.row(self.frame["canonical_key"])["state"], "sending")
            box.reconcile(self.frame["canonical_key"], "delivered", "owner@example.invalid",
                ProviderReceipt("email", "confirmed-synthetic-message"))
        finally:
            box.close()
        self.assertEqual(self.dispatch()[1]["provider_message_id"], "confirmed-synthetic-message")
        self.assertEqual(len(self.calls), 1)

    def test_definite_failure_requires_fresh_outbox_approval_no_auto_reset(self):
        def refused(draft):
            self.calls.append(draft); raise OutboxError("private provider detail")
        self.gateway.transports["email"] = refused
        self.assertEqual(self.dispatch(), (409, {"code": "outbox_refused"}))
        self.assertEqual(self.dispatch(), (409, {"code": "fresh_outbox_approval_required"}))
        self.assertEqual(len(self.calls), 1)

    def test_fresh_approval_after_conclusive_failure_can_retry_same_key(self):
        def refused(draft):
            self.calls.append(draft); raise OutboxError("definitive synthetic refusal")
        self.gateway.transports["email"] = refused
        self.assertEqual(self.dispatch()[0], 409)
        self.gateway.transports["email"] = self.transport
        # Merely refreshing issuance does not create a fresh approval.
        self.gateway.clock = lambda: dt.datetime(2026, 10, 3, 10, 2, tzinfo=dt.timezone.utc)
        fresh = copy.deepcopy(self.frame); fresh["issued_at"] = "2026-10-03T10:02:00Z"
        self.assertEqual(self.dispatch(fresh)[1]["code"], "fresh_outbox_approval_required")
        fresh["approved_at"] = "2026-10-03T10:01:00Z"
        self.assertEqual(self.dispatch(fresh)[0], 200)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.dispatch(fresh)[0], 200)
        self.assertEqual(len(self.calls), 2)

    def test_new_approval_cannot_clear_uncertain_delivery(self):
        def uncertain(draft):
            self.calls.append(draft); raise AmbiguousFailure("synthetic uncertain outcome")
        self.gateway.transports["email"] = uncertain
        self.assertEqual(self.dispatch()[0], 503)
        self.gateway.clock = lambda: dt.datetime(2026, 10, 3, 10, 2, tzinfo=dt.timezone.utc)
        fresh = copy.deepcopy(self.frame); fresh["issued_at"] = "2026-10-03T10:02:00Z"; fresh["approved_at"] = "2026-10-03T10:01:00Z"
        self.assertEqual(self.dispatch(fresh)[1]["code"], "outbox_ambiguous")
        self.assertEqual(len(self.calls), 1)

    def test_invalid_receipt_is_not_reported_as_success(self):
        self.gateway.transports["email"] = lambda d: ProviderReceipt("muckrock", "wrong-channel")
        self.assertEqual(self.dispatch(), (503, {"code": "outbox_ambiguous"}))
        box = Outbox(self.path)
        try:
            self.assertEqual(box.row(self.frame["canonical_key"])["state"], "sending")
        finally:
            box.close()
        self.assertEqual(self.dispatch()[1]["code"], "outbox_ambiguous")

    def test_deeply_nested_signed_json_is_refused_before_journal(self):
        self.assertEqual(self.dispatch(raw=b"[" * 2000 + b"0" + b"]" * 2000)[0], 400)
        self.assertFalse(self.path.exists())

    def test_two_concurrent_dispatch_retries_send_once(self):
        raw = json.dumps(self.frame).encode()
        barrier = threading.Barrier(2)
        results = []
        def retry():
            barrier.wait(timeout=3)
            results.append(self.gateway.dispatch(raw, signature(self.secret, raw)))
        threads = [threading.Thread(target=retry) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(len(self.calls), 1)

    def test_journal_open_failure_is_bounded_and_does_not_send(self):
        self.path.parent.mkdir(); self.path.mkdir()
        self.assertEqual(self.dispatch(), (503, {"code": "outbox_ambiguous"}))
        self.assertEqual(self.calls, [])

    def test_actual_http_signature_replay_and_bounds(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.gateway))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            raw = json.dumps(self.frame).encode()
            def post(sig, payload=raw):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    connection.request("POST", "/send", body=payload, headers={"X-Outbox-Signature": sig})
                    response = connection.getresponse()
                    return response.status, response.read(), response.getheader("Cache-Control")
                finally:
                    connection.close()
            self.assertEqual(post("0" * 64)[0], 401)
            self.assertFalse(self.path.exists())
            first = post(signature(self.secret, raw)); second = post(signature(self.secret, raw))
            self.assertEqual(first, second); self.assertEqual(first[0], 200)
            self.assertEqual(first[2], "no-store"); self.assertEqual(len(self.calls), 1)
            self.assertEqual(post("0" * 64, b"x" * (MAX_FRAME_BYTES + 1))[0], 413)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
