"""Fictional captures and genuine fixed validator; no corpus or reviewer grants.

Readonly mount state is simulated in unit fixtures. Separate tests enforce its
failure, and Linux extraction uses the actual bounded native-text parser.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from campaign_tool.records.intake import local_capture
from campaign_tool.records.ledger import store, stages, original_resolution
from campaign_tool.records.ledger.occurrence_projection import project_occurrences
from campaign_tool.records.run import Pipeline, root_lock
from campaign_tool.records.runner.canonical_reconciliation import CanonicalOriginalReconciler
from tests.records import test_ledger_occurrences as projection_fixtures
from contextlib import closing


def fictional_policy(subject, size, *, marker=b"fictional approval"):
    value = {"schema": "approved-local-capture-policy-v1", "inventory_sha256": hashlib.sha256(b"fictional inventory").hexdigest(),
             "approval_sha256": hashlib.sha256(marker).hexdigest(), "originals": {subject: {
             "bytes": size, "role": "original", "scope": "in_scope", "evidence_sha256": hashlib.sha256(b"fictional role/scope evidence").hexdigest()}}}
    raw = local_capture.encoded(value)
    return local_capture.ApprovedCapturePolicy(raw, expected_sha256=local_capture.digest(raw))


class OriginalReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.helper = projection_fixtures.OccurrenceProjectionTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.import_id = self.helper.capture()
        project_occurrences(self.helper.database, self.import_id)
        self.database = self.helper.database
        self.base = self.database.parent.resolve()
        self.sha = self.helper.sha
        self.source = self.base / "fictional.txt"
        self.source.write_bytes(b"synthetic original")
        self.source.chmod(0o400)
        self.pipeline = Pipeline(self.base)
        self.policy = fictional_policy(self.sha, self.source.stat().st_size)
        self.backend = CanonicalOriginalReconciler(self.pipeline.root.sub("mail"), self.pipeline.root.intake,
                                                   self.database, policy=self.policy)
        self.identity = self.pipeline._identity()
        self.identity["run_id"] = "fictional-reconciliation"
        self.backend.start_run(self.identity)

    def readonly(self):
        return patch.object(local_capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY))

    def rows(self, sql, args=()):
        with store.ledger(self.database, readonly=True) as con:
            return [tuple(row) for row in con.execute(sql, args)]

    def enroll(self):
        with self.readonly():
            return self.backend.enroll_local_capture(self.source, original_sha256=self.sha)

    def test_initial_resolution_retains_declarations_occurrences_and_stage_history(self):
        declared = self.rows("SELECT * FROM originals")
        occurrences = self.rows("SELECT * FROM occurrences")
        events = self.rows("SELECT * FROM stage_events")
        result = self.enroll()
        self.assertEqual(result["stage_promotions"], 0)
        self.assertFalse(result["semantic_acceptance"])
        self.assertEqual(self.rows("SELECT * FROM originals"), declared)
        self.assertEqual(self.rows("SELECT * FROM occurrences WHERE acquisition_method LIKE 'captured_legacy_%'"), occurrences)
        self.assertEqual(self.rows("SELECT * FROM stage_events"), events)
        self.assertEqual(self.rows("SELECT DISTINCT status FROM stage_state"), [("pending",)])
        self.assertEqual(self.rows("SELECT count(*) FROM receipts"), [(0,)])
        original = self.pipeline._original(self.sha)
        self.assertEqual(original["scope"], "in_scope")
        self.assertEqual(original["role"], "original")
        self.assertEqual(original["resolution_sha256"], result["resolution_sha256"])
        self.assertEqual(original["storage_path"], str(self.pipeline.root.intake / "blobs" / self.sha))
        self.assertEqual(self.rows("PRAGMA user_version"), [(1,)])

    def test_enrollment_replay_no_extra_resolution_occurrence_or_stage(self):
        first = self.enroll()
        with store.ledger(self.database, readonly=True) as con:
            before = list(con.iterdump())
        second = self.enroll()
        self.assertTrue(second["reused"])
        self.assertEqual(second["resolution_sha256"], first["resolution_sha256"])
        with store.ledger(self.database, readonly=True) as con:
            self.assertEqual(list(con.iterdump()), before)

    def test_explicit_fixed_preserver_acceptance_then_exact_replay(self):
        result = self.enroll()
        accepted = self.backend.accept(self.sha)
        self.assertFalse(accepted["test_only"])
        self.assertFalse(accepted["semantic_acceptance"])
        self.assertEqual(self.rows("SELECT stage,status FROM stage_state WHERE stage='preserve'"), [("preserve", "done")])
        self.assertEqual(self.rows("SELECT DISTINCT status FROM stage_state WHERE stage!='preserve'"), [("pending",)])
        with store.ledger(self.database, readonly=True) as con:
            before = list(con.iterdump())
            content = json.loads(bytes(con.execute("SELECT a.payload FROM stage_content h JOIN stage_artifacts a "
                "ON a.sha256=h.content_sha256 WHERE h.stage='preserve'").fetchone()[0]))
        self.assertEqual(content["resolution_sha256"], result["resolution_sha256"])
        self.assertEqual(content["occurrence_ids"], [result["occurrence_id"]])
        self.assertTrue(self.backend.accept(self.sha)["reused"])
        with store.ledger(self.database, readonly=True) as con:
            self.assertEqual(list(con.iterdump()), before)

    def test_no_readonly_bypass_and_no_implicit_acceptance(self):
        with patch.object(local_capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=0)):
            with self.assertRaisesRegex(ValueError, "readonly_source"):
                self.backend.enroll_local_capture(self.source, original_sha256=self.sha)
        with self.assertRaisesRegex(ValueError, "verified_local_capture_required"):
            self.backend.accept(self.sha)
        self.assertEqual(self.rows("SELECT count(*) FROM receipts"), [(0,)])

    def test_hash_only_and_unknown_policy_inventory_cannot_resolve(self):
        other = fictional_policy("f"*64, self.source.stat().st_size)
        backend = CanonicalOriginalReconciler(self.backend.mail_root, self.backend.output, self.database, policy=other)
        backend.run_identity = self.identity
        with self.readonly(), self.assertRaisesRegex(ValueError, "approved_inventory"):
            backend.enroll_local_capture(self.source, original_sha256=self.sha)
        self.assertEqual(self.rows("SELECT role,scope,storage_path FROM originals"), [("unresolved", "unresolved", None)])

    def test_changed_source_refuses_without_modifying_original_or_stages(self):
        self.source.chmod(0o600)
        self.source.write_bytes(b"different original")
        with self.readonly(), self.assertRaisesRegex(ValueError, "source_changed"):
            self.backend.enroll_local_capture(self.source, original_sha256=self.sha)
        self.assertEqual(self.rows("SELECT count(*) FROM receipts"), [(0,)])
        self.assertEqual(self.rows("SELECT DISTINCT status FROM stage_state"), [("pending",)])

    def test_symlink_and_hardlink_source_refuse(self):
        link = self.base / "linked.txt"
        link.symlink_to(self.source)
        with self.readonly(), self.assertRaises(ValueError):
            self.backend.enroll_local_capture(link, original_sha256=self.sha)
        link.unlink()
        os.link(self.source, link)
        with self.readonly(), self.assertRaisesRegex(ValueError, "unsafe_local_capture_source"):
            self.backend.enroll_local_capture(self.source, original_sha256=self.sha)

    def test_changed_policy_or_capture_cannot_replace_initial_binding(self):
        self.enroll()
        other = fictional_policy(self.sha, self.source.stat().st_size, marker=b"different fictional approval")
        backend = CanonicalOriginalReconciler(self.backend.mail_root, self.backend.output, self.database, policy=other)
        backend.run_identity = self.identity
        with self.readonly(), self.assertRaisesRegex(ValueError, "original_resolution_conflict"):
            backend.enroll_local_capture(self.source, original_sha256=self.sha)
        self.assertEqual(self.rows("SELECT count(*) FROM original_resolutions"), [(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM occurrences WHERE acquisition_method='verified_local_capture'"), [(1,)])

    def test_resolution_and_original_are_immutable_and_unissued_write_refuses(self):
        self.enroll()
        with store.ledger(self.database) as con:
            for sql in ("UPDATE originals SET scope='in_scope'", "DELETE FROM originals",
                        "UPDATE original_resolutions SET policy_sha256='wrong'", "DELETE FROM original_resolutions",
                        "INSERT OR REPLACE INTO original_resolutions SELECT * FROM original_resolutions"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.Error):
                    con.execute(sql)
                con.rollback()

    def test_missing_capture_or_policy_bytes_blocks_effective_reads_and_acceptance(self):
        result = self.enroll()
        receipt = self.backend.output / "local-capture-receipts" / result["capture_receipt_sha256"]
        receipt.unlink()
        with self.assertRaises(OSError):
            self.pipeline._original(self.sha)
        with self.assertRaises(OSError):
            self.backend.accept(self.sha)
        self.assertEqual(self.rows("SELECT count(*) FROM receipts"), [(0,)])

    def test_bound_declaration_cannot_be_claimed_as_genuine_acquisition(self):
        self.enroll()
        self.backend.accept(self.sha)
        context = self.backend._existing_preservation_context(self.sha)
        declared = self.rows("SELECT id FROM occurrences WHERE acquisition_method LIKE 'captured_legacy_%'")[0][0]
        context["preservation_evidence"]["occurrence_ids"] = [declared]
        self.assertFalse(self.backend._validator(context))

    def test_captured_declaration_tamper_is_not_ignored(self):
        self.enroll()
        with store.ledger(self.database) as con:
            con.execute("UPDATE occurrences SET source_ref='{}' WHERE acquisition_method LIKE 'captured_legacy_%'")
            con.commit()
        with self.assertRaisesRegex(ValueError, "local_capture_domain_validation_failed"):
            self.backend.accept(self.sha)
        self.assertEqual(self.rows("SELECT status FROM stage_state WHERE stage='preserve'"), [("pending",)])

    def test_relabeling_projected_declaration_cannot_evade_validation(self):
        self.enroll()
        with store.ledger(self.database) as con:
            con.execute("UPDATE occurrences SET acquisition_method='fictional-unclassified',source_ref='{}' "
                        "WHERE acquisition_method LIKE 'captured_legacy_%'")
            con.commit()
        with self.assertRaisesRegex(ValueError, "local_capture_domain_validation_failed"):
            self.backend.accept(self.sha)
        self.assertEqual(self.rows("SELECT count(*) FROM receipts"), [(0,)])
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name='stage_claims'"), [])
        self.assertEqual(self.rows("SELECT status FROM stage_state WHERE stage='preserve'"), [("pending",)])

    def test_changed_subject_cannot_hide_owned_projection(self):
        self.enroll()
        other, _, _ = self.secondary(b"fictional alternate subject")
        with store.ledger(self.database) as con:
            con.execute("UPDATE occurrences SET original_sha256=?,acquisition_method='fictional-unclassified' "
                        "WHERE acquisition_method LIKE 'captured_legacy_%'", (other,))
            con.commit()
        with self.assertRaisesRegex(ValueError, "local_capture_domain_validation_failed"):
            self.backend.accept(self.sha)
        self.assertEqual(self.rows("SELECT count(*) FROM receipts"), [(0,)])
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name='stage_claims'"), [])

    def test_synthetic_done_receipt_cannot_return_functional_replay(self):
        enrolled = self.enroll()
        runner = stages.testing_runner(self.database, run_id=self.identity["run_id"], owner="fictional-holder",
            validators={"preserve": self.backend._validator}, version="reconciliation-synthetic-replay",
            engine_version=self.identity["runtime"]["version"], config_sha256=self.identity["config_sha256"])
        evidence = {"schema": "preservation-evidence-v1", "original_sha256": self.sha,
            "byte_length": self.source.stat().st_size, "storage_ref": str(self.backend.output / "blobs" / self.sha),
            "verification_receipt_sha256": enrolled["capture_receipt_sha256"],
            "occurrence_ids": [enrolled["occurrence_id"]], "resolution_sha256": enrolled["resolution_sha256"]}
        head = runner.set_preservation_evidence(self.sha, evidence, author_id="fictional-holder", tier="A")
        claim = runner.claim(self.sha, "preserve", ttl_seconds=300)
        packet = {"schema": "ledger-stage-receipt-v1", "subject_sha256": self.sha, "stage": "preserve",
            "content_sha256": head["content_sha256"], "author_id": "fictional-holder", "tier": "A",
            "reviewer_id": "fictional-holder", "role": "preserver", "verdict": "pass",
            "coverage": {"denominator": {"kind": "bytes", "total": self.source.stat().st_size},
                "covered": self.source.stat().st_size, "scope": "selected"},
            "locators": ["cas:" + self.sha], "rationale": "Explicit synthetic authority regression.",
            "model_or_tool": "fictional-test-only", "created_at_tz": "2026-10-12T00:00:00+00:00",
            "input_hashes": {"original": self.sha}}
        synthetic = runner.promote(self.sha, "preserve", json.dumps(packet).encode(), claim_id=claim["claim_id"])
        self.assertTrue(synthetic["test_only"])
        before = self.rows("SELECT * FROM stage_state")
        with self.assertRaisesRegex(ValueError, "functional_preservation_authority_required"):
            self.backend.accept(self.sha)
        self.assertEqual(self.rows("SELECT * FROM stage_state"), before)

    def test_functional_replay_keeps_test_only_false(self):
        self.enroll()
        first = self.backend.accept(self.sha)
        second = self.backend.accept(self.sha)
        self.assertFalse(second["test_only"])
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])

    def test_cached_functional_receipt_requires_current_code_binding(self):
        self.enroll()
        self.backend.accept(self.sha)
        with patch.object(self.backend, "_binding_context", return_value=["fictional-different-code"]):
            with self.assertRaisesRegex(ValueError, "preservation_replay_profile_failed"):
                self.backend.accept(self.sha)

    def test_format_routing_distinguishes_declaration_and_current_capture(self):
        self.enroll()
        self.backend.accept(self.sha)
        self.assertEqual(self.pipeline._form(self.sha, self.pipeline._original(self.sha)), "txt")

    def test_format_routing_refuses_relabelled_projection(self):
        self.enroll()
        self.backend.accept(self.sha)
        with store.ledger(self.database) as con:
            con.execute("UPDATE occurrences SET acquisition_method='verified_local_document_drop' "
                        "WHERE acquisition_method LIKE 'captured_legacy_%'")
            con.commit()
        with self.assertRaises(ValueError):
            self.pipeline._form(self.sha, self.pipeline._original(self.sha))

    def test_effective_original_supports_plain_sqlite_without_mutating_row_factory(self):
        with closing(sqlite3.connect(self.database)) as con, con:
            self.assertIsNone(con.row_factory)
            self.assertEqual(store.effective_original(con, self.sha)["role"], "unresolved")
            self.assertIsNone(con.row_factory)
        result = self.enroll()
        with closing(sqlite3.connect(self.database)) as con, con:
            resolved = store.effective_original(con, self.sha)
            self.assertEqual(resolved["resolution_sha256"], result["resolution_sha256"])
            self.assertEqual(resolved["role"], "original")
            self.assertIsNone(con.row_factory)
            self.assertIsInstance(con.execute("SELECT sha256 FROM originals").fetchone(), tuple)

    def test_shared_root_lock_rejects_concurrent_enrollment(self):
        with root_lock(self.base), self.readonly(), self.assertRaisesRegex(Exception, "root_locked"):
            self.backend.enroll_local_capture(self.source, original_sha256=self.sha)

    def secondary(self, data, *, role="original", scope="in_scope"):
        subject = hashlib.sha256(data).hexdigest()
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)", (subject,len(data),None,
                "2000-01-01T00:00:00Z",role,scope,str(self.backend.output/"blobs"/subject),"pending",None,"{}"))
            for stage in store.STAGES:
                con.execute("INSERT INTO stage_state VALUES(?,?,'pending',NULL,?,?,?,?)",
                    (subject,stage,"fictional", "2000-01-01T00:00:00Z",self.identity["run_id"],"fictional candidate"))
            con.commit()
        source = self.base / "secondary.txt"
        source.write_bytes(data); source.chmod(0o400)
        backend = CanonicalOriginalReconciler(self.backend.mail_root,self.backend.output,self.database,
            policy=fictional_policy(subject,len(data)))
        backend.run_identity = self.identity
        return subject,source,backend

    def test_existing_bound_pending_original_uses_capture_without_resolution(self):
        subject,source,backend = self.secondary(b"fictional bound original")
        with self.readonly():
            result = backend.enroll_local_capture(source,original_sha256=subject)
        self.assertIsNone(result["resolution_sha256"])
        self.assertEqual(self.rows("SELECT DISTINCT status FROM stage_state WHERE original_sha256=?",(subject,)),[("pending",)])
        self.assertFalse(backend.accept(subject)["test_only"])
        self.assertEqual(self.rows("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'",(subject,)),[("done",)])

    def test_excluded_scope_cannot_be_converted_by_approved_byte_identity(self):
        subject,source,backend = self.secondary(b"fictional excluded original",scope="out_of_scope")
        with self.readonly(),self.assertRaisesRegex(ValueError,"original_out_of_scope"):
            backend.enroll_local_capture(source,original_sha256=subject)
        self.assertEqual(self.rows("SELECT scope FROM originals WHERE sha256=?",(subject,)),[("out_of_scope",)])
        self.assertEqual(self.rows("SELECT count(*) FROM receipts"),[(0,)])

    def test_cached_unaccepted_content_requires_explicit_reconciliation(self):
        runner = stages.testing_runner(self.database,run_id=self.identity["run_id"],owner="fictional-holder",
            validators={},version="reconciliation-cache-test",engine_version=self.identity["runtime"]["version"],
            config_sha256=self.identity["config_sha256"])
        runner.set_content(self.sha,"preserve",b"synthetic original",author_id="fictional-holder",tier="A")
        with self.readonly(),self.assertRaisesRegex(ValueError,"requires_uncached_original"):
            self.backend.enroll_local_capture(self.source,original_sha256=self.sha)
        self.assertEqual(self.rows("SELECT role,scope,storage_path FROM originals"),[("unresolved","unresolved",None)])
        self.assertEqual(self.rows("SELECT count(*) FROM occurrences WHERE acquisition_method='verified_local_capture'"),[(0,)])

    def test_transaction_failure_retains_no_partial_occurrence_or_binding(self):
        from campaign_tool.records.ledger import original_resolution as resolution
        before = self.rows("SELECT * FROM occurrences")
        with self.readonly(),patch.object(resolution,"append_initial",side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.backend.enroll_local_capture(self.source,original_sha256=self.sha)
        self.assertEqual(self.rows("SELECT * FROM occurrences"),before)
        self.assertEqual(self.rows("SELECT count(*) FROM receipts"),[(0,)])
        self.assertEqual(self.enroll()["stage_promotions"],0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "bounded extraction runtime is Linux")
    def test_resolved_native_text_extracts_through_installed_callback(self):
        enrolled = self.enroll()
        self.backend.accept(self.sha)
        self.backend.finish_run(self.identity, "slice_completed", {"functional_preservation": 1})
        report = self.pipeline.run()
        self.assertEqual(self.rows("SELECT status FROM stage_state WHERE stage='extract'"), [("done",)], report)
        with store.ledger(self.database, readonly=True) as con:
            manifest = json.loads(con.execute("SELECT manifest_json FROM extraction_adapter_imports").fetchone()[0])
        self.assertEqual(manifest["original_resolution_sha256"], enrolled["resolution_sha256"])
        self.assertNotEqual(self.rows("SELECT status FROM stage_state WHERE stage='review'"), [("done",)])
