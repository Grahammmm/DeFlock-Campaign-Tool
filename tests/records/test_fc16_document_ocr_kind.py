"""Offline synthetic OCR classification regression; inert during discovery."""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import importlib
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
FIXTURE = ROOT / 'tests/fixtures/fc16_document_ocr_kind/cases.json'
TEMP_ROOT = None


def denied(*args, **kwargs):
    raise RuntimeError('offline-effect-denied')


@contextmanager
def offline(root=None):
    # Import closure inspected: document -> evidence -> grounded_evidence ->
    # retained_units, all stdlib-only and no import-time operational defaults.
    # Sentinels precede product imports; mocks and product imports are scoped.
    modules = {k: v for k, v in sys.modules.items() if k == 'campaign_tool' or k.startswith('campaign_tool.')}
    with ExitStack() as stack:
        for obj, name in ((socket, 'socket'), (socket, 'create_connection'),
                          (subprocess, 'Popen'), (os, 'system'), (sqlite3, 'connect')):
            stack.enter_context(patch.object(obj, name, denied))
        stack.enter_context(patch.object(sys, 'path', [str(ROOT), *sys.path]))
        if root is not None:
            stack.enter_context(patch.object(tempfile, 'tempdir', str(root)))
        try:
            yield
        finally:
            for name in list(sys.modules):
                if (name == 'campaign_tool' or name.startswith('campaign_tool.')) and name not in modules:
                    del sys.modules[name]
            sys.modules.update(modules)


def fixtures():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


