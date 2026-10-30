"""Fixed-code diagnostics ported from Opus reference 1f2156df; synthetic only."""
import json
import re
import sqlite3
import unittest
from unittest import mock
from campaign_tool.records import native_extract as ne, run_orchestrator
from campaign_tool.records.extraction_ledger import ExtractionBindingError
from campaign_tool.records.intake.mail_wire import MailWireError
from campaign_tool.records.run import Pipeline
from tests.records.test_native_extract import SCHEMA, ExtractStageCase

SUBJECT = "a" * 64
CODE = re.compile(r"[a-z_]{1,64}")


class FakePipeline:

    def __init__(self, result=None, error=None, alerts=()):
        self.result, self.error, self.alerts = (result, error, [])
        self.rows = [dict(key=key, count=count) for key, count in alerts]

    def stage_extract(self, subject, stage):
        if self.error is not None:
            raise self.error
        return self.result

    def _alert(self, key, owner):
        self.alerts.append(key)

    def _query(self, sql, values=()):
        if 'FROM alerts' in sql:
            prefix = values[1]
            return [row for row in self.rows if row['key'].startswith(prefix)]
        return [{'n': 0}]

def legacy_codes(key):
    """The 6787a8a1 parser, kept verbatim for the compatibility property."""
    return key.rsplit(':', 1)[-1].split(',')

class RefusalCodeTests(unittest.TestCase):

    def test_only_fixed_code_strings_are_recorded(self):
        cases = ((ExtractionBindingError('pdf_page_denominator_missing'), 'pdf_page_denominator_missing'), (MailWireError('quoted_printable_invalid'), 'quoted_printable_invalid'), (ValueError('source_size_limit; hashed but extraction queued'), 'source_size_limit'), (ValueError('Free text from /private/records/x.pdf'), 'unlisted'), (ValueError('QPDecodeError'), 'unlisted'), (ValueError('code123'), 'unlisted'), (ValueError('a' * 65), 'unlisted'), (ValueError(''), 'unlisted'), (ExtractionBindingError(), 'unlisted'), (OSError(2, 'No such file'), 'unlisted'), (RuntimeError('boom /private/x'), 'unlisted'))
        for error, expected in cases:
            with self.subTest(error=type(error).__name__, expected=expected):
                code = ne.refusal_code(error)
                self.assertEqual(code, expected)
                self.assertRegex(code, CODE)
                self.assertNotIn(type(error).__name__, code)

    def test_accept_status_codes(self):
        self.assertEqual(ne.accept_status_code('refused'), ('refused', 'accept_status_refused'))
        self.assertEqual(ne.accept_status_code('blocked'), ('blocked', 'accept_status_blocked'))
        self.assertEqual(ne.accept_status_code(None), ('missing', 'accept_status_missing'))
        self.assertEqual(ne.accept_status_code('Weird status!'), ('unlisted', 'unlisted'))
        self.assertEqual(ne.accept_status_code(7), ('unlisted', 'unlisted'))
        self.assertEqual(ne.accept_status_code({'x': 1}), ('unlisted', 'unlisted'))

