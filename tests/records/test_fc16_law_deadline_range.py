"""Synthetic, offline provisional-reminder arithmetic qualification.

Ordinary discovery imports stdlib only and changes no defaults or product imports.
Script mode requires --temporary-root before any product/compatibility import.
The JSON fixture holds independently frozen literal expected dates, not legal rules.
"""
import argparse
import ast
from contextlib import ExitStack, contextmanager
import copy
from datetime import date, timedelta
import hashlib
import importlib.util
import io
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
FIXTURE = REPO / 'tests/fixtures/fc16_law_deadline_range/cases.json'
ERROR = 'law_deadline_out_of_range'


@contextmanager
def offline():
    """Scoped sentinels before imports; no permanent audit hooks or fake modules."""
    def refused(*args, **kwargs):
        raise AssertionError('offline_effect_refused')
    with ExitStack() as stack:
        for target in (socket.socket,):
            stack.enter_context(mock.patch.object(target, 'connect', refused))
            stack.enter_context(mock.patch.object(target, 'connect_ex', refused))
        stack.enter_context(mock.patch.object(socket, 'create_connection', refused))
        stack.enter_context(mock.patch.object(urllib.request, 'urlopen', refused))
        stack.enter_context(mock.patch.object(subprocess, 'Popen', refused))
        yield


