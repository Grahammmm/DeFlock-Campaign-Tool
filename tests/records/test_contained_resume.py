"""New composition contracts only; synthetic source, no installed mail or config."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from campaign_tool.records import contained_resume as contained


class AdmissionTests(unittest.TestCase):
    def test_plan_touches_no_adapter_or_supervisor(self):
        with patch.object(contained, '_reviewed_source', side_effect=AssertionError), \
                patch.object(contained.worker_lifecycle, 'supervise', side_effect=AssertionError):
            self.assertEqual(contained.run_once('/not/a/real/file')['status'], 'plan_only')

    def test_execute_requires_explicit_binding(self):
        with patch.object(contained.worker_lifecycle, 'supervise', side_effect=AssertionError):
            self.assertEqual(contained.run_once(execute=True)['reason'], 'reviewed_adapter_binding_required')

    def test_invalid_budget_or_opt_in_is_rejected(self):
        for seconds in (True, 0, 2.99, 3, 5.99, 121, float('nan'), float('inf')):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                contained.run_once(seconds=seconds)
        with self.assertRaises(ValueError):
            contained.run_once(execute=1)

    def test_work_and_cleanup_share_the_120_second_budget(self):
        receipt = {'status': 'completed', 'quiescent': True, 'worker_exit': 0}
        with patch.object(contained.worker_lifecycle, 'supervise', return_value=receipt) as supervise:
            self.assertTrue(contained.run_once('/synthetic.py', sha256='a'*64,
                                              state_dir='/fresh-state', execute=True)['completion_claimed'])
        self.assertEqual(supervise.call_args.kwargs,
                         {'wall_seconds': 118, 'term_seconds': 1, 'kill_seconds': 1, 'max_owned': 32})

    def test_inner_fence_deadline_is_strictly_inside_outer_wall(self):
        receipt = {'status': 'completed', 'quiescent': True, 'worker_exit': 0}
        for seconds in (6, 60, 120):
            with self.subTest(seconds=seconds):
                with patch.object(contained.worker_lifecycle, 'supervise',
                                  return_value=receipt) as supervise:
                    contained.run_once('/synthetic.py', sha256='a'*64, state_dir='/fresh-state',
                                       execute=True, seconds=seconds)
                wall = supervise.call_args.kwargs['wall_seconds']
                argument = supervise.call_args.args[0][-1]
                with patch.object(contained, '_parent_state'), \
                        patch.object(contained.bounded_resume, 'run_once',
                                     return_value={'status': 'completed',
                                                   'completion_claimed': True}) as inner:
                    self.assertEqual(contained._worker_entry('/fresh-state', '/synthetic.py',
                                                             'a'*64, argument), 0)
                inner_seconds = inner.call_args.kwargs['seconds']
                self.assertEqual(wall, seconds - contained.CLEANUP_SECONDS)
                self.assertEqual(inner_seconds, wall - contained.INNER_MARGIN_SECONDS)
                # Inner fence plus its own 2s worker cleanup ends before the outer wall.
                self.assertGreater(inner_seconds, 0)
                self.assertLess(inner_seconds + 2, wall)

    def test_inner_timeout_is_reported_as_timeout_without_claim(self):
        with patch.object(contained, '_parent_state'), \
                patch.object(contained.bounded_resume, 'run_once',
                             return_value={'status': 'timed_out', 'completion_claimed': False}):
            self.assertEqual(contained._worker_entry('/fresh-state', '/synthetic.py', 'a'*64, '120.0'),
                             contained.INNER_TIMED_OUT_EXIT)
        receipt = {'status': 'worker_failed', 'quiescent': True,
                   'worker_exit': contained.INNER_TIMED_OUT_EXIT}
        with patch.object(contained.worker_lifecycle, 'supervise', return_value=receipt):
            self.assertEqual(contained.run_once('/synthetic.py', sha256='a'*64,
                                               state_dir='/fresh-state', execute=True),
                             {'status': 'timed_out', 'completion_claimed': False, 'quiescent': True})

    def test_leader_completion_without_quiescence_is_not_success(self):
        receipt = {'status': 'completed', 'quiescent': False, 'worker_exit': 0}
        with patch.object(contained.worker_lifecycle, 'supervise', return_value=receipt):
            self.assertEqual(contained.run_once('/synthetic.py', sha256='a'*64,
                                               state_dir='/fresh-state', execute=True),
                             {'status': 'not_quiescent', 'completion_claimed': False, 'quiescent': False})

    def test_unbound_internal_entry_never_captures_source(self):
        with patch.object(contained, '_parent_state', side_effect=ValueError), \
                patch.object(contained, '_reviewed_source', side_effect=AssertionError):
            self.assertEqual(contained._worker_entry('/absent', '/synthetic.py', 'a'*64, '120'), 10)

    def test_worker_cli_has_no_execution_backdoor(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            contained.main(['--worker'])
        self.assertEqual(error.exception.code, 2)

    def test_capture_rejects_mismatch_symlink_and_writable_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'adapter.py'
            raw = b'def run(request):\n    pass\n'
            source.write_bytes(raw)
            source.chmod(0o600)
            digest = hashlib.sha256(raw).hexdigest()
            self.assertEqual(contained._reviewed_source(source, digest), raw)
            with self.assertRaises(ValueError):
                contained._reviewed_source(source, '0'*64)
            alias = root / 'alias.py'
            alias.symlink_to(source)
            with self.assertRaises(OSError):
                contained._reviewed_source(alias, digest)
            source.chmod(0o620)
            with self.assertRaises(ValueError):
                contained._reviewed_source(source, digest)


CHILD = '''
pid = os.fork()
if pid == 0:
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    fields = Path('/proc/self/stat').read_bytes().rsplit(b')', 1)[1].split()
    temporary = root / 'identity-pending'
    temporary.write_text(json.dumps({'pid': os.getpid(), 'start': int(fields[19]), 'uid': os.getuid()}))
    temporary.replace(root / 'identity')
    count = 0
    while True:
        (root / 'heartbeat').write_text(str(count))
        count += 1
        time.sleep(.02)
deadline = time.monotonic() + 1
while not (root / 'heartbeat').exists():
    if time.monotonic() > deadline:
        raise RuntimeError('synthetic child readiness failed')
    time.sleep(.01)
'''


@unittest.skipUnless(sys.platform == 'linux', 'new Linux composition acceptance required')
class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.getuid(), 0, 'unprivileged Linux required; no root acceptance')
        self.temporary = tempfile.TemporaryDirectory(prefix='contained-resume-')
        self.root = Path(self.temporary.name).resolve()
        self.running = []

    def tearDown(self):
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
        self.temporary.cleanup()

    def start(self, body, *, seconds=10, wrong_hash=False, prefix=''):
        source = self.root / 'adapter.py'
        code = ('import json, os, signal, time\nfrom pathlib import Path\n'
                + prefix + '\ndef run(request):\n'
                + '    root = Path(' + repr(str(self.root)) + ')\n'
                + '\n'.join('    ' + line for line in body.splitlines()) + '\n')
        source.write_text(code)
        source.chmod(0o600)
        digest = '0'*64 if wrong_hash else hashlib.sha256(source.read_bytes()).hexdigest()
        harness = ('import json, sys; from campaign_tool.records.contained_resume import run_once; '
                   'print(json.dumps(run_once(sys.argv[1], sha256=sys.argv[2], '
                   'state_dir=sys.argv[3], execute=True, seconds=float(sys.argv[4]))))')
        process = subprocess.Popen([sys.executable, '-B', '-c', harness, str(source), digest,
                                    str(self.root/'state'), str(seconds)], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.running.append(process)
        return process

    def finish(self, process):
        stdout, stderr = process.communicate(timeout=9)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stderr, '')
        self.assertNotIn('synthetic-private', stdout)
        return json.loads(stdout)

    def pin_child(self, process):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                identity = json.loads((self.root/'identity').read_text())
                pid = identity['pid']
                before = Path(f'/proc/{pid}/stat').read_bytes().rsplit(b')', 1)[1].split()
                self.assertEqual(int(before[19]), identity['start'])
                fd = os.pidfd_open(pid)
                after = Path(f'/proc/{pid}/stat').read_bytes().rsplit(b')', 1)[1].split()
                self.assertEqual(int(after[19]), identity['start'])
                self.assertEqual(identity['uid'], os.getuid())
                self.assertFalse(select.select([fd], [], [], 0)[0])
                return fd
            except FileNotFoundError:
                if process.poll() is not None:
                    break
                time.sleep(.01)
        self.fail('no live synthetic descendant identity was pinned')

    def assert_child_stopped(self, fd):
        try:
            self.assertTrue(select.select([fd], [], [], 0)[0], 'original pidfd has not exited')
            before = (self.root/'heartbeat').read_text()
            time.sleep(.08)
            self.assertEqual((self.root/'heartbeat').read_text(), before, 'synthetic writer survived')
        finally:
            os.close(fd)

    def test_normal_request_and_import_raw_output_are_fenced(self):
        process = self.start("assert request.execute and request.max_items == 1\n"
                             "assert not request.notify and not request.prune and not request.spawn_children\n"
                             "os.write(1, b'synthetic-private-body')\n"
                             "(root/'admitted').touch()", prefix="os.write(2, b'synthetic-private-import')\n")
        self.assertEqual(self.finish(process), {'status':'completed','completion_claimed':True,'quiescent':True})
        self.assertTrue((self.root/'admitted').exists())

    def test_leader_exit_reaps_session_escaped_descendant(self):
        # Deliberate synthetic violation of the no-child admission contract.
        # It proves the outer composition fence, not permission for real forks.
        # Keep the leader alive until the test pins the original live child.
        # Heartbeat existence alone cannot synchronize pidfd admission.
        wait_for_pin = '''
deadline = time.monotonic() + 3
while not (root / 'release-leader').exists():
    if time.monotonic() > deadline:
        raise RuntimeError('synthetic pin acknowledgement expired')
    time.sleep(.01)
'''
        process = self.start(CHILD + wait_for_pin)
        fd = self.pin_child(process)
        (self.root/'release-leader').touch()
        result = self.finish(process)
        self.assertEqual(result, {'status':'completed','completion_claimed':True,'quiescent':True})
        self.assert_child_stopped(fd)

    def test_timeout_reaps_descendant_and_cannot_claim_completion(self):
        process = self.start(CHILD + '\nsignal.signal(signal.SIGALRM, signal.SIG_IGN)\ntime.sleep(30)', seconds=6)
        fd = self.pin_child(process)
        result = self.finish(process)
        self.assertEqual(result['status'], 'timed_out')
        self.assertFalse(result['completion_claimed'])
        self.assertTrue(result['quiescent'])
        self.assert_child_stopped(fd)

    def test_changed_source_fails_before_adapter_effect(self):
        process = self.start("(root/'must-not-exist').touch()", wrong_hash=True)
        result = self.finish(process)
        self.assertFalse(result['completion_claimed'])
        self.assertTrue(result['quiescent'])
        self.assertFalse((self.root/'must-not-exist').exists())

    def test_unrelated_child_of_test_process_survives(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.running.append(unrelated)
        result = self.finish(self.start('pass'))
        self.assertTrue(result['completion_claimed'])
        self.assertIsNone(unrelated.poll())


if __name__ == '__main__':
    unittest.main()
