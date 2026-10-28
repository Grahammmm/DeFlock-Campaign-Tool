"""Clock-contract regressions: no sockets; original stream test stays unchanged."""
import unittest
from unittest import mock
from campaign_tool.records.portal import engine
from tests import test_records_portal as baseline


class ClockContractTests(unittest.TestCase):
    def setUp(self):
        self.case = baseline.PortalTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def test_three_samples_timeout_in_stream_not_before_open(self):
        clock = mock.Mock(side_effect=[0, 0, 26])
        transport = baseline.FakeTransport(baseline.response())
        self.case.run_fetch(transport, mono=clock)
        self.assertEqual(clock.call_count, 3)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.closed, 1)
        self.assertEqual(self.case.reason(), 'timeout')
        self.assertEqual(self.case.queue.status()['versions'], 0)
        self.assertEqual(list(self.case.queue.objects.iterdir()), [])

    def test_run_timeout_before_open_no_transport(self):
        transport = baseline.FakeTransport(baseline.response())
        self.case.run_fetch(transport, mono=mock.Mock(side_effect=[0, 2]),
                            limits=engine.Limits(run_timeout=1, timeout=25))
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.case.reason(), 'timeout')
        self.assertEqual(self.case.queue.status()['versions'], 0)

    def test_preflight_time_counts_toward_run_timeout(self):
        now = [0]
        def preflight(queue):
            now[0] = 2
        transport = baseline.FakeTransport(baseline.response())
        self.case.run_fetch(transport, mono=lambda: now[0], preflight=preflight,
                            limits=engine.Limits(run_timeout=1, timeout=25))
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.case.reason(), 'timeout')

    def test_later_item_does_not_reset_run_deadline(self):
        self.case.queue.inventory(baseline.HOST, '26-001', '43', baseline.URL.replace('42', '43'), baseline.SOURCE)
        transport = baseline.FakeTransport(baseline.response(), baseline.response())
        # start, open, chunk, EOF, promote, second-item start
        clock = mock.Mock(side_effect=[0, 0, 0, 0, 0, 2])
        result = self.case.run_fetch(transport, mono=clock, limits=engine.Limits(run_timeout=1))
        self.assertEqual(result['received'], 1)
        self.assertEqual(result['attempted'], 1)
        self.assertEqual(result['deferred_by_run_cap'], 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sorted(i['state'] for i in self.case.queue.status()['items']), ['pending', 'received'])

    def test_clock_failure_before_attempt_propagates(self):
        transport = baseline.FakeTransport(baseline.response())
        with self.assertRaisesRegex(engine.ClockError, '^clock_failure$'):
            self.case.run_fetch(transport, mono=mock.Mock(side_effect=StopIteration('private clock detail')))
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.case.item()['state'], 'pending')
        self.assertEqual(self.case.queue.status()['attempts'], 0)

    def test_clock_failure_in_stream_propagates_and_is_recoverable(self):
        transport = baseline.FakeTransport(baseline.response())
        with self.assertRaisesRegex(engine.ClockError, '^clock_failure$'):
            self.case.run_fetch(transport, mono=mock.Mock(side_effect=[0, 0, StopIteration('private clock detail')]))
        self.assertEqual(transport.closed, 1)
        self.assertEqual(self.case.item()['state'], 'fetching')
        self.assertIsNone(self.case.reason())
        self.assertEqual(self.case.queue.status()['versions'], 0)
        self.assertEqual(list(self.case.queue.objects.iterdir()), [])
        attempt = self.case.queue.db.execute('SELECT result FROM portal_attempts').fetchone()[0]
        self.assertEqual(attempt, 'running')
        self.assertNotIn('transport_or_storage_failure', str(self.case.queue.status()))
        self.assertNotIn('private clock detail', str(self.case.queue.status()))
        recovered = self.case.run_fetch()
        self.assertEqual(recovered['received'], 1)
        results = [row[0] for row in self.case.queue.db.execute('SELECT result FROM portal_attempts ORDER BY id')]
        self.assertEqual(results, ['interrupted', 'received'])

    def test_clock_timeout_exception_is_not_transport_timeout(self):
        with self.assertRaisesRegex(engine.ClockError, '^clock_failure$'):
            self.case.run_fetch(mono=mock.Mock(side_effect=[0, TimeoutError('private clock detail')]))
        self.assertIsNone(self.case.reason())
        self.assertEqual(self.case.item()['state'], 'fetching')

    def test_wall_clock_exception_is_not_transport_failure(self):
        calls = [0]
        def wall():
            calls[0] += 1
            # recovery, SELECT, start attempt, then authorization inside _download
            if calls[0] == 4:
                raise ValueError('private clock detail')
            return baseline.NOW.timestamp()
        with self.assertRaisesRegex(engine.ClockError, '^clock_failure$'):
            engine.fetch_queue(self.case.queue, apply=True, approval_loader=baseline.approval,
                               transport=baseline.FakeTransport(baseline.response()),
                               egress=lambda url: True, wall=wall)
        self.assertEqual(self.case.item()['state'], 'fetching')
        self.assertIsNone(self.case.reason())

    def test_negative_control_clock_adapter_is_deciding_gate(self):
        # Keep queue, transport and clock input valid except for one injected
        # exception. Disabling ONLY the adapter reproduces the false reason.
        with mock.patch.object(engine, '_checked_clock', lambda clock: clock):
            with self.assertRaises(AssertionError):
                with self.assertRaises(engine.ClockError):
                    self.case.run_fetch(mono=mock.Mock(side_effect=[0, 0, StopIteration()]))
        self.assertEqual(self.case.reason(), 'transport_or_storage_failure')
        self.assertEqual(self.case.item()['state'], 'retry')
        self.assertEqual(self.case.queue.status()['versions'], 0)
