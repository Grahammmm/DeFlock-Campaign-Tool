"""Synthetic tests for RF0720-10: `flock_review_queue.py show` names every run it could not review."""
import json
import os
import re
import shutil
from tests.flock import test_publish as base
from tests.flock import test_review_queue as rq

queue = rq.queue
A, B = base.AGENCY, 'zz-agency'  # agency-a (has the real run) and agency-b (sorts after it)
DRIFTED = '2026-10-01'  # sorts before the fixture run 2026-10-30
TAIL = ' — fix with flock_publish recheck before approving'
REFUSED = 'queue refused: invalid input or output\n'


class ReviewQueueBlockedTest(rq.ReviewQueueTest):
    def drifted_run(self, name=DRIFTED):
        target = self.run_dir.parent / name
        shutil.copytree(self.run_dir, target)
        doc = json.loads((target / 'PUBLISH.json').read_text())
        doc['findings'][0]['text'] = 'Altered text.'
        (target / 'PUBLISH.json').write_text(json.dumps(doc))
        return target

    def linked_agency(self):
        other = self.store / 'agencies' / B
        other.mkdir()
        (other / 'runs').symlink_to(self.run_dir.parent, target_is_directory=True)

    def headings(self, out=None):
        return [line for line in self.report(out).splitlines() if line.startswith('## NOT REVIEWABLE')]

    def finding_rows(self, stdout):
        return [r for r in stdout.splitlines()[:-1] if ' | verdict=' in r]

    def test_drifted_run_named_in_store_order_before_totals(self):
        self.drifted_run()
        code, stdout = self.show()
        self.assertEqual(code, 1, stdout)
        rows = stdout.splitlines()
        self.assertEqual(rows[0], 'queue | %s | %s | not-reviewable | drifted' % (A, DRIFTED))
        self.assertEqual(len(self.finding_rows(stdout)), 4)
        self.assertEqual(rows[1:-1], self.finding_rows(stdout))  # waiting finding rows of the good run follow
        self.assertTrue(all(r.startswith('queue | %s | 2026-10-30 | ' % A) for r in rows[1:-1]))
        self.assertTrue(rows[-1].startswith('queue | waiting=4 needs_review=0 not_reviewable=1 warnings=0 | '), rows[-1])
        self.assertEqual(self.headings(), ['## NOT REVIEWABLE: drifted · %s · run %s%s' % (A, DRIFTED, TAIL)])
        text = self.report()
        self.assertLess(text.index('## NOT REVIEWABLE'), text.index('## finding '))

    def test_linked_runs_folder_named_at_agency_level(self):
        self.linked_agency()
        code, stdout = self.show()
        self.assertEqual(code, 1, stdout)
        rows = stdout.splitlines()
        self.assertEqual(rows[-2], 'queue | %s | - | not-reviewable | link-refused' % B)
        self.assertIn('not_reviewable=1 ', rows[-1])
        self.assertEqual(self.headings(), ['## NOT REVIEWABLE: link-refused · %s%s' % (B, TAIL)])
        self.assertNotIn(' · run', self.headings()[0])
        # an agency folder that is itself a link is named the same way
        (self.store / 'agencies' / B / 'runs').unlink()
        (self.store / 'agencies' / B).rmdir()
        (self.store / 'agencies' / B).symlink_to(self.store / 'agencies' / A, target_is_directory=True)
        code, stdout = self.show(self.tmp / 'agency-link')
        self.assertEqual(code, 1, stdout)
        self.assertIn('queue | %s | - | not-reviewable | link-refused' % B, stdout.splitlines())

    def test_linked_run_and_linked_publish_named(self):
        runs = self.run_dir.parent
        (runs / '2026-10-29').symlink_to(self.run_dir, target_is_directory=True)
        real = runs / '2026-11-02'
        real.mkdir()
        (real / 'PUBLISH.json').symlink_to(self.out)
        code, stdout = self.show()
        self.assertEqual(code, 1, stdout)
        blocked = [r for r in stdout.splitlines() if ' | not-reviewable | ' in r]
        self.assertEqual(blocked, ['queue | %s | 2026-10-29 | not-reviewable | link-refused' % A,
                                   'queue | %s | 2026-11-02 | not-reviewable | link-refused' % A])
        self.assertIn('not_reviewable=2 ', stdout.splitlines()[-1])
        self.assertEqual(self.headings(), ['## NOT REVIEWABLE: link-refused · %s · run %s%s' % (A, run, TAIL)
                                           for run in ('2026-10-29', '2026-11-02')])

    def test_unsafe_run_names_print_as_question_mark(self):
        runs = self.run_dir.parent
        dotted, long_name = 'run.1', 'r' * 70
        for name in (dotted, long_name):
            (runs / name).symlink_to(self.run_dir, target_is_directory=True)
        self.drifted_run('2026-x.y')
        code, stdout = self.call(None, '--json')
        self.assertEqual(code, 1, stdout)
        blocked = [r for r in stdout.splitlines() if ' | not-reviewable | ' in r]
        self.assertEqual(sorted(blocked), sorted(['queue | %s | ? | not-reviewable | drifted' % A] +
                                                 ['queue | %s | ? | not-reviewable | link-refused' % A] * 2))
        text = self.report()
        self.assertEqual(sorted(self.headings()), sorted(['## NOT REVIEWABLE: drifted · %s · run ?%s' % (A, TAIL)] +
                                                         ['## NOT REVIEWABLE: link-refused · %s · run ?%s' % (A, TAIL)] * 2))
        raw_json = (self.result / 'review-queue.json').read_text()
        for raw in (dotted, long_name, '2026-x.y'):
            self.assertNotIn(raw, stdout)
            self.assertNotIn(raw, text)
            self.assertNotIn(raw, raw_json)
        self.assertEqual({e['run'] for e in json.loads(raw_json)['not_reviewable']}, {'?'})

    def test_agency_filter_names_only_that_agency(self):
        self.drifted_run()
        self.linked_agency()
        code, stdout = self.show(None, '--agency', A)
        self.assertEqual(code, 1, stdout)
        self.assertNotIn(B, stdout)
        self.assertEqual([r for r in stdout.splitlines() if ' | not-reviewable | ' in r],
                         ['queue | %s | %s | not-reviewable | drifted' % (A, DRIFTED)])
        self.assertIn('not_reviewable=1 ', stdout.splitlines()[-1])
        self.assertNotIn(B, self.report())
        code, stdout = self.show(self.tmp / 'only-b', '--agency', B)
        self.assertEqual(code, 1, stdout)
        self.assertEqual(stdout.splitlines()[:-1], ['queue | %s | - | not-reviewable | link-refused' % B])
        self.assertIn('not_reviewable=1 ', stdout.splitlines()[-1])
        self.assertNotIn(DRIFTED, self.report(self.tmp / 'only-b'))

    def test_run_scope_names_only_the_chosen_run(self):
        self.drifted_run()
        code, stdout = self.show(None, '--agency', A, '--run', DRIFTED)
        self.assertEqual(code, 1, stdout)
        self.assertEqual(stdout.splitlines(), [
            'queue | %s | %s | not-reviewable | drifted' % (A, DRIFTED),
            'queue | run=%s waiting=0 needs_review=0 not_reviewable=1 warnings=0 | review-required; only a human approves'
            % DRIFTED])
        code, stdout = self.show(self.tmp / 'good', '--agency', A, '--run', '2026-10-30')
        self.assertEqual(code, 0, stdout)
        self.assertNotIn('not-reviewable', stdout)
        self.assertEqual(self.headings(self.tmp / 'good'), [])

    def test_clean_store_has_only_finding_rows_and_totals(self):
        code, stdout = self.show()
        self.assertEqual(code, 0, stdout)
        rows = stdout.splitlines()
        self.assertEqual(rows[:-1], self.finding_rows(stdout))
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(re.fullmatch(r'queue \| [^|]+ \| [^|]+ \| [^|]+ \| [^|]+ \| verdict=\w+ \| warnings=\d+', r)
                            for r in rows[:-1]), rows)
        self.assertRegex(rows[-1], r'^queue \| waiting=4 needs_review=0 not_reviewable=0 warnings=0 \| ')
        self.assertNotIn('not-reviewable', stdout)
        self.assertNotIn('NOT REVIEWABLE', self.report())

    def test_refused_output_prints_no_row(self):
        self.drifted_run()
        self.linked_agency()
        for out in (self.tmp / 'absent' / 'queue', base.ROOT / 'queue-refused'):
            with self.subTest(out=out):
                self.assertEqual(self.show(out), (1, REFUSED))
                self.assertFalse(os.path.lexists(out))
        existing = self.tmp / 'existing'
        existing.mkdir()
        self.assertEqual(self.show(existing), (1, REFUSED))
        self.assertEqual(os.listdir(existing), [])


for _name in [n for n in dir(rq.ReviewQueueTest) if n.startswith('test_')]:
    setattr(ReviewQueueBlockedTest, _name, None)
