"""Frozen, fictional weekday-only reminder oracle. No legal acceptance.

Importing this module does not import product code or change global defaults.
Script mode accepts --temporary-root before loading any product/test closure.
"""
import argparse
from contextlib import ExitStack, contextmanager
import copy
from datetime import date, timedelta
import importlib.util
import itertools
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from typing import cast
import urllib.request

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / 'tests/fixtures/fc16_law_business_oracle/cases.json'
CASE_MAP = {
    'all_weekdays': 'BusinessOracleTests.test_all_weekdays',
    'extension_total': 'BusinessOracleTests.test_extension_total',
    'day_basis': 'BusinessOracleTests.test_day_basis',
    'warning': 'BusinessOracleTests.test_warning',
    'deciding_controls': 'BusinessOracleTests.test_deciding_controls',
    'oracle_integrity': 'BusinessOracleTests.test_oracle_integrity',
}


def forbidden(*args, **kwargs):
    raise RuntimeError('offline sentinel: forbidden effect')


@contextmanager
def offline():
    """Scoped precaution for the inspected stdlib-only product import closure."""
    with ExitStack() as stack:
        for target in ('socket.socket', 'socket.create_connection',
                       'urllib.request.urlopen', 'subprocess.Popen',
                       'os.system', 'os.popen'):
            stack.enter_context(mock.patch(target, side_effect=forbidden))
        yield


