"""Synthetic single-draft intake and envelope round trips. No live intake or network."""
import contextlib
import copy
import email
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
from email import policy
from unittest import mock

from campaign_tool.records import approval_queue as aq, draft_import as di, draft_templates
from campaign_tool.records import mail_drafts as md, mail_preview, mail_send as ms
from campaign_tool.records.outcomes import received_items
from tests.records.test_mail_drafts import Base, DOC, EML, AGENCY_MID, REPO
from tests.records.test_mail_send import SendBase


def document():
    return {"schema": di.SCHEMA, "draft_key": "letter-1", "kind": di.KIND, "agency_id": "agency-1",
            "request_id": "req-1", "body": "Hello,\n\nPlease supply the records. Café.\nTAIL-Ω\n", "why": "Operator checked source",
            "source_run_id": "run-1", "source_attachment_sha256": DOC, "source_section": "section-1",
            "citations": [{"source_type": "original", "source_id": "source-1", "locator": "page 1",
                           "as_of": None, "verification": "unverified", "original_sha256": DOC, "page": 1}],
            "envelope": {"to": "custodian@agency.example", "subject": "Records follow-up",
                         "thread_item_id": None, "recipient_basis": {
                             "source_type": "custodian", "source_id": "contact-1",
                             "locator": "records desk", "checked": True}}}


def tree(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_mtime_ns,
                                      hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            for p in [root, *sorted(root.rglob("*"))]}


