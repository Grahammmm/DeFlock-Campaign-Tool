"""DF-2 gate: only a verified human signature approves an outbox row; nothing unapproved reaches a send."""
import contextlib
import io
import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from campaign_tool import outbox as ob
from tests.approval_signing import Signer, approval

DRAFT = dict(request_id="R-1", agency_id="sample-city", scope_version=1, channel="email",
             subject="Public records request", body="Please provide the records.\n", to="records@city.example",
             from_addr="organizer@example.invalid")


def receipt(_draft):
    return ob.ProviderReceipt("email", "<msg-1@example.invalid>")


class ApprovalGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner, cls.agent = Signer("owner@example.invalid"), Signer("opus-coder", trusted=False)

    @classmethod
    def tearDownClass(cls):
        cls.owner.close(); cls.agent.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Path(self.tmp.name) / "private" / "outbox.sqlite"
        self.box = ob.Outbox(self.journal)
        self.addCleanup(self.box.close)
        self.key = self.box.propose(ob.RequestDraft(**DRAFT))["idempotency_key"]
        self.sent = []

    def transport(self, draft):
        self.sent.append(draft)
        return receipt(draft)

    def evidence(self, signer, key=None):
        return {"kind": "ssh", "signature": signer.sign(self.box.approval_payload(key or self.key))}

    def test_agent_typed_name_cannot_approve(self):
        """Before DF-2 any non-empty string approved the row; an agent could approve itself."""
        for who in ("opus-coder", "owner@example.invalid"):
            with self.subTest(who=who), self.owner.trust():
                with self.assertRaises(ob.OutboxError):
                    self.box.approve(self.key, who)
        self.assertEqual(self.box.row(self.key)["state"], "proposed")

    def test_cli_approve_by_name_is_refused(self):
        """`approve KEY --by NAME` used to approve; the CLI now has no way to approve without a signature."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                code = ob.main(["--journal", str(self.journal), "approve", self.key, "--by", "opus-coder"])
            except SystemExit as exc:
                code = exc.code
        self.assertNotEqual(code, 0)
        self.assertEqual(self.box.row(self.key)["state"], "proposed")

    def test_unapproved_row_forged_in_sql_never_reaches_a_send(self):
        """Writing state='approved' straight into the journal must not make the row sendable."""
        self.box.db.execute("UPDATE outbox SET state='approved', approved_by='owner@example.invalid', "
                            "approved_at='2026-11-01T00:00:00Z' WHERE idempotency_key=?", (self.key,))
        with self.owner.trust():
            with self.assertRaises(ob.Blocked) as ctx:
                self.box.send(self.key, self.transport)
        self.assertEqual(ctx.exception.reason, "not_approved")
        self.assertEqual(self.sent, [])

    def test_agent_key_signature_is_refused(self):
        with self.owner.trust():
            with self.assertRaises(ob.OutboxError):
                self.box.approve(self.key, self.evidence(self.agent))
        self.assertEqual(self.box.row(self.key)["state"], "proposed")

    def test_owner_signature_approves_and_send_reverifies(self):
        with self.owner.trust():
            row = self.box.approve(self.key, self.evidence(self.owner))
            self.assertEqual((row["state"], row["approved_by"]), ("approved", "owner@example.invalid"))
            self.assertEqual(self.box.send(self.key, self.transport)["state"], "sent")
        self.assertEqual(len(self.sent), 1)

    def test_approval_is_bound_to_the_exact_text(self):
        other = self.box.propose(ob.RequestDraft(**{**DRAFT, "request_id": "R-2"}))["idempotency_key"]
        with self.owner.trust():
            with self.assertRaises(ob.OutboxError):  # a signature for another row's text
                self.box.approve(self.key, self.evidence(self.owner, other))
            self.box.approve(self.key, self.evidence(self.owner))
            changed = self.box.row(self.key)["draft_json"].replace("Please provide", "Please also provide")
            self.box.db.execute("UPDATE outbox SET draft_json=? WHERE idempotency_key=?", (changed, self.key))
            with self.assertRaises(ob.Blocked) as ctx:
                self.box.send(self.key, self.transport)
        self.assertEqual((ctx.exception.reason, self.sent), ("not_approved", []))

    def test_payload_shows_the_full_text_being_approved(self):
        text = self.box.approval_payload(self.key).decode("utf-8")
        for want in ("To: records@city.example", "Subject: Public records request", "Please provide the records.",
                     "scope: outbox", f"subject: {self.key}", "round: 0"):
            self.assertIn(want, text)

    def test_trust_file_owned_by_the_campaign_user_is_refused(self):
        """The real trust check: a signers file the agent's own user can write never counts."""
        if os.geteuid() == 0:
            self.skipTest("root owns every file it writes")
        with self.assertRaises(approval.ApprovalError):
            approval._require_root(self.owner.signers)
        evidence = self.evidence(self.owner)
        with unittest.mock.patch.object(approval, "TRUSTED_SIGNERS", self.owner.signers):
            with self.assertRaises(ob.OutboxError):
                self.box.approve(self.key, evidence)
        self.assertEqual(self.box.row(self.key)["state"], "proposed")

    def test_gateway_attested_row_is_reverified_and_only_inside_the_gateway(self):
        import json as _json
        from runner.outbox_gateway import ApprovedOutboxGateway, signature
        secret = "s" * 40
        gw = ApprovedOutboxGateway("campaign", secret, self.journal, DRAFT["from_addr"], {})
        fields = {k: v for k, v in DRAFT.items() if k != "from_addr"} | {"fee_cap_cents": 0, "kind": "send_request"}
        raw = _json.dumps({"v": 1, "campaign_id": "campaign", "action_id": "a1", "approved_by": "owner@example.invalid",
                           "approved_at": "2026-11-01T00:00:00Z", "issued_at": "2026-11-01T00:00:01Z",
                           "canonical_key": self.key, "draft": fields}).encode()
        evidence = {"kind": "gateway", "frame": raw.decode(), "mac": signature(secret, raw)}
        hosted = ob.Outbox(self.journal, attestor=gw.attest)
        self.addCleanup(hosted.close)
        with self.assertRaises(ob.OutboxError):  # without the attestor (the CLI) a frame is not evidence
            self.box.approve(self.key, evidence)
        with self.assertRaises(ob.OutboxError):  # a frame whose MAC does not verify
            hosted.approve(self.key, {**evidence, "mac": "0" * 64})
        self.assertEqual(hosted.approve(self.key, evidence)["approved_by"], "owner@example.invalid")
        with self.assertRaises(ob.Blocked) as ctx:  # the CLI cannot send a gateway-approved row
            self.box.send(self.key, self.transport)
        self.assertEqual(ctx.exception.reason, "not_approved")
        hosted.db.execute("UPDATE outbox SET approval_evidence=? WHERE idempotency_key=?",
                          (_json.dumps({**evidence, "mac": "0" * 64}), self.key))
        with self.assertRaises(ob.Blocked):  # tampered stored evidence never sends, even in the gateway
            hosted.send(self.key, self.transport)
        self.assertEqual(self.sent, [])

    def test_cli_owner_flow_payload_sign_approve(self):
        """How the owner approves: print the payload, read it, sign it, hand the signature to `approve`."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ob.main(["--journal", str(self.journal), "payload", self.key]), 0)
        sig = Path(self.tmp.name) / "approval.sig"
        sig.write_text(self.owner.sign(out.getvalue().encode("utf-8")), encoding="utf-8")
        with self.owner.trust(), contextlib.redirect_stdout(io.StringIO()) as done:
            self.assertEqual(ob.main(["--journal", str(self.journal), "approve", self.key, "--signature", str(sig)]), 0)
        self.assertIn('"approved_by": "owner@example.invalid"', done.getvalue())
        self.assertEqual(self.box.row(self.key)["state"], "approved")

    def test_retry_payload_export_is_the_round_retry_needs(self):
        """After a definite failure `payload KEY` used to print round 0, which approve_retry refuses."""
        with self.owner.trust():
            first = self.evidence(self.owner)
            self.box.approve(self.key, first)
            with self.assertRaises(ob.OutboxError):
                self.box.send(self.key, lambda d: (_ for _ in ()).throw(ob.OutboxError("definite refusal")))
            self.assertEqual(self.box.row(self.key)["state"], "failed")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(ob.main(["--journal", str(self.journal), "payload", self.key]), 0)
            self.assertIn("round: 1", out.getvalue())
            later = "2099-01-01T00:00:00Z"
            with self.assertRaises(ob.OutboxError):  # the original round-0 approval cannot be replayed
                self.box.approve_retry(self.key, first, later)
            evidence = {"kind": "ssh", "signature": self.owner.sign(out.getvalue().encode("utf-8"))}
            row = self.box.approve_retry(self.key, evidence, later)
            self.assertEqual((row["state"], row["approved_by"]), ("approved", "owner@example.invalid"))
            self.assertIn("round: 1", self.box.approval_payload(self.key).decode("utf-8"))  # send re-verifies round 1
            self.assertEqual(self.box.send(self.key, self.transport)["state"], "sent")
        self.assertEqual(len(self.sent), 1)

    def test_cli_approve_retry_refuses_the_original_signature(self):
        with self.owner.trust():
            sig = Path(self.tmp.name) / "first.sig"
            sig.write_text(self.evidence(self.owner)["signature"], encoding="utf-8")
            self.box.approve(self.key, {"kind": "ssh", "signature": sig.read_text(encoding="utf-8")})
            self.box.db.execute("UPDATE outbox SET state='failed', updated_at='2000-01-01T00:00:00Z' WHERE idempotency_key=?",
                                (self.key,))
            with contextlib.redirect_stdout(io.StringIO()) as done:
                code = ob.main(["--journal", str(self.journal), "approve-retry", self.key, "--signature", str(sig)])
        self.assertEqual(code, 2)
        self.assertIn('"state": "refused"', done.getvalue())
        self.assertEqual(self.box.row(self.key)["state"], "failed")

    # -- RF0210-3: the signed draft fee and the fee-cap column must be the same exact integer cents --
    def snapshot(self, box):
        return ([tuple(r) for r in box.db.execute("SELECT *, typeof(fee_cap_cents) FROM outbox ORDER BY idempotency_key")],
                [tuple(r) for r in box.db.execute("SELECT * FROM outbox_retry_approval ORDER BY event_id")])

    def fee_box(self, cap):
        box = ob.Outbox(self.journal, campaign_fee_cap_cents=cap)
        self.addCleanup(box.close)
        return box

    def approved_fee_row(self, box, request_id, fee):
        key = box.propose(ob.RequestDraft(**{**DRAFT, "request_id": request_id, "agency_id": request_id, "fee_cap_cents": fee}))["idempotency_key"]
        box.approve(key, {"kind": "ssh", "signature": self.owner.sign(box.approval_payload(key))})
        return key

    def test_signed_fee_column_mismatch_cannot_bypass_campaign_cap(self):
        """Signed 5000 cents with the fee column edited to 0 used to pass a zero campaign cap and send 5000."""
        for cap in (0, 5000, None):
            for signed, column in ((5000, 0), (0, 9999), (5000, 4999)):
                with self.subTest(cap=cap, signed=signed, column=column), self.owner.trust():
                    box = self.fee_box(cap)
                    key = self.approved_fee_row(box, f"R-FEE-{cap}-{signed}-{column}", signed)
                    box.db.execute("UPDATE outbox SET fee_cap_cents=? WHERE idempotency_key=?", (column, key))
                    before = self.snapshot(box)
                    with self.assertRaises(ob.Blocked) as ctx:
                        box.send(key, self.transport)
                    self.assertEqual((ctx.exception.reason, self.sent), ("invalid_fee_cap", []))
                    self.assertEqual(ctx.exception.detail, "stored fee must be valid integer cents matching the approved draft")
                    self.assertEqual(self.snapshot(box), before)
                    self.assertFalse(box.db.in_transaction)
                    # The signature itself is still valid: restoring the column puts the row back under the
                    # ordinary cap rules (sendable when the signed fee fits, fee_cap_exceeded otherwise).
                    box.db.execute("UPDATE outbox SET fee_cap_cents=? WHERE idempotency_key=?", (signed, key))
                    if cap is None or signed <= cap:
                        self.assertEqual(box.send(key, self.transport)["state"], "sent")
                        self.sent.clear()
                    else:
                        with self.assertRaises(ob.Blocked) as again:
                            box.send(key, self.transport)
                        self.assertEqual(again.exception.reason, "fee_cap_exceeded")

    def test_legacy_signed_malformed_fee_is_blocked_without_rewrite(self):
        """A malformed fee that was signed as-is is refused at send, even after reopening the journal."""
        cases = [("float", 0.9, 0), ("string", "5", 5), ("bool", True, 1), ("negative", -1, -1),
                 ("oversize", 2 ** 63, 0), ("nan", float("nan"), 0), ("null", None, 0), ("integral float", 5000.0, 5000)]
        for name, signed, column in cases:
            with self.subTest(name), self.owner.trust():
                box = self.fee_box(None)
                key = box.propose(ob.RequestDraft(**{**DRAFT, "request_id": "R-LEGACY-" + name, "agency_id": "legacy-" + name}))["idempotency_key"]
                fields = json.loads(box.row(key)["draft_json"])
                fields["fee_cap_cents"] = signed  # historical journals could hold this; propose() now refuses it
                box.db.execute("UPDATE outbox SET draft_json=?, fee_cap_cents=? WHERE idempotency_key=?",
                               (json.dumps(fields, sort_keys=True), column, key))
                box.approve(key, {"kind": "ssh", "signature": self.owner.sign(box.approval_payload(key))})
                self.assertEqual(box.row(key)["state"], "approved")  # the signature over the malformed text verifies
                before = self.snapshot(box)
                for attempt in (box, self.fee_box(None)):  # then again after reopening the journal
                    with self.assertRaises(ob.Blocked) as ctx:
                        attempt.send(key, self.transport)
                    self.assertEqual(ctx.exception.reason, "invalid_fee_cap")
                    self.assertEqual(self.snapshot(attempt), before)
                    self.assertFalse(attempt.db.in_transaction)
                self.assertEqual(self.sent, [])

    def test_fractional_fee_is_not_truncated_into_the_column(self):
        """propose() used int(0.9) == 0 for the column while the transport saw 0.9; it now refuses."""
        box = self.fee_box(0)
        with self.assertRaises(ob.OutboxError):
            box.propose(ob.RequestDraft(**{**DRAFT, "request_id": "R-FRAC", "fee_cap_cents": 0.9}))
        self.assertEqual([r["request_id"] for r in box.rows()], ["R-1"])


if __name__ == "__main__":
    unittest.main()
