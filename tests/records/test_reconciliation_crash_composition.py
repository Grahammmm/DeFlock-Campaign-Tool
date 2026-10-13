"""Synthetic installed-preserver crash boundaries, without semantic authority."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from campaign_tool.records.ledger import stages, store
from campaign_tool.records.runner.canonical_reconciliation import CanonicalOriginalReconciler
from campaign_tool.records.run import root_lock
from tests.records import test_original_reconciliation as fixtures


class ReconciliationCrashCompositionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OriginalReconciliationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.enroll()

    def interrupted_accept(self):
        with patch.object(stages.StageRunner, "promote", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.fixture.backend.accept(self.fixture.sha)
        self.assertEqual(self.fixture.rows("SELECT status FROM stage_state WHERE stage='preserve'"),
                         [("in_progress",)])
        self.assertEqual(self.fixture.rows("SELECT count(*) FROM receipts"), [(0,)])

    def new_backend(self, run_id):
        f = self.fixture
        identity = deepcopy(f.identity)
        identity["run_id"] = run_id
        backend = CanonicalOriginalReconciler(f.backend.mail_root, f.backend.output, f.database, policy=f.policy)
        backend.start_run(identity)
        return backend, identity

    def test_same_run_retry_reuses_claim_without_semantic_acceptance(self):
        self.interrupted_accept()
        f = self.fixture
        claims = f.rows("SELECT claim_id FROM stage_claims")
        result = f.backend.accept(f.sha)
        self.assertEqual(f.rows("SELECT claim_id FROM stage_claims"), claims)
        self.assertFalse(result["test_only"])
        self.assertFalse(result["semantic_acceptance"])
        self.assertEqual(f.rows("SELECT DISTINCT status FROM stage_state WHERE stage!='preserve'"), [("pending",)])

    def test_recovered_run_retries_immediately_without_waiting_for_claim_ttl(self):
        self.interrupted_accept()
        f = self.fixture
        self.assertEqual(f.backend.recover_run(f.identity), "finalized")
        backend, _ = self.new_backend("fictional-recovery-next")
        result = backend.accept(f.sha)
        self.assertFalse(result["test_only"])
        self.assertFalse(result["semantic_acceptance"])
        self.assertEqual(f.rows("SELECT status FROM runs WHERE run_id=?", (f.identity["run_id"],)), [("interrupted",)])
        self.assertEqual(f.rows("SELECT count(*) FROM receipts"), [(1,)])

    def test_completed_acceptance_replays_in_new_run_without_extra_receipts(self):
        f = self.fixture
        first = f.backend.accept(f.sha)
        f.backend.finish_run(f.identity, "slice_completed", {"fictional_preservation": 1})
        backend, _ = self.new_backend("fictional-replay-next")
        with store.ledger(f.database, readonly=True) as con:
            before = list(con.iterdump())
        result = backend.accept(f.sha)
        self.assertEqual(result["receipt_sha256"], first["receipt_sha256"])
        self.assertTrue(result["reused"])
        self.assertFalse(result["test_only"])
        with store.ledger(f.database, readonly=True) as con:
            self.assertEqual(list(con.iterdump()), before)

    def test_recovery_retry_after_finalization_does_not_release_other_run_claim(self):
        self.interrupted_accept()
        f = self.fixture
        # Simulate death after durable finalization and before lease recovery.
        f.backend._finalize(f.identity, "interrupted", {"fictional_crash": True})
        _, identity = self.new_backend("fictional-other-live-run")
        subject, source, other = f.secondary(b"fictional other live original")
        other.run_identity = identity
        with f.readonly():
            other.enroll_local_capture(source, original_sha256=subject)
        with patch.object(stages.StageRunner, "promote", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                other.accept(subject)
        before = f.rows("SELECT * FROM work_leases WHERE item_key=?", ("stage:" + subject + ":preserve",))
        state = f.rows("SELECT * FROM stage_state WHERE original_sha256=?", (subject,))
        self.assertEqual(f.backend.recover_run(f.identity), "already_finalized")
        self.assertEqual(f.rows("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'", (f.sha,)), [("pending",)])
        self.assertEqual(f.rows("SELECT * FROM work_leases WHERE item_key=?", ("stage:" + subject + ":preserve",)), before)
        self.assertEqual(f.rows("SELECT * FROM stage_state WHERE original_sha256=?", (subject,)), state)
        self.assertFalse(other.accept(subject)["test_only"])

    def test_recovery_requires_same_exclusive_root_lock_as_acceptance(self):
        self.interrupted_accept()
        f = self.fixture
        with store.ledger(f.database, readonly=True) as con:
            before = list(con.iterdump())
        with root_lock(f.base), self.assertRaisesRegex(Exception, "root_locked"):
            f.backend.recover_run(f.identity)
        with store.ledger(f.database, readonly=True) as con:
            self.assertEqual(list(con.iterdump()), before)

    def test_recovery_binding_conflict_refuses_before_lease_mutation(self):
        self.interrupted_accept()
        f = self.fixture
        identity = deepcopy(f.identity)
        identity["config_sha256"] = "f" * 64
        with store.ledger(f.database, readonly=True) as con:
            before = list(con.iterdump())
        with self.assertRaisesRegex(ValueError, "canonical_run_binding_conflict"):
            f.backend.recover_run(identity)
        with store.ledger(f.database, readonly=True) as con:
            self.assertEqual(list(con.iterdump()), before)


if __name__ == "__main__":
    unittest.main()
