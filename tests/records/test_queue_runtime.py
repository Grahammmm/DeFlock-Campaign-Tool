"""Small actual-WP1 synthetic claims; no promotions, corpus or cloud calls."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from campaign_tool.records import queue_runtime as runtime
from campaign_tool.records import queue

AVAILABLE = importlib.util.find_spec("campaign_tool.records.ledger") is not None


class PathsAdapter:
    def __init__(self, path):
        self.path = str(path)
    def __call__(self, item):
        return {"denominator": {"kind": "items", "total": 1},
                "derived_text": [self.path], "page_images": []}


def stalled_adapter(item):
    time.sleep(2)
    return {"blocked_reason": "synthetic_stall"}


def blocked_adapter(item):
    return {"blocked_reason": "synthetic_parser_unavailable"}


def record_selected_adapter(item):
    return {"adapter": "untrusted-callback", "command": "not-executed"}


@unittest.skipUnless(AVAILABLE, "WP1 stage authority dependency required")
class QueueRuntimeTests(unittest.TestCase):
    def setUp(self):
        from campaign_tool.records.ledger import store, stages
        self.store, self.stages = store, stages
        self.temp = tempfile.TemporaryDirectory(prefix="wp7-runtime-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.control = self.root / "control"
        self.control.mkdir(mode=0o700)
        self.reference = self.root / "derived.txt"
        self.reference.write_bytes(b"synthetic derived fixture")
        self.reference.chmod(0o600)
        self.database = self.root / "ledger.sqlite"
        store.initialize(self.database)
        self.raw = {}
        count = 21 if "starvation" in self._testMethodName else 1
        for index in range(count):
            raw = ("synthetic original " + str(index)).encode()
            self.raw[hashlib.sha256(raw).hexdigest()] = raw
        self.subjects = sorted(self.raw)
        self.run_id = "synthetic-runtime-run"
        self.config = hashlib.sha256(b"synthetic-runtime-config").hexdigest()
        stamp = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database) as c:
            c.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (self.run_id, "stage-runner", stamp, None, "synthetic-engine", None,
                       self.config, None, "running", "{}"))
            for subject, raw in self.raw.items():
                scope = "out_of_scope" if self._testMethodName == "test_scope_excluded_never_claimed" else "in_scope"
                c.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (subject, len(raw), "text/plain", stamp, "record", scope, None, "captured", "text", "{}"))
                for stage in queue.STAGES:
                    c.execute("INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)",
                              (subject, stage, "pending", None, "unassigned", stamp, self.run_id, None))
        self.runner = self.make_runner("synthetic-worker")
        self.adapter = PathsAdapter(self.reference)
        self.active = []
        self.addCleanup(self.close_all)

    def close_all(self):
        for instance in self.active:
            instance.close()

    def make_runner(self, owner):
        return self.stages.testing_runner(self.database, run_id=self.run_id, owner=owner,
            engine_version="synthetic-engine", config_sha256=self.config, validators={}, version="synthetic-runtime")

    def open(self, runner=None, adapter=None, **kwargs):
        instance = runtime.QueueRuntime(runner or self.runner, self.control, prepare=adapter or self.adapter,
                                        adapter_id="synthetic-packet", adapter_version="v1", **kwargs)
        self.active.append(instance)
        return instance

    def prepare(self, subject=None):
        subject = subject or self.subjects[0]
        self.runner.set_content(subject, "preserve", self.raw[subject], author_id=self.runner.owner, tier="A")

    def claim_count(self):
        with sqlite3.connect(self.database) as c:
            return c.execute("SELECT count(*) FROM stage_claims").fetchone()[0]

    def reopen(self, instance):
        instance.close()
        self.active.remove(instance)
        return self.open()

    def test_starvation_unprepared_top20_reaches_healthy_21st(self):
        healthy = self.subjects[-1]
        self.prepare(healthy)
        instance = self.open()
        first = instance.advance()
        self.assertEqual(len(first["attempts"]), 20)
        self.assertTrue(all(a["reason"] == "current_content_required" for a in first["attempts"]))
        instance = self.reopen(instance)
        second = instance.advance(max_attempts=1)
        item = second["attempts"][0]
        self.assertEqual(item["subject"], healthy)
        self.assertEqual(item["state"], "packet_ready")
        self.assertEqual(second["snapshot_originals"], 21)
        self.assertEqual(second["cursor_sequence"], 21)
        self.assertEqual(self.claim_count(), 1)

    def test_starvation_pending_packet_retries_do_not_hide_healthy_21st(self):
        healthy = self.subjects[-1]
        for subject in self.subjects:
            self.prepare(subject)
        instance = self.open()
        real = runtime._publish
        def selective(path, raw):
            if json.loads(raw)["original_sha256"] != healthy:
                raise OSError("synthetic disk fault")
            return real(path, raw)
        with mock.patch.object(runtime, "_publish", side_effect=selective):
            first = instance.advance()
            self.assertEqual(len(first["attempts"]), 20)
            self.assertTrue(all(a["state"] == "packet_staged" for a in first["attempts"]))
            second = instance.advance(max_attempts=1)
        self.assertEqual(second["attempts"][0]["subject"], healthy)
        self.assertEqual(second["attempts"][0]["state"], "packet_ready")

    def test_packet_exact_template_denominator_and_private_paths(self):
        self.prepare()
        instance = self.open()
        item = instance.advance()["attempts"][0]
        packet = instance.read_packet(item["attempt_id"])
        self.assertEqual(packet["denominator"], {"kind": "items", "total": 1})
        self.assertEqual(packet["derived_text"], [str(self.reference)])
        self.assertEqual(packet["receipt_template"]["content_sha256"], self.subjects[0])
        self.assertEqual(packet["receipt_template"]["input_hashes"], {"original": self.subjects[0]})
        self.assertIsNone(packet["receipt_template"]["verdict"])
        self.assertIsNone(packet["receipt_template"]["coverage"]["covered"])
        self.assertFalse(packet["reference_bytes_verified"])
        self.assertEqual(Path(item["packet_path"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.control.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.stages.counts(self.database)["stages"]["preserve"]["in_progress"], 1)
        self.assertEqual(self.stages.counts(self.database)["verified_seven_stage_complete"], 0)

    def test_crash_after_wp1_claim_recovers_same_token(self):
        self.prepare()
        instance = self.open()
        def crash(phase):
            if phase == "after_claim_before_journal":
                raise RuntimeError("synthetic crash")
        with self.assertRaises(RuntimeError):
            instance.advance(fault=crash)
        self.assertEqual(self.claim_count(), 1)
        instance = self.reopen(instance)
        result = instance.advance()
        item = result["attempts"][0]
        self.assertEqual(item["state"], "packet_ready")
        self.assertEqual(self.claim_count(), 1)
        self.assertTrue(instance.read_packet(item["attempt_id"])["claim"]["reused"])

    def test_crash_after_exposure_replays_exact_packet_without_prepare(self):
        self.prepare()
        instance = self.open()
        def crash(phase):
            if phase == "after_packet_exposure":
                raise RuntimeError("synthetic crash")
        with self.assertRaises(RuntimeError):
            instance.advance(fault=crash)
        before = next(instance.packets.glob("*.json")).read_bytes()
        instance = self.reopen(instance)
        with mock.patch.object(runtime, "_prepare", side_effect=AssertionError("must not repeat adapter")):
            result = instance.advance()
        item = result["attempts"][0]
        self.assertEqual(item["state"], "packet_ready")
        self.assertEqual(Path(item["packet_path"]).read_bytes(), before)
        self.assertEqual(self.claim_count(), 1)

    def test_partial_packet_write_recovers_without_partial_final_file(self):
        self.prepare()
        instance = self.open()
        def fail(path, raw):
            path.write_bytes(raw[:10])
            raise OSError("synthetic interrupted write")
        with mock.patch.object(runtime, "_write_new", side_effect=fail):
            result = instance.advance()
        item = result["attempts"][0]
        self.assertEqual(item["state"], "packet_staged")
        self.assertEqual(item["reason"], "packet_write_incomplete")
        self.assertFalse(Path(item["packet_path"]).exists())
        with mock.patch.object(runtime, "_prepare", side_effect=AssertionError("must use journal")):
            replay = instance.advance()
        self.assertEqual(replay["attempts"][0]["state"], "packet_ready")
        instance.read_packet(item["attempt_id"])

    def test_other_owner_live_lease_not_stolen(self):
        self.prepare()
        claim = self.runner.claim(self.subjects[0], "preserve", ttl_seconds=2700)
        other = self.make_runner("other-worker")
        result = self.open(other).advance()
        self.assertEqual(result["attempts"], [])
        self.assertEqual(self.claim_count(), 1)
        with sqlite3.connect(self.database) as c:
            self.assertEqual(c.execute("SELECT owner FROM work_leases").fetchone()[0], self.runner.owner)
        self.assertTrue(claim["claim_id"])

    def test_packet_cannot_be_consumed_by_other_runner(self):
        self.prepare()
        instance = self.open()
        item = instance.advance()["attempts"][0]
        other = self.open(self.make_runner("other-worker"))
        with self.assertRaisesRegex(queue.QueueError, "owner_run"):
            other.read_packet(item["attempt_id"])

    def test_scope_excluded_never_claimed(self):
        result = self.open().advance()
        self.assertEqual(result["excluded"], 1)
        self.assertEqual(result["attempts"], [])
        self.assertEqual(self.claim_count(), 0)

    def test_prepare_timeout_is_durable_and_bounded(self):
        self.prepare()
        instance = self.open(adapter=stalled_adapter, prepare_timeout_seconds=0.05)
        start = time.monotonic()
        result = instance.advance(budget_seconds=1)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(result["attempts"][0]["state"], "blocked")
        self.assertIn(result["attempts"][0]["reason"], ("prepare_timeout", "prepare_adapter_failed"))
        row = instance.db.execute("SELECT reason FROM attempts").fetchone()
        self.assertEqual(row[0], result["attempts"][0]["reason"])

    def test_adapter_blocked_reason_is_durable(self):
        self.prepare()
        instance = self.open(adapter=blocked_adapter)
        result = instance.advance()
        self.assertEqual(result["attempts"][0]["reason"], "synthetic_parser_unavailable")
        self.assertEqual(self.stages.counts(self.database)["verified_seven_stage_complete"], 0)

    def test_record_cannot_select_adapter_or_command(self):
        self.prepare()
        result = self.open(adapter=record_selected_adapter).advance()
        self.assertEqual(result["attempts"][0]["reason"], "invalid_or_stale_packet_metadata")

    def test_finished_wp1_run_fails_closed(self):
        self.prepare()
        instance = self.open()
        with sqlite3.connect(self.database) as c:
            c.execute("UPDATE runs SET status='complete',ended_at=? WHERE run_id=?",
                      (datetime.now(timezone.utc).isoformat(), self.run_id))
        with self.assertRaisesRegex(self.stages.StageError, "live_runner_run_required"):
            instance.advance()

    def test_expired_claim_cannot_replay_packet(self):
        self.prepare()
        instance = self.open()
        item = instance.advance()["attempts"][0]
        real = self.stages.datetime
        class Future(real):
            @classmethod
            def now(cls, tz=None):
                return real.now(tz) + timedelta(hours=1)
        with mock.patch.object(self.stages, "datetime", Future):
            with self.assertRaisesRegex(self.stages.StageError, "lease_expired"):
                instance.read_packet(item["attempt_id"])

    def test_mutated_packet_and_changed_reference_fail_closed(self):
        self.prepare()
        instance = self.open()
        item = instance.advance()["attempts"][0]
        self.reference.write_bytes(b"changed synthetic reference")
        with self.assertRaisesRegex(queue.QueueError, "reference_changed"):
            instance.read_packet(item["attempt_id"])
        Path(item["packet_path"]).write_bytes(b"{}")
        with self.assertRaisesRegex(queue.QueueError, "persisted_packet_changed"):
            instance.read_packet(item["attempt_id"])

    def test_reference_symlink_blocked(self):
        self.prepare()
        alias = self.root / "alias"
        alias.symlink_to(self.reference)
        result = self.open(adapter=PathsAdapter(alias)).advance()
        self.assertEqual(result["attempts"][0]["reason"], "invalid_or_stale_packet_metadata")

    def test_world_readable_root_rejected(self):
        self.control.chmod(0o755)
        with self.assertRaisesRegex(queue.QueueError, "owner_only"):
            self.open()

    def test_runtime_budget_and_attempt_bounds(self):
        instance = self.open()
        for arguments in ({"max_attempts": 21}, {"max_attempts": True},
                          {"budget_seconds": 2701}, {"budget_seconds": 0}):
            with self.subTest(arguments=arguments), self.assertRaises(queue.QueueError):
                instance.advance(**arguments)

    def test_public_repository_control_root_rejected(self):
        (self.control / ".git").write_text("synthetic repository marker")
        with self.assertRaisesRegex(queue.QueueError, "inside_repository"):
            self.open()


if __name__ == "__main__":
    unittest.main()
