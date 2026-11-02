"""BF-df-stale-alert acceptance: tools/stage_stale_check.py over synthetic run reports only."""
import ast
import contextlib
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tools import stage_stale_check as ssc

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/stage_stale_check.py'
BASE = json.loads((ROOT / 'examples/run-report/records-run-report-v1.synthetic.json').read_text())
NOW = '2026-01-02T12:00:00Z'
T0 = dt.datetime(2026, 1, 1, 6, 0, 0, tzinfo=dt.timezone.utc)
BANNED = re.compile(r'\b(late|overdue|violat\w*|unlawful|illegal|non-complian\w*|breach\w*)\b', re.I)


def stamp(value):
    return value.strftime('%Y-%m-%dT%H:%M:%SZ')


def report(index, mode='scheduled', start=None, **stages):
    """Report ``index`` (6 h apart from T0); stages maps name -> (status, input, output[, code])."""
    rep = copy.deepcopy(BASE)
    start = start or T0 + dt.timedelta(hours=6 * index)
    rep.update(mode=mode, started_at=stamp(start), finished_at=stamp(start + dt.timedelta(seconds=450)),
               run_id=f'run_{start.strftime("%Y%m%dT%H%M%SZ")}_{index:08x}', needs_g=[])
    for stage in rep['stages']:
        status, inp, out, *code = stages.get(stage['name'], ('ok', 1, 1))
        stage.update(status=status, error_code=(code or ['STAGE_FAILED'])[0] if status in ('warn', 'fail') else None,
                     counts={'input': inp, 'output': out, 'failed': 0, 'skipped': 0},
                     duration_seconds=None if status == 'skipped' else 1.0)
    statuses = {s['status'] for s in rep['stages']}
    rep['overall_status'] = 'fail' if 'fail' in statuses else 'warn' if 'warn' in statuses else 'ok'
    rep['notify_failed'] = False
    return rep


class StaleCheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.dir = self.tmp / 'reports'
        self.dir.mkdir()

    def put(self, rep, name=None):
        path = self.dir / (name or rep['run_id'] + '.json')
        path.write_text(rep if isinstance(rep, str) else json.dumps(rep))
        return path

    def run_check(self, *extra, now=NOW, reports=None):
        out, err = io.StringIO(), io.StringIO()
        argv = ['check', '--reports', str(reports or self.dir), '--now', now, *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = ssc.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue().splitlines(), err.getvalue()

    def ids(self, lines):
        return sorted(line.split(' | ')[1] for line in lines[:-1])

    def series(self, count=3, start=0, **stages):
        for index in range(start, start + count):
            self.put(report(index, **stages))

    def test_stuck_alerts_and_idle_does_not(self):
        self.series(extract=('ok', 4, 0))
        code, lines, _ = self.run_check()
        self.assertEqual((code, self.ids(lines)), (3, ['stale:extract']))
        self.assertIn('| extract | kind=stuck | runs=3 | last_produced never |', lines[0])
        self.assertTrue(lines[-1].startswith('stale | stages=7 alerts=1 info=0 runs_considered=3 '))
        for path in self.dir.iterdir():
            path.unlink()
        self.series(extract=('ok', 0, 0))
        code, lines, _ = self.run_check()
        self.assertEqual((code, len(lines)), (0, 1))
        self.assertIn('alerts=0 info=0', lines[0])

    def test_produced_or_idle_in_window_breaks_streak(self):
        for first in (('ok', 2, 2), ('ok', 0, 0)):
            with self.subTest(first=first):
                for path in self.dir.iterdir():
                    path.unlink()
                self.put(report(0, extract=first))
                self.series(2, start=1, extract=('ok', 4, 0))
                self.assertEqual(self.run_check()[0], 0)

    def test_last_produced_and_window_ids(self):
        self.put(report(0, portal=('ok', 1, 1)))
        self.series(3, start=1, portal=('warn', 3, 0, 'PORTAL_RATE_LIMITED'))
        code, lines, _ = self.run_check('--json-out', str(self.tmp / 'a.json'))
        alert = json.loads((self.tmp / 'a.json').read_text())['alerts'][0]
        self.assertEqual((code, alert['last_produced_run'], alert['first_run'], alert['last_run']),
                         (3, report(0)['run_id'], report(1)['run_id'], report(3)['run_id']))

    def test_unknown_is_not_calm(self):
        cases = [({'outputs': ('skipped', 0, 0)}, 'outputs', 'skipped'),
                 ({'digest_gates': ('fail', 2, 0)}, 'digest_gates', 'failing'),
                 ({'extract': ('fail', 2, 0, 'RUN_INTERRUPTED')}, 'extract', 'failing')]
        for stages, stage, kind in cases:
            with self.subTest(stage=stage, kind=kind):
                for path in self.dir.iterdir():
                    path.unlink()
                self.series(**stages)
                code, lines, _ = self.run_check()
                self.assertEqual((code, self.ids(lines)), (3, [f'stale:{stage}']))
                self.assertIn(f'kind={kind}', lines[0])

    def test_stub_backend_never_counts_as_produced(self):
        for index in range(3):
            rep = report(index)
            rep['needs_g'] = [{'code': 'STUB_STAGE_BACKEND', 'stage': 'sync', 'severity': 'info', 'count': 1}]
            self.put(rep)
        code, lines, _ = self.run_check()
        self.assertEqual((code, self.ids(lines)), (3, ['stale:sync']))
        self.assertIn('kind=skipped', lines[0])

    def test_kind_tie_prefers_failing_then_stuck(self):
        self.put(report(0, extract=('fail', 1, 0)))
        self.put(report(1, extract=('ok', 1, 0)))
        self.put(report(2, extract=('skipped', 0, 0)))
        self.put(report(3, extract=('ok', 1, 0)))
        self.assertIn('kind=stuck', self.run_check()[1][0])
        self.assertIn('kind=failing', self.run_check('--runs', '2', now=stamp(T0 + dt.timedelta(hours=7)))[1][0])

    def test_insufficient_history_is_never_silent(self):
        self.series(2, extract=('ok', 4, 0))
        code, lines, _ = self.run_check()
        self.assertEqual((code, len(lines)), (0, 2))
        self.assertIn('stale | insufficient-history | - | kind=insufficient-history', lines[0])
        self.assertIn('not all clear', lines[0])

    def test_expired_boundary_and_missing_dir(self):
        self.series()
        last_finish = T0 + dt.timedelta(hours=12, seconds=450)
        self.assertEqual(self.run_check(now=stamp(last_finish + dt.timedelta(hours=36)))[0], 0)
        code, lines, _ = self.run_check(now=stamp(last_finish + dt.timedelta(hours=36, seconds=1)))
        self.assertEqual((code, self.ids(lines)), (3, ['no-recent-report']))
        for where in (self.tmp / 'missing', self.tmp / 'empty'):
            (self.tmp / 'empty').mkdir(exist_ok=True)
            code, lines, _ = self.run_check(reports=where)
            self.assertEqual((code, self.ids(lines)), (3, ['insufficient-history', 'no-recent-report']))

    def test_order_by_started_at_not_name_and_future_excluded(self):
        for index, name in zip(range(4), ('d', 'c', 'b', 'a')):
            self.put(report(index, extract=('ok', 3, 0) if index else ('ok', 1, 1)), name + '.json')
        self.assertEqual(self.ids(self.run_check()[1]), ['stale:extract'])
        self.put(report(9, start=dt.datetime(2026, 1, 3, tzinfo=dt.timezone.utc), extract=('ok', 1, 1)))
        code, lines, _ = self.run_check()
        self.assertEqual((code, self.ids(lines)), (3, ['report-from-future', 'stale:extract']))

    def test_duplicate_run_ids(self):
        self.series(extract=('ok', 4, 0))
        self.put(report(2, extract=('ok', 4, 0)), 'copy.json')
        self.assertEqual(self.ids(self.run_check()[1]), ['stale:extract'])
        changed = report(2, extract=('ok', 4, 1))
        self.put(changed, 'copy.json')
        code, lines, _ = self.run_check()
        self.assertEqual((code, self.ids(lines)), (3, ['duplicate-run-id', 'insufficient-history']))

    def test_manual_and_test_reports_neither_create_nor_clear(self):
        self.series(extract=('ok', 4, 0))
        self.put(report(3, mode='manual', extract=('ok', 4, 4)))
        self.put(report(4, mode='test', extract=('ok', 4, 4)))
        code, lines, _ = self.run_check()
        self.assertEqual((code, self.ids(lines)), (3, ['stale:extract']))
        self.assertIn('excluded_manual=1 excluded_test=1 invalid=0', lines[-1])
        for path in self.dir.iterdir():
            path.unlink()
        self.series(mode='manual', extract=('ok', 4, 0))
        self.assertEqual(self.ids(self.run_check()[1]), ['insufficient-history', 'no-recent-report'])

    def test_invalid_and_unparseable_newest(self):
        self.series(extract=('ok', 4, 0))
        bad = report(3, extract=('ok', 4, 4))
        bad['overall_status'] = 'fail'
        self.put(bad)
        code, lines, _ = self.run_check()
        self.assertEqual((code, self.ids(lines)), (3, ['invalid-report', 'latest-report-invalid', 'stale:extract']))
        self.assertIn('invalid=1', lines[-1])
        (self.dir / bad['run_id']).with_suffix('.json').unlink()
        self.put('{not json', 'zz.json')
        code, lines, _ = self.run_check()
        self.assertEqual((code, self.ids(lines)), (3, ['stale:extract', 'unparseable-report']))

    def test_threshold_and_wording_bypass(self):
        self.series(extract=('ok', 4, 0))
        for bad in ('1', '0', '21', 'x', '2.5', '-3'):
            with self.subTest(runs=bad):
                self.assertEqual(self.run_check('--runs', bad)[0], 2)
        for bad in ('2026-01-02 12:00:00', '2026-01-02T12:00:00+00:00', 'now'):
            self.assertEqual(self.run_check(now=bad)[0], 2)
        self.assertEqual(self.run_check('--max-silent-hours', '99')[0], 2)
        for path in self.dir.iterdir():
            path.unlink()
        for index in range(3):
            rep = report(index, extract=('warn', 4, 0, 'ALL_CLEAR'))
            rep['needs_g'] = [{'code': 'RESOLVED', 'stage': 'extract', 'severity': 'info', 'count': 1}]
            self.put(rep)
        self.assertEqual(self.ids(self.run_check()[1]), ['stale:extract'])

    def test_untrusted_reports_dir(self):
        self.series()
        (self.dir / 'link.json').symlink_to(self.dir / f'{report(0)["run_id"]}.json')
        self.assertEqual(self.run_check()[0], 1)
        (self.dir / 'link.json').unlink()
        (self.dir / 'big.json').write_text(' ' * (ssc.MAX_BYTES + 1))
        code, lines, err = self.run_check()
        self.assertEqual((code, lines), (1, []))
        self.assertNotIn(str(self.tmp), err)

    def test_json_out_refusals_and_mode(self):
        self.series(extract=('ok', 4, 0))
        (self.tmp / 'exists.json').write_text('x')
        (self.tmp / 'real').mkdir()
        (self.tmp / 'link').symlink_to(self.tmp / 'real')
        for target in (self.tmp / 'exists.json', ROOT / 'stale-out.json', self.tmp / 'link' / 'o.json',
                       self.tmp / 'nodir' / 'o.json', 'relative.json'):
            with self.subTest(target=str(target)):
                self.assertEqual(self.run_check('--json-out', str(target))[0], 1)
        self.assertFalse(os.path.lexists(ROOT / 'stale-out.json'))
        self.assertEqual(sorted(p.name for p in (self.tmp / 'real').iterdir()), [])
        out = self.tmp / 'alerts.json'
        self.assertEqual(self.run_check('--json-out', str(out))[0], 3)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        doc = json.loads(out.read_text())
        self.assertEqual((doc['schema'], doc['review_required'], doc['now']), ('flock-alerts-v1', True, NOW))
        self.assertEqual([a['id'] for a in doc['alerts']], ['stale:extract'])
        self.assertEqual(doc['alerts'][0]['source'], 'stage-stale-check')

    def test_json_out_failure_mid_write_leaves_nothing(self):
        self.series(extract=('ok', 4, 0))
        out = self.tmp / 'alerts.json'

        class Broken:
            def __init__(self, fd, *args, **kwargs):
                self.fd = fd

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                os.close(self.fd)

            def write(self, data):
                os.write(self.fd, data[:10])
                raise OSError('disk full')
        with mock.patch('os.fdopen', Broken):
            code, lines, _ = self.run_check('--json-out', str(out))
        self.assertEqual((code, lines, os.path.lexists(out)), (1, [], False))

    def test_output_hygiene_and_no_clock(self):
        self.series(extract=('ok', 4, 0), outputs=('fail', 1, 0))
        self.put('[]', 'odd.json')

        class NoClock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                raise AssertionError('clock read')
            utcnow = now
        with mock.patch.object(time, 'time', side_effect=AssertionError('clock read')), \
                mock.patch.object(ssc, 'datetime', NoClock):
            code, lines, _ = self.run_check()
        self.assertEqual(code, 3)
        for line in lines:
            self.assertFalse(BANNED.search(line), line)
            self.assertNotIn('@', line)
            self.assertNotIn(str(self.tmp), line)
            self.assertTrue(line.startswith('stale | '), line)
        failing = next(line for line in lines if 'kind=failing' in line)
        self.assertIn('records status', failing)
        self.assertIn('operator', failing)
        banned = {'smtplib', 'imaplib', 'socket', 'ssl', 'http', 'urllib', 'requests', 'subprocess', 'sqlite3'}
        for node in ast.walk(ast.parse(SCRIPT.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ''] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                self.assertNotIn(name.split('.')[0], banned)
        for text in ssc.ACTIONS.values():  # action texts: substring ban, stricter than whole lines
            self.assertFalse(re.search(r'late|overdue|violat|unlawful|illegal|non-complian|breach', text, re.I), text)

    def test_help_runs(self):
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('check', result.stdout)


if __name__ == '__main__':
    unittest.main()
