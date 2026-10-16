"""Synthetic private journal + injected Docker; no systemd or provider activation."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import threading
import subprocess
import sys
from unittest import mock
import unittest

from campaign_tool.records import container_host as host
from campaign_tool.records import systemd_control as control
from tests.records import test_container_host as fixtures


class SystemdControlTests(fixtures.HostFixture):
    invocation = "1" * 32

    @contextmanager
    def journal(self):
        with host.Profile(self.profile) as profile:
            journal = host.Journal(profile)
            try:
                yield journal
            finally:
                journal.close()

    def prepare(self, invocation=None):
        with self.journal() as journal:
            return control._prepare(journal, invocation or self.invocation)

    def bound_run(self, fake, invocation=None):
        return control.run(self.profile, invocation or self.invocation,
                           invoke=fake.invoke, containment=fake.containment)

    def stop_post(self, fake, invocation=None):
        return control.stop_post(self.profile, invocation or self.invocation,
                                 invoke=fake.invoke, containment=fake.containment)

    def held_run(self):
        fake = self.fake(launch=host.TransportError())
        self.assertEqual(self.bound_run(fake)["status"], "held")
        return fake

    def deny_new_writer(self, fake):
        before = len(fake.calls)
        with self.assertRaises(host.Rejected):
            host.run(self.profile, invoke=fake.invoke, containment=fake.containment)
        self.assertEqual(len(fake.calls), before)

    def test_binding_and_job_receipt_durable_before_any_docker_call(self):
        fake = self.fake()
        def invoke(argv, timeout):
            with self.journal() as journal:
                raw, value = control._owned(journal, self.invocation)
                self.assertEqual(control._binding(journal)[0], raw)
                self.assertEqual(journal.active()["job_id"], value["job_id"])
                self.assertEqual(journal.read(value["job_id"])["profile_sha256"], value["profile_sha256"])
                for name in ("invocation-" + self.invocation + ".json",
                             "reservation-" + value["job_id"] + ".json", control.BINDING):
                    self.assertEqual((self.receipts / name).stat().st_mode & 0o777, 0o600)
            return fake.invoke(argv, timeout)
        result = control.run(self.profile, self.invocation, invoke=invoke, containment=fake.containment)
        self.assertTrue(result["healthy"] and result["quiescent"])

    def test_natural_success_stop_post_noop_preserves_exact_receipt(self):
        fake = self.fake()
        self.assertEqual(self.bound_run(fake)["status"], "completed")
        before = (self.receipts / self.current()["job_id"] / "receipt.json").read_bytes()
        calls = len(fake.calls)
        for _ in range(3):
            self.assertEqual(self.stop_post(fake), host._output("completed", quiescent=True, healthy=True, cards=5))
        self.assertEqual(len(fake.calls), calls)
        self.assertEqual((self.receipts / self.current()["job_id"] / "receipt.json").read_bytes(), before)

    def test_natural_postrun_failure_noop_cannot_turn_healthy(self):
        fake = self.fake(report=host._output("postrun_failed", quiescent=True, cards=5))
        self.assertEqual(self.bound_run(fake)["status"], "postrun_failed")
        before = self.current()
        calls = len(fake.calls)
        result = self.stop_post(fake)
        self.assertEqual(result["status"], "postrun_failed")
        self.assertTrue(result["quiescent"])
        self.assertFalse(result["healthy"])
        self.assertEqual(self.current(), before)
        self.assertEqual(len(fake.calls), calls)

    def test_worker_failure_preserved_through_cleanup_and_repeated_stop(self):
        fake = self.fake(launch=(10, fixtures.worker("worker_failed")))
        self.assertEqual(self.bound_run(fake)["status"], "held")
        result = self.stop_post(fake)
        self.assertEqual(result["status"], "postrun_failed")
        self.assertFalse(result["healthy"])
        self.assertEqual((self.current()["worker_status"], self.current()["worker_exit"]), ("worker_failed", -15))
        before, calls = self.current(), len(fake.calls)
        self.assertEqual(self.stop_post(fake), result)
        self.assertEqual(self.current(), before)
        self.assertEqual(len(fake.calls), calls)

    def test_no_allocation_nonzero_no_ack_idempotent_and_late_run_fenced(self):
        job_id = self.prepare()
        fake = self.fake()
        for _ in range(2):
            self.assertEqual(self.stop_post(fake), host._output("not_allocated"))
        self.assertEqual(fake.calls, [])
        self.assertFalse((self.receipts / job_id).exists())
        with self.assertRaises(host.Rejected):
            host.run(self.profile, job_id=job_id, invoke=fake.invoke, containment=fake.containment)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.bound_run(fake, "2" * 32)["status"], "completed")
        with self.assertRaises(host.Rejected):
            host.run(self.profile, job_id=job_id, invoke=fake.invoke, containment=fake.containment)

    def test_no_mapping_does_not_adopt_or_cancel_other_active_job(self):
        fake = self.launch_hold()
        before, calls = self.current(), len(fake.calls)
        self.assertEqual(self.stop_post(fake)["status"], "held")
        self.assertEqual(self.current(), before)
        self.assertEqual(len(fake.calls), calls)
        self.deny_new_writer(fake)

    def test_unknown_invocation_after_success_is_hold_not_old_success(self):
        fake = self.fake()
        self.bound_run(fake)
        before, calls = self.current(), len(fake.calls)
        self.assertEqual(self.stop_post(fake, "2" * 32)["status"], "held")
        self.assertEqual(self.current(), before)
        self.assertEqual(len(fake.calls), calls)
        self.deny_new_writer(fake)

    def test_partial_binding_crash_boundaries_deny_admission(self):
        for name in ("systemd-managed.json", control.BINDING,
                     "invocation-" + self.invocation + ".json"):
            with self.subTest(name=name):
                private = self.root / ("boundary-" + str(len(list(self.root.iterdir()))))
                private.mkdir(mode=0o700)
                value = json.loads(self.profile.read_text())
                value["host_receipts"] = str(private)
                self.write_profile(value)
                self.receipts = private
                original = host.PrivateDir.write
                def write(directory, target, raw, **options):
                    original(directory, target, raw, **options)
                    if target == name:
                        raise SystemExit()
                with self.journal() as journal, mock.patch.object(host.PrivateDir, "write", write):
                    with self.assertRaises(SystemExit):
                        control._prepare(journal, self.invocation)
                fake = self.fake()
                self.assertEqual(self.stop_post(fake)["status"], "held")
                self.assertEqual(fake.calls, [])
                self.deny_new_writer(fake)

    def test_controller_loss_before_job_receipt_is_uncertain_not_no_allocation(self):
        job_id = self.prepare()
        (self.receipts / job_id).mkdir(mode=0o700)
        fake = self.fake()
        self.assertEqual(self.stop_post(fake)["status"], "held")
        self.assertEqual(fake.calls, [])
        self.deny_new_writer(fake)

    def test_controller_loss_after_receipt_before_active_pointer_is_hold(self):
        job_id = self.prepare()
        original = host.PrivateDir.write
        def write(directory, name, raw, **options):
            if name == "active.json":
                raise SystemExit()
            return original(directory, name, raw, **options)
        with self.journal() as journal, mock.patch.object(host.PrivateDir, "write", write):
            with self.assertRaises(SystemExit):
                journal.allocate(job_id)
        fake = self.fake()
        self.assertEqual(self.stop_post(fake)["status"], "held")
        self.assertEqual(fake.calls, [])
        self.assertTrue((self.receipts / job_id / "receipt.json").exists())
        self.deny_new_writer(fake)

    def test_controller_loss_after_active_before_docker_remains_hold(self):
        job_id = self.prepare()
        with self.journal() as journal:
            self.assertEqual(journal.allocate(job_id), job_id)
        fake = self.fake()
        result = self.stop_post(fake)
        self.assertEqual(result["status"], "held")
        self.assertFalse(result["quiescent"])
        self.assertTrue(self.current()["cancel_requested"])
        self.assertEqual(fake.calls, [])
        self.deny_new_writer(fake)

    def test_running_controller_loss_independent_exact_container_cleanup(self):
        fake = self.held_run()
        cid = self.current()["container_id"]
        self.assertTrue(fake.containers[cid]["running"])
        result = self.stop_post(fake)
        self.assertEqual(result["status"], "postrun_failed")
        self.assertTrue(result["quiescent"])
        self.assertFalse(result["healthy"])
        self.assertEqual(fake.containers, {})
        self.assertTrue(self.current()["worker_removed"] and self.current()["reporter_removed"])

    def test_already_finished_cancelled_idempotent_and_outcome_preserved(self):
        fake = self.held_run()
        result = self.stop_post(fake)
        self.assertEqual(self.current()["ack"], "contained")
        self.assertTrue(self.current()["cancel_requested"])
        before, calls = self.current(), len(fake.calls)
        self.assertEqual(self.stop_post(fake), result)
        self.assertFalse(result["healthy"])
        self.assertEqual(self.current(), before)
        self.assertEqual(len(fake.calls), calls)

    def test_older_finalized_success_never_cancels_subsequent_writer(self):
        fake = self.fake()
        self.bound_run(fake)
        fake.launch = host.TransportError()
        self.assertEqual(self.bound_run(fake, "2" * 32)["status"], "held")
        before, calls = self.current(), len(fake.calls)
        pending = (self.receipts / control.OPERATION).read_bytes()
        self.assertEqual(self.stop_post(fake)["status"], "completed")
        self.assertEqual((self.receipts / control.OPERATION).read_bytes(), pending)
        self.assertEqual(self.current(), before)
        self.assertEqual(len(fake.calls), calls)
        self.assertTrue(next(iter(fake.containers.values()))["running"])

    def test_mutated_intent_or_reservation_is_hold_no_docker(self):
        for prefix in ("invocation-", "reservation-"):
            with self.subTest(prefix=prefix):
                private = self.root / prefix
                private.mkdir(mode=0o700)
                value = json.loads(self.profile.read_text())
                value["host_receipts"] = str(private)
                self.write_profile(value)
                self.receipts = private
                fake = self.held_run()
                job = self.current()["job_id"]
                name = prefix + (self.invocation if prefix == "invocation-" else job) + ".json"
                (self.receipts / name).write_bytes(b'{"private":"NOT_AUTHORITY"}')
                before, calls = self.current(), len(fake.calls)
                self.assertEqual(self.stop_post(fake)["status"], "held")
                self.assertEqual(self.current(), before)
                self.assertEqual(len(fake.calls), calls)
                self.deny_new_writer(fake)

    def test_deleted_active_binding_managed_marker_fails_closed(self):
        fake = self.held_run()
        (self.receipts / control.BINDING).unlink()
        calls = len(fake.calls)
        self.assertEqual(self.stop_post(fake)["status"], "held")
        self.assertEqual(len(fake.calls), calls)
        self.deny_new_writer(fake)

    def test_profile_changed_no_other_job_cancelled(self):
        fake = self.held_run()
        value = json.loads(self.profile.read_text())
        value["wall_seconds"] = 2000
        self.write_profile(value)
        before, calls = self.current(), len(fake.calls)
        self.assertEqual(self.stop_post(fake)["status"], "held")
        self.assertEqual(self.current(), before)
        self.assertEqual(len(fake.calls), calls)
        self.deny_new_writer(fake)

    def install_other_finished_pointer(self):
        old = self.current()
        other_id = "f" * 32
        other = dict(old, job_id=other_id, container_id=None, phase="finished", hold=False,
                     quiescent=True, healthy=False, worker_removed=True, reporter_id=None,
                     reporter_removed=False, ack="contained", cancel_requested=False)
        with self.journal() as journal:
            with journal.directory.child(other_id, fresh=True) as directory:
                directory.write("receipt.json", host.encoded(host._receipt(other)), fresh=True)
            journal.directory.write("active.json", host.encoded(
                {"schema": 1, "job_id": other_id, "profile_sha256": journal.profile.sha256}))
        return other_id

    def test_expected_guard_inside_stop_lock_rejects_pointer_race(self):
        fake = self.held_run()
        original = host.locked
        calls, switched = len(fake.calls), []
        @contextmanager
        def locked(directory, name, **options):
            with original(directory, name, **options):
                if name == "stop.lock" and not switched:
                    switched.append(self.install_other_finished_pointer())
                yield
        with mock.patch.object(host, "locked", locked):
            self.assertEqual(self.stop_post(fake)["status"], "held")
        self.assertEqual(len(fake.calls), calls)
        self.assertFalse(self.current()["cancel_requested"])
        self.deny_new_writer(fake)

    def test_expected_guard_rechecked_after_admission_lock_pointer_race(self):
        fake = self.held_run()
        original = host.locked
        calls, switched = len(fake.calls), []
        @contextmanager
        def locked(directory, name, **options):
            with original(directory, name, **options):
                if name == "admission.lock" and not switched:
                    switched.append(self.install_other_finished_pointer())
                yield
        with mock.patch.object(host, "locked", locked):
            self.assertEqual(self.stop_post(fake)["status"], "held")
        self.assertEqual(len(fake.calls), calls)
        self.assertFalse(self.current()["cancel_requested"])
        self.deny_new_writer(fake)

    def test_stop_waits_for_allocate_journal_commit_real_flock(self):
        job_id = self.prepare()
        original = host.PrivateDir.write
        attempted, finished = threading.Event(), threading.Event()
        results, errors, threads = [], [], []
        fake = self.fake()
        def stop():
            attempted.set()
            try:
                results.append(self.stop_post(fake))
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()
        def write(directory, name, raw, **options):
            result = original(directory, name, raw, **options)
            if name == "receipt.json" and not threads:
                thread = threading.Thread(target=stop)
                threads.append(thread)
                thread.start()
                self.assertTrue(attempted.wait(2))
                self.assertFalse(finished.wait(.03))
            return result
        with self.journal() as journal, mock.patch.object(host.PrivateDir, "write", write):
            journal.allocate(job_id)
        for thread in threads:
            thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["status"], "held")
        self.assertTrue(self.current()["cancel_requested"])
        self.assertEqual(fake.calls, [])

    def test_cleanup_failure_then_retry_stays_nonhealthy(self):
        fake = self.held_run()
        fake.remove_failure = True
        self.assertEqual(self.stop_post(fake)["status"], "held")
        self.assertTrue(self.current()["hold"])
        self.deny_new_writer(fake)
        fake.remove_failure = False
        result = self.stop_post(fake)
        self.assertEqual(result["status"], "postrun_failed")
        self.assertTrue(result["quiescent"])
        self.assertFalse(result["healthy"])
        self.assertEqual(self.stop_post(fake), result)

    def test_unknown_docker_no_ack_keeps_hold_and_denies_writer(self):
        fake = self.held_run()
        fake.daemon_down = True
        result = self.stop_post(fake)
        self.assertEqual(result["status"], "held")
        self.assertFalse(result["quiescent"] or result["healthy"])
        self.assertTrue(self.current()["hold"])
        self.deny_new_writer(fake)

    def test_bound_invocation_cannot_be_reused_to_launch_again(self):
        fake = self.fake()
        self.bound_run(fake)
        calls = len(fake.calls)
        self.assertEqual(self.bound_run(fake)["status"], "held")
        self.assertEqual(len(fake.calls), calls)
        self.deny_new_writer(fake)

    def test_invalid_invocation_id_and_cli_output_do_not_leak_arguments(self):
        fake = self.fake()
        for value in ("A" * 32, "1" * 31, "../PRIVATE_ARGUMENT", "", None, 7):
            with self.assertRaises(host.Rejected):
                control.run(self.profile, value, invoke=fake.invoke)
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = control.main(["stop-post", "--profile", "PRIVATE_ARGUMENT",
                                 "--invocation-id", "../PRIVATE_ARGUMENT"])
        self.assertEqual(code, 30)
        self.assertNotIn("PRIVATE_ARGUMENT", output.getvalue())
        self.assertEqual(set(json.loads(output.getvalue())),
                         {"schema", "status", "quiescent", "healthy", "cards"})
        self.assertEqual(fake.calls, [])

    def test_clean_env_no_paid_forwarding_or_untrusted_callbacks(self):
        fake = self.fake()
        # Deliberately synthetic input, not a credential or scanner exemption.
        synthetic_value = "fixture-only-not-a-credential"
        with mock.patch.dict(os.environ, {"MODEL_API_KEY": synthetic_value,
                                         "MODEL_BASE_URL": "https://synthetic.invalid",
                                         "CHALLENGE_MODEL_BASE_URL": "https://synthetic.invalid"}):
            self.assertEqual(os.environ["MODEL_API_KEY"], synthetic_value)
            self.bound_run(fake)
        for argv, _ in fake.calls:
            self.assertNotIn(synthetic_value, repr(argv))
            if argv[1] in {"create", "exec"}:
                self.assertIn("-i", argv)
                self.assertIn("MODEL_BASE_URL=", argv)
                self.assertIn("CHALLENGE_MODEL_BASE_URL=", argv)
                self.assertIn("PRIVACY_TIER=strict_local", argv)
        value = json.loads(self.profile.read_text())
        value["callback"] = "PRIVATE_ARGUMENT"
        self.write_profile(value)
        with self.assertRaises(host.Rejected):
            control.stop_post(self.profile, self.invocation, invoke=fake.invoke)

    def test_cli_not_allocated_has_distinct_nonzero_status(self):
        self.prepare()
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = control.main(["stop-post", "--profile", str(self.profile),
                                 "--invocation-id", self.invocation])
        self.assertEqual(code, 11)
        self.assertEqual(json.loads(output.getvalue()), host._output("not_allocated"))

    def test_host_optional_expected_id_rejects_invalid_before_docker(self):
        fake = self.held_run()
        calls = len(fake.calls)
        with self.assertRaises(host.Rejected):
            host.stop(self.profile, expected_job_id="b" * 32, invoke=fake.invoke)
        self.assertEqual(len(fake.calls), calls)
        self.assertFalse(self.current()["cancel_requested"])


    @contextmanager
    def failed_hold_write(self):
        original = host.PrivateDir.write
        def write(directory, name, raw, **options):
            if name == "systemd-hold.json":
                raise OSError("synthetic persistence fault")
            return original(directory, name, raw, **options)
        with mock.patch.object(host.PrivateDir, "write", write):
            yield

    def assert_fresh_process_denied(self):
        before = (self.receipts / "active.json").read_bytes()
        jobs = sorted(p.name for p in self.receipts.iterdir() if p.is_dir())
        child = """
