"""Trust outage/revocation semantics, synthetic journals and ephemeral signing keys only."""
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import approval_queue as aq, console_apply as ca, console_export as ce
from campaign_tool.records import console_journal as cj, public_packets as pp, public_updates as pu
from tests.approval_signing import Signer
from tests.records.test_console_model import ModelBase


class ErrorClasses(unittest.TestCase):
    def setUp(self):
        self.signer = Signer()
        self.addCleanup(self.signer.close)
        self.data = b"synthetic approval"
        self.sig = self.signer.sign(self.data)

    def test_unavailable_vs_invalid(self):
        with self.signer.trust():
            for error in (OSError("offline"), subprocess.TimeoutExpired("ssh-keygen", 30)):
                with mock.patch.object(approval.subprocess, "run", side_effect=error):
                    with self.assertRaises(approval.TrustUnavailable):
                        approval.verify(self.data, self.sig)
            with mock.patch.object(approval, "_require_root", side_effect=approval.TrustUnavailable("missing")):
                with self.assertRaises(approval.TrustUnavailable):
                    approval.verify(self.data, self.sig)
                with self.assertRaises(approval.ApprovalError) as got:
                    approval.verify(self.data, "not an SSH signature")
                self.assertNotIsInstance(got.exception, approval.TrustUnavailable)
            with self.assertRaises(approval.ApprovalError) as got:
                approval.verify(b"different bytes", self.sig)
            self.assertNotIsInstance(got.exception, approval.TrustUnavailable)
            self.signer.signers.write_text("")
            with self.assertRaises(approval.TrustUnavailable):
                approval.verify(self.data, self.sig)

    def test_untrusted_key_is_final_but_missing_file_is_unavailable(self):
        other = Signer("other@example.invalid")
        self.addCleanup(other.close)
        with self.signer.trust():
            with self.assertRaises(approval.ApprovalError) as got:
                approval.verify(self.data, other.sign(self.data))
            self.assertNotIsInstance(got.exception, approval.TrustUnavailable)
            self.signer.signers.unlink()
            with self.assertRaises(approval.TrustUnavailable):
                approval.verify(self.data, self.sig)

    def test_unreadable_uid_map_and_writable_verifier_fail_closed(self):
        with mock.patch("builtins.open", side_effect=OSError("unreadable")):
            self.assertFalse(approval.init_userns())
            with self.assertRaises(approval.TrustUnavailable):
                approval._require_root(approval.TRUSTED_SIGNERS)
        with mock.patch.object(approval, "init_userns", return_value=False), \
                mock.patch.object(approval, "overflowuid", return_value=65534), \
                mock.patch.object(approval, "_ro", return_value=False):
            with self.assertRaises(approval.TrustUnavailable):
                approval._require_root(approval.SSH_KEYGEN)

    def test_anchor_cli_is_metadata_only_and_rejects_other_options(self):
        out = io.StringIO()
        with mock.patch.object(approval, "anchor_state", return_value={"state": "unavailable", "reason": "missing"}), \
                redirect_stdout(out):
            self.assertEqual(approval.main(["--anchor-status"]), 0)
        self.assertEqual(out.getvalue(), "anchor unavailable: missing\n")


