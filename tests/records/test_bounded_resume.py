"""Synthetic only: no installed adapter, credentials, network or campaign data."""
import contextlib
import io
import json
import os
from pathlib import Path
import platform
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

from campaign_tool.records import bounded_resume as bounded


class BoundedResumeTests(unittest.TestCase):
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

    @unittest.skipUnless(platform.system() == 'Linux', 'qualified Linux supervisor case')
    def test_adapter_receives_one_item_no_notify_no_prune(self):
        def adapter(request):
            assert request.execute and request.max_items == 1
            assert not request.notify and not request.prune
            print('synthetic output must not be returned')
        self.assertEqual(bounded.run_once(adapter, execute=True, seconds=1)['status'], 'completed')

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


if __name__ == '__main__':
    unittest.main()
