"""Explicit large SQLite boundary acceptance; not default discovery.

Builds 10001 actual current review rows exclusively through testing_runner.
All callbacks remain test_only; no SQL population mocks or disabled protections.
Run with python -B -m tests.records.retirement_boundary in supported Linux CI.
"""
import json
from pathlib import Path
import tempfile
import time
import unittest
from campaign_tool.records import publish, review_retirement
from campaign_tool.records.ledger import stages, store
from campaign_tool.records.run import Pipeline, root_lock
from tests.records import preaccepted_publication


class BoundaryTests(unittest.TestCase):
    def test_real_10000_10001_rows_hold_and_resume(self):
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="retirement-boundary-") as temporary:
            root, target = Path(temporary).resolve() / "root", Path(temporary).resolve() / "target"
            pipeline = Pipeline(root)
            proposal = preaccepted_publication.policy(pipeline)
            publish.approve(root, proposal, owner_id="synthetic-owner")
            def count(sql):
                with store.ledger(pipeline.root.ledger, readonly=True) as con:
                    return con.execute(sql).fetchone()[0]
            for index in range(9999):
                preaccepted_publication.seed(pipeline, f"synthetic-boundary-original:{index}".encode(), "", until="review")
                if (index + 1) % 1000 == 0:
                    print(json.dumps({"synthetic_rows_seeded": index + 2}), flush=True)
            self.assertEqual(count("SELECT count(*) " + review_retirement.ELIGIBLE), 10000)
            with root_lock(root):
                pipeline._open_stage_run()
                pipeline._arm_review_retirement()
            self.assertEqual(count("SELECT count(*) " + review_retirement.ELIGIBLE), 10000)
            preaccepted_publication.seed(pipeline, b"synthetic-boundary-original:late", "", until="review")
            self.assertEqual(count("SELECT count(*) " + review_retirement.ELIGIBLE), 10001)
            with self.assertRaisesRegex(publish.PublishError, "review_authority_retirement_pending"):
                publish.publish(root, proposal, staging=target)
            with root_lock(root):
                self.assertFalse(pipeline._reconcile_review_authority(pipeline._stage))
            self.assertEqual(pipeline.review_retirement["processed"], 400)
            self.assertEqual(count("SELECT count(*) " + review_retirement.ELIGIBLE), 9601)
            with self.assertRaisesRegex(publish.PublishError, "review_authority_retirement_pending"):
                publish.approve(root, proposal, owner_id="synthetic-owner")
            # A new pipeline/run resumes from protected current state, no saved SQL cursor.
            resumed = Pipeline(root)
            with root_lock(root):
                stage = resumed._open_stage_run()
                complete, batches = False, 0
                while not complete:
                    previous = resumed.review_retirement["processed"]
                    complete = resumed._reconcile_review_authority(stage)
                    self.assertLessEqual(resumed.review_retirement["processed"] - previous, 400)
                    batches += 1
                self.assertEqual(batches, 25)
                self.assertEqual(resumed.review_retirement["processed"], 9601)
                resumed._close_stage_run({"synthetic_boundary": True})
            self.assertEqual(count("SELECT count(*) " + review_retirement.ELIGIBLE), 0)
            self.assertEqual(count("SELECT count(*) FROM receipts WHERE stage='review'"), 10001)
            self.assertEqual(count("SELECT count(*) FROM stage_validation_authority WHERE test_only=0"), 0)
            self.assertEqual(count("SELECT count(*) FROM stage_events WHERE stage='review' AND reason LIKE '%cached_automated_or_test_review%'"), 10001)
            with store.ledger(pipeline.root.ledger, readonly=True) as con:
                self.assertFalse(review_retirement.pending(con))
            with self.assertRaisesRegex(publish.PublishError, "owner_approval_required"):
                publish.publish(root, proposal, staging=target)
            # Existing aggregate/orchestration cap is separate from retirement.
            with self.assertRaisesRegex(stages.StageError, "counts_metadata_bound_exceeded"):
                stages.counts(pipeline.root.ledger)
            print(json.dumps({"real_sqlite_rows": 10001, "initial_batch": 400, "remaining_after_initial_batch": 9601,
                              "resume_batches": 25, "history_review_receipts_retained": 10001,
                              "test_only": True, "population_mocked": False,
                              "full_root_counting": "blocked_counts_metadata_bound_exceeded",
                              "elapsed_seconds": round(time.monotonic() - started, 3)}), flush=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
