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

    def test_zero_coverage_is_valid_only_for_denied_review(self):
        receipt = {"coverage": {"denominator": {"kind": "items", "total": 3},
                                "covered": 0, "scope": "selected"}, "verdict": "challenge",
                   "locators": ["synthetic:fixture"]}
        stages._coverage(receipt, "review")
        receipt["verdict"] = "pass"
        with self.assertRaisesRegex(stages.StageError, "review_coverage_unknown"):
            stages._coverage(receipt, "review")
