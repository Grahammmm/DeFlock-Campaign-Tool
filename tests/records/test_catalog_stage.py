"""Synthetic catalog-only adapter composition; no private data or real promotions."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import campaign_tool.records
for root in json.loads(os.environ.get("RECORDS_TEST_DEPENDENCIES", "[]")):
    campaign_tool.records.__path__.append(str(Path(root) / "campaign_tool" / "records"))
from campaign_tool.records import catalog_stage as module
try:
    from campaign_tool.records.ledger import store, stages
except ImportError:
    store = stages = None

sha = lambda value: hashlib.sha256(value).hexdigest()


@unittest.skipUnless(store is not None, "WP1 dependency required")
class CatalogStageTests(unittest.TestCase):
    def setUp(self):
        # Shared by non-inheriting drift tests: class decorators do not transfer.
        if store is None or stages is None:
            self.skipTest("WP1 dependency required")
        self.temp = tempfile.TemporaryDirectory(prefix="catalog-stage-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.database = self.root / "ledger.sqlite"
        store.initialize(self.database)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir(mode=0o700)
        self.original = b"Synthetic policy title. Agency North and Agency South."
        self.subject = sha(self.original)
        self.config = sha(b"synthetic configuration")
        self.run_id = "synthetic-" + self.subject[:8]
        self.write("unit.txt", self.original)
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (self.run_id, "stage-runner", store.now(), None, "synthetic-engine", None, self.config, None, "running", "{}"))
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (self.subject, len(self.original), "text/plain", store.now(), "record", "in_scope",
                         None, "captured", "text", "{}"))
            for stage in stages.STAGES:
                con.execute("INSERT INTO stage_state VALUES(?,?,?,NULL,?,?,?,NULL)",
                            (self.subject, stage, "pending", "unassigned", store.now(), self.run_id))
            con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)",
                        ("delivery", self.subject, "local", "synthetic", None, None, "fixture", "{}"))
            con.execute("INSERT INTO units VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ("unit-one", self.subject, "synthetic-text-parser", "1", "page:1", "text",
                         sha(self.original), str(self.artifacts / "unit.txt"), "ok", None, "{}"))
            for agency in ("north", "south"):
                con.execute("INSERT INTO agencies VALUES(?,?,NULL,NULL)", ("agency:" + agency, "Synthetic " + agency))
                con.execute("INSERT INTO requests VALUES(?,?,?,NULL,NULL,NULL,NULL,?)",
                            ("request:" + agency, "agency:" + agency, "SYN", "open"))
            con.commit()
        self.adapter = module.CatalogAdapter(self.database, self.artifacts)

        def verify(context):
            return context["content"] == self.original if context["stage"] == "preserve" else context["content"] == b"synthetic extraction"

        self.runner = stages.testing_runner(self.database, run_id=self.run_id, owner="synthetic-cataloger",
            engine_version="synthetic-engine", config_sha256=self.config,
            validators={"preserve": verify, "extract": verify, "catalog": self.adapter.validate},
            version="catalog-fixture-v1")
        self.catalog = module.CatalogStage(self.adapter, self.runner)
        self.card = {"schema": "catalog-card-evidence-v1", "subject_sha256": self.subject,
                     "metadata": {}, "field_support": {},
                     "supports": [{"unit_id": "unit-one", "source_sha256": self.subject, "locator": "page:1",
                                   "artifact_sha256": sha(self.original), "quote": "Synthetic policy title."}]}
        self.path = self.artifacts / "card.json"

    def write(self, name, data):
        path = self.artifacts / name
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    def save(self):
        raw = module.catalog.encoded(self.card)
        self.write("card.json", raw)
        return sha(raw)

    def prepare(self):
        return self.adapter.prepare(self.path, self.save())

    def advance_prerequisites(self):
        for stage, content in (("preserve", self.original), ("extract", b"synthetic extraction")):
            registered = self.runner.set_content(self.subject, stage, content, author_id="synthetic-author", tier="A")
            claim = self.runner.claim(self.subject, stage)
            inputs = {"original": self.subject}
            if stage == "extract":
                with store.ledger(self.database, readonly=True) as con:
                    inputs["preserve"] = con.execute("SELECT receipt_sha256 FROM stage_state WHERE stage='preserve'").fetchone()[0]
            packet = {"schema": "ledger-stage-receipt-v1", "subject_sha256": self.subject, "stage": stage,
                      "content_sha256": registered["content_sha256"], "tier": "A", "author_id": "synthetic-author",
                      "reviewer_id": "synthetic-cataloger", "role": stages.ROLES[stage], "verdict": "pass",
                      "coverage": {"denominator": {"kind": "items", "total": 1}, "covered": 1, "scope": "selected"},
                      "locators": ["page:1"], "rationale": "Synthetic byte check", "model_or_tool": "synthetic",
                      "created_at_tz": store.now(), "input_hashes": inputs, "reviews": []}
            self.runner.promote(self.subject, stage, module.catalog.encoded(packet), claim_id=claim["claim_id"])

    def process(self):
        return self.catalog.process(self.path, self.save(), author_id="synthetic-author")

    def test_schema_only_card_stays_pending(self):
        self.card["supports"] = []
        result = self.process()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "actual_evidence_required")
        self.assertFalse(result["canonical_stage_changed"])
        with store.ledger(self.database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT status FROM stage_state WHERE stage='catalog'").fetchone()[0], "pending")

    def test_pending_extraction_cannot_accept_evidence_schema(self):
        result = self.process()
        self.assertEqual(result["reason"], "accepted_extraction_receipt_required")
        with store.ledger(self.database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM stage_content WHERE stage='catalog'").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT status FROM stage_state WHERE stage='catalog'").fetchone()[0], "pending")

    def test_card_receipt_keeps_exact_immutable_bytes(self):
        self.advance_prerequisites()
        self.process()
        old_bytes = self.path.read_bytes()
        old = self.artifacts / "catalog-card-artifacts" / (sha(old_bytes) + ".json")
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertEqual(old.stat().st_mode & 0o777, 0o400)
        self.card["metadata"] = {"title": "Synthetic policy title.", "evidence": [{"source_sha256": self.subject, "locator": "page:1"}]}
        self.card["field_support"] = {"title": ["unit-one"]}
        self.process()
        self.assertEqual(old.read_bytes(), old_bytes)

    def test_card_reformatting_keeps_classification_hash(self):
        self.advance_prerequisites()
        first = self.process()
        raw = json.dumps(self.card, indent=3).encode()
        self.write("reformatted.json", raw)
        second = self.catalog.process(self.artifacts / "reformatted.json", sha(raw), author_id="synthetic-author")
        self.assertTrue(second["reused"])
        self.assertEqual(first["content_sha256"], second["content_sha256"])

    def test_unknowns_explicit_with_actual_primary_evidence(self):
        envelope = self.prepare()["envelope"]
        self.assertEqual(envelope["agency_status"], "unknown")
        self.assertIsNone(envelope["metadata"]["date_from"])
        self.assertIn("date_from", envelope["unknown_reasons"])
        self.assertEqual(envelope["metadata_status"], "candidate")
        self.assertFalse(envelope["review_complete"])

    def test_catalog_only_synthetic_stage_composition(self):
        self.advance_prerequisites()
        result = self.process()
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["test_only"])
        self.assertTrue(result["promoted"])
        with store.ledger(self.database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT status FROM stage_state WHERE stage='review'").fetchone()[0], "pending")
            self.assertEqual(con.execute("SELECT status FROM stage_state WHERE stage='privacy'").fetchone()[0], "pending")
        self.assertEqual(stages.query_counts(self.database)["verified_seven_stage_complete"], 0)

    def test_stable_envelope_does_not_include_global_counts_status_or_run(self):
        initial = self.prepare()
        self.advance_prerequisites()
        self.process()
        after = self.prepare()
        self.assertEqual(initial["content"], after["content"])
        self.assertNotIn("counts", after["envelope"])
        self.assertNotIn("run_id", after["envelope"])
        self.assertNotIn("stages", after["envelope"])

    def test_replay_stable_content_without_claim_or_new_receipt(self):
        self.advance_prerequisites()
        first = self.process()
        second = self.process()
        self.assertTrue(second["reused"])
        self.assertFalse(second["promoted"])
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])

    def test_card_capture_interruption_leaves_no_partial_published_artifact(self):
        self.advance_prerequisites()
        saved = self.save()
        with mock.patch.object(module.os, "link", side_effect=OSError("synthetic interruption")):
            with self.assertRaises(OSError):
                self.catalog.process(self.path, saved, author_id="synthetic-author")
        destination = self.artifacts / "catalog-card-artifacts" / (saved + ".json")
        self.assertFalse(destination.exists())
        self.assertEqual(self.process()["status"], "done")
        self.assertEqual(destination.read_bytes(), self.path.read_bytes())

    def test_foreign_active_catalog_lease_not_invalidated(self):
        self.advance_prerequisites()
        prepared = self.prepare()
        other = stages.testing_runner(self.database, run_id=self.run_id, owner="other-owner",
            engine_version="synthetic-engine", config_sha256=self.config,
            validators={"preserve": self.runner.validators["preserve"][1],
                        "extract": self.runner.validators["extract"][1], "catalog": self.adapter.validate},
            version="catalog-fixture-v1")
        other.set_content(self.subject, "catalog", prepared["content"], author_id="synthetic-author", tier="A")
        old_claim = other.claim(self.subject, "catalog")
        self.card["metadata"] = {"title": "Synthetic policy title.", "evidence": [{"source_sha256": self.subject, "locator": "page:1"}]}
        self.card["field_support"] = {"title": ["unit-one"]}
        self.assertEqual(self.process()["reason"], "catalog_lease_owned_elsewhere")
        with store.ledger(self.database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT owner FROM work_leases WHERE stage='catalog'").fetchone()[0], "other-owner")
            self.assertEqual(con.execute("SELECT claim_id FROM stage_claims WHERE stage='catalog' ORDER BY leased_at DESC LIMIT 1").fetchone()[0], old_claim["claim_id"])

    def test_malformed_untrusted_metadata_is_a_gap_not_job_crash(self):
        self.card["metadata"] = {"doc_type": ["not", "a", "scalar"]}
        result = self.process()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "invalid_card_or_support_structure")
        self.assertFalse(result["canonical_stage_changed"])

    def test_receipt_locators_include_source_identity(self):
        prepared = self.prepare()
        self.assertEqual(prepared["locators"], [self.subject + ":page:1"])

    def test_primary_quote_must_exist(self):
        self.card["supports"][0]["quote"] = "invented phrase"
        self.assertEqual(self.process()["reason"], "quote_not_in_source")

    def test_artifact_and_unit_hash_required(self):
        self.write("unit.txt", b"changed bytes")
        self.assertEqual(self.process()["reason"], "support_artifact_hash_mismatch")

    def test_exact_card_hash_required(self):
        self.save()
        with self.assertRaisesRegex(module.CatalogStageError, "card_artifact_hash_mismatch"):
            self.adapter.prepare(self.path, sha(b"wrong artifact"))

    def test_unit_locator_must_match_canonical(self):
        self.card["supports"][0]["locator"] = "page:99"
        self.assertEqual(self.process()["reason"], "support_locator_binding")

    def test_unit_parser_provenance_required(self):
        with store.ledger(self.database) as con:
            con.execute("UPDATE units SET parser_version=NULL")
            con.commit()
        self.assertEqual(self.process()["reason"], "support_unit_unverified")

    def test_known_field_requires_support(self):
        self.card["metadata"] = {"title": "Synthetic policy title.", "evidence": [{"source_sha256": self.subject, "locator": "page:1"}]}
        self.assertEqual(self.process()["reason"], "known_field_without_support")
        self.card["field_support"] = {"title": ["unit-one"]}
        self.assertEqual(self.prepare()["envelope"]["metadata"]["title"], "Synthetic policy title.")

    def test_low_value_remains_proposed(self):
        self.card["metadata"] = {"low_value_reason": "Possible branding", "value_score": 0, "value_reason": "Low-value candidate"}
        self.card["field_support"] = {"low_value_reason": ["unit-one"], "value_score": ["unit-one"], "value_reason": ["unit-one"]}
        self.assertEqual(self.prepare()["envelope"]["low_value_status"], "proposed low-value")

    def test_typed_cross_agency_joins_supported_without_inference(self):
        with store.ledger(self.database) as con:
            for agency in ("north", "south"):
                con.execute("INSERT INTO joins VALUES(?,?,?,?,?,?,?,NULL)",
                            ("join-" + agency, self.subject, "agency:" + agency, "request:" + agency,
                             "agreement_party", json.dumps({"source_sha256": self.subject, "locator": "page:1"}), "typed"))
            con.commit()
        self.assertEqual(self.prepare()["envelope"]["agency_ids"], ["agency:north", "agency:south"])

    def test_hints_do_not_become_typed_agency(self):
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO joins VALUES(?,?,?,?,?,?,?,NULL)",
                        ("hint", self.subject, "agency:north", "request:north", "filename", "hint only", "hinted"))
            con.commit()
        self.assertEqual(self.prepare()["envelope"]["agency_ids"], [])

    def test_join_without_verified_locator_blocks(self):
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO joins VALUES(?,?,?,?,?,?,?,NULL)",
                        ("bad-join", self.subject, "agency:north", "request:north", "agreement_party",
                         json.dumps({"source_sha256": self.subject, "locator": "page:2"}), "typed"))
            con.commit()
        self.assertEqual(self.process()["reason"], "typed_join_support_missing")

    def test_changed_classification_reopens_only_subject_downstream(self):
        self.advance_prerequisites()
        old = self.process()
        self.card["metadata"] = {"title": "Synthetic policy title.", "evidence": [{"source_sha256": self.subject, "locator": "page:1"}]}
        self.card["field_support"] = {"title": ["unit-one"]}
        new = self.process()
        self.assertNotEqual(old["content_sha256"], new["content_sha256"])
        self.assertEqual(new["status"], "done")

    def test_private_artifact_symlink_rejected(self):
        other = self.root / "other.txt"
        other.write_bytes(self.original)
        other.chmod(0o600)
        (self.artifacts / "unit.txt").unlink()
        (self.artifacts / "unit.txt").symlink_to(other)
        self.assertEqual(self.process()["reason"], "symlink_path")

    def test_installed_factory_does_not_create_or_reprofile_runs(self):
        first = module.register_catalog_adapter(self.database, self.artifacts)
        self.assertEqual(first, module.register_catalog_adapter(self.database, self.artifacts))
        with self.assertRaisesRegex(stages.StageError, "run_profile_substitution"):
            module.catalog_run_factory(self.database, self.artifacts, run_id=self.run_id, owner="catalog",
                profile_id="installed:" + self.subject[:12], engine_version="synthetic-engine", config_sha256=self.config)

    def test_fresh_installed_factory_binds_explicit_adapter_id(self):
        run = self.run_id + "-installed"
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (run, "stage-runner", store.now(), None, "synthetic-engine", None, self.config, None, "running", "{}"))
            con.commit()
        instance = module.catalog_run_factory(self.database, self.artifacts, run_id=run, owner="catalog",
            profile_id="installed:" + sha(str(self.root).encode())[:20], engine_version="synthetic-engine", config_sha256=self.config)
        self.assertFalse(instance.runner.test_only)
        self.assertEqual(instance.runner.validators["catalog"][0], self.adapter.adapter_id)
        self.assertEqual(set(instance.runner.validators), {"catalog"})
        self.advance_prerequisites()
        result = instance.process(self.path, self.save(), author_id="synthetic-author")
        self.assertEqual(result["reason"], "production_extraction_authority_required")
        with store.ledger(self.database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT status FROM stage_state WHERE stage='catalog'").fetchone()[0], "pending")


if __name__ == "__main__":
    unittest.main()