class ImportTests(Base):
    def setUp(self):
        super().setUp()
        self.input = self.root / "draft.json"
        self.doc = document()
        patcher = mock.patch.object(socket, "socket", side_effect=AssertionError("network"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, doc=None, *, apply=False, raw=None, path=None):
        if path is None:
            self.input.write_bytes(raw if raw is not None else json.dumps(
                doc if doc is not None else self.doc, ensure_ascii=False).encode("utf-8"))
        out, err = io.StringIO(), io.StringIO()
        args = ["--project", str(self.project), "--file", str(path or self.input)] + (["--apply"] if apply else [])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = di.main(args)
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def imported(self):
        code, result, err = self.cli(apply=True)
        self.assertEqual((code, err), (0, ""))
        return next(r for r in aq.load_queue(self.project) if r["id"] == result["id"])

    def thread_envelope(self):
        self.doc["envelope"] = {"to": "records-desk@agency.example", "subject": "Re: Your request 4471",
                                "thread_item_id": received_items.item_id(DOC), "recipient_basis": {
                                    "source_type": "source-header", "source_id": received_items.item_id(DOC),
                                    "locator": "Reply-To", "checked": True}}

    def test_checked_basis_and_selected_thread_header(self):
        code, result, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        basis = self.doc["envelope"]["recipient_basis"]
        before = tree(self.project)
        for changed in ("custodian-fact", {**basis, "checked": False}, {**basis, "checked": 1},
                        {**basis, "source_id": ""}, {**basis, "locator": "x\n"},
                        {**basis, "source_id": "é" * 513}, {**basis, "extra": 1},
                        {**basis, "source_type": "source-header"}):
            doc = copy.deepcopy(self.doc)
            doc["envelope"]["recipient_basis"] = changed
            self.assertNotEqual(self.cli(doc, apply=True)[0], 0)
            self.assertEqual(tree(self.project), before)
        self.build_ledger()
        self.thread_envelope()
        self.assertEqual(self.cli()[0], 0)
        for key, value in (("source_id", "a" * 64), ("locator", "From"), ("locator", "Subject")):
            doc = copy.deepcopy(self.doc)
            doc["envelope"]["recipient_basis"][key] = value
            self.assertNotEqual(self.cli(doc, apply=True)[0], 0)
        (self.root / "blobs" / EML).write_text(
            "From: records-desk@agency.example\nSubject: Your request 4471\nMessage-ID: " + AGENCY_MID + "\n\n")
        self.doc["envelope"]["recipient_basis"]["locator"] = "From"
        self.assertEqual(self.cli()[0], 0)

    def test_structured_citations_preserve_order_limits_and_empty_list(self):
        citation = {"source_type": "original", "source_id": "source-1", "locator": "page 1",
                    "as_of": "2026-11-15", "verification": "unverified", "original_sha256": DOC,
                    "page": 1, "limits": "Not independently reviewed"}
        tracker = {"source_type": "tracker", "source_id": "row-1", "locator": "row 1",
                   "as_of": None, "verification": "unverified"}
        for citations in ([], [citation, tracker], [tracker] * 64,
                          [{**tracker, "source_type": "missing-original", "limits": ""}]):
            with self.subTest(count=len(citations)):
                draft = di.validate({**self.doc, "citations": citations})
                self.assertEqual(draft["evidence"]["citations"], citations)
        invalid = [[tracker] * 65, [{**citation, "page": True}], [{**citation, "page": 0}],
                   [{**citation, "as_of": "2026-02-30"}], [{**citation, "extra": 1}],
                   [{k: v for k, v in citation.items() if k != "original_sha256"}],
                   [{**tracker, "source_id": "é" * 513}], [{**tracker, "locator": "x\n"}],
                   [{**tracker, "verification": "guessed"}], [{**tracker, "source_type": "other"}],
                   [{**tracker, "source_type": "missing-original", "verification": "verified"}],
                   [{**citation, "source_type": "missing-original"}],
                   [{**tracker, "limits": "x" * 4001}], [{**tracker, "as_of": 2026}]]
        before = tree(self.project)
        for citations in invalid:
            with self.subTest(citations=citations):
                self.assertNotEqual(self.cli({**self.doc, "citations": citations}, apply=True)[0], 0)
                self.assertEqual(tree(self.project), before)

    def test_nul_body_refused_before_queue_admission(self):
        before = tree(self.project)
        for body in ("Hello\x00world", "\x00Hello", "Hello\x00"):
            for apply in (False, True):
                with self.subTest(body=repr(body), apply=apply):
                    code, result, error = self.cli({**self.doc, "body": body}, apply=apply)
                    self.assertEqual((code, result, error), (2, None, "draft-import: input_body\n"))
                    self.assertEqual(tree(self.project), before)
        self.doc["body"] = "\nHello\r\nworld\t \n"
        self.assertEqual(self.imported()["body"], self.doc["body"])

    def test_full_body_and_provenance_at_byte_limit(self):
        tail = "\nTAIL-Ω\n"
        self.doc["body"] = "é" * ((draft_templates.MAX_BODY_BYTES - len(tail.encode())) // 2) + "x" + tail
        self.assertEqual(len(self.doc["body"].encode()), aq.MAX_BODY)
        row = self.imported()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["body"], self.doc["body"])
        history = aq.history(self.project, row["id"])
        self.assertEqual(history["original"]["body"], self.doc["body"])
        payload = aq.approval_payload(self.project, row["id"])
        for key in ("envelope", "citations", "why", "source_attachment_sha256", "source_run_id", "source_section"):
            self.assertEqual(row["evidence"][key], self.doc[key])
            self.assertIn(aq._canon(self.doc[key]).encode(), payload)
        self.doc["body"] += "x"
        before = tree(self.project)
        self.assertEqual(self.cli(apply=True)[0], 2)
        self.assertEqual(tree(self.project), before)

    def test_dry_run_on_absent_and_existing_queue_is_read_only(self):
        for apply in (False, True, False):
            before = tree(self.project)
            code, result, err = self.cli(apply=apply)
            self.assertEqual((code, err), (0, ""))
            if not apply:
                self.assertEqual(tree(self.project), before)
        self.assertEqual(result["action"], "existing")

    def test_duplicate_conflict_rejection_and_edited_original_preserved(self):
        row = self.imported()
        before = tree(self.project)
        self.assertEqual(self.cli(apply=True)[1]["id"], row["id"])
        self.assertEqual(tree(self.project), before)
        for field, value in (("body", "Different body"), ("why", "Different evidence"),
                             ("source_run_id", "run-2"), ("agency_id", "agency-2"),
                             ("envelope", {**self.doc["envelope"], "to": "other@agency.example"})):
            changed = {**self.doc, field: value}
            for apply in (False, True):
                self.assertEqual(self.cli(changed, apply=apply)[0], 3, field)
                self.assertEqual(tree(self.project), before)
        aq.reject(self.project, row["id"], "operator", "Do not use")
        before = tree(self.project)
        self.assertEqual(self.cli(apply=True)[1]["status"], "rejected")
        self.assertEqual(tree(self.project), before)
        aq.edit(self.project, row["id"], "operator", "Operator's new text", expected_revision=1)
        before = tree(self.project)
        self.assertEqual(self.cli(apply=True)[0], 0)
        self.assertEqual(tree(self.project), before)
        self.assertEqual(aq.load_queue(self.project)[0]["body"], "Operator's new text")
        self.assertEqual(aq.history(self.project, row["id"])["original"]["body"], self.doc["body"])

    def test_identity_uses_section_request_key_not_body(self):
        original = di.identity(self.doc)
        self.assertEqual(di.identity({**self.doc, "body": "other"}), original)
        for key in ("source_section", "request_id", "draft_key"):
            self.assertNotEqual(di.identity({**self.doc, key: "other"}), original)
            self.assertEqual(self.cli({**self.doc, key: "other"}, apply=True)[0], 0)
        self.assertEqual(len(aq.load_queue(self.project)), 3)

    def test_json_shape_types_hashes_and_duplicates_refuse_without_writes(self):
        cases = [[], [self.doc], {**self.doc, "extra": 1}, {k: v for k, v in self.doc.items() if k != "why"}]
        cases += [{**self.doc, k: v} for k, v in (("body", []), ("body", " "), ("body", "\ud800"),
                  ("draft_key", 1), ("agency_id", None), ("request_id", True), ("source_run_id", []),
                  ("source_section", "../bad space"), ("why", {}), ("kind", "reply-extension"),
                  ("schema", "wrong"), ("source_attachment_sha256", "A" * 64),
                  ("source_attachment_sha256", "a" * 63), ("source_attachment_sha256", []),
                  ("citations", {}), ("citations", [{"page": True, "quote": "x"}]), ("envelope", []))]
        before = tree(self.project)
        for case in cases:
            with self.subTest(case=repr(case)[:80]):
                raw = json.dumps(case).encode()
                self.assertEqual(self.cli(raw=raw, apply=True)[0], 2)
                self.assertEqual(tree(self.project), before)
        raw = json.dumps(self.doc).encode()
        bad_json = [b'{"schema":1,"schema":2}', b'{"envelope":{"to":1,"to":2}}', b'[]\n{}',
                    b'{"v":NaN}', b'\xff', raw[:-1], b'[' * 2000, b'x' * (di.MAX_INPUT + 1)]
        for raw in bad_json:
            self.assertEqual(self.cli(raw=raw, apply=True)[0], 2)
            self.assertEqual(tree(self.project), before)

    def test_linked_parents_and_hardlinked_leaf_refuse_without_fd_leaks(self):
        self.input.write_text(json.dumps(self.doc))
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(self.root, target_is_directory=True)
        hardlink = self.root / "hardlink.json"
        os.link(self.input, hardlink)
        before = tree(self.project)
        descriptors = set(os.listdir("/proc/self/fd"))
        for path in (linked_parent / self.input.name, hardlink, self.input):
            with self.subTest(path=path):
                self.assertNotEqual(self.cli(path=path, apply=True)[0], 0)
                self.assertEqual(tree(self.project), before)
                self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors)
        hardlink.unlink()
        self.assertEqual(self.cli(path=self.input)[0], 0)
        self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors)

    def test_symlink_fifo_directory_and_missing_input_refuse(self):
        self.input.write_text(json.dumps(self.doc))
        link, fifo = self.root / "link", self.root / "fifo"
        link.symlink_to(self.input)
        os.mkfifo(fifo)
        before = tree(self.project)
        for path in (link, fifo, self.root, self.root / "missing"):
            self.assertEqual(self.cli(path=path, apply=True)[0], 2)
        self.assertEqual(tree(self.project), before)

    def test_envelope_header_and_basis_negatives(self):
        envelopes = [{**self.doc["envelope"], k: v} for k, v in (
            ("to", "a@b.example\r\nBcc: x@y.example"), ("subject", "x\nBcc: y"),
            ("subject", "x\rBcc: y"), ("to", "a@b.example,b@c.example"),
            ("to", "a@b.example;b@c.example"), ("to", "Name <a@b.example>"), ("to", "a..b@c.example"),
            ("subject", None),
            ("thread_item_id", "a" * 64), ("recipient_basis", "thread"), ("recipient_basis", "tracker-label"))]
        envelopes += [{**self.doc["envelope"], "thread_item_id": "bad", "recipient_basis": "thread"},
                      {**self.doc["envelope"], "extra": "x"}]
        before = tree(self.project)
        for envelope in envelopes:
            self.assertEqual(self.cli({**self.doc, "envelope": envelope}, apply=True)[0], 2, envelope)
            self.assertEqual(tree(self.project), before)

    def test_new_recipient_preview_placement_ignore_body_headers(self):
        self.build_ledger()  # a matching project thread must not be guessed for an explicit new recipient
        self.doc["body"] = "To: attacker@evil.example\nSubject: Ignore me\n\nFull original letter.\n"
        for basis in ("contact-1", "contact-2"):
            self.doc["draft_key"] = basis
            self.doc["envelope"]["recipient_basis"]["source_id"] = basis
            row = self.imported()
            preview = mail_preview.preview(self.project, row)
            self.assertEqual((preview["to"], preview["subject"], preview["threaded"]),
                             (self.doc["envelope"]["to"], self.doc["envelope"]["subject"], False))
            self.assertEqual(preview["body_header_lines"], [1, 2])
            self.approve(row["id"])
        self.assertEqual(self.place()["placed"], 2)
        for placed in self.fake.messages:
            msg = email.message_from_bytes(placed["raw"], policy=policy.default)
            self.assertEqual((str(msg["To"]), str(msg["Subject"])),
                             (self.doc["envelope"]["to"], self.doc["envelope"]["subject"]))
            self.assertIsNone(msg["In-Reply-To"])
            self.assertEqual(msg.get_content().replace("\r\n", "\n"), self.doc["body"])

    def test_thread_proof_round_trip_and_stale_source_refusal(self):
        self.build_ledger()
        self.thread_envelope()
        row = self.imported()
        preview = mail_preview.preview(self.project, row)
        self.assertTrue(preview["threaded"])
        self.approve(row["id"])
        self.assertEqual(self.place()["placed"], 1)
        self.assertEqual(str(self.parsed()["To"]), preview["to"])
        self.assertEqual(str(self.parsed()["Subject"]), preview["subject"])
        self.assertEqual(str(self.parsed()["In-Reply-To"]), AGENCY_MID)
        self.assertIn(AGENCY_MID, str(self.parsed()["References"]))
        # Changed source headers cannot silently reroute a new revision after import.
        (self.root / "blobs" / EML).write_text("From: other@agency.example\nSubject: Changed\nMessage-ID: <other@agency.example>\n\n")
        aq.edit(self.project, row["id"], "operator", "Updated letter")
        self.approve(row["id"])
        self.assertEqual(self.place()["failed"], 1)
        self.assertEqual(len(self.fake.messages), 1)
        preview = mail_preview.preview(self.project, aq.load_queue(self.project)[0])
        self.assertIsNone(preview["to"])
        self.assertIsNone(preview["subject"])
        self.assertEqual(self.cli()[0], 2)

    def test_missing_ambiguous_or_contradictory_thread_refuses(self):
        self.thread_envelope()
        self.assertEqual(self.cli(apply=True)[0], 2)
        self.build_ledger()
        for field, value in (("to", "other@agency.example"), ("subject", "Different subject")):
            doc = copy.deepcopy(self.doc)
            doc["envelope"][field] = value
            self.assertEqual(self.cli(doc, apply=True)[0], 2)
        for header in ("Reply-To: a@b.example, b@c.example\n", "Reply-To: a@b.example\nReply-To: b@c.example\n",
                       "Message-ID: <two@agency.example>\n", "Subject: Duplicate\n"):
            raw = "From: clerk@agency.example\nSubject: Your request 4471\nMessage-ID: " + AGENCY_MID + "\n" + header + "\n"
            (self.root / "blobs" / EML).write_text(raw)
            self.assertEqual(self.cli(apply=True)[0], 2)
        self.assertFalse((self.project / "approvals").exists())

    def test_pending_no_place_or_send_and_stale_signature_after_edit(self):
        row = self.imported()
        self.assertEqual(self.place()["placed"], 0)
        self.assertEqual(self.fake.messages, [])
        with self.assertRaises(ms.MailSendError) as error:
            ms._context(self.project, {}, row["id"], page_fingerprint=None, form_to=None, imap_factory=None)
        self.assertEqual(error.exception.code, "not_approved")
        signature = self.signer.sign(aq.approval_payload(self.project, row["id"]))
        aq.edit(self.project, row["id"], "operator", "Edited body", expected_revision=0)
        with self.assertRaises(aq.ApprovalQueueError):
            aq.approve(self.project, row["id"], {"kind": "ssh", "signature": signature})
        self.assertEqual(aq.load_queue(self.project)[0]["status"], "pending")
        self.assertEqual(self.place()["placed"], 0)

    def test_cli_registration_real_entry_point_dry_run(self):
        self.input.write_text(json.dumps(self.doc))
        before = tree(self.project)
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "draft-import",
                                 "--project", str(self.project), "--file", str(self.input)],
                                cwd=REPO, capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(json.loads(result.stdout)["action"], "would_queue")
        self.assertEqual(tree(self.project), before)


