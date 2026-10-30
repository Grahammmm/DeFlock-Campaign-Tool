"""Synthetic adapter composition; no live adapter, network or private records."""
import contextlib
import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from campaign_tool.records import contained_resume as contained
from campaign_tool.records import worker_lifecycle as lifecycle


@unittest.skipUnless(sys.platform == 'linux' and os.getuid() != 0,
                     'unprivileged Linux pidfd/subreaper qualification')
class ContainedResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='contained-resume-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent = self.root / 'runs'
        self.parent.mkdir(mode=0o700)
        self.adapter = self.root / 'adapter.py'

    def bind(self, source):
        raw = source.encode()
        self.adapter.write_bytes(raw)
        return contained.AdapterIdentity(str(self.adapter), hashlib.sha256(raw).hexdigest(), 'resume')

    def run_adapter(self, source, **kwargs):
        return contained.run_contained(self.bind(source), execute=True,
                                       run_parent=str(self.parent), seconds=6, **kwargs)

    def test_clean_leader_and_request(self):
        result = self.run_adapter('''
def resume(request):
    from campaign_tool.records.bounded_resume import Request
    assert request == Request(execute=True)
''')
        self.assertEqual(result, {'status': 'completed', 'completion_claimed': True,
                                  'quiescent': True, 'escalated': False})

    def test_descendants_are_quiescent_before_completion(self):
        for daemon in (False, True):
            with self.subTest(daemon=daemon):
                marker = self.root / ('daemon' if daemon else 'child')
                source = f'''
import json, os, pathlib, signal, time
def resume(request):
    marker = pathlib.Path({str(marker)!r})
    if os.fork() == 0:
        if {daemon!r}:
            os.setsid()
            if os.fork(): os._exit(0)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        fields = pathlib.Path('/proc/self/stat').read_bytes().rsplit(b')', 1)[1].split()
        identity = dict(pid=os.getpid(), start=int(fields[19]), uid=os.getuid())
        pending = marker.with_suffix('.pending')
        pending.write_text(json.dumps(identity))
        pending.replace(marker)
        time.sleep(30)
        os._exit(0)
    while not marker.with_suffix('.release').exists(): time.sleep(.005)
'''
                identity = self.bind(source)
                runner = self.root / 'runner.py'
                runner.write_text('import os, sys\nsys.path.insert(0, os.getcwd())\n'
                                  'import json\nfrom campaign_tool.records.contained_resume import '
                                  'AdapterIdentity, run_contained\n'
                                  f'print(json.dumps(run_contained(AdapterIdentity({identity.path!r}, '
                                  f'{identity.sha256!r}, "resume"), execute=True, seconds=6, '
                                  f'run_parent={str(self.parent)!r})))\n')
                started = time.monotonic()
                with subprocess.Popen([sys.executable, '-B', str(runner)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                    fd = None
                    try:
                        deadline = started + 3
                        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(.01)
                        expected = json.loads(marker.read_text())
                        self.assertEqual(lifecycle._identity(expected['pid'])[0], expected)
                        fd = os.pidfd_open(expected['pid'])
                        self.assertEqual(lifecycle._identity(expected['pid'])[0], expected)
                        self.assertFalse(select.select([fd], [], [], 0)[0])
                        marker.with_suffix('.release').touch()
                        stdout, stderr = process.communicate(timeout=6)
                        result = json.loads(stdout)
                        self.assertLess(time.monotonic() - started, 6)
                        self.assertEqual(stderr, b'')
                        self.assertEqual(result['status'], 'completed')
                        self.assertTrue(result['quiescent'])
                        self.assertTrue(result['completion_claimed'])
                        self.assertEqual(result['escalated'], daemon)
                        self.assertTrue(select.select([fd], [], [], 0)[0])
                    finally:
                        if fd is not None:
                            if not select.select([fd], [], [], 0)[0]:
                                signal.pidfd_send_signal(fd, signal.SIGKILL)
                                select.select([fd], [], [], 2)
                            os.close(fd)
                        if process.poll() is None:
                            process.kill()
                            process.wait()

    def test_timeout(self):
        started = time.monotonic()
        result = self.run_adapter('''
import signal, time
def resume(request):
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    time.sleep(30)
''')
        self.assertEqual(result['status'], 'timed_out')
        self.assertFalse(result['completion_claimed'])
        self.assertLessEqual(time.monotonic() - started, 15)

    def test_hash_mismatch_before_import(self):
        marker = self.root / 'marker'
        identity = self.bind(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
        identity = dataclasses.replace(identity, sha256=hashlib.sha256(b'wrong').hexdigest())
        with patch.object(contained.subprocess, 'Popen', side_effect=AssertionError('launch')) as launch:
            result = contained.run_contained(identity, execute=True, run_parent=str(self.parent))
        launch.assert_not_called()
        self.assertEqual(result['status'], 'not_admitted')
        self.assertFalse(marker.exists())

    def test_replacement_runs_captured_bytes(self):
        original = subprocess.Popen
        def replace_then_launch(*args, **kwargs):
            self.adapter.write_text('raise RuntimeError("replacement must not run")')
            return original(*args, **kwargs)
        with patch.object(contained.subprocess, 'Popen', side_effect=replace_then_launch):
            self.assertEqual(self.run_adapter('def resume(request): pass')['status'], 'completed')

    def test_invalid_admission_never_launches(self):
        identity = self.bind('def resume(request): pass')
        link = self.root / 'link.py'
        link.symlink_to(self.adapter)
        directory_link = self.root / 'linked-runs'
        directory_link.symlink_to(self.parent, target_is_directory=True)
        public = self.root / 'public'
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        cases = [dict(identity=dataclasses.replace(identity, path=str(link))),
                 dict(identity=dataclasses.replace(identity, path='relative.py')),
                 dict(identity=dataclasses.replace(identity, attribute='bad.name')),
                 dict(identity=dataclasses.replace(identity, sha256='INVALID')),
                 dict(run_parent=str(directory_link)), dict(run_parent=str(public)),
                 dict(run_parent='relative')]
        cases.extend(dict(seconds=value) for value in (True, 5, 121, float('nan'), float('inf')))
        with patch.object(contained.subprocess, 'Popen', side_effect=AssertionError('launch')) as launch:
            for case in cases:
                arguments = dict(identity=identity, execute=True, seconds=6, run_parent=str(self.parent))
                arguments.update(case)
                with self.subTest(case=case):
                    self.assertEqual(contained.run_contained(**arguments)['status'], 'not_admitted')
            original = os.fstat
            def foreign(fd):
                info = original(fd)
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            with patch.object(os, 'fstat', side_effect=foreign):
                self.assertEqual(contained.run_contained(identity, execute=True,
                                 run_parent=str(self.parent))['status'], 'not_admitted')
        launch.assert_not_called()

    def test_unsupported_runtime_never_launches(self):
        with patch.object(lifecycle, '_linux', side_effect=lifecycle.Unsupported), \
                patch.object(contained.subprocess, 'Popen') as launch:
            self.assertEqual(self.run_adapter('def resume(request): pass')['status'], 'unsupported')
        launch.assert_not_called()

    def test_binding_bytes_unread_until_supervisor_is_verified(self):
        binding = str(self.parent / 'absent.binding')
        with patch.object(contained, '_read_file', side_effect=AssertionError('early read')) as read, \
                patch.object(os, '_exit', side_effect=SystemExit) as exit_worker:
            with self.assertRaises(SystemExit):
                contained._worker(binding)
        exit_worker.assert_called_once_with(3)
        read.assert_not_called()

    def test_direct_worker_rejected_before_adapter_import(self):
        marker = self.root / 'direct-marker'
        raw = f'from pathlib import Path\nPath({str(marker)!r}).touch()'.encode()
        binding = self.parent / 'direct.binding'
        binding.write_text(json.dumps(dict(state_dir=str(self.parent / 'absent'), data=raw.hex(),
                                          sha256=hashlib.sha256(raw).hexdigest(), attribute='resume', seconds=3)))
        binding.chmod(0o600)
        for mismatch in ('pid', 'start'):
            state_dir = self.parent / mismatch
            with contextlib.closing(lifecycle._Directory(state_dir, create=True)) as directory:
                identity = dict(lifecycle._identity(os.getpid())[0])
                identity[mismatch] += 1
                directory.write('state.json', dict(schema=1, identity=identity, boot=lifecycle._boot(),
                                run_id=os.urandom(16).hex(), token=os.urandom(32).hex(),
                                wall_seconds=6, term_seconds=2, kill_seconds=2, max_owned=8))
            value = json.loads(binding.read_text())
            value['state_dir'] = str(state_dir)
            binding = self.parent / (mismatch + '.binding')
            binding.write_text(json.dumps(value))
            binding.chmod(0o600)
            result = subprocess.run([sys.executable, '-B', '-m', contained.__name__,
                                     '__supervised_worker__', str(binding)], capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout + result.stderr, b'')
            self.assertFalse(marker.exists())
        binding = self.parent / 'absent.binding'
        binding.write_text(json.dumps(dict(state_dir=str(self.parent / 'absent'), data=raw.hex())))
        binding.chmod(0o600)
        result = subprocess.run([sys.executable, '-B', '-m', contained.__name__,
                                 '__supervised_worker__', str(binding)], capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout + result.stderr, b'')
        self.assertFalse(marker.exists())

    def test_receipt_required_and_bound(self):
        identity = self.bind('def resume(request): pass')
        for kind in ('missing', 'run_id', 'identity', 'stdout', 'nonquiescent', 'valid'):
            with self.subTest(kind=kind):
                stdout = lifecycle._result('completed', True, 0)
                def fake_stdout(process, seconds):
                    state_dir = launch.call_args.args[0][6]
                    with contextlib.closing(lifecycle._Directory(state_dir, create=True)) as directory:
                        state = dict(schema=1, identity=lifecycle._identity(os.getpid())[0],
                                     boot=lifecycle._boot(), run_id=os.urandom(16).hex(),
                                     token=os.urandom(32).hex(), wall_seconds=6, term_seconds=2,
                                     kill_seconds=2, max_owned=8)
                        directory.write('state.json', state)
                        value = dict(stdout)
                        if kind == 'nonquiescent':
                            value['quiescent'] = False
                            stdout['quiescent'] = False
                        if kind == 'stdout': value['worker_exit'] = 7
                        envelope = {key: state[key] for key in ('identity', 'boot', 'run_id')}
                        envelope['result'] = value
                        if kind == 'run_id': envelope['run_id'] = os.urandom(16).hex()
                        if kind == 'identity': envelope['identity'] = dict(state['identity'], start=1)
                        if kind != 'missing': directory.write('receipt.json', envelope)
                    return stdout
                with patch.object(contained.subprocess, 'Popen') as launch, \
                        patch.object(contained, '_stdout', side_effect=fake_stdout):
                    result = contained.run_contained(identity, execute=True, seconds=6,
                                                     run_parent=str(self.parent))
                self.assertEqual(result['completion_claimed'], kind == 'valid')
                self.assertEqual(result['status'] == 'completed', kind == 'valid')

    def test_parent_timeout_kills_only_unreaped_supervisor(self):
        with patch.object(contained.subprocess, 'Popen') as launch, \
                patch.object(contained, '_stdout', side_effect=subprocess.TimeoutExpired('synthetic', .001)):
            process = launch.return_value.__enter__.return_value
            process.poll.return_value = None
            result = self.run_adapter('def resume(request): pass')
        self.assertEqual(result['status'], 'not_quiescent')
        self.assertFalse(result['completion_claimed'])
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with()

    def test_stdout_is_capped(self):
        reader, writer = os.pipe()
        try:
            os.write(writer, b'x' * 4097)
            with os.fdopen(reader, 'rb', closefd=False) as stream:
                process = SimpleNamespace(stdout=stream, args=[])
                with self.assertRaises(lifecycle.Rejected): contained._stdout(process, 1)
        finally:
            os.close(reader)
            os.close(writer)

    def test_redaction_through_wrapping_process(self):
        identity = self.bind('''
import os, sys
def resume(request):
    print('synthetic-private')
    print('synthetic-private', file=sys.stderr)
    os.write(1, b'synthetic-private')
    os.write(2, b'synthetic-private')
    raise RuntimeError('synthetic-private')
''')
        runner = self.root / 'redaction.py'
        runner.write_text('import os, sys\nsys.path.insert(0, os.getcwd())\nimport json\n'
                          'from campaign_tool.records.contained_resume import AdapterIdentity, run_contained\n'
                          f'print(json.dumps(run_contained(AdapterIdentity({identity.path!r}, '
                          f'{identity.sha256!r}, "resume"), execute=True, seconds=6, '
                          f'run_parent={str(self.parent)!r})))\n')
        result = subprocess.run([sys.executable, '-B', str(runner)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['status'], 'failed')
        self.assertNotIn(b'synthetic-private', result.stdout + result.stderr)
        self.assertEqual(result.stderr, b'')

    def test_plan_has_no_filesystem_or_launch(self):
        with patch('builtins.open', side_effect=AssertionError('read')), \
                patch.object(os, 'open', side_effect=AssertionError('read')), \
                patch.object(contained.subprocess, 'Popen', side_effect=AssertionError('launch')):
            result = contained.run_contained(None, run_parent='/absent')
            self.assertEqual(result['status'], 'plan_only')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(contained.main([]), 0)
                self.assertEqual(contained.main(['--execute']), 2)
            self.assertNotIn('absent', output.getvalue())


if __name__ == '__main__':
    unittest.main()