class ExtractOneBranchTests(unittest.TestCase):

    def run_one(self, pipeline, form='pdf'):
        return ne.extract_one(pipeline, None, SUBJECT, form=form, timeout=120)

    def assertDetail(self, detail, code, accept_status, source):
        self.assertEqual(detail, {'refusal_code': code, 'accept_status': accept_status, 'refusal_source': source})
        self.assertRegex(detail['refusal_code'], CODE)

    def test_binding_error_refusal_carries_its_code(self):
        pipeline = FakePipeline(error=ExtractionBindingError('pdf_page_denominator_missing'))
        outcome, form, units, pages, detail = self.run_one(pipeline)
        self.assertEqual((outcome, form, units, pages), (ne.NOT_ACCEPTED, 'pdf', 0, 0))
        self.assertDetail(detail, 'pdf_page_denominator_missing', None, 'binding_error')
        self.assertEqual(pipeline.alerts, [f'extract:{SUBJECT}:extraction_check_refused:pdf_page_denominator_missing'])

    def test_value_error_refusal_with_free_text_is_unlisted(self):
        pipeline = FakePipeline(error=ValueError('Damaged at /private/records/x.pdf offset 12'))
        outcome, _, _, _, detail = self.run_one(pipeline)
        self.assertEqual(outcome, ne.NOT_ACCEPTED)
        self.assertDetail(detail, 'unlisted', None, 'value_error')
        self.assertEqual(pipeline.alerts, [f'extract:{SUBJECT}:extraction_check_refused:unlisted'])
        blob = json.dumps(detail) + ''.join(pipeline.alerts)
        for leaked in ('private', 'Damaged', 'ValueError', 'offset'):
            self.assertNotIn(leaked, blob)

    def test_mail_wire_error_refusal(self):
        pipeline = FakePipeline(error=MailWireError('mime_boundary_missing'))
        outcome, _, _, _, detail = self.run_one(pipeline, form='eml')
        self.assertEqual(outcome, ne.NOT_ACCEPTED)
        self.assertDetail(detail, 'mime_boundary_missing', None, 'value_error')

    def test_workbook_refusal_keeps_its_key_and_records_the_code(self):
        pipeline = FakePipeline(error=ExtractionBindingError('workbook_formula_cells_present'))
        outcome, form, _, _, detail = self.run_one(pipeline, form='xlsx')
        self.assertEqual((outcome, form), (ne.WORKBOOK_HELD, 'xlsx'))
        self.assertDetail(detail, 'workbook_formula_cells_present', None, 'binding_error')
        self.assertEqual(pipeline.alerts, [f'extract:{SUBJECT}:workbook_policy_refused'], 'key unchanged')

    def test_unexpected_error_keeps_the_retry_key(self):
        pipeline = FakePipeline(error=RuntimeError('boom /private/x'))
        outcome, _, _, _, detail = self.run_one(pipeline)
        self.assertEqual(outcome, ne.ERROR)
        self.assertDetail(detail, 'unlisted', None, 'exception')
        self.assertEqual(pipeline.alerts, [f'extract:{SUBJECT}:extraction_stage_error'], 'retry counting stays on the unchanged key')

    def test_unexpected_error_with_a_code_string(self):
        pipeline = FakePipeline(error=KeyError('import_id'))
        outcome, _, _, _, detail = self.run_one(pipeline)
        self.assertEqual(outcome, ne.ERROR)
        self.assertDetail(detail, 'import_id', None, 'exception')

    def test_accept_status_fall_through_is_still_not_accepted_with_a_distinct_code(self):
        cases = (({'status': 'refused'}, 'refused', 'accept_status_refused'), ({'status': 'needs_review'}, 'needs_review', 'accept_status_needs_review'), ({}, 'missing', 'accept_status_missing'), ({'status': None}, 'missing', 'accept_status_missing'), ({'status': 'blocked', 'reason': 'policy_hold:x'}, 'blocked', 'accept_status_blocked'), ({'status': 'Bad Status /x'}, 'unlisted', 'unlisted'), ({'status': 3}, 'unlisted', 'unlisted'))
        for result, accept_status, code in cases:
            with self.subTest(result=result):
                pipeline = FakePipeline(result=result)
                outcome, form, units, pages, detail = self.run_one(pipeline)
                self.assertEqual((outcome, form, units, pages), (ne.NOT_ACCEPTED, 'pdf', 0, 0))
                self.assertDetail(detail, code, accept_status, 'accept_status')
                self.assertEqual(pipeline.alerts, [f'extract:{SUBJECT}:extraction_check_refused:{code}'])

    def test_non_refusal_paths_are_unchanged(self):
        pipeline = FakePipeline(result={'status': 'done', 'units': 4})
        self.assertEqual(self.run_one(pipeline), (ne.EXTRACTED, 'pdf', 4, 0, {}))
        self.assertEqual(pipeline.alerts, [])
        for reason, outcome in (('extraction_failed:extraction_error', ne.CORRUPT), ('extraction_partial:ocr_runtime_unconfigured', ne.AWAITING_OCR), ('extraction_blocked:extraction_timeout', ne.TIMEOUT), ('extraction_blocked:decoder_needed', ne.DECODER_NEEDED), ('extraction_partial:expansion_limit', ne.INCOMPLETE)):
            with self.subTest(reason=reason):
                pipeline = FakePipeline(result={'status': 'blocked', 'reason': reason})
                self.assertEqual(self.run_one(pipeline), (outcome, 'pdf', 0, 0, {}))
                self.assertEqual(pipeline.alerts, [], 'the pipeline wrote these holds itself')

