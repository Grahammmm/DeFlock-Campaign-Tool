"""Synthetic orchestration contracts plus real-ledger Linux runtime regressions."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

from campaign_tool.records import context_runtime, publish, run, unattended
from campaign_tool.records.ledger import store
from campaign_tool.records.run_safety import RunSafetyPolicy
from tests.records import test_run_safety as fixtures
from tests.records import test_context_runtime as context_fixtures
from tests.records.review_retirement_fixtures import independent_proposal


class ContextOrchestrationTests(fixtures.SafetyFixture, unittest.TestCase):
    def pipeline(self, limit=200, **options):
        p = unattended.UnattendedPipeline(self.root.path, policy=RunSafetyPolicy(max_originals=limit), **options)
        p.safety = self.guard(policy=p.policy, **({"clock": p.clock} if p.clock else {}))
        p._open_stage_run = Mock(return_value={"runner": Mock()})
        p._reconcile_review_authority = Mock(return_value=True)
        self.subject = fixtures.original(1)
        self.states = {name: {"status": "done", "receipt_sha256": fixtures.original(2)} for name in run.STAGE_ORDER}
        p._states = Mock(side_effect=lambda subject: self.states)
        p._original = Mock(return_value={})
        p._form = Mock(return_value="txt")
        for name in run.STAGE_ORDER:
            setattr(p, "stage_" + name, Mock(side_effect=AssertionError("unexpected stage dispatch")))
        self.snapshot = {"revision": {"revision_sha256": fixtures.original(3)}}
        return p

    def advance(self, p, subjects=None):
        with run.root_lock(self.root.path):
            return p.advance_all([self.subject] if subjects is None else subjects)

    def reconciler(self, p):
        def reconcile(pipeline, subject, runner):
            run.require_root_lock(self.root.path)
            self.assertEqual(p.safety.state["active"], {"phase": "advance", "identity": subject})
            return self.snapshot, {"applied_stages": []}
        return patch.object(context_runtime, "reconcile", side_effect=reconcile)

    def test_completed_subject_reconciles_once_before_all_cache_skips(self):
        p = self.pipeline()
        with self.reconciler(p) as reconcile:
            result = self.advance(p)
        reconcile.assert_called_once()
        self.assertEqual(p.safety.phases["advance"]["attempted"], 1)
        self.assertIs(p._contexts[self.subject], self.snapshot)
        self.assertEqual(result[0]["context_invalidated"], [])

    def test_changed_context_precedes_old_hold_and_dispatch(self):
        p = self.pipeline()
        self.states["review"]["status"] = "blocked"
        def reconcile(*args):
            self.states["detect"]["status"] = "pending"
            return self.snapshot, {"applied_stages": ["detect", "review", "compare", "privacy"]}
        p.stage_detect = Mock(return_value={"status": "pending"})
        with patch.object(context_runtime, "reconcile", side_effect=reconcile), patch.object(run.stages, "counts") as counts:
            result = self.advance(p)
        p.stage_detect.assert_called_once()
        counts.assert_not_called()
        self.assertEqual(result[0]["stages"]["detect"], "pending")

    def test_unchanged_hold_is_validated_and_charged_after_context(self):
        p = self.pipeline()
        self.states["review"]["status"] = "blocked"
        with self.reconciler(p) as reconcile, patch.object(run.stages, "counts") as counts:
            result = self.advance(p)
        reconcile.assert_called_once()
        counts.assert_called_once()
        self.assertTrue(result[0]["attempted"])
        self.assertEqual(p.review_holds_retained, 1)
        self.assertEqual(p.safety.phases["advance"]["review_required"], 1)

    def test_new_extraction_reconciles_before_catalog_in_same_attempt(self):
        p = self.pipeline()
        self.states["extract"] = {"status": "pending", "receipt_sha256": None}
        self.states["catalog"]["status"] = "pending"
        order = []
        def extract(*args):
            order.append("extract")
            self.states["extract"] = {"status": "done", "receipt_sha256": fixtures.original(4)}
            return {"status": "done"}
        def reconcile(*args):
            order.append("context")
            return self.snapshot, {"applied_stages": []}
        p.stage_extract = Mock(side_effect=extract)
        p.stage_catalog = Mock(side_effect=lambda *args: (order.append("catalog") or {"status": "pending"}))
        with patch.object(context_runtime, "reconcile", side_effect=reconcile):
            self.advance(p)
        self.assertEqual(order, ["extract", "context", "catalog"])
        self.assertEqual(p.safety.phases["advance"]["attempted"], 1)

    def test_missing_accepted_extraction_cannot_reuse_analytical_stages(self):
        p = self.pipeline()
        self.states["extract"] = {"status": "inapplicable", "receipt_sha256": None}
        with patch.object(context_runtime, "reconcile") as reconcile:
            result = self.advance(p)
        reconcile.assert_not_called()
        self.assertEqual(result[0]["stages"]["context"], "error:stage_failed")
        self.assertEqual(p.originals_deferred, 1)

    def test_context_error_is_fixed_charged_and_never_dispatches_next_subject(self):
        p = self.pipeline()
        with patch.object(context_runtime, "reconcile", side_effect=ValueError("PRIVATE_MARKER")) as reconcile:
            result = self.advance(p, [self.subject, fixtures.original(5)])
        reconcile.assert_called_once()
        self.assertNotIn("PRIVATE_MARKER", json.dumps(result))
        self.assertEqual(p.originals_deferred, 2)
        self.assertEqual(p.safety.phases["advance"]["failed"], 1)
        self.assertTrue(p.safety.stopped)

    def test_retirement_and_context_share_remaining_advance_budget(self):
        p = self.pipeline(limit=2)
        def retire(stage):
            p.safety.begin("advance", fixtures.original(7))
            p.safety.finish()
            return True
        p._reconcile_review_authority.side_effect = retire
        with self.reconciler(p) as reconcile:
            self.advance(p, [self.subject, fixtures.original(8)])
        reconcile.assert_called_once()
        self.assertEqual(p.safety.phases["advance"]["attempted"], 2)
        self.assertEqual(p.originals_deferred, 1)

    def test_elapsed_context_budget_prevents_stage_dispatch(self):
        time = [0]
        p = self.pipeline(clock=lambda: time[0])
        self.states["catalog"]["status"] = "pending"
        def reconcile(*args):
            time[0] = p.policy.seconds + 1
            return self.snapshot, {"applied_stages": []}
        with patch.object(context_runtime, "reconcile", side_effect=reconcile):
            result = self.advance(p)
        p.stage_catalog.assert_not_called()
        self.assertEqual(result[0]["failure_code"], "time_budget_exceeded")
        self.assertEqual(p.originals_deferred, 1)
        self.assertIsNone(p.safety.state["active"])

    def test_interruption_retains_active_attempt_and_requires_acknowledgement(self):
        p = self.pipeline()
        with patch.object(context_runtime, "reconcile", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.advance(p)
        self.assertIsNotNone(p.safety.state["active"])
        self.assertEqual(self.guard().state["hold"], "interrupted_attempt")

    def test_unlocked_advance_fails_before_any_side_effect(self):
        p = self.pipeline()
        with self.assertRaisesRegex(run.RunError, "context_root_lock_required"):
            p.advance_all([self.subject])
        p._open_stage_run.assert_not_called()
        self.assertEqual(p.safety.phases["advance"]["attempted"], 0)

    def test_default_query_includes_completed_originals_and_is_bounded(self):
        p = self.pipeline()
        p._query = Mock(return_value=[{"sha256": self.subject, "first_seen_at": "2026-01-01"}])
        with self.reconciler(p), run.root_lock(self.root.path):
            p.advance_all()
        sql = p._query.call_args.args[0]
        self.assertIn("LIMIT 10001", sql)
        self.assertNotIn("stage_state", sql)

    def test_context_failure_does_not_commit_cursor_or_reset_frontier(self):
        p = self.pipeline()
        key = ["2026-01-01", self.subject]
        resume = {"schema": "records-advance-resume-v1", "cursor": None, "frontier": key}
        p._query = Mock(return_value=[{"sha256": self.subject, "first_seen_at": key[0]}])
        p._advance_resume = Mock(return_value=(self.root.sub("reports") / "resume.json", resume))
        p._save_advance_resume = Mock()
        with patch.object(context_runtime, "reconcile", side_effect=ValueError("PRIVATE_MARKER")), run.root_lock(self.root.path):
            p.advance_all()
        self.assertEqual(resume, {"schema": "records-advance-resume-v1", "cursor": None, "frontier": key})
        self.assertEqual(p.originals_deferred, 1)
        p._save_advance_resume.assert_called_once()

    def test_overflow_stops_before_context_or_dispatch(self):
        p = self.pipeline()
        p._query = Mock(return_value=[{}] * 10001)
        with patch.object(context_runtime, "reconcile") as reconcile, run.root_lock(self.root.path):
            with self.assertRaisesRegex(run.RunError, "invalid_safety_state"):
                p.advance_all()
        reconcile.assert_not_called()
        self.assertEqual(p.safety.phases["advance"]["attempted"], 0)

    def test_privacy_receipt_hold_remains_hard_stop(self):
        p = self.pipeline()
        self.states["privacy"]["status"] = "blocked"
        with self.reconciler(p), patch.object(run.stages, "counts"):
            self.advance(p)
        self.assertEqual(p.safety.state["hold"], "privacy_redaction_fault")


class ContextLedgerTests(unittest.TestCase):
    # Reuse fixture setup only, without inheriting the other test methods.
    setUp = context_fixtures.ContextRuntimeTests.setUp
    initial = context_fixtures.ContextRuntimeTests.initial
    pointer = context_fixtures.ContextRuntimeTests.pointer

    def pipeline(self):
        p = unattended.UnattendedPipeline(self.root)
        p.safety = fixtures.RunSafety(p.root.ledger)
        return p

    def advance(self, p):
        with run.root_lock(self.root):
            try:
                return p.advance_all([self.subject])
            finally:
                p._close_stage_run({"synthetic": True})

    def test_rule_change_reconciles_real_old_hold_preserving_extraction(self):
        prior = self.initial()
        receipt = prior._states(self.subject)["extract"]["receipt_sha256"]
        p = self.pipeline()
        p.package = copy.deepcopy(p.package)
        p.package["rules"][0]["duty"] += " Fictional amended rule."
        result = self.advance(p)
        self.assertEqual(result[0]["context_invalidated"], ["detect", "review", "compare", "privacy"])
        self.assertEqual(p._states(self.subject)["extract"]["receipt_sha256"], receipt)
        self.assertEqual(p._states(self.subject)["review"]["status"], "blocked")

    def test_revocation_before_partial_invalidation_crash_and_repaired_replay(self):
        _, proposal = independent_proposal(self.root, self.base / "inbox", original=self.original)
        publish.approve(self.root, proposal, owner_id="synthetic-owner")
        pointer = self.pointer()
        p = self.pipeline()
        p.package = copy.deepcopy(p.package)
        p.package["rules"][0]["duty"] += " Fictional amended rule."
        with store.ledger(p.root.ledger, readonly=True) as con:
            receipts = {row[0] for row in con.execute("SELECT sha256 FROM receipts")}
        with run.root_lock(self.root):
            stage = p._open_stage_run()
            invalidate = stage["runner"].invalidate
            calls = []
            def crash(subject, name, **kwargs):
                calls.append(name)
                if len(calls) == 2:
                    raise RuntimeError("PRIVATE_CRASH_MARKER")
                return invalidate(subject, name, **kwargs)
            with patch.object(stage["runner"], "invalidate", side_effect=crash):
                result = p.advance_all([self.subject])
            p._close_stage_run({"synthetic_crash": True})
        self.assertNotIn("PRIVATE_CRASH_MARKER", json.dumps(result))
        self.assertEqual(self.pointer(), pointer)
        with store.ledger(p.root.ledger, readonly=True) as con:
            row = con.execute("SELECT owner_approval,approved_content_sha256 FROM proposals WHERE id=?", (proposal,)).fetchone()
            self.assertEqual(tuple(row), ("none", None))
            self.assertTrue(receipts <= {row[0] for row in con.execute("SELECT sha256 FROM receipts")})
        self.assertTrue(p.safety.stopped)
        # Direct locked helper replay after asserting the durable guard hold;
        # this does not restart unattended work or clear the production hold.
        with run.root_lock(self.root):
            stage = p._open_stage_run()
            current, plan = context_runtime.reconcile(p, self.subject, stage["runner"])
            p._close_stage_run({"synthetic_repaired_replay": True})
        self.assertNotEqual(self.pointer(), pointer)
        self.assertIn("review", plan["applied_stages"])
        self.assertEqual(current["subject_sha256"], self.subject)
        with self.assertRaises(publish.PublishError):
            publish.publish(self.root, proposal, staging=self.base / "target")
