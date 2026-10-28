"""Synthetic prepared-envelope identity closure. No installed provider authority.

Ordinary discovery is inert: product imports and offline patches are scoped to
setUp/tearDown. Script children may set their own temporary root before imports.
"""
import argparse
from contextlib import contextmanager, ExitStack
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
from typing import cast
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / 'tests/fixtures/fc10-preparation-revision-closure/cases.json'
# Independent frozen paths: intentionally not derived from preparation.DESCRIPTORS.
MATRIX = (
    ('normalization', 'revision'), ('normalization', 'code_sha256'), ('normalization', 'receipt_sha256'),
    ('support', 'revision'), ('support', 'code_sha256'), ('support', 'receipt_sha256'),
    ('dates', 'revision'), ('dates', 'code_sha256'), ('dates', 'receipt_sha256'),
    ('policy', 'revision'), ('policy', 'code_sha256'), ('policy', 'receipt_sha256'),
    ('provider', 'revision'), ('provider', 'code_sha256'), ('provider', 'receipt_sha256'),
)
DIMENSIONS = (
    'five_by_three_valid_descriptor_changes', 'pinned_model_revision', 'pinned_prompt_revision',
    'disabled_model_control', 'unchanged_replay', 'reconstruct_provider_inputs',
    'stale_source_revision_blocked', 'exact_descriptor_json_retained',
    'evaluation_result_invariant_for_metadata_only_change',
)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