def real_law():
    # Execute the actual source, with no fake product modules, sys.path writes,
    # package initialization or persistent sys.modules registration.
    spec = importlib.util.spec_from_file_location('fc16_isolated_law', REPO / 'campaign_tool/law.py')
    if spec is None or spec.loader is None:
        raise RuntimeError('real law source loader unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def zeller_weekday(value):
    year, month, day = map(int, value.split('-'))
    if month < 3:
        year, month = year - 1, month + 12
    k, j = year % 100, year // 100
    return ((day + 13 * (month + 1) // 5 + k + k // 4 + j // 4 + 5 * j) % 7 + 5) % 7


class BusinessOracleTests(unittest.TestCase):
    def setUp(self):
        self.effects = ExitStack()
        self.addCleanup(self.effects.close)
        self.effects.enter_context(offline())
        self.fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.law = real_law()

    def package(self, row):
        package = copy.deepcopy(self.fixture['package'])
        package['records_law'].update(determination_days=row['determination_days'],
                                      determination_extension_days=row['extension_days'],
                                      day_type=row['day_type'])
        return package

    def test_oracle_integrity(self):
        """Check complete literal dimensions and dates without product arithmetic."""
        table = self.fixture['calendar']
        labels = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        self.assertEqual(len(table), 67)
        self.assertEqual(table[0]['date'], '2020-02-24')
        self.assertEqual(table[-1]['date'], '2020-04-30')
        self.assertEqual(len({row['date'] for row in table}), 67)
        for index, row in enumerate(table):
            with self.subTest(calendar=row['date']):
                parsed = date.fromisoformat(row['date'])
                self.assertEqual(parsed.toordinal(), date(2020, 2, 24).toordinal() + index)
                weekday = zeller_weekday(row['date'])
                self.assertEqual(parsed.weekday(), weekday)
                self.assertEqual(row['weekday'], labels[weekday])
                self.assertIs(row['business_eligible'], weekday < 5)
                self.assertEqual(row['label'], 'holiday' if row['date'] == '2020-03-02' else '')
        receipts = [row['date'] for row in table[:14]]
        self.assertEqual(self.fixture['receipts'], receipts)
        self.assertEqual(self.fixture['determination_counts'], [1, 2, 5, 10])
        self.assertEqual(self.fixture['extension_counts'], [0, 3])
        expected_dimensions = set(itertools.product(receipts, (1, 2, 5, 10), (0, 3),
                                                    (False, True), ('calendar', 'business')))
        rows = self.fixture['cases']
        actual_dimensions = {(r['received'], r['determination_days'], r['extension_days'],
                              r['extension'], r['day_type']) for r in rows}
        self.assertEqual(len(rows), 448)
        self.assertEqual(len({r['id'] for r in rows}), 448)
        self.assertEqual(actual_dimensions, expected_dimensions)
        for row in rows:
            with self.subTest(oracle=row['id']):
                start = next(i for i, day in enumerate(table) if day['date'] == row['received'])
                candidates = [day for day in table[start + 1:]
                              if row['day_type'] == 'calendar' or day['business_eligible']]
                total = row['determination_days'] + (row['extension_days'] if row['extension'] else 0)
                self.assertEqual(row['expected'], candidates[total - 1]['date'])

    def test_all_weekdays(self):
        for row in self.fixture['cases']:
            with self.subTest(case=row['id']):
                package = self.package(row)
                before = copy.deepcopy(package)
                received = date.fromisoformat(row['received'])
                expected = date.fromisoformat(row['expected'])
                actual = self.law.deadline(package, received, extension=row['extension'])
                self.assertIs(type(actual), date)
                self.assertEqual(actual, expected, 'frozen-date ' + row['id'])
                total = row['determination_days'] + (row['extension_days'] if row['extension'] else 0)
                helper = self.law._add_days(received, total, row['day_type'])
                self.assertIs(type(helper), date)
                self.assertEqual(helper, expected, 'frozen-date helper ' + row['id'])
                self.assertEqual(package, before, 'package must remain unchanged')
                self.assertEqual(received.isoformat(), row['received'])

    def test_extension_total(self):
        by_dimension = {(r['received'], r['determination_days'], r['extension_days'],
                         r['extension'], r['day_type']): r for r in self.fixture['cases']}
        for received, days, extra, mode in itertools.product(self.fixture['receipts'], (1, 2, 5, 10),
                                                            (0, 3), ('calendar', 'business')):
            with self.subTest(received=received, days=days, extra=extra, mode=mode):
                off = by_dimension[received, days, extra, False, mode]
                on = by_dimension[received, days, extra, True, mode]
                package = self.package(off)
                before = copy.deepcopy(package)
                start = date.fromisoformat(received)
                unextended = self.law.deadline(package, start, extension=False)
                extended = self.law.deadline(package, start, extension=True)
                self.assertEqual(unextended, date.fromisoformat(off['expected']))
                self.assertEqual(extended, date.fromisoformat(on['expected']))
                combined = copy.deepcopy(package)
                combined['records_law']['determination_days'] = days + extra
                self.assertEqual(extended, self.law.deadline(combined, start, extension=False))
                ignored = copy.deepcopy(package)
                ignored['records_law']['determination_extension_days'] = 3 if extra == 0 else 0
                self.assertEqual(unextended, self.law.deadline(ignored, start, extension=False))
                if extra == 0:
                    self.assertEqual(unextended, extended)
                self.assertEqual(package, before)

    def test_day_basis(self):
        for row in self.fixture['cases']:
            if row['determination_days'] != 1 or row['extension_days'] != 0 or row['extension']:
                continue
            with self.subTest(start=row['received'], mode=row['day_type']):
                actual = self.law.deadline(self.package(row), date.fromisoformat(row['received']))
                self.assertEqual(actual, date.fromisoformat(row['expected']))
                self.assertGreater(actual, date.fromisoformat(row['received']), 'start not counted')
                if row['day_type'] == 'business':
                    label = next(day for day in self.fixture['calendar'] if day['date'] == row['expected'])
                    self.assertTrue(label['business_eligible'])
        # A weekday explicitly labeled "holiday" is not excluded by this API.
        with self.subTest(synthetic_label='holiday'):
            holiday = next(day for day in self.fixture['calendar'] if day['label'] == 'holiday')
            self.assertEqual(holiday['date'], '2020-03-02')
            self.assertEqual(holiday['weekday'], 'Monday')
            package = copy.deepcopy(self.fixture['package'])
            before = copy.deepcopy(package)
            self.assertEqual(self.law.deadline(package, date(2020, 2, 28)), date(2020, 3, 2))
            self.assertEqual(package, before)

    def test_warning(self):
        package = copy.deepcopy(self.fixture['package'])
        before = copy.deepcopy(package)
        with self.subTest(surface='summary'):
            summary = self.law.summary(package)
            self.assertEqual(summary['note'], self.law.HOLIDAY_NOTE)
            for phrase in ('Provisional reminders only, not legal due dates',
                           'Saturday and Sunday only', 'public holidays are not modeled',
                           'Confirm actual receipt', 'lawful extension notice', 'lateness conclusion'):
                self.assertIn(phrase, summary['note'])
            self.assertEqual(summary['status'], 'draft')
        with self.subTest(surface='draft'):
            draft = self.law.render_request(package, 'synthetic', 'Synthetic Agency', 'Synthetic Custodian',
                                            'Fictional interval', 'Synthetic cap', 'Synthetic Requester')
            self.assertIn('DRAFT ONLY', draft)
            self.assertIn('Law package status: draft', draft)
            self.assertIn('This draft does not determine a statutory deadline.', draft)
        with self.subTest(surface='reminder_caveat'):
            for phrase in ('Provisional determination reminder', 'does not model delivery delay',
                           'Neither extension=True nor a returned date establishes legal timeliness'):
                self.assertIn(phrase, self.law.deadline.__doc__)
        self.assertEqual(package, before)

    def test_deciding_controls(self):
        """Patch only the isolated real helper; catch the exact date assertion."""
        self.control_receipts = []
        package = copy.deepcopy(self.fixture['package'])
        start, expected = date(2020, 2, 28), date(2020, 3, 2)
        original = self.law._add_days

        def counts_start(value, days, basis):
            if basis == 'calendar':
                return value + timedelta(days=days - 1)
            current, eligible = value, []
            while len(eligible) < days:
                if current.weekday() < 5:
                    eligible.append(current)
                current += timedelta(days=1)
            return eligible[-1]

        def saturday_business(value, days, basis):
            if basis == 'calendar':
                return value + timedelta(days=days)
            current, eligible = value, []
            while len(eligible) < days:
                current += timedelta(days=1)
                if current.weekday() < 6:
                    eligible.append(current)
            return eligible[-1]

        for name, mutant, wrong in (('counts_start', counts_start, date(2020, 2, 28)),
                                    ('saturday_business', saturday_business, date(2020, 2, 29))):
            with self.subTest(mutant=name):
                before = copy.deepcopy(package)
                self.assertEqual(self.law.deadline(package, start), expected, 'genuine positive before')
                with mock.patch.object(self.law, '_add_days', mutant):
                    actual = self.law.deadline(package, start)
                    self.assertIs(type(actual), date)
                    self.assertEqual(actual, wrong, 'only the intended gate was changed')
                    with self.assertRaisesRegex(AssertionError, 'frozen-date') as caught:
                        self.assertEqual(actual, expected, 'frozen-date deciding control ' + name)
                self.assertIs(self.law._add_days, original)
                self.assertEqual(self.law.deadline(package, start), expected, 'restored genuine positive')
                self.assertEqual(package, before)
                receipt = {'mutant': name, 'actual': actual.isoformat(), 'expected': expected.isoformat(),
                           'failure': str(caught.exception), 'restored_positive': True}
                self.control_receipts.append(receipt)
                print('HANDLED_CONTROL ' + json.dumps(receipt, sort_keys=True))


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = []
        self.controls = []

    def addSubTest(self, test, subtest, err):
        self.events.append({'id': subtest.id(), 'kind': 'subcase', 'status': 'pass' if err is None else 'fail'})
        super().addSubTest(test, subtest, err)

    def addSuccess(self, test):
        self.events.append({'id': test.id(), 'kind': 'test', 'status': 'pass'})
        super().addSuccess(test)

    def addError(self, test, err):
        self.events.append({'id': test.id(), 'kind': 'test', 'status': 'error'})
        super().addError(test, err)

    def addFailure(self, test, err):
        self.events.append({'id': test.id(), 'kind': 'test', 'status': 'fail'})
        super().addFailure(test, err)

    def addSkip(self, test, reason):
        self.events.append({'id': test.id(), 'kind': 'test', 'status': 'skip', 'reason': reason})
        super().addSkip(test, reason)

    def stopTest(self, test):
        self.controls.extend(getattr(test, 'control_receipts', []))
        super().stopTest(test)


def peer_sentinel(snapshot):
    # A local class is deliberately not a discoverable test-module global.
    class PeerSentinel(unittest.TestCase):
        def runTest(self):
            environment, tempdir, paths, socket_type, popen, modules = snapshot
            self.assertEqual(dict(os.environ), environment)
            self.assertEqual(tempfile.tempdir, tempdir)
            self.assertEqual(sys.path, paths)
            self.assertIs(socket.socket, socket_type)
            self.assertIs(subprocess.Popen, popen)
            for name, module in modules.items():
                self.assertIs(sys.modules.get(name), module, 'existing import replaced: ' + name)
            self.assertFalse(any(name.startswith('fc16_isolated_law') for name in sys.modules))
            self.assertNotIn('campaign_tool.law', sys.modules)
            self.assertNotIn('campaign_tool', sys.modules)
    return PeerSentinel()


def run(args):
    root = Path(args.temporary_root).absolute()
    root.mkdir(parents=True, exist_ok=True)
    old_temp, old_path = tempfile.tempdir, sys.path[:]
    try:
        tempfile.tempdir = str(root)
        # Compatibility's inspected source closure needs the real repo on path.
        # Scope it to this isolated script process and restore after the run.
        if args.mode == 'compatibility':
            with offline():
                sys.path.insert(0, str(REPO))
                suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_law.LawFunctionTests')
                result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
        elif args.mode == 'discovery':
            snapshot = (dict(os.environ), tempfile.tempdir, sys.path[:], socket.socket,
                        subprocess.Popen, dict(sys.modules))
            # Discovery's standard import state is owned by unittest; do not
            # replace existing modules or path/default state inside test code.
            before = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(
                unittest.TestSuite([peer_sentinel(snapshot)]))
            sys.path.insert(0, str(REPO))
            suite = unittest.defaultTestLoader.discover(str(REPO / 'tests/records'),
                                                        pattern=Path(__file__).name, top_level_dir=str(REPO))
            sys.path[:] = old_path
            result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
            after = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(
                unittest.TestSuite([peer_sentinel(snapshot)]))
            if not before.wasSuccessful() or not after.wasSuccessful():
                return 1
        else:
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(BusinessOracleTests)
            result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
        result = cast(RecordingResult, result)
        report = {'mode': args.mode, 'tests': result.testsRun, 'subcases': sum(e['kind'] == 'subcase' for e in result.events),
                  'skips': len(result.skipped), 'errors': len(result.errors), 'failures': len(result.failures),
                  'events': result.events, 'controls': result.controls, 'case_map': CASE_MAP,
                  'success': result.wasSuccessful(), 'peer_sentinels': args.mode == 'discovery'}
        Path(args.report).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        return int(not result.wasSuccessful() or bool(result.skipped))
    finally:
        tempfile.tempdir = old_temp
        sys.path[:] = old_path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--temporary-root', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--mode', choices=('suite', 'compatibility', 'discovery'), default='suite')
    raise SystemExit(run(parser.parse_args()))
