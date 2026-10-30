"""Synthetic-only T15 reporting and checker acceptance."""
import contextlib
import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from campaign_tool.records import native_extract as ne, run_orchestrator as orch, run_report
from tools import run_report_check as checker
from tests.records import test_ocr_review as review_tests
from campaign_tool.records import ocr_review, run_stages
from tests.records import test_native_extract as native_tests
from campaign_tool.records.run import Pipeline
from campaign_tool.records.extraction_ledger import ExtractionBindingError


FIXTURES = Path(__file__).parent / 'fixtures' / 'run_report'


def report_fixture():
    return json.loads((FIXTURES / 'pass.json').read_text())


def stage_parse(stage):
    return orch.parse_stage_result(json.dumps(stage).encode(), orch.load_stage_result_schema())


class OCRTests(unittest.TestCase):
    setUp = review_tests.OCRReviewTests.setUp
    cli = review_tests.OCRReviewTests.cli
    mark = review_tests.OCRReviewTests.mark

    def stage(self):
        summary = dict(outcomes=dict.fromkeys(ne.OUTCOMES, 0), new_units=0, new_pages=0,
                       attempted=0, unexpected_failures=0, awaiting_ocr_images=7,
                       refusals=dict(not_accepted_coded=0, unlisted=0))
        with mock.patch.object(ne, 'run', return_value=summary), contextlib.redirect_stdout(io.StringIO()) as out:
            run_stages.main(['extract', '--project', str(self.project), '--timeout', '60', '--run-id', 'test'])
        return json.loads(out.getvalue())

    def test_current_pages_buckets_review_and_orchestrator_copy(self):
        self.assertEqual(self.mark(1)[0], 0)
        # A valid review of a page no longer current must not increase reviewed count.
        with run_stages.ledger_ro(self.ledger.parent) as db:
            stale = dict(ocr_review.pages(db)[1], page_no=99, schema='ocr-review-v1',
                         decision='confirmed', reviewer_label='synthetic',
                         reviewed_at='2026-10-29T00:00:00Z')
        stale['prev_line_sha256'] = hashlib.sha256(self.reviews.read_bytes()).hexdigest()
        with self.reviews.open('ab') as stream:
            stream.write(ocr_review.canonical(stale))
        stage = self.stage()
        expected = dict(pages_ocr=4, pages_conf_ge80=2, pages_conf_lt80=1, pages_conf_unknown=1,
                        pages_reviewed=1, awaiting_ocr_images=7)
        self.assertEqual(stage['ocr'], expected)
        self.assertEqual(stage['review_required'], 3)
        self.assertIsNotNone(stage_parse(stage))
        config = {'target': 'TEST', 'stages': [dict(name=n, enabled=True, timeout_seconds=60, argv=['/synthetic'])
                             for n in orch.WORK_STAGES]}
        def run(argv, timeout, schema, work, env):
            value = stage if run.call == 4 else run_stages.result('ok', {})
            run.call += 1
            return value['status'], value['counts'], value['error_code'], value, 0
        run.call = 0
        path = self.project / 'report.json'
        with mock.patch.object(orch, 'run_stage', side_effect=run):
            orch.execute(config, 'run_20261029T000000Z_1234abcd', 'test', orch.utc_now(),
                         time.time() + 60, path, self.project, env={})
        report = json.loads(path.read_text())
        self.assertEqual(report['ocr'], expected)
        self.assertEqual(report['refusals'], stage['refusals'])
        self.assertEqual(run_report.validate_run_report(report), report)
        config['stages'][4]['enabled'] = False
        with mock.patch.object(orch, 'run_stage', side_effect=run):
            orch.execute(config, 'run_20261029T000000Z_1234abcd', 'test', orch.utc_now(),
                         time.time() + 60, path, self.project, env={})
        skipped = json.loads(path.read_text())
        self.assertNotIn('ocr', skipped)
        self.assertNotIn('refusals', skipped)
        self.assertEqual(self.ledger.read_bytes(), self.original)

    def test_busy_omits_ocr_and_broken_chain_preserves_code(self):
        with mock.patch.object(run_stages, 'ledger_ro', side_effect=run_stages.LedgerBusy):
            self.assertNotIn('ocr', self.stage())
        self.reviews.parent.mkdir()
        self.reviews.write_text('broken synthetic chain\n')
        stage = self.stage()
        self.assertEqual(stage['error_code'], 'OCR_REVIEW_CHAIN_BROKEN')
        self.assertEqual(stage_parse(stage), stage)


