"""Independently authored synthetic FC15 occurrence conservation tests.

Author: gpt-6.1-sol / openai-codex. No copied planner probes or test helpers.
Imports are inert; all product imports and guards have a restorable lifetime.
"""
import ast
import copy
from contextlib import contextmanager, ExitStack
import hashlib
import importlib
import inspect
import io
import itertools
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tests/fixtures/fc15-row-cross-source-dedup/cases.json'
CASE_TESTS = {
    'same_semantics': 'test_same_semantics',
    'different_values': 'test_different_values',
    'third_occurrence': 'test_third_occurrence',
    'scope_separation': 'test_scope_separation',
    'permutations': 'test_permutations',
    'control_and_flags': 'test_control_and_flags',
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def deny(*args, **kwargs):
    raise RuntimeError('offline sentinel: prohibited side effect')


@contextmanager
def offline(root=None):
    """Restored mocks, not permanent audit hooks or a hostile-code sandbox."""
    import builtins
    with ExitStack() as stack:
        stack.enter_context(patch.object(sys, 'dont_write_bytecode', True))
        for owner in (builtins, io):
            previous = owner.open
            def read_only(file, mode='r', *args, _previous=previous, **kwargs):
                if any(flag in mode for flag in 'wax+'):
                    deny()
                return _previous(file, mode, *args, **kwargs)
            stack.enter_context(patch.object(owner, 'open', read_only))
        for owner, name in ((socket, 'socket'), (socket, 'create_connection'),
                            (socket, 'getaddrinfo'), (subprocess, 'Popen'),
                            (sqlite3, 'connect'), (os, 'system'), (os, 'open'),
                            (os, 'mkdir'), (os, 'remove'), (os, 'unlink'),
                            (os, 'rename'), (os, 'replace'), (os, 'rmdir')):
            stack.enter_context(patch.object(owner, name, deny))
        if root is not None:
            stack.enter_context(patch.object(tempfile, 'tempdir', str(root)))
            stack.enter_context(patch.dict(os.environ, {'TMPDIR': str(root), 'TEMP': str(root), 'TMP': str(root)}))
        yield


def inputs(count, conflict=False, scope=None):
    """Producer-shaped receipts and independent inventory, no product helpers."""
    spec = json.loads(FIXTURE.read_text())
    result = {'run_id': 'fictional-fc15', 'receipts': [], 'mappings': [], 'source_bindings': []}
    for index, artifact in enumerate(spec['artifacts'][:count]):
        original = sha(artifact['label'])
        purpose = spec['outlier_purpose'] if conflict and index == count - 1 else spec['purpose']
        values = [spec['event_id'], spec['event_at'], purpose, spec['user_id']]
        producer_text = json.dumps(values, ensure_ascii=True, sort_keys=True)
        units = [
            {'kind': 'csv_header', 'locator': {'record': 1, 'line_start': 1, 'line_end': 1},
             'text': json.dumps(artifact['headers'], ensure_ascii=True, sort_keys=True), 'data': {}},
            {'kind': 'csv_row', 'locator': {'record': 2, 'line_start': 2, 'line_end': 2},
             'text': producer_text, 'data': {'values': values, 'presence': ['present'] * 4,
             'duplicate_of_record': None, 'row_sha256': sha(producer_text), 'column_mapping': 'positional'}}]
        receipt = {'schema': 'format-extraction-v1', 'original_sha256': original,
                   'original_changed': False, 'status': 'complete', 'review_status': 'not_reviewed',
                   'issues': [], 'pages': [], 'units': units}
        raw = (json.dumps(receipt, ensure_ascii=True, sort_keys=True) + '\n').encode()
        receipt_hash = sha(raw)
        result['receipts'].append({'artifact_sha256': original, 'derived_receipt_sha256': receipt_hash,
                                   'derived_receipt': raw})
        importer = sha(canonical(['receipt-bound-derivative-v1', original, receipt_hash, 'native']))
        for ordinal, unit in enumerate(units, 1):
            unit_hash = sha(canonical(unit))
            result['source_bindings'].append({'unit_id': sha(canonical([importer, ordinal, unit_hash])),
                'original_sha256': original, 'artifact_sha256': unit_hash, 'locator': copy.deepcopy(unit['locator'])})
        agency, kind = 'fictional-agency-A', 'search-log'
        if scope == 'agency' and index:
            agency = 'fictional-agency-B'
        if scope == 'type' and index:
            kind = 'network-search'
        result['mappings'].append({'schema': 'agency-row-mapping-v1', 'version': 'fictional-v1',
            'artifact_sha256': original, 'agency_id': agency, 'record_type': kind,
            'unit_kind': 'csv_row', 'delimiter': ',', 'columns': [
                {'name': name, 'target': target, 'type': 'str'} for name, target in
                zip(artifact['headers'], ['event_id', 'event_at', 'purpose', 'user_id'])]})
    return result


class CrossSourceTests(unittest.TestCase):
    def setUp(self):
        self.guard = offline()
        self.guard.__enter__()
        self.addCleanup(self.guard.__exit__, None, None, None)
        self.rows = importlib.import_module('campaign_tool.records.understanding.rows')
        self.spec = json.loads(FIXTURE.read_text())

    def check_output(self, args, output, conflict=False):
        expected_groups = {}
        expected_units = {}
        expected_diagnostics = {}
        for pin, mapping in zip(args['receipts'], args['mappings']):
            # Pair by original explicitly, not enumeration (permuted callers).
            mapping = next(m for m in args['mappings'] if m['artifact_sha256'] == pin['artifact_sha256'])
            receipt = json.loads(pin['derived_receipt'])
            for unit in receipt['units']:
                source = next(s for s in args['source_bindings'] if
                              s['original_sha256'] == pin['artifact_sha256'] and s['locator'] == unit['locator'])
                ref = {k: source[k] for k in ('unit_id', 'artifact_sha256')}
                if unit['kind'] == 'csv_header':
                    record = {'original_sha256': pin['artifact_sha256'], 'locator': unit['locator'],
                              'record_type': 'other', 'event_id': source['unit_id'], 'event_at': None, 'fields': {}}
                    cells = {}
                    reasons = []
                else:
                    values = unit['data']['values']
                    cells = {key: {'state': 'observed', 'value': value} for key, value in
                             zip(['event_id', 'event_at', 'purpose', 'user_id'], values)}
                    record = {'original_sha256': pin['artifact_sha256'], 'locator': unit['locator'],
                              'record_type': mapping['record_type'], 'event_id': values[0], 'event_at': values[1],
                              'fields': {k: cells[k] for k in ('purpose', 'user_id')}}
                    key = (mapping['agency_id'], mapping['record_type'], values[0])
                    expected_groups.setdefault(key, []).append(ref)
                    reasons = ['conflicting_duplicate_event'] if conflict else []
                expected_units[source['unit_id']] = {'record': record, 'source': ref}
                expected_diagnostics[source['unit_id']] = {'unit_id': source['unit_id'],
                    'agency_id': mapping['agency_id'], 'cells': cells, 'reasons': reasons,
                    'status': 'unresolved' if reasons else 'mapped'}
        actual_events = {tuple(e['identity']): e for e in output['events']}
        self.assertEqual(set(actual_events), set(expected_groups))
        for key, sources in expected_groups.items():
            event = actual_events[key]
            self.assertEqual(sorted(event['sources'], key=canonical), sorted(sources, key=canonical), 'exact_source_set')
            self.assertEqual(event['duplicate_rows'], len(sources) - 1)
            self.assertEqual(event['status'], 'unresolved' if conflict else 'mapped')
            self.assertEqual(event['reason'], 'conflicting_duplicate_event' if conflict else 'same_semantic_event')
        self.assertEqual(len(output['events']), len(expected_groups))
        actual_units = output['normalized']['units']
        self.assertEqual(len(actual_units), len(expected_units))
        self.assertEqual({u['source']['unit_id']: u for u in actual_units}, expected_units)
        self.assertEqual(len(output['diagnostics']), len(expected_diagnostics))
        self.assertEqual({d['unit_id']: d for d in output['diagnostics']}, expected_diagnostics)
        self.assertEqual(output['normalized']['source_inventory_sha256'], sha(canonical(args['source_bindings'])))
        self.assertEqual(output['source_bindings'], args['source_bindings'])
        self.assertEqual(output['normalized']['complete'], not conflict)
        self.assertEqual(output['upstream'], [])
        self.assertIs(output['review_required'], True)
        for key in ('qualified', 'activation_allowed', 'publication_ready'):
            self.assertIs(output[key], False)
        self.assertEqual(output['stage_promotions'], 0)

    def exercise(self, count, conflict=False, scope=None):
        args = inputs(count, conflict, scope)
        saved = copy.deepcopy(args)
        result = self.rows.normalize(**args)
        self.check_output(args, result, conflict)
        self.assertEqual(args, saved)
        return args, result

    def test_same_semantics(self):
        self.exercise(2)

    def test_different_values(self):
        self.exercise(2, True)

    def test_third_occurrence(self):
        self.exercise(3, True)

    def test_scope_separation(self):
        for scope in ('agency', 'type'):
            with self.subTest(scope=scope):
                self.exercise(2, scope=scope)

    def test_permutations(self):
        for count, conflict in ((2, False), (3, True)):
            args = inputs(count, conflict)
            saved = copy.deepcopy(args)
            # All orders of receipts, mappings, AND individual inventory entries.
            inventory_hashes = set()
            for r in itertools.permutations(range(count)):
                for m in itertools.permutations(range(count)):
                    for s in itertools.permutations(range(2 * count)):
                        with self.subTest(count=count, receipts=r, mappings=m, inventory=s):
                            ordered = {**args, 'receipts': [args['receipts'][i] for i in r],
                                'mappings': [args['mappings'][i] for i in m],
                                'source_bindings': [args['source_bindings'][i] for i in s]}
                            out = self.rows.normalize(**ordered)
                            self.check_output(ordered, out, conflict)
                            inventory_hashes.add(out['normalized']['source_inventory_sha256'])
            self.assertEqual(len(inventory_hashes), 24 if count == 2 else 720)
            self.assertEqual(args, saved)

    def test_control_and_flags(self):
        self.exercise(1)
        self.assertEqual(set(self.spec['case_ids']), set(CASE_TESTS))

    def test_deciding_source_projection_control(self):
        original = self.rows._normalize
        source = textwrap.dedent(inspect.getsource(original))
        tree = ast.parse(source)
        matches = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == 'sources' and isinstance(value, ast.ListComp):
                        matches.append(value)
        self.assertEqual(len(matches), 1)
        # Isolated event projection: retain every unrelated statement verbatim.
        comp = matches[0]
        comp.generators[0].iter = ast.Subscript(value=comp.generators[0].iter,
                                               slice=ast.Slice(upper=ast.Constant(value=1)), ctx=ast.Load())
        ast.fix_missing_locations(tree)
        namespace = dict(original.__globals__)
        exec(compile(tree, '<fc15-isolated-source-projection>', 'exec'), namespace)
        with patch.object(self.rows, '_normalize', namespace['_normalize']):
            self.exercise(1)
            for count, conflict in ((2, False), (2, True), (3, True)):
                with self.subTest(control_count=count, conflict=conflict):
                    with self.assertRaisesRegex(AssertionError, 'exact_source_set'):
                        self.exercise(count, conflict)
        self.assertIs(self.rows._normalize, original)
        self.exercise(1)
        self.exercise(2)
        self.exercise(3, True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--temporary-root', required=True)
    options = parser.parse_args()
    temporary_root = Path(options.temporary_root).resolve(strict=True)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    with offline(temporary_root):
        unittest.main(argv=[sys.argv[0]], verbosity=2)
