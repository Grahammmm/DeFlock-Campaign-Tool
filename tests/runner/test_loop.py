"""Poll loop: lease -> dispatch -> result with the FakeWorkspace; backoff; --once; blocked kinds."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from runner import __main__ as cli
from runner.client import FakeWorkspace, WorkspaceError
from runner.loop import Result, Runner, private_write
from tests.runner.helpers import Silent, context, job, settings, tempdir


def ok_handler(ctx):
    ctx.write("scratch.bin", b"private bytes")
    return Result("done", {"seen": ctx.job["inputs"]})


def boom_handler(ctx):
    raise RuntimeError("handler exploded on 7ABC123")


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempdir()
        self.ws = FakeWorkspace()
        self.log = Silent()
        self.settings = settings(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, handlers):
        sleeps = []
        r = Runner(self.ws, self.settings, handlers=handlers, log=self.log, sleep=sleeps.append)
        return r, sleeps

    def test_lease_dispatch_result_and_followups(self):
        self.ws.enqueue("extract", {"sha256": "a" * 64})
        def with_followup(ctx):
            return Result("done", {"followups": [{"kind": "digest", "inputs": {"sha256": "a" * 64}, "idempotency_key": "f" * 64}]})
        r, _ = self.runner({"extract": with_followup})
        self.assertEqual(r.step(), "done")
        self.assertEqual(self.ws.results[0]["status"], "done")
        kinds = [j["kind"] for j in self.ws.jobs.values()]
        self.assertEqual(kinds, ["extract", "digest"])
        self.assertEqual(self.ws.jobs["job_0000000000000001"]["state"], "done")
        self.assertEqual(r.step(), "blocked")  # digest has no handler in this test registry
        self.assertEqual(r.step(), "idle")

    def test_handler_exception_is_failed_without_record_text(self):
        self.ws.enqueue("extract", {"sha256": "b" * 64})
        r, _ = self.runner({"extract": boom_handler})
        self.assertEqual(r.step(), "failed")
        result = self.ws.results[-1]
        self.assertTrue(result["error"].startswith("RuntimeError"))
        # the job requeues while attempts remain
        self.assertEqual(self.ws.jobs["job_0000000000000001"]["state"], "queued")
        # log lines carry identifiers and counts only, never the workdir contents
        for line in self.log.lines:
            self.assertNotIn("private bytes", line)

    def test_workdir_permissions_and_cleanup(self):
        self.ws.enqueue("extract", {"x": 1})
        r, _ = self.runner({"extract": ok_handler})
        r.step()
        work = Path(self.settings.workdir)
        self.assertEqual(os.stat(work).st_mode & 0o777, 0o700)
        self.assertFalse((work / "job_0000000000000001").exists())
        self.settings.keep_workdir = True
        self.ws.enqueue("extract", {"x": 2})
        r.step()
        kept = work / "job_0000000000000002" / "scratch.bin"
        self.assertEqual(os.stat(kept).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(kept.parent).st_mode & 0o777, 0o700)

    def test_private_write_refuses_symlink(self):
        target = Path(self.tmp.name) / "target"
        target.write_bytes(b"x")
        link = Path(self.tmp.name) / "link"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            private_write(link, b"y")

    def test_backoff_on_5xx_is_exponential_and_capped(self):
        self.ws.fail_next = 3
        r, sleeps = self.runner({})
        outcomes = []
        for _ in range(3):
            outcome = r.step()
            outcomes.append(outcome)
            if outcome == "backoff":
                sleeps.append(r.backoff)
        self.assertEqual(outcomes, ["backoff"] * 3)
        self.assertEqual(sleeps, [1.0, 2.0, 4.0])
        self.assertEqual(r.step(), "idle")
        self.assertEqual(r.backoff, 0.0)
        r.backoff = 250.0
        self.ws.fail_next = 1
        r.step()
        self.assertEqual(r.backoff, 300.0)

    def test_non_retryable_workspace_error_propagates(self):
        class Bad(FakeWorkspace):
            def lease_job(self, lease_seconds=300):
                raise WorkspaceError(401, "bad token")
        r = Runner(Bad(), self.settings, handlers={}, log=self.log, sleep=lambda s: None)
        with self.assertRaises(WorkspaceError):
            r.step()

    def test_send_request_is_blocked_and_unknown_kind_is_blocked(self):
        from runner.loop import default_handlers
        self.ws.enqueue("send_request", {"request_id": "req_x"})
        self.ws.enqueue("intake", {"slot": "2026-09-30T13:00"})
        r, _ = self.runner(default_handlers())
        self.assertEqual(r.step(), "blocked")
        self.assertIn("not a runner job", self.ws.results[-1]["error"])
        self.assertEqual(r.step(), "blocked")
        self.assertIn("no handler", self.ws.results[-1]["error"])
        self.assertEqual(self.ws.jobs["job_0000000000000001"]["state"], "blocked")

    def test_once_exit_codes_and_sigterm(self):
        r, _ = self.runner({"extract": ok_handler})
        self.assertEqual(r.run(once=True), 3)
        self.ws.enqueue("extract", {"n": 1})
        self.assertEqual(r.run(once=True), 0)
        self.ws.enqueue("extract", {"n": 2})
        r2, _ = self.runner({"extract": boom_handler})
        self.assertEqual(r2.run(once=True), 1)
        # continuous mode stops after a SIGTERM-style request
        self.ws.enqueue("extract", {"n": 3})
        calls = []
        r3 = Runner(self.ws, self.settings, handlers={"extract": ok_handler}, log=self.log, sleep=lambda s: (calls.append(s), r3.request_stop()))
        self.assertEqual(r3.run(), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.ws.jobs["job_0000000000000003"]["state"], "done")

    def test_cli_fake_once_and_missing_env(self):
        env = {"RUNNER_WORKDIR": os.path.join(self.tmp.name, "cli"), "POLL_INTERVAL": "0"}
        old = dict(os.environ)
        os.environ.update(env)
        try:
            os.environ.pop("WORKSPACE_URL", None)
            os.environ.pop("RUNNER_TOKEN", None)
            self.assertEqual(cli.main(["--once", "--fake"]), 3)
            with self.assertRaises(SystemExit):
                cli.main(["--once"])
            os.environ["PRIVACY_TIER"] = "strict_local"
            os.environ["MODEL_BASE_URL"] = "https://api.example.invalid/v1"
            with self.assertRaises(SystemExit):
                cli.main(["--once", "--fake"])
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_privacy_tier_defaults_to_redacted_cloud_with_strict_local_as_the_switch(self):
        from runner.loop import Settings
        self.assertEqual(Settings.from_env({}).privacy_tier, "redacted_cloud")
        self.assertEqual(Settings.from_env({"PRIVACY_TIER": "strict_local"}).privacy_tier, "strict_local")
        with self.assertRaisesRegex(ValueError, "PRIVACY_TIER"):
            Settings.from_env({"PRIVACY_TIER": "open_cloud"})

    def test_job_tier_never_loosens_the_operator_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(tmp, job("digest", tier="redacted_cloud"), privacy_tier="strict_local")
            self.assertEqual(ctx.privacy_tier, "strict_local")
            ctx = context(tmp, job("digest", tier="strict_local"), privacy_tier="redacted_cloud")
            self.assertEqual(ctx.privacy_tier, "strict_local")
            ctx = context(tmp, job("digest", tier="redacted_cloud"), privacy_tier="redacted_cloud")
            self.assertEqual(ctx.privacy_tier, "redacted_cloud")

    def test_structured_log_is_json(self):
        self.ws.enqueue("extract", {})
        r, _ = self.runner({"extract": ok_handler})
        r.step()
        events = [json.loads(line)["event"] for line in self.log.lines]
        self.assertEqual(events, ["job.start", "job.finish"])


if __name__ == "__main__":
    unittest.main()