class BlockValidationTests(unittest.TestCase):
    def test_optional_and_cross_field_rules(self):
        report = report_fixture()
        run_report.validate_run_report(report)
        ocr = dict(pages_ocr=4, pages_conf_ge80=2, pages_conf_lt80=1, pages_conf_unknown=1,
                   pages_reviewed=1, awaiting_ocr_images=7)
        refusals = dict(not_accepted_coded=1, unlisted=1)
        stage = dict(stage_result_version=1, status='ok', error_code=None,
                     counts=report['stages'][4]['counts'], outcomes=report['stages'][4]['outcomes'],
                     ocr=ocr, refusals=refusals)
        report.update(ocr=ocr, refusals=refusals)
        run_report.validate_run_report(report)
        self.assertIsNotNone(stage_parse(stage))
        for block, key, value in [('ocr', 'pages_ocr', 5), ('ocr', 'pages_reviewed', 5),
                                  ('refusals', 'not_accepted_coded', 2), ('refusals', 'unlisted', 425)]:
            with self.subTest(block=block, key=key):
                bad, bad_stage = copy.deepcopy(report), copy.deepcopy(stage)
                bad[block][key] = bad_stage[block][key] = value
                with self.assertRaises(ValueError):
                    run_report.validate_run_report(bad)
                self.assertIsNone(stage_parse(bad_stage))
                with tempfile.TemporaryDirectory() as tmp:
                    script = Path(tmp) / 'synthetic_stage.py'
                    script.write_text('print(' + repr(json.dumps(bad_stage)) + ')\n')
                    result = orch.run_stage([sys.executable, '-B', str(script)], 10,
                                            orch.load_stage_result_schema(), tmp, {})
                    self.assertEqual(result[2], 'STAGE_RESULT_INVALID')
        for block, key, value in [('ocr', 'pages_ocr', True), ('ocr', 'private', 0),
                                  ('refusals', 'unlisted', -1), ('refusals', 'private', 0)]:
            bad = copy.deepcopy(report)
            bad[block][key] = value
            with self.assertRaises(ValueError):
                run_report.validate_run_report(bad)


class ModeTests(unittest.TestCase):
    def test_run_and_abort_modes_with_explicit_environment(self):
        args = SimpleNamespace(run_id='run_20261029T000000Z_1234abcd',
                               started_at='2026-10-29T00:00:00Z', target_config='synthetic',
                               scheduled=True, expect_target=None, deadline_epoch=0,
                               report='unused', work_dir='unused', reason='TEST_ABORT', stage='sync')
        for env, expected in (({}, 'manual'), ({'INVOCATION_ID': ''}, 'manual'),
                              ({'INVOCATION_ID': 'synthetic'}, 'scheduled')):
            for target_mode in ('live', 'test'):
                mode = 'test' if target_mode == 'test' else expected
                with self.subTest(env=env, target=target_mode), mock.patch.dict(os.environ, env, clear=True), \
                        mock.patch.object(orch, 'load_target_config', return_value={'mode': target_mode}), \
                        mock.patch.object(orch.signal, 'signal'), mock.patch.object(orch, 'execute') as execute:
                    orch.cmd_run(args)
                    self.assertEqual(execute.call_args.args[2], mode)
                    with mock.patch.object(orch, 'atomic_write_json') as write:
                        orch.cmd_abort(args)
                        self.assertEqual(write.call_args.args[1]['mode'], mode)
                    with mock.patch.object(orch, 'load_target_config', side_effect=orch.TargetConfigError), \
                            mock.patch.object(orch, 'atomic_write_json') as write:
                        orch.cmd_abort(args)
                        self.assertEqual(write.call_args.args[1]['mode'], expected)
        self.assertFalse(orch.scheduled_invocation(False, {'INVOCATION_ID': 'synthetic'}))