class OcrKindTests(unittest.TestCase):
    def setUp(self):
        self.scope = offline(TEMP_ROOT)
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.document = importlib.import_module('campaign_tool.records.understanding.document')
        self.evidence = importlib.import_module('campaign_tool.records.understanding.evidence')
        self.fx = fixtures()

    def build(self, cases):
        e = self.evidence
        original = e.sha(b'FC16 synthetic original')
        raw_units = [{'kind': c['kind'], 'locator': {'page': n},
                      'text': c.get('text', self.fx['text']), 'data': deepcopy(c['data'])}
                     for n, c in enumerate(cases, 1)]
        raw = {'schema': 'format-extraction-v1', 'original_sha256': original,
               'original_changed': False, 'status': 'complete', 'parser': 'synthetic',
               'parser_version': '0', 'route': {'format': 'pdf'},
               'pages': list(range(1, len(cases) + 1)), 'units': raw_units}
        before = deepcopy(raw)
        receipt_hash = e.sha(e.canonical(raw))
        bundle = e.normalize_receipt(raw, original, receipt_hash,
                                     extra_context={'page_count': len(cases)})
        self.assertEqual(raw, before, 'receipt input preserved')
        # Real bound units, not an invalid edit of a signed bundle.
        if any(c['attempt'] != 'native' for c in cases):
            units = [e.unit(u, original, receipt_hash, n, c['attempt'])
                     for n, (u, c) in enumerate(zip(raw_units, cases), 1)]
            # One receipt represents one attempt; rebind its metadata together
            # with the units instead of claiming native origin for an OCR attempt.
            attempts = {u['attempt_kind'] for u in units}
            self.assertEqual(len(attempts), 1, 'one fixture receipt has one attempt kind')
            receipts = deepcopy(bundle['source_receipts'])
            receipts[0]['attempt_kind'] = next(iter(attempts))
            bundle = e.bundle(units, bundle['source_context'], receipts)
        return bundle

    def check_case(self, case):
        bundle = self.build([case])
        saved = deepcopy(bundle)
        result = self.document.analyze(bundle)
        # Deciding assertion is deliberately first for baseline/mutant diagnosis.
        self.assertEqual(result['gaps'], case['gaps'], 'ocr-global-warning')
        page = result['coverage'][0]['pages'][0]
        has_text = bool(case.get('text', self.fx['text']).strip())
        self.assertEqual(page['ocr_visual_review'], case['warning'])
        self.assertEqual(page['text_sources'], [case['source']] if has_text and case['source'] != 'visual' else [])
        self.assertEqual(page['evidence_attempts'], [case['source']])
        self.assertEqual(page['text_unit_count'], int(has_text))
        self.assertEqual(page['text_coverage'], 'text_unit_present' if has_text else 'no_text_unit')
        self.assertEqual(page['visual_attempt_count'], int(case['source'] == 'visual'))
        self.assertEqual(page['visual_review'], 'not_established')
        self.assertEqual(result['coverage'][0]['visual_review'], 'not_established')
        self.assertIs(result['partial_understanding'], bool(case['gaps']))
        self.assertIs(result['review_acceptance'], False)
        self.assertEqual(result['semantic_assessment'], 'NOT_ASSESSED')
        self.assertEqual(bundle, saved)
        self.assertEqual(self.document.analyze(bundle), result)
        for item in result['classifications']:
            citation = item['citation']
            unit = bundle['units'][0]
            self.evidence.validate_citation(citation, bundle)
            self.assertEqual(citation, self.evidence.citation(unit, 0, 6))
            self.assertEqual(citation['quote'], 'Policy')
        if has_text:
            self.assertEqual(len(result['classifications']), 1)
        return result

    def group(self, name):
        for case in self.fx['cases']:
            if case['group'] == name:
                with self.subTest(case=case['id']):
                    self.check_case(case)

    def test_kind_only(self):
        self.group('kind_only')

    def test_explicit_control(self):
        self.check_case(next(c for c in self.fx['cases'] if c['id'] == 'explicit_data_true'))

    def test_existing_markers(self):
        self.group('existing_markers')
        attempt = next(c for c in self.fx['cases'] if c['id'] == 'attempt_ocr')
        bundle = self.build([attempt])
        receipts = deepcopy(bundle['source_receipts'])
        self.assertEqual(receipts[0]['attempt_kind'], attempt['attempt'])
        receipts[0]['attempt_kind'] = 'native'
        with self.assertRaisesRegex(ValueError, '^receipt_attempt_kind_mismatch$'):
            self.evidence.bundle(bundle['units'], bundle['source_context'], receipts)
        self.evidence.validate_bundle(bundle)
        for case in self.fx['predicate_cases']:
            with self.subTest(case=case['id']):
                self.assertIs(self.document._is_ocr(case['unit']), case['expected'])
                expected = 'ocr' if case['expected'] else 'native'
                self.assertEqual(self.document._source_kind(case['unit']), expected)
                # Top-level legacy flags cannot pass the strict receipt schema;
                # qualify their existing warning consumer directly, not by
                # weakening evidence validation or inventing a receipt shape.
                legacy = {'kind': 'text', 'text': self.fx['text'], 'locator': {'page': 1},
                          'original_sha256': 'synthetic', **deepcopy(case['unit'])}
                rows, gaps = self.document._coverage({'units': [legacy],
                    'source_context': {'synthetic': {'pages': [1], 'page_count': 1}}})
                self.assertEqual(gaps, ['ocr_visual_review_required'] if case['expected'] else [])
                self.assertEqual(rows[0]['pages'][0]['ocr_visual_review'],
                                 'required' if case['expected'] else 'not_established')

    def test_native_visual(self):
        self.group('native_visual')

    def test_mixed_empty(self):
        self.group('mixed_empty')
        ids = ('native', 'kind_ocr_text', 'empty_ocr')
        cases = [next(c for c in self.fx['cases'] if c['id'] == name) for name in ids]
        bundle = self.build(cases)
        saved = deepcopy(bundle)
        result = self.document.analyze(bundle)
        self.assertEqual(result['gaps'], ['ocr_visual_review_required', 'page_without_text_unit'])
        self.assertIs(result['partial_understanding'], True)
        self.assertIs(result['review_acceptance'], False)
        self.assertEqual([(p['text_sources'], p['ocr_visual_review'], p['text_unit_count'])
                          for p in result['coverage'][0]['pages']],
                         [(['native'], 'not_established', 1), (['ocr'], 'required', 1), ([], 'not_established', 0)])
        # Reorder already-bound units; preserve exact source/citation identities.
        body = {k: deepcopy(v) for k, v in bundle.items() if k != 'snapshot_sha256'}
        body['units'].reverse()
        reordered = {**body, 'snapshot_sha256': self.evidence.sha(body)}
        other = self.document.analyze(reordered)
        self.assertEqual(other['coverage'], result['coverage'])
        self.assertEqual(other['gaps'], result['gaps'])
        self.assertIs(other['review_acceptance'], False)
        for key in ('classifications', 'entity_candidates', 'date_mentions'):
            self.assertEqual(sorted(other[key], key=lambda x: x['unit_id']),
                             sorted(result[key], key=lambda x: x['unit_id']))
        original_facts = {f['unit_id']: f for f in result['summary_facts']}
        self.assertEqual(set(original_facts), {f['unit_id'] for f in other['summary_facts']})
        for fact in other['summary_facts']:
            previous = original_facts[fact['unit_id']]
            self.assertEqual(fact['kind'], previous['kind'])
            # Reordering changes the bundle snapshot and thus the derived-claim
            # digest, but never its exact support/citation, text or context.
            old_claim = previous['claim']
            new_claim = fact['claim']
            self.assertEqual({k: v for k, v in new_claim.items() if k not in ('snapshot_sha256', 'evidence_sha256')},
                             {k: v for k, v in old_claim.items() if k not in ('snapshot_sha256', 'evidence_sha256')})
            self.assertEqual(old_claim['snapshot_sha256'], bundle['snapshot_sha256'])
            self.assertEqual(new_claim['snapshot_sha256'], reordered['snapshot_sha256'])
            self.evidence.validate_claim(new_claim, reordered)
        for item in other['classifications']:
            self.evidence.validate_citation(item['citation'], reordered)
        self.assertEqual(bundle, saved)
        self.assertEqual(self.document.analyze(bundle), result)

    def test_isolated_kind_mutant(self):
        def no_kind(unit):
            data = unit.get('data') if isinstance(unit.get('data'), dict) else {}
            return bool('ocr' in str(unit.get('attempt_kind', '')).lower()
                        or data.get('ocr_receipt_id') or data.get('ocr') is True or unit.get('ocr') is True)
        kind = next(c for c in self.fx['cases'] if c['id'] == 'kind_ocr_text')
        explicit = next(c for c in self.fx['cases'] if c['id'] == 'explicit_data_true')
        with patch.object(self.document, '_is_ocr', no_kind):
            with self.assertRaisesRegex(AssertionError, 'ocr-global-warning'):
                self.check_case(kind)
            self.check_case(explicit)
        self.check_case(kind)
        self.check_case(explicit)


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ids = []
        self.subcases = []

    def startTest(self, test):
        self.ids.append(test.id())
        super().startTest(test)

    def addSubTest(self, test, subtest, err):
        self.subcases.append({'id': subtest.id(), 'passed': err is None})
        super().addSubTest(test, subtest, err)


