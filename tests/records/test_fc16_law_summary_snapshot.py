"""Synthetic, offline qualification that law.summary detaches reviewed_by.

Ordinary discovery imports only the standard library. It changes no defaults,
sys.path entries or product imports: law.py is loaded per test as an isolated
module object from its file path. Script mode requires --temporary-root, and
installs scoped offline sentinels and the temporary-root redirect before any
product or compatibility import. The fixture holds literal oracle values
transcribed from the declared package; reviewer strings are synthetic.
Passing is REVIEW REQUIRED only, never legal or factual acceptance.
"""
import argparse
import ast
from contextlib import ExitStack, contextmanager
import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / 'tests/fixtures/fc16_law_summary_snapshot/cases.json'
# Script-mode baseline only: points at law.py inside an exact base-commit clone.
LAW_PATH = REPO / 'campaign_tool/law.py'
JURISDICTION = 'us-ca'
ADDED = 'SYNTHETIC-ADDED'
REPLACED = 'SYNTHETIC-REPLACED'
DIRECT_REFERENCE = 'package["reviewed_by"]'
DETACHED_PROJECTION = 'list(package["reviewed_by"])'


def _append(values):
    values.append(ADDED)


def _replace(values):
    values[0] = REPLACED


def _remove(values):
    values.pop()


OPERATIONS = {'append': _append, 'replace': _replace, 'remove': _remove}


@contextmanager
def offline():
    """Scoped sentinels before imports; no permanent audit hooks or fake modules."""
    def refused(*args, **kwargs):
        raise AssertionError('offline_effect_refused')
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(socket.socket, 'connect', refused))
        stack.enter_context(mock.patch.object(socket.socket, 'connect_ex', refused))
        stack.enter_context(mock.patch.object(socket, 'create_connection', refused))
        stack.enter_context(mock.patch.object(urllib.request, 'urlopen', refused))
        stack.enter_context(mock.patch.object(subprocess, 'Popen', refused))
        yield


