import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from tools import ci_affected


def synthetic(module, body):
    return type('Case', (unittest.TestCase,), {'__module__': module, 'test_it': body})('test_it')


def run(tests, fake=None):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), mock.patch.object(ci_affected, 'time', fake or ci_affected.time):
        code = ci_affected.run_timed(unittest.TestSuite(tests), 3, stream=io.StringIO())
    return code, out.getvalue().splitlines()


class TimingSummaryTest(unittest.TestCase):
    def test_ten_slowest_descending_with_name_tiebreak(self):
        clock = [0.0]
        def advance(seconds):
            return lambda self: clock.__setitem__(0, clock[0] + seconds)
        durations = [5, 1, 12, 3, 7, 7, 2, 9, 11, 4, 6, 8]
        tests = [synthetic(f'synthetic.m{i:02d}', advance(d)) for i, d in enumerate(durations)]
        code, lines = run(tests, types.SimpleNamespace(perf_counter=lambda: clock[0]))
        self.assertEqual((code, len(lines)), (0, 1))
        self.assertEqual(json.loads(lines[0]), {'shard': 3, 'total_seconds': 75.0, 'slowest': [
            [f'synthetic.m{i:02d}', float(s)] for i, s in
            ((2, 12), (8, 11), (7, 9), (11, 8), (4, 7), (5, 7), (10, 6), (0, 5), (9, 4), (3, 3))]})
        self.assertEqual(ci_affected.slowest({'b': 1.26, 'a': 1.26, 'c': 0.04}), [['a', 1.3], ['b', 1.3], ['c', 0.0]])

    def test_failure_and_import_failure_still_report_and_return_one(self):
        code, lines = run([synthetic('synthetic.bad', lambda self: self.fail('x'))])
        self.assertEqual((code, len(lines), json.loads(lines[0])['slowest'][0][0]), (1, 1, 'synthetic.bad'))
        name, out = 'tests.no_such_module_xyz', io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(sys, 'path', list(sys.path)), \
                mock.patch.object(ci_affected, 'inventory', return_value=({name: Path('x.py')}, {name})), \
                mock.patch.object(ci_affected, 'partition', return_value=[[name]]):
            code = ci_affected.main(['--tier', 'full'])
        lines = out.getvalue().splitlines()
        self.assertEqual((code, len(lines), json.loads(lines[0])['selected_modules']), (1, 2, [name]))
        self.assertEqual([m for m, _ in json.loads(lines[1])['slowest']], ['unittest.loader'])

    def test_skip_is_timed_and_passes(self):
        code, lines = run([synthetic('synthetic.skip', lambda self: self.skipTest('n/a'))])
        [[module, seconds]] = json.loads(lines[0])['slowest']
        self.assertEqual((code, module), (0, 'synthetic.skip'))
        self.assertLess(seconds, 1.0)

    def test_list_prints_only_the_selection_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ci_affected.main(['--list', '--tier', 'full']), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(sorted(json.loads(lines[0])), ['selected_modules', 'shard', 'tier'])

    def test_partition_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            files = {name: Path(directory) / name for name in 'abcde'}
            for path, size in zip(files.values(), (50, 40, 30, 20, 10)):
                path.write_text('x' * size)
            self.assertEqual(ci_affected.partition(set(files), files, 2), [['a', 'd', 'e'], ['b', 'c']])
