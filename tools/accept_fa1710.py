"""Small offline unittest/evidence launcher for the FA1710 test-only card."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib
import io
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
import uuid

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
TEST = 'tests.records.test_fa1710_receipt_line_bytes'
NEW_PATHS = ('tests/records/test_fa1710_receipt_line_bytes.py', 'tools/accept_fa1710.py')
SOURCE = 'campaign_tool/records/ledger/receipt_projection.py'
COMPATIBILITY = [
    'tests.records.test_ledger_receipts.ReceiptProjectionTests.test_exact_bytes_roles_and_nonpromotion',
    'tests.records.test_ledger_receipts.ReceiptProjectionTests.test_source_snapshot_mutation_rejected',
]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def checked(path):
    path = Path(os.path.abspath(path))
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink ancestry')
    return path


def git(*args, cwd=REPO):
    return subprocess.check_output(['git', *args], cwd=cwd).decode().strip()


def identity():
    manifest_path = checked(REPO.parent / 'SOURCE-MANIFEST.json')
    manifest = json.loads(manifest_path.read_bytes())
    base = manifest['head']
    require(re.fullmatch('[0-9a-f]{40}', base) is not None, 'invalid admitted base')
    head = git('rev-parse', 'HEAD')
    require(re.fullmatch('[0-9a-f]{40}', head) is not None, 'invalid candidate HEAD')
    require(subprocess.run(['git', 'merge-base', '--is-ancestor', base, head], cwd=REPO).returncode == 0,
            'base is not candidate ancestor')
    baseline = subprocess.check_output(['git', 'show', base + ':' + SOURCE], cwd=REPO)
    require((REPO / SOURCE).read_bytes() == baseline, 'protected source drift')
    changes = git('diff', '--name-only', base, 'HEAD').splitlines()
    require(set(changes) <= set(NEW_PATHS), 'committed scope drift')
    require(not git('diff', '--name-only'), 'tracked working changes')
    for name in NEW_PATHS:
        data = (REPO / name).read_bytes()
        require(re.search(rb'(?<![a-fA-F0-9])[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?(?![a-fA-F0-9])', data) is None,
                'committed input may not embed hash literals')
        require(re.search(rb'/(?:home|Users)/', data) is None, 'box-only path in authored code')
    hashes = {name: sha((REPO / name).read_bytes()) for name in (*NEW_PATHS, SOURCE)}
    return manifest, dict(head=head, tree=git('rev-parse', 'HEAD^{tree}'), base=base,
        source_manifest_sha256=sha(manifest_path.read_bytes()), hashes=hashes,
        git_blobs={name: git('rev-parse', head + ':' + name) for name in (SOURCE,)},
        status=git('status', '--porcelain', '--untracked-files=all'))


def run_command(run, label, argv, cwd):
    completed = subprocess.run([str(v) for v in argv], cwd=cwd, capture_output=True)
    stdout = run / (label + '.stdout')
    stderr = run / (label + '.stderr')
    stdout.write_bytes(completed.stdout)
    stderr.write_bytes(completed.stderr)
    row = dict(label=label, argv=[str(v) for v in argv], cwd=str(cwd),
               exit=completed.returncode, stdout=str(stdout), stderr=str(stderr),
               stdout_sha256=sha(completed.stdout), stderr_sha256=sha(completed.stderr))
    (run / (label + '.command.json')).write_text(json.dumps(row, indent=2) + '\n')
    require(completed.returncode == 0, label + ' child failed; retained raw output')
    return completed.stdout, row


def run_suite(run, label, selectors, *, negative=False):
    # Safety scope surrounds importing even the original compatibility module.
    module = importlib.import_module(TEST)
    stdout = io.StringIO()
    stderr = io.StringIO()
    old_temp = tempfile.tempdir
    tempfile.tempdir = str(run)
    try:
        with module.offline(run):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                loader = unittest.TestLoader()
                suite = loader.loadTestsFromNames(selectors)
                require(not loader.errors, 'unittest loading error')
                def test_ids(tests):
                    for test in tests:
                        if isinstance(test, unittest.TestSuite):
                            yield from test_ids(test)
                        else:
                            yield test.id()
                selected = list(test_ids(suite))
                result = unittest.TextTestRunner(stream=stderr, verbosity=2).run(suite)
    finally:
        tempfile.tempdir = old_temp
    out = stdout.getvalue().encode()
    err = stderr.getvalue().encode()
    (run / (label + '.stdout')).write_bytes(out)
    (run / (label + '.stderr')).write_bytes(err)
    row = dict(label=label, selectors=selectors, selected_test_ids=selected, executed=result.testsRun,
               failures=len(result.failures), errors=len(result.errors), skips=len(result.skipped),
               expected_failures=len(result.expectedFailures), unexpected_successes=len(result.unexpectedSuccesses),
               stdout_sha256=sha(out), stderr_sha256=sha(err),
               exit=0 if result.wasSuccessful() else 1)
    (run / (label + '.tests.json')).write_text(json.dumps(row, indent=2) + '\n')
    require(result.testsRun == len(selected) > 0 and not result.errors and not result.skipped
            and not result.expectedFailures and not result.unexpectedSuccesses,
            'missing/error/skipped/unexpected tests')
    if negative:
        require(result.testsRun == 1 and len(result.failures) == 1, 'control must have exactly one assertion failure')
        require(result.failures[0][0].id() == selectors[0], 'wrong failed test')
        require('AssertionError:' in result.failures[0][1] and
                'FA1710 exact raw byte spans including terminators' in result.failures[0][1],
                'control failed at wrong assertion')
    else:
        require(result.wasSuccessful(), 'positive unittest failure')
    return row


def clean_scan(run, head):
    require(not git('status', '--porcelain', '--untracked-files=all'), 'scans require clean signed-off HEAD')
    for name in NEW_PATHS:
        require(git('rev-parse', head + ':' + name), 'authored path not committed')
    checkout = run / 'scan-checkout'
    run_command(run, 'clone', ['git', 'clone', '--no-hardlinks', '--no-checkout',
                             '--', REPO, checkout], REPO)
    run_command(run, 'detach', ['git', 'checkout', '--detach', head], checkout)
    require(git('rev-parse', 'HEAD', cwd=checkout) == head, 'scan head mismatch')
    require(git('rev-parse', 'HEAD^{tree}', cwd=checkout) == git('rev-parse', 'HEAD^{tree}'), 'scan tree mismatch')
    original_paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=REPO)
    scan_paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=checkout)
    require(original_paths == scan_paths, 'incomplete tracked path set')
    require(not git('status', '--porcelain', '--untracked-files=all', cwd=checkout), 'dirty scan checkout')
    for relative in scan_paths.decode().split('\0'):
        if relative:
            path = checked(checkout / relative)
            require(path.is_file(), 'nonregular scan file')
    require(not (checkout / '.venv').exists(), 'runtime artifact in scan checkout')
    return checkout, len([p for p in scan_paths.split(b'\0') if p])


def ambient():
    mask = os.umask(0o077)
    os.umask(mask)
    return (tuple(sys.path), tempfile.tempdir, socket.socket, socket.create_connection,
            subprocess.Popen, sqlite3.connect, os.open, dict(os.environ), mask)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, choices=('behavior', 'compatibility',
        'negative-control', 'isolation', 'credential-scan', 'public-tree'))
    parser.add_argument('--output-root', required=True, type=Path)
    args = parser.parse_args()
    parent = checked(args.output_root)
    require(REPO.parent == parent or REPO.parent in parent.parents, 'output outside candidate root')
    require(parent != REPO and REPO not in parent.parents, 'output inside repository')
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    run = parent / ('run-' + uuid.uuid4().hex)
    run.mkdir(mode=0o700)
    sys.path.insert(0, str(REPO))
    tempfile.tempdir = str(run)
    os.environ['TMPDIR'] = str(run)
    # Inherited child control is bytecode-only, not interpreter/provider routing.
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    receipt: dict = dict(argv=[sys.executable, *sys.argv], cwd=str(Path.cwd()),
                   interpreter=sys.executable, case=args.case, output=str(run), rows=[])
    status = 1
    try:
        manifest, before = identity()
        receipt['identity'] = before
        behavior = [TEST + '.ReceiptLineBytesTests']
        positive = [TEST + '.ReceiptLineBytesTests.test_crlf_oracle_positive']
        if args.case == 'behavior':
            receipt['rows'].append(run_suite(run, 'behavior', behavior))
        elif args.case == 'compatibility':
            receipt['rows'].append(run_suite(run, 'compatibility', COMPATIBILITY))
        elif args.case == 'negative-control':
            receipt['rows'].append(run_suite(run, 'positive-before', positive))
            receipt['rows'].append(run_suite(run, 'stripped-result',
                [TEST + '.ReceiptLineBytesTests.stripped_result_control'], negative=True))
            receipt['rows'].append(run_suite(run, 'positive-after', positive))
        elif args.case == 'isolation':
            snapshot = ambient()
            receipt['rows'].append(run_suite(run, 'ordinary-before', behavior))
            require(ambient() == snapshot, 'ordinary loading poisoned ambient state')
            # Separate fresh prefix and process; only the named file launcher is executed.
            _, row = run_command(run, 'second-prefix', [sys.executable, '-B', __file__,
                '--case', 'behavior', '--output-root', run / 'second-fixtures'], REPO)
            receipt['rows'].append(row)
            receipt['rows'].append(run_suite(run, 'ordinary-after', behavior))
            receipt['rows'].append(run_suite(run, 'peer-compatibility', COMPATIBILITY))
            require(ambient() == snapshot, 'suite poisoned peer ambient state')
            receipt['ambient_restored'] = True
        else:
            checkout, count = clean_scan(run, before['head'])
            receipt['scanned_tracked_files'] = count
            if args.case == 'credential-scan':
                runtime = manifest['runtime']
                interpreter = Path(runtime['credential_scan_python3'])
                detector = Path(runtime['detect_secrets'])
                require(interpreter.is_file() and detector.is_file(), 'declared scanner missing')
                data, row = run_command(run, 'detect-secrets', [interpreter, detector,
                    'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify'], checkout)
                receipt['rows'].append(row)
                report = json.loads(data)
                require(isinstance(report.get('results'), dict), 'invalid real scan report')
                output, row = run_command(run, 'check-secret-report', [interpreter,
                    checkout / 'tools/check_secret_report.py', run / 'detect-secrets.stdout'], checkout)
                require(b'potential secrets: 0;' in output, 'unapproved potential secrets')
                receipt['rows'].append(row)
            else:
                data, row = run_command(run, 'public-tree', [REPO / '.venv/bin/python3',
                    checkout / 'tools/check_public_tree.py'], checkout)
                report = json.loads(data)
                require(report['scanned_files'] == count and report['findings'] == [], 'incomplete/violating public scan')
                receipt['rows'].append(row)
        _, after = identity()
        require(before == after, 'source/test/HEAD/status identity changed during acceptance')
        status = 0
    except Exception:
        receipt['error'] = traceback.format_exc()
        (run / 'launcher-error.stderr').write_text(receipt['error'])
    receipt['exit'] = status
    receipt['verdict'] = 'REVIEW_REQUIRED' if status == 0 else 'FAIL'
    (run / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(dict(exit=status, receipt=str(run / 'receipt.json'), verdict=receipt['verdict'])))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