class AlertKeyCompatibilityTests(unittest.TestCase):
    LEGACY = (f'extract:{SUBJECT}:extraction_check_refused', f'extract:{SUBJECT}:extraction_stage_error', f'extract:{SUBJECT}:extraction_error', f'extract:{SUBJECT}:ocr_needed_or_blank_page,ocr_runtime_unconfigured', f'extract:{SUBJECT}:workbook_policy_refused', f'extract:{SUBJECT}:extraction_timeout', f'extract:{SUBJECT}:', f'extract:{SUBJECT}:a:b,c')

    def test_legacy_keys_parse_exactly_as_before(self):
        for key in self.LEGACY:
            with self.subTest(key=key):
                self.assertEqual(ne.alert_codes(key), legacy_codes(key))
                self.assertEqual(ne.classify_codes(ne.alert_codes(key)), ne.classify_codes(legacy_codes(key)))

    def test_suffixed_refusal_keys_classify_as_not_accepted(self):
        for code in ('pdf_page_denominator_missing', 'unlisted', 'accept_status_refused', 'ocr_runtime_unconfigured', 'extraction_timeout', 'extraction_error', 'decoder_needed', 'workbook_policy_refused', 'extraction_stage_error'):
            with self.subTest(code=code):
                key = f'extract:{SUBJECT}:extraction_check_refused:{code}'
                self.assertEqual(ne.alert_codes(key), ['extraction_check_refused'])
                self.assertEqual(ne.classify_codes(ne.alert_codes(key)), ne.NOT_ACCEPTED)
                self.assertFalse(ne.ocr_released(ne.alert_codes(key)))

    def test_classify_codes_still_sees_extraction_check_refused(self):
        self.assertEqual(ne.classify_codes({'extraction_check_refused'}), ne.NOT_ACCEPTED)
        self.assertEqual(ne.classify_codes({'extraction_stage_error'}), ne.ERROR)

    def test_held_outcome_decisions(self):
        refused = f'extract:{SUBJECT}:extraction_check_refused:pdf_page_denominator_missing'
        error = f'extract:{SUBJECT}:extraction_stage_error'
        ocr = f'extract:{SUBJECT}:ocr_needed_or_blank_page,ocr_runtime_unconfigured'
        cases = ((((refused, 1),), False, ne.NOT_ACCEPTED), (((refused, 1),), True, ne.NOT_ACCEPTED), (((f'extract:{SUBJECT}:extraction_check_refused', 1),), False, ne.NOT_ACCEPTED), (((error, 1),), False, None), (((error, ne.MAX_ATTEMPTS),), False, ne.ERROR), (((ocr, 1),), False, ne.AWAITING_OCR), (((ocr, 1),), True, None), (((ocr, 1), (refused, 1)), True, ne.NOT_ACCEPTED), ((), False, None))
        for alerts, ocr_enabled, expected in cases:
            with self.subTest(alerts=alerts, ocr_enabled=ocr_enabled):
                pipeline = FakePipeline(alerts=alerts)
                self.assertEqual(ne.held_outcome(pipeline, SUBJECT, ocr_enabled=ocr_enabled), expected)

    def test_other_subjects_do_not_hold(self):
        other = 'b' * 64
        pipeline = FakePipeline(alerts=((f'extract:{other}:extraction_check_refused:unlisted', 1),))
        self.assertIsNone(ne.held_outcome(pipeline, SUBJECT))

    def test_holding_alert_is_the_held_outcome_decision_plus_its_key(self):
        refused = f'extract:{SUBJECT}:extraction_check_refused:pdf_page_denominator_missing'
        error = f'extract:{SUBJECT}:extraction_stage_error'
        ocr = f'extract:{SUBJECT}:ocr_needed_or_blank_page,ocr_runtime_unconfigured'
        legacy = f'extract:{SUBJECT}:extraction_check_refused'
        cases = ((((refused, 1),), False, (ne.NOT_ACCEPTED, refused)), (((legacy, 1),), False, (ne.NOT_ACCEPTED, legacy)), (((error, 1),), False, (None, None)), (((error, 1), (refused, 1)), False, (ne.NOT_ACCEPTED, refused)), (((error, ne.MAX_ATTEMPTS),), False, (ne.ERROR, error)), (((ocr, 1), (refused, 1)), True, (ne.NOT_ACCEPTED, refused)), (((ocr, 1), (refused, 1)), False, (ne.AWAITING_OCR, ocr)), ((), False, (None, None)))
        for alerts, ocr_enabled, expected in cases:
            with self.subTest(alerts=alerts, ocr_enabled=ocr_enabled):
                self.assertEqual(ne._holding_alert(FakePipeline(alerts=alerts), SUBJECT, ocr_enabled=ocr_enabled), expected)
                self.assertEqual(ne.held_outcome(FakePipeline(alerts=alerts), SUBJECT, ocr_enabled=ocr_enabled), expected[0])

    def test_held_refusal_detail_reads_only_fixed_codes_from_the_key(self):
        base = f'extract:{SUBJECT}:extraction_check_refused'
        cases = ((base + ':pdf_page_denominator_missing', 'pdf_page_denominator_missing', None), (base + ':unlisted', 'unlisted', None), (base + ':accept_status_refused', 'accept_status_refused', 'refused'), (base + ':accept_status_missing', 'accept_status_missing', 'missing'), (base, 'unlisted', None), (base + ':Free text', 'unlisted', None), (base + ':code1', 'unlisted', None), (base + ':' + 'a' * 65, 'unlisted', None), (base + ':', 'unlisted', None), (base + ':accept_status_', 'accept_status_', None), (f'extract:{SUBJECT}:x,extraction_check_refused', 'unlisted', None), (f'extract:{SUBJECT}:a:b', 'unlisted', None))
        for key, code, accept_status in cases:
            with self.subTest(key=key):
                detail = ne.held_refusal_detail(key)
                self.assertEqual(detail, {'refusal_code': code, 'accept_status': accept_status, 'refusal_source': 'held_alert'})
                self.assertRegex(detail['refusal_code'], CODE)
                self.assertIn(detail['refusal_source'], ne.REFUSAL_SOURCES)