@contextmanager
def offline_scope():
    """Scoped guard for this pure call graph, not a hostile-code sandbox.

    No audit hook survives discovery. File-backed SQLite, processes and sockets
    are disabled before any product import. Only fixture/source reads are used.
    """
    original_connect = sqlite3.connect

    def memory_only(database, *args, **kwargs):
        if database != ':memory:':
            raise RuntimeError('fc10 requires in-memory SQLite')
        return original_connect(database, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise RuntimeError('fc10 offline call graph violation')

    with ExitStack() as stack:
        stack.enter_context(patch.object(sqlite3, 'connect', memory_only))
        stack.enter_context(patch.object(subprocess, 'Popen', forbidden))
        stack.enter_context(patch.object(socket.socket, 'connect', forbidden))
        stack.enter_context(patch.object(socket.socket, 'connect_ex', forbidden))
        stack.enter_context(patch.object(socket.socket, 'bind', forbidden))
        stack.enter_context(patch.object(socket, 'create_connection', forbidden))
        stack.enter_context(patch.object(socket, 'getaddrinfo', forbidden))
        yield


class SyntheticProvider:
    def __init__(self, supplied):
        self.supplied = supplied

    def provide(self, snapshot):
        return self.supplied


class RevisionClosureTests(unittest.TestCase):
    def setUp(self):
        # enterContext registers the context cleanup even if an import fails.
        self.enterContext(offline_scope())
        from campaign_tool.records.detectors import preparation
        from campaign_tool.records.detectors.pipeline import canonical_joins, evaluate_structured
        from campaign_tool.records.detectors.sources import SourceSnapshot
        from tests.records.test_structured_detector_pipeline import context, ledger
        self.p = preparation
        self.evaluate = evaluate_structured
        self.spec = json.loads(FIXTURE.read_text())
        self.assertEqual(self.spec['schema'], 'fc10-preparation-revision-closure/v1')
        self.assertEqual(self.spec['authority'], 'test_only')
        self.assertEqual(self.spec['descriptor_paths'], [list(row) for row in MATRIX])
        self.assertEqual(self.spec['dimensions'], list(DIMENSIONS))
        self.assertEqual(self.spec['negative_control'], {'omit_descriptor': 'support',
                         'expected_failing_fields': ['revision', 'code_sha256', 'receipt_sha256']})
        args, joins = context(self.spec['synthetic']['detector'])
        con = ledger(joins, args['source_bindings'])
        try:
            bound = canonical_joins(con, args['source_bindings'])
        finally:
            con.close()
        bindings = args['source_bindings']
        originals = [{'original_sha256': value} for value in sorted({v['original_sha256'] for v in bindings})]
        units = [{'unit_id': value['unit_id'], 'payload': {'text': 'fc10 synthetic extraction'}} for value in bindings]
        revision = digest('fc10 synthetic source revision')
        self.snapshot = SourceSnapshot(args['subject_sha256'], encoded(bindings), encoded(units),
                                       encoded(originals), bound, revision)
        common = {'revision': 'fc10-synthetic-v1', 'code_sha256': digest('fc10 synthetic code'),
                  'receipt_sha256': digest('fc10 synthetic receipt'), 'source_revision_sha256': revision}
        events = {}
        # Independent fixture vocabulary for purpose-quality, not TARGET_TYPES.
        for item in args['normalized']['units']:
            record = item['record']
            if record['record_type'] == 'search-log':
                events.setdefault(item['source']['unit_id'], []).append(
                    {key: record[key] for key in ('event_id', 'event_at')})
        evidence = [value['unit_id'] for value in bindings]
        descriptors = {
            'normalization': {**common, 'payload_sha256': digest(args['normalized'])},
            'support': {**common, 'originals_sha256': digest(originals), 'bindings_sha256': digest(bindings),
                        'required_original_sha256': [v['original_sha256'] for v in originals], 'unresolved': []},
            'dates': {**common, 'normalization_sha256': digest(args['normalized']), 'events_sha256': digest(events),
                      'decision_sha256': digest('fc10 synthetic date assertion'), 'evidence_unit_ids': evidence},
            'policy': {**common, 'payload_sha256': digest(args['rules']),
                       'decision_sha256': digest('fc10 synthetic policy assertion'),
                       'evidence_unit_ids': evidence, 'interval_end': 'exclusive'},
            'provider': {**common, 'model': {'mode': 'disabled'}},
        }
        self.inputs = preparation.ProviderInputs(args['normalized'], args['rules'], args['config'], descriptors)

    def prepared(self, inputs=None, snapshot=None):
        result = self.p.prepare(self.snapshot if snapshot is None else snapshot,
                                provider=SyntheticProvider(self.inputs if inputs is None else inputs))
        self.assertEqual(result.status, 'prepared', result.reason)
        self.assertEqual(result.reason, 'host_supplied_inputs_bound')
        self.assertIsNotNone(result.prepared)
        return result.prepared

    def changed_inputs(self, name, key):
        supplied = copy.deepcopy(self.inputs)
        supplied.descriptors[name][key] = (self.spec['synthetic']['revision_replacement'] if key == 'revision'
                                          else digest(self.spec['synthetic']['hash_replacement_seed'] + name + key))
        return supplied

    def check_pair(self, first, second, changed, path):
        self.assertEqual(first.inputs_json, second.inputs_json, 'detector payload must not change')
        self.assertEqual(first.source_revision_sha256, self.snapshot.revision_sha256)
        self.assertEqual(second.source_revision_sha256, self.snapshot.revision_sha256)
        self.assertEqual(first.joins, second.joins)
        self.assertEqual(json.loads(second.descriptors_json), changed.descriptors)
        self.assertNotEqual(first.descriptors_json, second.descriptors_json)
        self.assertNotEqual(first.revision_sha256, second.revision_sha256,
                            'descriptor identity must change: ' + path)

    def check_descriptor(self, name, key):
        first = self.prepared()
        changed = self.changed_inputs(name, key)
        # Exactly one field changes, and validation/source/input prerequisites stay valid.
        expected = copy.deepcopy(self.inputs.descriptors)
        expected[name][key] = changed.descriptors[name][key]
        self.assertEqual(expected, changed.descriptors)
        second = self.prepared(changed)
        print('OBSERVATION ' + encoded({'path': [name, key], 'status': 'prepared',
              'inputs_unchanged': first.inputs_json == second.inputs_json,
              'source_unchanged': first.source_revision_sha256 == second.source_revision_sha256,
              'descriptors_exact': json.loads(second.descriptors_json) == changed.descriptors,
              'before': first.revision_sha256, 'after': second.revision_sha256}), flush=True)
        self.check_pair(first, second, changed, name + '.' + key)
        # Full structured evaluation is byte-for-byte invariant, including revision fields.
        baseline = self.evaluate(**first.kwargs)
        self.assertEqual(baseline, self.evaluate(**second.kwargs))
        self.assertEqual(baseline['structured']['purpose-quality']['status'], 'evaluated')
        self.assertTrue(baseline['structured']['purpose-quality']['hits'])
        self.assertFalse(baseline['publication_ready'])
        self.assertEqual(baseline['stage_promotions'], 0)

    def pinned(self):
        supplied = copy.deepcopy(self.inputs)
        supplied.descriptors['provider']['model'] = {
            'mode': 'pinned', 'revision_sha256': digest(self.spec['synthetic']['model_revision_seed']),
            'prompt_sha256': digest(self.spec['synthetic']['prompt_seed'])}
        return supplied

    def check_model(self, key):
        self.assertEqual(self.spec['model_paths'], [['provider', 'model', 'revision_sha256'],
                                                    ['provider', 'model', 'prompt_sha256']])
        supplied = self.pinned()
        first = self.prepared(supplied)
        changed = copy.deepcopy(supplied)
        changed.descriptors['provider']['model'][key] = digest('fc10 changed pinned ' + key)
        self.check_pair(first, self.prepared(changed), changed, 'provider.model.' + key)

    def test_pinned_model_revision(self):
        self.check_model('revision_sha256')

    def test_pinned_prompt_revision(self):
        self.check_model('prompt_sha256')

    def test_disabled_model_control(self):
        disabled = self.prepared()
        self.assertEqual(json.loads(disabled.descriptors_json)['provider']['model'], {'mode': 'disabled'})
        self.assertNotEqual(disabled.revision_sha256, self.prepared(self.pinned()).revision_sha256)
        invalid = copy.deepcopy(self.inputs)
        invalid.descriptors['provider']['model']['prompt_sha256'] = digest('fc10 inactive prompt')
        result = self.p.prepare(self.snapshot, provider=SyntheticProvider(invalid))
        self.assertEqual(result, self.p.Preparation('blocked', 'model_revision_unresolved'))

    def test_unchanged_replay(self):
        for supplied in (self.inputs, self.pinned()):
            with self.subTest(mode=supplied.descriptors['provider']['model']['mode']):
                self.assertEqual(self.prepared(supplied), self.prepared(supplied))
                reordered = copy.deepcopy(supplied)
                reordered.descriptors.clear()
                reordered.descriptors.update(reversed(list(copy.deepcopy(supplied.descriptors).items())))
                self.assertEqual(self.prepared(supplied), self.prepared(reordered))

    def test_reconstruct_provider_inputs(self):
        for supplied in (self.inputs, self.pinned()):
            with self.subTest(mode=supplied.descriptors['provider']['model']['mode']):
                original = self.prepared(supplied)
                values = json.loads(original.inputs_json)
                restored = self.p.ProviderInputs(values['normalized'], values['rules'], values['config'],
                                                 json.loads(original.descriptors_json))
                snapshot = replace(self.snapshot, bindings_json=encoded(json.loads(self.snapshot.bindings_json)),
                                   units_json=encoded(json.loads(self.snapshot.units_json)),
                                   originals_json=encoded(json.loads(self.snapshot.originals_json)))
                self.assertEqual(original, self.prepared(restored, snapshot))
                restored.descriptors['normalization']['revision'] = 'fc10 changed after decode'
                self.assertNotEqual(original.revision_sha256, self.prepared(restored).revision_sha256)
                self.assertEqual(json.loads(original.descriptors_json), supplied.descriptors)

    def test_stale_source_revision_blocked(self):
        for name in ('normalization', 'support', 'dates', 'policy', 'provider'):
            with self.subTest(descriptor=name):
                supplied = copy.deepcopy(self.inputs)
                supplied.descriptors[name]['source_revision_sha256'] = digest('fc10 stale source')
                self.assertEqual(self.p.prepare(self.snapshot, provider=SyntheticProvider(supplied)),
                                 self.p.Preparation('blocked', 'descriptor_source_changed'))
        changed_snapshot = replace(self.snapshot, revision_sha256=digest('fc10 new source'))
        self.assertEqual(self.p.prepare(changed_snapshot, provider=SyntheticProvider(self.inputs)),
                         self.p.Preparation('blocked', 'descriptor_source_changed'))

    def test_exact_descriptor_json_retained(self):
        result = self.prepared()
        self.assertEqual(result.descriptors_json, encoded(self.inputs.descriptors))
        # Independent full preimage, no product digest helper or descriptor projection.
        envelope = {'schema': 'structured-preparation-v1', 'inputs': json.loads(result.inputs_json),
                    'descriptors': self.inputs.descriptors,
                    'source_revision_sha256': self.snapshot.revision_sha256,
                    'joins_revision_sha256': self.snapshot.joins.revision_sha256,
                    'join_rows': json.loads(self.snapshot.joins.rows_json)}
        self.assertEqual(result.revision_sha256, digest(envelope))

    def test_evaluation_result_invariant_for_metadata_only_change(self):
        baseline = self.prepared()
        kwargs = baseline.kwargs
        kwargs['normalized']['units'].clear()
        self.assertTrue(baseline.kwargs['normalized']['units'])
        expected = self.evaluate(**baseline.kwargs)
        self.assertEqual(expected, self.evaluate(**self.prepared(self.pinned()).kwargs))
        self.assertFalse(expected['publication_ready'])
        self.assertEqual(expected['stage_promotions'], 0)


def matrix_test(name, key):
    def test(self):
        self.check_descriptor(name, key)
    test.__doc__ = 'Frozen valid descriptor change: ' + name + '.' + key
    return test


for _name, _key in MATRIX:
    setattr(RevisionClosureTests, 'test_matrix_' + _name + '_' + _key, matrix_test(_name, _key))


class RecordingResult(unittest.TextTestResult):
    def startTest(self, test):
        self.started_ids.append(test.id())
        super().startTest(test)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started_ids = []


def run_suite(suite, summary_path):
    result = cast(RecordingResult, unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite))
    names = {test_id.rsplit('.', 1)[-1] for test_id in result.started_ids}
    executed_cells = [list(row) for row in MATRIX if 'test_matrix_' + '_'.join(row) in names]
    coverage = [value for value in DIMENSIONS if 'test_' + value in names]
    if executed_cells:
        coverage.insert(0, 'five_by_three_valid_descriptor_changes')
    summary = {'tests_run': result.testsRun, 'test_ids': result.started_ids,
               'failures': [{'id': test.id(), 'traceback': trace} for test, trace in result.failures],
               'errors': [{'id': test.id(), 'traceback': trace} for test, trace in result.errors],
               'skipped': [(test.id(), reason) for test, reason in result.skipped],
               'successful': result.wasSuccessful(), 'matrix_cells': executed_cells,
               'dimensions': coverage}
    if summary_path is not None:
        Path(summary_path).write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    return 0 if result.wasSuccessful() and not result.skipped else 1


