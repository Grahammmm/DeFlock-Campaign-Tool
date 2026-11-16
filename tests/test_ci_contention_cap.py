"""Bounded executor candidate; real synthetic I/O, not hosted performance evidence."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tools import ci_affected

PACKAGE = 'ci_contention_fixture'
PAYLOAD = bytes(range(256)) * 16384  # 4 MiB per test, identical across settings.
PAYLOAD_HASH = hashlib.sha256(PAYLOAD).hexdigest()
FIXTURE = '''import hashlib, json, os, signal, tempfile, unittest, warnings
from pathlib import Path

class Case(unittest.TestCase):
    def exercise(self):
        identity = self.id()
        root = Path(os.environ['CICAP_EVENTS'])
        event = {'id': identity, 'pid': os.getpid(), 'tmpdir': os.environ['TMPDIR']}
        (root / (identity + '.json')).write_text(json.dumps(event))
        payload = bytes(range(256)) * 16384
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'payload'
            path.write_bytes(payload)
            with path.open('rb') as handle:
                os.fsync(handle.fileno())
                actual = handle.read()
            self.assertEqual(len(actual), 4194304)
            self.assertEqual(hashlib.sha256(actual).hexdigest(), %r)
        if KIND == 'fail':
            self.fail('synthetic assertion')
        if KIND == 'skip':
            self.skipTest('synthetic skip')
        if KIND == 'warning':
            warnings.warn('synthetic resource warning', ResourceWarning)
        if KIND == 'kill':
            os.kill(os.getpid(), signal.SIGKILL)

    def test_a(self):
        self.exercise()

    def test_b(self):
        self.exercise()
''' % PAYLOAD_HASH
# A pipe launch barrier makes live-process observation deterministic; no sleeps or
# synthetic delay masquerading as I/O. Release all children once fan-out is complete.
BOOTSTRAP = '''import os, runpy, sys
fd, root, script = int(sys.argv[1]), sys.argv[2], sys.argv[3]
os.read(fd, 1)
os.close(fd)
sys.path.insert(0, root)
sys.argv = sys.argv[3:]
runpy.run_path(script, run_name='__main__')
'''


class Comparison(unittest.TestCase):
    def compare(self, jobs, cpu=4, count=4, kind='green', serial=False):
        with tempfile.TemporaryDirectory(prefix='ci-cap-') as directory:
            root = Path(directory)
            package = root / PACKAGE
            package.mkdir()
            (package / '__init__.py').write_text('')
            events = root / 'events'
            events.mkdir()
            bootstrap = root / 'launch.py'
            bootstrap.write_text(BOOTSTRAP)
            files = {}
            for i in range(count):
                body = 'KIND = %r\n' % (kind if i == 0 else 'green') + FIXTURE
                if kind == 'import' and i == 0:
                    body = 'import ci_cap_missing_module\n' + body
                path = package / f'test_{i}.py'
                path.write_text(body)
                files[f'{PACKAGE}.test_{i}'] = path
            names = sorted(files)
            hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
            children, results, peak = [], [], 0
            chunks = min(jobs or cpu, count - int(serial))
            read_fd, write_fd = os.pipe()
            real_popen = subprocess.Popen
            real_run = unittest.TextTestRunner.run

            def spawn(argv, **kwargs):
                nonlocal peak, write_fd
                # Keep interpreter flags and the actual, unchanged selector script.
                command = argv[:4] + [str(bootstrap), str(read_fd), str(root)] + argv[4:]
                child = real_popen(command, pass_fds=(read_fd,), **kwargs)
                children.append(child)
                peak = max(peak, sum(c.poll() is None for c in children))
                if len(children) == chunks:
                    os.close(write_fd)
                    write_fd = None
                return child

            def record_run(runner, suite):
                if serial:
                    self.assertTrue(all(c.poll() is not None for c in children), 'serial starts after children exit')
                result = real_run(runner, suite)
                results.append({'ran': result.testsRun, 'failures': len(result.failures),
                                'errors': len(result.errors), 'skipped': len(result.skipped)})
                return result

            out, err = io.StringIO(), io.StringIO()
            started = time.monotonic()
            try:
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(sys, 'path', [str(root)] + sys.path))
                    stack.enter_context(mock.patch.dict(os.environ, {'TMPDIR': str(root), 'CICAP_EVENTS': str(events)}))
                    stack.enter_context(mock.patch.object(ci_affected.os, 'cpu_count', return_value=cpu))
                    stack.enter_context(mock.patch.object(ci_affected.subprocess, 'Popen', side_effect=spawn))
                    stack.enter_context(mock.patch.object(unittest.TextTestRunner, 'run', record_run))
                    stack.enter_context(mock.patch.object(ci_affected, 'SERIAL', frozenset(names[-1:]) if serial else frozenset()))
                    stack.enter_context(contextlib.redirect_stdout(out))
                    stack.enter_context(contextlib.redirect_stderr(err))
                    code = ci_affected.run_shard(names, files, jobs, None, 0)
                elapsed = time.monotonic() - started
                text = out.getvalue() + err.getvalue()
                records = [json.loads(line[len(ci_affected.RESULT):]) for line in out.getvalue().splitlines()
                           if line.startswith(ci_affected.RESULT)]
                summary = json.loads(out.getvalue().splitlines()[-1])
                completed = records + results
                counts = {key: sum(r[key] for r in completed) for key in ('ran', 'failures', 'errors', 'skipped')}
                entries = [json.loads(p.read_text()) for p in sorted(events.glob('*.json'))]
                ids = sorted(entry['id'] for entry in entries)
                self.assertEqual(len(ids), len(set(ids)), 'no duplicate test execution')
                if children:
                    self.assertTrue(all(c.poll() is not None for c in children))
                    self.assertEqual(list(root.glob('ci-chunk-*')), [], 'child TMPDIR cleanup')
                    self.assertEqual(list(root.glob('ci-record-*')), [], 'record cleanup')
                    tmpdirs = {e['tmpdir'] for e in entries if e['pid'] != os.getpid()}
                    self.assertTrue(all(Path(p).parent == root for p in tmpdirs))
                    self.assertTrue(all(not Path(p).exists() for p in tmpdirs))
                    pids = {e['pid'] for e in entries if e['pid'] != os.getpid()}
                    self.assertEqual(len(tmpdirs), len(pids), 'one fresh TMPDIR per child')
                if serial:
                    self.assertEqual({e['pid'] for e in entries if '.test_3.' in e['id']}, {os.getpid()})
                return {'jobs': jobs, 'cpu': cpu, 'modules': names, 'fixture_hashes': hashes,
                        'payload_sha256': PAYLOAD_HASH, 'elapsed_seconds': round(elapsed, 6),
                        'peak_children': peak, 'exit': code, 'expected': summary.get('expected', 2 * count),
                        'ran': summary.get('ran', counts['ran']), 'counts': counts, 'test_ids': ids}, text
            finally:
                if write_fd is not None:
                    os.close(write_fd)
                os.close(read_fd)
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.wait()
                for name in list(sys.modules):
                    if name == PACKAGE or name.startswith(PACKAGE + '.'):
                        sys.modules.pop(name)

    def assert_same_coverage(self, baseline, candidate):
        for key in ('modules', 'fixture_hashes', 'payload_sha256', 'exit', 'expected', 'ran', 'counts', 'test_ids'):
            self.assertEqual(baseline[key], candidate[key], key)

    def test_repeated_io_comparison(self):
        runs = []
        for repeat in range(3):
            pair = {}
            for jobs in ((0, 2) if repeat % 2 == 0 else (2, 0)):
                row, _ = self.compare(jobs)
                pair[jobs] = row
                runs.append(row)
                self.assertEqual((row['exit'], row['expected'], row['ran']), (0, 8, 8))
                self.assertEqual(row['counts'], {'ran': 8, 'failures': 0, 'errors': 0, 'skipped': 0})
                self.assertEqual(row['peak_children'], 4 if jobs == 0 else 2)
            self.assert_same_coverage(pair[0], pair[2])
        receipt = {'interpreter': sys.version, 'platform': platform.platform(),
                   'selector_sha256': hashlib.sha256(Path(ci_affected.__file__).read_bytes()).hexdigest(),
                   'test_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   'payload_bytes_per_test': len(PAYLOAD), 'tests_per_run': 8, 'runs': runs,
                   'median_seconds': {str(j): statistics.median(r['elapsed_seconds'] for r in runs if r['jobs'] == j)
                                      for j in (0, 2)}}
        print('CI_CONTENTION_COMPARISON ' + json.dumps(receipt, sort_keys=True), flush=True)

    def test_cpu_and_small_selection_seams(self):
        for cpu in (1, 2, 4, 16):
            for count in (1, 2, 4):
                with self.subTest(cpu=cpu, count=count):
                    a, _ = self.compare(0, cpu, count)
                    b, _ = self.compare(2, cpu, count)
                    self.assert_same_coverage(a, b)
                    self.assertLessEqual(b['peak_children'], 2)
                    self.assertEqual((b['exit'], b['ran']), (0, count * 2))
        with self.assertRaisesRegex(RuntimeError, 'empty test suite'):
            ci_affected.run_shard([], {}, 2, None, 0)

    def test_failure_skip_warning_import_and_killed_child(self):
        for kind in ('fail', 'skip', 'warning', 'import', 'kill'):
            with self.subTest(kind=kind):
                a, text_a = self.compare(0, kind=kind)
                b, text_b = self.compare(2, kind=kind)
                self.assertLessEqual(b['peak_children'], 2)
                if kind != 'kill':
                    print('CI_CONTENTION_CONTROL ' + json.dumps({'kind': kind, 'baseline': a, 'candidate': b}, sort_keys=True))
                    self.assert_same_coverage(a, b)
                    self.assertEqual(b['exit'], 0 if kind == 'skip' else 1)
                    self.assertEqual(b['counts']['skipped'], 2 if kind == 'skip' else 0)
                    self.assertEqual(b['counts']['failures'], 2 if kind == 'fail' else 0)
                    self.assertEqual(b['counts']['errors'], {'warning': 2, 'import': 1}.get(kind, 0))
                else:
                    for row, text in ((a, text_a), (b, text_b)):
                        self.assertEqual(row['exit'], 1)
                        self.assertLess(row['ran'], row['expected'])
                        self.assertIn('killed by signal 9', text)
                        self.assertIn(f"ran {row['ran']} tests, expected {row['expected']}", text)
                    print('CI_CONTENTION_KILLED ' + json.dumps({'baseline': a, 'candidate': b}, sort_keys=True))

    def test_serial_parent_after_children(self):
        for jobs in (0, 2):
            with self.subTest(jobs=jobs):
                row, _ = self.compare(jobs, serial=True)
                self.assertEqual((row['exit'], row['ran']), (0, 8))


class WorkflowContract(unittest.TestCase):
    def test_bounded_matrices_coverage_and_gates(self):
        text = (ci_affected.ROOT / '.github/workflows/records.yml').read_text()
        blocks = dict(re.findall(r'^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)', text, re.M | re.S))
        fast_event = "github.event_name == 'pull_request' || github.event_name == 'merge_group'"
        full_event = "github.event_name != 'pull_request' && github.event_name != 'merge_group'"
        for name, event, tier, shards, timeout in (('affected', fast_event, 'pr', 5, 20),
                                                 ('full', full_event, 'full', 2, 35)):
            block = blocks[name]
            self.assertIn('    if: ' + event, block)
            self.assertIn(f'    timeout-minutes: {timeout}\n', block)
            self.assertIn(f'    strategy:\n      fail-fast: false\n      max-parallel: {shards if name == "affected" else 6}\n      matrix:', block)
            self.assertIn('        shard: ' + str(list(range(shards))), block)
            self.assertIn(f'tools/ci_affected.py --tier {tier} --shard ${{{{ matrix.shard }}}} --shards {shards} --jobs 2\n', block)
        self.assertIn("python: ['3.11', '3.12', '3.13']", blocks['full'])
        offline = blocks['offline']
        for required in ('needs: [affected, full]', 'if: always()', '${{ needs.affected.result }}',
                         '${{ needs.full.result }}', 'tools/check_public_tree.py',
                         'tools/check_pin_guard.py --all', 'tools/ci_affected.py --self-test'):
            self.assertIn(required, offline)
        self.assertEqual(offline.count('run: test "$SHARDS" = success'), 2)
        # Execute the exact unchanged gate: cancelled/expired/stale/skipped/failed cannot pass.
        gate = re.findall(r'run: (test "\$SHARDS" = success)', offline)[0]
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'gate.sh'
            script.write_text(gate + '\n')
            for verdict in ('success', 'failure', 'cancelled', 'skipped', 'expired', 'stale', ''):
                self.assertEqual(subprocess.run(['sh', str(script)], env={**os.environ, 'SHARDS': verdict}).returncode == 0,
                                 verdict == 'success')
        self.assertIn('scan-public --patterns-only', blocks['pin-guard'])
        self.assertIn('tools/check_pin_guard.py --base HEAD^1', blocks['pin-guard'])
        self.assertIn('permissions:\n  contents: read\n', text)
        self.assertIn('group: ${{ github.workflow }}-${{ github.event_name }}-${{ github.event.pull_request.number || github.run_id }}', text)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", text)
        self.assertNotIn('--jobs 0', text)


if __name__ == '__main__':
    unittest.main()