def snapshot():
    return (list(sys.path), dict(os.environ), tempfile.tempdir, socket.socket,
            subprocess.Popen, sqlite3.connect,
            {k: id(v) for k, v in sys.modules.items() if k == 'campaign_tool' or k.startswith('campaign_tool.')})


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--temp-root', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--mode', choices=('matrix', 'baseline', 'compatibility', 'discovery'), default='matrix')
    args = parser.parse_args()
    args.temp_root.mkdir(parents=True, exist_ok=False)
    global TEMP_ROOT
    TEMP_ROOT = args.temp_root
    before = snapshot()
    loader = unittest.TestLoader()
    if args.mode == 'compatibility':
        with offline(args.temp_root):
            suite = loader.loadTestsFromName('tests.records.test_fc_doc_coverage.FcDocCoverageTests')
            result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    else:
        if args.mode == 'baseline':
            suite = unittest.TestSuite([OcrKindTests('test_kind_only'), OcrKindTests('test_explicit_control')])
        elif args.mode == 'discovery':
            class Peer(unittest.TestCase):
                def runTest(self):
                    self.assertEqual(snapshot(), before, 'discovery leaked process-global state')
            # Discovery itself must be inert as well as running the discovered suite.
            suite = unittest.TestSuite([Peer(), loader.discover(str(ROOT / 'tests/records'),
                                       pattern='test_fc16_document_ocr_kind.py', top_level_dir=str(ROOT)), Peer()])
            # unittest discovery intentionally adds the top-level dir. Scope that
            # documented loader behavior, not any changes made by the module.
            sys.path[:] = before[0]
        else:
            suite = loader.loadTestsFromTestCase(OcrKindTests)
        result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    unittest.TestCase().assertEqual(snapshot(), before, 'runner leaked process-global state')
    report = {'mode': args.mode, 'tests': result.ids, 'subcases': result.subcases,
              'count': result.testsRun, 'skips': len(result.skipped),
              'failures': [(t.id(), msg) for t, msg in result.failures],
              'errors': [(t.id(), msg) for t, msg in result.errors], 'success': result.wasSuccessful()}
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    return int(not result.wasSuccessful() or bool(result.skipped))


if __name__ == '__main__':
    raise SystemExit(main())
