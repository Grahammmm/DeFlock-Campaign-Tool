"""Synthetic, inert-on-discovery qualification of partial-population suppression."""
import argparse
import copy
from contextlib import contextmanager, ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / 'tests' / 'fixtures' / 'fc10-partial-hit-suppression' / 'cases.json'
DETECTOR_NAMES = ('search-before-training', 'purpose-quality', 'external-sharing',
                  'retention-over-policy', 'audit-gap', 'cpra-deadline', 'volume-anomaly')
DIMENSIONS = ('seven_positive_plus_blocked', 'seven_zero_plus_blocked', 'clean_positive',
              'clean_zero', 'input_order_reverse', 'supporting_training_row_preserved',
              'inapplicable_known_type_control', 'precise_manifest_counts', 'immutable_inputs')


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


@contextmanager
def offline_scope():
    """Scoped defense for this reviewed pure call graph, not a hostile sandbox."""
    connect = sqlite3.connect

    def memory_only(database, *args, **kwargs):
        require(database == ':memory:', 'offline fixture requires memory SQLite')
        return connect(database, *args, **kwargs)

    def unavailable(*args, **kwargs):
        raise RuntimeError('offline call graph attempted an external operation')

    with ExitStack() as stack:
        for owner, name in ((socket, 'socket'), (socket, 'create_connection'),
                            (socket, 'getaddrinfo'), (subprocess, 'Popen'), (os, 'system')):
            stack.enter_context(patch.object(owner, name, unavailable))
        stack.enter_context(patch.object(sqlite3, 'connect', memory_only))
        stack.enter_context(patch.object(sys, 'dont_write_bytecode', True))
        yield


def spec_for(name):
    data = json.loads(FIXTURE.read_text())
    require(data['schema'] == 'fc10-partial-hit-suppression/v1', 'fixture schema')
    require(data['authority'] == 'test_only', 'fixture authority')
    require(tuple(data['dimensions']) == DIMENSIONS, 'frozen dimensions')
    require(tuple(row['name'] for row in data['detectors']) == DETECTOR_NAMES, 'seven detectors')
    return data, next(row for row in data['detectors'] if row['name'] == name)


def h(text):
    return hashlib.sha256(text.encode()).hexdigest()


