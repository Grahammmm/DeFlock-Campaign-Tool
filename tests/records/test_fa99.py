"""FA99 synthetic exporter preflight regressions (portable unittest selectors).

The extended wire shape is the existing test_mail_wire_intake chain fixture:
scalar EML -> binary nested.eml -> RFC822 child -> binary attachment. No live data.
"""
import base64
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records.intake import eml_export, mail_delta, mail_wire, native_receipt

REPO = Path(__file__).resolve().parents[2]
PREFLIGHT = ('    mail_delta.short_text(account, "invalid_account")\n'
             '    mail_delta.short_text(mailbox, "invalid_folder")\n')
INVALID = (None, True, False, 1, [], {}, "", "x" * 257, "bad\x00id")


def baseline_source():
    # Standard-stage acceptance uses its authoritative manifest pin. Portable
    # unittest (without operator staging) compares against committed HEAD bytes;
    # it does not claim qualification of an absent historical baseline.
    manifest = REPO.parent / "SOURCE-MANIFEST.json"
    base = json.loads(manifest.read_text())["head"] if manifest.is_file() else "HEAD"
    result = subprocess.run(["git", "show", base + ":campaign_tool/records/intake/eml_export.py"],
                            cwd=REPO, capture_output=True, check=True)
    return result.stdout


def exporter_from(source, label):
    namespace = {"__name__": eml_export.__name__, "__package__": eml_export.__package__}
    exec(compile(source, label, "exec"), namespace)
    return namespace["export_message"]


