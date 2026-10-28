"""Synthetic regressions for independent WP0 release findings."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from campaign_tool import __version__
from campaign_tool.records import export_clearance, release_manifest

class IndependentReviewRegressions(unittest.TestCase):
    def test_private_hash_filename_cannot_receive_clearance(self):
        source_sha = hashlib.sha256(b"fictional original").hexdigest()
        index = {
            "original_sha256": [source_sha], "portal_hosts": [],
            "correspondence_sources": [],
            "coverage": {"schema_version": 1,
                         "inventory_sha256": hashlib.sha256(b"fictional inventory").hexdigest(),
                         "originals_complete": True, "portal_hosts_complete": True,
                         "correspondence_complete": True, "correspondence_source_sha256": []},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / (source_sha.upper() + ".txt")).write_text("synthetic public example")
            _, findings, _, _ = export_clearance.scan_export(root, index)
        self.assertTrue(any(item[2] == "private_original_hash_path" for item in findings))

    def test_private_hash_empty_directory_cannot_receive_clearance(self):
        source_sha = hashlib.sha256(b"fictional directory original").hexdigest()
        index = {
            "original_sha256": [source_sha], "portal_hosts": [], "correspondence_sources": [],
            "coverage": {"schema_version": 1,
                         "inventory_sha256": hashlib.sha256(b"fictional directory inventory").hexdigest(),
                         "originals_complete": True, "portal_hosts_complete": True,
                         "correspondence_complete": True, "correspondence_source_sha256": []},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / source_sha).mkdir()
            (root / "public.txt").write_text("ordinary synthetic public example")
            _, findings, _, _ = export_clearance.scan_export(root, index)
        self.assertTrue(any(item[2] == "private_original_hash_path" for item in findings))

    def test_hidden_dependency_edit_is_not_clean_or_tagged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "campaign_tool").mkdir()
            (root / "campaign_tool" / "__init__.py").write_text("# fictional package\n")
            lock = root / "requirements-records-test.txt"
            lock.write_bytes(b"fictional-parser==1.0\n")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Synthetic Tester",
                            "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
            subprocess.run(["git", "-C", str(root), "tag", __version__], check=True)
            subprocess.run(["git", "-C", str(root), "update-index", "--assume-unchanged",
                            "requirements-records-test.txt"], check=True)
            lock.write_bytes(b"fictional-parser==2.0\n")
            self.assertEqual(subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"]), b"")
            manifest = release_manifest.source_manifest(root)
        self.assertTrue(manifest["source_dirty"])
        self.assertEqual(manifest["release_status"], "candidate")