class ClassifyFormDiagnosticsTests(unittest.TestCase):

    class FormPipeline(FakePipeline):

        def __init__(self, error):
            super().__init__()
            self.form_error = error

        def _original(self, subject):
            return {'storage_path': '/x'}

        def _form(self, subject, original):
            raise self.form_error

    def test_unresolved_form_records_a_code_and_keeps_the_retry_key(self):
        for error, code in ((KeyError('declared_form_missing'), 'declared_form_missing'), (RuntimeError('cannot read /private/records/x'), 'unlisted')):
            with self.subTest(code=code):
                pipeline = self.FormPipeline(error)
                diagnostics = {}
                self.assertEqual(ne.classify_form(pipeline, SUBJECT, missing=[], diagnostics=diagnostics), (None, ne.ERROR))
                self.assertEqual(diagnostics, {'refusal_code': code, 'accept_status': None, 'refusal_source': 'form_resolution'})
                self.assertEqual(pipeline.alerts, [f'extract:{SUBJECT}:extraction_stage_error'])
                self.assertEqual(ne.classify_form(self.FormPipeline(error), SUBJECT, missing=[]), (None, ne.ERROR))

class AdditionalContractTests(unittest.TestCase):
    def test_codes_reject_newlines_and_other_non_codes(self):
        for value in ('valid_code\n', 'UPPER', 'a:b', 'a' * 65, '/private/x'):
            with self.subTest(value=value):
                self.assertEqual(ne.refusal_code(ValueError(value)), 'unlisted')
                self.assertEqual(ne.held_refusal_detail(f'extract:{SUBJECT}:extraction_check_refused:{value}')['refusal_code'], 'unlisted')
        self.assertEqual(ne.refusal_code(ValueError('  valid_code ; private text')), 'valid_code')
        self.assertEqual(ne.accept_status_code('a' * 64), ('unlisted', 'unlisted'))

    def test_histogram_is_pure_counts_only(self):
        documents = [{'refusal_code': 'unlisted'}, {'refusal_code': 'unlisted'},
                     {'refusal_code': 'fixed_code'}, {'outcome': ne.EXTRACTED}]
        before = json.dumps(documents)
        self.assertEqual(ne.refusal_code_histogram(documents), {'unlisted': 2, 'fixed_code': 1})
        self.assertEqual(json.dumps(documents), before)
        self.assertEqual(ne.refusal_code_histogram([]), {})

    def test_only_legacy_ocr_keys_are_resolved(self):
        base = f'extract:{SUBJECT}:'
        ocr = base + 'ocr_needed_or_blank_page,ocr_runtime_unconfigured'
        keys = [ocr, base + 'extraction_check_refused',
                base + 'extraction_check_refused:ocr_runtime_unconfigured']
        pipeline = FakePipeline(alerts=[(key, 1) for key in keys])
        pipeline._resolve_alert = mock.Mock()
        ne.resolve_ocr_released(pipeline, SUBJECT)
        self.assertEqual(pipeline._resolve_alert.call_args_list, [mock.call(ocr)])
        self.assertEqual(pipeline.alerts, [])


