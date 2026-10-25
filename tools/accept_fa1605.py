"""File-based, offline FA1605 unittest and exact-HEAD scanner launcher."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
NEW_TEST = 'tests.records.test_fa1605_occurrence_budgets'
BEHAVIOR = [NEW_TEST + '.OccurrenceBudgetTests.' + name for name in (
    'test_exact_aggregate_bytes_and_rows_project_and_replay',
    'test_row_only_one_below_refuses_before_decode',
    'test_byte_only_one_below_refuses_before_decode',
    'test_utf8_between_character_and_byte_lengths_refuses')]
COMPATIBILITY = ['tests.records.test_ledger_occurrences.OccurrenceProjectionTests.' + name for name in (
    'test_invalid_projection_limits_and_import_id',
    'test_incomplete_capture_is_not_projected',
    'test_local_projection_preserves_payload_and_stage_holds')]
AUTHORED = ['tests/records/test_fa1605_occurrence_budgets.py', 'tools/accept_fa1605.py']


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def checked(path):
    path = Path(os.path.abspath(path))
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink ancestry refused')
    return path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def command(argv, cwd, run, label, expected=0):
    result = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True, text=True)
    out, err = run / (label + '.stdout'), run / (label + '.stderr')
    out.write_text(result.stdout)
    err.write_text(result.stderr)
    save(run / (label + '.command.json'), dict(argv=[str(a) for a in argv], cwd=str(cwd),
         exit=result.returncode, stdout_sha256=digest(out), stderr_sha256=digest(err)))
    require(result.returncode == expected, label + ' unexpected exit: ' + str(result.returncode))
    return result.stdout


def git(*args):
    return subprocess.check_output(['git', *args], cwd=REPO, text=True).strip()


def identity(manifest):
    base = manifest['head']
    require(re.fullmatch('[0-9a-f]{40}', base) is not None, 'invalid baseline identity')
    require(git('merge-base', base, 'HEAD') == base, 'baseline drift')
    return dict(head=git('rev-parse', 'HEAD'), tree=git('rev-parse', 'HEAD^{tree}'), base=base,
                status=git('status', '--porcelain', '--untracked-files=all'),
                source_hashes={p: digest(REPO / p) for p in (
                    'campaign_tool/records/ledger/occurrence_projection.py',
                    'campaign_tool/records/ledger/store.py',
                    'campaign_tool/records/ledger/migrations/v001.py',
                    'campaign_tool/records/ledger/migrations/occurrence_projection_v001.py',
                    'tests/records/test_ledger_occurrences.py',
                    'tests/records/test_ledger.py', *AUTHORED)},
                blobs={p: git('hash-object', p) for p in AUTHORED})


def run_suite(ids, run, label, *, ordinary=False, expected_failure=False):
    # No application import precedes this scope. No data-bearing module defaults
    # occur in the inspected store/projection/migrations/selected-test closure.
    module = importlib.import_module(NEW_TEST)
    fixture_root = run / (label + '-fixtures')
    fixture_root.mkdir(mode=0o700)
    output = io.StringIO()
    ambient = (list(sys.path), tempfile.tempdir, socket_identity(), os.environ.copy())
    with patch.object(tempfile, 'tempdir', str(fixture_root)), module.offline(fixture_root), redirect_stdout(output), redirect_stderr(output):
        if ordinary:
            program = unittest.main(module=None, argv=['python3 -m unittest', '-v', *ids], exit=False, testRunner=unittest.TextTestRunner(stream=output, verbosity=2))
            result = program.result
        else:
            suite = unittest.defaultTestLoader.loadTestsFromNames(ids)
            result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    require(ambient == (list(sys.path), tempfile.tempdir, socket_identity(), os.environ.copy()), 'ambient state leaked')
    raw = run / (label + '.unittest.txt')
    raw.write_text(output.getvalue())
    data = dict(selected_test_ids=ids, testsRun=result.testsRun,
                failures=[(test.id(), text) for test, text in result.failures],
                errors=[(test.id(), text) for test, text in result.errors],
                skipped=[(test.id(), reason) for test, reason in result.skipped], successful=result.wasSuccessful(),
                ordinary_unittest=ordinary, raw_sha256=digest(raw))
    save(run / (label + '.tests.json'), data)
    require(result.testsRun == len(ids) and result.testsRun > 0, 'missing or zero tests')
    require(not result.errors and not result.skipped, 'unexpected error or skip')
    if expected_failure:
        require(len(result.failures) == 1 and not result.wasSuccessful(), 'control did not fail exactly once')
        require('UTF8_REFUSAL_ORACLE' in result.failures[0][1] and 'decode_spy_reached' in result.failures[0][1], 'unrelated control failure')
    else:
        require(result.wasSuccessful(), 'required unittest failure')
    return data


def socket_identity():
    import builtins
    import socket
    import sqlite3
    return (socket.socket.connect, socket.create_connection, subprocess.Popen,
            sqlite3.connect, builtins.open, io.open, tempfile.TemporaryDirectory)


def exact_checkout(run, head):
    require(not git('status', '--porcelain', '--untracked-files=all'), 'scan requires committed clean source')
    target = run / 'exact-head-source'
    command(['git', 'clone', '--local', '--no-hardlinks', '--no-checkout', REPO, target], REPO, run, 'clone')
    command(['git', 'checkout', '--detach', head], target, run, 'checkout')
    exported = command(['git', 'rev-parse', 'HEAD'], target, run, 'export-head').strip()
    require(exported == head, 'export head mismatch')
    tree = command(['git', 'rev-parse', 'HEAD^{tree}'], target, run, 'export-tree').strip()
    require(tree == git('rev-parse', 'HEAD^{tree}'), 'export tree mismatch')
    paths = command(['git', 'ls-files', '-z'], target, run, 'export-paths')
    require(paths == subprocess.check_output(['git', 'ls-files', '-z'], cwd=REPO, text=True), 'tracked set mismatch')
    names = paths.rstrip('\0').split('\0')
    hashes = {}
    for name in names:
        left, right = REPO / name, target / name
        require(not left.is_symlink() and not right.is_symlink(), 'tracked symlink refused')
        require(left.read_bytes() == right.read_bytes(), 'export byte mismatch: ' + name)
        hashes[name] = digest(right)
    require(set(AUTHORED).issubset(names), 'authored files missing from export')
    require(not command(['git', 'status', '--porcelain'], target, run, 'export-status').strip(), 'dirty export')
    save(run / 'export-identity.json', dict(head=head, tree=tree, tracked_count=len(names), files=hashes))
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, choices=('behavior', 'compatibility', 'negative-control', 'isolation', 'credential-scan', 'public-tree'))
    parser.add_argument('--output-root', required=True, type=Path)
    args = parser.parse_args()
    candidate = checked(REPO.parent)
    output = checked(args.output_root)
    require(output.is_relative_to(candidate) and not output.is_relative_to(REPO), 'output must be outside repo inside candidate')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    run = Path(tempfile.mkdtemp(prefix='run-', dir=output))
    temporary = run / 'tmp'
    temporary.mkdir(mode=0o700)
    manifest = json.loads((candidate / 'SOURCE-MANIFEST.json').read_text())
    sys.path.insert(0, str(REPO))
    before = identity(manifest)
    receipt = dict(case=args.case, argv=[sys.executable, *sys.argv], cwd=str(Path.cwd()), interpreter=sys.executable,
                   manifest_sha256=digest(candidate / 'SOURCE-MANIFEST.json'), identity=before,
                   author_runtime='gpt-6.1-sol / openai-codex / sol-reviewer', exit=1)
    save(run / 'receipt.json', receipt)
    print('Retained run: ' + str(run), flush=True)
    try:
        with patch.object(tempfile, 'tempdir', str(temporary)):
            if args.case == 'behavior':
                run_suite(BEHAVIOR, run, 'behavior')
            elif args.case == 'compatibility':
                run_suite(COMPATIBILITY, run, 'compatibility')
            elif args.case == 'negative-control':
                module = importlib.import_module(NEW_TEST)
                # Import the real loader under the same pre-import safety scope.
                with module.offline(temporary):
                    from campaign_tool.records.ledger import occurrence_projection
                with module.character_budget_variant():
                    run_suite([BEHAVIOR[-1]], run, 'faulty-character-budget', expected_failure=True)
                    run_suite([BEHAVIOR[1]], run, 'faulty-row-gate-still-effective')
                run_suite([BEHAVIOR[-1], BEHAVIOR[1]], run, 'restored-positive')
            elif args.case == 'isolation':
                for path in AUTHORED:
                    text = (REPO / path).read_text()
                    require(not re.search(r'/(?:home|Users|workspace)/', text), 'box-only path')
                    require(not re.search(r'(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])|(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])', text), 'literal hash in authored code')
                run_suite(BEHAVIOR + COMPATIBILITY, run, 'ordinary-before', ordinary=True)
                run_suite(BEHAVIOR, run, 'second-prefix-behavior')
                run_suite(BEHAVIOR + COMPATIBILITY, run, 'ordinary-after', ordinary=True)
            else:
                export = exact_checkout(run, before['head'])
                if args.case == 'credential-scan':
                    runtime = manifest['runtime']
                    python = Path(runtime['credential_scan_python3'])
                    scanner = Path(runtime['detect_secrets'])
                    require(python.is_file() and scanner.is_file(), 'declared scanner runtime unavailable')
                    command([python, '-B', scanner, 'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify'], export, run, 'detect-secrets')
                    report = run / 'detect-secrets.stdout'
                    data = json.loads(report.read_text())
                    require(isinstance(data['results'], dict), 'invalid real scan report')
                    checked_output = command([python, '-B', export / 'tools/check_secret_report.py', report], export, run, 'check-secret-report')
                    require('potential secrets: 0;' in checked_output, 'unapproved secrets')
                else:
                    raw = command([sys.executable, '-B', export / 'tools/check_public_tree.py'], export, run, 'public-tree')
                    result = json.loads(raw)
                    require(result['scanned_files'] > 0 and not result['findings'], 'public-tree violations')
            require(identity(manifest) == before, 'source identity changed during invocation')
        receipt['exit'] = 0
    except Exception:
        (run / 'failure.txt').write_text(traceback.format_exc())
        traceback.print_exc()
    finally:
        receipt['evidence_hashes'] = {p.name: digest(p) for p in run.iterdir() if p.is_file() and p.name != 'receipt.json'}
        save(run / 'receipt.json', receipt)
    return receipt['exit']


if __name__ == '__main__':
    raise SystemExit(main())