def isolated_law():
    spec = importlib.util.spec_from_file_location('_fc16_summary_isolated_law', LAW_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LawSummarySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(offline())
        self.law = isolated_law()
        self.fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.oracle = self.fixture['declared_oracle']
        self.overlay_values = self.fixture['synthetic_overlay']['reviewed_by']
        self.schema = self.law.load_schema()

    # Package-shaped inputs -------------------------------------------------
    def declared(self):
        return self.law.load_package(JURISDICTION)

    def overlay(self):
        package = self.declared()
        overlay = self.fixture['synthetic_overlay']
        package['status'] = overlay['status']
        package['reviewed_at'] = overlay['reviewed_at']
        package['reviewed_by'] = list(overlay['reviewed_by'])
        # Keep the synthetic overlay valid and package-shaped.
        self.law.validate(package, self.schema)
        self.law._semantic_checks(package, JURISDICTION)
        return package

    def expected_overlay_summary(self):
        return {**self.expected_declared_summary(), 'status': 'reviewed',
                'reviewed_at': '2026-01-02', 'reviewed_by': list(self.overlay_values)}

    def expected_declared_summary(self):
        o = self.oracle
        package = self.declared()
        rules = [{key: rule[key] for key in o['rule_keys']} for rule in package['rules']]
        return {'jurisdiction': o['jurisdiction'], 'status': o['status'],
                'reviewed_by': list(o['reviewed_by']), 'reviewed_at': o['reviewed_at'],
                'records_law': dict(o['records_law']), 'rule_count': o['rule_count'],
                'review_counts': dict(o['review_counts']), 'rules': rules,
                'request_scopes': list(o['request_scopes']), 'note': self.law.HOLIDAY_NOTE}

    # Deciding assertion helpers --------------------------------------------
    def assert_two_way(self, side, operation, empty=False):
        """One mutation on one side; the opposite side must stay unchanged."""
        package = self.declared() if empty else self.overlay()
        result = self.law.summary(package)
        status, reviewed_at = package['status'], package['reviewed_at']
        caller_before = list(package['reviewed_by'])
        summary_before = list(result['reviewed_by'])
        self.assertEqual(summary_before, caller_before)
        if side == 'caller':
            target, other, other_before = package['reviewed_by'], result['reviewed_by'], summary_before
        else:
            target, other, other_before = result['reviewed_by'], package['reviewed_by'], caller_before
        expected_target = list(target)
        OPERATIONS[operation](expected_target)
        OPERATIONS[operation](target)
        self.assertEqual(target, expected_target, 'two_way_target_mutation_applied')
        self.assertEqual(other, other_before, 'two_way_' + side + '_' + operation + '_leak')
        self.assertEqual((package['status'], package['reviewed_at']), (status, reviewed_at))
        self.assertEqual((result['status'], result['reviewed_at']), (status, reviewed_at))

    def assert_repeat(self, subcase):
        package = self.overlay()
        caller_before = list(package['reviewed_by'])
        first = self.law.summary(package)
        second = self.law.summary(package)
        self.assertEqual(first, second)
        self.assertEqual(first, self.expected_overlay_summary())
        if subcase == 'mutate_first':
            _append(first['reviewed_by'])
            self.assertEqual(second['reviewed_by'], caller_before, 'repeat_mutate_first_leak')
        elif subcase == 'mutate_second':
            _replace(second['reviewed_by'])
            self.assertEqual(first['reviewed_by'], caller_before, 'repeat_mutate_second_leak')
        else:
            _append(first['reviewed_by'])
            _remove(second['reviewed_by'])
            _remove(second['reviewed_by'])
            fresh = self.law.summary(package)
            self.assertEqual(fresh, self.expected_overlay_summary(), 'repeat_fresh_after_leak')
            self.assertIsNot(fresh['reviewed_by'], first['reviewed_by'])
            self.assertIsNot(fresh['reviewed_by'], second['reviewed_by'])
        self.assertEqual(package['reviewed_by'], caller_before, 'repeat_' + subcase + '_caller_leak')

    def two_way_matrix(self):
        for side in ('caller', 'summary'):
            for operation in ('append', 'replace', 'remove'):
                yield side + '_' + operation, side, operation, False
        for side in ('caller', 'summary'):
            yield 'empty_' + side + '_append', side, 'append', True

    # Frozen cases -----------------------------------------------------------
    def test_two_way(self):
        for subcase, side, operation, empty in self.two_way_matrix():
            with self.subTest(case='two_way', subcase=subcase):
                self.assert_two_way(side, operation, empty)

    def test_repeat(self):
        for subcase in ('mutate_first', 'mutate_second', 'fresh_after'):
            with self.subTest(case='repeat', subcase=subcase):
                self.assert_repeat(subcase)

    def test_empty_and_order(self):
        with self.subTest(case='empty_and_order', subcase='empty_draft'):
            package = self.declared()
            self.assertEqual(package['reviewed_by'], [])
            result = self.law.summary(package)
            self.assertIs(type(result['reviewed_by']), list)
            self.assertEqual(result['reviewed_by'], [])
            self.assertEqual(result['status'], 'draft')
            self.assertIsNone(result['reviewed_at'])
        with self.subTest(case='empty_and_order', subcase='ordered_overlay'):
            package = self.overlay()
            result = self.law.summary(package)
            self.assertIs(type(result['reviewed_by']), list)
            self.assertEqual(len(result['reviewed_by']), 4)
            for index, literal in enumerate(self.overlay_values):
                self.assertIs(type(result['reviewed_by'][index]), str)
                self.assertEqual(result['reviewed_by'][index], literal)
            self.assertEqual(result['reviewed_by'][1], ' Synthetic Reviewer B ')
            self.assertEqual(result['reviewed_by'][2], 'Synthetic R\u00e9viewer C')
            self.assertEqual(result['reviewed_by'][0], result['reviewed_by'][3])
            self.assertEqual(result['status'], 'reviewed')
            self.assertEqual(result['reviewed_at'], '2026-01-02')
            self.assertEqual(package['reviewed_by'], self.overlay_values)

    def assert_projection(self, package, result, expected_reviewers, status, reviewed_at):
        o = self.oracle
        self.assertEqual(list(result), o['keys'])
        self.assertEqual(result['jurisdiction'], o['jurisdiction'])
        self.assertEqual(result['status'], status)
        self.assertEqual(result['reviewed_at'], reviewed_at)
        self.assertEqual(result['reviewed_by'], expected_reviewers)
        self.assertEqual(result['records_law'], o['records_law'])
        self.assertEqual(result['rule_count'], o['rule_count'])
        self.assertEqual(result['review_counts'], o['review_counts'])
        self.assertEqual(sum(result['review_counts'].values()), o['rule_count'])
        self.assertEqual([r['rule_id'] for r in result['rules']], o['rule_ids'])
        self.assertEqual([r['review'] for r in result['rules']], o['rule_reviews'])
        for rule, source in zip(result['rules'], package['rules'], strict=True):
            self.assertEqual(list(rule), o['rule_keys'])
            self.assertEqual(rule, {key: source[key] for key in o['rule_keys']})
        partition = {'verified': 0, 'likely': 0, 'needs_attorney_review': 0}
        for source in package['rules']:
            partition[source['review']] += 1
        self.assertEqual(partition, o['review_counts'])
        self.assertEqual(result['request_scopes'], o['request_scopes'])
        self.assertEqual(result['request_scopes'], [s['scope_id'] for s in package['request_scopes']])
        self.assertEqual(result['note'], self.law.HOLIDAY_NOTE)
        for fragment in o['note_fragments']:
            self.assertIn(fragment, result['note'])
        law_source = package['records_law']
        for key, value in o['records_law'].items():
            self.assertEqual(law_source[key], value)

    def test_projection(self):
        with self.subTest(case='projection', subcase='declared_package'):
            package = self.declared()
            before = copy.deepcopy(package)
            result = self.law.summary(package)
            self.assert_projection(package, result, [], 'draft', None)
            self.assertEqual(result, self.expected_declared_summary())
            self.assertEqual(package, before)
        with self.subTest(case='projection', subcase='overlay_package'):
            package = self.overlay()
            before = copy.deepcopy(package)
            result = self.law.summary(package)
            self.assert_projection(package, result, self.overlay_values, 'reviewed', '2026-01-02')
            self.assertEqual(result, self.expected_overlay_summary())
            self.assertEqual(package, before)

    def direct_reference_mutant(self):
        """Isolated in-memory summary copy with only the reviewed_by detachment undone."""
        source = LAW_PATH.read_text(encoding='utf-8')
        tree = ast.parse(source)
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == 'summary')
        detached = ast.dump(ast.parse(DETACHED_PROJECTION, mode='eval').body)

        class ReinsertDirectReference(ast.NodeTransformer):
            replaced = 0

            def visit_Call(self, node):
                if ast.dump(node) == detached:
                    self.replaced += 1
                    return node.args[0]
                return self.generic_visit(node)

        transform = ReinsertDirectReference()
        mutated = transform.visit(function)
        self.assertEqual(transform.replaced, 1, 'mutant must alter exactly the reviewed_by projection')
        rendered = ast.unparse(mutated)
        self.assertIn("'reviewed_by': " + ast.unparse(ast.parse(DIRECT_REFERENCE, mode='eval')), rendered)
        self.assertNotIn(ast.unparse(ast.parse(DETACHED_PROJECTION, mode='eval')), rendered)
        namespace = dict(self.law.__dict__)
        module = ast.fix_missing_locations(ast.Module(body=[mutated], type_ignores=[]))
        exec(compile(module, '<isolated-summary-mutant>', 'exec'), namespace)
        return namespace['summary']

    def test_deciding_control(self):
        mutant = self.direct_reference_mutant()
        original = self.law.summary
        try:
            self.law.summary = mutant
            for subcase, side, operation, empty in self.two_way_matrix():
                if empty:
                    continue
                with self.subTest(case='deciding_control', subcase='mutant_' + subcase):
                    with self.assertRaises(AssertionError) as caught:
                        self.assert_two_way(side, operation)
                    self.assertIn('two_way_' + side + '_' + operation + '_leak', str(caught.exception))
            with self.subTest(case='deciding_control', subcase='mutant_equality_still_passes'):
                package = self.overlay()
                result = self.law.summary(package)
                self.assertIs(result['reviewed_by'], package['reviewed_by'])
                self.assertEqual(result, self.expected_overlay_summary())
        finally:
            self.law.summary = original
        with self.subTest(case='deciding_control', subcase='restored_positive'):
            self.assertIs(self.law.summary, original)
            for subcase, side, operation, empty in self.two_way_matrix():
                self.assert_two_way(side, operation, empty)

    def test_fixture_case_map(self):
        case_map = self.fixture['case_map']
        self.assertEqual(list(case_map), ['two_way', 'repeat', 'empty_and_order', 'projection', 'deciding_control'])
        self.assertEqual(case_map['two_way'], [name for name, *_ in self.two_way_matrix()])
        self.assertEqual(case_map['repeat'], ['mutate_first', 'mutate_second', 'fresh_after'])
        self.assertEqual(sum(len(v) for v in case_map.values()), 23)
        provenance = self.fixture['provenance']
        # Exact source identity is bound by the recorder against SOURCE-MANIFEST.json.
        self.assertRegex(provenance['source_commit'], '^[0-9a-f]{40}$')
        self.assertRegex(provenance['source_tree'], '^[0-9a-f]{40}$')
        self.assertEqual(provenance['source_package'], 'campaign_tool/_resources/jurisdictions/us-ca/package.json')


