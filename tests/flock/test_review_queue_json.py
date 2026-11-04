"""Synthetic tests for `flock_review_queue.py show --json` (RF0250-7): machine copy, written with the .md or not at all."""
import json
import os
import re
import shutil
from unittest import mock
from tests.flock import test_publish as base
from tests.flock import test_review_queue as rq

queue = rq.queue
CODES = {'verify-row-missing', 'verifier-not-independent', 'reason-under-20', 'verdict-not-confirm', 'identical-reason'}


class ReviewQueueJsonTest(rq.ReviewQueueTest):
    def data(self, out=None):
        return json.loads(((out or self.result) / 'review-queue.json').read_text())

    def pending(self):  # stage re-derivation is covered by test_review_queue; here only the JSON shape matters
        return mock.patch.object(queue.publish, 'stage', return_value=('pending', None))

    def summary(self, stdout):
        last = stdout.strip().splitlines()[-1]
        return {k: int(v) for k, v in re.findall(r'(\w+)=(\d+)', last)}

    def test_waiting_order_statuses_and_counts_match_markdown_and_stdout(self):
        self.assertEqual(len(self.doc['findings']), 4)
        self.doc['findings'][3]['review'] = dict(status='approved', approved_by='G', approved_on='2026-10-30', approval_ref='R-2',
                                                 content_hash=queue.content_hash(self.doc['findings'][3]))  # bound when approved
        self.doc['findings'][2]['review'] = dict(status='needs-review', approved_by='G', approved_on='2026-10-30',
                                                 approval_ref='R-1')
        self.save()
        with self.pending():
            code, stdout = self.call(None, '--json')
        self.assertEqual(code, 0, stdout)
        data, text = self.data(), self.report()
        self.assertEqual(data['schema'], 'flock-review-queue-v1')
        self.assertIs(data['review_required'], True)
        self.assertEqual(data['note'], 'only a human approves; this file changes nothing')
        self.assertIsNone(data['agency_filter'])
        self.assertRegex(data['built'], r'^\d{4}-\d{2}-\d{2}$')
        md_ids = re.findall(r'^## finding (\S+) · ', text, re.M)
        self.assertEqual([w['finding_id'] for w in data['waiting']], md_ids)
        self.assertEqual(len(md_ids), 3)
        rows = [r.split(' | ') for r in stdout.splitlines()[:-1]]
        self.assertEqual([(w['agency'], w['run'], w['finding_id'], w['status'], 'verdict=' + w['verdict'],
                           'warnings=%d' % len(w['warnings'])) for w in data['waiting']], [tuple(r[1:]) for r in rows])
        self.assertEqual([w['status'] for w in data['waiting']], ['needs-review', 'pending', 'pending'])
        self.assertEqual(data['waiting'][0]['agency'], base.AGENCY)
        self.assertEqual(data['waiting'][0]['run'], '2026-10-30')
        approved = int(re.findall(r'Approved entries \(counted only\): (\d+)', text)[0])
        self.assertEqual(data['counts'], dict(self.summary(stdout), approved=approved))
        self.assertEqual(data['counts'], {'waiting': 3, 'needs_review': 1, 'approved': 1, 'not_reviewable': 0, 'warnings': 0})
        self.assertEqual(data['not_reviewable'], [])
        raw = (self.result / 'review-queue.json').read_text()
        self.assertEqual(raw, json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True) + '\n')

    def test_each_warning_code_for_its_trigger(self):
        errs, findings, facts, challenges = queue.verify.load(self.run_dir)
        ids = sorted(f['finding_id'] for f in self.doc['findings'])
        short, exact = ids[0], ids[1]
        challenges[short].update(reason='x' * 19, verdict='downgrade')
        challenges[exact]['reason'] = 'y' * 20  # 20 characters is not a short reason
        missing = ids[2]
        challenges.pop(missing)
        with self.pending(), mock.patch.object(queue.verify, 'load', return_value=(errs, findings, facts, challenges)), \
                mock.patch.object(queue.verify, 'family', return_value='refused'):
            code, stdout = self.call(None, '--json')
        self.assertEqual(code, 1, stdout)
        got = {w['finding_id']: w['warnings'] for w in self.data()['waiting']}
        self.assertEqual(got[short], ['verifier-not-independent', 'reason-under-20', 'verdict-not-confirm'])
        self.assertEqual(got[exact], ['verifier-not-independent'])
        self.assertEqual(got[missing], ['verify-row-missing', 'verifier-not-independent'])
        self.assertEqual({w['finding_id']: w['verdict'] for w in self.data()['waiting']}[short], 'downgrade')
        self.assertEqual({w['finding_id']: w['verdict'] for w in self.data()['waiting']}[missing], 'missing')
        self.assertEqual(self.data()['counts']['warnings'], sum(len(v) for v in got.values()))
        self.assertEqual(self.summary(stdout)['warnings'], self.data()['counts']['warnings'])
        # identical challenge text: two copies are fine, the third flags all three (real VERIFY.csv, no mocks)
        reason = 'The independent reader checked the cited page carefully.'
        self.edit_verify(('N1', 'O1'), reason=reason)
        two = self.tmp / 'two'
        self.assertEqual(self.call(two, '--json')[0], 0)
        self.assertFalse(any(w['warnings'] for w in self.data(two)['waiting']))
        self.edit_verify(('A1',), reason=reason)
        three = self.tmp / 'three'
        self.assertEqual(self.call(three, '--json')[0], 1)
        flagged = {w['finding_id'] for w in self.data(three)['waiting'] if w['warnings'] == ['identical-reason']}
        self.assertEqual(flagged, {'N1', 'O1', 'A1'})
        for out in (None, two, three):
            for entry in self.data(out)['waiting']:
                self.assertLessEqual(set(entry['warnings']), CODES)

    def test_not_reviewable_reasons_link_drift_invalid(self):
        runs = self.run_dir.parent
        drifted, invalid = runs / '2026-10-31', runs / '2026-11-01'
        shutil.copytree(self.run_dir, drifted)
        shutil.copytree(self.run_dir, invalid)
        doc = json.loads((drifted / 'PUBLISH.json').read_text())
        doc['findings'][0]['text'] = 'Altered text.'
        (drifted / 'PUBLISH.json').write_text(json.dumps(doc))
        (invalid / 'PUBLISH.json').write_text(json.dumps({'findings': 'not a list'}))
        (runs / '2026-10-29').symlink_to(self.run_dir, target_is_directory=True)
        other = self.store / 'agencies' / 'zz-agency'
        other.mkdir()
        (other / 'runs').symlink_to(runs, target_is_directory=True)
        code, stdout = self.call(None, '--json')
        self.assertEqual(code, 1, stdout)
        data = self.data()
        self.assertEqual(data['not_reviewable'], [
            {'agency': base.AGENCY, 'run': '2026-10-29', 'reason': 'link-refused'},
            {'agency': base.AGENCY, 'run': '2026-10-31', 'reason': 'drifted'},
            {'agency': base.AGENCY, 'run': '2026-11-01', 'reason': 'invalid'},
            {'agency': 'zz-agency', 'run': None, 'reason': 'link-refused'}])
        self.assertEqual(data['counts']['not_reviewable'], 4)
        self.assertEqual(self.summary(stdout)['not_reviewable'], 4)
        self.assertEqual({w['run'] for w in data['waiting']}, {'2026-10-30'})
        self.assertEqual(self.report().count('NOT REVIEWABLE:'), 4)
        # --agency narrows both lists; the filter is recorded
        code, stdout = self.call(self.tmp / 'filtered', '--json', '--agency', 'zz-agency')
        self.assertEqual(code, 1, stdout)
        data = self.data(self.tmp / 'filtered')
        self.assertEqual(data['agency_filter'], 'zz-agency')
        self.assertEqual(data['waiting'], [])
        self.assertEqual(data['not_reviewable'], [{'agency': 'zz-agency', 'run': None, 'reason': 'link-refused'}])

    def test_agency_filter_excludes_other_agencies(self):
        other = self.store / 'agencies' / 'zz-agency'
        other.mkdir()
        (other / 'runs').symlink_to(self.run_dir.parent, target_is_directory=True)
        code, stdout = self.show(None, '--json', '--agency', base.AGENCY)
        self.assertEqual(code, 0, stdout)
        data = self.data()
        self.assertEqual(data['agency_filter'], base.AGENCY)
        self.assertEqual(data['not_reviewable'], [])
        self.assertTrue(data['waiting'])
        self.assertEqual({w['agency'] for w in data['waiting']}, {base.AGENCY})
        self.assertNotIn('zz-agency', (self.result / 'review-queue.json').read_text())

    def test_json_carries_no_prose_names_or_citations(self):
        for n, entry in enumerate(self.doc['findings']):
            entry['text'] = 'PLANTCLAIM%d about the cameras.' % n
            for cite in entry['cites']:
                cite['label'] = 'PLANTLABEL cite %d' % n
                if 'url' in cite:
                    cite['url'] = 'https://plantlaw.example/section-%d' % n
        self.doc['findings'][0]['review'] = dict(status='needs-review', approved_by='PLANTAPPROVER Person',
                                                 approved_on='2026-10-30', approval_ref='PLANTREF-9')
        self.save()
        self.edit_verify(('N1', 'O1', 'A1'), reason='PLANTREASON the reader re-read page two closely.',
                         records_sought='PLANTSOUGHT Existing contracts for the cameras')
        with self.pending():
            code, stdout = self.call(None, '--json')
        self.assertIn(code, (0, 1), stdout)
        report = self.report()
        for planted in ('PLANTCLAIM', 'PLANTLABEL', 'plantlaw.example', 'PLANTAPPROVER', 'PLANTREASON', 'PLANTSOUGHT'):
            with self.subTest(planted=planted):
                self.assertIn(planted, report)  # the human copy shows it, so it was really planted
                self.assertNotIn(planted.encode(), (self.result / 'review-queue.json').read_bytes())
        self.assertNotIn(b'PLANTREF', (self.result / 'review-queue.json').read_bytes())
        self.assertNotIn(b'Person', (self.result / 'review-queue.json').read_bytes())

    def test_fault_writing_json_leaves_neither_file(self):
        real = os.fdopen
        for fail_on in (1, 2):
            calls = []

            def fdopen(*args, **kwargs):
                calls.append(1)
                if len(calls) == fail_on:
                    raise OSError('injected')
                return real(*args, **kwargs)
            with self.subTest(fail_on=fail_on), mock.patch.object(queue.os, 'fdopen', fdopen):
                code, stdout = self.call(None, '--json')
                self.assertEqual(code, 1)
                self.assertEqual(stdout, 'queue refused: invalid input or output\n')
                self.assertFalse(os.path.lexists(self.result))
        with mock.patch.object(queue.os, 'open', side_effect=[os.open(os.devnull, os.O_WRONLY), OSError('injected')]):
            self.assertEqual(self.call(None, '--json')[0], 1)
        self.assertFalse(os.path.lexists(self.result))

    def test_without_json_output_and_stdout_unchanged(self):
        plain, with_json = self.tmp / 'plain', self.tmp / 'withjson'
        code, stdout = self.call(plain)
        text, rows, failed, data = queue.build(self.store, None)
        self.assertEqual(code, int(failed))
        self.assertEqual(sorted(os.listdir(plain)), ['review-queue.md'])
        self.assertEqual((plain / 'review-queue.md').read_bytes(), text.encode('utf-8'))
        self.assertEqual(stdout, '\n'.join(rows) + '\n')
        self.assertEqual(data['schema'], 'flock-review-queue-v1')
        code2, stdout2 = self.call(with_json, '--json')
        self.assertEqual((code2, stdout2), (code, stdout))
        self.assertEqual(sorted(os.listdir(with_json)), ['review-queue.json', 'review-queue.md'])
        self.assertEqual((with_json / 'review-queue.md').read_bytes(), (plain / 'review-queue.md').read_bytes())

    def test_modes_and_out_refusals_unchanged_with_json(self):
        code, stdout = self.show(None, '--json')
        self.assertEqual(code, 0, stdout)
        self.assertEqual(self.result.stat().st_mode & 0o777, 0o700)
        for name in ('review-queue.md', 'review-queue.json'):
            self.assertEqual((self.result / name).stat().st_mode & 0o777, 0o600)
        existing = self.tmp / 'existing'
        existing.mkdir()
        refused = [existing, base.ROOT / 'queue-refused', self.store / 'agencies' / 'queue-refused',
                   self.tmp / 'relative/../queue-refused', self.tmp / 'absent' / 'queue-refused']
        for out in refused:
            with self.subTest(out=out):
                self.assertEqual(self.show(out, '--json'), (1, 'queue refused: invalid input or output\n'))
                if out != existing:
                    self.assertFalse(os.path.lexists(out))
        self.assertEqual(os.listdir(existing), [])
        rel = base.run('flock_review_queue.py', 'show', str(self.store), '--out', 'queue-relative', '--json')
        self.assertEqual(rel, (1, 'queue refused: invalid input or output\n'))
        self.assertFalse(os.path.lexists(base.ROOT / 'queue-relative'))


for _name in [n for n in dir(rq.ReviewQueueTest) if n.startswith('test_')]:
    setattr(ReviewQueueJsonTest, _name, None)
