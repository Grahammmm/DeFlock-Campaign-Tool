"""Bounded synthetic acceptance, recovery, filters and composed-package checks."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

from tests.records import test_ledger_catalog as fixtures
store, catalog_links, H1, H2 = fixtures.store, fixtures.catalog_links, fixtures.H1, fixtures.H2
from campaign_tool.records import ledger_catalog as catalog
from campaign_tool.records import ledger_catalog_acceptance as acceptance


@unittest.skipUnless(store is not None and catalog_links is not None, "WP1/PR19 dependency overlays required")
class SnapshotAcceptanceTests(unittest.TestCase):
    setUp = fixtures.CanonicalCatalogTests.setUp
    join = fixtures.CanonicalCatalogTests.join

    def finalized(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET status='completed',ended_at='2026-01-03T00:00:00+00:00',config_sha256=? WHERE run_id='synthetic'", (H2,))

    def prepare(self, **kw):
        self.finalized()
        result = catalog.build(self.db, self.root, run_id="synthetic", **kw)
        catalog.export(result, self.output)
        payloads = catalog.artifact_payloads(result)
        args = {"run_id": "synthetic", "expected_catalog_sha256": catalog.digest(payloads["catalog.json"]),
                "expected_manifest_sha256": catalog.digest(payloads["artifact-hashes.json"])}
        return result, args

    def accept(self, result, args):
        return acceptance.accept_snapshot(self.db, self.root, self.output, result["snapshot_id"], **args)

    def test_transactional_pointer_exact_bindings(self):
        result, args = self.prepare()
        receipt = self.accept(result, args)
        self.assertFalse(receipt["reused"])
        self.assertFalse(receipt["receipt"]["publication_ready"])
        self.assertFalse(receipt["receipt"]["document_review_approval"])
        self.assertEqual(receipt["receipt"]["stage_promotions"], 0)
        self.assertEqual(acceptance.read_accepted(self.output)["receipt_sha256"], receipt["receipt_sha256"])
        self.assertEqual((self.output / "accepted-snapshots.sqlite").stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM stage_state WHERE status='pending'").fetchone()[0], 14)

    def test_replay_same_receipt_one_event(self):
        result, args = self.prepare()
        first, second = self.accept(result, args), self.accept(result, args)
        self.assertTrue(second["reused"])
        self.assertEqual(first["receipt"], second["receipt"])
        with sqlite3.connect(self.output / "accepted-snapshots.sqlite") as con:
            self.assertEqual(con.execute("SELECT count(*) FROM snapshot_events").fetchone()[0], 1)

    def test_commit_then_response_failure_replay_recovers(self):
        result, args = self.prepare()
        with mock.patch.object(acceptance, "_after_commit", side_effect=RuntimeError("synthetic response loss")):
            with self.assertRaises(RuntimeError):
                self.accept(result, args)
        replay = self.accept(result, args)
        self.assertTrue(replay["reused"])
        with sqlite3.connect(self.output / "accepted-snapshots.sqlite") as con:
            self.assertEqual(con.execute("SELECT count(*) FROM snapshot_events").fetchone()[0], 1)

    def test_directory_sync_failure_replay_resyncs(self):
        result, args = self.prepare()
        with mock.patch.object(acceptance, "_sync_root", side_effect=OSError("synthetic directory sync")):
            with self.assertRaises(OSError):
                self.accept(result, args)
        with mock.patch.object(acceptance, "_sync_root", wraps=acceptance._sync_root) as sync:
            self.assertTrue(self.accept(result, args)["reused"])
            sync.assert_called_once_with(self.output)

    def test_precommit_failure_rolls_back_pointer_and_receipt(self):
        result, args = self.prepare()
        with mock.patch.object(catalog, "build", side_effect=RuntimeError("synthetic precommit")):
            with self.assertRaises(RuntimeError):
                self.accept(result, args)
        self.assertIsNone(acceptance.read_accepted(self.output))
        self.assertFalse(self.accept(result, args)["reused"])

    def test_stale_source_fails_closed(self):
        result, args = self.prepare()
        self.join("after-snapshot")
        with self.assertRaisesRegex(catalog.CatalogError, "stale_source_snapshot"):
            self.accept(result, args)
        self.assertIsNone(acceptance.read_accepted(self.output))

    def test_wrong_exact_hash_and_run_fail(self):
        result, args = self.prepare()
        wrong = dict(args, expected_catalog_sha256=H1)
        with self.assertRaisesRegex(catalog.CatalogError, "artifact_hash_mismatch"):
            self.accept(result, wrong)
        wrong = dict(args, run_id="another-run")
        with self.assertRaisesRegex(catalog.CatalogError, "run_binding_mismatch"):
            self.accept(result, wrong)

    def test_unfinalized_run_not_accepted(self):
        result = catalog.build(self.db, self.root, run_id="synthetic")
        catalog.export(result, self.output)
        payloads = catalog.artifact_payloads(result)
        args = {"run_id": "synthetic", "expected_catalog_sha256": catalog.digest(payloads["catalog.json"]),
                "expected_manifest_sha256": catalog.digest(payloads["artifact-hashes.json"])}
        with self.assertRaisesRegex(catalog.CatalogError, "run_not_finalized_or_bound"):
            self.accept(result, args)

    def test_missing_run_binding_not_accepted(self):
        result = catalog.build(self.db, self.root)
        catalog.export(result, self.output)
        payloads = catalog.artifact_payloads(result)
        args = {"run_id": "synthetic", "expected_catalog_sha256": catalog.digest(payloads["catalog.json"]),
                "expected_manifest_sha256": catalog.digest(payloads["artifact-hashes.json"])}
        with self.assertRaisesRegex(catalog.CatalogError, "run_binding_mismatch"):
            self.accept(result, args)

    def test_pointer_compare_and_swap_and_no_old_reactivation(self):
        old, old_args = self.prepare()
        self.accept(old, old_args)
        self.join("new-evidence")
        new, new_args = self.prepare()
        with self.assertRaisesRegex(catalog.CatalogError, "accepted_pointer_changed"):
            self.accept(new, new_args)
        new_args["expected_current_snapshot_id"] = old["snapshot_id"]
        self.assertFalse(self.accept(new, new_args)["reused"])
        old_args["expected_current_snapshot_id"] = new["snapshot_id"]
        with self.assertRaisesRegex(catalog.CatalogError, "superseded_snapshot_cannot_reactivate"):
            self.accept(old, old_args)
        self.assertEqual(acceptance.read_accepted(self.output)["receipt"]["snapshot_id"], new["snapshot_id"])

    def test_tampered_filter_or_counts_fails_before_accept(self):
        for filename in ("counts.json", "index.html"):
            result, args = self.prepare()
            path = self.output / result["snapshot_id"] / filename
            before = path.read_bytes()
            path.write_bytes(b"synthetic tamper")
            with self.assertRaisesRegex(catalog.CatalogError, "artifact_bytes_mismatch"):
                self.accept(result, args)
            path.write_bytes(before)
        self.assertFalse(self.accept(result, args)["reused"])

    def test_schema_change_rejected_even_rehashed(self):
        result, args = self.prepare()
        changed = dict(result, schema_version=99)
        changed["snapshot_id"] = catalog.digest(catalog.encoded({k: v for k, v in changed.items() if k != "snapshot_id"}))
        catalog.export(changed, self.output)
        payloads = catalog.artifact_payloads(changed)
        args["expected_catalog_sha256"] = catalog.digest(payloads["catalog.json"])
        args["expected_manifest_sha256"] = catalog.digest(payloads["artifact-hashes.json"])
        with self.assertRaisesRegex(catalog.CatalogError, "snapshot_schema_mismatch"):
            self.accept(changed, args)

    def test_accepted_artifact_tamper_detected_on_read(self):
        result, args = self.prepare()
        self.accept(result, args)
        (self.output / result["snapshot_id"] / "index.html").write_bytes(b"changed")
        with self.assertRaisesRegex(catalog.CatalogError, "artifact_bytes_mismatch"):
            acceptance.read_accepted(self.output)

    def test_unknown_filters_and_metrics_explicit(self):
        result, args = self.prepare()
        filters = catalog.filter_pages(result)
        self.assertEqual(set(filters.values()), {("agency", None), ("type", None), ("year", None)})
        self.assertEqual(result["metrics"]["unknown_agency"], 2)
        self.assertEqual(result["metrics"]["unknown_date_range"], 2)
        self.assertIsNone(result["metrics"]["weekly_throughput"])
        for name in filters:
            self.assertTrue((self.output / result["snapshot_id"] / name).is_file())

    def test_filters_select_without_changing_canonical_counts(self):
        self.join("north")
        overlays = {H1: {"doc_type": "policy", "date_from": "2026-01-01", "date_to": "2026-01-02",
                         "evidence": [{"source_sha256": H1, "locator": "page:1"}]}}
        result, _ = self.prepare(overlays=overlays)
        board = catalog.render_board(result, selection=("type", "policy")).decode()
        self.assertIn("Visible cards: 1", board)
        self.assertIn('id="doc-' + H1 + '"', board)
        self.assertNotIn('id="doc-' + H2 + '"', board)
        self.assertEqual(result["counts"]["originals"], 2)
        self.assertEqual(result["cards"][0]["metadata_status"] in ("candidate", "unknown"), True)

    def test_filter_bound_no_partial_export(self):
        self.finalized()
        result = catalog.build(self.db, self.root, run_id="synthetic")
        with mock.patch.object(catalog, "MAX_FILTER_PAGES", 1):
            with self.assertRaisesRegex(catalog.CatalogError, "filter_page_limit"):
                catalog.export(result, self.output)
        self.assertFalse((self.output / result["snapshot_id"]).exists())

    def test_package_composition_without_host_path_discovery(self):
        composed = self.root / "composed"
        composed.mkdir(mode=0o700)
        package = composed / "campaign_tool"
        shutil.copytree(Path(catalog.__file__).parents[1], package,
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(Path(store.__file__).parent, package / "records" / "ledger",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(catalog_links.__file__, package / "records" / "catalog_links.py")
        probe = "import sys,json;sys.path.insert(0," + repr(str(composed)) + ");from campaign_tool.records.ledger_catalog import dependency_contract;from campaign_tool.records.ledger_catalog_acceptance import accept_snapshot;print(json.dumps(dependency_contract()))"
        result = subprocess.run([sys.executable, "-I", "-c", probe], cwd=self.root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        contract = json.loads(result.stdout)
        self.assertEqual(contract["wp5_schema_version"], 2)
        self.assertEqual(contract["pr19_source_sha256"], catalog.digest(Path(catalog_links.__file__).read_bytes()))
        self.assertEqual(contract["wp1_counts_api"], "query_counts")

    def test_dependency_contract_preserves_pr19(self):
        contract = catalog.dependency_contract()
        self.assertEqual(contract["pr19_schema_version"], 1)
        self.assertFalse(contract["source_copied_into_wp5"])


if __name__ == "__main__":
    unittest.main()
