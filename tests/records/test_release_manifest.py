"""Synthetic release identity and installed-file integrity regressions."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool import __version__
from campaign_tool.records.release_manifest import FEATURES, package_files, source_manifest, verify_installed
from campaign_tool.records.public_scan import load_private_index, violations


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / "campaign_tool"
        (self.package / "records").mkdir(parents=True)
        (self.package / "__init__.py").write_text("# synthetic\n")
        self.lock = b"synthetic-parser==1.0\n"
        (self.root / "requirements-records-test.txt").write_bytes(self.lock)
        (self.package / "records" / "_release_dependencies.txt").write_bytes(self.lock)
        self.manifest = {"manifest_schema_version": 1, "commit": "1" * 40,
                         "package_version": __version__, "ledger_schema_version": 0,
                         "dependency_lock_sha256": hashlib.sha256(self.lock).hexdigest(),
                         "enabled_features": list(FEATURES), "runtime_services_enabled": [],
                         "source_dirty": False, "release_tag": None, "release_status": "candidate",
                         "package_files": package_files(self.package)}

    def test_valid_install_does_not_claim_tag_or_running_services(self):
        result = verify_installed(self.package, self.manifest)
        self.assertEqual(result["release_status"], "candidate")
        self.assertEqual(result["ledger_schema_version"], 0)
        self.assertEqual(result["runtime_services_enabled"], [])

    def test_installed_code_change_rejected(self):
        (self.package / "__init__.py").write_text("# changed\n")
        with self.assertRaisesRegex(ValueError, "package files"):
            verify_installed(self.package, self.manifest)

    def test_added_code_rejected(self):
        (self.package / "extra.py").write_text("# extra\n")
        with self.assertRaisesRegex(ValueError, "package files"):
            verify_installed(self.package, self.manifest)

    def test_changed_dependency_lock_rejected(self):
        (self.package / "records" / "_release_dependencies.txt").write_bytes(b"different\n")
        with self.assertRaisesRegex(ValueError, "dependency lock"):
            verify_installed(self.package, self.manifest)

    def test_generated_cache_does_not_change_identity(self):
        cache = self.package / "__pycache__"
        cache.mkdir()
        (cache / "module.pyc").write_bytes(b"synthetic bytecode")
        verify_installed(self.package, self.manifest)

    def test_traversal_and_symlink_rejected(self):
        bad = copy.deepcopy(self.manifest)
        bad["package_files"]["../other.py"] = hashlib.sha256(b"other").hexdigest()
        with self.assertRaisesRegex(ValueError, "inventory path"):
            verify_installed(self.package, bad)
        (self.package / "linked.py").symlink_to(self.package / "__init__.py")
        with self.assertRaisesRegex(ValueError, "symlink"):
            verify_installed(self.package, self.manifest)

    def test_false_feature_schema_version_or_tag_rejected(self):
        cases = [("ledger_schema_version", True), ("enabled_features", ["automatic_publication"]),
                 ("release_status", "tagged_release"), ("source_dirty", True),
                 ("package_version", "9.9.9")]
        for key, value in cases:
            candidate = copy.deepcopy(self.manifest)
            candidate[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify_installed(self.package, candidate)

    def test_source_records_dirty_tree_as_candidate(self):
        def git(root, *args):
            if args == ("rev-parse", "--show-toplevel"): return str(self.root)
            if args == ("rev-parse", "HEAD"): return "2" * 40
            if args[0] == "status": return " M modified.py"
            if args[0] == "tag": return __version__
            self.fail(args)
        with patch("campaign_tool.records.release_manifest._git", side_effect=git), \
                patch("campaign_tool.records.release_manifest.committed_package_files",
                      return_value=package_files(self.package)), \
                patch("campaign_tool.records.release_manifest.subprocess.check_output", return_value=self.lock):
            value = source_manifest(self.root)
        self.assertTrue(value["source_dirty"])
        self.assertEqual(value["release_status"], "candidate")

    def test_private_hash_and_correspondence_scan_never_echoes_values(self):
        original = b"fictional source bytes"
        identity = hashlib.sha256(original).hexdigest()
        fragment = "Fictional correspondence phrase for a private test"
        index = {"original_sha256": [identity], "text_fragments": [fragment], "portal_hosts": ["records.portal.invalid"]}
        payload = (identity + "\n" + fragment + "\nrecords.portal.invalid").encode()
        hits = violations("sample.txt", payload, strict=True, private_index=index)
        self.assertEqual({hit[2] for hit in hits}, {"private_original_hash", "private_correspondence", "private_portal_host"})
        self.assertNotIn(identity, repr(hits))
        self.assertNotIn(fragment, repr(hits))
        self.assertIn("private_original_bytes", {hit[2] for hit in violations("sample.txt", original, private_index=index)})

    def test_strict_actor_path_and_portal_patterns(self):
        actor = b"/home/" + b"coder/config"
        portal = b"fictional." + b"nextrequest.com"
        self.assertEqual({hit[2] for hit in violations("sample.txt", actor + b"\n" + portal, strict=True)},
                         {"private_actor_path", "portal_host"})

    def test_private_index_rejects_invalid_hash_and_short_fragments(self):
        path = self.root / "index.json"
        for value in ({"original_sha256": ["not a hash"]}, {"text_fragments": ["short"]}):
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError): load_private_index(path)


if __name__ == "__main__":
    unittest.main()
