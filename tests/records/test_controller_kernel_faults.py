"""Linux kernel-boundary acceptance; synthetic journals and injected Docker only."""
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import unittest
from unittest import mock

from campaign_tool.records import container_host as host
from campaign_tool.records import systemd_control as control
from tests.records import test_container_host as fixtures


CHILD_EXIT = 73
INVOCATION = "1" * 32
NEXT = "2" * 32


def _child(profile_path, action, invocation, job_id):
    """Trusted test driver, never selected by record content or profile callbacks."""
    profile_path = Path(profile_path)
    fake = fixtures.FakeDocker(profile_path)
    original_write, original_sync = host.PrivateDir.write, os.fsync
    original_flock = host.fcntl.flock
    context = [None]
    faults = []
    blocked = []
    crash = action in {"crash-run", "crash-stop"}
    barrier = action == "stop-barrier"

    def write(directory, name, raw, **options):
        context[0] = name
        try:
            result = original_write(directory, name, raw, **options)
            if name == control.OPERATION and crash:
                # Original write returned after file AND directory fsync.
                os._exit(CHILD_EXIT)
            if name == control.OPERATION and barrier:
                print("intent-durable", flush=True)
                if sys.stdin.readline() != "release\n":
                    os._exit(74)
            return result
        finally:
            context[0] = None

    def sync(fd):
        name = context[0]
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        fail = (
            action == "hold-file" and name == "systemd-hold.json" and not directory
            or action == "hold-directory" and name == "systemd-hold.json" and directory
            or action == "initial-once" and name == control.OPERATION and not directory and not faults
            or action == "initial-directory" and name == control.OPERATION and directory
            or action == "initial-all" and name in {control.OPERATION, "systemd-hold.json"} and not directory
        )
        if fail:
            faults.append({"name": name, "directory": directory})
            raise OSError(errno.EIO, "synthetic fsync fault")
        return original_sync(fd)

    def flock(fd, operation):
        try:
            return original_flock(fd, operation)
        except BlockingIOError:
            if not blocked:
                blocked.append(True)
                print("lock-blocked", flush=True)
            raise

    def denied():
        try:
            host.run(profile_path, invoke=fake.invoke, containment=fake.containment)
        except host.Rejected:
            direct = "rejected"
        else:
            direct = "unexpected"
        result = control.run(profile_path, "3" * 32, invoke=fake.invoke,
                             containment=fake.containment)
        return {"direct": direct, "bound": result["status"]}

    with mock.patch.object(host.PrivateDir, "write", write), mock.patch.object(os, "fsync", sync), \
            mock.patch.object(host.fcntl, "flock", flock):
        if action == "deny":
            result = denied()
        elif action == "allocate":
            with host.Profile(profile_path) as profile:
                journal = host.Journal(profile)
                try:
                    try:
                        journal.allocate(job_id)
                        result = {"status": "allocated"}
                    except host.Rejected:
                        result = {"status": "rejected"}
                finally:
                    journal.close()
        elif action == "crash-run":
            result = control.run(profile_path, invocation, invoke=fake.invoke,
                                 containment=fake.containment)
        else:
            result = control.stop_post(profile_path, invocation, invoke=fake.invoke,
                                       containment=fake.containment)
    print(json.dumps({"result": result, "calls": len(fake.calls), "faults": faults,
                      "model_environment": any(name.startswith(("MODEL_", "CHALLENGE_MODEL_"))
                                               for name in os.environ)}, sort_keys=True), flush=True)


