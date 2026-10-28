"""Synthetic CSV receipt -> normalizer -> SQLite joins -> seven detectors.

No product imports or ambient changes during discovery. Script bootstrap occurs
only in __main__, before product imports; all IO/process guards are scoped.
"""
import argparse
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / 'tests' / 'fixtures' / 'fc9-rows-detector-handoff' / 'cases.json'
DATA = json.loads(FIXTURE.read_text())


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(value):
    return sha(canonical(value).encode())


def producer_json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def source_receipt(label, kind, fields, stamp):
    """Independent inventory per the existing receipt-bound derivative format.

    Never calls evidence.unit, rows helpers or pipeline source construction.
    Field labels are explicit mapping inputs, not inferred column names.
    """
    columns = [('ID', 'event_id', 'str'), ('When', 'event_at', 'str')]
    columns += [(key, key, codec) for key, codec, _ in fields]
    values = [label + '-event', stamp] + [value for _, _, value in fields]
    csv_stream = io.StringIO(newline='')
    writer = csv.writer(csv_stream, delimiter=',', lineterminator='\n')
    writer.writerow([c[0] for c in columns])
    writer.writerow(values)
    csv_bytes = csv_stream.getvalue().encode('utf-8')
    original = sha(csv_bytes)
    parsed = list(csv.reader(io.StringIO(csv_bytes.decode(), newline=''), strict=True))
    if parsed != [[c[0] for c in columns], values] or len(csv_bytes.splitlines()) != 2:
        raise ValueError('fixture must be ordinary comma-delimited single-line rows')
    raw_units = []
    for ordinal, (unit_kind, cells) in enumerate(zip(['csv_header', 'csv_row'], parsed), 1):
        text = producer_json(cells)
        data = {} if ordinal == 1 else {
            'values': cells, 'presence': ['empty' if v == '' else 'redacted_marker'
                if v == '[redacted]' else 'present' for v in cells],
            'duplicate_of_record': None, 'row_sha256': sha(text.encode()),
            'column_mapping': 'positional'}
        raw_units.append({'kind': unit_kind, 'locator': {
            'record': ordinal, 'line_start': ordinal, 'line_end': ordinal},
            'text': text, 'data': data})
    receipt = {'schema': 'format-extraction-v1', 'original_sha256': original,
               'original_changed': False, 'status': 'complete',
               'review_status': 'not_reviewed', 'issues': [], 'pages': [], 'units': raw_units}
    raw = (producer_json(receipt) + '\n').encode()
    receipt_hash = sha(raw)
    import_hash = digest(['receipt-bound-derivative-v1', original, receipt_hash, 'native'])
    sources = []
    expected = []
    for ordinal, unit in enumerate(raw_units, 1):
        artifact = digest(unit)
        uid = digest([import_hash, ordinal, artifact])
        binding = {'unit_id': uid, 'original_sha256': original,
                   'artifact_sha256': artifact, 'locator': copy.deepcopy(unit['locator'])}
        sources.append(binding)
        record = {'original_sha256': original, 'locator': copy.deepcopy(unit['locator']),
                  'record_type': 'other' if ordinal == 1 else kind,
                  'event_id': uid if ordinal == 1 else values[0],
                  'event_at': None if ordinal == 1 else stamp, 'fields': {}}
        if ordinal == 2:
            for key, codec, value in fields:
                if value == '[redacted]':
                    cell = {'state': 'redacted'}
                elif value == '':
                    cell = {'state': 'blank'}
                else:
                    converted = {'str': lambda v: v, 'int': int,
                                 'bool': lambda v: v == 'true', 'json': json.loads}[codec](value)
                    cell = {'state': 'observed', 'value': converted}
                record['fields'][key] = cell
        expected.append({'record': record, 'source': {
            'unit_id': uid, 'artifact_sha256': artifact}})
    mapping = {'schema': 'agency-row-mapping-v1', 'version': 'fc9-explicit-v1',
               'artifact_sha256': original, 'agency_id': DATA['agency'],
               'record_type': kind, 'unit_kind': 'csv_row', 'delimiter': ',',
               'columns': [{'name': n, 'target': t, 'type': c} for n, t, c in columns]}
    pin = {'artifact_sha256': original, 'derived_receipt_sha256': receipt_hash,
           'derived_receipt': raw}
    return pin, sources, mapping, expected