class StageRefusalDiagnosticsTests(ExtractStageCase):
    """End to end through run_stages: codes in the private list and alert key only."""
    attachments = (('note.txt', b'Synthetic plain-text note about retention.'),)

    def listing(self):
        body = json.loads((self.processing / 'runs' / 'extract-run-t11k.json').read_text())
        return body['documents']

    def test_recorded_main_decision_table(self):
        # Recorded against main a1c58131: outcome, attempted, unexpected, hold.
        cases = (
            ('binding', ExtractionBindingError('pdf_page_denominator_missing'), None, 'pdf', ne.NOT_ACCEPTED, 2, 2, ne.REFUSED_CODE),
            ('value', ValueError('free text 12'), None, 'pdf', ne.NOT_ACCEPTED, 2, 2, ne.REFUSED_CODE),
            ('exception', RuntimeError('/private/x'), None, 'pdf', ne.ERROR, 2, 2, ne.ERROR_CODE),
            ('odd', None, {'status': 'odd'}, 'pdf', ne.NOT_ACCEPTED, 2, 2, ne.REFUSED_CODE),
            ('missing', None, {'status': None}, 'pdf', ne.NOT_ACCEPTED, 2, 2, ne.REFUSED_CODE),
            ('invalid', None, {'status': 'Bad Status'}, 'pdf', ne.NOT_ACCEPTED, 2, 2, ne.REFUSED_CODE),
            ('workbook', ExtractionBindingError('workbook_formula_cells_present'), None, 'xlsx', ne.WORKBOOK_HELD, 2, 0, ne.WORKBOOK_REFUSED_CODE),
            ('form', RuntimeError('/private/x'), None, None, ne.ERROR, 0, 0, ne.ERROR_CODE),
            ('success', None, {'status': 'done'}, 'pdf', ne.EXTRACTED, 2, 0, None),
        )
        for name, error, result, form, outcome, attempted, unexpected, hold in cases:
            with self.subTest(branch=name), mock.patch.object(Pipeline, '_form', return_value=form,
                    side_effect=error if form is None else None), mock.patch.object(Pipeline, '_alert') as alert, \
                    mock.patch.object(Pipeline, 'stage_extract', return_value=result,
                    side_effect=error if form is not None else None):
                summary = ne.run(self.processing, run_id='run-t11k', deadline=float('inf'))
                self.assertEqual({k: v for k, v in summary['outcomes'].items() if v}, {outcome: 2})
                self.assertEqual((summary['attempted'], summary['unexpected_failures']), (attempted, unexpected))
                self.assertEqual(len(alert.call_args_list), 2 if hold else 0)
                for call in alert.call_args_list:
                    # Only refusal keys gain a diagnostic suffix.
                    self.assertEqual(call.args[0].split(':')[2], hold)
                    if hold != ne.REFUSED_CODE:
                        self.assertEqual(len(call.args[0].split(':')), 3)
                blob = json.dumps(self.listing())
                self.assertNotIn('/private/x', blob)
                self.assertNotIn('free text 12', blob)
                self.assertEqual((self.processing / 'runs' / 'extract-run-t11k.json').stat().st_mode & 0o777, 0o600)

    def test_refusals_record_codes_and_decisions_are_unchanged(self):
        with mock.patch.object(Pipeline, 'stage_extract', side_effect=ExtractionBindingError('pdf_page_denominator_missing')):
            code, parsed, raw = self.stage()
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.outcomes(parsed), {'EXTRACT_NOT_ACCEPTED': 2})
        self.assertEqual((parsed['status'], parsed['error_code']), ('warn', 'EXTRACT_DOCUMENT_FAILURES'))
        self.assertNotIn('refusal', raw, 'the stage result line is unchanged')
        self.assertNotIn('pdf_page_denominator_missing', raw)
        self.assertIsNotNone(run_orchestrator.parse_stage_result(raw.encode(), SCHEMA))
        documents = self.listing()
        self.assertEqual({d['refusal_code'] for d in documents}, {'pdf_page_denominator_missing'})
        self.assertEqual({d['accept_status'] for d in documents}, {None})
        self.assertEqual({d['refusal_source'] for d in documents}, {'binding_error'})
        keys = [row[0] for row in self.query("SELECT key FROM alerts WHERE state='open' ORDER BY key")]
        self.assertEqual(len(keys), 2)
        for key in keys:
            self.assertRegex(key, '^extract:[0-9a-f]{64}:extraction_check_refused:pdf_page_denominator_missing$')
        with mock.patch.object(Pipeline, 'stage_extract') as stage_extract:
            code, parsed, raw = self.stage()
        self.assertEqual(stage_extract.call_count, 0)
        self.assertEqual(self.outcomes(parsed), {'EXTRACT_NOT_ACCEPTED': 2})
        self.assertEqual([(d['refusal_code'], d['accept_status'], d['refusal_source']) for d in self.listing()], [('pdf_page_denominator_missing', None, 'held_alert')] * 2)
        self.assertEqual(self.query('SELECT key,count FROM alerts ORDER BY key'), [(key, 1) for key in keys], 'no second alert row, no recount')
        con = sqlite3.connect(self.processing / 'ledger.sqlite')
        con.execute("UPDATE alerts SET state='resolved' WHERE key LIKE 'extract:%'")
        con.commit()
        con.close()
        code, parsed, raw = self.stage()
        self.assertEqual(self.outcomes(parsed), {'EXTRACTED': 2})

    def test_fall_through_status_records_accept_status_code(self):
        with mock.patch.object(Pipeline, 'stage_extract', return_value={'status': 'refused'}):
            code, parsed, raw = self.stage()
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.outcomes(parsed), {'EXTRACT_NOT_ACCEPTED': 2})
        self.assertEqual({(d['refusal_code'], d['accept_status'], d['refusal_source']) for d in self.listing()}, {('accept_status_refused', 'refused', 'accept_status')})
        keys = {row[0].rsplit(':', 1)[-1] for row in self.query("SELECT key FROM alerts WHERE state='open'")}
        self.assertEqual(keys, {'accept_status_refused'})

    def test_unresolved_form_error_records_a_code_in_the_private_list(self):
        with mock.patch.object(Pipeline, '_form', side_effect=RuntimeError('unreadable /private/x')):
            code, parsed, raw = self.stage()
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.outcomes(parsed), {'EXTRACT_ERROR': 2})
        self.assertNotIn('private', raw)
        self.assertEqual({(d['form'], d['refusal_code'], d['refusal_source']) for d in self.listing()}, {(None, 'unlisted', 'form_resolution')})
        self.assertNotIn('private', json.dumps(self.listing()))

    def test_legacy_refused_alert_still_holds_under_candidate_d(self):
        con = sqlite3.connect(self.processing / 'ledger.sqlite')
        subjects = [row[0] for row in con.execute('SELECT sha256 FROM originals')]
        self.assertEqual(len(subjects), 2)
        for subject in subjects:
            con.execute("INSERT INTO alerts(id,key,first_seen,last_seen,count,owner,state) VALUES(?,?,'t','t',1,'runtime','open')", (subject[:32], f'extract:{subject}:extraction_check_refused'))
        con.commit()
        con.close()
        with mock.patch.object(Pipeline, 'stage_extract') as stage_extract:
            code, parsed, raw = self.stage()
        self.assertEqual(stage_extract.call_count, 0)
        self.assertEqual(self.outcomes(parsed), {'EXTRACT_NOT_ACCEPTED': 2})
        self.assertEqual({(d['refusal_code'], d['accept_status'], d['refusal_source']) for d in self.listing()}, {('unlisted', None, 'held_alert')}, 'a legacy hold has no code to recover')
        self.assertEqual(self.query('SELECT key,count FROM alerts ORDER BY key'), sorted(((f'extract:{s}:extraction_check_refused', 1) for s in subjects)))

    def test_held_fall_through_restores_accept_status(self):
        with mock.patch.object(Pipeline, 'stage_extract', return_value={'status': 'refused'}):
            self.stage()
        with mock.patch.object(Pipeline, 'stage_extract') as stage_extract:
            code, parsed, raw = self.stage()
        self.assertEqual(stage_extract.call_count, 0)
        self.assertEqual(self.outcomes(parsed), {'EXTRACT_NOT_ACCEPTED': 2})
        self.assertEqual({(d['refusal_code'], d['accept_status'], d['refusal_source']) for d in self.listing()}, {('accept_status_refused', 'refused', 'held_alert')})
