"""Synthetic FC16 structured isolation qualification; inert on ordinary import."""
import argparse
import ast
from contextlib import ExitStack, contextmanager
import copy
import hashlib
import importlib
import inspect
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

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tests/fixtures/fc16_structured_support_types/cases.json'


def h(text):
    return hashlib.sha256(text.encode()).hexdigest()


@contextmanager
def offline(temporary_root=None):
    """Scoped sentinels, not a hostile-code sandbox; no persistent audit hook."""
    connect = sqlite3.connect

    def memory_only(database, *args, **kwargs):
        if database != ':memory:':
            raise RuntimeError('offline_sqlite_memory_only')
        return connect(database, *args, **kwargs)

    def denied(*args, **kwargs):
        raise RuntimeError('offline_effect_denied')

    with ExitStack() as stack:
        stack.enter_context(patch.object(sys, 'dont_write_bytecode', True))
        for target in ('socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.socket.bind', 'socket.create_connection',
                       'socket.getaddrinfo', 'subprocess.Popen', 'os.system'):
            stack.enter_context(patch(target, denied))
        stack.enter_context(patch('sqlite3.connect', memory_only))
        if temporary_root is not None:
            stack.enter_context(patch.object(tempfile, 'tempdir', str(temporary_root)))
        yield


def api():
    with offline():
        return importlib.import_module('campaign_tool.records.detectors.pipeline')


def make_context(kind='retention-setting', *, training=False):
    """Independently construct the pinned producer schema, never import its fixture."""
    subject = h('FC16 synthetic primary')
    policy = h('FC16 synthetic policy')
    record = {'original_sha256': subject, 'locator': {'row': 1},
              'event_id': 'synthetic-primary', 'event_at': '2020-01-20T12:00:00+00:00',
              'record_type': kind, 'fields': {'retention_mode': 'days', 'retention_days': 31}}
    if training:
        record.update(record_type='search-log', fields={'user_id': 'synthetic-user'})
    rule = {'detector': 'search-before-training' if training else 'retention-over-policy',
            'agency_id': 'synthetic-agency', 'rule_id': 'synthetic-rule',
            'version': 'synthetic-v1', 'source_sha256': policy,
            'effective_from': '2020-01-01', 'effective_to': '2021-01-01',
            'version_verified': True, 'applicability': 'yes',
            'parameters': {} if training else {'maximum_days': 30, 'unit': 'calendar_days'}}
    context = {'subject_sha256': subject, 'source_bindings': [],
               'normalized': {'schema': 'structured-normalization-v1', 'version': 'synthetic-fc16-v1',
                              'complete': True, 'units': []},
               'rules': {'version': 'synthetic-pack-v1', 'entries': [rule]},
               'config': {'as_of': '2020-01-20'}}
    append_record(context, record, 'primary')
    append_record(context, {'original_sha256': policy, 'locator': {'page': 1},
                           'event_id': 'synthetic-policy', 'record_type': 'policy', 'fields': {}}, 'policy')
    return context


def append_record(context, record, identity):
    p = api()
    source = {'unit_id': identity, 'original_sha256': record['original_sha256'],
              'locator': copy.deepcopy(record['locator']), 'artifact_sha256': p.digest(record)}
    context['source_bindings'].append(source)
    context['normalized']['units'].append({'record': record, 'source': {
        'unit_id': identity, 'artifact_sha256': source['artifact_sha256']}})
    context['normalized']['source_inventory_sha256'] = p.digest(context['source_bindings'])


def support(context, kind, identity='extra'):
    record = {'original_sha256': h('FC16 synthetic foreign ' + identity),
              'locator': {'row': 9}, 'event_id': 'synthetic-' + identity,
              'event_at': '2020-01-20T12:00:00+00:00', 'record_type': kind,
              'fields': {'retention_mode': 'days', 'retention_days': 31}}
    if kind == 'training-roster':
        record['fields'] = {'user_id': 'synthetic-user',
                            'training_completed_at': '2020-01-21T12:00:00+00:00'}
    append_record(context, record, identity)


def ledger(context):
    con = sqlite3.connect(':memory:')
    con.executescript('''
        CREATE TABLE joins(id TEXT, original_sha256 TEXT, status TEXT,
                           agency_id TEXT, request_id TEXT, evidence TEXT);
        CREATE TABLE agencies(id TEXT);
        CREATE TABLE requests(id TEXT, agency_id TEXT);
        INSERT INTO agencies VALUES('synthetic-agency');
        INSERT INTO requests VALUES('synthetic-request','synthetic-agency');
    ''')
    for index, source in enumerate(context['source_bindings']):
        proof = {'source_sha256': source['original_sha256'], 'locator': json.dumps(source['locator'])}
        con.execute('INSERT INTO joins VALUES(?,?,?,?,?,?)',
                    (str(index), source['original_sha256'], 'typed', 'synthetic-agency',
                     'synthetic-request', json.dumps(proof)))
    return con


class SupportTypesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = api()
        cls.cases = json.loads(FIXTURE.read_text())

    def setUp(self):
        self.guard = offline()
        self.guard.__enter__()
        self.addCleanup(self.guard.__exit__, None, None, None)

    def checked_evaluate(self, context, evaluator=None):
        con = ledger(context)
        self.addCleanup(con.close)
        before = self.p.canonical(context)
        sql = tuple(con.iterdump())
        changes = con.total_changes
        joins = self.p.canonical_joins(con, context['source_bindings'])
        result = (evaluator or self.p.evaluate_structured)(**context, joins=joins)
        self.assertEqual(self.p.canonical(context), before)
        self.assertEqual(tuple(con.iterdump()), sql)
        self.assertEqual(con.total_changes, changes)
        self.assertFalse(result['publication_ready'])
        self.assertEqual(result['stage_promotions'], 0)
        self.assertEqual(result['acceptance'], 'supplied_evidence_triage_only')
        self.assertEqual(set(result['structured']), set(self.cases['detectors']))
        return result

    def assert_refusal(self, output):
        oracle = self.cases['oracle']
        for name, entry in output['structured'].items():
            self.assertEqual(entry['reason'], oracle['reason'], 'isolation_reason:' + name)
            for key in ('status', 'hits', 'hit_count', 'publication_ready'):
                self.assertEqual(entry[key], oracle[key], key + ':' + name)

    def assert_retention(self, output):
        oracle = self.cases['oracle']
        target = output['structured'][oracle['target']]
        self.assertEqual(target['status'], oracle['target_status'])
        self.assertEqual(target['hit_count'], oracle['target_hit_count'])
        self.assertEqual(target['hits'][0]['observed_values_private'], oracle['observed_values_private'])
        self.assertTrue(all(output['revisions'].values()))
        return target

    def test_inventory(self):
        eligible = set().union(*self.p.TYPES.values())
        self.assertEqual(eligible, set(self.cases['forbidden_types']))
        self.assertEqual(self.p.KNOWN_TYPES - eligible, set(self.cases['allowed_support']))
        self.assertEqual(set(self.p.DETECTORS), set(self.cases['detectors']))
        self.assertEqual(len(self.p.DETECTORS), 7)

    def test_forbidden_types(self):
        for kind in self.cases['forbidden_types']:
            for reverse in (False, True):
                with self.subTest(case='forbidden_types', kind=kind, reverse=reverse):
                    context = make_context()
                    support(context, kind)
                    if reverse:
                        context['normalized']['units'].reverse()
                        context['source_bindings'].reverse()
                        context['normalized']['source_inventory_sha256'] = self.p.digest(context['source_bindings'])
                    self.assert_refusal(self.checked_evaluate(context))

    def test_allowed_support(self):
        baseline = self.assert_retention(self.checked_evaluate(make_context()))
        for kind in self.cases['allowed_support']:
            with self.subTest(case='allowed_support', kind=kind):
                context = make_context()
                support(context, kind)
                target = self.assert_retention(self.checked_evaluate(context))
                self.assertEqual(target['hits'], baseline['hits'])
                self.assertEqual(target['reason'], baseline['reason'])
                self.assertEqual(target['manifest']['counts']['evaluated'], 1)
                self.assertEqual(target['manifest']['counts']['blocked'], 0)
                if kind == 'training-roster':
                    context = make_context(training=True)
                    support(context, kind)
                    entry = self.checked_evaluate(context)['structured']['search-before-training']
                    self.assertEqual(entry['status'], self.cases['oracle']['training_status'])
                    self.assertEqual(entry['hit_count'], self.cases['oracle']['training_hit_count'])
                    self.assertEqual(len(entry['hits'][0]['evidence']), 2)

    def test_primary_control(self):
        for kind in self.cases['forbidden_types']:
            with self.subTest(case='primary_control', kind=kind):
                output = self.checked_evaluate(make_context(kind))
                self.assertTrue(all(output['revisions'].values()))
                for entry in output['structured'].values():
                    self.assertNotEqual(entry['reason'], self.cases['oracle']['reason'])
                    self.assertIsNotNone(entry['manifest'])
                    self.assertIn(entry['reason'], ('required_context_unresolved',
                        'supplied_context_evaluated', 'no_matching_type_in_complete_normalization'))
                if kind == 'retention-setting':
                    self.assert_retention(output)

    def test_order_and_failure(self):
        for kind in self.cases['forbidden_types']:
            for reverse in (False, True):
                with self.subTest(case='order_and_failure', kind=kind, reverse=reverse):
                    context = make_context()
                    for allowed in self.cases['allowed_support']:
                        support(context, allowed, 'peer-' + allowed)
                    support(context, kind, 'forbidden')
                    if reverse:
                        context['normalized']['units'].reverse()
                        context['source_bindings'].reverse()
                        context['normalized']['source_inventory_sha256'] = self.p.digest(context['source_bindings'])
                    self.assert_refusal(self.checked_evaluate(context))

    def test_deciding_control(self):
        tree = ast.parse(inspect.getsource(self.p.evaluate_structured))
        matches = [node for node in ast.walk(tree) if isinstance(node, ast.Expr)
                   and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                   and node.value.func.id == 'require' and len(node.value.args) == 2
                   and isinstance(node.value.args[1], ast.Constant)
                   and node.value.args[1].value == 'unrelated_support_record']
        self.assertEqual(len(matches), 1)
        matches[0].value = ast.Constant(None)
        ast.fix_missing_locations(tree)
        namespace = dict(self.p.evaluate_structured.__globals__)
        exec(compile(tree, '<synthetic-fc16-isolation-mutant>', 'exec'), namespace)
        context = make_context()
        support(context, 'retention-setting')
        real = self.checked_evaluate(context)
        self.assert_refusal(real)
        mutant = self.checked_evaluate(context, namespace['evaluate_structured'])
        target = mutant['structured']['retention-over-policy']
        self.assertEqual(target['status'], 'evaluated')
        self.assertEqual(target['hit_count'], 2)
        self.assertTrue(all(mutant['revisions'].values()))
        self.assertEqual(target['manifest']['counts']['evaluated'], 2)
        self.assertEqual(target['manifest']['counts']['blocked'], 0)
        for hit in target['hits']:
            self.assertEqual(hit['observed_values_private'], self.cases['oracle']['observed_values_private'])
        with self.assertRaisesRegex(AssertionError, 'isolation_reason:') as caught:
            self.assert_refusal(mutant)
        restored = self.checked_evaluate(context)
        self.assert_refusal(restored)
        positive = self.checked_evaluate(make_context())
        self.assert_retention(positive)
        self.control_evidence = {'mutation': 'remove only unrelated_support_record require AST expression',
                                 'expected_assertion': str(caught.exception),
                                 'genuine_refusal': real, 'mutant': mutant,
                                 'restored_refusal': restored, 'ordinary_positive': positive}


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_ids = []
        self.subcases = []
        self.controls = []

    def addSuccess(self, test):
        if hasattr(test, 'control_evidence'):
            self.controls.append(getattr(test, 'control_evidence'))
        super().addSuccess(test)

    def startTest(self, test):
        self.test_ids.append(test.id())
        super().startTest(test)

    def addSubTest(self, test, subtest, err):
        self.subcases.append({'id': subtest.id(), 'passed': err is None})
        super().addSubTest(test, subtest, err)


