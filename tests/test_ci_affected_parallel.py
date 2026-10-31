"""ci_affected --jobs: real child processes on synthetic test modules only, never the repository suite."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock
import uuid

from tools import ci_affected

HEAD = 'import gc, os, signal, sys, unittest\n\n\nclass Case(unittest.TestCase):\n'
GREEN = HEAD + '    def test_a(self):\n        pass\n\n    def test_b(self):\n        pass\n'
BODIES = {
    'fail': HEAD + '    def test_bad(self):\n        self.fail("synthetic failure")\n',
    'skip': HEAD + '    def test_skip(self):\n        self.skipTest("n/a")\n',
    'kill': HEAD + '    def test_kill(self):\n        os.kill(os.getpid(), signal.SIGKILL)\n',
    # A leaked file's warning, emitted in the test (not in a finalizer): an error only if -W error::ResourceWarning
    # reached the child; under default filters it is ignored and the test passes.
    'leak': HEAD + '    def test_leak(self):\n        handle = open(__file__)\n        self.addCleanup(handle.close)\n'
            '        import warnings\n        warnings.warn(f"unclosed file {handle!r}", ResourceWarning)\n',
    # Exits 0 silently, but only in a child given this synthetic package (never in the loading parent).
    'exit0': 'import os, sys\nif any(a.startswith(__name__.split(".")[0] + ".") for a in sys.argv):\n    os._exit(0)\n'
             + GREEN,
    'broken': 'import cipar_no_such_module_zz\n' + GREEN,
    'tmpdir': HEAD + '    def test_tmp(self):\n        tmp = os.environ["TMPDIR"]\n        self.assertTrue('
              'os.path.basename(tmp).startswith("ci-chunk-") and os.path.isdir(tmp), tmp)\n'
              '        self.assertEqual(os.path.dirname(tmp), os.environ["CIPAR_PARENT"])\n',
}


class ParallelShardTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.package = 'cipar_' + uuid.uuid4().hex[:10]
        (self.root / self.package).mkdir()
        (self.root / self.package / '__init__.py').write_text('')
        self.enterContext(mock.patch.object(sys, 'path', [str(self.root)] + sys.path))
        self.enterContext(mock.patch.dict(os.environ, {'PYTHONPATH': str(self.root)}))
        self.addCleanup(lambda: [sys.modules.pop(n) for n in list(sys.modules) if n.startswith(self.package)])

    def modules(self, *kinds):
        files = {}
        for i, kind in enumerate(kinds):
            path = self.root / self.package / f'test_{i}_{kind}.py'
            path.write_text(BODIES.get(kind, GREEN))
            files[f'{self.package}.test_{i}_{kind}'] = path
        return sorted(files), files

    def shard(self, kinds, jobs):
        names, files = self.modules(*kinds)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = ci_affected.run_shard(names, files, jobs, None, 2)
        text = out.getvalue()
        summary = json.loads(text.splitlines()[-1]) if text.strip() else {}
        return code, text, summary

    def chunk_of(self, text, needle):
        groups = re.findall(r'::group::chunk (\d+) \(\d+ modules\)\n(.*?)\n::endgroup::', text, re.S)
        return [int(k) for k, body in groups if needle in body]

    def test_all_green_three_chunks(self):
        code, text, summary = self.shard(['green'] * 3, 3)
        self.assertEqual((code, summary), (0, {'shard': 2, 'jobs': 3, 'cpu_count': os.cpu_count(),
                                               'expected': 6, 'ran': 6, 'chunks': 3}))
        self.assertEqual(len(re.findall(r'CI_CHUNK_RESULT .*"ok": true', text)), 3)
        self.assertNotIn('::error::', text)

    def test_child_exiting_zero_without_running_is_red(self):
        code, text, summary = self.shard(['green', 'exit0'], 2)
        self.assertEqual((code, summary['expected'], summary['ran']), (1, 4, 2))
        self.assertRegex(text, r'::error::chunk \d: exit 0, last line is not a valid CI_CHUNK_RESULT')
        self.assertIn('::error::ran 2 tests, expected 4', text)

    def test_one_failing_chunk_is_red_and_named(self):
        code, text, _ = self.shard(['green', 'fail', 'green'], 3)
        [k] = self.chunk_of(text, 'synthetic failure')
        self.assertEqual(code, 1)
        self.assertEqual(re.findall(r'::error::chunk (\d+)', text), [str(k)])
        self.assertIn(f'::error::chunk {k}: exit 1, ok false', text)

    def test_import_error_kill_and_leak_are_red(self):
        for kind, rule in (('broken', 'exit 1, ok false'), ('kill', 'killed by signal 9'),
                           ('leak', 'exit 1, ok false')):
            with self.subTest(kind):
                code, text, _ = self.shard(['green', kind], 2)
                self.assertEqual(code, 1)
                self.assertRegex(text, r'::error::chunk \d: ' + re.escape(rule))
                if kind == 'leak':  # -W error::ResourceWarning reached the child
                    self.assertIn('ResourceWarning: unclosed file', text)

    def test_skips_count_and_stay_green(self):
        code, text, summary = self.shard(['green', 'skip'], 2)
        self.assertEqual((code, summary['expected'], summary['ran']), (0, 3, 3))
        self.assertRegex(text, r'CI_CHUNK_RESULT \{"errors": 0, "failures": 0, "ok": true, "ran": 1, "skipped": 1\}')

    def test_jobs_one_and_single_module_stay_in_process(self):
        for kinds, jobs in ((['green'] * 3, 1), (['green'], 8)):
            with self.subTest(jobs=jobs), mock.patch.object(ci_affected.subprocess, 'Popen') as popen:
                code, text, summary = self.shard(kinds, jobs)
                popen.assert_not_called()
                self.assertEqual((code, sorted(summary)), (0, ['shard', 'slowest', 'total_seconds']))

    def test_jobs_eight_three_modules_and_jobs_zero_cpu_count(self):
        seen = []
        real = ci_affected.partition
        with mock.patch.object(ci_affected, 'partition', lambda *a: seen.append(real(*a)) or seen[-1]):
            code, _, summary = self.shard(['green'] * 3, 8)
        self.assertEqual((code, summary['chunks'], [len(c) for c in seen[0]]), (0, 3, [1, 1, 1]))
        with mock.patch.object(ci_affected.os, 'cpu_count', return_value=2):
            code, _, summary = self.shard(['green'] * 3, 0)
        self.assertEqual((code, summary['jobs'], summary['chunks'], summary['ran']), (0, 2, 2, 6))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ci_affected.main(['--jobs', '-1'])

    def test_chunk_tmpdir_is_fresh_and_a_shared_tmp_moves_home(self):
        own = self.root / 'own-tmp'
        own.mkdir(mode=0o700)
        for inherited, parent in ((str(own), str(own)), ('/var/tmp', str(Path.home())), ('/tmp', str(Path.home()))):
            with self.subTest(inherited), mock.patch.dict(os.environ, {'TMPDIR': inherited, 'CIPAR_PARENT': parent}):
                code, text, _ = self.shard(['tmpdir', 'tmpdir'], 2)
                self.assertEqual(code, 0, text)
        self.assertEqual(list(own.iterdir()), [])  # chunk directories are removed

    def test_serial_modules_run_in_parent_after_children(self):
        for last, expected_code in (('green', 0), ('fail', 1)):
            serial = frozenset({f'{self.package}.test_2_{last}'})
            with self.subTest(last), mock.patch.object(ci_affected, 'SERIAL', serial):
                code, text, summary = self.shard(['green', 'green', last], 4)
                self.assertEqual((code, summary['chunks'], summary['ran']), (expected_code, 2, summary['expected']))
                self.assertEqual('SERIAL modules failed' in text, bool(expected_code))


class AggregatorTest(unittest.TestCase):
    def verdict(self, returncode, last, before='noise\n'):
        return ci_affected.chunk_verdict(returncode, before + last + '\n')

    def test_rules(self):
        ok = 'CI_CHUNK_RESULT {"errors": 0, "failures": 0, "ok": true, "ran": 3, "skipped": 0}'
        self.assertEqual(self.verdict(0, ok), (3, None))
        self.assertEqual(self.verdict(1, ok), (3, 'exit 1, ok true'))
        self.assertEqual(self.verdict(0, ok.replace('true', 'false')), (3, 'exit 0, ok false'))
        self.assertEqual(self.verdict(-9, ok), (0, 'killed by signal 9'))
        for bad in (ok + '\ntrailing', ok.replace('3', '"3"'), ok.replace('3', 'true'), ok.replace('3', '-1'),
                    ok.replace('"ok": true, ', ''), 'CI_CHUNK_RESULT {', 'CI_CHUNK_RESULT [1]', ''):
            with self.subTest(bad=bad):
                self.assertEqual(self.verdict(0, bad, before='')[1],
                                 'exit 0, last line is not a valid CI_CHUNK_RESULT')

    def test_fewer_ran_than_expected_is_red(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ci_affected.aggregate([(2, None), (1, None)], 4), (1, 3))
            self.assertEqual(ci_affected.aggregate([(2, None), (2, None)], 4), (0, 4))
            self.assertEqual(ci_affected.aggregate([(3, None), (2, None)], 4), (1, 5))
            self.assertEqual(ci_affected.aggregate([], 1), (1, 0))
        self.assertEqual(out.getvalue().splitlines(), ['::error::ran 3 tests, expected 4',
                                                       '::error::ran 5 tests, expected 4',
                                                       '::error::ran 0 tests, expected 1'])


if __name__ == '__main__':
    unittest.main()
