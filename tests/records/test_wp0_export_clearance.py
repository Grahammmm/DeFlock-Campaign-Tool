"""Synthetic regressions for committed builds and private exact-export gates."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool import __version__
from campaign_tool.records import export_clearance, public_scan, release_manifest


class CommittedPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.package = self.root / "campaign_tool"
        self.package.mkdir()
        (self.package / "__init__.py").write_text("# synthetic committed code\n")
        (self.root / "requirements-records-test.txt").write_text("synthetic==1\n")
        self.expected = release_manifest.package_files(self.package)
        self.expected_lock = (self.root / "requirements-records-test.txt").read_bytes()

    def git(self, root, *args):
        if args == ("rev-parse", "--show-toplevel"):
            return str(self.root)
        if args == ("rev-parse", "HEAD"):
            return "1" * 40
        if args[0] == "status":
            return ""
        if args[0] == "tag":
            return __version__
        self.fail(args)

    def manifest(self):
        with patch.object(release_manifest, "_git", side_effect=self.git), \
                patch.object(release_manifest, "committed_package_files", return_value=self.expected), \
                patch.object(release_manifest.subprocess, "check_output", return_value=self.expected_lock):
            return release_manifest.source_manifest(self.root)

    def test_clean_committed_bytes_can_be_tagged(self):
        value = self.manifest()
        self.assertFalse(value["source_dirty"])
        self.assertEqual(value["release_status"], "tagged_release")

    def test_ignored_package_bytes_cannot_get_clean_provenance(self):
        private = self.package / "private"
        private.mkdir()
        (private / "__init__.py").write_text("# ignored synthetic package\n")
        value = self.manifest()
        self.assertTrue(value["source_dirty"])
        self.assertEqual(value["release_status"], "candidate")

    def test_uncommitted_additions_and_hidden_tracked_edits_are_dirty(self):
        for variant in ("new", "changed", "missing"):
            with self.subTest(variant=variant):
                target = self.package / "__init__.py"
                extra = self.package / "extra.py"
                target.write_text("# synthetic committed code\n")
                if extra.exists():
                    extra.unlink()
                if variant == "new":
                    extra.write_text("# uncommitted\n")
                elif variant == "changed":
                    target.write_text("# changed despite clean status\n")
                else:
                    target.unlink()
                self.assertTrue(self.manifest()["source_dirty"])

    def test_committed_inventory_reads_head_blobs_not_worktree(self):
        payload = b"# committed synthetic bytes\n"
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            item = tarfile.TarInfo("campaign_tool/__init__.py")
            item.size = len(payload)
            archive.addfile(item, io.BytesIO(payload))
        with patch.object(release_manifest.subprocess, "check_output", return_value=stream.getvalue()) as git:
            observed = release_manifest.committed_package_files(self.root)
        self.assertEqual(observed, {"__init__.py": hashlib.sha256(payload).hexdigest()})
        self.assertEqual(git.call_args.args[0][-3:], ["--format=tar", "HEAD", "campaign_tool"])

    def test_untagged_matching_source_is_still_candidate(self):
        def untagged(root, *args):
            return "" if args[0] == "tag" else self.git(root, *args)
        with patch.object(release_manifest, "_git", side_effect=untagged), \
                patch.object(release_manifest, "committed_package_files", return_value=self.expected), \
                patch.object(release_manifest.subprocess, "check_output", return_value=self.expected_lock):
            value = release_manifest.source_manifest(self.root)
        self.assertIsNone(value["release_tag"])
        self.assertEqual(value["release_status"], "candidate")
        self.assertEqual(value["runtime_services_enabled"], [])


class ExactExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        self.export = self.root / "export"
        self.export.mkdir(mode=0o700)
        self.code = self.export / "module.py"
        self.code.write_text("# fictional public engine implementation\n")
        self.index_path = self.root / "index.json"
        self.receipt = self.root / "attestation.json"
        self.private_text = "Fictional confidential correspondence regarding orchard delivery records."
        self.original = self.private_text.encode()
        self.original_sha = hashlib.sha256(self.original).hexdigest()
        self.empty_sha = hashlib.sha256(b"").hexdigest()
        self.index = {
            "original_sha256": [self.original_sha, self.empty_sha],
            "portal_hosts": ["records.portal.invalid"],
            "text_fragments": [],
            "coverage": {"schema_version": 1,
                         "inventory_sha256": hashlib.sha256(b"synthetic private inventory").hexdigest(),
                         "originals_complete": True, "portal_hosts_complete": True,
                         "correspondence_complete": True,
                         "correspondence_source_sha256": [self.original_sha]},
            "correspondence_sources": [{"source_sha256": self.original_sha, "text": self.private_text}],
        }
        self.save_index()

    def save_index(self):
        self.index_path.write_text(json.dumps(self.index))
        self.index_path.chmod(0o600)

    def run_gate(self, verify=False):
        return export_clearance.clearance(self.export, self.index_path, self.receipt, verify=verify)

    def test_exact_export_attests_and_reverifies_privately(self):
        result = self.run_gate()
        self.assertTrue(result["private_export_clearance"])
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.run_gate(verify=True)["attestation_verified"])
        raw = self.receipt.read_text()
        self.assertNotIn(self.private_text, raw)
        self.assertNotIn(self.original_sha, raw)
        self.assertNotIn("records.portal.invalid", raw)

    def test_missing_index_or_attestation_blocks_strict_cli(self):
        for arguments in (["--strict", "--root", str(self.export)],
                          ["--strict", "--root", str(self.export), "--private-index", str(self.index_path)]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(public_scan.main(arguments), 2)
        self.assertFalse(self.receipt.exists())

    def test_missing_receipt_cannot_verify(self):
        with self.assertRaises(OSError):
            self.run_gate(verify=True)

    def test_hash_only_index_cannot_claim_correspondence_clearance(self):
        del self.index["coverage"]
        del self.index["correspondence_sources"]
        self.save_index()
        with self.assertRaisesRegex(ValueError, "coverage"):
            self.run_gate()

    def test_incomplete_coverage_is_rejected(self):
        original = copy.deepcopy(self.index)
        for field in ("originals_complete", "portal_hosts_complete", "correspondence_complete"):
            self.index = copy.deepcopy(original)
            self.index["coverage"][field] = False
            self.save_index()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "incomplete"):
                self.run_gate()

    def test_missing_correspondence_source_is_rejected(self):
        self.index["correspondence_sources"] = []
        self.save_index()
        with self.assertRaisesRegex(ValueError, "missing_correspondence"):
            self.run_gate()

    def test_wrong_correspondence_binding_is_rejected(self):
        self.index["correspondence_sources"][0]["source_sha256"] = "b" * 64
        self.save_index()
        with self.assertRaisesRegex(ValueError, "binding"):
            self.run_gate()

    def test_stale_export_receipt_rejected(self):
        self.run_gate()
        self.code.write_text("# changed public engine implementation\n")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.run_gate(verify=True)

    def test_added_hidden_file_invalidates_exact_inventory(self):
        self.run_gate()
        (self.export / ".hidden").write_text("new public material\n")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.run_gate(verify=True)

    def test_stale_index_and_coverage_bindings_rejected(self):
        self.run_gate()
        self.index["coverage"]["inventory_sha256"] = hashlib.sha256(b"changed inventory").hexdigest()
        self.save_index()
        with self.assertRaisesRegex(ValueError, "stale"):
            self.run_gate(verify=True)

    def test_changed_scanner_binding_rejected(self):
        self.run_gate()
        with patch.object(export_clearance, "scanner_binding", return_value={"changed": "1" * 64}):
            with self.assertRaisesRegex(ValueError, "stale"):
                self.run_gate(verify=True)

    def test_existing_receipt_is_not_overwritten(self):
        self.run_gate()
        with self.assertRaises(FileExistsError):
            self.run_gate()

    def test_zero_byte_original_is_explicit_non_content(self):
        (self.export / "__init__.py").write_bytes(b"")
        value = self.run_gate()
        self.assertTrue(value["private_export_clearance"])
        self.assertEqual(value["non_content_matches"], [
            {"path": "__init__.py", "classification": "zero_byte_original_non_content"}])

    def test_nonempty_original_bytes_still_block(self):
        self.code.write_bytes(self.original)
        result = self.run_gate()
        self.assertFalse(result["private_export_clearance"])
        self.assertIn("private_original_bytes", {item[2] for item in result["findings"]})
        self.assertFalse(self.receipt.exists())

    def test_printed_empty_original_hash_still_blocks(self):
        self.code.write_text(self.empty_sha)
        result = self.run_gate()
        self.assertIn("private_original_hash", {item[2] for item in result["findings"]})

    def test_non_content_does_not_waive_denied_path(self):
        (self.export / "private").mkdir()
        (self.export / "private" / "empty.txt").write_bytes(b"")
        result = self.run_gate()
        self.assertFalse(result["private_export_clearance"])
        self.assertIn("private_path", {item[2] for item in result["findings"]})
        self.assertTrue(result["non_content_matches"])

    def test_case_whitespace_and_partial_correspondence_match(self):
        self.code.write_text("prefix " + "   ".join(self.private_text.upper().split()[1:7]) + " suffix")
        result = self.run_gate()
        self.assertIn("private_correspondence_normalized", {item[2] for item in result["findings"]})
        self.assertFalse(self.receipt.exists())

    def test_credential_scan_not_weakened_by_indexed_clearance(self):
        self.code.write_text("ghp_" + "a" * 36)
        result = self.run_gate()
        self.assertIn("github_token", {item[2] for item in result["findings"]})
        self.assertFalse(self.receipt.exists())

    def test_unscanned_binary_and_archives_block(self):
        for content in (b"\xff\xfe", b"PK\x03\x04synthetic"):
            self.code.write_bytes(content)
            result = self.run_gate()
            self.assertFalse(result["private_export_clearance"])
            self.assertFalse(self.receipt.exists())

    def test_symlink_and_git_metadata_block(self):
        link = self.export / "linked.py"
        link.symlink_to(self.code)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.run_gate()
        link.unlink()
        (self.export / ".git").write_text("synthetic metadata")
        with self.assertRaisesRegex(ValueError, "git"):
            self.run_gate()

    def test_private_index_inside_export_is_rejected(self):
        internal = self.export / "index.json"
        internal.write_text(json.dumps(self.index))
        internal.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "inside_export"):
            export_clearance.clearance(self.export, internal, self.receipt)

    def test_resource_limit_blocks_instead_of_claiming_partial_coverage(self):
        with patch.object(export_clearance, "MAX_WINDOWS", 1):
            with self.assertRaisesRegex(ValueError, "resource_limit"):
                self.run_gate()


if __name__ == "__main__":
    unittest.main()