def population(name, *, zero=False, mixed=True, reverse=False, inapplicable=False):
    from campaign_tool.records.detectors.core import canonical, digest
    from campaign_tool.records.detectors.pipeline import NORMALIZED_SCHEMA, canonical_joins
    data, spec = spec_for(name)
    subject = h('FC10 synthetic subject ' + name)
    primary = {'original_sha256': subject, 'locator': {'row': 1}, 'event_id': 'qualified-event',
               'event_at': spec.get('event_at', data['event_at']),
               'record_type': spec['record_type'], 'fields': copy.deepcopy(spec['fields'])}
    if zero:
        primary['fields'].update(copy.deepcopy(spec.get('zero_fields', {})))
        primary['event_at'] = spec.get('zero_event_at', primary['event_at'])
    units = [primary]
    if mixed:
        invalid = copy.deepcopy(primary)
        invalid.update(event_id='blocked-event', locator={'row': 2})
        del invalid['fields'][spec['missing_field']]
        units.append(invalid)
    if name == 'search-before-training':
        units.append({'original_sha256': h('FC10 synthetic training roster'), 'locator': {'row': 9},
                      'event_id': 'training-support', 'record_type': 'training-roster',
                      'event_at': data['event_at'], 'fields': {'user_id': 'synthetic-user',
                      'training_completed_at': spec['zero_training_completed_at'] if zero
                      else spec['training_completed_at']}})
    policy_hash = h('FC10 synthetic supplied policy')
    units.append({'original_sha256': policy_hash, 'locator': {'page': 1},
                  'event_id': 'policy-support', 'record_type': 'policy', 'fields': {}})
    if inapplicable:
        for unit in units:
            if unit['original_sha256'] == subject:
                unit['record_type'] = 'other'
    rule = {'detector': name, 'agency_id': data['agency'], 'rule_id': 'synthetic-rule',
            'version': 'synthetic-v1', 'source_sha256': policy_hash, 'effective_from': '2020-01-01',
            'effective_to': '2021-01-01', 'parameters': copy.deepcopy(spec['parameters']),
            'version_verified': True, 'applicability': 'yes'}
    rules = {'version': 'synthetic-pack-v1', 'entries': [rule]}
    if 'holiday_table' in spec:
        table = copy.deepcopy(spec['holiday_table'])
        key = digest(table)
        rules['holiday_tables'] = {key: table}
        rule['parameters']['holiday_table_sha256'] = key
    sources, mapped = [], []
    for unit in units:
        source = {'unit_id': unit['event_id'], 'original_sha256': unit['original_sha256'],
                  'artifact_sha256': digest(unit), 'locator': copy.deepcopy(unit['locator'])}
        sources.append(source)
        mapped.append({'record': unit, 'source': {key: source[key] for key in ('unit_id', 'artifact_sha256')}})
    if reverse:
        sources.reverse()
        mapped.reverse()
    con = sqlite3.connect(':memory:')
    try:
        con.executescript('CREATE TABLE joins(id TEXT, original_sha256 TEXT, status TEXT, '
                          'agency_id TEXT, request_id TEXT, evidence TEXT); '
                          'CREATE TABLE agencies(id TEXT); CREATE TABLE requests(id TEXT, agency_id TEXT);')
        con.execute('INSERT INTO agencies VALUES(?)', (data['agency'],))
        for index, subject_hash in enumerate(sorted({s['original_sha256'] for s in sources})):
            source = next(s for s in sources if s['original_sha256'] == subject_hash)
            proof = {'source_sha256': subject_hash, 'locator': canonical(source['locator'])}
            con.execute('INSERT INTO joins VALUES(?,?,?,?,?,?)',
                        (str(index), subject_hash, 'typed', data['agency'], None, json.dumps(proof)))
        joins = canonical_joins(con, sources)
    finally:
        con.close()
    return {'subject_sha256': subject, 'normalized': {'schema': NORMALIZED_SCHEMA,
            'version': 'synthetic-mapper-v1', 'complete': True,
            'source_inventory_sha256': digest(sources), 'units': mapped},
            'source_bindings': sources, 'joins': joins, 'rules': rules, 'config': {'as_of': data['as_of']}}, spec


class PartialHitSuppressionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(offline_scope())
        from campaign_tool.records.detectors import pipeline
        self.pipeline = pipeline

    def inspect_population(self, name, *, zero=False, mixed=True, reverse=False):
        from campaign_tool.records.detectors.core import canonical, digest, evaluate
        args, spec = population(name, zero=zero, mixed=mixed, reverse=reverse)
        before = copy.deepcopy(args)
        output = self.pipeline.evaluate_structured(**args)
        self.assertEqual(args, before, 'adapter changed caller-owned inputs')
        units = [item['record'] for item in args['normalized']['units']]
        private = evaluate(name, units, json.loads(args['joins'].rows_json), args['rules'], args['config'])
        entry = output['structured'][name]
        self.assertEqual(tuple(output['structured']), tuple(sorted(DETECTOR_NAMES)))
        self.assertTrue(all(output['revisions'].values()), 'context failed before per-event gate')
        self.assertFalse(output['publication_ready'])
        self.assertEqual(output['stage_promotions'], 0)
        self.assertEqual(output['acceptance'], 'supplied_evidence_triage_only')
        self.assertEqual(entry['manifest'], private['manifest'])
        expected_hits = 0 if zero else spec['positive_hits']
        support = 2 if name == 'search-before-training' else 1
        total = 1 + int(mixed) + support
        counts = entry['manifest']['counts']
        self.assertEqual(counts, {'evaluated': 1, 'blocked': int(mixed), 'skipped': support,
                         'input_rows': total, 'unique_records': total, 'eligible': 1 + int(mixed),
                         'duplicate_rows': 0, 'hits': expected_hits})
        self.assertEqual(len(private['hits']), expected_hits)
        self.assertEqual(len(entry['manifest']['hit_keys']), expected_hits)
        self.assertEqual(entry['manifest']['hit_keys'], [hit['dedupe_key'] for hit in private['hits']])
        self.assertEqual(entry['status'], 'blocked' if mixed else 'evaluated')
        self.assertEqual(entry['reason'], 'required_context_unresolved' if mixed else 'supplied_context_evaluated')
        self.assertFalse(entry['publication_ready'])
        outcomes = {row['event_key']: row for row in entry['manifest']['outcomes']}
        data, _ = spec_for(name)
        for unit in units:
            key = digest(canonical([data['agency'], unit['record_type'], unit['event_id']]))
            row = outcomes[key]
            self.assertEqual(row['evidence'], [{'original_sha256': unit['original_sha256'], 'locator': unit['locator']}])
            self.assertEqual(row['duplicate_rows'], 0)
            if unit['event_id'] == 'blocked-event':
                self.assertEqual((row['disposition'], row['reason']), ('blocked', spec['blocked_reason']))
            elif unit['event_id'] == 'qualified-event':
                self.assertEqual((row['disposition'], row['reason']), ('evaluated', 'supplied_context_evaluated'))
            else:
                self.assertEqual((row['disposition'], row['reason']), ('skipped', 'not_detector_input_type'))
        for hit in private['hits']:
            self.assertEqual(hit['signal'], spec['signal'])
            self.assertEqual(hit['applicability']['agency_id'], data['agency'])
            self.assertTrue(hit['triage_only'])
            self.assertFalse(hit['publication_ready'])
            self.assertNotIn({'original_sha256': args['subject_sha256'], 'locator': {'row': 2}}, hit['evidence'])
            self.assertIn({'original_sha256': args['subject_sha256'], 'locator': {'row': 1}}, hit['evidence'])
        return args, entry, private

    def check_population(self, name, **options):
        args, entry, private = self.inspect_population(name, **options)
        if options.get('mixed', True):
            self.assertEqual(entry['hits'], [], 'NO_EXPOSED_HITS: blocked partial population')
            self.assertEqual(entry['hit_count'], 0)
            self.assertEqual(entry['suppressed_hit_count'], len(private['hits']))
        else:
            self.assertEqual(entry['hits'], private['hits'])
            self.assertEqual(entry['hit_count'], len(private['hits']))
            self.assertEqual(entry['suppressed_hit_count'], 0)
        return args, entry, private

    def exercise(self, name, dimension):
        if dimension == 'seven_zero_plus_blocked':
            self.check_population(name, zero=True)
        elif dimension in ('clean_positive', 'clean_zero'):
            self.check_population(name, mixed=False, zero=dimension == 'clean_zero')
        elif dimension == 'input_order_reverse':
            for zero in (False, True):
                _, a, pa = self.check_population(name, zero=zero)
                _, b, pb = self.check_population(name, zero=zero, reverse=True)
                self.assertEqual(a['manifest']['counts'], b['manifest']['counts'])
                self.assertEqual(a['manifest']['outcomes'], b['manifest']['outcomes'])
                self.assertEqual(pa['hits'], pb['hits'])
        elif dimension == 'inapplicable_known_type_control':
            args, _ = population(name, inapplicable=True)
            entry = self.pipeline.evaluate_structured(**args)['structured'][name]
            size = len(args['normalized']['units'])
            self.assertEqual((entry['status'], entry['reason']),
                             ('inapplicable', 'no_matching_type_in_complete_normalization'))
            self.assertEqual(entry['manifest']['counts'], {'input_rows': size, 'unique_records': size,
                'duplicate_rows': 0, 'evaluated': 0, 'blocked': 0, 'eligible': 0, 'skipped': size, 'hits': 0})
            self.assertEqual((entry['hits'], entry['hit_count'], entry['suppressed_hit_count']), ([], 0, 0))
        elif dimension == 'immutable_inputs':
            args, entry, _ = self.check_population(name)
            before = copy.deepcopy(args)
            entry['manifest']['outcomes'][0]['evidence'][0]['locator']['changed'] = 99
            self.assertEqual(args, before)
            args2, clean, _ = self.check_population(name, mixed=False)
            saved = copy.deepcopy(args2)
            clean['hits'][0]['evidence'][0]['locator']['changed'] = 99
            self.assertEqual(args2, saved)
        else:
            # Both the primary matrix and the explicit precise-count dimension use
            # full dictionary equality, exact blocker identity and private hit keys.
            _, mixed, private = self.check_population(name)
            _, clean, clean_private = self.check_population(name, mixed=False)
            self.assertEqual(private['hits'], clean_private['hits'])
            self.assertEqual(mixed['suppressed_hit_count'], clean['hit_count'])

    def test_supporting_training_row_preserved(self):
        for zero in (False, True):
            args, entry, private = self.check_population('search-before-training', zero=zero)
            roster = next(item for item in args['normalized']['units']
                          if item['record']['record_type'] == 'training-roster')
            self.assertNotEqual(roster['record']['original_sha256'], args['subject_sha256'])
            self.assertIn(roster['source']['unit_id'], [s['unit_id'] for s in args['source_bindings']])
            self.assertIn(roster['record']['original_sha256'], args['joins'].rows_json)
            self.assertEqual(entry['manifest']['counts']['skipped'], 2)
            if not zero:
                self.assertIn({'original_sha256': roster['record']['original_sha256'], 'locator': {'row': 9}},
                              private['hits'][0]['evidence'])

    def mutation(self, name):
        _, baseline, _ = self.check_population(name)
        original = self.pipeline._entry

        def expose_blocked(status, reason, result=None):
            entry = original(status, reason, result)
            if status == 'blocked' and result is not None:
                entry.update(hits=result['hits'], hit_count=len(result['hits']), suppressed_hit_count=0)
            return entry

        log = io.StringIO()
        with patch.object(self.pipeline, '_entry', expose_blocked):
            _, injected, _ = self.inspect_population(name)
            self.assertEqual(injected['manifest'], baseline['manifest'])
            deciding = unittest.FunctionTestCase(lambda: self.check_population(name),
                                                 description='NO_EXPOSED_HITS ' + name)
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(deciding)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.skipped, [])
        self.assertIn('NO_EXPOSED_HITS: blocked partial population', result.failures[0][1])
        self.assertIs(self.pipeline._entry, original)
        _, restored, _ = self.check_population(name)
        self.assertEqual(restored, baseline)
        self.check_population(name, mixed=False)
        print(json.dumps({'control': name, 'unmodified_baseline': baseline, 'injected': injected,
                          'expected_assertion_output': log.getvalue(), 'restored': restored}, sort_keys=True))


