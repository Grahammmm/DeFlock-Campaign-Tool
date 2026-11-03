"""RF2020-4: run_report_check --json (records-run-report-check-v1); synthetic reports only."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from tools.run_report_check import evaluate

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/run_report'
SCRIPT = ROOT / 'tools/run_report_check.py'
NAMES = ('pass', 'widened-gap', 'counts-mismatch', 'missing-stage', 'notify-failed')
NOTE = 'machine checks only; review required; not production acceptance'
PLANTED_RUN = 'run_20991231T235959Z_deadbeef'


def fixture(name):
    return json.loads((FIXTURES / (name + '.json')).read_text())


def t15_inputs():
    """The extended report/baseline the T15 checker tests use (all P10-P17 PASS)."""
    report = fixture('pass')
    report.update(ocr=dict(pages_ocr=4, pages_conf_ge80=2, pages_conf_lt80=1, pages_conf_unknown=1,
                           pages_reviewed=1, awaiting_ocr_images=7),
                  refusals=dict(not_accepted_coded=1, unlisted=1), review_required_count=3)
    baseline = fixture('baseline-20261023')
    baseline.update(run_budget_seconds=240, expected_outcomes={'EXTRACTED': 0},
                    expected_awaiting_ocr_images=7)
    return report, baseline


def parse_text(text):
    """Text lines -> the comparable facts, independent of the JSON renderer."""
    lines = text.splitlines()
    run = [line[len('run_id='):] for line in lines if line.startswith('run_id=')]
    stages = [dict(name=m[1], status=m[2], input=int(m[3]), output=int(m[4]), failed=int(m[5]), skipped=int(m[6]))
              for m in (re.fullmatch(r'CHECK (\w+) (PASS|FAIL) input=(\d+) output=(\d+) failed=(\d+) skipped=(\d+)', line)
                        for line in lines) if m]
    gaps = [dict(stage=m[1], code=m[2], status=m[3], count=int(m[4]), baseline=int(m[5]))
            for m in (re.fullmatch(r'GAP (\w+) (\w+) (PASS|FAIL) count=(\d+) baseline=(\d+)', line)
                      for line in lines) if m]
    criteria = [dict(id=m[1], status=m[2], reason=m[3])
                for m in (re.fullmatch(r'(P\d+) (PASS|FAIL|NA) (.*)', line) for line in lines) if m]
    exits = [int(m[1]) for m in (re.fullmatch(r'EXIT (\d); machine checks only; review required', line)
                                 for line in lines) if m]
    return dict(valid='CHECK INPUT FAIL invalid report or baseline; values suppressed' not in lines,
                run_id=run[0] if run else None, stages=stages, gaps=gaps, criteria=criteria,
                exit=exits[-1] if exits else None)


class RunReportCheckJsonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, body):
        path = self.dir / name
        path.write_text(body if isinstance(body, str) else json.dumps(body))
        return str(path)

    def cli(self, report, baseline, *extra):
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), report, '--baseline', baseline, *extra],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.stderr, '')
        self.assertNotIn('Traceback', result.stdout)
        return result

    def both(self, report, baseline, prior=None, replay=False):
        """Run text and --json on the same inputs; return (text result, JSON document)."""
        args = [self.write('report.json', report), self.write('baseline.json', baseline)]
        if prior is not None:
            args += ['--prior', self.write('prior.json', prior)]
        if replay:
            args += ['--replay']
        text = self.cli(*args)
        machine = self.cli(*args, '--json')
        self.assertEqual(machine.returncode, text.returncode)
        doc = self.assert_document(machine.stdout)
        self.assertEqual(doc['exit'], text.returncode)
        return text, doc

    def assert_document(self, stdout):
        self.assertTrue(stdout.endswith('\n'))
        self.assertEqual(stdout.count('\n'), 1, stdout)
        doc = json.loads(stdout)
        self.assertEqual(stdout[:-1], json.dumps(doc, sort_keys=True))
        self.assertEqual(set(doc), {'schema', 'valid', 'run_id', 'stages', 'gaps', 'criteria', 'exit', 'note'})
        self.assertEqual((doc['schema'], doc['note']), ('records-run-report-check-v1', NOTE))
        self.assertEqual([c['id'] for c in doc['criteria']], [f'P{i}' for i in range(1, 18)])
        self.assertIn(doc['exit'], (0, 1))
        return doc

    def assert_round_trip(self, text, doc):
        parsed = parse_text(text.stdout)
        self.assertEqual(parsed, {k: doc[k] for k in parsed}, text.stdout)
        self.assertEqual(parsed['exit'], text.returncode)

    def assert_invalid(self, doc, *planted):
        self.assertEqual((doc['valid'], doc['run_id'], doc['stages'], doc['gaps'], doc['exit']),
                         (False, None, [], [], 1))
        self.assertEqual({(c['status'], c['reason']) for c in doc['criteria']},
                         {('NA', 'invalid input prevents assessment')})
        encoded = json.dumps(doc)
        for value in planted:
            self.assertNotIn(value, encoded)

    def test_balanced_report_within_baseline(self):
        text, doc = self.both(fixture('pass'), fixture('baseline-20261023'))
        self.assertEqual((doc['exit'], doc['valid']), (0, True))
        self.assertIn('EXIT 0;', text.stdout)
        self.assertEqual(len(doc['stages']), 8)
        self.assertEqual({s['status'] for s in doc['stages']}, {'PASS'})
        self.assertEqual({g['status'] for g in doc['gaps']}, {'PASS'})
        self.assert_round_trip(text, doc)

    def test_unbalanced_stage_and_widened_gap(self):
        report = fixture('pass')
        report['stages'][2]['counts']['failed'] += 1
        text, doc = self.both(report, fixture('baseline-20261023'))
        self.assertEqual(doc['exit'], 1)
        self.assertEqual([s['name'] for s in doc['stages'] if s['status'] == 'FAIL'], [report['stages'][2]['name']])
        self.assert_round_trip(text, doc)
        text, doc = self.both(fixture('widened-gap'), fixture('baseline-20261023'))
        self.assertEqual(doc['exit'], 1)
        failed = [g for g in doc['gaps'] if g['status'] == 'FAIL']
        self.assertEqual(failed, [dict(stage='extract', code='AWAITING_OCR', status='FAIL', count=2, baseline=1)])

    def test_code_absent_from_baseline_allows_zero(self):
        report = fixture('pass')
        report['needs_g'].append({'stage': 'portal', 'code': 'NEW_GAP', 'count': 1, 'severity': 'info'})
        text, doc = self.both(report, fixture('baseline-20261023'))
        self.assertEqual(doc['exit'], 1)
        self.assertIn(dict(stage='portal', code='NEW_GAP', status='FAIL', count=1, baseline=0), doc['gaps'])
        self.assert_round_trip(text, doc)
        report['needs_g'] = []
        _, doc = self.both(report, fixture('baseline-20261023'))
        self.assertEqual(doc['exit'], 0)
        self.assertEqual({(g['status'], g['count']) for g in doc['gaps']}, {('PASS', 0)})

    def test_round_trip_every_existing_fixture(self):
        for name in NAMES:
            with self.subTest(name=name):
                self.assert_round_trip(*self.both(fixture(name), fixture('baseline-20261023')))
        report, baseline = t15_inputs()
        text, doc = self.both(report, baseline, copy.deepcopy(report), replay=True)
        self.assertEqual([c['status'] for c in doc['criteria'][9:]], ['PASS'] * 8)
        self.assert_round_trip(text, doc)
        for criterion, (mutation, value) in json.loads((FIXTURES / 't15-cases.json').read_text()).items():
            bad = copy.deepcopy(report)
            if mutation == 'review_required_count':
                bad[mutation] = value
            elif mutation == 'awaiting_ocr_images':
                bad['ocr'][mutation] = value
            elif mutation == 'not_accepted_coded':
                bad['refusals'][mutation] = value
            else:
                continue
            with self.subTest(criterion=criterion):
                text, doc = self.both(bad, baseline, copy.deepcopy(report), replay=True)
                self.assertEqual(doc['criteria'][int(criterion[1:]) - 1]['status'], 'FAIL')
                self.assert_round_trip(text, doc)

    def test_evaluate_and_text_unchanged(self):
        for name in NAMES:
            with self.subTest(name=name):
                code, lines = evaluate(fixture(name), fixture('baseline-20261023'))
                text = self.cli(self.write('r.json', fixture(name)), self.write('b.json', fixture('baseline-20261023')))
                self.assertEqual((text.returncode, text.stdout), (code, '\n'.join(lines) + '\n'))
                self.assertNotIn('{', text.stdout)

    def test_invalid_inputs_suppressed(self):
        good = self.write('baseline.json', fixture('baseline-20261023'))
        report = fixture('pass')
        report['run_id'] = PLANTED_RUN
        cases = {
            'duplicate': self.write('dup.json', '{"run_id": "%s", "PLANTED_KEY": 1, "PLANTED_KEY": 2}' % PLANTED_RUN),
            'nan': self.write('nan.json', '{"run_id": "%s", "PLANTED_KEY": NaN}' % PLANTED_RUN),
            'unreadable': str(self.dir / 'PLANTED_PATH_missing.json'),
        }
        planted = (PLANTED_RUN, 'PLANTED_KEY', 'PLANTED_PATH', str(self.dir))
        for name, path in cases.items():
            with self.subTest(case=name):
                text = self.cli(path, good)
                machine = self.cli(path, good, '--json')
                self.assertEqual((text.returncode, machine.returncode), (1, 1))
                self.assert_invalid(self.assert_document(machine.stdout), *planted)
                self.assertTrue(text.stdout.startswith('CHECK INPUT FAIL'))
                for value in planted:
                    self.assertNotIn(value, machine.stdout + text.stdout)
        text, doc = self.both(report, fixture('baseline-20261023'), replay=True)
        self.assert_invalid(doc, *planted)
        self.assert_round_trip(text, doc)
        self.assertNotIn(PLANTED_RUN, text.stdout)
        _, doc = self.both(report, fixture('baseline-20261023'))
        self.assertEqual((doc['valid'], doc['run_id']), (True, PLANTED_RUN))

    def test_prior_replay_new_documents_fails_p16(self):
        report, baseline = t15_inputs()
        prior = copy.deepcopy(report)
        report['new_data']['documents'] = 1
        text, doc = self.both(report, baseline, prior, replay=True)
        self.assertEqual(doc['criteria'][15], dict(id='P16', status='FAIL',
                                                   reason='replay adds no documents or needs_g counts'))
        self.assertIn('P16 FAIL', text.stdout)
        self.assertEqual(doc['exit'], 1)
        self.assert_round_trip(text, doc)
        _, doc = self.both(report, baseline, prior)
        self.assertEqual(doc['criteria'][15]['status'], 'NA')

    def test_one_line_deterministic_and_no_writes(self):
        args = [str(FIXTURES / 'pass.json'), '--baseline', str(FIXTURES / 'baseline-20261023.json'), '--json']
        before = sorted(os.listdir(self.dir)), sorted(os.listdir(FIXTURES))
        first = subprocess.run([sys.executable, '-B', str(SCRIPT), *args], capture_output=True, cwd=self.dir)
        second = subprocess.run([sys.executable, '-B', str(SCRIPT), *args], capture_output=True, cwd=self.dir)
        self.assertEqual((first.returncode, first.stderr), (0, b''))
        self.assertEqual(first.stdout, second.stdout)
        self.assert_document(first.stdout.decode())
        self.assertEqual((sorted(os.listdir(self.dir)), sorted(os.listdir(FIXTURES))), before)


if __name__ == '__main__':
    unittest.main()
