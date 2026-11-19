"""A6 phone tap: a passkey assertion approves a queued draft once, through records decisions-apply (synthetic keys only).

Contract: the tap counts only when ssh-keygen verifies it against the trust file as an enrolled phone key, its signed
envelope names this decision and the exact approval payload, it is inside its validity window, its nonce is unused and
the authenticator reports presence and verification. The typed SSH signature path is unchanged beside it.
"""
import io
import shutil
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from campaign_tool import approval, phone_approval
from campaign_tool.records import approval_queue as aq, console_apply as ca, console_journal as cj
from tests.approval_signing import Signer
from tests.phone_signing import Phone


class PhoneTap(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.live, self.j = self.tmp / "live", self.tmp / "journal"
        self.live.mkdir()
        self.j.mkdir(mode=0o700)
        self.computer, self.phone = Signer("g@example.invalid"), Phone()
        self.addCleanup(self.computer.close)
        self.trust_file = self.tmp / "allowed_signers"
        self.trust_file.write_text(self.computer.signers.read_text() + self.phone.signers_line())
        trust = self.computer.trust(self.trust_file)
        trust.__enter__()
        self.addCleanup(trust.__exit__, None, None, None)
        self.ident = aq.add_draft(self.live, item_id="item", kind="reply-extension", agency_id="example", request_id="r1",
                                  body="Synthetic draft.", evidence={}, source_run_id="synthetic")["id"]
        self.pin = types.SimpleNamespace(source_project_id=aq._project_id(self.live), run_id="run", name="export")

    def envelope(self, issued=None, ttl=phone_approval.DEFAULT_TTL, payload=None, nonce="ab" * 16):
        payload = aq.approval_payload(self.live, self.ident) if payload is None else payload
        return phone_approval.envelope(self.ident, payload, int(time.time()) if issued is None else issued, nonce, ttl)

    def journal(self, action, value, actor="g-phone"):
        cj.append(str(self.j), self.pin, actor, "/approvals/" + action, action, self.ident, 0, value)

    def tap_entry(self, tap, actor="g-phone"):
        self.journal(ca.PHONE_APPROVE, cj._canon(tap), actor)

    def apply(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ca.main(["--project", str(self.live), "--from-journal", str(self.j / cj.FILE)]), 0)
        return [line.split(" ", 3)[3] for line in out.getvalue().splitlines() if not line.endswith("unavailable)")]

    def row(self):
        return aq.load_queue(self.live)[0]

    def queue_bytes(self):
        return (self.live / "approvals" / "queue.jsonl").read_bytes()

    # Bug caught: a tap path that does not reach ssh-keygen with the phone namespace, or does not record the approval.
    def test_valid_tap_is_applied_once(self):
        self.tap_entry(self.phone.tap(self.envelope()))
        self.assertEqual(self.apply(), ["APPLIED"])
        row = self.row()
        self.assertEqual((row["status"], row["approved_by"]), ("approved", self.phone.principal))
        before = self.queue_bytes()
        self.assertEqual(self.apply(), ["ALREADY"])
        self.assertEqual(self.queue_bytes(), before)

    # Bug caught: a captured tap re-journaled (a new entry id) being accepted again or silently counted as applied.
    def test_replay_of_a_used_tap_is_refused(self):
        tap = self.phone.tap(self.envelope())
        self.tap_entry(tap)
        self.assertEqual(self.apply(), ["APPLIED"])
        before = self.queue_bytes()
        self.tap_entry(tap, actor="someone-else")
        self.assertEqual(self.apply(), ["ALREADY", "REFUSED phone_replay"])
        self.assertEqual(self.queue_bytes(), before)

    # Bug caught: approving text other than the exact payload (edited draft, swapped hash) or an envelope edited after
    # the tap.
    def test_tampered_item_hash_is_refused(self):
        wrong = self.envelope(payload=aq.approval_payload(self.live, self.ident) + b"x")
        self.tap_entry(self.phone.tap(wrong))
        signed = self.phone.tap(self.envelope(nonce="cd" * 16))
        forged = dict(signed, envelope=signed["envelope"].replace("cd" * 16, "ef" * 16))
        self.tap_entry(forged)
        self.assertEqual(self.apply(), ["SIGNATURE_REFUSED", "SIGNATURE_REFUSED"])
        self.assertEqual(self.row()["status"], "pending")

    # Bug caught: a missing or inverted clock check, or an envelope that grants itself more than MAX_TTL.
    def test_expired_or_future_tap_is_refused(self):
        now = int(time.time())
        self.tap_entry(self.phone.tap(self.envelope(issued=now - 2 * 86400, nonce="01" * 16)))
        self.tap_entry(self.phone.tap(self.envelope(issued=now + 3600, nonce="02" * 16)))
        self.assertEqual(self.apply(), ["REFUSED phone_expired", "REFUSED phone_expired"])
        with self.assertRaises(approval.ApprovalError):
            self.envelope(ttl=phone_approval.MAX_TTL + 1)
        self.assertEqual(self.row()["status"], "pending")

    # Bug caught: trusting the key carried inside the tap, a phone line without the phone namespace, a tap without
    # user verification, from another origin or from a framed (cross-origin) page, or keeping a removed (lost) phone's
    # approvals valid.
    def test_unknown_unverified_or_revoked_device_is_refused(self):
        stranger = Phone("stranger@example.invalid")
        self.tap_entry(stranger.tap(self.envelope(nonce="03" * 16)))
        self.tap_entry(self.phone.tap(self.envelope(nonce="04" * 16), flags=0x01))
        self.tap_entry(self.phone.tap(self.envelope(nonce="05" * 16), origin="https://evil.example.invalid"))
        self.tap_entry(self.phone.tap(self.envelope(nonce="08" * 16), cross=True))
        self.assertEqual(self.apply(), ["SIGNATURE_REFUSED"] * 4)
        self.trust_file.write_text(self.computer.signers.read_text()
                                   + self.phone.signers_line(namespaces=approval.NAMESPACE))
        self.tap_entry(self.phone.tap(self.envelope(nonce="06" * 16)))
        self.assertEqual(self.apply()[-1], "SIGNATURE_REFUSED")
        self.trust_file.write_text(self.computer.signers.read_text() + self.phone.signers_line())
        self.tap_entry(self.phone.tap(self.envelope(nonce="07" * 16)))
        self.assertEqual(self.apply()[-1], "APPLIED")
        self.trust_file.write_text(self.computer.signers.read_text())  # lost phone: its line is removed
        self.assertEqual(self.row()["approval_unverified"], "untrusted")
        self.assertEqual(self.row()["status"], "pending")

    # Bug caught: the phone path breaking or replacing the typed (pasted SSH signature) path.
    def test_typed_signature_fallback_still_applies(self):
        self.journal("approve", self.computer.sign(aq.approval_payload(self.live, self.ident)))
        self.assertEqual(self.apply(), ["APPLIED"])
        self.assertEqual(self.row()["approved_by"], "g@example.invalid")


if __name__ == "__main__":
    unittest.main()