import json, sys
from pathlib import Path
from campaign_tool.records import container_host as host, systemd_control as control
from tests.records.test_container_host import FakeDocker
profile = Path(sys.argv[1])
fake = FakeDocker(profile)
try:
    host.run(profile, invoke=fake.invoke, containment=fake.containment)
    direct = "unexpected"
except host.Rejected:
    direct = "rejected"
result = control.run(profile, "3" * 32, invoke=fake.invoke, containment=fake.containment)
print(json.dumps({"direct": direct, "bound": result["status"], "calls": len(fake.calls)}))
"""
        env = {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "TMPDIR": str(self.root), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": os.environ.get("PYTHONPATH", "")}
        result = subprocess.run([sys.executable, "-B", "-c", child, str(self.profile)],
                                capture_output=True, timeout=10, env=env)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout),
                         {"direct": "rejected", "bound": "held", "calls": 0})
        self.assertEqual((self.receipts / "active.json").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.receipts.iterdir() if p.is_dir()), jobs)

    def test_failed_hold_persistence_denies_direct_and_fresh_managed_writer(self):
        fake = self.fake()
        self.assertEqual(self.bound_run(fake)["status"], "completed")
        before, calls = self.current(), len(fake.calls)
        with self.failed_hold_write():
            self.assertEqual(self.stop_post(fake, "2" * 32)["status"], "held")
        self.assertFalse((self.receipts / "systemd-hold.json").exists())
        self.assertTrue((self.receipts / control.OPERATION).exists())
        self.deny_new_writer(fake)
        self.assert_fresh_process_denied()
        self.assertEqual(self.current(), before)
        self.assertEqual(len(fake.calls), calls)

    def test_managed_unreserved_run_never_inherits_terminal_permission(self):
        fake = self.fake()
        self.assertEqual(self.bound_run(fake)["status"], "completed")
        self.assertFalse((self.receipts / control.OPERATION).exists())
        self.deny_new_writer(fake)
        # Rejecting the bypass does not disable legitimate next invocations.
        self.assertEqual(self.bound_run(fake, "2" * 32)["status"], "completed")
        self.assertFalse((self.receipts / control.OPERATION).exists())

    def test_corrupt_binding_and_failed_hold_remain_denied_after_restart(self):
        fake = self.fake()
        self.bound_run(fake)
        (self.receipts / control.BINDING).write_bytes(b"{}")
        with self.failed_hold_write():
            self.assertEqual(self.stop_post(fake)["status"], "held")
        self.assertFalse((self.receipts / "systemd-hold.json").exists())
        self.deny_new_writer(fake)
        self.assert_fresh_process_denied()

    def test_operation_corruption_is_not_ignored_after_restart(self):
        fake = self.fake()
        self.bound_run(fake)
        (self.receipts / control.OPERATION).write_bytes(b"{}")
        with self.failed_hold_write():
            self.assertEqual(self.stop_post(fake, "2" * 32)["status"], "held")
        self.assertFalse((self.receipts / "systemd-hold.json").exists())
        self.deny_new_writer(fake)
        self.assert_fresh_process_denied()

    def test_controller_loss_after_intent_before_validation_blocks_restart(self):
        fake = self.fake()
        self.bound_run(fake)
        original = host.PrivateDir.write
        def write(directory, name, raw, **options):
            if name == "systemd-hold.json":
                raise OSError("synthetic persistence fault")
            result = original(directory, name, raw, **options)
            if name == control.OPERATION:
                raise SystemExit()
            return result
        with mock.patch.object(host.PrivateDir, "write", write):
            self.assertEqual(self.stop_post(fake, "2" * 32)["status"], "held")
        self.assertFalse((self.receipts / "systemd-hold.json").exists())
        self.assert_fresh_process_denied()

    def test_intent_initial_write_failure_has_independent_fallback(self):
        fake = self.fake()
        self.bound_run(fake)
        original, attempted = host.PrivateDir.write, []
        def write(directory, name, raw, **options):
            if name == "systemd-hold.json":
                raise OSError("synthetic persistence fault")
            if name == control.OPERATION and not attempted:
                attempted.append(True)
                raise OSError("synthetic first write fault")
            return original(directory, name, raw, **options)
        with mock.patch.object(host.PrivateDir, "write", write):
            self.assertEqual(self.stop_post(fake, "2" * 32)["status"], "held")
        self.assertEqual(json.loads((self.receipts / control.OPERATION).read_bytes())["purpose"], "hold")
        self.assertFalse((self.receipts / "systemd-hold.json").exists())
        self.assert_fresh_process_denied()

    def test_deleted_operation_cannot_admit_a_reserved_job(self):
        job = self.prepare()
        (self.receipts / control.OPERATION).unlink()
        fake = self.fake()
        with self.assertRaises(host.Rejected):
            host.run(self.profile, job_id=job, invoke=fake.invoke, containment=fake.containment)
        self.assertEqual(fake.calls, [])
        self.assertFalse((self.receipts / job).exists())

    def test_matching_cleanup_retry_resolves_only_its_operation(self):
        fake = self.held_run()
        fake.remove_failure = True
        self.assertEqual(self.stop_post(fake)["status"], "held")
        pending = json.loads((self.receipts / control.OPERATION).read_bytes())
        self.assertEqual((pending["purpose"], pending["invocation_id"]), ("stop", self.invocation))
        self.deny_new_writer(fake)
        fake.remove_failure = False
        self.assertEqual(self.stop_post(fake)["status"], "postrun_failed")
        self.assertFalse((self.receipts / control.OPERATION).exists())
        fake.launch = (0, fixtures.worker())
        self.assertEqual(self.bound_run(fake, "2" * 32)["status"], "completed")



    def test_no_allocation_seal_contains_only_persistent_binding_fields(self):
        self.prepare()
        fake = self.fake()
        self.assertEqual(self.stop_post(fake), host._output("not_allocated"))
        raw = (self.receipts / ("closed-" + self.invocation + ".json")).read_bytes()
        with self.journal() as journal:
            _, value = control._owned(journal, self.invocation)
            self.assertEqual(raw, control._closure(value))
        self.assertEqual(set(json.loads(raw)),
                         {"schema", "invocation_id", "job_id", "profile_sha256", "status"})
        self.assertEqual(fake.calls, [])
        self.assertFalse((self.receipts / control.OPERATION).exists())
        self.assertEqual(self.stop_post(fake), host._output("not_allocated"))


if __name__ == "__main__":
    unittest.main()
