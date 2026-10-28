"""Synthetic-only mailbox receipt delta regressions."""
from email.message import EmailMessage
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from campaign_tool.records.intake import folder, mail_delta


def sha(data):
    return hashlib.sha256(data).hexdigest()


class MailDeltaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "export"
        self.root.mkdir(mode=0o700)
        self.out = self.base / "intake"
        self.out.mkdir(mode=0o700)
        (self.out / "blobs").mkdir(mode=0o700)
        self.db_path = self.out / "intake.sqlite"
        self.db = sqlite3.connect(self.db_path)
        self.addCleanup(self.db.close)
        self.db.executescript(folder.SCHEMA)
        self.db_path.chmod(0o600)
        self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",
                        ("baseline", folder.now(), folder.now(), "inventory", "complete", "{}"))
        self.db.execute("INSERT INTO meta VALUES('inventory_run','baseline')")
        prior = b"synthetic existing catalog record"
        self.baseline_sha = sha(prior)
        self._existing_doc(self.baseline_sha, prior, "txt")
        self.db.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",
                        ("baseline-occurrence", self.baseline_sha, "synthetic-prior.txt",
                         str(self.root), None, "null", "original", "{}", "before", "before"))
        self.db.execute("INSERT INTO seen VALUES(?,?,?)",
                        ("baseline", "baseline-occurrence", self.baseline_sha))
        self.db.commit()
        message = EmailMessage()
        message["From"] = "synthetic@example.invalid"
        message["To"] = "recipient@example.invalid"
        message["Subject"] = "Synthetic fixture"
        message.set_content("Synthetic body")
        self.attachment = b"%PDF-1.4\nsynthetic attachment bytes\n"
        message.add_attachment(self.attachment, maintype="application", subtype="pdf",
                               filename="synthetic.pdf")
        self.eml = message.as_bytes()
        (self.root / "mail").mkdir()
        (self.root / "mail" / "message.eml").write_bytes(self.eml)
        (self.root / "mail" / "attachment.pdf").write_bytes(self.attachment)
        self._existing_doc(sha(self.attachment), self.attachment, "pdf")
        self.db.commit()
        self.receipt = {
            "account_id": "synthetic-account", "folder": "INBOX",
            "uidvalidity": 9, "uid": 64, "complete": True,
            "bytes": len(self.eml), "headers": {"subject": "ignored raw narrative"},
            "internaldate": "2026-01-01T00:00:00Z",
            "original_eml": {"path": "mail/message.eml", "bytes": len(self.eml),
                             "sha256": sha(self.eml)},
            "attachments": [{"part": "2", "path": "mail/attachment.pdf",
                             "bytes": len(self.attachment), "sha256": sha(self.attachment),
                             "filename": "synthetic.pdf",
                             "original_filename": "synthetic.pdf",
                             "content_type": "application/pdf"}],
        }
        self.receipt_path = self.base / "receipt.json"
        self._write_receipt()

    def _existing_doc(self, digest, data, form):
        blob = self.out / "blobs" / digest
        blob.write_bytes(data)
        blob.chmod(0o400)
        self.db.execute("INSERT INTO docs(sha,bytes,format,stage,first_seen,review_status,version,digest) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (digest, len(data), form, "complete", "before", "not_reviewed",
                         folder.VERSION, '{"synthetic":"prior"}'))
        self.db.execute("INSERT INTO preservations VALUES(?,?,?,?)",
                        (digest, str(blob), "before", len(data)))
        self.db.execute("INSERT INTO units VALUES(?,?,?,?,?,?)",
                        (digest, 1, "synthetic_unit", "{}", "existing unit", "{}"))

    def _write_receipt(self):
        self.receipt_path.write_text(json.dumps(self.receipt))

    def _import(self):
        return mail_delta.import_receipt(self.receipt_path, self.root, self.out)

    def _active(self):
        return self.db.execute("SELECT value FROM meta WHERE key='inventory_run'").fetchone()[0]

    def test_one_eml_attachment_replay_and_prior_catalog_preserved(self):
        before = self.db.execute("SELECT stage,version,digest FROM docs WHERE sha=?",
                                 (sha(self.attachment),)).fetchone()
        result = self._import()
        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["added_occurrences"], 2)
        self.assertEqual(result["added_edges"], 1)
        self.assertNotEqual(result["run_id"], "baseline")
        self.assertEqual(self.db.execute("SELECT count(*) FROM seen WHERE run=?",
                                         (self._active(),)).fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT count(*) FROM seen WHERE run='baseline'").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM docs").fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT stage,version,digest FROM docs WHERE sha=?",
                                         (sha(self.attachment),)).fetchone(), before)
        self.assertEqual(self.db.execute("SELECT count(*) FROM units WHERE sha=?",
                                         (sha(self.attachment),)).fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM edges WHERE parent=? AND child=?",
                                         (sha(self.eml), sha(self.attachment))).fetchone()[0], 1)
        record = self.db.execute("SELECT path,parent,locator FROM occurrences WHERE sha=? "
                                 "AND parent IS NOT NULL", (sha(self.attachment),)).fetchone()
        self.assertEqual(record[0], "mail/attachment.pdf")
        self.assertEqual(record[1], sha(self.eml))
        self.assertEqual(json.loads(record[2])["mail_receipt_part"], "2")
        replay = self._import()
        self.assertEqual(replay["status"], "replay")
        self.assertEqual(replay["run_id"], result["run_id"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM occurrences").fetchone()[0], 3)

    def test_distinct_uid_same_bytes_adds_provenance_not_docs(self):
        self._import()
        self.receipt["uid"] = 65
        self._write_receipt()
        result = self._import()
        self.assertEqual(result["added_occurrences"], 2)
        self.assertEqual(result["added_edges"], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM docs").fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT count(*) FROM seen WHERE run=?",
                                         (self._active(),)).fetchone()[0], 5)

    def test_malformed_hash_escape_and_symlink_rejected_without_mutation(self):
        variants = [
            {"complete": False},
            {"original_eml": {**self.receipt["original_eml"], "sha256": "bad"}},
            {"original_eml": {**self.receipt["original_eml"], "sha256": "0" * 64}},
            {"original_eml": {**self.receipt["original_eml"], "path": "../outside.eml"}},
        ]
        for patch in variants:
            with self.subTest(patch=patch):
                self.receipt_path.write_text(json.dumps(dict(self.receipt, **patch)))
                with self.assertRaises(mail_delta.Rejected):
                    self._import()
                self.assertEqual(self._active(), "baseline")
                self.assertEqual(self.db.execute("SELECT count(*) FROM docs").fetchone()[0], 2)
        link = self.root / "mail" / "linked.eml"
        link.symlink_to(self.root / "mail" / "message.eml")
        self.receipt["original_eml"]["path"] = "mail/linked.eml"
        self._write_receipt()
        with self.assertRaises(mail_delta.Rejected):
            self._import()
        self.assertEqual(self._active(), "baseline")

    def test_path_only_exclusion_blocks_import(self):
        self.db.execute("INSERT INTO scope_exclusions VALUES(?,?,?,?)",
                        (str(self.root / "mail" / "message.eml"), "", "synthetic", "now"))
        self.db.commit()
        with self.assertRaisesRegex(mail_delta.Rejected, "quarantined_path"):
            self._import()
        self.assertEqual(self._active(), "baseline")

    def test_relative_exclusion_blocks_absolute_receipt_path(self):
        self.receipt["original_eml"]["path"] = str(self.root / "mail" / "message.eml")
        self.db.execute("INSERT INTO scope_exclusions VALUES(?,?,?,?)",
                        ("mail/message.eml", "", "synthetic", "now"))
        self.db.commit()
        self._write_receipt()
        with self.assertRaisesRegex(mail_delta.Rejected, "quarantined_path"):
            self._import()
        self.assertEqual(self._active(), "baseline")

    def test_sanitized_exporter_metadata_and_absolute_paths(self):
        self.receipt["original_eml"]["path"] = str(self.root / "mail" / "message.eml")
        self.receipt["attachments"][0]["path"] = str(self.root / "mail" / "attachment.pdf")
        self.receipt["attachments"][0]["part"] = "1.2"
        self._write_receipt()
        result = self._import()
        self.assertEqual(result["added_occurrences"], 2)
        self.assertEqual(self.db.execute("SELECT path FROM occurrences WHERE parent IS NOT NULL").fetchone()[0],
                         str(self.root / "mail" / "attachment.pdf"))

    def test_partial_active_run_blocks_promotion(self):
        self.db.execute("UPDATE runs SET status='partial' WHERE id='baseline'")
        self.db.commit()
        with self.assertRaisesRegex(mail_delta.Rejected, "incomplete_active_run"):
            self._import()
        self.assertEqual(self._active(), "baseline")

    def test_ambiguous_unnamed_inline_text_fails_closed(self):
        for subtype in ("calendar", "csv"):
            with self.subTest(subtype=subtype):
                message = EmailMessage()
                message["From"] = "synthetic@example.invalid"
                message.set_content("Synthetic body")
                message.add_alternative("synthetic,structured,text", subtype=subtype)
                message.add_attachment(self.attachment, maintype="application",
                                       subtype="pdf", filename="synthetic.pdf")
                raw = message.as_bytes()
                (self.root / "mail" / "message.eml").write_bytes(raw)
                self.receipt["original_eml"].update(bytes=len(raw), sha256=sha(raw))
                self.receipt["bytes"] = len(raw)
                self._write_receipt()
                with self.assertRaisesRegex(mail_delta.Rejected, "ambiguous_inline_text_part"):
                    self._import()
                self.assertEqual(self._active(), "baseline")

    def test_extra_inline_plain_or_html_under_mixed_fails_closed(self):
        for subtype in ("plain", "html"):
            with self.subTest(subtype=subtype):
                message = EmailMessage()
                message["From"] = "synthetic@example.invalid"
                message.set_content("Primary body")
                message.make_mixed()
                extra = EmailMessage()
                extra.set_content("Extra inline record", subtype=subtype)
                message.attach(extra)
                message.add_attachment(self.attachment, maintype="application",
                                       subtype="pdf", filename="synthetic.pdf")
                raw = message.as_bytes()
                (self.root / "mail" / "message.eml").write_bytes(raw)
                self.receipt["original_eml"].update(bytes=len(raw), sha256=sha(raw))
                self.receipt["bytes"] = len(raw)
                self._write_receipt()
                with self.assertRaisesRegex(mail_delta.Rejected, "ambiguous_inline_body_part"):
                    self._import()
                self.assertEqual(self._active(), "baseline")

    def test_alternative_body_with_attachment_is_allowed(self):
        message = EmailMessage()
        message["From"] = "synthetic@example.invalid"
        message.set_content("Primary body")
        message.add_alternative("<p>Primary body</p>", subtype="html")
        message.add_attachment(self.attachment, maintype="application",
                               subtype="pdf", filename="synthetic.pdf")
        raw = message.as_bytes()
        (self.root / "mail" / "message.eml").write_bytes(raw)
        self.receipt["original_eml"].update(bytes=len(raw), sha256=sha(raw))
        self.receipt["bytes"] = len(raw)
        self._write_receipt()
        result = self._import()
        self.assertEqual(result["added_occurrences"], 2)
        self.assertEqual(result["added_edges"], 1)

    def test_attachment_metadata_must_match_mime(self):
        for field, value, code in (("filename", "wrong.csv", "export_filename_mismatch"),
                                   ("original_filename", "wrong.csv", "mime_filename_mismatch"),
                                   ("content_type", "text/csv", "mime_content_type_mismatch")):
            with self.subTest(field=field):
                original = self.receipt["attachments"][0][field]
                self.receipt["attachments"][0][field] = value
                self._write_receipt()
                with self.assertRaisesRegex(mail_delta.Rejected, code):
                    self._import()
                self.assertEqual(self._active(), "baseline")
                self.receipt["attachments"][0][field] = original

    def test_safe_filename_contract_and_exporter_name_binding(self):
        self.assertEqual(mail_delta.safe_filename("../folder\\record report?.pdf"),
                         "record_report_.pdf")
        self.assertEqual(mail_delta.safe_filename("..."), "attachment.bin")
        self.assertEqual(mail_delta.safe_filename(None), "attachment.bin")
        self.assertEqual(mail_delta.safe_filename("a" * 120 + ".pdf"), "a" * 100)
        original_name = "folder\\record report?.pdf"
        export_name = mail_delta.safe_filename(original_name)
        message = EmailMessage()
        message["From"] = "synthetic@example.invalid"
        message.set_content("Synthetic body")
        message.add_attachment(self.attachment, maintype="application",
                               subtype="pdf", filename=original_name)
        raw = message.as_bytes()
        (self.root / "mail" / "message.eml").write_bytes(raw)
        (self.root / "mail" / export_name).write_bytes(self.attachment)
        self.receipt["original_eml"].update(bytes=len(raw), sha256=sha(raw))
        self.receipt["bytes"] = len(raw)
        self.receipt["attachments"][0].update(path="mail/" + export_name,
                                              filename=export_name,
                                              original_filename=original_name)
        self._write_receipt()
        result = self._import()
        self.assertEqual(result["added_occurrences"], 2)
        self.assertEqual(result["added_edges"], 1)
        self.assertEqual(self.db.execute("SELECT format FROM docs WHERE sha=?",
                                         (sha(self.attachment),)).fetchone()[0], "pdf")

    def test_unicode_original_filename_uses_exporter_sanitization(self):
        original_name = "R\u00e9sum\u00e9 2026?.pdf"
        export_name = mail_delta.safe_filename(original_name)
        self.assertEqual(export_name, "R_sum__2026_.pdf")
        message = EmailMessage()
        message["From"] = "synthetic@example.invalid"
        message.set_content("Synthetic body")
        message.add_attachment(self.attachment, maintype="application",
                               subtype="pdf", filename=original_name)
        raw = message.as_bytes()
        (self.root / "mail" / "message.eml").write_bytes(raw)
        (self.root / "mail" / export_name).write_bytes(self.attachment)
        self.receipt["original_eml"].update(bytes=len(raw), sha256=sha(raw))
        self.receipt["bytes"] = len(raw)
        self.receipt["attachments"][0].update(path="mail/" + export_name,
                                              filename=export_name,
                                              original_filename=original_name)
        self._write_receipt()
        result = self._import()
        self.assertEqual(result["added_occurrences"], 2)
        receipt = json.loads(self.db.execute(
            "SELECT receipt FROM occurrences WHERE parent=?", (sha(raw),)).fetchone()[0])
        self.assertEqual(receipt["original_filename"], original_name)
        self.assertEqual(receipt["filename"], export_name)

    def test_identical_bytes_at_two_parts_keep_distinct_provenance(self):
        message = EmailMessage()
        message["From"] = "synthetic@example.invalid"
        message.set_content("Synthetic body")
        names = ("copy-one.pdf", "copy-two.pdf")
        for name in names:
            message.add_attachment(self.attachment, maintype="application",
                                   subtype="pdf", filename=name)
            (self.root / "mail" / name).write_bytes(self.attachment)
        raw = message.as_bytes()
        (self.root / "mail" / "message.eml").write_bytes(raw)
        self.receipt["original_eml"].update(bytes=len(raw), sha256=sha(raw))
        self.receipt["bytes"] = len(raw)
        self.receipt["attachments"] = [
            {"part": part, "path": "mail/" + name, "bytes": len(self.attachment),
             "sha256": sha(self.attachment), "filename": name,
             "original_filename": name, "content_type": "application/pdf"}
            for part, name in enumerate(names, 2)
        ]
        self._write_receipt()
        result = self._import()
        self.assertEqual(result["added_occurrences"], 3)
        self.assertEqual(result["added_edges"], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM docs").fetchone()[0], 3)
        rows = list(self.db.execute(
            "SELECT oid,path,locator FROM occurrences WHERE parent=? ORDER BY path",
            (sha(raw),)))
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertEqual({json.loads(row[2])["mime"] for row in rows}, {"1.2", "1.3"})
        self.assertEqual({json.loads(row[2])["mail_receipt_part"] for row in rows}, {2, 3})
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM preservations WHERE sha=?",
            (sha(self.attachment),)).fetchone()[0], 1)
        self.assertEqual(self._import()["status"], "replay")
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM occurrences WHERE parent=?", (sha(raw),)).fetchone()[0], 2)

    def test_exact_source_sha_and_size_mismatches_reject_without_promotion(self):
        cases = (("eml_sha", "source_hash_mismatch"),
                 ("eml_size", "source_size_mismatch"),
                 ("attachment_sha", "source_hash_mismatch"),
                 ("attachment_size", "source_size_mismatch"))
        for case, code in cases:
            with self.subTest(case=case):
                original = json.loads(json.dumps(self.receipt))
                if case == "eml_sha":
                    self.receipt["original_eml"]["sha256"] = "0" * 64
                elif case == "eml_size":
                    self.receipt["original_eml"]["bytes"] += 1
                    self.receipt["bytes"] += 1
                elif case == "attachment_sha":
                    self.receipt["attachments"][0]["sha256"] = "0" * 64
                else:
                    self.receipt["attachments"][0]["bytes"] += 1
                self._write_receipt()
                with self.assertRaisesRegex(mail_delta.Rejected, code):
                    self._import()
                self.assertEqual(self._active(), "baseline")
                self.assertEqual(self.db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
                self.receipt = original

    def test_owner_only_boundary_and_rejected_cli_redacts_narrative(self):
        self.out.chmod(0o755)
        with self.assertRaisesRegex(mail_delta.Rejected, "output_not_owner_only"):
            self._import()
        self.out.chmod(0o700)
        marker = "SYNTHETIC-PRIVATE-NARRATIVE-MARKER"
        self.receipt["headers"]["subject"] = marker
        self.receipt["original_eml"]["path"] = "../" + marker + ".eml"
        self._write_receipt()
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool.records.intake.mail_delta",
             "--receipt", str(self.receipt_path), "--mail-root", str(self.root),
             "--output", str(self.out)], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source_path_escape", result.stderr)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertEqual(self._active(), "baseline")

    def test_conflicting_identity_fails_closed(self):
        self._import()
        run = self._active()
        changed = self.eml + b"\nsynthetic extra byte"
        (self.root / "mail" / "changed.eml").write_bytes(changed)
        self.receipt["original_eml"] = {"path": "mail/changed.eml",
                                        "bytes": len(changed), "sha256": sha(changed)}
        self.receipt["bytes"] = len(changed)
        self._write_receipt()
        with self.assertRaises(mail_delta.Rejected):
            self._import()
        self.assertEqual(self._active(), run)
        self.assertEqual(self.db.execute("SELECT count(*) FROM docs").fetchone()[0], 3)

    def test_no_attachment_receipt(self):
        message = EmailMessage()
        message["From"] = "synthetic@example.invalid"
        message.set_content("Synthetic body only")
        raw = message.as_bytes()
        (self.root / "mail" / "message.eml").write_bytes(raw)
        self.receipt["original_eml"].update(bytes=len(raw), sha256=sha(raw))
        self.receipt["bytes"] = len(raw)
        self.receipt["attachments"] = []
        self._write_receipt()
        result = self._import()
        self.assertEqual(result["added_occurrences"], 1)
        self.assertEqual(result["added_edges"], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM edges").fetchone()[0], 0)

    def test_complete_receipt_cannot_omit_mime_attachment(self):
        self.receipt["attachments"] = []
        self._write_receipt()
        with self.assertRaisesRegex(mail_delta.Rejected, "unlisted_mime_attachment"):
            self._import()
        self.assertEqual(self._active(), "baseline")

    def test_message_body_cannot_be_attachment(self):
        body = b"Synthetic body\n"
        (self.root / "mail" / "body.txt").write_bytes(body)
        self.receipt["attachments"] = [{"part": "1", "path": "mail/body.txt",
                                        "bytes": len(body), "sha256": sha(body),
                                        "filename": "body.txt"}]
        self._write_receipt()
        with self.assertRaisesRegex(mail_delta.Rejected, "missing_or_mismatched_mime_part"):
            self._import()
        self.assertEqual(self._active(), "baseline")

    def test_nested_full_walk_integer_parts_and_wrong_index(self):
        message = EmailMessage()
        message["From"] = "synthetic@example.invalid"
        message.make_mixed()
        related = EmailMessage()
        related.make_related()
        alternative = EmailMessage()
        alternative.make_alternative()
        body = EmailMessage()
        body.make_alternative()
        plain = EmailMessage()
        plain.set_content("Synthetic body")
        body.attach(plain)
        alternative.attach(body)
        related.attach(alternative)
        entries = []
        for n in range(6):
            data = b"\x89PNG\r\n\x1a\nsynthetic-" + bytes([n])
            name = "inline-" + str(n) + ".png"
            image = EmailMessage()
            image.set_content(data, maintype="image", subtype="png")
            image.add_header("Content-Disposition", "inline", filename=name)
            related.attach(image)
            entries.append((data, name, "image/png"))
        message.attach(related)
        for n in range(2):
            data = b"%PDF-1.4\nsynthetic-" + bytes([n])
            name = "record-" + str(n) + ".pdf"
            pdf = EmailMessage()
            pdf.set_content(data, maintype="application", subtype="pdf")
            pdf.add_header("Content-Disposition", "attachment", filename=name)
            message.attach(pdf)
            entries.append((data, name, "application/pdf"))
        walk = list(enumerate(message.walk()))
        self.assertEqual([n for n, part in walk if part.get_content_type() in
                          {"image/png", "application/pdf"}], list(range(5, 13)))
        raw = message.as_bytes()
        (self.root / "mail" / "message.eml").write_bytes(raw)
        self.receipt["original_eml"].update(bytes=len(raw), sha256=sha(raw))
        self.receipt["bytes"] = len(raw)
        attachments = []
        for part, (data, name, content_type) in enumerate(entries, 5):
            (self.root / "mail" / name).write_bytes(data)
            attachments.append({"part": part, "path": "mail/" + name,
                                "bytes": len(data), "sha256": sha(data),
                                "filename": name, "original_filename": name,
                                "content_type": content_type})
        self.receipt["attachments"] = attachments
        self._write_receipt()
        result = self._import()
        self.assertEqual(result["added_occurrences"], 9)
        self.assertEqual(result["added_edges"], 8)
        locators = {json.loads(row[0])["mime"]: json.loads(row[0])["mail_receipt_part"]
                    for row in self.db.execute("SELECT locator FROM occurrences WHERE parent=?",
                                               (sha(raw),))}
        self.assertEqual(locators, {**{"1.1." + str(n + 2): n + 5 for n in range(6)},
                                    "1.2": 11, "1.3": 12})
        run = self._active()
        self.receipt["uid"] = 65
        self.receipt["attachments"][0]["part"] = 99
        self._write_receipt()
        with self.assertRaisesRegex(mail_delta.Rejected, "missing_or_mismatched_mime_part"):
            self._import()
        self.assertEqual(self._active(), run)

    def test_wrong_part_cannot_bind_by_hash(self):
        self.receipt["attachments"][0]["part"] = "99"
        self._write_receipt()
        with self.assertRaisesRegex(mail_delta.Rejected, "missing_or_mismatched_mime_part"):
            self._import()
        self.assertEqual(self._active(), "baseline")

    def test_receipt_bytes_must_equal_eml_bytes(self):
        self.receipt["bytes"] = 0
        self._write_receipt()
        with self.assertRaisesRegex(mail_delta.Rejected, "receipt_bytes_mismatch"):
            self._import()
        self.assertEqual(self._active(), "baseline")

    def test_blob_seal_conflict_is_safe_rejection(self):
        blob = self.out / "blobs" / sha(self.attachment)
        blob.chmod(0o600)
        blob.write_bytes(b"corrupt synthetic preexisting blob")
        with self.assertRaisesRegex(mail_delta.Rejected, "existing_blob_invalid"):
            self._import()
        self.assertEqual(self._active(), "baseline")
        self.assertEqual(self.db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        self.assertFalse((self.out / "blobs" / sha(self.eml)).exists())
        self.assertEqual(blob.read_bytes(), b"corrupt synthetic preexisting blob")

    def test_replay_must_reverify_existing_blob(self):
        self._import()
        run = self._active()
        blob = self.out / "blobs" / sha(self.eml)
        blob.chmod(0o600)
        blob.write_bytes(b"corrupt synthetic blob")
        with self.assertRaises(mail_delta.Rejected):
            self._import()
        self.assertEqual(self._active(), run)
        self.assertEqual(self.db.execute("SELECT count(*) FROM runs").fetchone()[0], 2)

    def test_replay_missing_blob_fails_closed(self):
        self._import()
        run = self._active()
        (self.out / "blobs" / sha(self.attachment)).unlink()
        with self.assertRaises(mail_delta.Rejected):
            self._import()
        self.assertEqual(self._active(), run)
        self.assertEqual(self.db.execute("SELECT count(*) FROM runs").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
