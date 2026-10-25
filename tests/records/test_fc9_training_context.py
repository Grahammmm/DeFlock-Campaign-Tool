"""Synthetic same-user training-context integration; imports are discovery-inert."""
import argparse
import builtins
import copy
from datetime import datetime
import hashlib
import itertools
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / 'tests' / 'fixtures' / 'fc9-training-context' / 'cases.json'
CASE_IDS = (
    'before_equal_after', 'before_after', 'equal_after', 'after_later',
    'conflicting_duplicate', 'missing_key', 'missing_state', 'null_value',
    'redacted_state', 'redacted_literal', 'blank_value', 'invalid_timestamp',
    'invalid_type', 'timezone_missing', 'other_user_invalid',
)


def fixture():
    return json.loads(FIXTURE.read_text())


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def values(case):
    data = fixture()
    def unit(identity, row, kind, fields):
        return {'event_id': identity, 'original_sha256': sha('synthetic-fc9-' + identity),
                'locator': {'row': row}, 'record_type': kind,
                'event_at': data['search_at'], 'fields': fields}
    search = unit('search-1', 1, 'search-log', {'user_id': data['user']})
    rosters = [unit('roster-' + name, row, 'training-roster',
                    {'user_id': data['user'], 'training_completed_at': data['completion_values'][name]})
               for row, name in enumerate(case['rosters'], 10)]
    extra = case.get('extra')
    if extra == 'conflict':
        other = copy.deepcopy(rosters[-1])
        other['original_sha256'] = sha('synthetic-fc9-conflicting-source')
        other['locator'] = {'row': 99}
        other['fields']['training_completed_at'] = data['completion_values']['later']
        rosters.append(other)
    elif extra:
        fields = {'user_id': data['user']}
        malformed = {
            'missing_state': {'state': 'missing'}, 'null_value': None,
            'redacted_state': {'state': 'redacted'}, 'redacted_literal': '[redacted]',
            'blank_value': '  ', 'invalid_timestamp': 'not-a-timestamp',
            'invalid_type': 17, 'timezone_missing': '2020-06-15T13:00:00',
            'other_user_invalid': 'not-a-timestamp',
        }
        if extra != 'missing_key':
            fields['training_completed_at'] = malformed[extra]
        if extra == 'other_user_invalid':
            fields['user_id'] = 'synthetic-fc9-unrelated-user'
        rosters.append(unit('roster-extra', 99, 'training-roster', fields))
    units = [search, *rosters]
    joins = [{'original_sha256': item['original_sha256'], 'agency_id': data['agency'], 'status': 'typed'}
             for item in units]
    rules = {'version': 'synthetic-fc9-pack-v1', 'entries': [{
        'detector': 'search-before-training', 'agency_id': data['agency'],
        'rule_id': 'synthetic-fc9-rule', 'version': 'v1',
        'source_sha256': sha('synthetic-fc9-policy'), 'effective_from': '2020-01-01',
        'effective_to': '2021-01-01', 'parameters': {},
    }]}
    return units, joins, rules, {}


def reference(unit):
    # Independent literal projection, not the detector's reference helper.
    return {key: copy.deepcopy(unit[key]) for key in ('original_sha256', 'locator')}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


class TrainingContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Deferred until test execution; ordinary discovery has no product import.
        from campaign_tool.records.detectors import core
        cls.core = core

    def case(self, identity):
        return next(item for item in fixture()['cases'] if item['id'] == identity)

    def observe(self, case, supplied, label):
        before = copy.deepcopy(supplied)
        result = self.core.evaluate('search-before-training', *supplied)
        self.assertEqual(supplied, before, 'caller input must remain unchanged')
        search_ref = reference(supplied[0][0])
        outcomes = [row for row in result['manifest']['outcomes'] if row['evidence'] == [search_ref]]
        self.assertEqual(len(outcomes), 1, 'exact search outcome required')
        print(json.dumps({'kind': 'observation', 'case': case['id'], 'label': label,
                          'order': [u['event_id'] for u in supplied[0][1:]],
                          'result': result}, sort_keys=True), flush=True)
        return result, outcomes[0]

    def check(self, case, supplied, result, outcome):
        if 'reason' in case:
            self.assertEqual(outcome['disposition'], 'blocked', 'required blocked disposition')
            self.assertEqual(outcome['reason'], case['reason'])
            self.assertEqual(result['hits'], [])
            expected_evaluated, expected_blocked = 0, 1
        else:
            self.assertEqual(outcome['disposition'], 'evaluated')
            self.assertEqual(outcome['reason'], 'supplied_context_evaluated')
            data = fixture()
            # Freeze earliest independently of min() and the evaluated product result.
            completions = [datetime.fromisoformat(data['completion_values'][name]) for name in case['rosters']]
            earliest = datetime.fromisoformat(case['earliest'])
            self.assertIn(earliest, completions)
            self.assertTrue(all(earliest <= value for value in completions))
            expected_hits = int(datetime.fromisoformat(data['search_at']) < earliest)
            self.assertEqual(expected_hits, case['hits'])
            self.assertEqual(len(result['hits']), expected_hits, 'strict earliest comparison')
            if expected_hits:
                hit = result['hits'][0]
                self.assertEqual(hit['observed_values_private'], {
                    'event_at': data['search_at'], 'earliest_supplied_completion': case['earliest'],
                    'user_id': data['user']})
                supports = [reference(u) for u in supplied[0]
                            if u['fields']['user_id'] == data['user']]
                self.assertEqual(hit['evidence'], sorted(supports, key=canonical))
                self.assertEqual(hit['signal'], 'search_precedes_supplied_training_completion')
                self.assertEqual(hit['applicability']['agency_id'], data['agency'])
                self.assertEqual(hit['severity'], 3)
                self.assertTrue(hit['triage_only'])
                self.assertFalse(hit['publication_ready'])
            expected_evaluated, expected_blocked = 1, 0
        counts = result['manifest']['counts']
        self.assertEqual(counts['eligible'], 1)
        self.assertEqual(counts['evaluated'], expected_evaluated)
        self.assertEqual(counts['blocked'], expected_blocked)
        self.assertEqual(counts['hits'], len(result['hits']))
        self.assertEqual(counts['input_rows'], len(supplied[0]))
        self.assertEqual(counts['unique_records'], len(supplied[0]) - int(case.get('extra') == 'conflict'))
        self.assertEqual(counts['duplicate_rows'], int(case.get('extra') == 'conflict'))
        self.assertEqual(counts['skipped'], counts['unique_records'] - 1)
        self.assertTrue(all(row['reason'] == 'not_detector_input_type'
                            for row in result['manifest']['outcomes'] if row != outcome))
        self.assertEqual(result['manifest']['stage_promotions'], 0)
        self.assertFalse(result['manifest']['publication_ready'])

    def run_case(self, identity):
        case = self.case(identity)
        supplied = values(case)
        baseline = None
        for order in itertools.permutations(supplied[0][1:]):
            current = ([supplied[0][0], *order], *supplied[1:])
            with self.subTest(case=identity, order=[u['original_sha256'] for u in order]):
                result, outcome = self.observe(case, current, 'ordinary')
                self.check(case, current, result, outcome)
                # Input-order hash is legitimately different; all semantic outputs are not.
                stable = copy.deepcopy(result)
                stable['manifest'].pop('input_sha256')
                stable['manifest'].pop('manifest_sha256')
                if baseline is None:
                    baseline = stable
                self.assertEqual(stable, baseline)

    def test_fixture_inventory(self):
        data = fixture()
        self.assertTrue(data['synthetic_only'])
        self.assertEqual(data['schema'], 'fc9-training-context/v1')
        self.assertEqual(tuple(c['id'] for c in data['cases']), CASE_IDS)
        self.assertEqual(data['controls'], ['latest_instead_of_earliest', 'skip_matching_validity'])

    def test_negative_control_latest_instead_of_earliest(self):
        case = self.case('before_after')
        supplied = values(case)
        self.check(case, supplied, *self.observe(case, supplied, 'baseline'))
        # Only the unqualified min seam inside core is changed, for this invocation.
        with patch.object(self.core, 'min', builtins.max, create=True):
            result, outcome = self.observe(case, supplied, 'injected_latest_instead_of_earliest')
            self.assertEqual(outcome['disposition'], 'evaluated')
            self.assertEqual(len(result['hits']), 1)
            self.assertEqual(result['hits'][0]['observed_values_private']['earliest_supplied_completion'],
                             fixture()['completion_values']['after'])
            with self.assertRaisesRegex(AssertionError, 'strict earliest comparison') as killed:
                self.check(case, supplied, result, outcome)
            print(json.dumps({'kind': 'killed_control', 'control': 'latest_instead_of_earliest',
                              'assertion': str(killed.exception)}, sort_keys=True), flush=True)
        self.assertNotIn('min', vars(self.core))
        self.check(case, supplied, *self.observe(case, supplied, 'restored'))

    def test_negative_control_skip_matching_validity(self):
        case = self.case('conflicting_duplicate')
        supplied = values(case)
        self.check(case, supplied, *self.observe(case, supplied, 'baseline'))
        original = self.core.need
        calls = []
        def skip_only_matching_validity(value, reason):
            if reason == 'training_context_invalid':
                calls.append(value)
                return None
            return original(value, reason)
        with patch.object(self.core, 'need', skip_only_matching_validity):
            result, outcome = self.observe(case, supplied, 'injected_skip_matching_validity')
            self.assertIn(False, calls)
            self.assertEqual(outcome['disposition'], 'evaluated')
            self.assertEqual(result['hits'], [])
            with self.assertRaisesRegex(AssertionError, 'required blocked disposition') as killed:
                self.check(case, supplied, result, outcome)
            print(json.dumps({'kind': 'killed_control', 'control': 'skip_matching_validity',
                              'assertion': str(killed.exception)}, sort_keys=True), flush=True)
        self.assertIs(self.core.need, original)
        self.check(case, supplied, *self.observe(case, supplied, 'restored'))


def make_test(identity):
    def test(self):
        self.run_case(identity)
    return test


for _identity in CASE_IDS:
    setattr(TrainingContextTests, 'test_case_' + _identity, make_test(_identity))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--temporary-root', required=True, type=Path)
    args, remaining = parser.parse_known_args()
    args.temporary_root.mkdir(parents=True, exist_ok=False)
    tempfile.tempdir = str(args.temporary_root.resolve())
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    unittest.main(argv=[sys.argv[0], *remaining])


if __name__ == '__main__':
    main()