BASELINE_TESTS = ('test_two_way', 'test_repeat', 'test_empty_and_order', 'test_projection')


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(LawSummarySnapshotTests)


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ids = []
        self.subcases = []

    def startTest(self, test):
        self.ids.append(test.id())
        super().startTest(test)

    def addSubTest(self, test, subtest, err):
        outcome = 'pass' if err is None else 'fail'
        self.subcases.append({'id': subtest.id(), 'params': {k: str(v) for k, v in subtest.params.items()},
                              'outcome': outcome,
                              'message': None if err is None else str(err[1])})
        super().addSubTest(test, subtest, err)


def run_suite(suite, report_path):
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    failed = {getattr(t, 'test_case', t).id() for t, _ in result.failures + result.errors}
    report = {'tests': result.testsRun, 'ids': result.ids, 'subcases': result.subcases,
              'outcomes': {i: ('fail' if i in failed else 'pass') for i in result.ids},
              'skips': [(t.id(), reason) for t, reason in result.skipped],
              'failures': [(t.id(), text) for t, text in result.failures],
              'errors': [(t.id(), text) for t, text in result.errors],
              'law_path': str(LAW_PATH),
              'success': result.wasSuccessful() and not result.skipped}
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0 if report['success'] else 1


def discovery_suite():
    """Dedicated process: peer probes straddle ordinary single-module discovery."""
    discovery_path = str(Path(__file__).parent)
    if discovery_path not in sys.path:
        sys.path.insert(0, discovery_path)  # unittest's documented framework search path
    before = {'env': dict(os.environ), 'tempdir': tempfile.tempdir, 'path': list(sys.path),
              'connect': socket.socket.connect, 'popen': subprocess.Popen,
              'campaign_modules': {k: v for k, v in sys.modules.items() if k.startswith('campaign_tool')}}

    class Peer(unittest.TestCase):
        def runTest(self):
            self.assertEqual(dict(os.environ), before['env'])
            self.assertEqual(tempfile.tempdir, before['tempdir'])
            self.assertEqual(sys.path, before['path'])
            self.assertIs(socket.socket.connect, before['connect'])
            self.assertIs(subprocess.Popen, before['popen'])
            self.assertEqual({k: v for k, v in sys.modules.items() if k.startswith('campaign_tool')},
                             before['campaign_modules'])
    loader = unittest.TestLoader()
    discovered = loader.discover(str(Path(__file__).parent), pattern=Path(__file__).name)
    return unittest.TestSuite([Peer(), discovered, Peer()])


