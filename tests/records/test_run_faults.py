"""Fault injection for ``records run``: a crash after any stage, a crash inside a
stage after its lease is taken but before promotion, and two concurrent runners
on one root. The next run must resume without duplicate promotions, duplicate
proposals or a second accepted identity, and the interrupted run must stay
visible as unfinished (health reports it as stalled).
"""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from campaign_tool.records import schedule
from campaign_tool.records.ledger import stages
from campaign_tool.records.run import STAGE_ORDER, Pipeline
from tests.records.test_run_slice import synthetic_email


class Crash(BaseException):
    """Not an Exception: the stage loop must not swallow it, like SIGKILL would not be."""


class FaultTests(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.tmp = tempfile.TemporaryDirectory(prefix="records-faults-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        os.chmod(self.base, 0o700)
        self.inbox = self.base / "inbox"
        self.inbox.mkdir(mode=0o700)
        (self.inbox / "a.eml").write_bytes(synthetic_email())
        (self.inbox / "b.eml").write_bytes(synthetic_email(message_id="<b@agency.example.invalid>",
                                                           body="Second response with the same policy attached.\n"))
        self.root = self.base / "root"

    def query(self, sql, values=()):
        con = sqlite3.connect(self.root / "ledger.sqlite")
        try:
            return con.execute(sql, values).fetchall()
        finally:
            con.close()

    def snapshot(self):
        """Everything that must be identical after a crash-and-resume and after a clean run."""
        return {
            "stage_state": self.query("SELECT original_sha256,stage,status,receipt_sha256 FROM stage_state ORDER BY 1,2"),
            "receipts": self.query("SELECT count(*) FROM receipts"),
            "proposals": self.query("SELECT id,public_content_sha256,owner_approval FROM proposals ORDER BY id"),
            "units": self.query("SELECT count(*) FROM units"),
            "originals": self.query("SELECT count(*) FROM originals"),
        }

    _reference = None

    def clean_reference(self):
        """The snapshot a never-interrupted run produces for the same inbox (computed once)."""
        if FaultTests._reference is not None:
            return FaultTests._reference
        FaultTests._reference = self._clean_reference()
        return FaultTests._reference

    def _clean_reference(self):
        with tempfile.TemporaryDirectory(prefix="records-clean-", dir=self.base) as other:
            os.chmod(other, 0o700)
            root = Path(other) / "root"
            Pipeline(root).run(self.inbox)
            con = sqlite3.connect(root / "ledger.sqlite")
            try:
                return {
                    "stages": sorted((r[1], r[2]) for r in con.execute("SELECT original_sha256,stage,status FROM stage_state")),
                    "receipts": con.execute("SELECT count(*) FROM receipts").fetchone()[0],
                    "proposals": con.execute("SELECT count(*) FROM proposals").fetchone()[0],
                }
            finally:
                con.close()

    def assert_matches_clean_run(self, snapshot):
        reference = self.clean_reference()
        self.assertEqual(sorted((r[1], r[2]) for r in snapshot["stage_state"]), reference["stages"])
        self.assertEqual(snapshot["receipts"][0][0], reference["receipts"])
        self.assertEqual(len(snapshot["proposals"]), reference["proposals"])

    def crash_after(self, stage_name):
        """Run the pipeline and crash right after the real ``stage_name`` returns the first time."""
        real = getattr(Pipeline, "stage_" + stage_name)
        fired = []

        def wrapped(pipeline, subject, stage):
            result = real(pipeline, subject, stage)
            if not fired:
                fired.append(subject)
                raise Crash(stage_name)
            return result

        with patch.object(Pipeline, "stage_" + stage_name, wrapped):
            with self.assertRaises(Crash):
                Pipeline(self.root).run(self.inbox)
        return fired[0]

    def test_crash_after_every_stage_then_resume_matches_a_clean_run(self):
        for name in STAGE_ORDER:
            with self.subTest(stage=name):
                if self.root.exists():
                    shutil.rmtree(self.root)
                subject = self.crash_after(name)
                # The crashed run is still "running" with no end: visible, not hidden.
                crashed = self.query("SELECT run_id,status,ended_at FROM runs WHERE kind='stage-runner'")
                self.assertEqual([(r[1], r[2]) for r in crashed], [("running", None)])
                before = self.snapshot()
                done_before = {(r[0], r[1]): r[3] for r in before["stage_state"] if r[2] == "done"}
                # Fresh pipeline object = fresh process semantics: new run id, no in-memory state.
                report = Pipeline(self.root).run(self.inbox)
                after = self.snapshot()
                # Every stage that was done before the resume kept the very same receipt.
                for key, receipt in done_before.items():
                    row = next(r for r in after["stage_state"] if (r[0], r[1]) == key)
                    self.assertEqual((row[2], row[3]), ("done", receipt), key)
                self.assert_matches_clean_run(after)
                self.assertEqual(report["intake"]["messages"], 2)
                self.assertEqual(len(report["intake"]["preserved"]), 0, "replayed inbox must preserve nothing new")
                runs = self.query("SELECT status,ended_at IS NOT NULL FROM runs WHERE kind='stage-runner' ORDER BY started_at")
                self.assertEqual([tuple(r) for r in runs], [("interrupted", 1), ("completed", 1)])
                self.assertEqual(report["recovery"]["interrupted_runs"], [crashed[0][0]])

    def test_crash_inside_a_stage_after_lease_before_promotion_resumes_without_a_second_identity(self):
        real_promote = stages.StageRunner.promote
        fired = []

        def promote(runner, subject, stage, receipt_bytes, *, claim_id=None):
            if stage == "catalog" and not fired:
                fired.append((subject, claim_id))
                raise Crash("before-promote")
            return real_promote(runner, subject, stage, receipt_bytes, claim_id=claim_id)

        with patch.object(stages.StageRunner, "promote", promote):
            with self.assertRaises(Crash):
                Pipeline(self.root).run(self.inbox)
        subject, claim_id = fired[0]
        state = self.query("SELECT status FROM stage_state WHERE original_sha256=? AND stage='catalog'", (subject,))
        self.assertEqual(state, [("in_progress",)], "a taken lease is visible as in_progress")
        # Resume at once (lease not expired). The run must recover the stage, not wait for TTL
        # and not create a second accepted identity.
        report = Pipeline(self.root).run(self.inbox)
        self.assertEqual(report["recovery"]["recovered_leases"], [[subject, "catalog"]])
        self.assertEqual(self.query("SELECT count(*) FROM alerts WHERE key LIKE 'run:lease_recovered:%'"), [(1,)])
        after = self.snapshot()
        self.assertEqual(self.query("SELECT status FROM stage_state WHERE original_sha256=? AND stage='catalog'", (subject,)),
                         [("done",)])
        self.assertEqual(self.query("SELECT count(*) FROM stage_state WHERE original_sha256=? AND stage='catalog'", (subject,)),
                         [(1,)])
        self.assert_matches_clean_run(after)

    def test_health_reports_the_crashed_run_as_stalled_then_ok_after_resume(self):
        self.crash_after("extract")
        started = self.query("SELECT started_at FROM runs WHERE kind='stage-runner'")[0][0]
        later = datetime.fromisoformat(started).astimezone(ZoneInfo("UTC")) + timedelta(hours=1)
        slot_time = schedule.due_slots(later, 1)[0]
        # Pretend the crashed run belonged to the latest slot: move its start to the slot.
        con = sqlite3.connect(self.root / "ledger.sqlite")
        con.execute("UPDATE runs SET started_at=? WHERE kind='stage-runner'", ((slot_time + timedelta(minutes=1)).isoformat(),))
        con.commit()
        con.close()
        report = schedule.health(self.root, now=slot_time + timedelta(hours=1))
        self.assertEqual(report["status"], "stalled")
        Pipeline(self.root).run(self.inbox)
        con = sqlite3.connect(self.root / "ledger.sqlite")
        con.execute("UPDATE runs SET started_at=?,ended_at=? WHERE status='completed' AND kind='stage-runner'",
                    ((slot_time + timedelta(minutes=2)).isoformat(), (slot_time + timedelta(minutes=5)).isoformat()))
        con.commit()
        con.close()
        report = schedule.health(self.root, now=slot_time + timedelta(hours=1))
        self.assertEqual(report["slots"][0]["status"], "ok")

    def test_second_concurrent_runner_on_one_root_is_refused_and_state_stays_single(self):
        from campaign_tool.records.run import RunError
        errors, reports = [], []
        holder_inside = threading.Event()
        release = threading.Event()
        pipelines = [Pipeline(self.root), Pipeline(self.root)]  # prepared before either runs
        original = pipelines[0].recover

        def paused_recover():
            holder_inside.set()
            release.wait(timeout=120)
            return original()

        pipelines[0].recover = paused_recover

        def worker(pipeline):
            try:
                reports.append(pipeline.run(self.inbox))
            except RunError as error:
                errors.append(str(error))

        threads = [threading.Thread(target=worker, args=(pipelines[0],))]
        threads[0].start()
        self.assertTrue(holder_inside.wait(timeout=60), "first runner never took the lock")
        threads.append(threading.Thread(target=worker, args=(pipelines[1],)))
        threads[1].start()
        threads[1].join(timeout=60)
        release.set()
        for thread in threads:
            thread.join(timeout=600)
        self.assertEqual(errors, ["root_locked"])
        self.assertEqual(len(reports), 1)
        # One runner did all the work; a quiet replay changes nothing and there is exactly one
        # receipt per (subject, stage) with no duplicate proposals.
        Pipeline(self.root).run(self.inbox)
        after = self.snapshot()
        self.assert_matches_clean_run(after)
        duplicates = self.query("SELECT public_content_sha256, count(*) FROM proposals GROUP BY 1 HAVING count(*) > 1")
        self.assertEqual(duplicates, [])
        receipts = self.query("SELECT subject_sha256,stage,count(*) FROM receipts GROUP BY 1,2 HAVING count(*) > 1")
        self.assertEqual(receipts, [], "a stage was promoted twice for one subject")


if __name__ == "__main__":
    unittest.main()