class RefusalTests(native_tests.ExtractStageCase):
    attachments = (('note.txt', b'Synthetic note.'),)

    def assert_coverage(self, stage, unlisted=0):
        self.assertEqual(self.outcomes(stage), {ne.NOT_ACCEPTED: 2})
        self.assertEqual(stage['refusals'], dict(not_accepted_coded=2, unlisted=unlisted))
        self.assertEqual(stage['needs_g'], [dict(code=ne.NOT_ACCEPTED, severity='action', count=2)])

    def test_fresh_and_held_refusals(self):
        with mock.patch.object(Pipeline, 'stage_extract', side_effect=ExtractionBindingError('fixed_code')):
            self.assert_coverage(self.stage()[1])
        keys = self.query('SELECT key,count FROM alerts ORDER BY key')
        with mock.patch.object(Pipeline, 'stage_extract') as extract:
            self.assert_coverage(self.stage()[1])
            extract.assert_not_called()
        self.assertEqual(self.query('SELECT key,count FROM alerts ORDER BY key'), keys)
        self.assertEqual(ne.classify_codes({'extraction_check_refused'}), ne.NOT_ACCEPTED)

    def test_fallthrough_and_unlisted(self):
        with mock.patch.object(Pipeline, '_alert'), mock.patch.object(Pipeline, 'stage_extract', return_value={'status': 'refused'}):
            self.assert_coverage(self.stage()[1])
        with mock.patch.object(Pipeline, 'stage_extract', side_effect=ValueError('Private text /x 12')):
            self.assert_coverage(self.stage()[1], unlisted=2)


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.report = report_fixture()
        self.report.update(ocr=dict(pages_ocr=4, pages_conf_ge80=2, pages_conf_lt80=1,
                                   pages_conf_unknown=1, pages_reviewed=1, awaiting_ocr_images=7),
                           refusals=dict(not_accepted_coded=1, unlisted=1), review_required_count=3)
        self.baseline = json.loads((FIXTURES / 'baseline-20261023.json').read_text())
        self.baseline.update(run_budget_seconds=240, expected_outcomes={'EXTRACTED': 0},
                             expected_awaiting_ocr_images=7)

    def invoke(self, report, baseline, prior=None, replay=False):
        with tempfile.TemporaryDirectory() as tmp:
            argv = [str(Path(tmp) / 'report.json'), '--baseline', str(Path(tmp) / 'baseline.json')]
            for name, body in [('report', report), ('baseline', baseline), ('prior', prior)]:
                (Path(tmp) / (name + '.json')).write_text(json.dumps(body))
            if prior is not None:
                argv += ['--prior', str(Path(tmp) / 'prior.json')]
            if replay:
                argv += ['--replay']
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = checker.main(argv)
            return code, out.getvalue()

    def test_pass_and_fail_fixtures_each_new_criterion(self):
        prior = copy.deepcopy(self.report)
        for criterion, (mutation, value) in json.loads((FIXTURES / 't15-cases.json').read_text()).items():
            with self.subTest(criterion=criterion):
                code, text = self.invoke(self.report, self.baseline, prior, replay=True)
                self.assertEqual(code, 0, text)
                self.assertIn(criterion + ' PASS', text)
                report, baseline = copy.deepcopy(self.report), copy.deepcopy(self.baseline)
                if mutation == 'duration_seconds':
                    report.update(duration_seconds=value, finished_at='2000-01-01T00:02:00Z')
                elif mutation == 'expected_extracted':
                    baseline['expected_outcomes']['EXTRACTED'] = value
                elif mutation == 'unknown_outcome':
                    report['stages'][0]['outcomes'] = [dict(code='UNKNOWN_OUTCOME', count=value)]
                elif mutation == 'awaiting_ocr_images':
                    report['ocr'][mutation] = value
                elif mutation == 'review_required_count':
                    report[mutation] = value
                elif mutation == 'preserve_pending_growth':
                    report['stages'][4]['outcomes'][-1]['count'] += value
                    report['stages'][4]['counts']['input'] += value
                    report['stages'][4]['counts']['skipped'] += value
                elif mutation == 'new_documents':
                    report['new_data']['documents'] = value
                else:
                    report['refusals'][mutation] = value
                code, text = self.invoke(report, baseline, prior, replay=True)
                self.assertEqual(code, 1, text)
                self.assertIn(criterion + ' FAIL', text)
                self.assertNotIn('Traceback', text)
                self.assertIn('unlisted=1', text)

    def test_na_and_existing_fixture_exit_codes(self):
        baseline = json.loads((FIXTURES / 'baseline-20261023.json').read_text())
        for name, expected in [('pass', 0), ('widened-gap', 1), ('counts-mismatch', 1),
                               ('missing-stage', 1), ('notify-failed', 1)]:
            code, text = self.invoke(json.loads((FIXTURES / (name + '.json')).read_text()), baseline)
            self.assertEqual(code, expected, text)
            for number in (10, 11, 13, 14, 15, 16, 17):
                self.assertIn(f'P{number} NA', text)
        self.assertEqual(checker.KNOWN_OUTCOMES, set(ne.OUTCOMES))

    def test_invalid_inputs_are_suppressed(self):
        for key, value in [('unknown_private_text', 1), ('run_budget_seconds', 'private text'),
                           ('run_budget_seconds', True), ('run_budget_seconds', 120),
                           ('expected_outcomes', {'UNKNOWN_CODE': 1}),
                           ('expected_outcomes', {'EXTRACTED': True}),
                           ('expected_awaiting_ocr_images', -1)]:
            baseline = dict(self.baseline, **{key: value})
            code, text = self.invoke(self.report, baseline)
            self.assertEqual(code, 1)
            self.assertIn('CHECK INPUT FAIL', text)
            for number in range(1, 18):
                self.assertIn(f'P{number} NA', text)
            self.assertNotIn('private', text)
        for prior, replay in [(None, True), ({'private text': 1}, False)]:
            code, text = self.invoke(self.report, self.baseline, prior, replay)
            self.assertEqual(code, 1)
            self.assertIn('CHECK INPUT FAIL', text)

    def test_replay_identity_growth_and_no_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'prior.json'
            path.write_text('null')
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = checker.main([str(FIXTURES / 'pass.json'), '--baseline',
                                     str(FIXTURES / 'baseline-20261023.json'), '--prior', str(path)])
            self.assertEqual(code, 1)
            self.assertIn('CHECK INPUT FAIL', out.getvalue())
        prior = copy.deepcopy(self.report)
        prior['needs_g'][0]['count'] -= 1
        code, text = self.invoke(self.report, self.baseline, prior, replay=True)
        self.assertEqual(code, 1)
        self.assertIn('P16 FAIL', text)