def main():
    global LAW_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--temporary-root', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--mode', choices=('suite', 'baseline', 'compatibility', 'discovery'), default='suite')
    parser.add_argument('--law-path', type=Path, help='baseline only: law.py inside an exact base clone')
    parser.add_argument('--compat-root', type=Path, help='compatibility only: repository root to load selectors from')
    args = parser.parse_args()
    root = args.temporary_root.resolve(strict=True)
    if not root.is_dir() or REPO == root or REPO in root.parents:
        parser.error('temporary root must be an existing directory outside repository')
    if (args.law_path is not None) != (args.mode == 'baseline'):
        parser.error('--law-path is required for, and only for, baseline mode')
    if args.compat_root is not None and args.mode != 'compatibility':
        parser.error('--compat-root is only for compatibility mode')
    if args.law_path is not None:
        law_path = args.law_path.resolve(strict=True)
        if law_path.name != 'law.py' or REPO in law_path.parents or not (law_path.parent / '_resources').is_dir():
            parser.error('baseline law path must be law.py in a clone outside the repository')
        LAW_PATH = law_path
    with mock.patch.object(tempfile, 'tempdir', str(root)), offline():
        loader = unittest.TestLoader()
        if args.mode == 'baseline':
            suite = unittest.TestSuite(LawSummarySnapshotTests(name) for name in BASELINE_TESTS)
        elif args.mode == 'compatibility':
            compat_root = (args.compat_root or REPO).resolve(strict=True)
            old_path = list(sys.path)
            try:
                sys.path.insert(0, str(compat_root))
                suite = loader.loadTestsFromNames(['tests.test_law.CaliforniaPackageTests',
                                                   'tests.test_law.LawFunctionTests.test_request_scope_lookup'])
                loaded = sys.modules['tests.test_law'].__file__
                if Path(loaded).resolve() != compat_root / 'tests/test_law.py':
                    parser.error('compatibility selectors loaded from an unexpected root')
            finally:
                sys.path[:] = old_path
        elif args.mode == 'discovery':
            suite = discovery_suite()
        else:
            suite = loader.loadTestsFromTestCase(LawSummarySnapshotTests)
        return run_suite(suite, args.report)


if __name__ == '__main__':
    raise SystemExit(main())
