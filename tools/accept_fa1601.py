"""FA1601 offline unittest/evidence launcher; no service or live-data support."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import traceback
import unittest

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
CANDIDATE = REPO.parent
TEST_MODULE = 'tests.records.test_fa1601_receipt_partitions'
NEW_FILES = ('tests/records/test_fa1601_receipt_partitions.py', 'tools/accept_fa1601.py')
COMPATIBILITY = tuple('tests.records.test_ledger_receipts.ReceiptProjectionTests.' + name for name in (
    'test_checkpoint_resume', 'test_atomic_failure_recovery', 'test_exact_bytes_roles_and_nonpromotion'))
CASES = ('behavior', 'compatibility', 'negative-control', 'isolation', 'credential-scan', 'public-tree')
SOURCE = 'campaign_tool/records/ledger/receipt_projection.py'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(path):
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        require(not part.is_symlink(), 'symlink ancestry rejected: ' + str(part))
        if part.exists():
            require(part.is_dir(), 'directory ancestry required: ' + str(part))
    return path


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


class Launcher:
    def __init__(self, case, root):
        self.case = case
        root = safe_path(root)
        require(CANDIDATE in root.parents and REPO not in root.parents and root != REPO,
                'output root must be outside repo under its candidate parent')
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.run = Path(tempfile.mkdtemp(prefix=case + '-', dir=root))
        self.tmp = self.run / 'tmp'
        self.tmp.mkdir(mode=0o700)
        self.manifest_path = CANDIDATE / 'SOURCE-MANIFEST.json'
        require(not self.manifest_path.is_symlink(), 'source manifest symlink')
        self.manifest = json.loads(self.manifest_path.read_text())
        self.receipt = {'case': case, 'argv': [sys.executable, *sys.argv], 'cwd': str(Path.cwd()),
                        'interpreter': sys.executable, 'run_root': str(self.run),
                        'source_manifest_sha256': sha(self.manifest_path.read_bytes()),
                        'commands': [], 'unittest_results': [], 'status': 'RUNNING', 'exit': None}
        self.counter = 0

    def command(self, argv, cwd=REPO, allowed=(0,)):
        self.counter += 1
        label = f'command-{self.counter:02d}'
        env = dict(os.environ)
        env['TMPDIR'] = str(self.tmp)
        result = subprocess.run([str(x) for x in argv], cwd=cwd, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=240)
        out = self.run / (label + '.stdout')
        err = self.run / (label + '.stderr')
        out.write_bytes(result.stdout)
        err.write_bytes(result.stderr)
        self.receipt['commands'].append({'argv': [str(x) for x in argv], 'cwd': str(cwd),
                                         'exit': result.returncode, 'stdout': str(out), 'stderr': str(err),
                                         'stdout_sha256': sha(result.stdout), 'stderr_sha256': sha(result.stderr)})
        require(result.returncode in allowed, f'{label} child exit {result.returncode}; see retained output')
        return result.stdout

    def git(self, *args, cwd=REPO):
        return self.command(['git', *args], cwd=cwd)

    def identity(self):
        head = self.git('rev-parse', 'HEAD').decode().strip()
        base = self.manifest['head']
        require(re.fullmatch('[0-9a-f]{40}', head) is not None, 'invalid HEAD')
        require(re.fullmatch('[0-9a-f]{40}', base) is not None, 'invalid staged base')
        self.git('merge-base', '--is-ancestor', base, head)
        baseline = self.git('show', base + ':' + SOURCE)
        require((REPO / SOURCE).read_bytes() == baseline, 'product source drift')
        changed = self.git('diff', '--name-only', base, 'HEAD').decode().splitlines()
        require(all(p in NEW_FILES or p.startswith('tests/fixtures/fa1601/') for p in changed),
                'committed changes outside admitted paths')
        self.receipt.update(head=head, base=base,
                            tree=self.git('rev-parse', 'HEAD^{tree}').decode().strip(),
                            status_porcelain=self.git('status', '--porcelain=v1', '--untracked-files=all').decode(),
                            source_sha256=sha(baseline),
                            authored_sha256={p: sha((REPO / p).read_bytes()) for p in NEW_FILES})
        # Hash the complete inspected application/compatibility import closure.
        closure = ['campaign_tool/__init__.py', 'campaign_tool/records/__init__.py',
                   'campaign_tool/records/ledger/__init__.py', 'campaign_tool/records/ledger/store.py',
                   SOURCE, 'campaign_tool/records/ledger/migrations/__init__.py',
                   'campaign_tool/records/ledger/migrations/v001.py',
                   'campaign_tool/records/ledger/migrations/receipt_projection_v001.py',
                   'campaign_tool/records/ledger/migrations/receipt_source_guard_v001.py',
                   'tests/__init__.py', 'tests/records/__init__.py', 'tests/records/test_ledger_receipts.py']
        self.receipt['inspected_closure_sha256'] = {p: sha((REPO / p).read_bytes()) for p in closure}
        for name in NEW_FILES:
            raw = (REPO / name).read_bytes()
            require(re.search(rb'(?<![0-9a-fA-F])(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})(?![0-9a-fA-F])', raw) is None,
                    'literal identity in new code')
            host_prefixes = [b'/' + name + b'/' for name in (b'home', b'Users')]
            require(not any(prefix in raw for prefix in host_prefixes), 'box-only path in new code')

    def tests(self, selectors, label, negative=False):
        module = importlib.import_module(TEST_MODULE)
        stdout, stderr = io.StringIO(), io.StringIO()
        ids = []

        class Result(unittest.TextTestResult):
            def startTest(self, test):
                ids.append(test.id())
                super().startTest(test)

        with redirect_stdout(stdout), redirect_stderr(stderr), module.offline_boundary(self.tmp):
            suite = module.negative_suite() if negative else unittest.defaultTestLoader.loadTestsFromNames(selectors)
            expected_count = suite.countTestCases()
            result = unittest.TextTestRunner(stream=stderr, verbosity=2, resultclass=Result).run(suite)
        out, err = stdout.getvalue().encode(), stderr.getvalue().encode()
        (self.run / (label + '.stdout')).write_bytes(out)
        (self.run / (label + '.stderr')).write_bytes(err)
        row = {'label': label, 'selected_test_ids': selectors, 'executed_test_ids': ids,
               'selected_count': expected_count, 'executed_count': result.testsRun,
               'failures': len(result.failures), 'errors': len(result.errors), 'skips': len(result.skipped),
               'expected_failures': len(result.expectedFailures), 'unexpected_successes': len(result.unexpectedSuccesses),
               'stdout_sha256': sha(out), 'stderr_sha256': sha(err),
               'stdout': str(self.run / (label + '.stdout')), 'stderr': str(self.run / (label + '.stderr')),
               'unittest_exit': 0 if result.wasSuccessful() else 1}
        self.receipt['unittest_results'].append(row)
        require(expected_count > 0 and result.testsRun == expected_count, 'zero/missing tests')
        require(not result.errors and not result.skipped and not result.expectedFailures
                and not result.unexpectedSuccesses, 'unexpected error/skip/result')
        if negative:
            require(result.testsRun == 1 and len(result.failures) == 1, 'control did not fail exactly once')
            test, detail = result.failures[0]
            require(test.id().endswith('.test_faulty_resume_reader') and module.FROZEN_MESSAGE in detail,
                    'control did not reach the intended frozen-index assertion')
            row['intended_failure_detected'] = True
        else:
            require(result.wasSuccessful(), 'unittest failure; preserve retained result')
        return row

    def portable(self, label):
        # Explicitly admitted ordinary module-loading surface; no custom path,
        # dynamic source, shell, or process-global hook is passed to the child.
        out = self.command([sys.executable, '-B', '-m', 'unittest', '-v', TEST_MODULE])
        command = self.receipt['commands'][-1]
        text = Path(command['stderr']).read_text()
        require(re.search(r'Ran 2 tests? in ', text) is not None and '\nOK\n' in text
                and 'skipped=' not in text, 'ordinary unittest count/result mismatch')
        command.update(label=label, executed_count=2,
                       selected_test_ids=[TEST_MODULE + '.ReceiptPartitionsTests.' + n for n in (
                           'test_missing_peer_frozen_across_batches', 'test_hash_mismatch_peer_frozen_across_batches')])
        return out

    def isolation(self):
        import builtins
        import socket
        import sqlite3
        import urllib.request
        module = importlib.import_module(TEST_MODULE)

        def ambient():
            return (tempfile.tempdir, tuple(sys.path), builtins.__import__, socket.socket.connect,
                    socket.socket.connect_ex, socket.create_connection, urllib.request.urlopen, sqlite3.connect)

        prior = ambient()
        self.portable('ordinary-before')
        self.tests(list(COMPATIBILITY), 'synthetic-peer-before')
        self.tests([TEST_MODULE], 'second-fixture-prefix')
        self.tests(list(COMPATIBILITY), 'synthetic-peer-after')
        with module.offline_boundary(self.tmp):
            checks = [('network', lambda: socket.create_connection(('fictional.invalid', 1))),
                      ('model', lambda: builtins.__import__('ollama')),
                      ('real-data', lambda: sqlite3.connect(':memory:'))]
            for name, action in checks:
                try:
                    action()
                except RuntimeError as error:
                    require('FA1601' in str(error), 'wrong sentinel failure')
                else:
                    raise RuntimeError('missing ' + name + ' sentinel')
        self.portable('ordinary-after')
        require(ambient() == prior, 'poisoned temporary/network/import/SQLite/sys.path state')
        require(self.git('rev-parse', 'HEAD').decode().strip() == self.receipt['head'], 'HEAD changed')
        require(sha((REPO / SOURCE).read_bytes()) == self.receipt['source_sha256'], 'source changed')
        self.receipt['isolation_checks'] = ['second fresh fixture prefix', 'ordinary unittest before/after',
                                            'network/model/real-data sentinels', 'ambient hooks restored',
                                            'source and HEAD unchanged', 'no box-only authored paths']

    def exact_checkout(self):
        require(not self.git('status', '--porcelain=v1', '--untracked-files=all').strip(),
                'scanner requires committed clean candidate')
        export = self.run / 'exact-head'
        self.git('clone', '--local', '--no-hardlinks', '--no-checkout', '--', str(REPO), str(export))
        self.git('checkout', '--detach', self.receipt['head'], cwd=export)
        require(not (export / '.git/objects/info/alternates').exists(), 'linked object store rejected')
        require(self.git('rev-parse', 'HEAD', cwd=export).decode().strip() == self.receipt['head'], 'export HEAD')
        require(self.git('rev-parse', 'HEAD^{tree}', cwd=export).decode().strip() == self.receipt['tree'], 'export tree')
        index = self.git('ls-files', '--stage', '-z')
        require(self.git('ls-files', '--stage', '-z', cwd=export) == index, 'tracked path/mode/blob set mismatch')
        paths = []
        file_hashes = {}
        for record in index.split(b'\0'):
            if not record:
                continue
            header, encoded = record.split(b'\t', 1)
            mode, blob, stage = header.decode().split()
            name = encoded.decode()
            require(mode in ('100644', '100755') and stage == '0', 'nonregular/unmerged tracked path')
            require(not Path(name).is_absolute() and '..' not in Path(name).parts, 'invalid tracked name')
            source, target = REPO / name, export / name
            require(not source.is_symlink() and not target.is_symlink(), 'tracked symlink')
            data = target.read_bytes()
            require(data == source.read_bytes(), 'export bytes differ: ' + name)
            actual_blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            require(actual_blob == blob, 'export blob differs: ' + name)
            paths.append(name)
            file_hashes[name] = sha(data)
        require(all(p in paths for p in NEW_FILES), 'new files absent in exact HEAD')
        self.git('diff', '--exit-code', 'HEAD', cwd=export)
        require(not self.git('status', '--porcelain=v1', '--untracked-files=all', cwd=export).strip(), 'dirty export')
        write_json(self.run / 'export-files.json', file_hashes)
        self.receipt['export'] = {'head': self.receipt['head'], 'tree': self.receipt['tree'],
                                  'tracked_count': len(paths), 'root': str(export),
                                  'file_map_sha256': sha((self.run / 'export-files.json').read_bytes())}
        return export

    def credential_scan(self):
        runtime = self.manifest['runtime']
        interpreter, entry = runtime['credential_scan_python3'], runtime['detect_secrets']
        require(Path(interpreter).is_file() and os.access(interpreter, os.X_OK), 'missing declared scan interpreter')
        require(Path(entry).is_file(), 'missing declared detect-secrets entry')
        export = self.exact_checkout()
        raw = self.command([interpreter, entry, 'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify'], cwd=export)
        report = self.run / 'detect-secrets.json'
        report.write_bytes(raw)
        data = json.loads(raw)
        require(isinstance(data.get('results'), dict), 'scanner did not return a real report')
        checked = self.command([interpreter, str(export / 'tools/check_secret_report.py'), str(report)], cwd=export)
        require(b'potential secrets: 0;' in checked, 'unapproved potential secrets')
        self.receipt['credential_scan'] = {'report_sha256': sha(raw), 'checker_sha256': sha(checked),
                                           'unapproved_potential_secrets': 0}

    def public_tree(self):
        export = self.exact_checkout()
        raw = self.command([str(REPO / '.venv/bin/python3'), '-B', str(export / 'tools/check_public_tree.py')], cwd=export)
        result = json.loads(raw)
        require(result['findings'] == [] and result['scanned_files'] == self.receipt['export']['tracked_count'],
                'public-tree violations or incomplete scan')
        self.receipt['public_tree'] = result

    def execute(self):
        old_path = list(sys.path)
        sys.path.insert(0, str(REPO))
        try:
            self.identity()
            if self.case == 'behavior':
                self.tests([TEST_MODULE], 'behavior')
            elif self.case == 'compatibility':
                row = self.tests(list(COMPATIBILITY), 'compatibility')
                require(row['executed_test_ids'] == list(COMPATIBILITY), 'compatibility selector substitution')
                require(row['executed_count'] == 3, 'missing compatibility tests')
            elif self.case == 'negative-control':
                self.tests(['test-local FaultyCaptureReader.test_faulty_resume_reader'], 'faulty-capture-reader', negative=True)
                self.tests([TEST_MODULE], 'unchanged-positive-after-control')
            elif self.case == 'isolation':
                self.isolation()
            elif self.case == 'credential-scan':
                self.credential_scan()
            elif self.case == 'public-tree':
                self.public_tree()
            self.receipt.update(status='PASS_REVIEW_REQUIRED', exit=0)
        except Exception:
            (self.run / 'launcher-error.txt').write_text(traceback.format_exc())
            self.receipt.update(status='FAIL_PRESERVED', exit=1)
        finally:
            sys.path[:] = old_path
            write_json(self.run / 'receipt.json', self.receipt)
        print(json.dumps({'case': self.case, 'status': self.receipt['status'],
                          'exit': self.receipt['exit'], 'receipt': str(self.run / 'receipt.json')}))
        return self.receipt['exit']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, choices=CASES)
    parser.add_argument('--output-root', required=True, type=Path)
    args = parser.parse_args()
    return Launcher(args.case, args.output_root).execute()


if __name__ == '__main__':
    raise SystemExit(main())
