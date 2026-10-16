"""Offline claim fencing, bounded renewal and durable finalization recovery."""
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from runner.client import FakeWorkspace, WorkspaceError
from runner.loop import (ClaimLease, ClaimLost, ClaimedWorkspace, Context, Result,
                         ResultOutbox, Runner)
from tests.runner.helpers import Silent, settings
from tests.runner.test_client import FakeResponse, Opener, client, http_error


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class QueueClientTests(unittest.TestCase):
    def test_claim_token_on_result_and_renewal(self):
        opener = Opener([FakeResponse(200, b'{"job_id":"job_1"}'),
                         FakeResponse(200, b'{"leased_until":"later"}')])
        ws, _ = client(opener)
        ws.post_result("job_1", "done", {"count": 1}, {"sha256": "a" * 64}, claim_token="claim-a")
        ws.renew_job("job_1", "claim-a", 120)
        result, renewal = opener.requests
        self.assertEqual(json.loads(result["body"])["claim_token"], "claim-a")
        self.assertTrue(renewal["url"].endswith("/jobs/job_1/renew"))
        self.assertEqual(json.loads(renewal["body"]), {"claim_token": "claim-a", "lease_seconds": 120})

    def test_renewal_is_bounded_without_transport_retry(self):
        opener = Opener([http_error(503), FakeResponse(200)])
        ws, sleeps = client(opener)
        with self.assertRaises(WorkspaceError):
            ws.renew_job("job_1", "claim-a", 300)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(ws.max_retries, 4)  # normal result transport is unchanged

    def test_result_timeout_then_exact_transport_replay(self):
        opener = Opener([TimeoutError(), FakeResponse(200, b'{"job_id":"job_1","state":"done"}')])
        ws, _ = client(opener)
        ws.post_result("job_1", "done", {"count": 2}, claim_token="claim-a")
        self.assertEqual(opener.requests[0]["body"], opener.requests[1]["body"])

    def test_handler_transport_retries_recheck_lease_and_stop(self):
        for reason in ("lease", "stop"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                clock = Clock()
                stopped = [False]
                fake = FakeWorkspace(clock=clock)
                fake.enqueue("extract", {})
                job = fake.lease_job(30)
                claim = ClaimLease(fake, settings(tmp, lease_seconds=30), job, Silent(),
                                   monotonic=clock, wall_clock=clock, stopping=lambda: stopped[0])
                opener = Opener([http_error(503), FakeResponse(200)])
                ws, _ = client(opener)
                def backoff(_):
                    if reason == "lease":
                        clock.advance(31)
                    else:
                        stopped[0] = True
                ws.sleep = backoff
                with self.assertRaises(ClaimLost):
                    ClaimedWorkspace(ws, claim).put_original(b"synthetic")
                self.assertEqual(len(opener.requests), 1)

    def test_heartbeat_is_not_locked_out_by_inflight_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            fake = FakeWorkspace(clock=clock)
            fake.enqueue("extract", {})
            job = fake.lease_job(30)
            claim = ClaimLease(fake, settings(tmp, lease_seconds=30), job, Silent(),
                               monotonic=clock, wall_clock=clock)
            started, release = threading.Event(), threading.Event()
            def opener(request, timeout=None):
                started.set()
                self.assertTrue(release.wait(2))
                return FakeResponse(200, b'{}')
            ws, _ = client(opener)
            errors = []
            def upload():
                try:
                    ClaimedWorkspace(ws, claim).put_original(b"synthetic")
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=upload)
            thread.start()
            try:
                self.assertTrue(started.wait(1))
                clock.advance(20)
                self.assertTrue(claim.renew())
            finally:
                release.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.ws = FakeWorkspace(clock=self.clock)
        self.settings = settings(self.tmp.name, lease_seconds=30, max_job_seconds=180)
        self.ws.enqueue("extract", {})
        self.job = self.ws.lease_job(30)
        self.log = Silent()

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self, **kwargs):
        return ClaimLease(self.ws, self.settings, self.job, self.log,
                          monotonic=self.clock, wall_clock=self.clock, **kwargs)

    def test_long_handler_renews_exact_claim_but_runtime_is_absolute(self):
        claim = self.claim()
        for _ in range(4):
            self.clock.advance(20)
            self.assertTrue(claim.renew())
        self.assertGreater(self.clock(), Clock().now + 30)
        claim.check()
        self.assertEqual(self.ws.jobs[self.job["job_id"]]["attempt"], 1)
        self.clock.advance(100)
        with self.assertRaisesRegex(ClaimLost, "job_runtime_exhausted"):
            claim.renew()
        renewals = [c for c in self.ws.calls if c[0] == "renew"]
        self.assertEqual(len(renewals), 4)

    def test_heartbeat_loop_extends_lease_and_stops_at_budget(self):
        claim = self.claim()
        # Drive the production heartbeat loop with synthetic time, no sleep or model.
        real_wait = claim.closed.wait
        def wait(seconds):
            self.clock.advance(seconds)
            return real_wait(0)
        with patch.object(claim.closed, "wait", side_effect=wait):
            claim._heartbeat()
        renewals = [c for c in self.ws.calls if c[0] == "renew"]
        self.assertGreater(len(renewals), 2)
        self.assertLessEqual(self.ws._deadlines[self.job["job_id"]], claim.deadline)
        self.assertLessEqual(len(renewals), 18)

    def test_stop_and_finish_do_not_renew_again(self):
        stopped = [False]
        claim = self.claim(stopping=lambda: stopped[0])
        stopped[0] = True
        with self.assertRaisesRegex(ClaimLost, "runner_stopping"):
            claim.renew()
        self.assertFalse(any(c[0] == "renew" for c in self.ws.calls))
        finished = self.claim()
        finished.finish()
        self.assertFalse(finished.renew())
        with self.assertRaisesRegex(ClaimLost, "handler_finished"):
            ClaimedWorkspace(self.ws, finished).put_original(b"late synthetic write")

    def test_expired_claim_cannot_mutate_or_complete_replacement(self):
        claim = self.claim()
        proxy = ClaimedWorkspace(self.ws, claim)
        self.clock.advance(31)
        replacement = self.ws.lease_job(30)
        self.assertNotEqual(self.job["claim_token"], replacement["claim_token"])
        for name, args in (
            ("put_original", (b"synthetic private original",)),
            ("post_receipt", ({},)),
            ("post_correspondence", ({},)),
            ("propose", ("deploy_site", "v1", {}, "a" * 64, "runner")),
            ("put_site_file", ("v1", "index.html", b"hi", "text/html")),
        ):
            with self.assertRaises(ClaimLost, msg=name):
                getattr(proxy, name)(*args)
        with self.assertRaises(WorkspaceError):
            self.ws.post_result(self.job["job_id"], "done", claim_token=self.job["claim_token"])
        self.ws.post_result(replacement["job_id"], "done", claim_token=replacement["claim_token"])
        self.assertFalse(self.ws.originals)
        self.assertFalse(self.ws.proposals)
        self.assertFalse(self.ws.receipts)
        self.assertFalse(self.ws.site_files)
        self.assertFalse(self.ws.correspondence)

    def test_failed_renewal_fences_subsequent_mutations_even_before_expiry(self):
        claim = self.claim()
        self.ws.fail_next = 1
        with self.assertRaisesRegex(ClaimLost, "lease_renewal_failed"):
            claim.renew()
        with self.assertRaises(ClaimLost):
            ClaimedWorkspace(self.ws, claim).put_original(b"synthetic")
        self.assertFalse(self.ws.originals)
        self.assertTrue(claim.closed.is_set())

    def test_accepted_failure_replay_survives_next_attempt_without_changing_it(self):
        first = self.ws.post_result(self.job["job_id"], "failed", error="synthetic_failure",
                                    claim_token=self.job["claim_token"])
        next_job = self.ws.lease_job(30)
        replay = self.ws.post_result(self.job["job_id"], "failed", error="synthetic_failure",
                                     claim_token=self.job["claim_token"])
        self.assertEqual(first, replay)
        self.assertEqual(self.ws.jobs[next_job["job_id"]]["claim_token"], next_job["claim_token"])
        self.assertEqual(self.ws.jobs[next_job["job_id"]]["state"], "leased")
        with self.assertRaisesRegex(WorkspaceError, "result_conflict"):
            self.ws.post_result(self.job["job_id"], "done", claim_token=self.job["claim_token"])

    def test_new_claim_can_shorten_lease_but_renewal_cannot(self):
        self.ws.renew_job(self.job["job_id"], self.job["claim_token"], 300)
        self.ws.post_result(self.job["job_id"], "failed", claim_token=self.job["claim_token"])
        retry = self.ws.lease_job(60)
        deadline = self.ws._deadlines[retry["job_id"]]
        self.assertEqual(deadline, self.clock() + 60)
        self.clock.advance(10)
        self.ws.renew_job(retry["job_id"], retry["claim_token"], 30)
        self.assertEqual(self.ws._deadlines[retry["job_id"]], deadline)


class ResultRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = settings(self.tmp.name)
        self.ws = FakeWorkspace()
        self.log = Silent()
        self.handled = 0

    def tearDown(self):
        self.tmp.cleanup()

    def handler(self, ctx):
        self.handled += 1
        ctx.write("original.bin", b"synthetic original not in retry envelope")
        return Result("done", {"count": 2}, {"sha256": "a" * 64})

    def runner(self, ws=None, handler=None):
        return Runner(ws or self.ws, self.settings, {"extract": handler or self.handler}, self.log)

    def test_outage_retries_persisted_result_before_leasing_and_cleans_after_ack(self):
        self.ws.enqueue("extract", {"raw_private_input": "not for retry storage"})
        runner = self.runner()
        with patch.object(self.ws, "post_result", side_effect=WorkspaceError(503, "outage", retryable=True)):
            self.assertEqual(runner.step(), "backoff")
            envelope, = runner.outbox.pending()
            payload = envelope.read_bytes()
            self.assertNotIn(b"synthetic original", payload)
            self.assertNotIn(b"raw_private_input", payload)
            self.assertEqual(os.stat(envelope).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(envelope.parent).st_mode & 0o777, 0o700)
            self.assertFalse(list((self.settings.workdir / "scratch").glob("job-*")))
            self.assertEqual(runner.step(), "backoff")
        restarted = self.runner()
        self.assertEqual(restarted.step(), "done")
        self.assertEqual(self.handled, 1)
        self.assertEqual(len([c for c in self.ws.calls if c[0] == "lease"]), 1)
        self.assertFalse(envelope.exists())
        self.assertEqual(len(self.ws.results), 1)

    def test_lost_ack_replays_exact_result_once_after_restart(self):
        self.ws.enqueue("extract", {})
        def handler(ctx):
            self.handler(ctx)
            return Result("done", {"followups": [{"kind": "digest", "inputs": {"sha256": "a" * 64}}]})
        runner = self.runner(handler=handler)
        real_post = self.ws.post_result
        def lose_ack(*args, **kwargs):
            real_post(*args, **kwargs)
            raise WorkspaceError(0, "lost acknowledgment", retryable=True)
        with patch.object(self.ws, "post_result", side_effect=lose_ack):
            self.assertEqual(runner.step(), "backoff")
        self.assertEqual(self.runner().step(), "done")
        self.assertEqual(self.handled, 1)
        self.assertEqual(len(self.ws.jobs), 2)
        self.assertEqual(len(self.ws.results), 1)
        self.assertFalse(runner.outbox.pending())

    def test_process_crash_after_envelope_saved_restarts_delivery_only(self):
        self.ws.enqueue("extract", {})
        runner = self.runner()
        with patch.object(runner.outbox, "deliver", side_effect=SystemExit("synthetic crash")):
            with self.assertRaises(SystemExit):
                runner.step()
        self.assertEqual(len(runner.outbox.pending()), 1)
        self.assertEqual(self.runner().step(), "done")
        self.assertEqual(self.handled, 1)
        self.assertFalse(runner.outbox.pending())

    def test_persistent_outbox_recovers_after_ephemeral_scratch_is_replaced(self):
        self.settings.result_dir = Path(self.tmp.name) / "persistent-results"
        self.ws.enqueue("extract", {})
        runner = self.runner()
        with patch.object(self.ws, "post_result", side_effect=WorkspaceError(503, retryable=True)):
            self.assertEqual(runner.step(), "backoff")
        path, = runner.outbox.pending()
        shutil.rmtree(self.settings.workdir)
        restarted = self.runner()
        self.assertEqual(restarted.step(), "done")
        self.assertEqual(self.handled, 1)
        self.assertFalse(path.exists())
        self.assertEqual(os.stat(self.settings.result_dir).st_mode & 0o777, 0o700)

    def test_crash_between_fsync_and_scratch_cleanup_recovers(self):
        self.ws.enqueue("extract", {})
        job = self.ws.lease_job()
        ctx = Context(self.ws, self.settings, job, self.log)
        ctx.write("original.bin", b"synthetic")
        outbox = ResultOutbox(self.ws, self.settings, self.log)
        envelope = outbox.save(job, Result("done", {"count": 1}), ctx)
        self.assertTrue(ctx.dir.exists())
        self.assertEqual(self.runner().step(), "done")
        self.assertFalse(envelope.exists())
        self.assertFalse(ctx.dir.exists())
        self.assertEqual(self.handled, 0)

    def test_concurrent_scratch_and_result_files_never_share_cleanup(self):
        self.ws.enqueue("extract", {})
        job = self.ws.lease_job()
        a = Context(self.ws, self.settings, job, self.log)
        b = Context(self.ws, self.settings, {**job, "claim_token": "other-claim"}, self.log)
        a.write("a", b"a")
        b.write("b", b"b")
        outbox = ResultOutbox(self.ws, self.settings, self.log)
        pa = outbox.save(job, Result("done"), a)
        pb = outbox.save({**job, "claim_token": "other-claim"}, Result("done"), b)
        self.assertNotEqual(pa, pb)
        outbox.deliver(pa)
        self.assertFalse(a.dir.exists())
        self.assertTrue((b.dir / "b").exists())
        self.assertTrue(pb.exists())

    def test_conflicting_result_is_retained_for_operator_and_not_retried_forever(self):
        self.ws.enqueue("extract", {})
        runner = self.runner()
        with patch.object(self.ws, "post_result", side_effect=WorkspaceError(409, "claim_conflict")):
            self.assertEqual(runner.step(), "failed")
        self.assertFalse(runner.outbox.pending())
        conflict, = runner.outbox.root.glob("*.conflict")
        self.assertEqual(json.loads(conflict.read_text())["status"], "done")

    def test_unacknowledged_auth_error_keeps_envelope(self):
        self.ws.enqueue("extract", {})
        runner = self.runner()
        with patch.object(self.ws, "post_result", side_effect=WorkspaceError(401, "unauthorized")):
            with self.assertRaises(WorkspaceError):
                runner.step()
            with self.assertRaises(WorkspaceError):
                runner.step()
        self.assertEqual(self.handled, 1)
        self.assertEqual(len(runner.outbox.pending()), 1)
        self.assertEqual(runner.step(), "done")

    def test_invalid_acknowledgment_keeps_envelope(self):
        self.ws.enqueue("extract", {})
        runner = self.runner()
        with patch.object(self.ws, "post_result", return_value={"job_id": "job_0000000000000001"}):
            self.assertEqual(runner.step(), "backoff")
        self.assertEqual(len(runner.outbox.pending()), 1)
        self.assertEqual(self.handled, 1)
        self.assertEqual(runner.step(), "done")

    def test_result_persistence_failure_preserves_scratch_and_closes_heartbeat(self):
        self.ws.enqueue("extract", {})
        runner = self.runner()
        with patch.object(runner.outbox, "save", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                runner.step()
        raw, = (self.settings.workdir / "scratch").glob("job-*/original.bin")
        self.assertIn(b"synthetic original", raw.read_bytes())
        self.assertFalse(self.ws.results)

    def test_handler_failure_and_stop_close_heartbeat(self):
        for mode in ("exception", "stop"):
            with self.subTest(mode=mode):
                self.ws = FakeWorkspace()
                self.ws.enqueue("extract", {})
                claims = []
                def handler(ctx):
                    claims.append(ctx.workspace._claim)
                    if mode == "stop":
                        runner.request_stop()
                        ctx.workspace.put_original(b"must not be written")
                    raise RuntimeError("synthetic private record text")
                runner = self.runner(handler=handler)
                self.assertEqual(runner.step(), "failed")
                self.assertTrue(claims[0].closed.is_set())
                claims[0].thread.join(timeout=1)
                self.assertFalse(claims[0].thread.is_alive())
                self.assertFalse(self.ws.originals)
                self.assertFalse(claims[0].renew())
                self.assertNotIn("synthetic private record text", " ".join(self.log.lines))
                if mode == "stop":
                    self.assertFalse(self.ws.results)

    def test_local_delivery_lock_prevents_duplicate_active_senders(self):
        import fcntl
        self.ws.enqueue("extract", {})
        runner = self.runner()
        with patch.object(self.ws, "post_result", side_effect=WorkspaceError(503, retryable=True)):
            runner.step()
        path, = runner.outbox.pending()
        with path.open("r") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.runner().step(), "idle")
        self.assertEqual(self.runner().step(), "done")


if __name__ == "__main__":
    unittest.main()