def make_test(name, dimension):
    def test(self):
        self.exercise(name, dimension)
    return test


def make_mutation(name):
    def test(self):
        self.mutation(name)
    return test


for _name in DETECTOR_NAMES:
    for _dimension in DIMENSIONS:
        if _dimension != 'supporting_training_row_preserved':
            setattr(PartialHitSuppressionTests, 'test_' + _name.replace('-', '_') + '__' + _dimension,
                    make_test(_name, _dimension))
    setattr(PartialHitSuppressionTests, 'test_' + _name.replace('-', '_') + '__negative_control', make_mutation(_name))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--temp-root', required=True, type=Path)
    args = parser.parse_args()
    require(args.temp_root.is_absolute() and args.temp_root.is_dir()
            and not any(p.is_symlink() for p in (args.temp_root, *args.temp_root.parents)),
            'temporary root must be an existing canonical nonsymlink directory')
    # Script children own their temporary-root setting; ordinary discovery does not.
    tempfile.tempdir = str(args.temp_root.resolve())
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    with offline_scope():
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(PartialHitSuppressionTests)
        ids = [test.id() for test in suite]
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({'testsRun': result.testsRun, 'failures': len(result.failures),
                      'errors': len(result.errors), 'skips': len(result.skipped),
                      'test_ids': ids, 'dimensions': DIMENSIONS}))
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == '__main__':
    raise SystemExit(main())