def script_main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--temporary-root', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--mode', choices=('suite', 'compatibility', 'discovery'), default='suite')
    args = parser.parse_args()
    original_state = (dict(os.environ), tempfile.tempdir, list(sys.path), sys.dont_write_bytecode,
                      socket.socket.connect, sqlite3.connect, subprocess.Popen)
    args.temporary_root.mkdir(parents=True, exist_ok=False)
    original_path = list(sys.path)
    sys.path.insert(0, str(ROOT))
    try:
        with offline(args.temporary_root):
            if args.mode == 'compatibility':
                suite = unittest.defaultTestLoader.loadTestsFromName(
                    'tests.records.test_structured_detector_pipeline.StructuredPipelineTests')
            elif args.mode == 'discovery':
                state = (dict(os.environ), tempfile.tempdir, list(sys.path),
                         socket.socket.connect, sqlite3.connect, subprocess.Popen)
                peer = unittest.FunctionTestCase(lambda: unittest.TestCase().assertEqual(
                    (dict(os.environ), tempfile.tempdir, list(sys.path),
                     socket.socket.connect, sqlite3.connect, subprocess.Popen), state),
                    description='inert-peer-before-and-after')
                discovered = unittest.defaultTestLoader.discover(
                    str(ROOT / 'tests/records'), pattern=Path(__file__).name, top_level_dir=str(ROOT))
                suite = unittest.TestSuite([peer, discovered, peer])
            else:
                suite = unittest.defaultTestLoader.loadTestsFromTestCase(SupportTypesTests)
            result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    finally:
        sys.path[:] = original_path
    unittest.TestCase().assertEqual(
        (dict(os.environ), tempfile.tempdir, list(sys.path), sys.dont_write_bytecode,
         socket.socket.connect, sqlite3.connect, subprocess.Popen), original_state)
    if not isinstance(result, RecordingResult):
        raise TypeError('recording_result_required')
    report = {'tests': result.testsRun, 'skips': len(result.skipped),
              'success': result.wasSuccessful() and not result.skipped,
              'test_ids': result.test_ids, 'subcases': result.subcases, 'controls': result.controls,
              'failures': len(result.failures), 'errors': len(result.errors), 'mode': args.mode,
              'ambient_state_restored': True}
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    return int(not report['success'])


if __name__ == '__main__':
    raise SystemExit(script_main())
