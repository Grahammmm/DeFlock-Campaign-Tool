"""CIS-DF-1: measured-time shard packing with a fail-safe size fallback (synthetic modules only)."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import random
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location('ci_affected_under_test', ROOT / 'tools' / 'ci_affected.py')
ci = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ci)


class PackingTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def files_for(self, sizes):
        files = {}
        for name, size in sizes.items():
            path = self.root / (name + '.py')
            path.write_bytes(b'x' * size)
            files[name] = path
        return files

    def load(self, text):
        path = self.root / 'weights.json'
        if text is not None:
            path.write_bytes(text if isinstance(text, bytes) else text.encode())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = ci.load_weights(path)
        return result, out.getvalue().splitlines()

    def test_random_selections_cover_exactly_once(self):
        rng = random.Random(20261031)
        files = self.files_for({f'tests.test_m{i:02d}': rng.randint(0, 9000) for i in range(60)})
        pool = sorted(files)
        for _ in range(200):
            selection = set(rng.sample(pool, rng.randint(1, 60)))
            count = rng.randint(1, 7)
            weighted = rng.sample(sorted(selection), rng.randint(0, len(selection)))
            weights = rng.choice([None, {name: rng.randint(1, 300) for name in weighted}])
            weights = None if weights is None else dict(weights, **{'tests.test_gone': 50})
            bins = ci.partition(selection, files, count, weights)
            self.assertEqual(len(bins), count)
            flat = [name for bucket in bins for name in bucket]
            self.assertEqual(len(flat), len(set(flat)))
            self.assertEqual(set(flat), selection)
            self.assertNotIn('tests.test_gone', flat)
            if len(selection) >= count:
                self.assertTrue(all(bins), (count, len(selection)))
            for name in selection - set(weights or ()):
                self.assertEqual(sum(name in bucket for bucket in bins), 1)

    def test_bad_weights_fall_back_with_one_warning(self):
        cases = {'missing': None, 'invalid json': '{"version": 1,', 'not utf-8': b'\xff\xfe{',
                 'version 2': '{"version": 2, "seconds": {"tests.test_a": 3}}',
                 'no version': '{"seconds": {"tests.test_a": 3}}', 'list': '[1]',
                 'seconds list': '{"version": 1, "seconds": [3]}',
                 'string': '{"version": 1, "seconds": {"tests.test_a": "3"}}',
                 'negative': '{"version": 1, "seconds": {"tests.test_a": -1}}',
                 'zero': '{"version": 1, "seconds": {"tests.test_a": 0}}',
                 'float': '{"version": 1, "seconds": {"tests.test_a": 2.5}}',
                 'bool': '{"version": 1, "seconds": {"tests.test_a": true}}'}
        for label, text in cases.items():
            with self.subTest(label):
                result, lines = self.load(text)
                self.assertIsNone(result)
                self.assertEqual(len(lines), 1, lines)
                self.assertTrue(lines[0].startswith('::warning::shard weights unavailable ('), lines)
                self.assertTrue(lines[0].endswith('); packing by size'), lines)
        result, lines = self.load('{"version": 1, "source": "x", "seconds": {"tests.test_a": 3}}')
        self.assertEqual((result, lines), ({'tests.test_a': 3}, []))

    def test_none_weights_equal_size_packing(self):
        files = self.files_for({'a': 50, 'b': 40, 'c': 30, 'd': 20, 'e': 10, 'f': 10})
        self.assertEqual(ci.partition(set(files), files, 3), [['a', 'f'], ['b', 'e'], ['c', 'd']])
        self.assertEqual(ci.partition(set(files), files, 3, None), [['a', 'f'], ['b', 'e'], ['c', 'd']])

    def test_weights_override_size_and_estimate_unknown(self):
        files = self.files_for({'big': 9000, 'slow': 10, 'new': 3 * ci.BYTES_PER_SECOND})
        bins = ci.partition(set(files), files, 2, {'big': 1, 'slow': 100})
        self.assertEqual(bins, [['slow'], ['big', 'new']])

    def test_balance(self):
        weights = dict(zip(map('m{:02d}'.format, range(47)), [289, 176, 84, 53, 38, 38, 33] + [10] * 40))
        bins = ci.partition(set(weights), {}, 5, weights)
        loads = [sum(weights[name] for name in bucket) for bucket in bins]
        self.assertLessEqual(max(loads), 1.1 * max(289, sum(weights.values()) / 5))

    def test_deterministic_across_calls_and_input_order(self):
        rng = random.Random(7)
        files = self.files_for({f'tests.test_d{i:02d}': rng.choice([100, 200, 300]) for i in range(40)})
        weights = {name: rng.choice([5, 10]) for name in sorted(files)[::2]}
        names = sorted(files)
        for value in (None, weights):
            first = ci.partition(set(names), files, 4, value)
            self.assertEqual(first, ci.partition(names, files, 4, value))
            self.assertEqual(first, ci.partition(list(reversed(names)), files, 4, value))

    def test_committed_weights_file(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            weights = ci.load_weights()
        self.assertEqual(out.getvalue(), '')
        data = json.loads(ci.WEIGHTS.read_text(encoding='utf-8'))
        self.assertEqual(data['version'], 1)
        self.assertEqual(list(data['seconds']), sorted(data['seconds']))
        self.assertTrue(all(type(v) is int and v >= 1 for v in weights.values()))
        raw = subprocess.check_output(['git', 'ls-files', '-z', 'tests/*.py'], cwd=ROOT).decode()
        tracked = {p[:-3].replace('/', '.') for p in raw.split('\0') if Path(p).name.startswith('test_')}
        stale = sorted(set(weights) - tracked)
        if stale:  # warn-only: partition ignores keys outside the selection
            print(f'::warning::stale shard weight keys: {stale}')

    def test_self_test_still_ok(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ci.self_test(), 0)
        self.assertIn('sharding OK', out.getvalue())


if __name__ == '__main__':
    unittest.main()
