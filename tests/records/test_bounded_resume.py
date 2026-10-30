"""Synthetic only: no installed adapter, credentials, network or campaign data."""
import contextlib
import io
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from campaign_tool.records import bounded_resume as bounded


class BoundedResumeTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux' and os.getuid() != 0,
                         'unprivileged Linux pidfd regression')
    def test_inherited_sentinel_does_not_delay_leader(self):
        import select
        from campaign_tool.records import worker_lifecycle as lifecycle
        reader, writer = os.pipe()
        def adapter(request):
            if os.fork() == 0:
                identity, _ = lifecycle._identity(os.getpid())
                os.write(writer, json.dumps(identity).encode() + b'\n')
                time.sleep(3)
                os._exit(0)
        fd = None
        try:
            started = time.monotonic()
            result = bounded.run_once(adapter, execute=True, seconds=5)
            elapsed = time.monotonic() - started
            self.assertTrue(select.select([reader], [], [], 1)[0])
            expected = json.loads(os.read(reader, 4096))
            try:
                before, _ = lifecycle._identity(expected['pid'])
                self.assertEqual(before, expected)
                fd = os.pidfd_open(expected['pid'])
                self.assertEqual(lifecycle._identity(expected['pid'])[0], expected)
            except FileNotFoundError:
                self.fail('descendant exited before identity pin')
            self.assertLess(elapsed, 1.5)
            self.assertEqual(result, {'status': 'leader_completed',
                                     'completion_claimed': False, 'quiescence': 'unproved'})
        finally:
            if fd is not None:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
                self.assertTrue(select.select([fd], [], [], 2)[0])
                os.close(fd)
            os.close(reader)
            os.close(writer)

    def test_plan_never_calls_adapter(self):
        def forbidden(request):
            self.fail('plan executed adapter')
        self.assertEqual(bounded.run_once(forbidden)['status'], 'plan_only')

    def test_execute_without_adapter_fails_closed(self):
        self.assertEqual(bounded.run_once(execute=True),
                         {'status': 'blocked', 'reason': 'authorized_adapter_required'})

    def test_no_config_or_adapter_discovery_in_cli(self):
        with patch('builtins.open', side_effect=AssertionError('unexpected file read')), \
                patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bounded.main(['--execute']), 2)
        self.assertEqual(json.loads(output.getvalue())['reason'], 'authorized_adapter_required')

    def test_worker_flag_is_rejected_before_work(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            bounded.main(['--worker'])
        self.assertEqual(result.exception.code, 2)

    def test_invalid_budgets_rejected(self):
        for value in [True, 0, -1, 121, float('nan'), float('inf')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                bounded.run_once(execute=True, seconds=value)

    def test_direct_worker_requires_opt_in(self):
        messages = []
        connection = type('Connection', (), {'send': lambda _, v: messages.append(v),
                                             'close': lambda _: None})()
        bounded._worker(lambda _: self.fail('adapter called'), bounded.Request(),
                        time.monotonic() + 1, connection)
        self.assertEqual(messages, ['not_admitted'])

    def test_direct_worker_rejects_expired_deadline(self):
        messages = []
        connection = type('Connection', (), {'send': lambda _, v: messages.append(v),
                                             'close': lambda _: None})()
        bounded._worker(lambda _: self.fail('adapter called'), bounded.Request(execute=True),
                        time.monotonic() - 1, connection)
        self.assertEqual(messages, ['not_admitted'])

    def test_worker_rejects_request_allowing_children(self):
        messages = []
        connection = type('Connection', (), {'send': lambda _, v: messages.append(v),
                                             'close': lambda _: None})()
        bounded._worker(lambda _: self.fail('adapter called'),
                        bounded.Request(execute=True, spawn_children=True),
                        time.monotonic() + 1, connection)
        self.assertEqual(messages, ['not_admitted'])

    @unittest.skipUnless(platform.system() == 'Linux', 'qualified Linux supervisor case')
    def test_adapter_receives_one_item_no_notify_no_prune(self):
        def adapter(request):
            assert request.execute and request.max_items == 1
            assert not request.notify and not request.prune
            assert not request.spawn_children
            print('synthetic output must not be returned')
        self.assertEqual(bounded.run_once(adapter, execute=True, seconds=1)['status'], 'leader_completed')

    @unittest.skipUnless(platform.system() == 'Linux', 'qualified Linux supervisor case')
    def test_timeout_cannot_claim_completion_even_if_alarm_is_disabled(self):
        def adapter(request):
            signal.signal(signal.SIGALRM, signal.SIG_IGN)
            time.sleep(5)
        result = bounded.run_once(adapter, execute=True, seconds=0.1)
        self.assertEqual(result, {'status': 'timed_out', 'completion_claimed': False})

    @unittest.skipUnless(platform.system() == 'Linux', 'qualified Linux supervisor case')
    def test_exception_is_redacted(self):
        def adapter(request):
            raise ValueError('synthetic private details')
        result = bounded.run_once(adapter, execute=True, seconds=1)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('private', json.dumps(result))

    @unittest.skipUnless(platform.system() == 'Linux', 'qualified Linux output fence case')
    def test_raw_fd_and_inherited_child_output_do_not_escape(self):
        # The short-lived child deliberately violates the no-child admission
        # contract only to test descriptor inheritance. This is not descendant
        # cleanup qualification or permission for a live adapter to spawn.
        source = '''
import json, os, subprocess, sys
from campaign_tool.records.bounded_resume import run_once
def adapter(request):
    os.write(1, b'synthetic-private-stdout')
    os.write(2, b'synthetic-private-stderr')
    subprocess.run([sys.executable, '-c', 'import os; os.write(1, b"synthetic-private-child")'], check=True)
print(json.dumps(run_once(adapter, execute=True, seconds=2)))
'''
        result = subprocess.run([sys.executable, '-B', '-c', source], capture_output=True,
                                text=True, timeout=5, check=True)
        self.assertEqual(json.loads(result.stdout)['status'], 'leader_completed')
        self.assertEqual(result.stderr, '')
        self.assertNotIn('synthetic-private', result.stdout)


if __name__ == '__main__':
    unittest.main()