def isolated_law():
    spec = importlib.util.spec_from_file_location('_fc16_isolated_law', REPO / 'campaign_tool/law.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeadlineRangeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(offline())
        self.law = isolated_law()
        self.fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.schema = self.law.load_schema()

    def package(self, mode, days, extra=0):
        package = copy.deepcopy(self.fixture['package'])
        package['records_law'].update(day_type=mode, determination_days=days,
                                      determination_extension_days=extra)
        self.law.validate(package, self.schema)
        return package

    def assert_refusal(self, package, start, extension=False):
        with self.assertRaises(ValueError) as caught:
            self.law.deadline(package, start, extension=extension)
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(str(caught.exception), ERROR)
        self.assertIsNone(caught.exception.__cause__)

    def check_case(self, case):
        rows = [row for row in self.fixture['rows'] if row['case'] == case]
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(case=case, fixture_id=row['id']):
                package = self.package(row['mode'], row['days'], row['extra'])
                before = copy.deepcopy(package)
                start = date.fromisoformat(row['start'])
                if row['expected'] is None:
                    self.assert_refusal(package, start, row['extension'])
                else:
                    result = self.law.deadline(package, start, extension=row['extension'])
                    self.assertIs(type(result), date)
                    self.assertEqual(result.isoformat(), row['expected'])
                self.assertEqual(package, before)

    def test_range_edges(self):
        self.check_case('range_edges')

    def test_weekday_boundary(self):
        self.check_case('weekday_boundary')
        weekdays = {date.fromisoformat(r['start']).weekday() for r in self.fixture['rows']
                    if r['case'] == 'weekday_boundary'}
        self.assertEqual(weekdays, set(range(7)))

    def test_preservation(self):
        self.check_case('preservation')
        self.assertIn('Saturday and Sunday only', self.law.HOLIDAY_NOTE)
        self.assertIn('not legal due dates', self.law.HOLIDAY_NOTE)

    def test_zero_helper_preserves_start(self):
        # Schema determination_days starts at one; helper's zero arithmetic is unchanged.
        for start in (date.min, date(2026, 1, 3), date.max):
            for mode in ('calendar', 'business'):
                with self.subTest(start=start.isoformat(), mode=mode):
                    self.assertEqual(self.law._add_days(start, 0, mode), start)

    def bounded_refusal(self, mode, huge_extension=False):
        package = self.package(mode, 1 if huge_extension else 10**100,
                               10**100 if huge_extension else 0)
        before = copy.deepcopy(package)
        calls = []
        def step(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError('date_step_before_refusal')
        with mock.patch.object(self.law, 'timedelta', side_effect=step):
            self.assert_refusal(package, date.min, huge_extension)
        self.assertEqual(calls, [])
        self.assertEqual(package, before)

    def test_large_integer_before_date_step(self):
        for mode in ('calendar', 'business'):
            for huge_extension in (False, True):
                with self.subTest(case='large_integer', mode=mode, huge_extension=huge_extension):
                    self.bounded_refusal(mode, huge_extension)

    def test_deciding_preflight_mutants(self):
        source = (REPO / 'campaign_tool/law.py').read_text(encoding='utf-8')
        for mode, bound in (('calendar', 'available'), ('business', 'capacity')):
            with self.subTest(control='remove_representability_only', mode=mode):
                tree = ast.parse(source)
                function = next(node for node in tree.body
                                if isinstance(node, ast.FunctionDef) and node.name == '_add_days')
                class RemoveGate(ast.NodeTransformer):
                    removed = 0
                    def visit_If(self, node):
                        if ast.dump(node.test) == ast.dump(ast.parse('days > ' + bound, mode='eval').body):
                            self.removed += 1
                            return None
                        return self.generic_visit(node)
                transform = RemoveGate()
                mutated = transform.visit(function)
                self.assertEqual(transform.removed, 1, 'mutant must alter exactly the named preflight')
                namespace = dict(self.law.__dict__)
                module = ast.fix_missing_locations(ast.Module(body=[mutated], type_ignores=[]))
                exec(compile(module, '<isolated-preflight-mutant>', 'exec'), namespace)
                original = self.law._add_days
                try:
                    self.law._add_days = namespace['_add_days']
                    # The assertion seam is the deciding failure, before even one date step.
                    calls = []
                    def step(*args, **kwargs):
                        calls.append(1)
                        raise AssertionError('date_step_before_refusal')
                    # Mutant globals are separate; patch its timedelta, not the product module.
                    namespace['timedelta'] = step
                    with self.assertRaisesRegex(AssertionError, '^date_step_before_refusal$'):
                        self.assert_refusal(self.package(mode, 10**100), date.min)
                    self.assertEqual(calls, [1])
                finally:
                    self.law._add_days = original
                self.bounded_refusal(mode)

    def test_fixture_integrity_and_case_map(self):
        rows = self.fixture['rows']
        self.assertEqual(len(rows), 328)
        self.assertEqual(len({r['id'] for r in rows}), len(rows))
        self.assertEqual({r['case'] for r in rows}, {'range_edges', 'weekday_boundary', 'preservation'})
        for mode in ('calendar', 'business'):
            exact = next(r for r in rows if r['id'] == 'exact-' + mode + '-ext0')
            beyond = next(r for r in rows if r['id'] == 'exact-' + mode + '-ext1')
            self.assertEqual(exact['expected'], '9999-12-31')
            self.assertIsNone(beyond['expected'])


class BaselineControls(DeadlineRangeTests):
    """Selected only explicitly, not via discovery (load_tests below)."""
    def test_ordinary_control(self):
        self.assertEqual(self.law.deadline(self.package('calendar', 10), date(2026, 1, 2)), date(2026, 1, 12))
        self.assertEqual(self.law.deadline(self.package('business', 10), date(2026, 1, 2)), date(2026, 1, 16))

    def test_calendar_date_max_typed_refusal(self):
        self.assert_refusal(self.package('calendar', 10), date.max)

    def test_business_date_max_typed_refusal(self):
        self.assert_refusal(self.package('business', 10), date.max)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(DeadlineRangeTests)


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ids = []
        self.subcases = []
    def startTest(self, test):
        self.ids.append(test.id())
        super().startTest(test)
    def addSubTest(self, test, subtest, err):
        self.subcases.append({'id': subtest.id(), 'outcome': 'pass' if err is None else 'fail'})
        super().addSubTest(test, subtest, err)


def run_suite(suite, report_path):
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    report = {'tests': result.testsRun, 'ids': result.ids, 'subcases': result.subcases,
              'skips': result.skipped, 'failures': [(t.id(), text) for t, text in result.failures],
              'errors': [(t.id(), text) for t, text in result.errors], 'success': result.wasSuccessful() and not result.skipped}
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
            self.assertEqual({k: v for k, v in sys.modules.items() if k.startswith('campaign_tool')}, before['campaign_modules'])
    loader = unittest.TestLoader()
    discovered = loader.discover(str(Path(__file__).parent), pattern=Path(__file__).name)
    return unittest.TestSuite([Peer(), discovered, Peer()])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--temporary-root', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--mode', choices=('suite', 'baseline', 'compatibility', 'discovery'), default='suite')
    args = parser.parse_args()
    root = args.temporary_root.resolve(strict=True)
    if not root.is_dir() or REPO == root or REPO in root.parents:
        parser.error('temporary root must be an existing directory outside repository')
    with mock.patch.object(tempfile, 'tempdir', str(root)), offline():
        loader = unittest.TestLoader()
        if args.mode == 'baseline':
            suite = unittest.TestSuite(BaselineControls(name) for name in (
                'test_ordinary_control', 'test_calendar_date_max_typed_refusal', 'test_business_date_max_typed_refusal'))
        elif args.mode == 'compatibility':
            old_path = list(sys.path)
            try:
                sys.path.insert(0, str(REPO))
                suite = loader.loadTestsFromName('tests.test_law.LawFunctionTests')
            finally:
                sys.path[:] = old_path
        elif args.mode == 'discovery':
            suite = discovery_suite()
        else:
            suite = loader.loadTestsFromTestCase(DeadlineRangeTests)
        return run_suite(suite, args.report)


if __name__ == '__main__':
    raise SystemExit(main())