class Availability(ModelBase):
    def setUp(self):
        super().setUp()
        self.row = aq.add_draft(self.live, item_id="item", kind="reply-extension", agency_id="example", request_id="r1",
                                body="Synthetic draft.", evidence={}, source_run_id="synthetic")
        self.ident = self.row["id"]
        self.journal = self.tmp / "decisions.jsonl"

    def entry(self, action, value, revision=0):
        entry = {"schema": cj.SCHEMA, "action": action, "route": "/approvals/" + action,
                 "target_id": self.ident, "revision_seen": revision, "value": value, "actor": "operator",
                 "source_project_id": aq._project_id(self.live)}
        entry["entry_id"] = hashlib.sha256(cj._canon(entry).encode()).hexdigest()
        self.journal.write_text(json.dumps(entry) + "\n")
        return entry["entry_id"]

    def run_apply(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ca.main(["--project", str(self.live), "--from-journal", str(self.journal)]), 0)
        return out.getvalue()

    def raw(self):
        return (self.live / "approvals/queue.jsonl").read_bytes()

    def sign(self):
        return self.signer.sign(aq.approval_payload(self.live, self.ident))

    def approve(self):
        return aq.approve(self.live, self.ident, {"kind": "ssh", "signature": self.sign()})

    def test_mixed_version_window_defers_keeps_intent_then_applies_and_replays(self):
        ident = self.entry("approve", self.sign())
        before = self.raw()
        with mock.patch.object(approval, "_require_root", side_effect=approval.TrustUnavailable("missing")):
            log = self.run_apply()
            self.assertIn("approve DEFERRED", log)
            self.assertEqual(log.count("approval trust unavailable (anchor or verifier unavailable)"), 1)
            self.assertEqual(self.raw(), before)
            final, intents = ca.ledger(self.live)
            self.assertEqual(final, {})
            self.assertIn(ident, intents)
        self.assertIn("approve APPLIED", self.run_apply())
        self.assertEqual(aq.load_queue(self.live)[0]["status"], "approved")
        self.assertEqual(ca.receipts(self.live), {ident: "APPLIED"})
        self.assertIn("approve ALREADY", self.run_apply())

    def test_junk_is_final_before_trust_check(self):
        ident = self.entry("approve", "junk")
        with mock.patch.object(approval, "_require_root", side_effect=approval.TrustUnavailable("missing")):
            self.assertIn("SIGNATURE_REFUSED", self.run_apply())
        self.assertEqual(ca.receipts(self.live), {ident: "SIGNATURE_REFUSED"})

    def test_s1_restore_same_evidence_and_export_effective_state(self):
        approved = self.approve()
        before = self.raw()
        with mock.patch.object(approval, "_require_root", side_effect=approval.TrustUnavailable("missing")):
            pending = aq.load_queue(self.live)[0]
            self.assertEqual((pending["status"], pending["revision"], pending["approval_unverified"]),
                             ("pending", 1, "unavailable"))
            dest, manifest, model = self.export()
            self.assertEqual(manifest["approval_anchor"], {"state": "unavailable", "reason": "missing"})
            self.assertEqual(model["queue.json"]["rows"][0]["status"], "pending")
        restored = aq.load_queue(self.live)[0]
        self.assertEqual(restored["approval_evidence"], approved["approval_evidence"])
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.export()[1]["approval_anchor"], {"state": "ok"})

    def test_s2_reject_waits_then_refuses_without_poisoning_queue(self):
        self.approve()
        before = self.raw()
        ident = self.entry("reject", "not ours", 1)
        with mock.patch.object(approval, "_require_root", side_effect=approval.TrustUnavailable("missing")):
            self.assertIn("reject DEFERRED", self.run_apply())
        self.assertEqual(self.raw(), before)
        self.assertIn("reject INVALID_TRANSITION", self.run_apply())
        self.assertEqual(ca.receipts(self.live)[ident], "INVALID_TRANSITION")
        self.assertEqual(aq.load_queue(self.live)[0]["status"], "approved")
        self.assertEqual(self.raw(), before)

    def test_s3_rotation_requires_edit_before_current_key_approval(self):
        self.approve()
        before = self.raw()
        new = Signer("new@example.invalid")
        self.addCleanup(new.close)
        with new.trust():
            self.assertEqual(aq.load_queue(self.live)[0]["approval_unverified"], "untrusted")
            ident = self.entry("approve", new.sign(aq.approval_payload(self.live, self.ident)), 1)
            self.assertIn("approve INVALID_TRANSITION", self.run_apply())
            self.assertEqual(ca.receipts(self.live)[ident], "INVALID_TRANSITION")
            self.assertEqual(self.raw(), before)
            self.entry("edit", "Revised synthetic draft.", 1)
            self.assertIn("edit APPLIED", self.run_apply())
            self.entry("approve", new.sign(aq.approval_payload(self.live, self.ident)), 2)
            self.assertIn("approve APPLIED", self.run_apply())
        combined = new.dir / "both"
        combined.write_text(new.signers.read_text() + self.signer.signers.read_text())
        with new.trust(combined):
            self.assertEqual(aq.load_queue(self.live)[0]["status"], "approved")

    def test_s4_edit_in_window_clears_recorded_approval(self):
        self.approve()
        self.entry("edit", "Changed synthetic text.", 1)
        with mock.patch.object(approval, "_require_root", side_effect=approval.TrustUnavailable("missing")):
            self.assertIn("edit APPLIED", self.run_apply())
            row = aq.load_queue(self.live)[0]
            self.assertEqual((row["status"], row["revision"]), ("pending", 2))
            self.assertNotIn("approval_unverified", row)

    def test_downstream_mail_and_followup_restore_without_new_approval(self):
        from campaign_tool.records import followup_clock as fc, mail_drafts
        from tests.records.test_followup_clock import row, doc
        settings = {"default": {"autonomy": "off", "notice_after_days": 10, "silence_after_days": 0},
                    "agencies": {"ag1": {"autonomy": "draft"}}}
        item = fc.item_id("ag1", "req-1", "determination", "2030-01-12")
        draft = aq.add_draft(self.live, item_id=item, kind="reminder-no-response", agency_id="ag1", request_id="req-1",
                             body="Synthetic reminder.", evidence={}, source_run_id="synthetic")
        ident = draft["id"]
        aq.approve(self.live, ident, {"kind": "ssh", "signature": self.signer.sign(aq.approval_payload(self.live, ident))})
        register = doc([row()], as_of="2035-01-01")
        def observed():
            queue = aq.load_queue(self.live)
            planned = fc._plan(register, settings, queue, "2035-01-01")[0]
            due = fc.clock_table(register, settings, queue, as_of="2035-01-01")[0]["notice_due"]
            mail = mail_drafts.place(self.live, {}, dry_run=True)["would_place"]
            return any(p["kind"] == fc.NOTICE for p in planned), due, mail
        before = observed()
        self.assertTrue(before[0])
        self.assertIsNotNone(before[1])
        self.assertEqual(before[2], 1)
        with mock.patch.object(approval, "_require_root", side_effect=approval.TrustUnavailable("missing")):
            self.assertEqual(observed(), (False, None, 0))
        self.assertEqual(observed(), before)

    def test_import_accepts_manifest_with_and_without_anchor_field(self):
        from tests.records.test_console_export import unlock
        for with_anchor in (True, False):
            dest, manifest, _ = self.export()
            if not with_anchor:
                manifest.pop("approval_anchor")
                (dest / ce.MANIFEST).write_text(json.dumps(manifest))
            stream = io.BytesIO()
            ce.write_tar(dest, stream)
            target = self.tmp / ("installed-" + str(with_anchor))
            target.mkdir(mode=0o700)
            self.addCleanup(unlock, target)
            ce.install(str(target), io.BytesIO(stream.getvalue()))
            self.assertTrue((target / "current" / ce.MANIFEST).exists())

    def test_packet_and_switch_availability_is_not_final_signature_refusal(self):
        packet = {"kind": "site_update", "agency_key": "synthetic", "request_id": "", "source": "q1",
                  "title": "Synthetic update", "text": "Synthetic text."}
        ident = pp.propose(self.live, packet)["id"]
        with mock.patch.object(approval, "verify", side_effect=approval.TrustUnavailable("missing")):
            for fn, args, cls in ((pp.approve, (self.live, ident, "armored"), pp.PacketError),
                                  (pp.enable, (self.live, "armored"), pp.PacketError),
                                  (pu.enable, (self.live, "armored"), pu.PublicUpdatesError)):
                with self.assertRaises(cls) as got:
                    fn(*args)
                self.assertEqual(got.exception.code, "approval_unavailable")
