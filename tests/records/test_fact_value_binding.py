"""M8: valid citations cannot launder unrelated model-supplied fact values."""
import unittest
from tests.records import test_understanding as fixtures
from campaign_tool.records import grounded_evidence
from campaign_tool.records.understanding import evidence, reconcile


class FactBindingTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.ReconcileTests('runTest')
        self.h.setUp()

    def compare(self, value, cites, **extra):
        fact = dict(fact_id='f1', field='retention_days', value=value, citations=cites, **extra)
        return reconcile.compare([fact], self.h.versions, bundle=self.h.bundle, **self.h.scope)['findings'][0]

    def test_unrelated_valid_quote_does_not_ground_either_comparison_outcome(self):
        cite = evidence.cite_text(self.h.bundle['units'][0], 'Example Agency')
        for value in (10, 45, 'fictional value'):
            with self.subTest(value=value):
                row = self.compare(value, [cite])
                self.assertEqual((row['classification'], row['reason']), ('not_assessed', 'fact_value_unbound'))
                self.assertFalse(row['review_acceptance'])

    def test_partial_words_cannot_ground_any_string_operator(self):
        cite = evidence.cite_text(self.h.bundle['units'][0], 'Example Agency')
        for operator in ('equals', 'not_equals', 'contains'):
            self.h.versions[0]['requirements'][0]['test'].update(operator=operator, expected='Example Agency')
            for value in ('a', 'e', 'Ex', 'ample', 'Agenc', 'gency'):
                with self.subTest(operator=operator, value=value):
                    row = self.compare(value, [cite])
                    self.assertEqual((row['classification'], row['reason']),
                                     ('not_assessed', 'fact_value_unbound'))

    def test_whole_phrase_still_reaches_string_comparison(self):
        cite = evidence.cite_text(self.h.bundle['units'][0], 'Example Agency')
        for operator, expected, classification in (
                ('equals', 'Example Agency', 'no_conflict_observed'),
                ('not_equals', 'Example Agency', 'potential_conflict'),
                ('contains', 'Agency', 'no_conflict_observed')):
            with self.subTest(operator=operator):
                self.h.versions[0]['requirements'][0]['test'].update(operator=operator, expected=expected)
                row = self.compare('Example Agency', [cite])
                self.assertEqual(row['classification'], classification)
                self.assertEqual(row['legal_assessment'], 'NOT_ASSESSED')
                self.assertFalse(row['review_acceptance'])

    def test_string_boundaries_handle_punctuation_and_unicode_without_case_folding(self):
        quote = 'Notice provided. (No notice); café policy, agency_name.'
        for value in ('Not', 'otice', 'café pol', 'fé', 'agency', 'name', ' ', 'no notice'):
            with self.subTest(unbound=value):
                self.assertFalse(reconcile.fact_value_bound(dict(value=value, citations=[{'quote': quote}])))
        for value in ('Notice', 'No notice', 'café policy', 'agency_name'):
            with self.subTest(bound=value):
                self.assertTrue(reconcile.fact_value_bound(dict(value=value, citations=[{'quote': quote}])))
        self.assertFalse(reconcile.fact_value_bound(dict(value='No', citations=[{'quote': 'Notice provided'}])))

    def test_json_pointer_requires_exact_typed_value(self):
        for value in (3, 300, '30', True):
            with self.subTest(value=value):
                self.assertEqual(self.compare(value, [self.h.fact_cite])['reason'], 'fact_value_unbound')
        self.assertEqual(self.compare(30, [self.h.fact_cite])['classification'], 'no_conflict_observed')

    def test_text_numeric_tokens_not_substrings_and_matching_values_still_work(self):
        cite = evidence.cite_text(self.h.bundle['units'][0], 'audit log entries for 30 days')
        self.assertEqual(self.compare(3, [cite])['reason'], 'fact_value_unbound')
        self.assertEqual(self.compare(30, [cite])['classification'], 'no_conflict_observed')
        self.assertEqual(self.compare('audit log entries', [cite])['classification'], 'not_assessed')
        self.assertNotEqual(self.compare('audit log entries', [cite])['reason'], 'fact_value_unbound')

    def test_grounded_conflict_is_retained(self):
        receipt = fixtures.native_receipt()
        receipt['units'][1]['text'] = '{"retention_days": 45}'
        self.h.bundle = evidence.normalize_receipt(receipt, fixtures.ORIGINAL, evidence.sha(receipt))
        self.h.versions[0]['requirements'][0]['citations'] = [
            evidence.cite_text(self.h.bundle['units'][0], 'audit log entries for 30 days')]
        cite = grounded_evidence.pointer_support(self.h.bundle['units'][1], '/retention_days')
        row = self.compare(45, [cite])
        self.assertEqual(row['classification'], 'potential_conflict')
        self.assertEqual(row['legal_assessment'], 'NOT_ASSESSED')

    def test_unbound_counterevidence_and_exception_cannot_bypass_gate(self):
        for extra in ({'counterevidence': True}, {'exception_applies': True}):
            self.assertEqual(self.compare(45, [self.h.fact_cite], **extra)['reason'], 'fact_value_unbound')
