"""Real synthetic-ledger contextual invalidation, dates and crash/replay."""
import copy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.digest import model
from campaign_tool.records import context_runtime as runtime, publish
from campaign_tool.records.ledger import store
from campaign_tool.records.run import Pipeline, root_lock, sha
from tests.records.test_run_slice import synthetic_email
from tests.records.review_retirement_fixtures import independent_proposal


class ContextRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="context-runtime-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "root"
        self.original = b"FICTIONAL POLICY: ALPR retention is 90 days."
        self.subject = sha(self.original)

    def initial(self, **options):
        inbox = self.base / "inbox"
        inbox.mkdir(mode=0o700)
        (inbox / "synthetic.eml").write_bytes(synthetic_email(attachment=self.original))
        pipeline = Pipeline(self.root, **options)
        report = pipeline.run(inbox)
        self.assertEqual(report["counts"]["detect"]["done"], 2)
        self.assertEqual(report["counts"]["review"]["blocked"], 2)
        return pipeline

    def pointer(self):
        with store.ledger(self.root / "ledger.sqlite", readonly=True) as con:
            return con.execute("SELECT value FROM ledger_meta WHERE key=?",
                ("context-revision-v1:" + self.subject,)).fetchone()[0]

    def test_receipt_and_cli_dates_never_become_conduct_or_retention(self):
        pipeline = self.initial(event_date=date(2040, 1, 1))
        current = runtime.snapshot(pipeline, self.subject)
        self.assertIsNone(pipeline._event_date(self.subject))
        self.assertEqual(current["context"]["dates"], {role: [] for role in ("conduct", "receipt", "disclosure", "retention")})
        evidence = current["context"]["evidence_context"]
        self.assertTrue(evidence["receipt_declarations"])
        self.assertEqual(evidence["requested_event_date"], "2040-01-01")
        self.assertEqual(current["applicability"]["status"], "blocked")
        self.assertFalse(pipeline._content(self.subject, "catalog")["metadata"].get("date_from"))

    def test_changed_rule_text_reopens_analysis_but_keeps_original_extraction(self):
        self.initial()
        pipeline = Pipeline(self.root)
        pipeline.package = copy.deepcopy(pipeline.package)
        pipeline.package["rules"][0]["duty"] += " Synthetic corrected wording."
        before = pipeline._states(self.subject)["extract"]["receipt_sha256"]
        with root_lock(self.root):
            stage = pipeline._open_stage_run()
            current, plan = runtime.reconcile(pipeline, self.subject, stage["runner"])
            self.assertEqual(plan["stages"], ["detect", "review", "compare", "privacy"])
            pipeline._close_stage_run({"synthetic": True})
        states = pipeline._states(self.subject)
        self.assertEqual(states["extract"]["receipt_sha256"], before)
        self.assertEqual(states["detect"]["status"], "pending")
        self.assertEqual(states["review"]["status"], "pending")

    def test_prompt_revision_reopens_review_without_reextracting_or_redetecting(self):
        self.initial()
        pipeline = Pipeline(self.root)
        before = pipeline._states(self.subject)
        with root_lock(self.root), patch.object(model, "SYSTEM_PROMPT", model.SYSTEM_PROMPT + "\nSynthetic prompt revision."):
            stage = pipeline._open_stage_run()
            _, plan = runtime.reconcile(pipeline, self.subject, stage["runner"])
            self.assertEqual(plan["stages"], ["review", "compare", "privacy"])
            pipeline._close_stage_run({"synthetic": True})
        self.assertEqual(pipeline._states(self.subject)["detect"]["receipt_sha256"], before["detect"]["receipt_sha256"])

    def test_repeated_bytes_in_a_new_delivery_context_reopen_catalog_without_duplicate_original(self):
        self.initial()
        with store.ledger(self.root / "ledger.sqlite") as con:
            con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)",
                ("synthetic-second-context", self.subject, "local", "synthetic-context",
                 None, None, "synthetic", "{}"))
            con.commit()
        pipeline = Pipeline(self.root)
        with root_lock(self.root):
            stage = pipeline._open_stage_run()
            current, plan = runtime.reconcile(pipeline, self.subject, stage["runner"])
            self.assertEqual(plan["stages"], ["catalog", "detect", "review", "compare", "privacy"])
            self.assertEqual(len([r for r in current["context"]["evidence_context"]["occurrences"]
                                 if r["original_sha256"] == self.subject]), 2)
            pipeline._close_stage_run({"synthetic": True})
        with store.ledger(pipeline.root.ledger, readonly=True) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM originals WHERE sha256=?", (self.subject,)).fetchone()[0], 1)

    def test_revocation_commits_before_invalidation_crash_and_replay_retains_history(self):
        subject, proposal = independent_proposal(self.root, self.base / "inbox", original=self.original)
        self.assertEqual(subject, self.subject)
        publish.approve(self.root, proposal, owner_id="synthetic-owner")
        prior = self.pointer()
        with store.ledger(self.root / "ledger.sqlite", readonly=True) as con:
            receipts = set(r[0] for r in con.execute("SELECT sha256 FROM receipts"))
        pipeline = Pipeline(self.root)
        pipeline.package = copy.deepcopy(pipeline.package)
        pipeline.package["rules"][0]["duty"] += " Synthetic change."
        with root_lock(self.root):
            stage = pipeline._open_stage_run()
            with patch.object(stage["runner"], "invalidate", side_effect=RuntimeError("synthetic crash")):
                with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
                    runtime.reconcile(pipeline, self.subject, stage["runner"])
            pipeline._close_stage_run({"synthetic_crash": True})
        self.assertEqual(self.pointer(), prior)
        with store.ledger(pipeline.root.ledger, readonly=True) as con:
            row = con.execute("SELECT owner_approval,approved_content_sha256 FROM proposals WHERE id=?", (proposal,)).fetchone()
            self.assertEqual(tuple(row), ("none", None))
            self.assertTrue(receipts.issubset(set(r[0] for r in con.execute("SELECT sha256 FROM receipts"))))
        resumed = Pipeline(self.root)
        resumed.package = copy.deepcopy(pipeline.package)
        with root_lock(self.root):
            stage = resumed._open_stage_run()
            _, plan = runtime.reconcile(resumed, self.subject, stage["runner"])
            self.assertIn("review", plan["stages"])
            resumed._close_stage_run({"synthetic_resume": True})
        self.assertNotEqual(self.pointer(), prior)
        with self.assertRaises(publish.PublishError):
            publish.publish(self.root, proposal, staging=self.base / "target")

    def test_context_writes_require_the_shared_root_lock(self):
        self.initial()
        pipeline = Pipeline(self.root)
        prior = self.pointer()
        with self.assertRaisesRegex(Exception, "context_root_lock_required"):
            runtime.reconcile(pipeline, self.subject, None)
        self.assertEqual(self.pointer(), prior)

    def test_unchanged_context_reuses_immutable_snapshot(self):
        pipeline = self.initial()
        prior = self.pointer()
        again = Pipeline(self.root).run(None)
        self.assertEqual(self.pointer(), prior)
        self.assertTrue(all(not item.get("context_invalidated") for item in again["subjects"]))

    def test_invalid_snapshot_pointer_revokes_approval_without_deleting_history(self):
        _, proposal = independent_proposal(self.root, self.base / "inbox", original=self.original)
        publish.approve(self.root, proposal, owner_id="synthetic-owner")
        pipeline = Pipeline(self.root)
        with store.ledger(pipeline.root.ledger) as con:
            # Point at an existing immutable stage artifact, not a context snapshot.
            other = con.execute("SELECT content_sha256 FROM stage_content WHERE stage='extract' LIMIT 1").fetchone()[0]
            con.execute("UPDATE ledger_meta SET value=? WHERE key=?",
                        (other, "context-revision-v1:" + self.subject))
            con.commit()
        with root_lock(self.root):
            stage = pipeline._open_stage_run()
            with self.assertRaisesRegex(ValueError, "context_snapshot_invalid"):
                runtime.reconcile(pipeline, self.subject, stage["runner"])
            pipeline._close_stage_run({"synthetic_bad_pointer": True})
        with store.ledger(pipeline.root.ledger, readonly=True) as con:
            self.assertEqual(con.execute("SELECT owner_approval FROM proposals WHERE id=?", (proposal,)).fetchone()[0], "none")
            self.assertGreater(con.execute("SELECT count(*) FROM receipts").fetchone()[0], 0)

    def test_unresolved_comparison_is_denied_and_blocks_publication(self):
        _, proposal = independent_proposal(self.root, self.base / "inbox", original=self.original)
        publish.approve(self.root, proposal, owner_id="synthetic-owner")
        pipeline = Pipeline(self.root)
        with root_lock(self.root):
            stage = pipeline._open_stage_run()
            result = pipeline.stage_compare(self.subject, stage)
            self.assertEqual(result["status"], "blocked")
            pipeline._close_stage_run({"synthetic_unresolved": True})
        self.assertEqual(pipeline._content(self.subject, "compare")["classification"],
                         "VERSION_OR_APPLICABILITY_UNRESOLVED")
        with self.assertRaises(publish.PublishError):
            publish.publish(self.root, proposal, staging=self.base / "target")
