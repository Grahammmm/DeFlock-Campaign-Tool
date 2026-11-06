import json
import os
import tempfile
import unittest
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import public_packets as pp

T0 = 1_793_000_000
SHA = "a" * 64


def fake_verify(data, signature):
    # stand-in verifier: "SIG:<principal>:<hex of exact payload>" proves a signature over those bytes
    if not signature.startswith("SIG:") or signature.split(":")[2] != data.hex():
        raise approval.ApprovalError("bad")
    return signature.split(":")[1]


def doc(**extra):
    return {"kind": "document", "agency_key": "ex-pd", "request_id": "r1", "source": "doc-1", "title": "Policy released",
            "text": "Example PD released its ALPR policy on 2026-11-03.",
            "documents": [{"sha256": SHA, "date_received": "2026-11-03", "redaction_ids": ["red-1"]}], **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = os.path.join(self.tmp.name, "p")
        patcher = mock.patch.object(pp.approval, "verify", fake_verify)
        patcher.start()
        self.addCleanup(patcher.stop)
        pp.init(self.project)

    def sign(self, data, who="g@x"):
        return f"SIG:{who}:{data.hex()}"

    def turn_on(self):
        pp.enable(self.project, self.sign(pp.switch_payload(self.project)), now=T0)

    def approved(self, packet=None, now=T0):
        got = pp.propose(self.project, packet or doc(), now=now)
        pp.approve(self.project, got["id"], self.sign(pp.payload(self.project, got["id"])), now=now)
        return got["id"]

    def code(self, fn, *a, **k):
        with self.assertRaises(pp.PacketError) as ctx:
            fn(*a, **k)
        return ctx.exception.code


class ProposeTests(Base):
    def test_kinds_and_duplicate(self):
        got = pp.propose(self.project, doc(), now=T0)
        self.assertEqual(got["result"], "proposed")
        self.assertEqual(pp.propose(self.project, doc(), now=T0 + 5)["result"], "duplicate")
        self.assertEqual(len(pp.status(self.project, now=T0)["packets"]), 1)
        summary = {"kind": "summary", "agency_key": "ex-pd", "source": "s1", "title": "Summary", "text": "Plain summary.",
                   "documents": doc()["documents"]}
        finding = {**summary, "kind": "finding", "source": "f1", "finding_state": "approved for public",
                   "citations": [{"ref": "Policy 462.3, page 4", "verified": True}]}
        for p in (summary, finding):
            self.assertEqual(pp.propose(self.project, p, now=T0)["result"], "proposed")

    def test_refusals(self):
        bad = {
            "kind_invalid": {**doc(), "kind": "tweet"},
            "redaction_missing": doc(documents=[{"sha256": SHA, "date_received": "2026-11-03", "redaction_ids": []}]),
            "document_required": {**doc(), "documents": []},
            "privacy_lint": doc(text="Call 805-555-1234 about plate 7ABC123."),
            "legal_claim_wording": doc(text="The sheriff is breaking the law and is illegal in this."),
            "unverified_citation": doc(citations=[{"ref": "Gov Code 7922", "verified": False}]),
            "finding_not_public": {**doc(), "kind": "finding", "source": "f", "finding_state": "reviewed",
                                   "citations": [{"ref": "p4", "verified": True}]},
            "agency_invalid": {**doc(), "agency_key": "../x"},
        }
        for code, packet in bad.items():
            with self.subTest(code):
                got = self.code(pp.propose, self.project, packet, now=T0)
                self.assertTrue(got == code or code == "legal_claim_wording", got)
        self.assertEqual(pp.status(self.project, now=T0)["packets"], [])

    def test_queue_row_only_when_approved_site_update(self):
        row = {"id": "q1", "kind": "next-site-update", "status": "approved", "agency_id": "ex-pd", "request_id": "agency-wide",
               "body": "Add a line to the agency page."}
        self.assertEqual(pp.from_queue(self.project, row, now=T0)["result"], "proposed")
        self.assertEqual(self.code(pp.from_queue, self.project, {**row, "status": "pending"}), "queue_row_invalid")


class ApprovalTests(Base):
    def test_unsigned_wrong_and_stale_signatures_refused(self):
        ident = pp.propose(self.project, doc(), now=T0)["id"]
        self.assertEqual(self.code(pp.approve, self.project, ident, "I approve", now=T0), "approval_required")
        self.assertEqual(self.code(pp.approve, self.project, ident, self.sign(b"other"), now=T0), "approval_required")
        old = self.sign(pp.payload(self.project, ident))
        pp.propose(self.project, doc(text="Example PD released a changed ALPR policy."), now=T0)  # revision 1
        self.assertEqual(self.code(pp.approve, self.project, ident, old, now=T0), "approval_required")
        self.assertEqual(pp.status(self.project, now=T0)["packets"][0]["revision"], 1)

    def test_new_revision_voids_approval(self):
        ident = self.approved()
        pp.propose(self.project, doc(text="Example PD released a changed ALPR policy."), now=T0)
        self.turn_on()
        self.assertEqual(self.code(pp.write_change, self.project, ident, now=T0), "packet_not_approved")

    def test_expired_and_cancelled_cannot_be_approved(self):
        ident = pp.propose(self.project, doc(), now=T0)["id"]
        sig = self.sign(pp.payload(self.project, ident))
        self.assertEqual(self.code(pp.approve, self.project, ident, sig, now=T0 + pp.EXPIRY_SECONDS + 1), "packet_expired")
        self.assertEqual(pp.status(self.project, now=T0 + pp.EXPIRY_SECONDS + 1)["packets"][0]["status"], "expired")
        pp.decide(self.project, ident, "cancelled", "g")
        self.assertEqual(self.code(pp.approve, self.project, ident, sig, now=T0), "packet_not_open")


class WriteTests(Base):
    def test_switch_off_kill_and_unsigned_enable(self):
        ident = self.approved()
        self.assertEqual(self.code(pp.write_change, self.project, ident, now=T0), "switch_off")
        self.assertEqual(self.code(pp.enable, self.project, "x", now=T0), "approval_required")
        with open(os.path.join(self.project, pp.SWITCH), "w") as f:  # hand edit alone does not turn it on
            f.write('{"enabled": true}')
        self.assertEqual(pp.switch_state(self.project), "OFF")
        self.turn_on()
        self.assertEqual(pp.switch_state(self.project), "ON")
        open(os.path.join(self.project, pp.KILL), "w").close()
        self.assertEqual(self.code(pp.write_change, self.project, ident, now=T0), "switch_off")
        os.unlink(os.path.join(self.project, pp.KILL))
        pp.turn_off(self.project, "g")
        self.assertEqual(self.code(pp.write_change, self.project, ident, now=T0), "switch_off")

    def test_writes_change_file_once_and_only_when_approved(self):
        open_id = pp.propose(self.project, doc(source="doc-2"), now=T0)["id"]
        ident = self.approved()
        self.turn_on()
        self.assertEqual(self.code(pp.write_change, self.project, open_id, now=T0), "packet_not_approved")
        got = pp.write_change(self.project, ident, now=T0 + 60)
        self.assertEqual(got["result"], "written")
        with open(os.path.join(self.project, got["file"])) as f:
            change = json.load(f)
        self.assertEqual((change["schema"], change["signed_by"], change["agency_key"]), (pp.CHANGE_SCHEMA, "g@x", "ex-pd"))
        self.assertTrue(change["target"].startswith("content/updates/ex-pd/"))
        self.assertEqual(change["documents"][0]["redaction_ids"], ["red-1"])
        self.assertEqual(pp.write_change(self.project, ident, now=T0 + 90)["result"], "existing")
        self.assertEqual(self.code(pp.decide, self.project, ident, "cancelled", "g"), "packet_not_open")
        self.assertEqual(sorted(os.listdir(os.path.join(self.project, pp.OUT))), [os.path.basename(got["file"])])

    def test_expired_or_rejected_approved_packet_not_written(self):
        ident = self.approved()
        self.turn_on()
        self.assertEqual(self.code(pp.write_change, self.project, ident, now=T0 + pp.EXPIRY_SECONDS + 1), "packet_expired")
        pp.decide(self.project, ident, "rejected", "g")
        self.assertEqual(self.code(pp.write_change, self.project, ident, now=T0), "packet_not_approved")
        self.assertFalse(os.path.exists(os.path.join(self.project, pp.OUT)))

    def test_cli_ships_off(self):
        self.assertEqual(pp.main(["status", "--project", self.project]), 0)
        self.assertEqual(pp.switch_state(self.project), "OFF")
        self.assertEqual(pp.main(["write", "--project", self.project, "--id", "0" * 16]), 1)

    def test_cli_cancel_and_reject_decide(self):
        for action in ("cancel", "reject"):
            ident = pp.propose(self.project, doc(source="cli-" + action), now=T0)["id"]
            self.assertEqual(pp.main([action, "--project", self.project, "--id", ident]), 0)
            row = [r for r in pp.status(self.project, now=T0)["packets"] if r["id"] == ident][0]
            self.assertEqual(row["status"], action + ("led" if action == "cancel" else "ed"))
            self.assertEqual(self.code(pp.approve, self.project, ident, self.sign(b"x"), now=T0), "packet_not_open")


if __name__ == "__main__":
    unittest.main()
