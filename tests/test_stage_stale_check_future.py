"""RF1300-25 acceptance: a run report that started or finished after --now is from the future.

Synthetic run reports only, built from examples/run-report/records-run-report-v1.synthetic.json
through the existing test's ``report`` helper.
"""
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.test_stage_stale_check import T0, report, stamp
from tools import stage_stale_check as ssc

HOUR = dt.timedelta(hours=1)
LAST_FINISH = T0 + 12 * HOUR + dt.timedelta(seconds=450)  # finish of report(2), the newest of series()
QUIET_NOW = LAST_FINISH + 40 * HOUR  # newest in-time report finished 40 h before --now
NOW = dt.datetime(2026, 1, 2, 12, 0, 0, tzinfo=dt.timezone.utc)  # 17 h 52 m after LAST_FINISH


def spanning(index, start, finish, **kwargs):
    """A valid report started at ``start`` that claims to have finished at ``finish``."""
    rep = report(index, start=start, **kwargs)
    rep.update(finished_at=stamp(finish), duration_seconds=int((finish - start).total_seconds()))
    return rep


class FutureReportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.dir = self.tmp / 'reports'
        self.dir.mkdir()
        self.outs = 0

    def put(self, rep):
        (self.dir / (rep['run_id'] + '.json')).write_text(json.dumps(rep))

    def series(self, **stages):
        for index in range(3):
            self.put(report(index, **stages))

    def run_check(self, now):
        """Return (exit code, alert/info ids, flock-alerts-v1 document)."""
        self.outs += 1
        out = self.tmp / f'alerts-{self.outs}.json'
        argv = ['check', '--reports', str(self.dir), '--now', stamp(now), '--json-out', str(out)]
        with contextlib.redirect_stdout(io.StringIO()) as text, contextlib.redirect_stderr(io.StringIO()):
            code = ssc.main(argv)
        lines = text.getvalue().splitlines()
        return code, sorted(line.split(' | ')[1] for line in lines[:-1]), json.loads(out.read_text())

    def future_count(self, doc):
        return [a['count'] for a in doc['info'] if a['id'] == 'report-from-future']

    def test_quiet_history_alerts_no_recent_report(self):  # edge 1, unchanged
        self.series()
        code, ids, doc = self.run_check(QUIET_NOW)
        self.assertEqual((code, ids, doc['info']), (3, ['no-recent-report'], []))

    def test_finished_after_now_never_hides_no_recent_report(self):  # edge 2
        self.series()
        self.put(spanning(3, QUIET_NOW - HOUR, QUIET_NOW + 2 * HOUR))
        code, ids, doc = self.run_check(QUIET_NOW)
        self.assertEqual((code, ids), (3, ['no-recent-report', 'report-from-future']))
        self.assertEqual(self.future_count(doc), [1])
        self.assertEqual([a['id'] for a in doc['alerts']], ['no-recent-report'])

    def test_started_after_now_and_both_kinds_count(self):  # edge 3
        self.series()
        self.put(report(4, start=NOW + HOUR))
        code, ids, doc = self.run_check(NOW)
        self.assertEqual((code, ids, self.future_count(doc)), (0, ['report-from-future'], [1]))
        self.put(spanning(3, NOW - HOUR, NOW + 2 * HOUR))
        code, ids, doc = self.run_check(NOW)
        self.assertEqual((code, ids, self.future_count(doc)), (0, ['report-from-future'], [2]))

    def test_manual_mode_does_not_bypass_future(self):  # wording/mode bypass: still from the future
        self.series()
        self.put(spanning(3, NOW - HOUR, NOW + HOUR, mode='manual'))
        code, ids, doc = self.run_check(NOW)
        self.assertEqual((code, ids, self.future_count(doc)), (0, ['report-from-future'], [1]))

    def test_finished_exactly_at_now_is_kept(self):  # edge 4, boundary is strictly '>'
        self.series()
        self.put(spanning(3, QUIET_NOW - HOUR, QUIET_NOW))
        code, ids, doc = self.run_check(QUIET_NOW)
        self.assertEqual((code, ids, doc['info']), (0, [], []))
        code, ids, doc = self.run_check(QUIET_NOW - dt.timedelta(seconds=1))
        self.assertEqual((code, ids), (3, ['no-recent-report', 'report-from-future']))
        self.assertEqual(self.future_count(doc), [1])

    def test_future_report_never_enters_stage_window(self):  # edge 5
        self.series(extract=('ok', 4, 0))
        self.put(spanning(3, NOW - HOUR, NOW + 2 * HOUR, extract=('ok', 4, 4)))
        code, ids, doc = self.run_check(NOW)
        self.assertEqual((code, ids), (3, ['report-from-future', 'stale:extract']))
        alert = doc['alerts'][0]
        self.assertEqual((alert['kind'], alert['last_produced_run'], alert['first_run'], alert['last_run']),
                         ('stuck', None, report(0)['run_id'], report(2)['run_id']))

    def test_json_document_shape(self):  # edge 6
        self.series(extract=('ok', 4, 0))
        self.put(spanning(3, QUIET_NOW - HOUR, QUIET_NOW + HOUR))
        self.put(report(4, start=QUIET_NOW + HOUR))
        code, ids, doc = self.run_check(QUIET_NOW)
        self.assertEqual(code, 3)
        self.assertEqual((doc['schema'], doc['review_required'], doc['now']), ('flock-alerts-v1', True, stamp(QUIET_NOW)))
        common = dict(source='stage-stale-check', runs_considered=3)
        self.assertEqual(doc['info'], [dict(common, id='report-from-future', stage='-', kind='report-from-future',
                                            last_produced_run=None, count=2)])
        self.assertEqual(doc['alerts'], [
            dict(common, id='no-recent-report', stage='-', kind='no-recent-report', last_produced_run=None),
            dict(common, id='stale:extract', stage='extract', kind='stuck', last_produced_run=None,
                 first_run=report(0)['run_id'], last_run=report(2)['run_id'])])

    def test_unparseable_finish_is_invalid_never_recent(self):  # parse_stamp raising on an 'ok' report
        self.series()
        bad = report(3, start=QUIET_NOW - HOUR)
        bad['finished_at'] = '2026-1-3T9:7:30Z'
        self.put(bad)
        with mock.patch.object(ssc, 'validate_run_report', return_value=True):
            code, ids, doc = self.run_check(QUIET_NOW)
        self.assertEqual((code, ids), (3, ['invalid-report', 'latest-report-invalid', 'no-recent-report']))
        self.assertEqual(self.future_count(doc), [])


if __name__ == '__main__':
    unittest.main()