def script_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--temporary-root', type=Path, required=True)
    parser.add_argument('--summary', type=Path, required=True)
    parser.add_argument('--mode', choices=('full', 'matrix', 'discovery', 'compatibility'), default='full')
    parser.add_argument('--omit-descriptor', choices=('support',))
    args = parser.parse_args()
    root = args.temporary_root
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError('existing child-local temporary root required')
    # Isolated script process only; no ambient mutation in ordinary discovery.
    tempfile.tempdir = str(root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    with offline_scope(), ExitStack() as stack:
        from campaign_tool.records.detectors import preparation
        if args.omit_descriptor:
            real_digest = preparation.digest

            def omit_one_descriptor(value):
                if type(value) is dict and value.get('schema') == 'structured-preparation-v1':
                    value = copy.deepcopy(value)
                    del value['descriptors'][args.omit_descriptor]
                return real_digest(value)

            stack.enter_context(patch.object(preparation, 'digest', omit_one_descriptor))
        loader = unittest.TestLoader()
        if args.mode == 'matrix':
            suite = unittest.TestSuite(RevisionClosureTests('test_matrix_' + name + '_' + key) for name, key in MATRIX)
        elif args.mode == 'compatibility':
            suite = loader.loadTestsFromName('tests.records.test_detector_preparation')
        elif args.mode == 'discovery':
            import os
            before = (dict(os.environ), tempfile.tempdir, socket.socket.connect, sqlite3.connect, subprocess.Popen)
            events = []

            class Sentinel(unittest.TestCase):
                def runTest(self):
                    self.assertEqual(before, (dict(os.environ), tempfile.tempdir, socket.socket.connect,
                                              sqlite3.connect, subprocess.Popen))
                    con = sqlite3.connect(':memory:')
                    try:
                        self.assertEqual(con.execute('SELECT 7').fetchone(), (7,))
                    finally:
                        con.close()
                    events.append('sentinel')

            first = run_suite(unittest.TestSuite([Sentinel()]), root / 'sentinel-before.json')
            if first:
                return first
            suite = loader.discover(str(REPO / 'tests/records'), pattern=Path(__file__).name,
                                    top_level_dir=str(REPO))
            if before != (dict(os.environ), tempfile.tempdir, socket.socket.connect, sqlite3.connect, subprocess.Popen):
                raise RuntimeError('discovery contaminated ambient state')
            result = run_suite(unittest.TestSuite([*suite, Sentinel()]), args.summary)
            if events != ['sentinel', 'sentinel']:
                raise RuntimeError('sentinels did not execute')
            return result
        else:
            suite = loader.loadTestsFromTestCase(RevisionClosureTests)
        return run_suite(suite, args.summary)


if __name__ == '__main__':
    raise SystemExit(script_main())
