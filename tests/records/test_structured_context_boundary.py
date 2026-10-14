"""Synthetic PR96 -> PR86 prerequisite boundary; no new input authority."""
import json
from pathlib import Path
import tempfile
import unittest

from campaign_tool.records import context_revision as cr, context_runtime, runner_coverage
from campaign_tool.records.detectors.core import DETECTORS
from campaign_tool.records.detectors.pipeline import canonical_joins, evaluate_structured, ContractError
from campaign_tool.records.ledger import store
from campaign_tool.records.run import Pipeline, sha
from tests.records.test_run_slice import synthetic_email


class StructuredContextBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="structured-context-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.inbox = self.base / "inbox"
        self.inbox.mkdir(mode=0o700)
        self.original = b"FICTIONAL TEST TEXT: ALPR policy and search log labels do not establish field mappings."
        self.subject = sha(self.original)
        (self.inbox / "synthetic.eml").write_bytes(synthetic_email(attachment=self.original))
        self.pipeline = Pipeline(self.base / "root")
        self.pipeline.run(self.inbox)
        units, self.inventory, self.extraction = self.pipeline._unit_view(self.subject)
        self.sources = [{"unit_id": unit["unit_id"], "original_sha256": self.subject,
                         "artifact_sha256": unit["artifact_sha256"], "locator": unit["locator"]} for unit in units]
        self.assertTrue(self.sources)

    def join(self, *, wrong_request=False):
        with store.ledger(self.pipeline.root.ledger) as con:
            con.execute("INSERT INTO agencies VALUES(?,?,?,?)",
                        ("synthetic-a", "Synthetic agency A", "us-ca", None))
            con.execute("INSERT INTO agencies VALUES(?,?,?,?)",
                        ("synthetic-b", "Synthetic agency B", "us-ca", None))
            con.execute("INSERT INTO requests(id,agency_id,status) VALUES(?,?,?)",
                        ("synthetic-request", "synthetic-b" if wrong_request else "synthetic-a", "unknown"))
            proof = {"source_sha256": self.subject, "locator": json.dumps(self.sources[0]["locator"])}
            con.execute("INSERT INTO joins VALUES(?,?,?,?,?,?,?,?)",
                        ("synthetic-join", self.subject, "synthetic-a", "synthetic-request",
                         "synthetic-source-association", json.dumps(proof), "typed", None))
            con.commit()

    def test_real_current_extraction_joins_are_read_only_and_source_bound(self):
        self.join()
        with store.ledger(self.pipeline.root.ledger, readonly=True) as con:
            before = con.total_changes
            joined = canonical_joins(con, self.sources)
            self.assertEqual(con.total_changes, before)
        self.assertEqual(json.loads(joined.rows_json),
                         [{"original_sha256": self.subject, "status": "typed", "agency_id": "synthetic-a"}])
        self.assertEqual({s["unit_id"] for s in self.sources},
                         {u["unit_id"] for u in self.inventory})

    def test_join_changes_invalidate_context_without_duplicate_originals(self):
        before = context_runtime.snapshot(self.pipeline, self.subject)
        self.join()
        after = context_runtime.snapshot(self.pipeline, self.subject)
        plan = cr.invalidation_plan(before["revision"], after["revision"])
        self.assertEqual(plan["stages"], ["catalog", "detect", "review", "compare", "privacy"])
        self.assertTrue(plan["revoke_approval"])
        with store.ledger(self.pipeline.root.ledger, readonly=True) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM originals WHERE sha256=?", (self.subject,)).fetchone()[0], 1)

    def test_wrong_request_agency_is_not_repaired_or_guessed(self):
        self.join(wrong_request=True)
        with store.ledger(self.pipeline.root.ledger, readonly=True) as con:
            with self.assertRaisesRegex(ContractError, "typed_request_agency_mismatch"):
                canonical_joins(con, self.sources)

    def test_verified_bytes_and_joins_do_not_invent_normalization_for_seven_checks(self):
        self.join()
        with store.ledger(self.pipeline.root.ledger, readonly=True) as con:
            joined = canonical_joins(con, self.sources)
        output = evaluate_structured(subject_sha256=self.subject,
                                     source_bindings=self.sources, joins=joined)
        self.assertEqual(set(output["structured"]), set(DETECTORS))
        self.assertEqual({e["status"] for e in output["structured"].values()}, {"blocked"})
        self.assertEqual({e["reason"] for e in output["structured"].values()}, {"normalization_missing"})
        self.assertFalse(output["publication_ready"])
        self.assertEqual(output["stage_promotions"], 0)

    def test_current_context_never_translates_draft_law_or_mail_date_into_detector_policy(self):
        current = context_runtime.snapshot(self.pipeline, self.subject)
        self.assertEqual(current["applicability"]["status"], "blocked")
        self.assertEqual(current["applicability_bases"], [])
        self.assertEqual(current["context"]["dates"]["conduct"], [])
        self.assertIsNone(self.pipeline._event_date(self.subject))
        self.assertNotIn("version_verified", current["context"]["evidence_context"])
        self.assertNotIn("normalized", current)

    def test_capped_selection_cannot_be_declared_complete_detector_support(self):
        selected, inventory, extraction = runner_coverage.unit_view(self.pipeline, self.subject, 0)
        self.assertEqual(selected, [])
        self.assertGreater(len(inventory), 0)
        self.assertEqual(extraction["manifest_sha256"], self.extraction["manifest_sha256"])
        output = evaluate_structured(subject_sha256=self.subject, source_bindings=[], normalized=None)
        self.assertTrue(all(e["status"] == "blocked" for e in output["structured"].values()))
