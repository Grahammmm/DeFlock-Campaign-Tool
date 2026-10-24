"""Offline acceptance checks: synthetic fixtures only; no LIVE paths."""
import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from tools.run_report_check import evaluate

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/run_report'
BASELINE = FIXTURES / 'baseline-20261023.json'
SCRIPT = ROOT / 'tools/run_report_check.py'


class RunReportCheckTests(unittest.TestCase):
    def setUp(self):
        self.baseline = json.loads(BASELINE.read_text())
        self.report = json.loads((FIXTURES / 'pass.json').read_text())

    def invoke(self, path, baseline=BASELINE):
        return subprocess.run([sys.executable, '-B', str(SCRIPT), str(path), '--baseline', str(baseline)],
                              capture_output=True, text=True, cwd=ROOT)

    def assert_table(self, text):
        rows = re.findall(r'^P([1-9]) (PASS|FAIL|NA) (.+)$', text, re.M)
        self.assertEqual([int(row[0]) for row in rows], list(range(1, 10)), text)
        self.assertNotIn('Traceback', text)

    def test_five_scenarios_cli(self):
        for name, expected in [('pass', 0), ('widened-gap', 1), ('counts-mismatch', 1),
                               ('missing-stage', 1), ('notify-failed', 1)]:
            with self.subTest(name=name):
                result = self.invoke(FIXTURES / (name + '.json'))
                self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
                self.assert_table(result.stdout)
                self.assertEqual(result.stderr, '')
        result = self.invoke(FIXTURES / 'notify-failed.json')
        self.assertIn('P8 FAIL', result.stdout)
        self.assertIn('AWAITING_OCR FAIL count=2 baseline=1',
                      self.invoke(FIXTURES / 'widened-gap.json').stdout)
        self.assertIn('CHECK sync FAIL', self.invoke(FIXTURES / 'counts-mismatch.json').stdout)

    def test_accepted_baseline_identities(self):
        expected = {('delta', 'ADOPTION_CODED_GAPS'): 116,
                    ('adoption', 'ADOPTION_CODED_GAPS'): 116,
                    ('adoption', 'ADOPT_BACKUP_SKIPPED'): 1,
                    ('portal', 'PORTAL_FETCH_DRY_RUN_ONLY'): 126,
                    ('extract', 'AWAITING_OCR'): 1,
                    ('extract', 'EXTRACT_CORRUPT_DOCUMENT'): 22,
                    ('extract', 'EXTRACT_NOT_ACCEPTED'): 1,
                    ('extract', 'EXTRACT_PRESERVE_PENDING'): 400}
        self.assertEqual({(i['stage'], i['code']): i['count'] for i in self.baseline['needs_g']}, expected)

    def test_pass_is_not_production_acceptance(self):
        code, lines = evaluate(self.report, self.baseline)
        self.assertEqual(code, 0)
        text = '\n'.join(lines)
        self.assert_table(text)
        self.assertIn('P1 FAIL', text)
        self.assertIn('P2 FAIL', text)
        for number in (3, 4, 5, 6, 7, 8, 9):
            self.assertIn(f'P{number} NA', text)
        self.assertIn('not production acceptance', text)

    def test_extraction_input_is_not_full_scope_denominator(self):
        self.report['stages'][4]['counts'].update(input=1, output=1, failed=0, skipped=0)
        self.report['stages'][4]['outcomes'] = [{'code': 'EXTRACTED', 'count': 1}]
        _, lines = evaluate(self.report, self.baseline)
        self.assertIn('P1 NA', '\n'.join(lines))
        self.assertIn('full in-scope denominator absent', '\n'.join(lines))
        del self.report['stages'][4]['outcomes']
        self.assertIn('P1 NA', '\n'.join(evaluate(self.report, self.baseline)[1]))

    def test_unseen_gap_fails_and_decreased_gaps_pass(self):
        self.report['needs_g'] = [{'stage': 'portal', 'code': 'NEW_GAP', 'count': 1, 'severity': 'info'}]
        self.assertEqual(evaluate(self.report, self.baseline)[0], 1)
        self.report['needs_g'] = []
        self.assertEqual(evaluate(self.report, self.baseline)[0], 0)

    def test_no_stage_offset_or_code_relocation(self):
        self.report['needs_g'][0]['count'] = 115
        self.report['needs_g'][1]['count'] = 117
        self.assertEqual(evaluate(self.report, self.baseline)[0], 1)
        self.report = json.loads((FIXTURES / 'pass.json').read_text())
        self.report['needs_g'][4]['stage'] = 'delta'
        self.assertEqual(evaluate(self.report, self.baseline)[0], 1)

    def test_all_stages_balance(self):
        for index in range(8):
            report = copy.deepcopy(self.report)
            report['stages'][index]['counts']['failed'] += 1
            with self.subTest(stage=index):
                self.assertEqual(evaluate(report, self.baseline)[0], 1)

    def test_workbook_and_portal_absence_never_pass(self):
        self.report['needs_g'] = []
        _, lines = evaluate(self.report, self.baseline)
        self.assertIn('P2 NA', '\n'.join(lines))
        self.report['needs_g'] = [{'stage': 'extract', 'code': 'EXTRACT_WORKBOOK_POLICY_REQUIRED',
                                   'count': 1, 'severity': 'info'}]
        self.assertIn('P4 FAIL', '\n'.join(evaluate(self.report, self.baseline)[1]))

    def test_notify_skipped_is_not_success(self):
        self.report['stages'][-1].update(status='skipped', duration_seconds=None)
        self.report['stages'][-1]['counts'].update(output=0, skipped=1)
        code, lines = evaluate(self.report, self.baseline)
        self.assertEqual(code, 1)
        self.assertIn('P8 FAIL', '\n'.join(lines))

    def test_invalid_types_missing_and_duplicate_stages(self):
        for mutation in ('negative', 'bool', 'float', 'missing_counts', 'duplicate_stage',
                         'unknown_stage', 'missing_needs', 'duplicate_gap', 'bad_outcome', 'bad_id'):
            report = copy.deepcopy(self.report)
            if mutation in ('negative', 'bool', 'float'):
                report['stages'][0]['counts']['input'] = {'negative': -1, 'bool': True, 'float': 1.0}[mutation]
            elif mutation == 'missing_counts':
                del report['stages'][0]['counts']['input']
            elif mutation == 'duplicate_stage':
                report['stages'][1]['name'] = 'sync'
            elif mutation == 'unknown_stage':
                report['stages'][0]['name'] = 'unknown'
            elif mutation == 'missing_needs':
                del report['needs_g']
            elif mutation == 'duplicate_gap':
                report['needs_g'].append(copy.deepcopy(report['needs_g'][0]))
            elif mutation == 'bad_outcome':
                report['stages'][4]['outcomes'][0]['count'] = 2
            else:
                report['run_id'] = 'run_20000101T000000Z_00000000\n'
            with self.subTest(mutation=mutation):
                code, lines = evaluate(report, self.baseline)
                self.assertEqual(code, 1)
                self.assert_table('\n'.join(lines))
                self.assertTrue(all(' NA ' in line for line in lines if re.match(r'^P[1-9] ', line)))

    def test_bad_baseline_fails_closed(self):
        for baseline in ({}, [], {'schema_version': True, 'needs_g': []},
                         {'schema_version': 1, 'needs_g': [{'stage': 'extract', 'code': 'BAD', 'count': True}]},
                         {'schema_version': 1, 'needs_g': [self.baseline['needs_g'][0]] * 2}):
            with self.subTest(baseline=baseline):
                self.assertEqual(evaluate(self.report, baseline)[0], 1)

    def test_document_text_is_never_echoed(self):
        marker = 'DO_NOT_ECHO_DOCUMENT_BODY alice@example.invalid /private/original.pdf'
        for field in ('run_id', 'needs_g', 'stages', 'extra_text'):
            report = copy.deepcopy(self.report)
            report[field] = marker
            code, lines = evaluate(report, self.baseline)
            self.assertEqual(code, 1)
            self.assertNotIn(marker, '\n'.join(lines))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.json'
            for text in (marker, '{"secret": "' + marker + '"}', '{"a": 1, "a": 2}', '{"a": NaN}'):
                path.write_text(text)
                result = self.invoke(path)
                self.assertEqual(result.returncode, 1)
                self.assert_table(result.stdout)
                self.assertNotIn(marker, result.stdout + result.stderr)
                self.assertEqual(result.stderr, '')
            result = self.invoke(Path(tmp) / 'missing.json')
            self.assertEqual(result.returncode, 1)
            self.assert_table(result.stdout)
            result = self.invoke(FIXTURES / 'pass.json', path)
            self.assertEqual(result.returncode, 1)


if __name__ == '__main__':
    unittest.main()
