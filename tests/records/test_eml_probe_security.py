"""Secure bounded format probes using synthetic mail-wire-receipt-v2 schemas."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from campaign_tool.records.intake import eml_export, folder, mail_delta


class SecureReceiptProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-eml-probe-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "mail"
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True)
        self.receipt_path = self.objects / "receipt.json"
        self.payload = b"%PDF-1.7\nsynthetic format probe\n" + b"x" * 40000
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.attachment = self.objects / "attachment.bin"
        self.attachment.write_bytes(self.payload)
        raw = b"From: a@example.invalid\r\nTo: b@example.invalid\r\nSubject: synthetic\r\n\r\nbody\r\n"
        (self.objects / "message.eml").write_bytes(raw)
        # Exact native_receipt.parse vocabulary; every test goes through the real parser.
        self.receipt = {"schema": "mail-wire-receipt-v2", "complete": True,
                        "account_id": "synthetic-account", "folder": "INBOX",
                        "uidvalidity": 1, "uid": 1, "bytes": len(raw),
                        "original_eml": {"format": "eml", "path": "objects/message.eml",
                                         "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
                        "attachments": [{"part": "1.2", "path": "objects/attachment.bin",
                                         "bytes": len(self.payload), "sha256": self.sha,
                                         "format": None, "original_filename": "attachment.bin"}],
                        "native_items": [], "budget": {}}
        self.save()

    def save(self):
        self.receipt_path.write_text(json.dumps(self.receipt))

    def probe(self, **kwargs):
        return eml_export.attachment_forms(self.receipt_path, **kwargs)

    def assert_probe_rejects(self):
        with self.assertRaises((mail_delta.Rejected, OSError, ValueError)):
            self.probe()

    def test_regular_relative_object_positive_control(self):
        self.assertEqual(self.probe()[self.sha], "pdf")

    def test_real_extended_exporter_receipt_and_replay(self):
        from tests.records.test_mail_wire_intake import mixed, leaf, binary
        child = mixed([leaf(), binary(self.payload, b"attachment.bin")], b"inner")
        raw = mixed([leaf(), binary(child, b"nested.eml")])
        kwargs = {"mail_root": self.root, "account": "synthetic-account",
                  "mailbox": "INBOX", "uidvalidity": 7, "uid": 2}
        receipt = eml_export.export_message(raw, **kwargs)
        value, _, _ = mail_delta.load_receipt(receipt)
        self.assertEqual(value["schema"], "mail-wire-receipt-v2")
        original_receipt = receipt.read_bytes()
        self.assertEqual(eml_export.attachment_forms(receipt)[self.sha], "pdf")
        self.assertEqual(eml_export.export_message(raw, **kwargs), receipt)
        self.assertEqual(receipt.read_bytes(), original_receipt)
        self.assertEqual(eml_export.attachment_forms(receipt)[self.sha], "pdf")

    def test_existing_importer_output_exclusion_is_unchanged(self):
        output = self.root / "intake"
        with self.assertRaisesRegex(mail_delta.Rejected, "source_is_output"):
            mail_delta.source_path(self.root, "intake/blobs/example", output)
        self.assertEqual(mail_delta.source_path(self.root, "objects/attachment.bin", output),
                         self.attachment)

    def test_paths_rejected_before_any_object_open(self):
        outside = self.base / "outside.bin"
        outside.write_bytes(self.payload)
        bad_paths = (str(outside), str(self.attachment), "../outside.bin",
                     "objects/../../outside.bin", "objects/../objects/attachment.bin",
                     "objects\\attachment.bin", "", "objects/\x00bad")
        for literal in bad_paths:
            with self.subTest(path=literal):
                self.receipt["attachments"][0]["path"] = literal
                self.save()
                real_open = folder.secure_open
                opened = []

                def observe(path):
                    opened.append(Path(path))
                    return real_open(path)

                with mock.patch.object(folder, "secure_open", side_effect=observe):
                    self.assert_probe_rejects()
                self.assertEqual(opened, [self.receipt_path])

    def test_leaf_and_intermediate_symlinks_rejected(self):
        outside = self.base / "outside.bin"
        outside.write_bytes(self.payload)
        (self.objects / "leaf.bin").symlink_to(outside)
        (self.root / "linked").symlink_to(self.objects, target_is_directory=True)
        for literal in ("objects/leaf.bin", "linked/attachment.bin"):
            with self.subTest(path=literal):
                self.receipt["attachments"][0]["path"] = literal
                self.save()
                self.assert_probe_rejects()

    def test_symlink_swap_between_validation_and_open_is_rejected(self):
        real_source_path = mail_delta.source_path
        outside = self.base / "outside.bin"
        outside.write_bytes(self.payload)

        def swap(*args, **kwargs):
            path = real_source_path(*args, **kwargs)
            self.attachment.unlink()
            self.attachment.symlink_to(outside)
            return path

        with mock.patch.object(mail_delta, "source_path", side_effect=swap):
            self.assert_probe_rejects()

    def test_probe_reads_only_16384_bytes_and_never_read_bytes(self):
        real_open = folder.secure_open
        reads = []

        @contextmanager
        def observe(path):
            with real_open(path) as source:
                if Path(path) != self.attachment:
                    yield source
                else:
                    class LimitedReader:
                        def read(self, size=-1):
                            reads.append(size)
                            self_test.assertEqual(size, 16384)
                            return source.read(size)
                    yield LimitedReader()

        self_test = self
        with mock.patch.object(folder, "secure_open", side_effect=observe), \
                mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read forbidden")):
            self.assertEqual(self.probe()[self.sha], "pdf")
        self.assertEqual(reads, [16384])

    def test_supplied_canonical_heads_never_open_receipt_objects(self):
        self.receipt["attachments"][0]["path"] = "../absent.bin"
        self.save()
        real_open = folder.secure_open
        opened = []

        def receipt_only(path):
            opened.append(Path(path))
            self.assertEqual(Path(path), self.receipt_path)
            return real_open(path)

        with mock.patch.object(folder, "secure_open", side_effect=receipt_only):
            self.assertEqual(self.probe(heads={self.sha: self.payload}), {self.sha: "pdf"})
            self.assertEqual(self.probe(heads={}), {})
        self.assertEqual(opened, [self.receipt_path, self.receipt_path])

    def test_declared_native_format_does_not_require_object_read(self):
        self.receipt["attachments"][0].update(format="msg", path="objects/absent.msg")
        self.save()
        self.assertEqual(self.probe()[self.sha], "msg")

    def test_directory_object_rejected(self):
        self.receipt["attachments"][0]["path"] = "objects"
        self.save()
        self.assert_probe_rejects()

    def test_broken_following_open_negative_control_defeats_symlink_assertion(self):
        target = self.base / "synthetic-outside.bin"
        target.write_bytes(self.payload)
        (self.objects / "link.bin").symlink_to(target)
        self.receipt["attachments"][0]["path"] = "objects/link.bin"
        self.save()
        # Deliberately break the no-follow implementation only for this synthetic fixture.
        with mock.patch.object(folder, "secure_open", side_effect=lambda path: open(path, "rb")):
            with self.assertRaises(AssertionError):
                self.assert_probe_rejects()
            self.assertEqual(self.probe()[self.sha], "pdf")

    def test_relative_receipt_path_is_supported(self):
        # The caller may supply a relative receipt; root confinement still uses an absolute base.
        import os
        old = Path.cwd()
        try:
            os.chdir(self.base)
            self.assertEqual(eml_export.attachment_forms(Path("mail/objects/receipt.json"))[self.sha], "pdf")
        finally:
            os.chdir(old)
