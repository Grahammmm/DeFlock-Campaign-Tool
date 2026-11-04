"""Synthetic tests for `flock_review_queue.py show --agency SLUG --run RUN` (RF0510-13): one run of one agency only."""
import json
import os
import re
import shutil
from unittest import mock
from tests.flock import test_publish as base
from tests.flock import test_review_queue as rq

queue = rq.queue
REFUSED = 'queue refused: invalid input or output\n'
FIRST, SECOND = '2026-10-30', '2026-10-31'


class ReviewQueueRunTest(rq.ReviewQueueTest):
    def setUp(self):
        super().setUp()
        self.second = self.run_dir.parent / SECOND  # edge path 1: a second real run, built the way the fixture run is
        self.assertEqual(base.run('flock_compare.py', 'scaffold', '--store', str(self.store), '--agency', base.AGENCY,
                                  '--date', SECOND, '--provider', 'anthropic', '--model', 'claude-opus-5-5',
                                  '--applicability', 'Sample city police')[0], 0)
        (self.second / 'FINDINGS.csv').write_text(self.fixture_text('findings-sample.csv'))
        self.assertEqual(base.run('flock_verify.py', 'start', str(self.second), '--provider', 'openai',
                                  '--model', 'gpt-5-codex')[0], 0)
        (self.second / 'VERIFY.csv').write_text(self.fixture_text('verify-sample.csv'))
        self.assertEqual(base.run('flock_verify.py', 'finish', str(self.second))[0], 0)
        self.assertEqual(base.run('flock_publish.py', 'export', str(self.second), '--site-slug', 'sample')[0], 0)

    def run_show(self, run, out=None, *extra):
        code, stdout = self.show(out, '--agency', base.AGENCY, '--run', run, *extra)
        self.hygiene(stdout)
        return code, stdout

    def hygiene(self, stdout):  # edge path 7: no PUBLISH text, path or reviewer name on stdout
        for entry in self.doc['findings']:
            self.assertNotIn(entry['text'], stdout)
        for planted in (str(self.tmp), 'PUBLISH.json', 'PLANTAPPROVER', 'anthropic', 'openai'):
            self.assertNotIn(planted, stdout)

    def headings(self, out=None):
        return re.findall(r'^## finding (\S+) · \S+ · run (\S+)$', self.report(out), re.M)

    def drift(self, run_dir):
        doc = json.loads((run_dir / 'PUBLISH.json').read_text())
        doc['findings'][0]['text'] = 'Altered text.'
        (run_dir / 'PUBLISH.json').write_text(json.dumps(doc))

    def test_one_run_listed_and_named_in_header_and_summary(self):
        code, stdout = self.show()
        self.assertEqual(code, 0, stdout)
        self.assertEqual({run for _, run in self.headings()}, {FIRST, SECOND})
        self.assertIn('queue | waiting=8 ', stdout)
        for chosen in (FIRST, SECOND):
            with self.subTest(run=chosen):
                out = self.tmp / ('only-' + chosen)
                code, stdout = self.run_show(chosen, out)
                self.assertEqual(code, 0, stdout)
                self.assertEqual({run for _, run in self.headings(out)}, {chosen})
                self.assertEqual(len(self.headings(out)), 4)
                rows = stdout.splitlines()
                self.assertEqual({row.split(' | ')[2] for row in rows[:-1]}, {chosen})
                self.assertTrue(rows[-1].startswith('queue | run=%s waiting=4 needs_review=0 ' % chosen), rows[-1])
                self.assertTrue(rows[-1].endswith(' | review-required; only a human approves'))
                header = self.report(out).splitlines()[0]
                self.assertRegex(header, r'^Private review queue\. Built \d{4}-\d{2}-\d{2}; run %s\. Approving is a human '
                                         r'decision recorded in PUBLISH\.json; this file changes nothing\.$' % chosen)

    def test_other_run_not_reviewable_does_not_fail_the_chosen_run(self):
        self.drift(self.second)
        code, stdout = self.show()
        self.assertEqual(code, 1, stdout)  # today, without --run, the drifted older/newer run fails the whole queue
        self.assertIn('not_reviewable=1', stdout)
        code, stdout = self.run_show(FIRST, self.tmp / 'good')
        self.assertEqual(code, 0, stdout)
        self.assertIn('not_reviewable=0', stdout)
        self.assertNotIn('NOT REVIEWABLE', self.report(self.tmp / 'good'))

    def test_chosen_run_drifted_is_not_reviewable(self):
        self.drift(self.second)
        code, stdout = self.run_show(SECOND)
        self.assertEqual(code, 1, stdout)
        self.assertEqual(stdout.splitlines(), ['queue | %s | %s | not-reviewable | drifted' % (base.AGENCY, SECOND),
                                               'queue | run=%s waiting=0 needs_review=0 not_reviewable=1 warnings=0 | '
                                               'review-required; only a human approves' % SECOND])
        self.assertIn('## NOT REVIEWABLE: drifted', self.report())
        self.assertNotIn('## finding ', self.report())

    def test_without_run_output_unchanged(self):
        shutil.rmtree(self.second)  # one run: --run must differ from no --run only at the two named places
        plain, chosen = self.tmp / 'plain', self.tmp / 'chosen'
        code, stdout = self.call(plain, '--json')
        text, rows, failed, data = queue.build(self.store, None)
        self.assertEqual((code, stdout), (int(failed), '\n'.join(rows) + '\n'))
        self.assertEqual((plain / 'review-queue.md').read_bytes(), text.encode('utf-8'))
        self.assertRegex(text.splitlines()[0], r'^Private review queue\. Built \d{4}-\d{2}-\d{2}\. Approving is a human')
        self.assertRegex(stdout.splitlines()[-1], r'^queue \| waiting=4 needs_review=0 not_reviewable=0 warnings=0 \| '
                                                  r'review-required; only a human approves$')
        self.assertNotIn('run', json.loads((plain / 'review-queue.json').read_text()))
        code2, stdout2 = self.run_show(FIRST, chosen, '--json')
        self.assertEqual(code2, code)
        self.assertEqual(stdout2.replace('queue | run=%s ' % FIRST, 'queue | '), stdout)
        self.assertEqual((chosen / 'review-queue.md').read_text().replace('; run ' + FIRST, '', 1),
                         (plain / 'review-queue.md').read_text())
        data = json.loads((chosen / 'review-queue.json').read_text())
        self.assertEqual((data['run'], data['agency_filter']), (FIRST, base.AGENCY))
        self.assertEqual({w['run'] for w in data['waiting']}, {FIRST})

    def test_malformed_or_unscoped_run_refused_before_output(self):
        cases = [('--run', FIRST), ('--agency', base.AGENCY, '--run', '../x'), ('--agency', base.AGENCY, '--run', 'a b'),
                 ('--agency', base.AGENCY, '--run', ''), ('--agency', base.AGENCY, '--run', 'x' * 65),
                 ('--agency', base.AGENCY, '--run', '?'), ('--agency', base.AGENCY, '--run', FIRST + '\n'),
                 ('--agency', 'Bad Slug', '--run', FIRST)]
        for n, extra in enumerate(cases):
            out = self.tmp / ('malformed%d' % n)
            with self.subTest(extra=extra):
                self.assertEqual(self.show(out, *extra), (1, REFUSED))
                self.assertFalse(os.path.lexists(out))

    def test_absent_or_linked_run_refused_before_output(self):
        runs = self.run_dir.parent
        (runs / '2026-10-29').symlink_to(self.run_dir, target_is_directory=True)
        (runs / '2026-10-28').write_text('not a folder')
        linked_agency = self.store / 'agencies' / 'zz-agency'
        linked_agency.symlink_to(self.store / 'agencies' / base.AGENCY, target_is_directory=True)
        linked_runs = self.store / 'agencies' / 'yy-agency'
        linked_runs.mkdir()
        (linked_runs / 'runs').symlink_to(runs, target_is_directory=True)
        cases = [(base.AGENCY, '2099-01-01'), (base.AGENCY, '2026-10-29'), (base.AGENCY, '2026-10-28'),
                 ('zz-agency', FIRST), ('yy-agency', FIRST), ('other-agency', FIRST)]
        for n, (agency, run) in enumerate(cases):
            out = self.tmp / ('absent%d' % n)
            with self.subTest(agency=agency, run=run):
                self.assertEqual(self.show(out, '--agency', agency, '--run', run), (1, REFUSED))
                self.assertFalse(os.path.lexists(out))
        code, stdout = self.run_show(FIRST)  # the real run beside those links still works
        self.assertEqual(code, 0, stdout)

    def test_reviewer_name_and_claim_text_stay_off_stdout(self):
        self.doc['findings'][0]['review'] = dict(status='needs-review', approved_by='PLANTAPPROVER Person',
                                                 approved_on='2026-10-30', approval_ref='PLANTREF-9')
        self.save()
        with mock.patch.object(queue.publish, 'stage', return_value=('pending', None)):
            code, stdout = self.call(None, '--agency', base.AGENCY, '--run', FIRST)
        self.assertEqual(code, 0, stdout)
        self.hygiene(stdout)
        self.assertIn('PLANTAPPROVER', self.report())  # the private report shows it, so it was really planted
        self.assertNotIn('PLANTREF', stdout)


for _name in [n for n in dir(rq.ReviewQueueTest) if n.startswith('test_')]:
    setattr(ReviewQueueRunTest, _name, None)