@unittest.skipUnless(sys.platform == "linux", "Linux process/flock acceptance")
class ControllerKernelFaultTests(fixtures.HostFixture):
    @contextmanager
    def journal(self):
        with host.Profile(self.profile) as profile:
            journal = host.Journal(profile)
            try:
                yield journal
            finally:
                journal.close()

    def completed(self):
        fake = self.fake()
        result = control.run(self.profile, INVOCATION, invoke=fake.invoke,
                             containment=fake.containment)
        self.assertEqual(result["status"], "completed")
        self.assertFalse((self.receipts / control.OPERATION).exists())
        return (self.receipts / "active.json").read_bytes(), self.current()

    def prepare(self):
        with self.journal() as journal:
            return control._prepare(journal, INVOCATION)

    @staticmethod
    def cleanup_child(child):
        # Popen owns this unreaped child; never signal a journal-supplied PID.
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()

    def spawn(self, action, invocation=NEXT, job_id=""):
        env = {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "TMPDIR": str(self.root), "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
               "PYTHONDONTWRITEBYTECODE": "1"}
        driver = ("from tests.records.test_controller_kernel_faults import _child; "
                  "import sys; _child(*sys.argv[1:])")
        child = subprocess.Popen([sys.executable, "-B", "-c", driver, str(self.profile),
                                  action, invocation, job_id], env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Register BEFORE any handshake, proc observation, wait, or assertion.
        self.addCleanup(self.cleanup_child, child)
        return child

    def finish(self, child, expected=0):
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, expected, stderr.decode(errors="replace"))
        if expected:
            self.assertEqual(stdout, b"")
            self.assertEqual(stderr, b"")
            return None
        self.assertEqual(stderr, b"")
        result = json.loads(stdout)
        self.assertFalse(result["model_environment"])
        self.assertEqual(result["calls"], 0)
        return result

    def line(self, child, expected):
        ready, _, _ = select.select([child.stdout], [], [], 5)
        self.assertTrue(ready, "bounded child handshake timed out")
        self.assertEqual(child.stdout.readline(), expected.encode() + b"\n")

    def unchanged(self, before):
        self.assertEqual((self.receipts / "active.json").read_bytes(), before[0])
        self.assertEqual(self.current(), before[1])
        jobs = [path.name for path in self.receipts.iterdir() if path.is_dir()]
        self.assertEqual(jobs, [before[1]["job_id"]])

    def restarted_denial(self, before=None):
        proof = self.finish(self.spawn("deny"))
        self.assertEqual(proof["result"], {"direct": "rejected", "bound": "held"})
        if before is not None:
            self.unchanged(before)

    def test_actual_exit_after_durable_run_intent_denies_restart(self):
        before = self.completed()
        self.finish(self.spawn("crash-run"), CHILD_EXIT)
        intent = json.loads((self.receipts / control.OPERATION).read_bytes())
        self.assertEqual((intent["purpose"], intent["invocation_id"]), ("run", NEXT))
        self.assertFalse((self.receipts / "systemd-hold.json").exists())
        self.unchanged(before)
        self.restarted_denial(before)

    def test_actual_exit_after_durable_stop_intent_denies_restart(self):
        before = self.completed()
        self.finish(self.spawn("crash-stop"), CHILD_EXIT)
        intent = json.loads((self.receipts / control.OPERATION).read_bytes())
        self.assertEqual((intent["purpose"], intent["invocation_id"]), ("stop", NEXT))
        self.assertFalse((self.receipts / "systemd-hold.json").exists())
        self.restarted_denial(before)

    def test_secondary_hold_file_fsync_failure_preserves_intent(self):
        before = self.completed()
        proof = self.finish(self.spawn("hold-file"))
        self.assertEqual(proof["result"]["status"], "held")
        self.assertEqual(proof["faults"], [{"name": "systemd-hold.json", "directory": False}])
        self.assertFalse((self.receipts / "systemd-hold.json").exists())
        intent = (self.receipts / control.OPERATION).read_bytes()
        self.restarted_denial(before)
        self.assertEqual((self.receipts / control.OPERATION).read_bytes(), intent)

    def test_secondary_hold_directory_fsync_failure_uses_prior_durable_intent(self):
        before = self.completed()
        proof = self.finish(self.spawn("hold-directory"))
        self.assertEqual(proof["result"]["status"], "held")
        self.assertEqual(proof["faults"], [{"name": "systemd-hold.json", "directory": True}])
        # The name is visible, but its failed fsync is NOT a durability proof.
        self.assertTrue((self.receipts / "systemd-hold.json").exists())
        intent = (self.receipts / control.OPERATION).read_bytes()
        self.restarted_denial(before)
        self.assertEqual((self.receipts / control.OPERATION).read_bytes(), intent)

    def test_initial_file_fsync_failure_successful_independent_fallback(self):
        before = self.completed()
        proof = self.finish(self.spawn("initial-once"))
        self.assertEqual(proof["result"]["status"], "held")
        self.assertEqual(proof["faults"], [{"name": control.OPERATION, "directory": False}])
        self.assertEqual(json.loads((self.receipts / control.OPERATION).read_bytes())["purpose"], "hold")
        self.assertTrue((self.receipts / "systemd-hold.json").exists())
        self.restarted_denial(before)

    def test_initial_directory_fsync_failure_does_not_launch_or_certify_name(self):
        before = self.completed()
        proof = self.finish(self.spawn("initial-directory"))
        self.assertEqual(proof["result"]["status"], "held")
        self.assertEqual(proof["faults"], [{"name": control.OPERATION, "directory": True}])
        # Only the independent successful HOLD write supplies the fence here.
        self.assertTrue((self.receipts / "systemd-hold.json").exists())
        self.restarted_denial(before)

    def test_total_initial_persistence_failure_has_no_durable_fence_claim(self):
        before = self.completed()
        proof = self.finish(self.spawn("initial-all"))
        self.assertEqual(proof["result"]["status"], "held")
        self.assertEqual([fault["name"] for fault in proof["faults"]],
                         [control.OPERATION, control.OPERATION])
        self.assertFalse((self.receipts / control.OPERATION).exists())
        self.assertFalse((self.receipts / "systemd-hold.json").exists())
        self.unchanged(before)
        # Do NOT assert durable managed restart denial when every persist failed.

    def test_real_stop_waits_for_cancel_admit_flock_then_seals_no_allocation(self):
        job_id = self.prepare()
        with self.journal() as journal:
            with host.locked(journal.directory, "cancel-admit.lock"):
                child = self.spawn("stop", INVOCATION)
                self.line(child, "lock-blocked")
                self.assertIsNone(child.poll())
                self.assertEqual(control._operation(journal)["purpose"], "run")
                self.assertFalse((self.receipts / ("closed-" + INVOCATION + ".json")).exists())
        proof = self.finish(child)
        self.assertEqual(proof["result"]["status"], "not_allocated")
        self.assertFalse((self.receipts / job_id).exists())
        with self.journal() as journal:
            _, value = control._owned(journal, INVOCATION)
            self.assertTrue(control._closed(journal, value))

    def test_real_stop_intent_serializes_late_allocation_and_restart(self):
        job_id = self.prepare()
        stopper = self.spawn("stop-barrier", INVOCATION)
        self.line(stopper, "intent-durable")
        allocator = self.spawn("allocate", INVOCATION, job_id)
        self.line(allocator, "lock-blocked")
        self.assertIsNone(allocator.poll())
        self.assertFalse((self.receipts / job_id).exists())
        stopper.stdin.write(b"release\n")
        stopper.stdin.flush()
        self.assertEqual(self.finish(stopper)["result"]["status"], "not_allocated")
        self.assertEqual(self.finish(allocator)["result"]["status"], "rejected")
        self.assertFalse((self.receipts / job_id).exists())
        proof = self.finish(self.spawn("allocate", INVOCATION, job_id))
        self.assertEqual(proof["result"]["status"], "rejected")

    def test_corrupt_binding_after_actual_crash_remains_denied(self):
        before = self.completed()
        self.finish(self.spawn("crash-stop"), CHILD_EXIT)
        (self.receipts / control.BINDING).write_bytes(b"{}")
        self.restarted_denial(before)


if __name__ == "__main__":
    unittest.main()
