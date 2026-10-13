"""Synthetic retirement fault tests; existing schema and controller protections."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from campaign_tool.records import publish, review_retirement, unattended
from campaign_tool.records.ledger import stages, store
from campaign_tool.records.run import Pipeline, RunError, root_lock, status
from campaign_tool.records.run_safety import RunSafetyPolicy
from tests.records import preaccepted_publication
from tests.records.review_retirement_fixtures import independent_proposal


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="review-retirement-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root, self.target = self.base / "root", self.base / "target"
        self.proposal = preaccepted_publication.policy(Pipeline(self.root))
        publish.approve(self.root, self.proposal, owner_id="synthetic-owner")

    def rows(self, sql, args=()):
        with store.ledger(self.root / "ledger.sqlite", readonly=True) as con:
            return [tuple(row) for row in con.execute(sql, args)]

    def blocked(self):
        self.assertTrue(status(self.root)["review_retirement_pending"])
        with self.assertRaisesRegex(publish.PublishError, "review_authority_retirement_pending"):
            publish.approve(self.root, self.proposal, owner_id="synthetic-owner")
        with self.assertRaisesRegex(publish.PublishError, "review_authority_retirement_pending"):
            publish.publish(self.root, self.proposal, staging=self.target)

    def other(self):
        other = preaccepted_publication.seed(Pipeline(self.root), b"synthetic-second-original", "# Fictional policy\n")
        publish.approve(self.root, other, owner_id="synthetic-owner")
        return other

    def test_hold_commits_before_selection_crash_and_replay(self):
        pipeline = Pipeline(self.root)
        with patch.object(pipeline, "_query", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                pipeline.run()
        self.assertEqual(self.rows("SELECT owner_approval FROM proposals"), [("approved",)])
        self.blocked()
        report = Pipeline(self.root).run()
        self.assertEqual(report["review_retirement"], {"state": "complete", "processed": 1})
        self.assertFalse(status(self.root)["review_retirement_pending"])
        self.assertEqual(self.rows("SELECT owner_approval FROM proposals"), [("none",)])

    def test_failure_holds_later_approval_and_fresh_retry(self):
        self.other()
        with patch.object(stages.StageRunner, "invalidate", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaisesRegex(RunError, "review_authority_retirement_failed"):
                Pipeline(self.root).run()
        self.assertEqual(self.rows("SELECT owner_approval,count(*) FROM proposals GROUP BY owner_approval ORDER BY owner_approval"),
                         [("approved", 1), ("none", 1)])
        self.blocked()
        failures = self.rows("SELECT summary FROM runs WHERE status='failed'")
        self.assertTrue(any(json.loads(row[0]).get("failure_code") == "review_authority_retirement_failed" for row in failures))
        report = Pipeline(self.root).run()
        self.assertEqual(report["review_retirement"]["processed"], 2)
        self.assertEqual(self.rows("SELECT owner_approval,count(*) FROM proposals GROUP BY owner_approval"), [("none", 2)])
        before = self.rows("SELECT count(*) FROM stage_events WHERE reason LIKE '%cached_automated_or_test_review%'")
        Pipeline(self.root).run()
        self.assertEqual(self.rows("SELECT count(*) FROM stage_events WHERE reason LIKE '%cached_automated_or_test_review%'"), before)

    def test_crash_after_revoke_preserves_history_and_retry(self):
        old = self.rows("SELECT receipt_sha256 FROM stage_state WHERE stage='review'")[0][0]
        with patch.object(stages.StageRunner, "invalidate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                Pipeline(self.root).run()
        self.assertEqual(self.rows("SELECT owner_approval FROM proposals"), [("none",)])
        self.assertEqual(self.rows("SELECT status FROM stage_state WHERE stage='review'"), [("done",)])
        self.blocked()
        Pipeline(self.root).run()
        self.assertEqual(self.rows("SELECT count(*) FROM receipts WHERE sha256=?", (old,)), [(1,)])
        self.assertEqual(self.rows("SELECT stage,status FROM stage_state WHERE stage IN ('compare','privacy') ORDER BY stage"),
                         [("compare", "pending"), ("privacy", "pending")])

    def test_batch_limit_is_resumable_without_advancing_other_stages(self):
        self.other()
        pipeline = unattended.UnattendedPipeline(self.root, policy=RunSafetyPolicy(max_originals=1))
        report = pipeline.run()
        self.assertEqual(report["review_retirement"], {"state": "pending", "processed": 1})
        self.assertEqual(report["subjects"], [])
        self.assertEqual((report["status"], report["exit_code"]), ("completed_with_gaps", 3))
        self.blocked()
        report = unattended.UnattendedPipeline(self.root, policy=RunSafetyPolicy(max_originals=1)).run()
        self.assertEqual(report["review_retirement"], {"state": "complete", "processed": 1})
        self.assertFalse(status(self.root)["review_retirement_pending"])

    def test_expired_before_advance_still_arms_hold(self):
        ticks = iter([0, 2])
        report = unattended.UnattendedPipeline(self.root, policy=RunSafetyPolicy(seconds=1),
                                               clock=lambda: next(ticks, 2)).run()
        self.assertEqual(report["review_retirement"], {"state": "pending", "processed": 0})
        self.assertEqual(report["safety"]["stop_code"], "time_budget_exceeded")
        self.assertEqual(report["exit_code"], 2)
        self.blocked()
        Pipeline(self.root).run()
        self.assertFalse(status(self.root)["review_retirement_pending"])

    def test_deadline_checked_between_rows(self):
        self.other()
        clock = [0]
        original = stages.StageRunner.invalidate
        def invalidate(runner, *args, **kwargs):
            result = original(runner, *args, **kwargs)
            clock[0] = 2
            return result
        pipeline = unattended.UnattendedPipeline(self.root, policy=RunSafetyPolicy(seconds=1), clock=lambda: clock[0])
        with patch.object(stages.StageRunner, "invalidate", new=invalidate):
            report = pipeline.run()
        self.assertEqual(report["review_retirement"], {"state": "pending", "processed": 1})
        self.assertEqual(report["safety"]["stop_code"], "time_budget_exceeded")
        self.blocked()
        Pipeline(self.root).run()
        self.assertFalse(status(self.root)["review_retirement_pending"])

    def test_unattended_cancellation_keeps_hold_and_safety_acknowledgement(self):
        with patch.object(stages.StageRunner, "invalidate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                unattended.UnattendedPipeline(self.root).run()
        self.blocked()
        report = unattended.UnattendedPipeline(self.root).run()
        self.assertEqual(report["safety"]["stop_code"], "interrupted_attempt")
        self.assertEqual(report["review_retirement"]["state"], "pending")
        Pipeline(self.root).run()  # Manual retirement does not clear unattended safety state.
        report = unattended.UnattendedPipeline(self.root).run()
        self.assertEqual(report["safety"]["stop_code"], "interrupted_attempt")

    def test_unattended_failure_has_fixed_reported_code(self):
        with patch.object(stages.StageRunner, "invalidate", side_effect=RuntimeError("synthetic failure")):
            report = unattended.UnattendedPipeline(self.root).run()
        self.assertEqual(report["safety"]["stop_code"], "review_authority_retirement_failed")
        self.assertEqual(report["review_retirement"]["failure_code"], "review_authority_retirement_failed")
        self.assertEqual(report["exit_code"], 2)
        self.blocked()

    def test_pending_hold_blocks_rollback_without_changing_staged_bytes(self):
        publish.publish(self.root, self.proposal, staging=self.target)
        before = (self.target / "content" / (self.proposal + ".md")).read_bytes()
        pipeline = Pipeline(self.root)
        with root_lock(self.root):
            pipeline._open_stage_run()
            pipeline._arm_review_retirement()
        with self.assertRaisesRegex(publish.PublishError, "review_authority_retirement_pending"):
            publish.rollback(self.root, self.proposal, staging=self.target)
        self.assertEqual((self.target / "content" / (self.proposal + ".md")).read_bytes(), before)

    def test_malformed_hold_cannot_reauthorize_or_clear(self):
        with store.ledger(self.root / "ledger.sqlite") as con:
            con.execute("INSERT INTO ledger_meta VALUES(?,?)", (review_retirement.KEY, "invalid"))
            con.commit()
        with self.assertRaisesRegex(publish.PublishError, "review_authority_retirement_state_invalid"):
            publish.approve(self.root, self.proposal, owner_id="synthetic-owner")
        with self.assertRaisesRegex(publish.PublishError, "review_authority_retirement_state_invalid"):
            publish.publish(self.root, self.proposal, staging=self.target)
        with self.assertRaises(RunError):
            Pipeline(self.root).run()
        self.assertEqual(self.rows("SELECT value FROM ledger_meta WHERE key=?", (review_retirement.KEY,)), [("invalid",)])

    def test_non_test_independent_controller_bindings_and_approval_preserved(self):
        subject, proposal = independent_proposal(self.root, self.base / "inbox")
        publish.approve(self.root, proposal, owner_id="synthetic-owner")
        challenge_subject, challenge_proposal = independent_proposal(self.root, self.base / "challenge-inbox",
            reviewer="challenge:synthetic", original=b"FICTIONAL TEST POLICY: Historical automated declaration.")
        publish.approve(self.root, challenge_proposal, owner_id="synthetic-owner")
        before = self.rows("SELECT * FROM stage_state WHERE original_sha256=?", (subject,))
        approval = self.rows("SELECT * FROM proposals WHERE id=?", (proposal,))
        receipt = self.rows("SELECT receipt_sha256 FROM stage_state WHERE original_sha256=? AND stage='review'", (subject,))[0][0]
        self.assertEqual(self.rows("SELECT test_only FROM stage_validation_authority WHERE receipt_sha256=?", (receipt,)), [(0,)])
        # Seed the cached row after real intake, which already retired the first fixture.
        preaccepted_publication.seed(Pipeline(self.root), b"late-synthetic-cached-original", "", until="review")
        pipeline = Pipeline(self.root)
        with root_lock(self.root):
            stage = pipeline._open_stage_run()
            self.assertTrue(pipeline._reconcile_review_authority(stage))
        self.assertEqual(self.rows("SELECT * FROM stage_state WHERE original_sha256=?", (subject,)), before)
        self.assertEqual(self.rows("SELECT * FROM proposals WHERE id=?", (proposal,)), approval)
        self.assertEqual(self.rows("SELECT status FROM stage_state WHERE original_sha256=? AND stage='review'", (challenge_subject,)), [("pending",)])
        self.assertEqual(self.rows("SELECT owner_approval FROM proposals WHERE id=?", (challenge_proposal,)), [("none",)])
        self.assertFalse(publish.publish(self.root, proposal, staging=self.target)["reused"])
        self.assertEqual(stages.counts(self.root / "ledger.sqlite")["publication_ready"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
