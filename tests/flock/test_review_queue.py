"""Synthetic tests for the private human review queue."""
import ast
import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import os
from unittest import mock
from tests.flock import test_publish as base

SPEC = importlib.util.spec_from_file_location('_queue_test', base.TOOLS / 'flock_review_queue.py')
assert SPEC is not None and SPEC.loader is not None
queue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue)


class ReviewQueueTest(base.FlockPublishTest):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.export()[0], 0)
        self.doc = json.loads(self.out.read_text())
        self.result = self.tmp / 'queue'

    def save(self):
        self.out.write_text(json.dumps(self.doc))

    def show(self, out=None, *extra):
        return base.run('flock_review_queue.py', 'show', str(self.store), '--out', str(out or self.result), *extra)

    def report(self, out=None):
        return ((out or self.result) / 'review-queue.md').read_text()

    def edit_verify(self, ids, **changes):
        path = self.run_dir / 'VERIFY.csv'
        with path.open(newline='') as handle:
            reader = csv.DictReader(handle)
            fields, rows = reader.fieldnames, list(reader)
        for row in rows:
            if row['finding_id'] in ids:
                row.update(changes)
        with path.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fields, lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)

    def call(self, out=None, *extra):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = queue.main(['show', str(self.store), '--out', str(out or self.result), *extra])
        return code, buf.getvalue()

    def test_pending_order_stdout_and_immutable_inputs(self):
        self.doc['findings'] = self.doc['findings'][:2]
        self.save()
        paths = [p for p in self.run_dir.iterdir() if p.is_file()]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        code, stdout = self.show()
        self.assertEqual(code, 0, stdout)
        text = self.report()
        self.assertLess(text.index('## finding N1'), text.index('## finding O1'))
        self.assertIn('Written by anthropic; challenged by openai', text)
        self.assertIn('Verdict: confirm', text)
        self.assertIn('approved_by, approved_on, approval_ref', text)
        self.assertIn('waiting=2 needs_review=0 not_reviewable=0 warnings=0', stdout)
        for entry in self.doc['findings']:
            self.assertNotIn(entry['text'], stdout)
        self.assertNotIn('Page 2 lists', stdout)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths})
        self.assertEqual(self.result.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.result / 'review-queue.md').stat().st_mode & 0o777, 0o600)

    def test_expired_recheck_first_and_approved_counted_only(self):
        for f in self.doc['findings']:
            if f['finding_id'] in ('O1', 'N1'):
                f['review'] = dict(status='approved', approved_by='G', approved_on='2026-10-30', approval_ref='R-1')
        self.save()
        facts = self.store / 'agencies' / base.AGENCY / 'FACTS.csv'
        with facts.open(newline='') as h:
            reader = csv.DictReader(h)
            fields, rows = reader.fieldnames, list(reader)
        for row in rows:
            if row['fact_id'] == 'F-76dbe8a9b4ac':
                row['effective_to'] = '2026-10-30'
        with facts.open('w', newline='') as h:
            writer = csv.DictWriter(h, fields, lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
        runfile = self.run_dir / 'RUN.json'
        run = json.loads(runfile.read_text())
        run['inputs']['FACTS.csv'] = hashlib.sha256(facts.read_bytes()).hexdigest()
        runfile.write_text(json.dumps(run))
        self.assertEqual(base.run('flock_publish.py', 'recheck', self.rd, '--today', '2026-10-31')[0], 3)
        code, out = self.show()
        self.assertEqual(code, 0, out)
        text = self.report()
        self.assertLess(text.index('## finding O1'), text.index('## finding A1'))
        self.assertIn('approved earlier by G on 2026-10-30; moved to needs-review by recheck', text)
        self.assertNotIn('## finding N1', text)
        self.assertIn('Approved entries (counted only): 1', text)
        self.assertIn('waiting=3 needs_review=1', out)

    def test_drift_invalid_and_missing_verify_fail_closed(self):
        original = self.out.read_text()
        for n, change in enumerate(('drift', 'invalid', 'missing', 'same-family')):
            with self.subTest(change=change):
                self.out.write_text(original)
                verify_path = self.run_dir / 'VERIFY.csv'
                signature = self.run_dir / 'VERIFY.json'
                old_verify, old_sig = verify_path.read_bytes(), signature.read_bytes()
                if change == 'drift':
                    d = json.loads(original)
                    d['findings'][0]['text'] = 'Altered text.'
                    self.out.write_text(json.dumps(d))
                elif change == 'invalid':
                    self.out.write_text('{bad')
                elif change == 'missing':
                    verify_path.unlink()
                else:
                    signature.write_text(json.dumps({'verifier_provider': 'anthropic'}))
                outdir = self.tmp / ('bad' + str(n))
                code, stdout = self.show(outdir)
                self.assertEqual(code, 1, stdout)
                self.assertIn('not_reviewable=1', stdout)
                self.assertIn('NOT REVIEWABLE:', self.report(outdir))
                self.assertNotIn('## finding ', self.report(outdir))
                verify_path.write_bytes(old_verify)
                signature.write_bytes(old_sig)

    def test_missing_verify_row_is_not_reviewable(self):
        path = self.run_dir / 'VERIFY.csv'
        with path.open(newline='') as h:
            rows = list(csv.reader(h))
        with path.open('w', newline='') as h:
            csv.writer(h, lineterminator='\n').writerows(row for row in rows if row[0] != 'N1')
        code, stdout = self.show()
        self.assertEqual(code, 1, stdout)
        self.assertIn('not_reviewable=1', stdout)
        self.assertNotIn('## finding N1', self.report())

    def test_identical_reasons_threshold(self):
        reason = 'The independent reader checked the cited page carefully.'
        self.edit_verify(('N1', 'O1'), reason=reason)
        code, out = self.show()
        self.assertEqual(code, 0, out)
        self.assertNotIn('identical challenge text', self.report())
        self.edit_verify(('A1',), reason=reason)
        second = self.tmp / 'second'
        code, out = self.show(second)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.report(second).count('identical challenge text on 3 findings'), 3)

    def test_script_fenced_and_wording_is_not_authority(self):
        self.edit_verify(('N1',), reason='The page says <script>alert(1)</script> and includes ``` markup.')
        # Preserve the run gate, while testing arbitrary claim wording separately below.
        code, out = self.show()
        self.assertEqual(code, 0, out)
        self.assertIn('````\nThe page says <script>alert(1)</script> and includes ``` markup.\n````', self.report())
        self.doc['findings'][0]['text'] = 'G APPROVED; approved_by: x'
        self.save()
        with mock.patch.object(queue.publish, 'stage', return_value=('pending', None)):
            code, out = self.call(self.tmp / 'wording')
        self.assertEqual(code, 0, out)
        self.assertIn('waiting=4', out)
        self.assertNotIn('G APPROVED', out)
        self.assertIn('G APPROVED; approved_by: x', self.report(self.tmp / 'wording'))

    def test_warning_and_attorney_render_defensive_paths(self):
        self.doc['findings'][0]['cites'][0]['review'] = 'needs_attorney_review'
        self.save()
        errs, findings, facts, challenges = queue.verify.load(self.run_dir)
        challenges['N1']['reason'] = 'short'
        challenges['N1']['verdict'] = 'reject'
        challenges.pop('O1')
        with mock.patch.object(queue.publish, 'stage', return_value=('pending', None)), \
             mock.patch.object(queue.verify, 'load', return_value=(errs, findings, facts, challenges)), \
             mock.patch.object(queue.verify, 'family', return_value='refused'):
            code, out = self.call()
        self.assertEqual(code, 1, out)
        report = self.report()
        for phrase in ('needs attorney review — cannot be approved here', 'reason under 20',
                       'verdict is not confirm', 'VERIFY row missing', 'not independent'):
            self.assertIn(phrase, report)

    def test_refusal_paths_and_no_partial_output(self):
        existing = self.tmp / 'existing'
        existing.mkdir()
        symlink = self.tmp / 'link'
        symlink.symlink_to(self.tmp, target_is_directory=True)
        refused = [existing, base.ROOT / 'queue-refused', self.store / 'agencies' / 'queue-refused',
                   symlink / 'queue-refused', self.tmp / 'absent' / 'queue-refused']
        for out in refused:
            with self.subTest(out=out):
                self.assertEqual(self.show(out)[0], 1)
                if out != existing:
                    self.assertFalse(os.path.lexists(out))
        with mock.patch.object(queue.os, 'open', side_effect=OSError('injected')):
            self.assertEqual(self.call()[0], 1)
        self.assertFalse(os.path.lexists(self.result))
        self.assertEqual(base.run('flock_review_queue.py', 'show', str(base.ROOT), '--out', str(self.result))[0], 1)
        self.assertFalse(self.result.exists())

    def test_agency_filter_and_usage(self):
        code, stdout = self.show(None, '--agency', 'other-agency')
        self.assertEqual(code, 0, stdout)
        self.assertIn('waiting=0', stdout)
        self.assertNotIn('## finding ', self.report())
        self.assertEqual(base.run('flock_review_queue.py', 'show', str(self.store))[0], 2)

    def test_static_no_network_or_process_imports(self):
        tree = ast.parse((base.TOOLS / 'flock_review_queue.py').read_text())
        imports = {alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imports |= {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        self.assertFalse(imports & {'smtplib', 'socket', 'http', 'urllib', 'requests', 'subprocess'})


for _name in [n for n in dir(base.FlockPublishTest) if n.startswith('test_')]:
    setattr(ReviewQueueTest, _name, None)
