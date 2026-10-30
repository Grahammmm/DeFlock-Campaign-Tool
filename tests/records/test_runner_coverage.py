"""Synthetic current-extraction selection and denied semantic coverage."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from campaign_tool.records import runner_coverage, semantic_coverage as sc
from campaign_tool.records.ledger import stages


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class RunnerCoverageTests(unittest.TestCase):
    def fixture(self, directory, size=3):
        subject = sha(b"synthetic-source")
        identities = [sha(str(i).encode()) for i in range(size)]
        body = json.dumps({"unit_ids": identities})
        extraction = {"schema": "canonical-extraction-content-v1", "original_sha256": subject,
                      "import_id": "synthetic-import", "manifest_sha256": sha(body.encode()),
                      "unit_count": size, "parser": "synthetic", "parser_version": "v1"}
        current = {"id": "synthetic-import", "manifest_sha256": extraction["manifest_sha256"],
                   "manifest_json": body}
        rows = []
        for i, identity in enumerate(identities):
            raw = json.dumps({"text": "synthetic text", "data": {}}).encode()
            path = Path(directory) / str(i)
            path.write_bytes(raw)
            rows.append({"id": identity, "provenance_json": json.dumps({
                "extraction_import_id": current["id"], "payload_sha256": sha(raw)}),
                "locator": json.dumps({"page": i + 1}), "unit_type": "text",
                "derived_path": str(path), "text_sha256": sha(b"synthetic text"), "status": "candidate_extracted"})
        pipeline = SimpleNamespace(_content=lambda *args: extraction,
            _query=lambda sql, values: [current] if "extraction_adapter_current" in sql else rows,
            allowlist=(), denylist=(), model=None, config_sha256=sha(b"synthetic-config"))
        return pipeline, subject, rows, extraction, current

    def test_selection_cap_retains_denominator_and_unaccepted_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            p, subject, rows, extraction, current = self.fixture(d)
            units, inventory, bound = runner_coverage.unit_view(p, subject, 2)
            self.assertEqual((len(units), len(inventory)), (2, 3))
            progress = runner_coverage.processing_coverage(p, subject, units, inventory, bound, stage="review")
            summary = progress["summary"]
            self.assertEqual((summary["inventory_total"], summary["selected_units"], summary["accepted_units"]), (3, 2, 0))
            self.assertFalse(progress["model_emission_recorded"])
            self.assertFalse(summary["semantic_understanding_established"])
            self.assertEqual(summary["receipt_coverage"]["scope"], "selected")
            restored = json.loads(json.dumps(progress["checkpoint"]))
            self.assertEqual(sc.validate_checkpoint(progress["manifest"], restored), restored)
            self.assertEqual(len(sc.pending_chunks(progress["manifest"], restored)), len(progress["manifest"]["chunks"]))

    def test_historical_import_units_cannot_replace_current_inventory(self):
        with tempfile.TemporaryDirectory() as d:
            p, subject, rows, extraction, current = self.fixture(d)
            rows.insert(0, {**rows[0], "id": sha(b"historical unit")})
            units, inventory, _ = runner_coverage.unit_view(p, subject, 2)
            self.assertEqual([u["unit_id"] for u in units], json.loads(current["manifest_json"])["unit_ids"][:2])
            self.assertEqual(len(inventory), 3)

    def test_missing_and_changed_derivatives_remain_explicit_gaps(self):
        with tempfile.TemporaryDirectory() as d:
            p, subject, rows, extraction, current = self.fixture(d)
            rows.pop()
            Path(rows[0]["derived_path"]).write_text('{"text":"changed"}')
            units, inventory, _ = runner_coverage.unit_view(p, subject, 3)
            self.assertEqual(len(units), 1)
            self.assertEqual(len(inventory), 3)
            self.assertEqual([u.get("blocked_reason") for u in inventory],
                             ["canonical_unit_unreadable_or_unbound", None, "canonical_unit_missing"])

    def test_stale_extraction_or_redaction_context_cannot_reuse_progress(self):
        with tempfile.TemporaryDirectory() as d:
            p, subject, rows, extraction, current = self.fixture(d)
            units, inventory, bound = runner_coverage.unit_view(p, subject, 2)
            prior = runner_coverage.processing_coverage(p, subject, units, inventory, bound, stage="review")
            p.denylist = ("synthetic",)
            fresh = runner_coverage.processing_coverage(p, subject, units, inventory, bound, stage="review")
            with self.assertRaisesRegex(ValueError, "checkpoint_binding"):
                sc.validate_checkpoint(fresh["manifest"], prior["checkpoint"])
            extraction["manifest_sha256"] = sha(b"changed snapshot")
            with self.assertRaisesRegex(ValueError, "snapshot_changed"):
                runner_coverage.unit_view(p, subject, 2)

    def test_mechanical_runner_status_resume_and_model_claims(self):
        import inspect
        with tempfile.TemporaryDirectory() as d:
            p, subject, rows, extraction, current = self.fixture(d, 6)
            Path(rows[1]["derived_path"]).write_text("unreadable")
            rows.pop(2)
            units, inventory, bound = runner_coverage.unit_view(p, subject, 4)
            progress = runner_coverage.processing_coverage(p, subject, units, inventory, bound, stage="review")
            self.assertEqual(progress["resume"], {"status": "fresh"})
            self.assertEqual(progress["status"], "planned_only")
            self.assertTrue(progress["review_required"])
            chunk = progress["manifest"]["chunks"][0]
            claim = {"accepted_ranges": chunk["ranges"], "all_units_accepted": True, "status": "accepted_scope_complete"}
            options = dict(payload_sha256=chunk["payload_sha256"], receipt_sha256=sha(json.dumps(claim).encode()))
            initial = json.loads(json.dumps(progress))
            progress = runner_coverage.record_emission(progress, chunk["chunk_id"], **options, completed=False)
            self.assertEqual(progress["status"], "emission_partial")
            self.assertEqual(initial["checkpoint"]["attempts"], [])
            progress = runner_coverage.record_emission(progress, chunk["chunk_id"],
                payload_sha256=chunk["payload_sha256"], receipt_sha256=sha(b"completed"), completed=True)
            self.assertEqual(progress["status"], "emission_complete")
            summary = progress["summary"]
            self.assertEqual((summary["accepted_units"], summary["status"], summary["receipt_coverage"]["covered"]),
                             (0, "partial", 0))
            self.assertEqual(len(progress["manifest"]["units"]), 6)
            self.assertEqual([u["blocked_reason"] for u in progress["manifest"]["units"]][1:3],
                             ["canonical_unit_unreadable_or_unbound", "canonical_unit_missing"])
            self.assertEqual(len(summary["gaps"]), 6)
            for state in (initial, progress):
                self.assertTrue(state["review_required"])
                self.assertFalse(state["semantic_review_established"])
            self.assertTrue(progress["model_emission_recorded"])
            for name, function in vars(runner_coverage).items():
                if not name.startswith("_") and callable(function):
                    self.assertNotIn("accepted_ranges", inspect.signature(function).parameters)

    def test_runner_restored_partial_and_stale_checkpoint_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            p, subject, rows, extraction, current = self.fixture(d)
            units, inventory, bound = runner_coverage.unit_view(p, subject, 3)
            p.model = SimpleNamespace(model_id="synthetic-model", privacy_tier="synthetic-local")
            # Long synthetic views create multiple chunks under the default budget.
            units = [{**u, "text": "x" * 13000} for u in units]
            def build(**options):
                return runner_coverage.processing_coverage(p, subject, units, inventory, bound, stage="review", **options)
            progress = build()
            for i, chunk in enumerate(progress["manifest"]["chunks"][:2]):
                progress = runner_coverage.record_emission(progress, chunk["chunk_id"],
                    payload_sha256=chunk["payload_sha256"], receipt_sha256=sha(str(i).encode()), completed=(i == 0))
            restored = json.loads(json.dumps(progress["checkpoint"]))
            resumed = build(checkpoint=restored)
            self.assertEqual(resumed["resume"], {"status": "resumed"})
            self.assertEqual(resumed["status"], "emission_partial")
            self.assertEqual(resumed["checkpoint"]["attempts"], restored["attempts"])
            for change in ("denylist", "model_id", "privacy_tier", "parser_version", "manifest_sha256", "subject"):
                old_subject = subject
                p.denylist = ()
                p.model = SimpleNamespace(model_id="synthetic-model", privacy_tier="synthetic-local")
                bound = dict(extraction)
                if change == "denylist":
                    p.denylist = ("synthetic",)
                elif change in ("model_id", "privacy_tier"):
                    setattr(p.model, change, "synthetic-changed")
                elif change == "subject":
                    subject = sha(b"other synthetic source")
                elif change == "manifest_sha256":
                    body = json.dumps({"unit_ids": json.loads(current["manifest_json"])["unit_ids"], "revision": 2})
                    bound[change] = sha(body.encode())
                else:
                    bound[change] = "v2"
                with self.subTest(change=change):
                    rejected = build(checkpoint=restored)
                    self.assertEqual(rejected["resume"], {"status": "rejected", "reason": "checkpoint_binding"})
                    self.assertEqual(rejected["checkpoint"]["attempts"], [])
                subject = old_subject
            p.denylist, bound = (), extraction
            p.model = SimpleNamespace(model_id="synthetic-model", privacy_tier="synthetic-local")
            restored["attempts"][0]["completed"] = False
            rejected = build(checkpoint=restored)
            self.assertEqual(rejected["resume"], {"status": "rejected", "reason": "checkpoint_sha256_mismatch"})
            self.assertEqual(rejected["checkpoint"]["attempts"], [])

    def test_request_records_only_verified_whole_views(self):
        with tempfile.TemporaryDirectory() as d:
            p, subject, rows, extraction, current = self.fixture(d)
            units, inventory, bound = runner_coverage.unit_view(p, subject, 3)
            progress = runner_coverage.processing_coverage(p, subject, units, inventory, bound, stage="review")
            text = json.dumps({"record_text": ['{"unit": 0}\tsynthetic text'], "truncated": True})
            recorded = runner_coverage.record_request(progress, text, [u["unit_id"] for u in units])
            self.assertEqual(progress["checkpoint"]["attempts"], [])
            self.assertEqual(recorded["request"]["verified_units"], 1)
            self.assertEqual(recorded["summary"]["emitted_chars"], len("synthetic text"))
            self.assertEqual(recorded["summary"]["unemitted_units"], [u["unit_id"] for u in units[1:]])
            self.assertEqual(recorded["status"], "emission_partial")
            self.assertEqual(recorded["summary"]["unemitted_chunks"], 1)
            self.assertEqual(recorded["summary"]["accepted_units"], 0)

    def test_zero_coverage_is_valid_only_for_denied_review(self):
        receipt = {"coverage": {"denominator": {"kind": "items", "total": 3},
                                "covered": 0, "scope": "selected"}, "verdict": "challenge",
                   "locators": ["synthetic:fixture"]}
        stages._coverage(receipt, "review")
        receipt["verdict"] = "pass"
        with self.assertRaisesRegex(stages.StageError, "review_coverage_unknown"):
            stages._coverage(receipt, "review")