def leaf(body=b"Synthetic body\r\n"):
    return (b"From: fixture@x.invalid\r\nSubject: original\r\n\tcontinued folding\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + body)


def mixed(parts, boundary):
    return (b'From: fixture@x.invalid\r\nSubject: outer\r\nContent-Type: multipart/mixed; boundary="' +
            boundary + b'"\r\n\r\n' +
            b''.join(b'--' + boundary + b'\r\n' + part + b'\r\n' for part in parts) +
            b'--' + boundary + b'--\r\n')


def binary(raw, name):
    return (b'Content-Type: application/octet-stream\r\nContent-Disposition: attachment; filename="' +
            name + b'"\r\nContent-Transfer-Encoding: base64\r\n\r\n' + base64.b64encode(raw))


def extended():
    child = mixed([leaf(), binary(b"Exact synthetic attachment\x00\xff", b"fixture.bin")], b"inner-boundary")
    scalar = mixed([leaf(), b"Content-Type: message/rfc822\r\n\r\n" + child], b"scalar-boundary")
    return mixed([leaf(), binary(scalar, b"nested.eml")], b"synthetic-boundary")


def snapshot(root):
    return {str(p.relative_to(root)): ("dir",) if p.is_dir() else
            ("file", p.read_bytes(), p.stat().st_ino) for p in root.rglob("*")}


class Behavior(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "mail"

    def export(self, raw, **values):
        options = dict(mail_root=self.root, account="fixture-account", mailbox="INBOX", uidvalidity=7, uid=41)
        options.update(values)
        return eml_export.export_message(raw, **options)

    def invalid_preflight(self, raw, field):
        # Every spy is forbidden, including branch selection itself. If the gate
        # disappears the assertion fails at the first observable side effect.
        for value in INVALID:
            with self.subTest(field=field, value=repr(value)):
                with ExitStack() as stack:
                    spies = [stack.enter_context(patch.object(owner, name, side_effect=AssertionError("forbidden:" + name)))
                             for owner, name in ((mail_wire, "needs_extended"), (mail_wire, "prepare"),
                                                 (mail_delta, "mime_candidates"), (native_receipt, "export"),
                                                 (Path, "mkdir"), (Path, "exists"), (Path, "open"),
                                                 (eml_export.os, "open"), (eml_export, "_write_private"),
                                                 (eml_export, "_ensure_private"))]
                    with self.assertRaises(mail_delta.Rejected) as caught:
                        self.export(raw, **{field: value})
                    self.assertEqual(caught.exception.args, ("invalid_account" if field == "account" else "invalid_folder",))
                    for spy in spies:
                        spy.assert_not_called()
                self.assertFalse(self.root.exists())

    def test_ordinary_invalid_account_before_branch_parser_or_write(self):
        self.invalid_preflight(leaf(), "account")

    def test_ordinary_invalid_folder_before_branch_parser_or_write(self):
        self.invalid_preflight(leaf(), "mailbox")

    def test_extended_invalid_account_before_branch_parser_or_write(self):
        self.assertTrue(mail_wire.needs_extended(extended(), "eml"))
        self.invalid_preflight(extended(), "account")

    def test_extended_invalid_folder_before_branch_parser_or_write(self):
        self.assertTrue(mail_wire.needs_extended(extended(), "eml"))
        self.invalid_preflight(extended(), "mailbox")

    def positive(self, raw, native):
        baseline = exporter_from(baseline_source(), "pinned-baseline-eml-export")
        # Spaces/control whitespace are accepted by short_text: no trimming or
        # normalization is introduced. Exercise Unicode and exact character caps.
        for index, (account, folder) in enumerate((("é", "箱"), ("é" * 256, "箱" * 256),
                                                  (" naïve/部门 ", "\t箱\n"))):
            with self.subTest(account_length=len(account), folder_length=len(folder)):
                root = self.root / str(index)
                reference = self.root / ("baseline-" + str(index))
                options = dict(account=account, mailbox=folder, uidvalidity=7, uid=41)
                with patch.object(native_receipt, "export", wraps=native_receipt.export) as branch:
                    path = self.export(raw, mail_root=root, **options)
                self.assertEqual(branch.call_count, int(native))
                original = baseline(raw, mail_root=reference, **options)
                self.assertEqual(path.relative_to(root), original.relative_to(reference))
                self.assertEqual(path.read_bytes(), original.read_bytes())
                receipt, identity, _ = mail_delta.load_receipt(path)
                self.assertIs(receipt["complete"], True)
                self.assertEqual(identity, (account, folder, "7", "41"))
                self.assertEqual(receipt.get("schema"), mail_wire.SCHEMA if native else None)
                accepted = snapshot(root)
                self.assertEqual(path, self.export(raw, mail_root=root, **options))
                self.assertEqual(accepted, snapshot(root))
                second_options = dict(options, mailbox="另箱")
                second = self.export(raw, mail_root=root, **second_options)
                reference_second = baseline(raw, mail_root=reference, **second_options)
                tag = hashlib.sha256("\0".join((account, "另箱", "7", "41")).encode()).hexdigest()[:16]
                self.assertEqual(second.name, "receipt-" + tag + ".json")
                self.assertNotEqual(path, second)
                self.assertEqual(second.read_bytes(), reference_second.read_bytes())
                self.assertEqual(mail_delta.load_receipt(second)[1], (account, "另箱", "7", "41"))
                for relative, data in accepted.items():
                    self.assertEqual(snapshot(root)[relative], data)
                self.assertEqual((path.parent / "message.eml").read_bytes(), raw)
                self.assertEqual({str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()},
                                 {str(p.relative_to(reference)): p.read_bytes() for p in reference.rglob("*") if p.is_file()})

    def test_ordinary_unicode_boundaries_exact_baseline_bytes_replay_second_folder(self):
        self.positive(leaf(), False)

    def test_extended_unicode_boundaries_exact_baseline_bytes_replay_second_folder(self):
        self.positive(extended(), True)

    def test_existing_tree_unchanged_for_rejected_new_exports_both_branches(self):
        base = self.root
        index = 0
        for raw in (leaf(b"new ordinary bytes\r\n"), extended()):
            for field in ("account", "mailbox"):
                for value in INVALID:
                    with self.subTest(extended=raw == extended(), field=field, value=repr(value)):
                        self.root = base / str(index)
                        index += 1
                        self.export(leaf())
                        before = snapshot(self.root)
                        with self.assertRaises(mail_delta.Rejected) as caught:
                            self.export(raw, uid=42, **{field: value})
                        self.assertEqual(caught.exception.args, ("invalid_account" if field == "account" else "invalid_folder",))
                        self.assertEqual(snapshot(self.root), before)

    def test_raw_size_numeric_admission_still_precedes_text(self):
        for raw, numbers, code in ((b"", {}, "empty_message"), ("bad", {}, "empty_message"),
                                    (leaf(), {"uid": True}, "invalid_identity_numbers"),
                                    (leaf(), {"uidvalidity": 0}, "invalid_identity_numbers")):
            with self.subTest(code=code):
                with self.assertRaises(mail_delta.Rejected) as caught:
                    self.export(raw, account=None, **numbers)
                self.assertEqual(caught.exception.args, (code,))
        with patch.object(eml_export, "MAX_MESSAGE", len(leaf()) - 1):
            with self.assertRaises(mail_delta.Rejected) as caught:
                self.export(leaf(), account=None)
            self.assertEqual(caught.exception.args, ("message_size_limit",))
        self.assertFalse(self.root.exists())


class GateControl(unittest.TestCase):
    def test_removed_entry_checks_hit_forbidden_spy_then_restored_real_exports_pass(self):
        source = Path(eml_export.__file__).read_text()
        self.assertEqual(source.count(PREFLIGHT), 1)
        broken = exporter_from(source.replace(PREFLIGHT, "", 1), "fa99-in-memory-gate-removed")
        evidence = Path(tempfile.mkdtemp(prefix="gate-control-"))
        target = "test_ordinary_invalid_account_before_branch_parser_or_write"
        broken_output = io.StringIO()
        with patch.object(eml_export, "export_message", broken):
            result = unittest.TextTestRunner(stream=broken_output, verbosity=2).run(unittest.TestSuite([Behavior(target)]))
        (evidence / "removed-gate.txt").write_text(broken_output.getvalue())
        self.assertEqual(result.testsRun, 1)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(result.skipped, [])
        self.assertIn("forbidden:needs_extended", broken_output.getvalue())
        positive_output = io.StringIO()
        restored = unittest.TestSuite(Behavior(name) for name in (
            target, "test_ordinary_unicode_boundaries_exact_baseline_bytes_replay_second_folder",
            "test_extended_unicode_boundaries_exact_baseline_bytes_replay_second_folder"))
        result = unittest.TextTestRunner(stream=positive_output, verbosity=2).run(restored)
        (evidence / "restored-positive.txt").write_text(positive_output.getvalue())
        self.assertEqual(result.testsRun, 3)
        self.assertEqual(result.skipped, [])
        self.assertTrue(result.wasSuccessful(), positive_output.getvalue())
        print("Negative-control raw outputs:", evidence)


if __name__ == "__main__":
    unittest.main()