def prepare(spec, variant):
    fields = copy.deepcopy(spec['fields'])
    stamp = spec.get('stamp', DATA['stamp'])
    if variant == 'zero':
        stamp = spec['zero'].get('event_at', stamp)
        for row in fields:
            row[2] = spec['zero'].get(row[0], row[2])
    elif variant == 'redacted':
        for row in fields:
            if row[0] == spec['redact']:
                row[2] = '[redacted]'
    label = spec['name'] + '-' + variant
    parts = [source_receipt(label, spec['record_type'], fields, stamp),
             source_receipt(label + '-policy', 'policy',
                            [('purpose', 'str', 'fictional policy support')], DATA['stamp'])]
    if 'roster' in spec:
        parts.append(source_receipt(label + '-roster', 'training-roster', [
            ('user_id', 'str', 'fictional-user'), ('training_completed_at', 'str',
             spec['roster']['zero' if variant == 'zero' else 'hit'])], DATA['stamp']))
    norm_args = {'run_id': 'fc9-' + label, 'receipts': [p[0] for p in parts],
                 'source_bindings': [s for p in parts for s in p[1]],
                 'mappings': [p[2] for p in parts]}
    expected_units = sorted([u for p in parts for u in p[3]], key=lambda u: u['source']['unit_id'])
    params = copy.deepcopy(spec['parameters'])
    rules = {'version': 'fc9-fictional-pack-v1', 'entries': []}
    if 'holiday_table' in spec:
        table_hash = digest(spec['holiday_table'])
        params['holiday_table_sha256'] = table_hash
        rules['holiday_tables'] = {table_hash: copy.deepcopy(spec['holiday_table'])}
    rule = {'detector': spec['name'], 'agency_id': DATA['agency'], 'rule_id': 'fc9-fictional-rule',
            'version': 'fc9-fictional-v1', 'source_sha256': parts[1][0]['artifact_sha256'],
            'effective_from': '2020-01-01', 'effective_to': '2021-01-01',
            'parameters': params, 'version_verified': True, 'applicability': 'yes'}
    rules['entries'].append(rule)
    return norm_args, expected_units, rules


class HandoffTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        def forbidden(*args, **kwargs):
            raise RuntimeError('offline fixture forbids network/process access')
        stack.enter_context(patch.object(socket.socket, 'connect', forbidden))
        stack.enter_context(patch.object(socket, 'create_connection', forbidden))
        stack.enter_context(patch.object(subprocess, 'Popen', forbidden))
        real_connect = sqlite3.connect
        def memory_only(database, *args, **kwargs):
            if database != ':memory:':
                raise RuntimeError('synthetic in-memory SQLite only')
            return real_connect(database, *args, **kwargs)
        stack.enter_context(patch.object(sqlite3, 'connect', memory_only))
        from campaign_tool.records.understanding import rows
        from campaign_tool.records.detectors import pipeline
        self.rows, self.pipeline = rows, pipeline

    def build(self, spec, variant):
        args, expected_units, rules = prepare(spec, variant)
        before = copy.deepcopy(args)
        normalized = self.rows.normalize(**args)
        self.assertEqual(args, before)
        self.assertEqual(normalized['source_bindings'], args['source_bindings'])
        self.assertEqual(normalized['normalized'], {
            'schema': 'structured-normalization-v1', 'version': 'explicit-csv-text-rows-v1',
            'complete': True, 'source_inventory_sha256': digest(args['source_bindings']),
            'units': expected_units})
        self.assertEqual(len(normalized['diagnostics']), len(expected_units))
        self.assertTrue(all(d['status'] == 'mapped' and d['reasons'] == []
                            for d in normalized['diagnostics']))
        self.assertEqual(normalized['source_receipts'], sorted([
            {k: p[k] for k in ('artifact_sha256', 'derived_receipt_sha256')}
            for p in args['receipts']], key=lambda p: p['artifact_sha256']))
        self.assertEqual(normalized['mapping_sha256'], digest(sorted(args['mappings'],
                         key=lambda m: m['artifact_sha256'])))
        self.assertTrue(normalized['review_required'])
        for key in ('qualified', 'activation_allowed', 'publication_ready'):
            self.assertIs(normalized[key], False)
        con = sqlite3.connect(':memory:')
        self.addCleanup(con.close)
        con.executescript('CREATE TABLE joins(id TEXT, original_sha256 TEXT, status TEXT, '
            'agency_id TEXT, request_id TEXT, evidence TEXT);'
            'CREATE TABLE agencies(id TEXT); CREATE TABLE requests(id TEXT, agency_id TEXT);')
        con.execute('INSERT INTO agencies VALUES(?)', (DATA['agency'],))
        con.execute('INSERT INTO requests VALUES(?,?)', ('fictional-request', DATA['agency']))
        revisions, join_rows = [], []
        for index, pin in enumerate(sorted(args['receipts'], key=lambda p: p['artifact_sha256'])):
            subject = pin['artifact_sha256']
            source = next(s for s in args['source_bindings'] if s['original_sha256'] == subject
                          and s['locator']['record'] == 2)
            proof = {'source_sha256': subject, 'locator': canonical(source['locator'])}
            row = {'id': str(index), 'original_sha256': subject, 'status': 'typed',
                   'agency_id': DATA['agency'], 'request_id': 'fictional-request',
                   'evidence': canonical(proof)}
            con.execute('INSERT INTO joins VALUES(?,?,?,?,?,?)', tuple(row.values()))
            revisions.extend([row, {'agency': [DATA['agency']]},
                              {'request_id': 'fictional-request', 'agency_id': DATA['agency']}])
            join_rows.append({k: row[k] for k in ('original_sha256', 'status', 'agency_id')})
        joins = self.pipeline.canonical_joins(con, args['source_bindings'])
        self.assertEqual(joins.source_bindings_sha256, digest(args['source_bindings']))
        self.assertEqual(joins.rows_json, canonical(join_rows))
        self.assertEqual(joins.revision_sha256, digest(revisions))
        evaluation = {'subject_sha256': args['receipts'][0]['artifact_sha256'],
                      'normalized': normalized['normalized'],
                      'source_bindings': normalized['source_bindings'], 'joins': joins,
                      'rules': rules, 'config': {'as_of': '2020-01-20'}}
        return args, normalized, evaluation

    def check_case(self, spec, variant):
        args, normalized, evaluation = self.build(spec, variant)
        print('FC9_INPUT ' + canonical({'id': spec['name'] + '/' + variant,
            'receipts': [{**p, 'derived_receipt': p['derived_receipt'].decode()} for p in args['receipts']],
            'source_bindings': args['source_bindings'], 'mappings': args['mappings']}))
        before = copy.deepcopy(evaluation)
        output = self.pipeline.evaluate_structured(**evaluation)
        self.assertEqual(evaluation, before)
        self.assertEqual(set(output['structured']), {s['name'] for s in DATA['detectors']})
        self.assertEqual(output['revisions'], {
            'normalization_sha256': digest(normalized['normalized']),
            'source_bindings_sha256': digest(args['source_bindings']),
            'joins_sha256': evaluation['joins'].revision_sha256,
            'rules_sha256': digest(evaluation['rules']), 'config_sha256': digest(evaluation['config'])})
        self.assertEqual(output['result_sha256'], digest({k: v for k, v in output.items() if k != 'result_sha256'}))
        self.assertFalse(output['publication_ready'])
        self.assertEqual(output['stage_promotions'], 0)
        entry = output['structured'][spec['name']]
        blocked = variant == 'redacted'
        self.assertEqual((entry['status'], entry['reason']),
            ('blocked', 'required_context_unresolved') if blocked else
            ('evaluated', 'supplied_context_evaluated'))
        expected_count = len(spec['hit_observations']) if variant == 'hit' else 0
        self.assertEqual(entry['hit_count'], expected_count)
        self.assertEqual(len(entry['hits']), expected_count)
        counts = entry['manifest']['counts']
        self.assertEqual(counts, {'evaluated': 0 if blocked else 1,
            'blocked': 1 if blocked else 0, 'skipped': len(args['source_bindings']) - 1,
            'input_rows': len(args['source_bindings']), 'unique_records': len(args['source_bindings']),
            'eligible': 1, 'duplicate_rows': 0, 'hits': expected_count})
        outcomes = [o for o in entry['manifest']['outcomes'] if o['disposition'] != 'skipped']
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]['reason'], 'field_' + spec['redact'] + '_redacted'
                         if blocked else 'supplied_context_evaluated')
        refs = [{'original_sha256': args['receipts'][0]['artifact_sha256'],
                 'locator': {'record': 2, 'line_start': 2, 'line_end': 2}}]
        if 'roster' in spec:
            refs.append({'original_sha256': args['receipts'][2]['artifact_sha256'],
                         'locator': {'record': 2, 'line_start': 2, 'line_end': 2}})
        refs.sort(key=canonical)
        observations = copy.deepcopy(spec['hit_observations']) if variant == 'hit' else []
        for obs in observations:
            if obs.get('holiday_table_sha256') == '$holiday_table_sha256':
                obs['holiday_table_sha256'] = digest(spec['holiday_table'])
        self.assertEqual(sorted([h['observed_values_private'] for h in entry['hits']], key=canonical),
                         sorted(observations, key=canonical))
        self.assertEqual(sorted(h['signal'] for h in entry['hits']),
                         sorted(spec['signals']) if variant == 'hit' else [])
        for hit in entry['hits']:
            self.assertEqual(hit['evidence'], refs)
            self.assertEqual(hit['severity'], spec['severity'])
            self.assertEqual(hit['rule_fingerprint_sha256'], digest(evaluation['rules']['entries'][0]))
            self.assertEqual(hit['applicability']['rule_source_sha256'], args['receipts'][1]['artifact_sha256'])
            self.assertTrue(hit['triage_only'])
            contributing = [i['record'] for i in normalized['normalized']['units']
                if i['record']['record_type'] in {spec['record_type'], 'training-roster'}]
            fingerprints = [digest([canonical({k: v for k, v in record.items()
                if k not in {'original_sha256', 'locator'}})]) for record in contributing]
            self.assertEqual(hit['semantic_observations_sha256'], digest(sorted(set(fingerprints))))
        print('FC9_CASE ' + canonical({'id': spec['name'] + '/' + variant,
            'status': entry['status'], 'reason': entry['reason'], 'hits': expected_count,
            'sources': len(args['source_bindings']), 'result_sha256': output['result_sha256']}))
        return evaluation, output

    def assert_binding_blocked(self, output):
        for entry in output['structured'].values():
            self.assertEqual(entry['reason'], 'normalized_source_missing_or_changed')
            self.assertEqual(entry['status'], 'blocked')
            self.assertEqual(entry['hits'], [])
            self.assertEqual(entry['hit_count'], 0)

    def test_binding_predicate_negative_control(self):
        spec = next(s for s in DATA['detectors'] if s['name'] == 'retention-over-policy')
        args, normalized, evaluation = self.build(spec, 'hit')
        baseline = self.pipeline.evaluate_structured(**evaluation)
        self.assertEqual(baseline['structured'][spec['name']]['hit_count'], 1)
        receipts_before, inventory_before = copy.deepcopy(args['receipts']), copy.deepcopy(args['source_bindings'])
        changed = copy.deepcopy(evaluation)
        item = next(i for i in changed['normalized']['units'] if i['record']['record_type'] == 'retention-setting')
        item['source']['artifact_sha256'] = sha(b'fictional changed binding only')
        blocked = self.pipeline.evaluate_structured(**changed)
        self.assert_binding_blocked(blocked)
        original_require = self.pipeline.require
        def bypass_binding_only(condition, reason):
            if reason != 'normalized_source_missing_or_changed':
                original_require(condition, reason)
        with patch.object(self.pipeline, 'require', bypass_binding_only):
            bypassed = self.pipeline.evaluate_structured(**changed)
            self.assertEqual(bypassed['structured'][spec['name']]['hit_count'], 1)
            with self.assertRaisesRegex(AssertionError, 'normalized_source_missing_or_changed'):
                self.assert_binding_blocked(bypassed)
        self.assert_binding_blocked(self.pipeline.evaluate_structured(**changed))
        self.assertEqual(self.pipeline.evaluate_structured(**evaluation), baseline)
        self.assertEqual(args['receipts'], receipts_before)
        self.assertEqual(args['source_bindings'], inventory_before)
        print('FC9_CONTROL ' + canonical({'injected': 'one normalized source artifact binding changed',
            'baseline': baseline['structured'][spec['name']], 'injected_output': blocked,
            'predicate_bypassed': bypassed['structured'][spec['name']],
            'binding_assertion_failed_as_expected': True, 'final_candidate_output': baseline}))

    def test_malformed_integer_stays_unresolved(self):
        spec = next(s for s in DATA['detectors'] if s['name'] == 'retention-over-policy')
        args, _, rules = prepare(spec, 'hit')
        # Integer text cannot silently become a boolean under an explicit codec.
        args['mappings'][0]['columns'][-1]['type'] = 'bool'
        normalized = self.rows.normalize(**args)
        self.assertFalse(normalized['normalized']['complete'])
        self.assertEqual({r for d in normalized['diagnostics'] for r in d['reasons']}, {'cell_type_invalid'})
        output = self.pipeline.evaluate_structured(subject_sha256=args['receipts'][0]['artifact_sha256'],
            normalized=normalized['normalized'], source_bindings=args['source_bindings'],
            rules=rules, config={})
        self.assertTrue(all(e['reason'] == 'normalization_incomplete' and e['hits'] == []
                            for e in output['structured'].values()))


def case_method(spec, variant):
    def test(self):
        self.check_case(spec, variant)
    return test


for _spec in DATA['detectors']:
    for _variant in DATA['variants']:
        setattr(HandoffTests, 'test_' + _spec['name'].replace('-', '_') + '_' + _variant,
                case_method(_spec, _variant))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--temporary-root', required=True, type=Path)
    opts, remaining = parser.parse_known_args()
    if not opts.temporary_root.is_absolute() or not opts.temporary_root.is_dir():
        parser.error('temporary-root must be an existing absolute invocation directory')
    tempfile.tempdir = str(opts.temporary_root)
    sys.path.insert(0, str(REPO))
    unittest.main(argv=[sys.argv[0], *remaining])
