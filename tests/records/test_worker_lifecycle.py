"""Bounded Linux process tests; synthetic workers only, no provider calls."""
import json
import os
import select
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from campaign_tool.records import worker_lifecycle as lifecycle

MODULE = "campaign_tool.records.worker_lifecycle"
READY_WORKER = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(30)"
IGNORE_WORKER = ("import pathlib,sys,time,signal; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                 "pathlib.Path(sys.argv[1]).touch(); time.sleep(30)")


CHILD_PROBE = """
import json

def owned_child_probe():
    marker = pathlib.Path(sys.argv[1])
    fields = pathlib.Path('/proc/self/stat').read_bytes().rsplit(b')', 1)[1].split()
    identity = {'pid': os.getpid(), 'start': int(fields[19]), 'uid': os.getuid()}
    heartbeat = marker.with_suffix('.heartbeat')
    heartbeat.write_text('0')
    temporary = marker.with_suffix('.identity-pending')
    temporary.write_text(json.dumps(identity))
    temporary.replace(marker)
    count = 1
    while True:
        heartbeat.write_text(str(count))
        count += 1
        time.sleep(.01)
"""


@unittest.skipUnless(sys.platform == "linux", "Linux ownership primitives required")
class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.getuid(), 0, "Synthetic lifecycle tests require unprivileged UID")
        self.temp = tempfile.TemporaryDirectory(prefix="worker-lifecycle-")
        self.root = Path(self.temp.name)
        self.state = self.root / "lifecycle"
        self.marker = self.root / "ready"
        self.running = []
        self.envelopes = []

    def tearDown(self):
        # Cleanup is restricted to the synthetic subprocess handles this test created.
        for process in self.running:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
        self.temp.cleanup()

    def start(self, code, *, wall=1, owned=256, extra=()):
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", MODULE, "supervise", "--state-dir", str(self.state),
             "--wall-seconds", str(wall), "--term-seconds", "0.2", "--kill-seconds", "1",
             "--max-owned", str(owned), *extra, "--", sys.executable, "-B", "-c", code,
             str(self.marker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.running.append(process)
        return process

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Synthetic worker readiness deadline exceeded")

    def ready(self, process):
        self.wait_for(lambda: self.marker.exists() or process.poll() is not None)
        self.assertIsNone(process.poll(), "Synthetic supervisor exited before worker readiness")

    def finish(self, process, expected):
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(stderr, "")
        result = json.loads(stdout)
        self.assertEqual(result["status"], expected)
        self.assertEqual(process.returncode, lifecycle.EXIT_CODES[expected])
        self.assertEqual(set(result), {"schema", "status", "quiescent", "worker_exit", "escalated"})
        if expected in {"completed", "worker_failed", "cancelled", "timed_out", "capacity_exceeded"}:
            self.assertTrue(result["quiescent"])
            envelope = json.loads((self.state / "receipt.json").read_text())
            self.assertEqual(envelope["result"], result)
            self.envelopes.append(envelope)
        return result

    def stop(self, *, wait=3):
        result = subprocess.run([sys.executable, "-B", "-m", MODULE, "cancel",
                                 "--state-dir", str(self.state), "--wait-seconds", str(wait)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.stderr, "")
        return result.returncode, json.loads(result.stdout)

    def state_value(self):
        return json.loads((self.state / "state.json").read_text())

    def mutate(self, change):
        path = self.state / "state.json"
        value = self.state_value()
        change(value)
        path.write_text(json.dumps(value))

    @staticmethod
    def child_identity(pid):
        try:
            fields = Path('/proc', str(pid), 'stat').read_bytes().rsplit(b')', 1)[1].split()
            uid_line = next(line for line in Path('/proc', str(pid), 'status').read_bytes().splitlines()
                            if line.startswith(b'Uid:'))
            return {'pid': pid, 'start': int(fields[19]), 'uid': int(uid_line.split()[1]),
                    'state': fields[0].decode('ascii'), 'parent': int(fields[1])}
        except FileNotFoundError:
            return None

    def pin_child(self, process):
        def published():
            try:
                value = json.loads(self.marker.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                return False
            return (isinstance(value, dict) and set(value) == {'pid', 'start', 'uid'}
                    and all(type(item) is int for item in value.values())
                    and value['pid'] > 1 and value['start'] > 0 and value['uid'] == os.getuid())
        self.wait_for(lambda: published() or process.poll() is not None)
        self.assertIsNone(process.poll(), 'Supervisor exited before identity publication')
        self.assertTrue(published(), 'Complete identity required, not marker existence')
        expected = json.loads(self.marker.read_text())
        before = self.child_identity(expected['pid'])
        self.assertIsNotNone(before, 'Original descendant must be live when pinned')
        self.assertEqual({key: before[key] for key in expected}, expected)
        fd = os.pidfd_open(expected['pid'])
        self.addCleanup(os.close, fd)
        after_open = self.child_identity(expected['pid'])
        self.assertIsNotNone(after_open)
        self.assertEqual({key: after_open[key] for key in expected}, expected)
        self.assertFalse(select.select([fd], [], [], 0)[0], 'Pin a live original, not a stale PID')
        return {'expected': expected, 'before': before, 'pidfd': fd,
                'heartbeat': self.marker.with_suffix('.heartbeat')}

    def assert_child_quiescent(self, proof):
        expected = proof['expected']
        after = self.child_identity(expected['pid'])
        first = proof['heartbeat'].read_bytes()
        time.sleep(.1)
        last = proof['heartbeat'].read_bytes()
        exited = bool(select.select([proof['pidfd']], [], [], 0)[0])
        same = after is not None and all(after[key] == expected[key] for key in expected)
        live_original = same and after['state'] not in {'Z', 'X'}
        evidence = {'schema': 'synthetic-descendant-quiescence-v1', 'case': self._testMethodName,
                    'expected': expected, 'before': proof['before'], 'after': after,
                    'pidfd_exited': exited, 'original_live': live_original,
                    'heartbeat_unchanged': first == last}
        # Fixed synthetic identity/liveness evidence, never command/environment text.
        print(json.dumps(evidence, sort_keys=True), flush=True)
        self.assertTrue(exited, 'Original descendant pidfd must acknowledge exit')
        self.assertFalse(live_original, 'A surviving owned descendant is never ignored')
        self.assertEqual(first, last, 'An owned descendant must stop writing after ACK')

    def test_success(self):
        result = self.finish(self.start("pass"), "completed")
        self.assertEqual(result["worker_exit"], 0)
        self.assertFalse(result["escalated"])

    def test_worker_failure(self):
        result = self.finish(self.start("raise SystemExit(7)"), "worker_failed")
        self.assertEqual(result["worker_exit"], 7)

    def test_worker_signal(self):
        result = self.finish(self.start("import os,signal; os.kill(os.getpid(),signal.SIGTERM)"), "worker_failed")
        self.assertEqual(result["worker_exit"], -signal.SIGTERM)

    def test_new_session(self):
        code = "import os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid()==os.getsid(0)==os.getpgrp()))"
        self.finish(self.start(code), "completed")
        self.assertEqual(self.marker.read_text(), "True")

    def test_wall_budget(self):
        started = time.monotonic()
        process = self.start(READY_WORKER, wall=0.25)
        result = self.finish(process, "timed_out")
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(result["escalated"])

    def test_term_ignoring_worker_escalates(self):
        process = self.start(IGNORE_WORKER, wall=0.4)
        self.ready(process)
        result = self.finish(process, "timed_out")
        self.assertTrue(result["escalated"])
        self.assertEqual(result["worker_exit"], -signal.SIGKILL)

    def test_authenticated_cancel_ack(self):
        process = self.start(READY_WORKER, wall=5)
        self.ready(process)
        code, acknowledgement = self.stop()
        self.assertEqual(code, 0)
        self.assertTrue(acknowledgement["quiescent"])
        self.assertEqual(self.finish(process, "cancelled"), acknowledgement)

    def test_cancel_escalates(self):
        process = self.start(IGNORE_WORKER, wall=5)
        self.ready(process)
        code, result = self.stop()
        self.assertEqual(code, 0)
        self.assertTrue(result["escalated"])
        self.finish(process, "cancelled")

    def test_supervisor_signal_requests_cleanup(self):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=sig):
                self.state = self.root / ("signal-" + str(sig))
                self.marker = self.root / ("marker-" + str(sig))
                process = self.start(IGNORE_WORKER, wall=5)
                self.ready(process)
                process.send_signal(sig)
                self.assertTrue(self.finish(process, "cancelled")["escalated"])

    def test_leader_exit_with_daemonized_descendant(self):
        code = CHILD_PROBE + """
import os,pathlib,signal,sys,time
pid = os.fork()
if pid:
    # Fence leader exit until the test has pinned the live daemon identity.
    while not pathlib.Path(sys.argv[1]).with_suffix('.release').exists(): time.sleep(.005)
    os._exit(0)
os.setsid()
pid = os.fork()
if pid: os._exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
owned_child_probe()
"""
        process = self.start(code, wall=5)
        proof = self.pin_child(process)
        self.marker.with_suffix('.release').touch()
        result = self.finish(process, "completed")
        self.assertTrue(result["escalated"])
        self.assert_child_quiescent(proof)

    def test_nested_descendants_cancel(self):
        code = CHILD_PROBE + """
import os,pathlib,signal,sys,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        owned_child_probe()
time.sleep(30)
"""
        process = self.start(code, wall=5)
        proof = self.pin_child(process)
        code, result = self.stop()
        self.assertEqual(code, 0)
        self.assertTrue(result["quiescent"])
        self.finish(process, "cancelled")
        self.assert_child_quiescent(proof)

    def test_thread_fork_descendant(self):
        code = CHILD_PROBE + """
import os,pathlib,signal,sys,threading,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
def spawn():
    if os.fork() == 0:
        os.setsid()
        owned_child_probe()
thread = threading.Thread(target=spawn)
thread.start()
thread.join()
time.sleep(30)
"""
        process = self.start(code, wall=5)
        proof = self.pin_child(process)
        self.assertEqual(self.stop()[0], 0)
        self.finish(process, "cancelled")
        self.assert_child_quiescent(proof)

    def test_partial_identity_wait_and_live_descendant_rejected(self):
        code = CHILD_PROBE + """
import os,pathlib,signal,sys,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.fork() == 0:
    os.setsid()
    # Regression for the old file-exists/PID-read readiness window.
    pathlib.Path(sys.argv[1]).write_text('')
    time.sleep(.15)
    owned_child_probe()
time.sleep(30)
"""
        process = self.start(code, wall=5)
        proof = self.pin_child(process)
        with self.assertRaises(AssertionError):
            self.assert_child_quiescent(proof)
        self.assertEqual(self.stop()[0], 0)
        self.finish(process, "cancelled")
        self.assert_child_quiescent(proof)

    def test_private_modes(self):
        process = self.start(READY_WORKER, wall=5)
        self.ready(process)
        for path, mode in ((self.state, 0o700), (self.state / "state.json", 0o600),
                           (self.state / "control.sock", 0o600)):
            self.assertEqual(path.stat().st_mode & 0o777, mode)
        self.assertEqual(self.stop()[0], 0)
        self.finish(process, "cancelled")
        self.assertEqual((self.state / "receipt.json").stat().st_mode & 0o777, 0o600)

    def test_unrelated_process_not_signalled(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.running.append(unrelated)
        process = self.start(READY_WORKER, wall=5)
        self.ready(process)
        self.assertEqual(self.stop()[0], 0)
        self.finish(process, "cancelled")
        self.assertIsNone(unrelated.poll())

    def test_wrong_token_does_not_cancel(self):
        process = self.start(READY_WORKER, wall=5)
        self.ready(process)
        value = self.state_value()
        with lifecycle._Directory(self.state) if False else socket.socket(socket.AF_UNIX) as connection:
            # Short anchored socket path avoids Unix pathname length limits.
            directory = lifecycle._Directory(self.state)
            try:
                connection.settimeout(2)
                connection.connect(directory.socket_path())
                connection.sendall(json.dumps({"schema": 1, "run_id": value["run_id"],
                                                "token": "0" * 64}).encode())
                result = json.loads(connection.recv(4096))
            finally:
                directory.close()
        self.assertEqual(result["status"], "invalid_state")
        self.assertIsNone(process.poll())
        self.assertEqual(self.stop()[0], 0)
        self.finish(process, "cancelled")

    def test_partial_client_cannot_defeat_budget(self):
        process = self.start(READY_WORKER, wall=0.5)
        self.ready(process)
        directory = lifecycle._Directory(self.state)
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect(directory.socket_path())
                self.finish(process, "timed_out")
        finally:
            directory.close()

    def test_mutated_token_fails_closed_and_cleans(self):
        process = self.start(IGNORE_WORKER, wall=5)
        self.ready(process)
        self.mutate(lambda value: value.update(token="0" * 64))
        self.assertNotEqual(self.stop()[0], 0)
        result = self.finish(process, "invalid_state")
        self.assertTrue(result["quiescent"])

    def test_start_identity_mismatch_no_arbitrary_kill(self):
        process = self.start(READY_WORKER, wall=5)
        self.ready(process)
        self.mutate(lambda value: value["identity"].update(start=value["identity"]["start"] + 1))
        self.assertNotEqual(self.stop()[0], 0)
        self.assertTrue(self.finish(process, "invalid_state")["quiescent"])

    def test_stale_state_rejects_disk_receipt(self):
        self.finish(self.start("pass"), "completed")
        code, result = self.stop()
        self.assertEqual(code, 30)
        self.assertFalse(result["quiescent"])

    def test_identical_state_replacement_is_mutation(self):
        process = self.start(IGNORE_WORKER, wall=5)
        self.ready(process)
        path = self.state / "state.json"
        replacement = self.state / "replacement"
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(path)
        self.assertTrue(self.finish(process, "invalid_state")["quiescent"])

    def test_bad_file_forms_rejected(self):
        for kind in ("symlink", "fifo", "oversize", "mode", "hardlink", "duplicate"):
            with self.subTest(kind=kind):
                self.state = self.root / kind
                self.state.mkdir(mode=0o700)
                path = self.state / "state.json"
                if kind == "symlink":
                    path.symlink_to(self.root / "absent")
                elif kind == "fifo":
                    os.mkfifo(path, 0o600)
                else:
                    path.write_bytes(b"x" * 4097 if kind == "oversize" else b'{"schema":1,"schema":1}')
                    path.chmod(0o644 if kind == "mode" else 0o600)
                    if kind == "hardlink":
                        os.link(path, self.state / "alias")
                code, result = self.stop()
                self.assertEqual(code, 30)
                self.assertFalse(result["quiescent"])

    def test_directory_reuse_and_symlink_rejected(self):
        self.state.mkdir(mode=0o700)
        self.finish(self.start("pass"), "invalid_state")
        target = self.root / "target"
        target.mkdir(mode=0o700)
        self.state = self.root / "link"
        self.state.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.stop()[0], 30)

    def test_capacity_exceeded_quiesces(self):
        code = """
import os,signal,time
for _ in range(4):
    if os.fork() == 0:
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        time.sleep(30)
        os._exit(0)
time.sleep(.1)
os._exit(0)
"""
        result = self.finish(self.start(code, owned=1), "capacity_exceeded")
        self.assertTrue(result["escalated"])

    def test_caps_and_nonfinite_rejected_before_spawn(self):
        for option, value in (("--wall-seconds", "2701"), ("--wall-seconds", "nan"),
                              ("--wall-seconds", "inf"), ("--term-seconds", "31"),
                              ("--kill-seconds", "0"), ("--max-owned", "257")):
            with self.subTest(option=option, value=value):
                self.finish(self.start("pass", extra=(option, value)), "invalid_state")
                self.assertFalse(self.state.exists())

    def test_sanitized_errors_and_worker_output(self):
        sentinel = "SYNTHETIC-ARGUMENT-NOT-FOR-OUTPUT"
        process = self.start(f"import sys; print('{sentinel}'); print('{sentinel}',file=sys.stderr)")
        self.finish(process, "completed")
        bad = subprocess.run([sys.executable, "-B", "-m", MODULE, "--" + sentinel],
                             capture_output=True, text=True, timeout=2)
        self.assertNotIn(sentinel, bad.stdout + bad.stderr)
        self.assertEqual(bad.stderr, "")
        self.assertEqual(bad.returncode, 30)

    def test_pidfd_identity_race_rejected(self):
        identity = {"pid": 123, "start": 1, "uid": os.getuid()}
        changed = dict(identity, start=2)
        with mock.patch.object(lifecycle, "_identity", side_effect=[(identity, os.getpid()),
                                                                  (changed, os.getpid())]), \
                mock.patch.object(os, "pidfd_open", return_value=77), \
                mock.patch.object(os, "close"), \
                mock.patch.object(signal, "pidfd_send_signal") as send:
            with self.assertRaises(lifecycle.Rejected):
                lifecycle._signal_child(123, signal.SIGTERM)
            send.assert_not_called()

    def test_no_unowned_child_signals(self):
        with mock.patch.object(lifecycle, "_identity", return_value=({"pid": 123}, 1)), \
                mock.patch.object(os, "pidfd_open") as opened:
            with self.assertRaises(lifecycle.Rejected):
                lifecycle._signal_child(123, signal.SIGTERM)
            opened.assert_not_called()

    def test_missing_pidfd_fails_closed(self):
        with mock.patch.object(os, "pidfd_open", side_effect=OSError):
            with self.assertRaises(lifecycle.Unsupported):
                lifecycle._linux()

    def test_cancel_exit_contract(self):
        for status, code in lifecycle.EXIT_CODES.items():
            for quiescent in (True, False):
                result = lifecycle._result(status, quiescent)
                acknowledged = status in {"completed", "worker_failed", "cancelled", "timed_out"}
                expected = (0 if quiescent else 32) if acknowledged else code
                self.assertEqual(lifecycle.exit_code(result, cancelling=True), expected)
        self.assertEqual(lifecycle.exit_code(lifecycle._result("completed", False)), 32)
        self.assertEqual(lifecycle.MAX_WALL_SECONDS, 45 * 60)


if __name__ == "__main__":
    unittest.main()
