"""FC16: fictional literal segmentation, never whole-source meaning.

Import/discovery is inert: no product imports, environment changes or permanent
hooks. Script mode accepts its temporary root before importing product modules.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import inspect
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
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / 'tests/fixtures/fc16_document_summary_segments/cases.json'
COMPATIBILITY = ('tests.records.test_understanding.EvidenceTests',
                 'tests.records.test_understanding.GroundedEvidenceTests')


def frozen():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


@contextmanager
def offline_scope(temp_root=None):
    """Scoped tripwires, not a hostile-code sandbox. No live probes."""
    def forbidden(*args, **kwargs):
        raise RuntimeError('FC16 offline side effect prohibited')
    with ExitStack() as stack:
        for target in ('socket.socket', 'socket.create_connection',
                       'socket.socketpair', 'sqlite3.connect',
                       'subprocess.Popen', 'os.system', 'os.popen'):
            stack.enter_context(mock.patch(target, forbidden))
        if temp_root is not None:
            stack.enter_context(mock.patch.object(tempfile, 'tempdir', str(temp_root)))
        yield


def product():
    # Called only inside offline_scope, never during ordinary test discovery.
    from campaign_tool.records.understanding import document, evidence
    return document, evidence


def bundle_for(texts, context_tag='original'):
    _, evidence = product()
    original = hashlib.sha256(b'FC16 fictional native document').hexdigest()
    receipt = {'schema': 'format-extraction-v1', 'original_sha256': original,
               'original_changed': False, 'status': 'complete',
               'parser': 'synthetic-fc16', 'parser_version': '0',
               'route': {'format': 'pdf'},
               'pages': [{'page_no': i, 'status': 'ok'} for i in range(1, len(texts)+1)],
               'units': [{'kind': 'text', 'locator': {'page': i}, 'text': text, 'data': {}}
                         for i, text in enumerate(texts, 1)]}
    raw = evidence.canonical(receipt)
    return evidence.normalize_receipt(receipt, original, evidence.sha(raw),
                                      extra_context={'synthetic_context': context_tag})


class SegmentTests(unittest.TestCase):
    def setUp(self):
        scope = offline_scope()
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        self.document, self.evidence = product()

    def check_case(self, row, analyze=None):
        """All expected quotes/spans come from the independent committed JSON."""
        bundle = bundle_for(row['units'])
        before = deepcopy(bundle)
        result = (analyze or self.document.analyze)(bundle)
        facts = result['summary_facts']
        self.assertEqual(len(facts), len(row['expected']), 'candidate_presence:'+row['id'])
        for index, (fact, expected) in enumerate(zip(facts, row['expected'])):
            unit = bundle['units'][expected['unit']]
            claim = fact['claim']
            self.assertEqual(len(claim['supports']), 1, 'single_actual_unit_support')
            support = claim['supports'][0]
            self.assertEqual(support['quote'], expected['quote'], 'literal_quote:'+row['id'])
            self.assertEqual(support['span'], {'start_byte': expected['start_byte'],
                                              'end_byte': expected['end_byte']}, 'literal_byte_span')
            self.assertEqual(unit['text'].encode('utf-8')[expected['start_byte']:expected['end_byte']],
                             expected['quote'].encode('utf-8'), 'independent_fixture_span')
            self.assertEqual(fact['unit_id'], unit['unit_id'])
            for key in ('unit_id', 'original_sha256', 'import_id', 'artifact_sha256', 'text_sha256', 'locator'):
                self.assertEqual(support[key], unit[key], 'actual_unit_binding:'+key)
            self.assertEqual(fact['kind'], 'verbatim_summary_candidate')
            self.assertEqual(claim['claim'], 'Source text states: '+expected['quote'])
            self.assertIs(claim['derived'], True)
            self.assertEqual(claim['semantic_assessment'], 'NOT_ASSESSED')
            self.assertIs(claim['review_acceptance'], False)
            self.assertEqual(claim['snapshot_sha256'], bundle['snapshot_sha256'])
            self.assertEqual(claim['context_sha256'], self.evidence.sha(bundle['source_context']))
            checked_support = self.evidence.validate_citation(support, bundle)
            checked_claim = self.evidence.validate_claim(claim, bundle)
            for checked in (checked_support, checked_claim):
                self.assertEqual(checked['structural_support'], 'verified_exact_retained_bytes')
                self.assertEqual(checked['semantic_assessment'], 'NOT_ASSESSED')
                self.assertIs(checked['review_acceptance'], False)
        self.assertIs(result['review_acceptance'], False)
        self.assertEqual(result['semantic_assessment'], 'NOT_ASSESSED')
        self.assertEqual(bundle, before, 'caller_bundle_unchanged')
        return bundle, result

    def test_binding_repeat_and_current_context(self):
        row = next(r for r in frozen()['cases'] if r['id'] == 'source_units_long')
        bundle, first = self.check_case(row)
        saved = deepcopy(bundle)
        first_copy = deepcopy(first)
        self.assertEqual(self.document.analyze(bundle), first)
        self.assertEqual(bundle, saved)
        self.assertEqual(first, first_copy)
        # A fresh context binds a fresh claim; it does not inherit acceptance.
        changed = bundle_for(row['units'], 'new-context')
        changed_before = deepcopy(changed)
        second = self.document.analyze(changed)
        self.assertNotEqual(second['snapshot_sha256'], first['snapshot_sha256'])
        for old, new in zip(first['summary_facts'], second['summary_facts']):
            self.assertEqual(old['claim']['supports'], new['claim']['supports'])
            self.assertNotEqual(old['claim']['context_sha256'], new['claim']['context_sha256'])
            self.assertEqual(new['claim']['context_sha256'], self.evidence.sha(changed['source_context']))
            self.assertIs(new['claim']['derived'], True)
            self.assertIs(new['claim']['review_acceptance'], False)
            self.assertEqual(new['claim']['semantic_assessment'], 'NOT_ASSESSED')
            self.evidence.validate_claim(new['claim'], changed)
        self.assertEqual(changed, changed_before)
        self.assertEqual(bundle, saved)

    def isolated_extract(self, replacements=(), globals_delta=None):
        # Exact function copy only, compiled in memory with a private globals map.
        source = inspect.getsource(self.document._extract)
        for old, new in replacements:
            self.assertEqual(source.count(old), 1, 'mutation_target_unique')
            source = source.replace(old, new, 1)
        namespace = dict(self.document.__dict__)
        namespace.update(globals_delta or {})
        exec(compile(source, '<fc16-isolated-candidate-loop>', 'exec'), namespace)
        return namespace['_extract']

    def test_control_minimum_11_mutant(self):
        row = next(r for r in frozen()['cases'] if r['id'] == 'body_11_bare')
        positive = next(r for r in frozen()['cases'] if r['id'] == 'body_12_bare')
        original = self.document._extract
        self.check_case(row)
        self.check_case(positive)
        # The minimum is enforced by BOTH regex body and post-trim gate. Lower
        # both in the isolated loop; changing regex alone is masked by trim.
        mutant = self.isolated_extract((('{12,512}', '{11,512}'),
                                        ('if len(quote) < 12:', 'if len(quote) < 11:')))
        mutated = mutant(bundle_for(row['units']))
        self.assertEqual(mutated['summary_facts'][0]['claim']['supports'][0]['quote'], 'AAAAAAAAAAA')
        with self.assertRaisesRegex(AssertionError, 'candidate_presence:body_11_bare') as caught:
            self.check_case(row, mutant)
        print('CONTROL minimum_11_mutant detected: '+str(caught.exception))
        self.assertIs(self.document._extract, original)
        self.check_case(row)
        self.check_case(positive)

    def test_control_adjacent_whitespace_mutant(self):
        row = next(r for r in frozen()['cases'] if r['id'] == 'ascii_trim')
        original = self.document._citation
        self.check_case(row)
        def expanded(bundle, unit, start, end):
            return original(bundle, unit, start, end+1)
        mutant = self.isolated_extract(globals_delta={'_citation': expanded})
        mutated_bundle = bundle_for(row['units'])
        mutated = mutant(mutated_bundle)
        support = mutated['summary_facts'][0]['claim']['supports'][0]
        self.assertEqual(support['quote'], 'AAAAAAAAAAAA ')
        # Still structurally valid: the exact literal-quote assertion decides.
        self.evidence.validate_citation(support, mutated_bundle)
        with self.assertRaisesRegex(AssertionError, 'literal_quote:ascii_trim') as caught:
            self.check_case(row, mutant)
        print('CONTROL adjacent_whitespace_mutant detected: '+str(caught.exception))
        self.assertIs(self.document._citation, original)
        self.check_case(row)


def _case_method(row):
    def test(self):
        self.check_case(row)
    test.__doc__ = row['group']+': '+row['id']+'; literal oracle and binding assertions'
    return test


for _row in frozen()['cases']:
    setattr(SegmentTests, 'test_'+_row['group']+'_'+_row['id'], _case_method(_row))
del _row


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ids = []
        self.subcases = []

    def startTest(self, test):
        self.ids.append(test.id())
        super().startTest(test)

    def addSubTest(self, test, subtest, err):
        self.subcases.append({'id': subtest.id(), 'ok': err is None})
        super().addSubTest(test, subtest, err)


def ambient():
    return {'environment': dict(os.environ), 'tempdir': tempfile.tempdir,
            'path': list(sys.path), 'socket': socket.socket,
            'connect': sqlite3.connect, 'popen': subprocess.Popen}


def run_script():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--temp-root', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--mode', choices=('suite', 'compatibility', 'discovery'), default='suite')
    args = parser.parse_args()
    args.temp_root.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(REPO))
    # Child-only bytecode suppression is supplied with -B by the recorder.
    initial = ambient()
    with offline_scope(args.temp_root):
        if args.mode == 'compatibility':
            suite = unittest.defaultTestLoader.loadTestsFromNames(COMPATIBILITY)
        elif args.mode == 'discovery':
            before_import = ambient()
            imported_before = dict(sys.modules)
            suite = unittest.TestLoader().discover(str(REPO/'tests/records'),
                pattern=Path(__file__).name, top_level_dir=str(REPO))
            if ambient() != before_import:
                raise RuntimeError('discovery changed ambient defaults')
            # Discovery may register the test module normally, but must not
            # import product modules or replace any existing module object.
            if any(sys.modules.get(k) is not v for k,v in imported_before.items()):
                raise RuntimeError('discovery replaced existing import state')
            if any(k.startswith('campaign_tool') for k in set(sys.modules)-set(imported_before)):
                raise RuntimeError('discovery imported product')
            expected_peer = ambient()
            class PeerBefore(unittest.TestCase):
                def runTest(self):
                    self.assertEqual(ambient(), expected_peer)
                    self.assertEqual(json.loads('{"peer":[2,1]}'), {'peer': [2,1]})
                    self.assertEqual(Path(tempfile.gettempdir()), args.temp_root)
            class PeerAfter(unittest.TestCase):
                def runTest(self):
                    self.assertEqual(ambient(), expected_peer)
                    self.assertEqual(json.loads('{"peer":[2,1]}'), {'peer': [2,1]})
                    self.assertEqual(Path(tempfile.gettempdir()), args.temp_root)
            suite = unittest.TestSuite([PeerBefore(), suite, PeerAfter()])
        else:
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(SegmentTests)
        result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
    if ambient() != initial:
        raise RuntimeError('test process ambient defaults not restored')
    report = {'mode': args.mode, 'tests_run': result.testsRun, 'ids': result.ids,
              'subcases': result.subcases, 'skips': result.skipped,
              'failures': [(t.id(), text) for t,text in result.failures],
              'errors': [(t.id(), text) for t,text in result.errors],
              'successful': result.wasSuccessful(), 'ambient_restored': True,
              'selectors': list(COMPATIBILITY) if args.mode == 'compatibility' else [],
              'fixture_sha256': hashlib.sha256(FIXTURE.read_bytes()).hexdigest()}
    args.report.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    return int(not result.wasSuccessful() or bool(result.skipped))


if __name__ == '__main__':
    raise SystemExit(run_script())
