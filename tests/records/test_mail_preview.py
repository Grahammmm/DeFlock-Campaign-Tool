"""Synthetic, offline inspection of the headers placement actually builds."""
import email
from email import policy
from unittest import mock

from campaign_tool.records import mail_preview, mail_poll
from tests.records.test_mail_drafts import Base, DOC, USER, md, received_items


class PreviewTests(Base):
    def row(self, **overrides):
        return {"id": "a" * 64, "revision": 1, "request_id": "req-1",
                "item_id": received_items.item_id(DOC), "kind": "reply-extension",
                "body": "Please supply the records.\n", "evidence": {}, **overrides}

    def test_headers_match_message_reply_to_and_from_fallback(self):
        for headers, address in ((None, "records-desk@agency.example"),
                                 ("From: clerk@example.test\r\nSubject: RE: Records\r\n"
                                  "Message-ID: <x@example.test>\r\n\r\n", "clerk@example.test")):
            with self.subTest(address=address):
                self.tearDown_project()
                self.build_ledger(eml_headers=headers)
                row = self.row()
                result = mail_preview.preview(self.project, row, self.config_path)
                thread, _ = md.find_thread(self.project, row["item_id"])
                message = email.message_from_bytes(md.build_message(row, thread, USER), policy=policy.default)
                self.assertEqual(result["to"], address)
                self.assertTrue(result["threaded"])
                self.assertTrue(result["subject"].lower().startswith("re:"))
                for key in ("to", "from", "subject"):
                    self.assertEqual(result[key], message[key])

    def test_missing_or_stale_thread_is_a_value_and_does_not_cache(self):
        row = self.row()
        result = mail_preview.preview(self.project, row)
        self.assertIsNone(result["to"])
        self.assertFalse(result["threaded"])
        self.assertIn("thread_not_found", result["to_note"])
        self.assertEqual(result["subject"], "Records request req-1 follow-up")
        for kwargs, reason in (({"store_file": False}, "eml_missing"),
                               ({"eml_headers": "From: a@example.test\r\n\r\n"}, "message_id_missing")):
            self.tearDown_project()
            self.build_ledger(**kwargs)
            self.assertIn(reason, mail_preview.preview(self.project, row)["to_note"])
        self.tearDown_project()
        self.build_ledger()
        self.assertTrue(mail_preview.preview(self.project, row)["threaded"])

    def test_config_missing_invalid_or_valid(self):
        result = mail_preview.preview(self.project, self.row())
        self.assertIsNone(result["from"])
        self.assertEqual(result["from_note"], "set by the Drafts mailbox config")
        for path, code in ((self.root / "absent.json", "config_unavailable"),
                           (self.write_config(self.pw.parent, "inbox.json", folder="INBOX"), "folder_required")):
            result = mail_preview.preview(self.project, self.row(), path)
            self.assertIsNone(result["from"])
            self.assertEqual(result["from_note"], code)
        self.assertEqual(mail_preview.preview(self.project, self.row(), self.config_path)["from"], USER)

    def test_attachments_are_metadata_not_opened_and_malformed_lists_are_empty(self):
        attachments = [{"name": "logs.pdf", "sha256": "b" * 64}, {"name": "policy.txt"}]
        result = mail_preview.preview(self.project, self.row(evidence={"attachments": attachments}))
        self.assertEqual(result["attachments"], attachments)
        result["attachments"][0]["name"] = "changed"
        self.assertEqual(attachments[0]["name"], "logs.pdf")
        for bad in (None, "logs.pdf", {}, ["logs.pdf"], [{}], [{"name": None}], [{"name": "x", "sha256": 1}]):
            with self.subTest(bad=bad):
                self.assertEqual(mail_preview.preview(self.project, self.row(evidence={"attachments": bad}))["attachments"], [])
        self.assertEqual(mail_preview.preview(self.project, self.row())["attachments"], [])

    def test_residue_and_one_based_header_lines_bounded_at_fifteen(self):
        body = "[EDIT ME: x]\nTo: agency\nSubject: Records\nFrom: me\n" + "text\n" * 10 + "To: last\nTo: outside\n"
        result = mail_preview.preview(self.project, self.row(body=body))
        self.assertEqual(result["residue"], ["edit_placeholder"])
        self.assertEqual(result["body_header_lines"], [2, 3, 4, 15])
        clean = mail_preview.preview(self.project, self.row())
        self.assertEqual((clean["residue"], clean["body_header_lines"]), ([], []))

    def test_playbook_uses_same_provenance_as_placement(self):
        self.build_ledger()
        row = self.row(kind="followup-playbook", item_id="route:synthetic", evidence={
            "vehicle": "reply_in_thread", "thread_item_id": received_items.item_id(DOC)})
        self.assertTrue(mail_preview.preview(self.project, row)["threaded"])
        for evidence, reason in (({}, "thread_provenance_invalid"),
                                 ({"vehicle": "reply_in_thread", "thread_item_id": "a" * 64}, "thread_provenance_unresolved"),
                                 ({"vehicle": "new_request"}, "new_request")):
            result = mail_preview.preview(self.project, {**row, "evidence": evidence})
            self.assertFalse(result["threaded"])
            self.assertIsNone(result["to"])
            self.assertIn(reason, result["to_note"])

    def test_repeated_preview_never_connects_reads_secret_or_changes_files(self):
        self.build_ledger()
        row = self.draft(approve=False)
        # A quiescent fixture avoids SQLite's transient WAL shared-memory bookkeeping.
        with md.store.ledger(self.ledger) as con:
            con.execute("PRAGMA journal_mode=DELETE")
        def snapshot():
            return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                    for p in self.root.rglob("*") if p.is_file()}
        before = snapshot()
        with mock.patch("imaplib.IMAP4_SSL", side_effect=AssertionError("IMAP")), \
                mock.patch("smtplib.SMTP", side_effect=AssertionError("SMTP")), \
                mock.patch.object(mail_poll, "_secret", side_effect=AssertionError("secret")):
            first = mail_preview.preview(self.project, row, self.config_path)
            self.assertEqual(mail_preview.preview(self.project, row, self.config_path), first)
        self.assertEqual(snapshot(), before)