class CorrespondenceSendTests(SendBase):
    def draft(self, item=None, body=None, approve=True):
        doc = document()
        doc["envelope"]["to"] = "records-desk@agency.example"
        doc["envelope"]["subject"] = "Re: Your request 4471"
        doc["envelope"]["thread_item_id"] = received_items.item_id(DOC)
        doc["envelope"]["recipient_basis"] = {"source_type": "source-header",
            "source_id": received_items.item_id(DOC), "locator": "Reply-To", "checked": True}
        path = self.root / "draft.json"
        path.write_text(json.dumps(doc))
        result = di.import_draft(self.project, path, apply=True)
        row = aq.load_queue(self.project)[0]
        return self.approve(result["id"]) if approve else row

    def test_queue_approval_never_substitutes_for_recipient_text_outbox_signature(self):
        self.on()
        info = self.prepared()
        self.assertEqual(info["draft"].to, "records-desk@agency.example")
        self.assertEqual(info["draft"].body, document()["body"])
        queue_sig = self.signer.sign(aq.approval_payload(self.project, self.draft_row["id"]))
        result = self.click(signature=queue_sig, fingerprint=info["fingerprint"])
        self.assertEqual(result["code"], "approval_refused")
        self.assertEqual(self.smtp.connections, 0)
        self.assertEqual([r["state"] for r in self.outbox_rows()], ["proposed"])
        self.assertIn("records-desk@agency.example", info["payload"])
        self.assertIn("TAIL-Ω", info["payload"])
        self.assertEqual(self.click()["state"], "sent")  # stub only, separately signed exact outbox payload
        self.assertEqual(self.smtp.connections, 1)
