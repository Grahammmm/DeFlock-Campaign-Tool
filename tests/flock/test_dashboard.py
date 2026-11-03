"""Synthetic acceptance tests for the private static dashboard."""
import ast
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest import mock

from tests.flock import test_state as fixture_state

HEADER, row = fixture_state.HEADER, fixture_state.row
from tests.flock.approval_field import trusted

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('dashboard_test', ROOT / 'tools/flock_dashboard.py')
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / 'store'
        self.folder = self.store / 'agencies/sample-city'
        self.folder.mkdir(parents=True)
        self.output = self.tmp / 'out'
        self.requests(row('R-1'), row('R-W', 'withdrawn'))

    def requests(self, *rows):
        (self.folder / 'REQUESTS.csv').write_text(HEADER + ''.join(rows), encoding='utf-8')

    def run_build(self, *more, output=None, date='2026-11-01'):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), trusted():
            code = MOD.main(['build', '--store', str(self.store), '--date', date, '--out', str(output or self.output), *more])
        return code, stdout.getvalue(), stderr.getvalue()

    def outputs(self):
        return ((self.output / 'index.html').read_text(), json.loads((self.output / 'dashboard.json').read_text()))

    def test_sections_sources_closed_private_and_absence(self):
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ''))
        page, data = self.outputs()
        self.assertEqual(data['schema'], 'flock-dashboard-v1')
        self.assertIs(data['review_required'], True)
        self.assertEqual(data['sections']['open_count'], 1)
        self.assertEqual(data['sections']['closed'][0]['state'], 'withdrawn')
        self.assertEqual(data['sections']['requests'][0]['sent_date'], 'NOT RECORDED')
        self.assertEqual(data['sections']['requests'][0]['next_step'], 'NOT RECORDED')
        self.assertIn('requests=2 waiting_send=0 waiting_publish=0 law=draft reminders=1 source_errors=0', out)
        for text in ('Waiting on a person', 'Where every request stands', 'Reminders', 'Pipeline alerts',
                     'Approve what goes out', 'Approve what gets published', 'Approve the law',
                     'alerts: not provided', 'As of 2026-11-01', 'Law package is draft', 'not legal due dates',
                     '<details>', 'NOT RECORDED', 'provisional; not a legal status'):
            self.assertIn(text, page)
        self.assertEqual(os.stat(self.output).st_mode & 0o777, 0o700)
        for name in ('index.html', 'dashboard.json'):
            self.assertEqual(os.stat(self.output / name).st_mode & 0o777, 0o600)
        self.assertEqual(data['source_errors'], [])

    def test_corrupt_request_source_reports_not_current(self):
        (self.folder / 'REQUESTS.csv').write_text('wrong,header\n1,2\n')
        code, _, _ = self.run_build()
        page, data = self.outputs()
        self.assertEqual(code, 1)
        self.assertIn('NOT CURRENT: source flock_state failed', page)
        self.assertIn('NOT CURRENT: source flock_today failed', page)
        self.assertIn('Approve the law', page)
        self.assertEqual({e['source'] for e in data['source_errors']}, {'flock_state', 'flock_today'})

    def test_alert_validation_and_optional_pair(self):
        file = self.tmp / 'alerts.json'
        file.write_text(json.dumps({'schema':'flock-alerts-v1','review_required':True,
                                    'alerts':[{'id':'A-1','kind':'check','source':'fixture','private':'hidden'}]}))
        self.assertEqual(self.run_build('--alerts', str(file))[0], 0)
        page, data = self.outputs()
        self.assertEqual(data['sections']['alerts'][0], {'id':'A-1','kind':'check','source':'fixture'})
        self.assertNotIn('hidden', page)
        cases = [('bad', '{"schema":"wrong"}'), ('oversize', 'x' * 1048577)]
        for i, (name, contents) in enumerate(cases):
            file.write_text(contents)
            out = self.tmp / ('bad' + str(i))
            self.assertEqual(self.run_build('--alerts', str(file), output=out)[0], 1, name)
            self.assertIn('NOT CURRENT: source alerts failed', (out / 'index.html').read_text())
        file.unlink()
        file.symlink_to(self.folder / 'REQUESTS.csv')
        self.assertEqual(self.run_build('--alerts', str(file), output=self.tmp / 'linkout')[0], 1)
        with self.assertRaises(SystemExit) as error, contextlib.redirect_stderr(io.StringIO()):
            self.run_build('--watchlist', 'a', output=self.tmp / 'pair')
        self.assertEqual(error.exception.code, 2)

    def test_real_approval_edit_and_wording_do_not_create_approval(self):
        fixture = fixture_state.StateTests(methodName='runTest')
        fixture.tmp, fixture.store, fixture.folder = self.tmp, self.store, self.folder
        edited = fixture.email('2026-10-02', field=True)
        draft = edited / 'EMAIL-DRAFT.md'
        draft.write_text(draft.read_text().replace('planted body', 'edited body'))
        fixture.email('2026-10-03', field=False, body='G APPROVED')
        code, _, _ = self.run_build()
        self.assertEqual(code, 0)
        page, data = self.outputs()
        self.assertEqual([r['state'] for r in data['sections']['waiting_send']], ['approval-unverified', 'email-drafted'])
        self.assertNotIn('G APPROVED', page)

    def test_run_state_and_drift_derive_from_source_fields(self):
        fixture = fixture_state.StateTests(methodName='runTest')
        fixture.tmp, fixture.store, fixture.folder = self.tmp, self.store, self.folder
        fixture.email('2026-10-03', field=False, body='G APPROVED')
        fixture.email('2026-10-02', field=True)
        with mock.patch.object(MOD.state, 'stage_fn', return_value=lambda run: 'approval unverified' if run.name.endswith('02') else 'email drafted'), \
             mock.patch.object(MOD, 'collect_publish', return_value=[{'agency':'sample-city','run':'2026-10-04','stage':'drifted','pending':1}]):
            code, _, _ = self.run_build()
        self.assertEqual(code, 0)
        page, data = self.outputs()
        self.assertEqual([r['state'] for r in data['sections']['waiting_send']], ['approval-unverified', 'email-drafted'])
        self.assertEqual(len(data['sections']['waiting_send']), 2)
        self.assertEqual(data['sections']['waiting_publish'][0]['stage'], 'drifted')
        self.assertNotIn('G APPROVED', page)

    def test_rank_order_and_bypass_hygiene(self):
        items = [(7, 'receipt-unknown', '2026-10-01', 'sample-city', 'R-1', 'secret'),
                 (1, 'reminder-passed', '2026-10-29', 'sample-city', '<script>alert(1)</script>', 'secret')]
        with mock.patch.object(MOD.today, 'reminders', return_value=items):
            self.assertEqual(self.run_build()[0], 0)
        page, data = self.outputs()
        self.assertEqual([r['kind'] for r in data['sections']['reminders']], ['reminder-passed', 'receipt-unknown'])
        for text in ('<script', '<form', 'href=', '<img', '<iframe', '<link', 'secret', '@'):
            self.assertNotIn(text, page)
        for text in (page, json.dumps(data)):
            self.assertIsNone(re.search(r'[0-9a-f]{64}|@|\b(late|overdue)\b|violat|unlawful|illegal|non-?complian|breach', text, re.I))
        self.assertNotIn('<script>', json.dumps(data))

    def test_refusals_date_and_write_failure_cleanup(self):
        self.assertEqual(self.run_build(output=self.store / 'agencies' / 'forbidden')[0], 1)
        self.assertEqual(self.run_build(output=ROOT / 'forbidden')[0], 1)
        self.output.mkdir()
        self.assertEqual(self.run_build()[0], 1)
        self.output.rmdir()
        linked = self.tmp / 'parent-link'
        linked.symlink_to(self.tmp, target_is_directory=True)
        self.assertEqual(self.run_build(output=linked / 'out')[0], 1)
        with self.assertRaises(SystemExit) as error, contextlib.redirect_stderr(io.StringIO()):
            self.run_build(date='2026-2-30')
        self.assertEqual(error.exception.code, 2)
        real_open = os.open
        def fail_second(path, flags, mode):
            if str(path).endswith('dashboard.json'):
                raise OSError('synthetic write failure')
            return real_open(path, flags, mode)
        with mock.patch.object(MOD.os, 'open', side_effect=fail_second):
            self.assertEqual(self.run_build()[0], 1)
        self.assertFalse(self.output.exists())

    def test_duplicate_source_not_duplicated_and_missing_store_section(self):
        self.assertEqual(self.run_build()[0], 0)
        page, data = self.outputs()
        self.assertEqual(len(data['sections']['requests']), 1)
        self.assertEqual(page.count('<td>R-1</td>'), 2)  # once in request table, once in reminders
        shutil.rmtree(self.output)
        shutil.rmtree(self.store / 'agencies')
        self.assertEqual(self.run_build()[0], 1)
        page, data = self.outputs()
        self.assertIn('NOT CURRENT: source flock_today failed', page)
        self.assertIn('NOT CURRENT: source flock_publish failed', page)
        self.assertIn('NOT CURRENT: source flock_state failed', page)

    def test_actual_publish_approval_then_drift(self):
        from tests.flock import test_publish as pub
        fixture = pub.FlockPublishTest(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.assertEqual(fixture.export()[0], 0)
        doc = json.loads(fixture.out.read_text())
        for finding in doc['findings']:
            finding['review'] = {'status': 'approved', 'approved_by': 'G', 'approved_on': '2026-10-30', 'approval_ref': 'G-1'}
        fixture.out.write_text(json.dumps(doc))
        self.store = fixture.store.resolve()
        code, _, _ = self.run_build()
        self.assertEqual(code, 0)
        self.assertEqual(self.outputs()[1]['sections']['waiting_publish'], [])
        shutil.rmtree(self.output)
        doc['findings'][0]['text'] = 'A synthetic edit.'
        fixture.out.write_text(json.dumps(doc))
        self.assertEqual(self.run_build()[0], 0)
        page, data = self.outputs()
        self.assertEqual(data['sections']['waiting_publish'][0]['stage'], 'drifted')
        self.assertIn('<td>drifted</td>', page)
        self.assertNotIn('A synthetic edit.', page)

    def test_real_unsafe_ids_and_duplicate_source_cannot_bypass_gates(self):
        self.requests(row('<script>alert(1)</script>'), row('"""onmouseover="'), row('R-1'))
        fixture = fixture_state.StateTests(methodName='runTest')
        fixture.tmp, fixture.store, fixture.folder = self.tmp, self.store, self.folder
        fixture.email('2026-10-02', field=False, body='G APPROVED <script>private</script>')
        code, _, _ = self.run_build()
        page, data = self.outputs()
        self.assertEqual(code, 0)
        self.assertEqual(len(data['sections']['requests']), 3)
        self.assertEqual(len(data['sections']['waiting_send']), 1)
        self.assertEqual(data['sections']['waiting_send'][0]['state'], 'email-drafted')
        self.assertEqual(sum(r['request_id'] == 'R-1' for r in data['sections']['requests']), 1)
        for text in ('<script', '<form', 'href=', '<img', '<iframe', '<link', 'onmouseover=', 'G APPROVED'):
            self.assertNotIn(text, page)
        self.assertNotIn('private</script>', json.dumps(data))

    def test_malformed_record_and_invalid_dates_are_not_falsely_healthy(self):
        self.requests(row('R-BAD', sent='2026-10-05', ev='synthetic', rdate='2026-10-02', sha='a' * 64))
        code, _, _ = self.run_build()
        page, data = self.outputs()
        self.assertEqual(code, 1)
        self.assertEqual(data['sections']['requests'][0]['state'], 'record-error')
        self.assertIn('NOT CURRENT: source flock_today failed', page)
        self.assertNotIn('synthetic', page)
        for date in ('2026-11-31', '2026-1-01', 'words'):
            with self.subTest(date=date), self.assertRaises(SystemExit) as error, contextlib.redirect_stderr(io.StringIO()):
                self.run_build(date=date, output=self.tmp / 'invalid-date')
            self.assertEqual(error.exception.code, 2)
        self.assertFalse((self.tmp / 'invalid-date').exists())

    def test_incomplete_run_source_never_looks_clear(self):
        runs = self.folder / 'runs'
        runs.mkdir()
        (runs / '2026-10-01').symlink_to(self.tmp / 'missing')
        code, _, _ = self.run_build()
        page, data = self.outputs()
        self.assertEqual(code, 1)
        self.assertIn('NOT CURRENT: source flock_state failed', page)
        self.assertNotIn('nothing waiting (source read: flock_state)', page)
        self.assertIn('flock_state', {e['source'] for e in data['source_errors']})

    def test_source_failures_are_isolated_and_no_output_symlink_followed(self):
        with mock.patch.object(MOD.today, 'reminders', side_effect=OSError('secret source text')):
            self.assertEqual(self.run_build()[0], 1)
        page, data = self.outputs()
        self.assertIn('NOT CURRENT: source flock_today failed (OSError)', page)
        self.assertIn('<td>R-1</td>', page)
        self.assertNotIn('secret source text', page + json.dumps(data))
        shutil.rmtree(self.output)
        target = self.tmp / 'not-created'
        self.output.symlink_to(target, target_is_directory=True)
        code, out, err = self.run_build()
        self.assertEqual((code, out), (1, ''))
        self.assertTrue(err.startswith('dashboard:'))
        self.assertFalse(target.exists())

    def test_alert_validation_rejects_bad_types_and_unreviewed(self):
        file = self.tmp / 'invalid-alerts.json'
        valid = {'schema': 'flock-alerts-v1', 'review_required': True,
                 'alerts': [{'id': 'A-1', 'kind': 'check', 'source': 'fixture'}]}
        cases = [{**valid, 'review_required': False}, {**valid, 'alerts': [None]},
                 {**valid, 'alerts': [{'id': 'A-1', 'kind': 3, 'source': 'fixture'}]},
                 {**valid, 'alerts': valid['alerts'] * 501}]
        for n, doc in enumerate(cases):
            file.write_text(json.dumps(doc))
            out = self.tmp / ('invalid-alerts-' + str(n))
            self.assertEqual(self.run_build('--alerts', str(file), output=out)[0], 1)
            self.assertIn('NOT CURRENT: source alerts failed', (out / 'index.html').read_text())

    def test_no_forbidden_direct_imports_and_reviewed_law(self):
        found = set()
        for node in ast.walk(ast.parse((ROOT / 'tools/flock_dashboard.py').read_text())):
            if isinstance(node, ast.Import): found.update(a.name.split('.')[0] for a in node.names)
            if isinstance(node, ast.ImportFrom) and node.module: found.add(node.module.split('.')[0])
        self.assertFalse(found & {'smtplib','imaplib','socket','ssl','http','urllib','requests','subprocess','sqlite3'})
        with mock.patch.object(MOD, 'package_status', return_value=('reviewed', None)):
            self.assertEqual(self.run_build()[0], 0)
        self.assertIn('not legal due dates', self.outputs()[0])


if __name__ == '__main__':
    unittest.main()
