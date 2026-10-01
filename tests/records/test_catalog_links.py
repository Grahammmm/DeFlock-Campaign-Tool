"""Synthetic, private-path-only tests for candidate catalog context links."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import catalog_links


H1 = "a" * 64
H2 = "b" * 64


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (catalog_links.canonical(value) + "\n").encode("utf-8")


class CatalogLinksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir(mode=0o700)
        self.registry_path = self.root / "registry.json"
        self.output = self.root / "output"
        self.manifest = {"schema_version": 1, "sources": []}
        self.cards = [{"sha256": H1}, {"sha256": H2}]
        self.write_snapshot()
        self.registry = {
            "schema_version": 1,
            "snapshot_id": self.snapshot_id,
            "catalog_sha256": self.catalog_sha,
            "agencies": [{"agency_id": "agency:north"}, {"agency_id": "agency:south"}],
            "requests": [
                {"request_id": "request:one", "agency_id": "agency:north"},
                {"request_id": "request:two", "agency_id": "agency:north"},
                {"request_id": "request:three", "agency_id": "agency:south"},
            ],
            "links": [{"source_sha256": H1, "agency_id": "agency:north",
                       "request_id": "request:one"}],
        }
        self.write_registry()

    def write_file(self, path, data):
        path.write_bytes(data)
        path.chmod(0o600)

    def write_snapshot(self):
        self.snapshot_id = sha(catalog_links.canonical(self.manifest).encode("utf-8"))
        catalog = {"schema_version": 1, "snapshot_id": self.snapshot_id, "cards": self.cards}
        catalog_bytes = encoded(catalog)
        manifest_bytes = encoded(self.manifest)
        self.catalog_sha = sha(catalog_bytes)
        self.write_file(self.snapshot / "catalog.json", catalog_bytes)
        self.write_file(self.snapshot / "input-manifest.json", manifest_bytes)
        self.write_file(self.snapshot / "artifact-hashes.json", encoded({
            "catalog.json": self.catalog_sha, "input-manifest.json": sha(manifest_bytes)
        }))

    def write_registry(self):
        self.write_file(self.registry_path, encoded(self.registry))

    def assert_blocked(self, code):
        with self.assertRaises(catalog_links.LinkError) as raised:
            catalog_links.run(self.snapshot, self.registry_path, self.output)
        self.assertEqual(raised.exception.code, code)

    def test_valid_exact_hash_link_receipt_and_unchanged_reuse(self):
        first = catalog_links.run(self.snapshot, self.registry_path, self.output)
        again = catalog_links.run(self.snapshot, self.registry_path, self.output)
        self.assertEqual(first["run_id"], again["run_id"])
        self.assertFalse(first["reused"])
        self.assertTrue(again["reused"])
        destination = self.output / first["run_id"]
        self.assertEqual({p.name for p in destination.iterdir()}, {"candidates.json", "receipt.json"})
        candidates = json.loads((destination / "candidates.json").read_text())
        receipt = json.loads((destination / "receipt.json").read_text())
        self.assertEqual(candidates["candidates"], [{
            "source_sha256": H1, "agency_id": "agency:north", "request_id": "request:one",
            "status": "candidate_only", "verified": False, "publication_ready": False,
        }])
        self.assertEqual(receipt["input_bindings"]["catalog_sha256"], self.catalog_sha)
        self.assertEqual(receipt["input_bindings"]["registry_sha256"], sha(self.registry_path.read_bytes()))
        self.assertEqual(receipt["result_sha256"], sha((destination / "candidates.json").read_bytes()))
        self.assertEqual(receipt["verified_attributions"], 0)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
        for file in destination.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_same_hash_multiple_legitimate_request_contexts_retained(self):
        self.registry["links"].append({"source_sha256": H1, "agency_id": "agency:north",
                                       "request_id": "request:two"})
        self.write_registry()
        run = catalog_links.run(self.snapshot, self.registry_path, self.output)
        candidates = json.loads((self.output / run["run_id"] / "candidates.json").read_text())
        self.assertEqual([item["request_id"] for item in candidates["candidates"]],
                         ["request:one", "request:two"])
        self.assertEqual(candidates["summary"]["source_hashes"], 1)

    def test_unknown_and_conflicting_ids_fail_closed(self):
        self.registry["links"][0]["agency_id"] = "agency:missing"
        self.write_registry()
        self.assert_blocked("unknown_agency_id")
        self.registry["links"][0]["agency_id"] = "agency:north"
        self.registry["links"][0]["request_id"] = "request:missing"
        self.write_registry()
        self.assert_blocked("unknown_request_id")
        self.registry["links"][0]["request_id"] = "request:three"
        self.write_registry()
        self.assert_blocked("request_agency_mismatch")
        self.registry["links"] = [{"source_sha256": H2, "agency_id": "agency:north",
                                    "request_id": "request:one"}]
        self.registry["requests"][0]["agency_id"] = "agency:missing"
        self.write_registry()
        self.assert_blocked("unknown_agency_id")

    def test_compound_native_request_identity_and_cross_agency_byte_contexts(self):
        self.registry["requests"].append({"request_id": "request:one",
                                          "agency_id": "agency:south"})
        self.registry["links"].append({"source_sha256": H1, "agency_id": "agency:south",
                                       "request_id": "request:one"})
        self.write_registry()
        run = catalog_links.run(self.snapshot, self.registry_path, self.output)
        candidates = json.loads((self.output / run["run_id"] / "candidates.json").read_text())
        self.assertEqual([(item["source_sha256"], item["agency_id"], item["request_id"])
                          for item in candidates["candidates"]],
                         [(H1, "agency:north", "request:one"),
                          (H1, "agency:south", "request:one")])
        self.assertEqual(candidates["summary"]["source_hashes"], 1)
        self.assertEqual(candidates["summary"]["registry_requests"], 4)

    def test_excluded_role_and_status_consistency(self):
        self.cards[0].update(role="excluded_unrelated_personal", agency_status="scope_excluded")
        self.write_snapshot()
        self.registry["catalog_sha256"] = self.catalog_sha
        self.write_registry()
        self.assert_blocked("source_scope_excluded")
        self.cards[0]["agency_status"] = "unreviewed"
        self.write_snapshot()
        self.registry["catalog_sha256"] = self.catalog_sha
        self.write_registry()
        self.assert_blocked("inconsistent_source_scope")
        self.cards[0]["role"] = "record"
        self.cards[0]["agency_status"] = "scope_excluded"
        self.write_snapshot()
        self.registry["catalog_sha256"] = self.catalog_sha
        self.write_registry()
        self.assert_blocked("inconsistent_source_scope")

    def test_malformed_foreign_key_types_have_fixed_codes(self):
        for field, code in (("source_sha256", "invalid_source_sha"),
                            ("agency_id", "invalid_agency_id"),
                            ("request_id", "invalid_request_id")):
            original = self.registry["links"][0][field]
            self.registry["links"][0][field] = []
            self.write_registry()
            self.assert_blocked(code)
            self.registry["links"][0][field] = original
        self.registry["requests"][0]["agency_id"] = []
        self.write_registry()
        self.assert_blocked("invalid_agency_id")

    def test_output_limit_is_compatible_with_reuse(self):
        for index in range(20):
            request_id = f"request:many-{index}"
            self.registry["requests"].append({"request_id": request_id,
                                              "agency_id": "agency:north"})
            self.registry["links"].append({"source_sha256": H1,
                                           "agency_id": "agency:north",
                                           "request_id": request_id})
        self.write_registry()
        raw = self.registry_path.read_bytes()
        result = catalog_links._registry(raw, self.snapshot_id, self.catalog_sha,
                                         {card["sha256"]: card for card in self.cards})
        result_size = len(encoded(result))
        largest_input = max(len(raw), *(len(path.read_bytes()) for path in self.snapshot.iterdir()))
        self.assertGreater(result_size, largest_input)
        with mock.patch.object(catalog_links, "MAX_INPUT_BYTES", result_size - 1):
            self.assert_blocked("output_too_large")
        self.assertFalse(self.output.exists())
        with mock.patch.object(catalog_links, "MAX_INPUT_BYTES", result_size):
            first = catalog_links.run(self.snapshot, self.registry_path, self.output)
            second = catalog_links.run(self.snapshot, self.registry_path, self.output)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["run_id"], second["run_id"])

    def test_directory_fsync_on_new_publication_and_reuse(self):
        with mock.patch.object(catalog_links, "_fsync_directory",
                               wraps=catalog_links._fsync_directory) as sync:
            first = catalog_links.run(self.snapshot, self.registry_path, self.output)
            paths = [call.args[0] for call in sync.call_args_list]
            self.assertTrue(any(path.name.startswith(".links-stage-") for path in paths))
            self.assertGreaterEqual(paths.count(self.output), 2)
            self.assertIn(self.output.parent, paths)
            sync.reset_mock()
            second = catalog_links.run(self.snapshot, self.registry_path, self.output)
            paths = [call.args[0] for call in sync.call_args_list]
            self.assertIn(self.output / first["run_id"], paths)
            self.assertGreaterEqual(paths.count(self.output), 2)
            self.assertFalse(any(path.name.startswith(".links-stage-") for path in paths))
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertTrue(second["reused"])

    def test_new_output_ancestor_sync_failure_retry_recovers(self):
        self.output = self.root / "new-parent" / "inner" / "output"
        actual = catalog_links._fsync_directory
        failed = False

        def fail_parent_once(path):
            nonlocal failed
            if path == self.output.parent and not failed:
                failed = True
                raise OSError("synthetic parent sync failure")
            actual(path)

        with mock.patch.object(catalog_links, "_fsync_directory", side_effect=fail_parent_once):
            self.assert_blocked("output_sync_failed")
        self.assertTrue(self.output.is_dir())
        self.assertFalse((self.output / "writer.lock").exists())
        with mock.patch.object(catalog_links, "_fsync_directory", wraps=actual) as sync:
            result = catalog_links.run(self.snapshot, self.registry_path, self.output)
            paths = [call.args[0] for call in sync.call_args_list]
        self.assertFalse(result["reused"])
        self.assertIn(self.output.parent, paths)
        self.assertIn(self.output.parent.parent, paths)
        self.assertIn(self.root, paths)
        self.assertTrue((self.output / result["run_id"] / "receipt.json").is_file())

    def test_post_rename_sync_failure_requires_reuse_recovery(self):
        actual = catalog_links._fsync_directory
        failed = False

        def fail_after_rename(path):
            nonlocal failed
            if (path == self.output and not failed and self.output.exists()
                    and any(child.is_dir() and len(child.name) == 64
                            for child in self.output.iterdir())):
                failed = True
                raise OSError("synthetic post-rename sync failure")
            actual(path)

        with mock.patch.object(catalog_links, "_fsync_directory", side_effect=fail_after_rename):
            self.assert_blocked("output_sync_failed")
        destinations = [path for path in self.output.iterdir()
                        if path.is_dir() and len(path.name) == 64]
        self.assertEqual(len(destinations), 1)
        destination = destinations[0]
        original = {path.name: path.read_bytes() for path in destination.iterdir()}

        def fail_reuse_destination(path):
            if path == destination:
                raise OSError("synthetic recovery sync failure")
            actual(path)

        with mock.patch.object(catalog_links, "_fsync_directory",
                               side_effect=fail_reuse_destination):
            self.assert_blocked("output_sync_failed")
        with mock.patch.object(catalog_links, "_fsync_directory", wraps=actual) as sync:
            recovered = catalog_links.run(self.snapshot, self.registry_path, self.output)
            paths = [call.args[0] for call in sync.call_args_list]
        self.assertTrue(recovered["reused"])
        self.assertEqual(recovered["run_id"], destination.name)
        self.assertIn(destination, paths)
        self.assertGreaterEqual(paths.count(self.output), 2)
        self.assertEqual({path.name: path.read_bytes() for path in destination.iterdir()},
                         original)

    def test_stale_snapshot_and_source_bindings_fail_closed(self):
        self.registry["snapshot_id"] = "f" * 64
        self.write_registry()
        self.assert_blocked("stale_registry_binding")
        self.registry["snapshot_id"] = self.snapshot_id
        self.registry["links"][0]["source_sha256"] = "f" * 64
        self.write_registry()
        self.assert_blocked("unknown_source_sha")
        self.registry["links"][0]["source_sha256"] = H1
        self.write_registry()
        self.manifest["sources"].append("changed")
        self.write_snapshot()
        self.assert_blocked("stale_registry_binding")
        self.registry["snapshot_id"] = self.snapshot_id
        self.registry["catalog_sha256"] = self.catalog_sha
        self.write_registry()
        artifacts = self.snapshot / "artifact-hashes.json"
        self.write_file(artifacts, encoded({"catalog.json": "f" * 64,
                                            "input-manifest.json": sha((self.snapshot / "input-manifest.json").read_bytes())}))
        self.assert_blocked("stale_snapshot_artifact")

    def test_changed_input_new_receipt_old_immutable(self):
        first = catalog_links.run(self.snapshot, self.registry_path, self.output)
        old = self.output / first["run_id"]
        old_content = {p.name: p.read_bytes() for p in old.iterdir()}
        self.registry["links"].append({"source_sha256": H2, "agency_id": "agency:north",
                                       "request_id": "request:two"})
        self.write_registry()
        second = catalog_links.run(self.snapshot, self.registry_path, self.output)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual({p.name: p.read_bytes() for p in old.iterdir()}, old_content)
        self.assertEqual(len(list(self.output.iterdir())), 3)  # two runs plus writer lock

    def test_duplicate_json_keys_and_schema_rejected(self):
        self.write_file(self.registry_path, b'{"schema_version":1,"schema_version":1}')
        self.assert_blocked("duplicate_json_key")
        self.registry["extra"] = True
        self.write_registry()
        self.assert_blocked("invalid_registry_schema")
        del self.registry["extra"]
        self.registry["agencies"][0]["agency_id"] = "north"
        self.write_registry()
        self.assert_blocked("invalid_agency_id")

    def test_private_output_and_no_symlink(self):
        self.output.mkdir(mode=0o755)
        self.output.chmod(0o755)
        self.assert_blocked("unsafe_output")
        self.output.chmod(0o700)
        self.output.rmdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.output.symlink_to(elsewhere, target_is_directory=True)
        self.assert_blocked("unsafe_output")
        self.output.unlink()
        first = catalog_links.run(self.snapshot, self.registry_path, self.output)
        (self.output / first["run_id"] / "receipt.json").unlink()
        (self.output / first["run_id"] / "receipt.json").symlink_to(self.registry_path)
        self.assert_blocked("existing_output_mismatch")

    def test_existing_receipt_tamper_rejected(self):
        first = catalog_links.run(self.snapshot, self.registry_path, self.output)
        receipt = self.output / first["run_id"] / "receipt.json"
        self.write_file(receipt, b'{}\n')
        self.assert_blocked("existing_output_mismatch")

    def test_machine_output_has_no_private_path_or_raw_text(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = catalog_links.main(["--snapshot", str(self.snapshot),
                                         "--registry", str(self.registry_path),
                                         "--output", str(self.output)])
        self.assertEqual(status, 0)
        self.assertNotIn(str(self.root), out.getvalue())
        self.assertNotIn("changed", out.getvalue())
        self.registry["links"][0]["request_id"] = "request:missing"
        self.write_registry()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = catalog_links.main(["--snapshot", str(self.snapshot),
                                         "--registry", str(self.registry_path),
                                         "--output", str(self.output)])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out.getvalue()), {"status": "blocked", "code": "unknown_request_id"})
        self.assertNotIn(str(self.root), out.getvalue())

    def test_registry_race_after_read_has_fixed_code(self):
        real_read = catalog_links._read
        for race in ("symlink", "missing"):
            with self.subTest(race=race):
                self.write_registry()
                def read_then_race(path):
                    data = real_read(path)
                    if Path(path) == self.registry_path:
                        self.registry_path.unlink()
                        if race == "symlink":
                            self.registry_path.symlink_to(self.snapshot / "catalog.json")
                    return data
                out = io.StringIO()
                with mock.patch.object(catalog_links, "_read", side_effect=read_then_race), \
                        contextlib.redirect_stdout(out):
                    status = catalog_links.main(["--snapshot", str(self.snapshot),
                                                 "--registry", str(self.registry_path),
                                                 "--output", str(self.output)])
                self.assertEqual(status, 2)
                self.assertEqual(json.loads(out.getvalue()),
                                 {"status": "blocked", "code": "unsafe_or_missing_input"})
                self.registry_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
