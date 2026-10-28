"""Synthetic public-wrapper tests, not native/private workbook qualification.

The double uses the documented inspect/account shapes from WorkbookPolicyTests.
No protected helper bytes, paths, digests or qualification authority are loaded.
Imports and normal unittest discovery install no process-global safety hooks.
"""
import copy
import io
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import PropertyMock, patch

from campaign_tool.records import workbook_policy as policy
from campaign_tool.records.intake import folder

REPO = Path(__file__).resolve().parents[2]
CASE_IDS = (
    'valid_sheet_and_row_units',
    'list_dict_null_boolean_numeric_empty_sheet',
    'unknown_string_sheet',
    'unicode_and_hidden_valid_names',
    'input_unchanged_and_existing_binding_gates',
    'typed_error_before_membership',
)


class WorkbookSheetShapeTests(unittest.TestCase):
    def setUp(self):
        self.binding = {
            'policy': policy.POLICY,
            'component_sha256': policy.COMPONENT_SHA256,
            'scope_contract': 'plain-workbook-native-rows-v3',
            'accounting_schema': 'workbook-native-accounting-v1',
        }
        self.inspection = {
            'contract': self.binding['scope_contract'], 'package_parts': 1,
            'scope': 'all_native_sheet_rows_only', 'gaps': [],
            'visual_review': False, 'semantic_acceptance': False,
        }
        self.accounting = {
            'schema': self.binding['accounting_schema'],
            'denominator': {'declared_sheets': 1, 'accounted_sheets': 1,
                            'serialized_rows': 1, 'serialized_cells': 1, 'formula_cells': 0},
            'sheets': [{'name': 'Synthetic', 'sheet_id': '1',
                        'part': 'xl/worksheets/sheet1.xml', 'state': 'visible',
                        'serialized_rows': 1, 'serialized_cells': 1,
                        'explicit_blank_cells': 0, 'formula_cells': 0}],
            'gaps': [], 'coverage': 'all_serialized_native_rows_and_cells',
            'sparse_grid_blanks_materialized': False, 'formula_execution': False,
            'visual_review': False, 'semantic_acceptance': False,
        }
        values = [{'cell': 'A1', 'value': 'Fictional value', 'type': 's'}]
        self.units = [
            {'kind': 'workbook_sheet', 'locator': {'sheet': 'Synthetic'},
             'text': 'Synthetic', 'data': {'state': 'visible'}},
            {'kind': 'workbook_row', 'locator': {'sheet': 'Synthetic', 'row': 1},
             'text': folder.js(values), 'data': {'cells': values}},
        ]
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as package:
            package.writestr('xl/workbook.xml', b'<synthetic/>')
        self.raw = stream.getvalue()
        self.component = types.SimpleNamespace(
            inspect=lambda package: self.inspection,
            account=lambda package: self.accounting,
        )
        # Only the binding property is replaced, scoped to this individual test.
        # NativeWorkbookPolicy.qualify itself is the actual production wrapper.
        binding_patch = patch.object(policy.NativeWorkbookPolicy, 'binding',
                                     new_callable=PropertyMock, return_value=self.binding)
        binding_patch.start()
        self.addCleanup(binding_patch.stop)
        self.wrapper = policy.NativeWorkbookPolicy(policy._TOKEN, None, self.component)

    def typed_error(self, units, reason):
        before = copy.deepcopy(units)
        with self.assertRaises(policy.enrollment.ExtractionBindingError) as caught:
            self.wrapper.qualify(self.raw, units)
        self.assertIs(type(caught.exception), policy.enrollment.ExtractionBindingError)
        self.assertEqual(str(caught.exception), reason)
        self.assertEqual(units, before)

    def positive(self):
        before = copy.deepcopy((self.units, self.inspection, self.accounting))
        result = self.wrapper.qualify(self.raw, self.units)
        self.assertEqual(set(result), {'binding', 'native_accounting',
                                      'visual_review_pending', 'semantic_acceptance'})
        self.assertIs(result['binding'], self.binding)
        self.assertIs(result['native_accounting'], self.accounting)
        self.assertIs(result['visual_review_pending'], True)
        self.assertIs(result['semantic_acceptance'], False)
        self.assertEqual((self.units, self.inspection, self.accounting), before)
        return result

    def test_valid_sheet_and_row_units(self):
        result = self.positive()
        self.assertEqual(result['native_accounting']['denominator'], {
            'declared_sheets': 1, 'accounted_sheets': 1, 'serialized_rows': 1,
            'serialized_cells': 1, 'formula_cells': 0})
        self.assertEqual(self.units[1]['text'], folder.js(self.units[1]['data']['cells']))
        # Legitimate empty serialized row and empty sheet remain supported.
        self.accounting['denominator']['serialized_cells'] = 0
        self.accounting['sheets'][0]['serialized_cells'] = 0
        self.units[1].update(text='[]', data={'cells': []})
        self.positive()
        self.accounting['denominator']['serialized_rows'] = 0
        self.accounting['sheets'][0]['serialized_rows'] = 0
        self.units.pop()
        self.positive()

    def test_list_dict_null_boolean_numeric_empty_sheet(self):
        for value in ([], ['Synthetic'], {}, {'sheet': 'Synthetic'}, None,
                      False, True, 0, 1, 1.5, ''):
            for index in (0, 1):
                with self.subTest(value=value, kind=self.units[index]['kind']):
                    units = copy.deepcopy(self.units)
                    units[index]['locator']['sheet'] = value
                    self.typed_error(units, 'workbook_unit_sheet_unbound')

    def test_unknown_string_sheet(self):
        for index in (0, 1):
            units = copy.deepcopy(self.units)
            units[index]['locator']['sheet'] = 'Unaccounted'
            self.typed_error(units, 'workbook_unit_sheet_unbound')
        for index in (0, 1):
            units = copy.deepcopy(self.units)
            units[index]['locator'].pop('sheet')
            self.typed_error(units, 'workbook_unit_sheet_unbound')

    def test_unicode_and_hidden_valid_names(self):
        for name in ('Résumé 海', ' Sheet with spaces ', 'Σύνθεση'):
            for state in ('visible', 'hidden', 'veryHidden'):
                with self.subTest(name=name, state=state):
                    self.accounting['sheets'][0].update(name=name, state=state)
                    for unit in self.units:
                        unit['locator']['sheet'] = name
                    self.units[0].update(text=name, data={'state': state})
                    result = self.positive()
                    self.assertEqual(result['native_accounting']['sheets'][0]['name'], name)
                    self.assertEqual(result['native_accounting']['sheets'][0]['state'], state)

    def test_input_unchanged_and_existing_binding_gates(self):
        self.positive()
        changes = (
            (lambda u: u[0].update(locator=[]), 'workbook_unit_shape'),
            (lambda u: u[0]['data'].update(state='hidden'), 'workbook_sheet_unit_mismatch'),
            (lambda u: u[1]['locator'].update(row=True), 'workbook_row_unit_mismatch'),
            (lambda u: u[1].update(text='changed'), 'workbook_row_text_mismatch'),
            (lambda u: u[1]['data']['cells'][0].update(type='f'), 'workbook_cell_unqualified'),
            (lambda u: u[1]['data']['cells'][0].update(cell='A2'), 'workbook_cell_locator_unbound'),
            (lambda u: u.pop(), 'workbook_serialized_denominator_mismatch'),
            (lambda u: u.append(copy.deepcopy(u[1])), 'workbook_row_unit_mismatch'),
        )
        for mutate, reason in changes:
            with self.subTest(reason=reason):
                units = copy.deepcopy(self.units)
                mutate(units)
                if reason in ('workbook_cell_unqualified', 'workbook_cell_locator_unbound'):
                    units[1]['text'] = folder.js(units[1]['data']['cells'])
                self.typed_error(units, reason)
        with patch.dict(self.accounting['denominator'], serialized_cells=2):
            self.typed_error(self.units, 'workbook_accounting_totals_mismatch')
        with patch.dict(self.accounting['denominator'], formula_cells=1):
            self.typed_error(self.units, 'workbook_denominator_or_formula_hold')
        with patch.dict(self.inspection, visual_review=True):
            self.typed_error(self.units, 'workbook_native_qualification_hold')
        with patch.dict(self.accounting, semantic_acceptance=True):
            self.typed_error(self.units, 'workbook_accounting_unqualified')
        self.positive()

    def test_typed_error_before_membership(self):
        class HashTripwire(str):
            def __hash__(self):
                raise RuntimeError('sheet membership reached')
        for value in (HashTripwire('Synthetic'), [], {}):
            for index in (0, 1):
                with self.subTest(value_type=type(value).__name__, index=index):
                    units = copy.deepcopy(self.units)
                    units[index]['locator']['sheet'] = value
                    self.typed_error(units, 'workbook_unit_sheet_unbound')
