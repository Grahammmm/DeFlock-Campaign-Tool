"""Five separate offline acceptance rows; every invocation retains fresh receipts."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest.mock import patch
import uuid

REPO = Path(__file__).resolve().parents[1]
AUTHORED = ('tests/records/test_fc10_partial_hit_suppression.py',
            'tests/fixtures/fc10-partial-hit-suppression/cases.json',
            'tools/fc10_partial_hit_suppression_acceptance.py')
UNCHANGED = ('campaign_tool/records/detectors/pipeline.py',
             'tests/records/test_structured_detector_pipeline.py',
             'tools/check_secret_report.py', 'tools/check_public_tree.py',
             'campaign_tool/records/public_scan.py')


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def hash_bytes(value):
    return hashlib.sha256(value).hexdigest()


def file_hash(path):
    return hash_bytes(path.read_bytes())


def safe_path(path):
    require(path.is_absolute() and path == Path(os.path.abspath(path)), 'noncanonical path')
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink ancestry refused')
    return path


def no_network(*args, **kwargs):
    raise RuntimeError('offline acceptance does not permit network operations')


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item.id()


def child(mode, temp_root):
    safe_path(temp_root)
    require(temp_root.is_dir(), 'child temporary root missing')
    tempfile.tempdir = str(temp_root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    connect = sqlite3.connect

    def memory_only(database, *args, **kwargs):
        require(database == ':memory:', 'child SQLite must be in memory')
        return connect(database, *args, **kwargs)

    # Do not import the authored test before the discovery sentinel: doing so
    # would hide first-import contamination behind Python's module cache.
    with ExitStack() as guards:
        for owner, name in ((socket, 'socket'), (socket, 'create_connection'),
                            (socket, 'getaddrinfo'), (subprocess, 'Popen'), (os, 'system')):
            guards.enter_context(patch.object(owner, name, no_network))
        guards.enter_context(patch.object(sqlite3, 'connect', memory_only))
        if mode == 'compatibility':
            suite = unittest.defaultTestLoader.loadTestsFromName('tests.records.test_structured_detector_pipeline')
        else:
            def state():
                return (dict(os.environ), tempfile.tempdir, socket.socket, socket.create_connection,
                        socket.getaddrinfo, subprocess.Popen, os.system, sqlite3.connect,
                        sys.dont_write_bytecode, tuple(sys.path))

            before = state()
            sentinel_calls = []

            def sentinel():
                require(state() == before, 'discovery contaminated ambient state')
                sentinel_calls.append('sentinel')

            sentinel()
            discovered = unittest.defaultTestLoader.discover(str(REPO / 'tests' / 'records'),
                pattern='test_fc10_partial_hit_suppression.py', top_level_dir=str(REPO))
            sentinel()
            suite = unittest.TestSuite([unittest.FunctionTestCase(sentinel), discovered,
                                        unittest.FunctionTestCase(sentinel)])
        ids = list(flatten(suite))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if mode == 'discovery':
            require(state() == before and len(sentinel_calls) == 4, 'post-suite sentinel contamination')
    print(json.dumps({'testsRun': result.testsRun, 'failures': len(result.failures),
                      'errors': len(result.errors), 'skips': len(result.skipped), 'test_ids': ids,
                      'mode': mode}, sort_keys=True))
    return 0 if result.wasSuccessful() and not result.skipped else 1


class Row:
    def __init__(self, args):
        self.args = args
        self.manifest_path = safe_path(args.source_manifest)
        self.manifest = json.loads(self.manifest_path.read_text())
        m = self.manifest
        require(m['schema'] == 'agentorange.source-manifest/v1', 'source manifest schema')
        require(safe_path(Path(m['repo'])) == REPO and REPO.name == 'repo', 'staged repository identity')
        require(safe_path(Path(m['write_root'])) == REPO.parent, 'write root identity')
        require(self.manifest_path.parent == REPO.parent, 'source manifest outside staged root')
        require(re.fullmatch('[a-f0-9]{40}', m['head']) and re.fullmatch('[a-f0-9]{40}', m['tree']), 'source identity grammar')
        require(safe_path(args.out_base) == REPO.parent / 'out', 'output root identity')
        args.out_base.mkdir(mode=0o700, exist_ok=True)
        self.root = args.out_base / str(uuid.uuid4())
        self.root.mkdir(mode=0o700)
        self.temp_root = self.root / 'temp'
        self.temp_root.mkdir(mode=0o700)
        tempfile.tempdir = str(self.temp_root)
        sys.dont_write_bytecode = True
        self.commands = []
        self.observations = {}
        self.identity = None
        self.start_status = None

    def run(self, label, argv, cwd=REPO, expected=(0,)):
        require(type(argv) is list and all(type(arg) is str for arg in argv), 'literal argv strings required')
        require(not any(entry['label'] == label for entry in self.commands), 'duplicate command label')
        result = subprocess.run(argv, cwd=cwd, shell=False, capture_output=True, timeout=180)
        stdout = self.root / (label + '.stdout')
        stderr = self.root / (label + '.stderr')
        stdout.write_bytes(result.stdout)
        stderr.write_bytes(result.stderr)
        self.commands.append({'label': label, 'argv': argv, 'cwd': str(cwd),
            'interpreter': argv[0], 'exit': result.returncode,
            'stdout': str(stdout), 'stdout_sha256': file_hash(stdout),
            'stderr': str(stderr), 'stderr_sha256': file_hash(stderr)})
        require(result.returncode in expected, label + ': unexpected exit ' + str(result.returncode))
        return result.stdout

    def git(self, label, *args, cwd=REPO):
        return self.run(label, ['git', *args], cwd).decode().strip()

    def prepare(self):
        m = self.manifest
        self.identity = {'head': self.git('head', 'rev-parse', 'HEAD'),
                         'tree': self.git('tree', 'rev-parse', 'HEAD^{tree}')}
        require(self.git('base-tree', 'rev-parse', m['head'] + '^{tree}') == m['tree'], 'base tree drift')
        self.git('base-ancestor', 'merge-base', '--is-ancestor', m['head'], self.identity['head'])
        require(self.git('remote', 'remote', 'get-url', 'origin') == m['remote'], 'remote drift')
        venv = REPO / '.venv'
        require(venv.is_symlink() and str(venv.resolve()) == m['runtime']['repo_venv'], 'standard runtime link drift')
        expected = m['runtime']['credential_scan_python3'] if self.args.phase == 'credentials' else str(venv / 'bin' / 'python3')
        require(os.path.abspath(sys.executable) == expected, 'wrong row interpreter')
        self.start_status = self.git('status-before', 'status', '--porcelain=v1', '--untracked-files=all')
        require(not self.git('index-diff', 'diff', '--cached', '--name-only'), 'staged bytes not final HEAD')
        changes = set(self.git('base-diff', 'diff', '--name-only', m['head'], 'HEAD').splitlines())
        require(changes <= set(AUTHORED), 'candidate diff outside exclusive write set')
        dirty = self.git('working-diff', 'diff', '--name-only')
        require(not dirty, 'tracked bytes differ from HEAD')
        if self.args.phase != 'suite' or self.identity['head'] != m['head']:
            require(not self.start_status, 'final acceptance requires clean committed source')
            require(changes == set(AUTHORED), 'all three authored files must be committed')
        else:
            paths = {line[3:] for line in self.start_status.splitlines()}
            require(paths <= set(AUTHORED), 'baseline untracked paths outside scope')
        self.observations['authored_sha256'] = {p: file_hash(REPO / p) for p in AUTHORED}
        self.observations['source_manifest_sha256'] = file_hash(self.manifest_path)
        self.observations['unchanged_sha256'] = {}
        for index, relative in enumerate(UNCHANGED):
            original = self.run('base-bytes-' + str(index), ['git', 'show', m['head'] + ':' + relative])
            digest = file_hash(REPO / relative)
            require(digest == hash_bytes(original), 'unchanged source drift: ' + relative)
            self.observations['unchanged_sha256'][relative] = digest
        self.observations['committed_author'] = self.git('commit-provenance', 'log', '-1', '--format=fuller')
        self.observations['baseline_only'] = self.identity['head'] == m['head']
        self.observations['runtime_link'] = {'path': str(venv), 'target': str(venv.resolve())}
        self.observations['fixture_spec_sha256'] = file_hash(REPO / AUTHORED[1])

    def summary(self, label, raw):
        lines = raw.decode().splitlines()
        value = json.loads(lines[-1])
        require(value['testsRun'] > 0 and value['failures'] == value['errors'] == value['skips'] == 0,
                label + ': tests failed or skipped')
        self.observations[label] = value
        return value

    def suite(self):
        raw = self.run('suite', [sys.executable, str(REPO / AUTHORED[0]), '--temp-root', str(self.temp_root)])
        summary = self.summary('suite', raw)
        fixture = json.loads((REPO / AUTHORED[1]).read_text())
        require(summary['dimensions'] == fixture['dimensions'], 'suite omitted frozen dimensions')
        expected = len(fixture['detectors']) * (len(fixture['dimensions']) - 1 + 1) + 1
        require(summary['testsRun'] == expected, 'suite missing generated cases')
        controls = [json.loads(line) for line in raw.decode().splitlines() if line.startswith('{')][:-1]
        require({row['control'] for row in controls} == {row['name'] for row in fixture['detectors']}, 'missing mutation controls')
        require(len(controls) == len(fixture['detectors']), 'duplicate mutation controls')
        for control in controls:
            require(control['unmodified_baseline'] == control['restored'], 'mutation not restored')
            require(control['unmodified_baseline']['manifest'] == control['injected']['manifest'], 'mutation changed prerequisites')
            require('NO_EXPOSED_HITS: blocked partial population' in control['expected_assertion_output'], 'wrong killed assertion')
        self.observations['generated_case_count'] = expected
        self.observations['negative_control_count'] = len(controls)

    def compatibility(self):
        raw = self.run('compatibility', [sys.executable, str(Path(__file__).resolve()),
            '--child', 'compatibility', '--temp-root', str(self.temp_root)])
        self.summary('compatibility', raw)

    def portable(self):
        unrelated = self.root / 'unrelated-cwd'
        unrelated.mkdir(mode=0o700)
        raw = self.run('portable-script', [sys.executable, str(REPO / AUTHORED[0]),
            '--temp-root', str(self.temp_root)], cwd=unrelated)
        summary = self.summary('portable-script', raw)
        raw2 = self.run('portable-discovery', [sys.executable, str(Path(__file__).resolve()),
            '--child', 'discovery', '--temp-root', str(self.temp_root)], cwd=unrelated)
        discovered = self.summary('portable-discovery', raw2)
        require(discovered['testsRun'] == summary['testsRun'] + 2, 'discovery case coverage differs')
        host_pattern = re.compile(r'/(?:home|Users|workspace|tmp)/')
        for name in AUTHORED:
            require(not host_pattern.search((REPO / name).read_text()), 'host-only source literal: ' + name)
        self.observations['host_literal_check'] = {'files': list(AUTHORED), 'findings': []}

    def credentials(self):
        runtime = self.manifest['runtime']
        scanner = Path(runtime['detect_secrets'])
        require(scanner.is_file() and scanner.parent == Path(runtime['credential_scan_python3']).parent,
                'credential scanner runtime mismatch')
        # The installed scanner uses os.walk with followlinks=False (default).
        # Inventory all staged symlinks without following dependency directories.
        links = []
        for directory, dirs, files in os.walk(REPO, followlinks=False):
            if Path(directory) == REPO and '.git' in dirs:
                dirs.remove('.git')
            for name in dirs + files:
                path = Path(directory) / name
                if path.is_symlink():
                    links.append(str(path.relative_to(REPO)))
        require(links == ['.venv'], 'scanner boundary contains unapproved symlinks')
        self.observations['credential_scan_symlinks_not_traversed'] = links
        raw = self.run('detect-secrets', [str(scanner), 'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify'])
        report = self.root / 'detect-secrets.stdout'
        parsed = json.loads(raw)
        require(isinstance(parsed['results'], dict) and parsed.get('plugins_used'), 'incomplete credential scan')
        self.run('check-secret-report', [sys.executable, str(REPO / 'tools' / 'check_secret_report.py'), str(report)])
        self.observations['credential_results_paths'] = sorted(parsed['results'])

    def public_tree(self):
        require(not self.start_status, 'public-tree requires committed source')
        inventory = self.git('scan-inventory', 'ls-files', '-z', '--cached', '--others', '--exclude-standard')
        paths = inventory.split('\0')
        paths = sorted(set(paths) - {''})
        tracked = sorted(set(self.git('tracked-inventory', 'ls-files', '-z').split('\0')) - {''})
        require(set(paths) - set(tracked) <= {'.venv'}, 'scan-visible untracked source')
        self.observations['scan_visible_paths'] = paths
        self.observations['tracked_paths'] = tracked
        clone = self.root / 'committed-tree'
        self.run('local-clone', ['git', 'clone', '--local', '--no-hardlinks', '--no-checkout',
                               '--no-recurse-submodules', str(REPO), str(clone)])
        self.run('local-checkout', ['git', 'checkout', '--detach', self.identity['head']], cwd=clone)
        require(self.git('clone-head', 'rev-parse', 'HEAD', cwd=clone) == self.identity['head'], 'clone HEAD drift')
        require(self.git('clone-tree', 'rev-parse', 'HEAD^{tree}', cwd=clone) == self.identity['tree'], 'clone tree drift')
        require(not self.git('clone-status', 'status', '--porcelain', '--untracked-files=all', cwd=clone), 'clone not clean')
        require(not (clone / '.venv').exists(), 'clone runtime link is forbidden')
        for name, digest in {**self.observations['authored_sha256'], **self.observations['unchanged_sha256']}.items():
            require(file_hash(clone / name) == digest, 'clone hash drift: ' + name)
        raw = self.run('committed-tree-public-scan', [sys.executable, str(clone / 'tools' / 'check_public_tree.py')], cwd=clone)
        parsed = json.loads(raw)
        require(parsed['findings'] == [] and parsed['scanned_files'] == len(tracked), 'incomplete or dirty public scan')
        self.observations['exact_committed_tree'] = parsed
        raw = self.run('in-place-informational', [sys.executable, str(REPO / 'tools' / 'check_public_tree.py')], expected=(0, 1))
        info = json.loads(raw)
        expected = [['.venv', 0, 'symlink_not_scanned']]
        require(info['findings'] in ([], expected), 'unexpected in-place finding')
        command = self.commands[-1]
        require(command['exit'] == int(bool(info['findings'])), 'in-place exit/finding mismatch')
        require(info['scanned_files'] == len(tracked), 'in-place scan incomplete')
        self.observations['in_place_informational_INC_174'] = info

    def finalize(self, error=None):
        if self.identity:
            try:
                require(self.git('head-after', 'rev-parse', 'HEAD') == self.identity['head'], 'HEAD changed during row')
                if self.start_status is not None:
                    require(self.git('status-after', 'status', '--porcelain=v1', '--untracked-files=all') == self.start_status,
                            'status changed during row')
                if 'authored_sha256' in self.observations:
                    require({p: file_hash(REPO / p) for p in AUTHORED} == self.observations['authored_sha256'],
                            'source changed during row')
            except Exception:
                error = (error or '') + '\nFINAL SOURCE RECHECK:\n' + traceback.format_exc()
        receipt = {'schema': 'fc10-partial-hit-suppression-row/v1', 'phase': self.args.phase,
                   'invocation': str(self.root), 'argv': sys.argv, 'cwd': str(Path.cwd()),
                   'interpreter': sys.executable, 'candidate': self.identity, 'commands': self.commands,
                   'observations': self.observations, 'exit': 1 if error else 0, 'error': error,
                   'limits': ['REVIEW REQUIRED; not semantic/legal/human/live acceptance',
                              'Scoped local tests, not full-repository CI',
                              'Remote transport and required exact-head shipping CI remain OWED',
                              'No publication or source/live hold changes']}
        (self.root / 'receipt.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
        # Freeze only after every child has exited and all raw output is closed.
        entries = {}
        for path in sorted(self.root.rglob('*')):
            require(not path.is_symlink(), 'evidence symlink')
            if path.is_file():
                relative = str(path.relative_to(self.root))
                require(relative not in entries and path == safe_path(path), 'duplicate evidence path')
                entries[relative] = {'sha256': file_hash(path), 'bytes': path.stat().st_size}
        manifest = {'schema': 'fc10-evidence-files/v1', 'files': entries,
                    'candidate': self.identity}
        target = self.root / 'evidence-manifest.json'
        target.write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
        actual = {str(p.relative_to(self.root)) for p in self.root.rglob('*') if p.is_file() and p != target}
        require(actual == set(entries), 'omitted evidence output')
        for name, entry in entries.items():
            require(file_hash(self.root / name) == entry['sha256'], 'evidence hash mismatch')
        print(json.dumps({'phase': self.args.phase, 'exit': receipt['exit'], 'candidate': self.identity,
                          'receipt': str(self.root / 'receipt.json'), 'manifest': str(target),
                          'manifest_sha256': file_hash(target), 'error': error}, sort_keys=True))
        return receipt['exit']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('suite', 'compatibility', 'portable', 'credentials', 'public-tree'))
    parser.add_argument('--source-manifest', type=Path)
    parser.add_argument('--out-base', type=Path)
    parser.add_argument('--child', choices=('compatibility', 'discovery'))
    parser.add_argument('--temp-root', type=Path)
    args = parser.parse_args()
    if args.child:
        require(args.temp_root is not None, 'child temporary root required')
        return child(args.child, args.temp_root)
    require(args.phase and args.source_manifest and args.out_base, 'row arguments required')
    with ExitStack() as stack:
        for name in ('socket', 'create_connection', 'getaddrinfo'):
            stack.enter_context(patch.object(socket, name, no_network))
        row = Row(args)
        error = None
        try:
            row.prepare()
            getattr(row, args.phase.replace('-', '_'))()
        except Exception:
            error = traceback.format_exc()
        return row.finalize(error)


if __name__ == '__main__':
    raise SystemExit(main())
