"""Synthetic local stores and real ephemeral signatures; no live source, trust or publication."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import threading
import time
import unittest
from unittest.mock import patch

from campaign_tool import approval
from campaign_tool.records import approval_queue as aq, draft_import, draft_promote as dp, public_packets as pp
from campaign_tool.records import redaction_record as rr
from campaign_tool.records.ledger import store
from tests.approval_signing import Signer
from tests.records import test_outcome_received_items as fixtures

BODY = "  Edited résumé\r\nReviewed wording. TAIL\r\n"
RUN = "run_20261113T150000Z_0a1b2c3d"
STAMP = "2026-11-10T00:00:00Z"


class PromotionTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ReceivedItemsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.project, self.fixture = fixture.project, fixture
        self.sha, self.original = fixture.document(["Plain synthetic policy."], "other")
        with store.ledger(fixture.database) as con:
            for stage in ("preserve", "extract"):
                receipt = dp._sha(stage.encode())
                con.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (receipt, stage, self.sha, "fixture", "fixture", "pass", "{}", "[]", "synthetic setup",
                             "fixture", "{}", STAMP, None, "fixture-receipt"))
                con.execute("UPDATE stage_state SET status='done', receipt_sha256=? WHERE original_sha256=? AND stage=?",
                            (receipt, self.sha, stage))
            con.commit()
        self.signer = Signer()
        self.addCleanup(self.signer.close)
        trust = self.signer.trust()
        trust.__enter__()
        self.addCleanup(trust.__exit__, None, None, None)
        self.received()
        self.record = self.redaction()
        self.id = self.queue()
        self.manifest = self.binding()

    def private(self, path, data):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for p in (path.parent, *path.parent.parents):
            if p == self.project.parent:
                break
            p.chmod(0o700)
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    def received(self, **extra):
        self.item = {"original_sha256": self.sha, "agency_id": "other", "request_id": "r1",
                     "extraction_status": "current", "received_at": STAMP, **extra}
        self.private(self.project / "run-outputs" / RUN / "outcomes" / "received-items.json",
                     json.dumps({"items": [self.item]}).encode())

    def redaction(self, derivative=b"Reviewed synthetic derivative.\n"):
        folder = rr.record_dir(self.project, "other", self.sha)
        def record(name, value):
            raw = (json.dumps(value, sort_keys=True) + "\n").encode()
            self.private(folder / name, raw)
            return raw
        raw = record("PROPOSAL.json", {"schema_version": 1, "document_id": self.sha, "source_sha256": self.sha,
                     "redaction_required": True, "proposed": [], "not_detected_note": "No patterns.",
                     "scan_receipt": "c" * 64, "created_at": STAMP})
        signed = {"document_id": self.sha, "source_sha256": self.sha, "proposal_sha256": dp._sha(raw),
                  "signed_by": self.signer.principal, "signed_at": STAMP}
        record("SIGNOFF.json", dict(signed, schema_version=1, scope="flock-redaction",
                                    signature=self.signer.sign(approval.payload("flock-redaction", self.sha, raw.decode()))))
        self.derivative = self.private(self.project / "redacted" / "policy.txt", derivative)
        return dp._sha(record("ELIGIBILITY.json", dict(signed, schema_version=1,
                      derivative_path=str(self.derivative), derivative_sha256=dp._sha(derivative), applied_at=STAMP)))

    def queue(self, kind="draft-agency-summary", body=BODY, approve=True, **extra):
        doc = {"schema": draft_import.SCHEMA, "draft_key": "summary" if kind.endswith("summary") else "correction",
               "kind": kind, "agency_id": "other", "request_id": "r1", "body": "Original imported wording.",
               "why": "Review for a proposed page update.", "source_run_id": RUN,
               "source_attachment_sha256": "d" * 64, "source_section": "section-1",
               "target": {"website": "example.invalid", "page": "agency", "section": "records"},
               "citations": [{"source_type": "original", "source_id": "sha256:" + self.sha,
                              "locator": "page 1", "as_of": "2026-11-10", "verification": "verified"}], **extra}
        self.input = self.private(self.project / (doc["draft_key"] + ".json"), json.dumps(doc).encode())
        ident = draft_import.import_draft(self.project, self.input, apply=True)["id"]
        aq.edit(self.project, ident, "editor", body, expected_revision=0)
        if approve:
            self.copy_signature = self.signer.sign(aq.approval_payload(self.project, ident))
            aq.approve(self.project, ident, {"kind": "ssh", "signature": self.copy_signature})
        return ident

    def binding(self, ident=None):
        row = next(r for r in aq.load_queue(self.project) if r["id"] == (ident or self.id))
        return {"schema": dp.SCHEMA, "project_id": aq._project_id(self.project), "queue_id": row["id"],
                "queue_revision": row["revision"], "body_sha256": dp._sha(row["body"].encode()),
                "evidence_sha256": dp._digest(row["evidence"]), "title": "Reviewed record summary",
                "sources": [{"citation_index": 0, "original_sha256": self.sha, "page": 1, "redaction_record_id": self.record}],
                "correction": None}

    def promote(self, manifest=None, apply=True, ident=None):
        return dp.promote(self.project, ident or self.id, manifest or self.manifest, apply=apply)

    def snapshot(self):
        return {str(p.relative_to(self.project)): (p.lstat().st_mode, p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.project.rglob("*") if p.is_file()}

    def stores(self):
        return [(self.project / name).read_bytes() if (self.project / name).exists() else None
                for name in (pp.JOURNAL, dp.RECEIPTS)]

    def refused(self, code=None, **kwargs):
        before = self.stores()
        with self.assertRaises(dp.DraftPromoteError) as ctx:
            self.promote(**kwargs)
        if code:
            self.assertEqual(ctx.exception.code, code)
        self.assertEqual(self.stores(), before)
        return ctx.exception.code

    def packet(self):
        return next(iter(pp.fold(dp._packet_events(self.project)).values()))

    def test_default_plan_creates_nothing_and_never_proposes(self):
        before = self.snapshot()
        with ExitStack() as stack:
            for name in ("propose", "write_change", "approve", "enable", "init"):
                stack.enter_context(patch.object(pp, name, side_effect=AssertionError(name)))
            result = self.promote(apply=False)
        self.assertEqual(result["action"], "would_propose")
        self.assertTrue(result["revision_is_prediction"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.project / dp.LOCK).exists())
        manifest = self.private(self.project / "manifest.json", json.dumps(self.manifest).encode())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(dp.main(["--project", str(self.project), "--id", self.id, "--reviewed-sources", str(manifest)]), 0)
        self.assertEqual(json.loads(out.getvalue())["schema"], dp.PLAN)
        self.assertNotIn("résumé", out.getvalue() + err.getvalue())
        self.assertFalse((self.project / pp.LOCK).exists())

    def test_edited_summary_and_correction_propose_exact_bytes(self):
        queue = (self.project / "approvals" / "queue.jsonl").read_bytes()
        original = self.original.read_bytes()
        result = self.promote()
        row = self.packet()
        self.assertEqual((result["action"], row["status"], row["revision"]), ("proposed", "proposed", 0))
        self.assertEqual(row["packet"]["text"].encode(), BODY.encode())
        self.assertNotIn("signed_by", row)
        self.assertFalse((self.project / pp.OUT).exists())
        self.assertFalse((self.project / pp.SWITCH).exists())
        self.assertEqual((self.project / "approvals" / "queue.jsonl").read_bytes(), queue)
        self.assertEqual(self.original.read_bytes(), original)
        self.assertEqual(stat.S_IMODE((self.project / dp.RECEIPTS).stat().st_mode), 0o600)
        pp.approve(self.project, row["id"], self.signer.sign(pp.payload(self.project, row["id"])))
        pp.enable(self.project, self.signer.sign(pp.switch_payload(self.project)))
        pp.write_change(self.project, row["id"])
        prior = self.stores()[0]
        correction = {"prior_statement": "Reviewed wording.", "prior_version": f"packet:{row['id']}@0",
                      "linkage": f"correction-of:{row['id']}"}
        ident = self.queue("draft-correction-notice", correction=correction)
        manifest = self.binding(ident)
        manifest["correction"] = dict(correction, packet_id=row["id"], packet_revision=0, text_sha256=dp._sha(BODY.encode()))
        self.assertEqual(self.promote(manifest, ident=ident)["action"], "proposed")
        rows = pp.fold(dp._packet_events(self.project))
        new = next(r for r in rows.values() if r["id"] != row["id"])
        self.assertEqual((new["packet"]["kind"], new["packet"]["text"].encode()), ("site_update", BODY.encode()))
        self.assertEqual(rows[row["id"]]["status"], "written")
        self.assertTrue(self.stores()[0].startswith(prior))

    def test_packet_cap_refuses_without_truncating(self):
        for text, allowed in (("é" * 4000, True), ("é" * 4001, False), ("x" * 3998 + "\r\n", True)):
            aq.edit(self.project, self.id, "editor", text)
            aq.approve(self.project, self.id, {"kind": "ssh", "signature": self.signer.sign(aq.approval_payload(self.project, self.id))})
            self.manifest = self.binding()
            if allowed:
                packet = dp.prepare(self.project, self.id, self.manifest)
                self.assertEqual(packet["text"].encode(), text.encode())
            else:
                self.refused("text_invalid")

    def test_current_queue_verification_not_snapshot_or_receipt(self):
        for key, value in (("project_id", "0" * 16), ("queue_revision", 0), ("body_sha256", "0" * 64),
                           ("evidence_sha256", "0" * 64)):
            with self.subTest(key=key):
                self.refused("stale_binding", manifest=dict(self.manifest, **{key: value}))
        other = Signer("other@example.invalid")
        self.addCleanup(other.close)
        self.signer.signers.write_text(other.signers.read_text())
        self.refused("queue_not_verified")
        self.signer.signers.unlink()
        self.refused("queue_not_verified")
        with other.trust():
            aq.edit(self.project, self.id, "editor", "Changed pending body.")
            self.refused("queue_not_verified")
            aq.reject(self.project, self.id, "editor", "Not ready")
            self.refused("queue_not_verified")

    def test_source_manifest_is_not_a_verification_override(self):
        for change, code in (({"page": 2}, "source_binding_invalid"), ({"original_sha256": "f" * 64}, "source_binding_invalid"),
                             ({"citation_index": 1}, "source_mapping_invalid"), ({"page": True}, "manifest_invalid")):
            manifest = copy.deepcopy(self.manifest)
            manifest["sources"][0].update(change)
            self.refused(code, manifest=manifest)
        self.refused("source_mapping_invalid", manifest=dict(self.manifest, sources=self.manifest["sources"] * 2))
        for extra in ({"agency_id": "elsewhere"}, {"request_id": "r2"}, {"received_at": "2026-02-30"},
                      {"extraction_status": "stale"}):
            self.received(**extra)
            self.refused()
        self.received()
        self.original.write_bytes(b"changed")
        self.refused("NOT_PRESERVED")

    def test_unverified_and_unsupported_citations_stay_review_only(self):
        # New signed queue identities avoid import conflicts; labels cannot be overridden by the manifest.
        for n, change in enumerate(({"source_type": "tracker"}, {"source_type": "public"},
                                    {"verification": "unverified"}, {"locator": "p. 1"}, {"source_id": "f" * 64})):
            citation = dict(source_type="original", source_id=self.sha, locator="page 1", as_of="2026-11-10", verification="verified")
            citation.update(change)
            ident = self.queue(draft_key="case-" + str(n), citations=[citation])
            self.refused(ident=ident, manifest=self.binding(ident))

    def test_redaction_and_review_gate_failure_changes_no_store(self):
        folder = rr.record_dir(self.project, "other", self.sha)
        signoff = folder / "SIGNOFF.json"
        raw = signoff.read_bytes()
        signoff.unlink()
        self.refused("REDACTION_UNVERIFIED")
        self.private(signoff, raw)
        self.derivative.write_bytes(b"changed")
        self.refused("REDACTION_UNVERIFIED")
        self.record = self.redaction(b"Contact fixture@example.invalid")
        self.manifest = self.binding()
        self.refused("privacy_lint")
        self.record = self.redaction(b"\xff")
        self.manifest = self.binding()
        self.refused("derivative_not_text")

    def test_correction_predecessor_is_resolved_not_just_nonempty(self):
        correction = {"prior_statement": "Prior words", "prior_version": "anything importable", "linkage": "free string"}
        ident = self.queue("draft-correction-notice", correction=correction)
        manifest = self.binding(ident)
        manifest["correction"] = dict(correction, packet_id="0" * 16, packet_revision=0, text_sha256="a" * 64)
        self.refused("correction_linkage_unavailable", ident=ident, manifest=manifest)
        # Canonical strings alone still prove no written predecessor.
        correction.update(prior_version="packet:" + "0" * 16 + "@0", linkage="correction-of:" + "0" * 16)
        ident = self.queue("draft-correction-notice", draft_key="canonical", correction=correction)
        manifest = self.binding(ident)
        manifest["correction"] = dict(correction, packet_id="0" * 16, packet_revision=0, text_sha256="a" * 64)
        self.refused("correction_linkage_unavailable", ident=ident, manifest=manifest)

    def test_written_predecessor_hash_statement_target_attribution_and_legacy_negatives(self):
        pp.enable(self.project, self.signer.sign(pp.switch_payload(self.project)))
        for n, failure in enumerate(("hash", "statement", "target", "agency", "request", "legacy", "revision")):
            predecessor = self.queue(draft_key="predecessor-" + str(n))
            old = dp.prepare(self.project, predecessor, self.binding(predecessor))
            if failure == "target":
                old["draft_provenance"]["target"]["section"] = "earlier-section"
            elif failure in ("agency", "request"):
                old["agency_key" if failure == "agency" else "request_id"] = "elsewhere"
            elif failure == "legacy":
                old.pop("draft_provenance")
            got = pp.propose(self.project, old)
            pp.approve(self.project, got["id"], self.signer.sign(pp.payload(self.project, got["id"])))
            pp.write_change(self.project, got["id"])
            revision = got["revision"] + (failure == "revision")
            correction = {"prior_statement": "Absent statement" if failure == "statement" else "Reviewed wording.",
                          "prior_version": f"packet:{got['id']}@{revision}", "linkage": f"correction-of:{got['id']}"}
            ident = self.queue("draft-correction-notice", draft_key="written-negative-" + str(n), correction=correction)
            manifest = self.binding(ident)
            manifest["correction"] = dict(correction, packet_id=got["id"], packet_revision=revision,
                                          text_sha256="0" * 64 if failure == "hash" else dp._sha(BODY.encode()))
            self.refused("correction_linkage_unavailable", ident=ident, manifest=manifest)

    def test_identical_repeat_revalidates_then_noops(self):
        self.promote()
        before = self.stores()
        self.assertEqual(self.promote()["action"], "existing")
        self.assertEqual(self.stores(), before)
        self.derivative.write_bytes(b"changed")
        self.refused("REDACTION_UNVERIFIED")

    def test_missing_units_out_of_range_legal_wording_and_finding_refuse(self):
        cases = [({"locator": "page 2"}, 2, "page_unavailable"),
                 ({"source_type": "finding"}, 1, "review_gate_unavailable")]
        for n, (change, page, code) in enumerate(cases):
            citation = dict(source_type="original", source_id=self.sha, locator="page 1", as_of="2026-11-10", verification="verified")
            citation.update(change)
            ident = self.queue(draft_key="pages-" + str(n), citations=[citation])
            manifest = self.binding(ident)
            manifest["sources"][0]["page"] = page
            self.refused(code, ident=ident, manifest=manifest)
        ident = self.queue(draft_key="legal", body="The sheriff is breaking the law and is illegal in this.")
        self.refused("legal_claim_wording", ident=ident, manifest=self.binding(ident))
        with store.ledger(self.fixture.database) as con:
            con.execute("DELETE FROM extraction_adapter_current")
            con.commit()
        self.refused("page_unavailable")

    def test_receipt_conflict_safety_and_full_preflight(self):
        with patch.object(dp, "MAX_RECEIPTS", 1):
            self.refused("receipt_full")
        self.assertFalse((self.project / pp.JOURNAL).exists())
        self.promote()
        path = self.project / dp.RECEIPTS
        receipt = json.loads(path.read_bytes())
        receipt["body_sha256"] = "0" * 64
        self.private(path, (json.dumps(receipt) + "\n").encode())
        self.refused("promotion_conflict")
        path.chmod(0o644)
        self.refused()

    def test_cancel_after_promoter_preflight_is_atomic(self):
        candidate = dp.prepare(self.project, self.id, self.manifest)
        got = pp.propose(self.project, candidate)
        real = pp.propose
        after_cancel = []
        def interleave(*args, **kwargs):
            pp.decide(self.project, got["id"], "cancelled", "tester")
            after_cancel.append(self.stores())
            return real(*args, **kwargs)
        with patch.object(pp, "propose", side_effect=interleave):
            with self.assertRaises(dp.DraftPromoteError) as ctx:
                self.promote()
        self.assertEqual(ctx.exception.code, "packet_conflict")
        self.assertEqual(self.stores(), after_cancel[0])

    def test_registered_cli_apply_and_default(self):
        from campaign_tool.records import cli
        manifest = self.private(self.project / "manifest.json", json.dumps(self.manifest).encode())
        args = ["records", "draft-promote", "--project", str(self.project), "--id", self.id, "--reviewed-sources", str(manifest)]
        for apply, expected in ((False, "would_propose"), (True, "proposed")):
            out = io.StringIO()
            with patch("sys.argv", args + (["--apply"] if apply else [])), redirect_stdout(out):
                self.assertEqual(cli.main(), 0)
            self.assertEqual(json.loads(out.getvalue())["action"], expected)

    def test_cancelled_rejected_expired_approved_written_never_reopen(self):
        candidate = dp.prepare(self.project, self.id, self.manifest)
        got = pp.propose(self.project, candidate)
        pp.decide(self.project, got["id"], "cancelled", "tester")
        self.refused("packet_conflict")
        # Default API recovery remains operator work, not promoter behavior.
        pp.propose(self.project, candidate)
        self.refused("packet_conflict")  # conflicting revision even with identical bytes

    def test_locking_and_receipt_crash_recovery(self):
        with patch.object(dp, "_receipt", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(dp.DraftPromoteError) as ctx:
                self.promote()
        self.assertEqual(ctx.exception.code, "receipt_pending")
        journal = self.stores()[0]
        self.assertEqual(self.promote()["action"], "existing")
        self.assertEqual(self.stores()[0], journal)
        receipt = self.stores()[1]
        self.assertEqual(len(receipt.splitlines()), 1)
        self.private(self.project / dp.RECEIPTS, receipt[:-1])
        self.refused("receipt_invalid")

    def test_short_write_cap_and_busy(self):
        real_write = os.write
        def short(fd, data):
            return real_write(fd, data[:7])
        receipt = dp._receipt
        def short_receipt(*args):
            with patch.object(dp.os, "write", side_effect=short):
                return receipt(*args)
        with patch.object(dp, "_receipt", side_effect=short_receipt):
            self.promote()
        self.assertEqual(len(self.stores()[1].splitlines()), 1)
        with patch.object(dp, "MAX_RECEIPTS", 1):
            self.refused("receipt_invalid")
        with open(self.project / dp.LOCK, "rb") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.refused("promotion_busy")

    def test_two_promoters_and_queue_edit_serialization(self):
        entered, release = threading.Event(), threading.Event()
        real = pp.propose
        results, errors = [], []
        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test release timed out")
            return real(*args, **kwargs)
        def promote():
            try:
                results.append(self.promote())
            except Exception as error:
                errors.append(error)
        with patch.object(pp, "propose", side_effect=paused):
            thread = threading.Thread(target=promote)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.refused("promotion_busy")
                edited = threading.Event()
                def edit():
                    aq.edit(self.project, self.id, "editor", "New pending text.")
                    edited.set()
                editor = threading.Thread(target=edit)
                editor.start()
                self.assertFalse(edited.wait(.1))
            finally:
                release.set()
                thread.join(4)
            editor.join(4)
        self.assertFalse(errors)
        self.assertEqual(len(results), 1)
        self.assertEqual(self.packet()["packet"]["text"].encode(), BODY.encode())
        self.refused("queue_not_verified")

    def test_reviewed_sources_admission_is_bounded_and_private(self):
        path = self.project / "manifest.json"
        raw = json.dumps(self.manifest).encode()
        for bad in (b'{"schema":1,"schema":2}', b'{"nested":{"a":1,"a":2}}', b'{"x":NaN}', b'\xff',
                    raw.replace(b'"title": "Reviewed record summary"', b'"title":"\\ud800"'), b"x" * (dp.MAX_INPUT + 1)):
            self.private(path, bad)
            with self.assertRaises(dp.DraftPromoteError):
                dp.read_reviewed_sources(path)
        self.private(path, raw)
        self.assertEqual(dp.read_reviewed_sources(path), self.manifest)
        link = self.project / "link"
        link.symlink_to(path)
        with self.assertRaises(dp.DraftPromoteError):
            dp.read_reviewed_sources(link)
        link.unlink()
        os.link(path, link)
        with self.assertRaises(dp.DraftPromoteError):
            dp.read_reviewed_sources(path)
        link.unlink()
        fifo = self.project / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(dp.DraftPromoteError):
            dp.read_reviewed_sources(fifo)
        ancestor = self.project / "ancestor"
        ancestor.symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(dp.DraftPromoteError):
            dp.read_reviewed_sources(ancestor / path.name)

    def test_packet_requires_its_own_signature_and_switch(self):
        self.promote()
        row = self.packet()
        with self.assertRaises(pp.PacketError) as ctx:
            pp.approve(self.project, row["id"], self.copy_signature)
        self.assertEqual(ctx.exception.code, "approval_required")
        signature = self.signer.sign(pp.payload(self.project, row["id"]))
        changed = copy.deepcopy(row)
        changed["packet"]["draft_provenance"]["target"]["section"] = "different"
        with self.assertRaises(approval.ApprovalError):
            approval.verify(pp._payload(self.project, changed), signature)
        pp.approve(self.project, row["id"], signature)
        with self.assertRaises(pp.PacketError) as ctx:
            pp.write_change(self.project, row["id"])
        self.assertEqual(ctx.exception.code, "switch_off")
        self.assertFalse((self.project / pp.OUT).exists())


if __name__ == "__main__":
    unittest.main()
