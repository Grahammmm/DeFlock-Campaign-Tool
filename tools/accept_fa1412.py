"""FA1412 bounded offline acceptance launcher; evidence is always external.

Test children run this same file with --worker. No blanket discovery, network,
policy files, on-disk mutation, or removable assert-based acceptance gates.
"""
import argparse
import ast
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
CASES = ('behavior', 'compatibility', 'negative-control', 'isolation', 'credential-scan', 'public-tree')
BEHAVIOR = 'tests.records.test_fa1412.PresendPolicyTests'
COMPAT = [
    'tests.test_records_portal_https.HTTPSTests.test_success_pins_numeric_ip_and_original_host',
    'tests.test_records_portal_https.HTTPSTests.test_expired_after_resolution_blocks_connect',
    'tests.test_records_portal_https.HTTPSTests.test_egress_is_separate_and_precedes_dns',
    'tests.test_records_portal_https.HTTPSTests.test_transport_returns_redirect_without_following',
]
EXPIRY = [BEHAVIOR + '.test_admin_expiry_after_connect_zero_send',
          BEHAVIOR + '.test_egress_expiry_after_connect_zero_send']
POSITIVE = BEHAVIOR + '.test_valid_permissions_exact_request'
FILES = ['campaign_tool/records/portal/https_transport.py',
         'tests/records/test_fa1412.py', 'tools/accept_fa1412.py',
         'tests/fixtures/fa1412/support.py']


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.success_ids = []

    def addSuccess(self, test):
        self.success_ids.append(test.id())
        super().addSuccess(test)


def worker(args):
    from tests.fixtures.fa1412.support import offline_boundary, hook_identity
    root = Path(args.fixture_root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    before = hook_identity()
    with offline_boundary(root, REPO):
        module = importlib.import_module('campaign_tool.records.portal.https_transport')
        if Path(module.__file__).resolve() != REPO / FILES[0]:
            raise RuntimeError('wrong candidate module identity')
        selectors = ([*EXPIRY, POSITIVE] if args.worker == 'mutant' else
                     COMPAT if args.worker == 'compatibility' else
                     [BEHAVIOR, *COMPAT] if args.worker == 'isolation' else [BEHAVIOR])
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromNames(selectors)
        if loader.errors or suite.countTestCases() == 0:
            raise RuntimeError('missing selected tests')
        original = module.StdlibHTTPS.open
        if args.worker == 'mutant':
            tree = ast.parse((REPO / FILES[0]).read_text(), filename=module.__file__)
            matches = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef) or node.name != 'open':
                    continue
                for block in ast.walk(node):
                    body = getattr(block, 'body', None)
                    if not isinstance(body, list):
                        continue
                    for i, statement in enumerate(body):
                        if (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                                and isinstance(statement.targets[0], ast.Name)
                                and statement.targets[0].id == 'decision_time'):
                            segment = body[i:i + 3]
                            expected = ast.parse('decision_time=self.wall()\n'
                                'self.approval_loader().authorize(url,self.origin,decision_time)\n'
                                'if not self.egress_loader().allows(host,decision_time):\n'
                                '    raise PortalError("egress_denied")').body
                            if [ast.dump(x) for x in segment] != [ast.dump(x) for x in expected]:
                                raise RuntimeError('unexpected deciding predicate')
                            matches.append((body, i))
            if len(matches) != 1:
                raise RuntimeError('missing or ambiguous deciding predicate')
            body, index = matches[0]
            del body[index:index + 3]
            mutant = types.ModuleType(module.__name__)
            mutant.__package__ = module.__package__
            mutant.__file__ = module.__file__
            exec(compile(tree, module.__file__, 'exec'), mutant.__dict__)
            replacement = mutant.StdlibHTTPS.open
        else:
            replacement = original
        with patch.object(module.StdlibHTTPS, 'open', replacement):
            result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
        if module.StdlibHTTPS.open is not original:
            raise RuntimeError('mutant leaked into candidate')
        details = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                       skips=len(result.skipped), unexpected_successes=len(result.unexpectedSuccesses),
                       expected_failures=len(result.expectedFailures),
                       failure_ids=[test.id() for test, _ in result.failures],
                       failure_traces=[trace for _, trace in result.failures],
                       error_ids=[test.id() for test, _ in result.errors],
                       success_ids=result.success_ids, selectors=selectors,
                       module_path=module.__file__, module_sha256=digest(module.__file__),
                       fixture_root=str(root))
        ok = (result.wasSuccessful() and result.testsRun > 0 and not result.skipped
              and not result.expectedFailures and not result.unexpectedSuccesses)
    details['hooks_restored'] = hook_identity() == before
    save(args.worker_result, details)
    if not details['hooks_restored']:
        return 2
    return 0 if ok else 1


def run_child(folder, label, argv):
    completed = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, timeout=180)
    stdout = folder / (label + '.stdout')
    stderr = folder / (label + '.stderr')
    stdout.write_text(completed.stdout)
    stderr.write_text(completed.stderr)
    row = dict(argv=argv, cwd=str(REPO), exit=completed.returncode,
               stdout=str(stdout), stderr=str(stderr), stdout_sha256=digest(stdout),
               stderr_sha256=digest(stderr))
    save(folder / (label + '.command.json'), row)
    print(label + ': exit ' + str(completed.returncode))
    return row, completed.stdout


def test_child(folder, label, kind):
    result_path = folder / (label + '.tests.json')
    row, _ = run_child(folder, label, [sys.executable, str(Path(__file__).resolve()),
        '--worker', kind, '--fixture-root', str(folder / (label + '-fixtures')),
        '--worker-result', str(result_path)])
    result = json.loads(result_path.read_text())
    row['test_result'] = result
    row['test_result_path'] = str(result_path)
    row['test_result_sha256'] = digest(result_path)
    clean = (row['exit'] == 0 and result['tests'] > 0 and result['hooks_restored']
             and all(result[key] == 0 for key in
                     ('failures', 'errors', 'skips', 'expected_failures', 'unexpected_successes')))
    return row, clean


def acceptance(args):
    output = Path(args.output_root).resolve()
    if output == REPO or REPO in output.parents:
        raise ValueError('evidence must be outside repository')
    output.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='fa1412-', dir=output))
    receipt = dict(case=args.case, argv=sys.argv, cwd=str(Path.cwd()), evidence=str(folder),
                   source_sha256={name: digest(REPO / name) for name in FILES}, rows=[])
    ok = False
    try:
        if args.case in ('behavior', 'compatibility'):
            row, ok = test_child(folder, args.case, args.case)
            receipt['rows'].append(row)
        elif args.case == 'negative-control':
            row, _ = test_child(folder, 'disabled-presend', 'mutant')
            receipt['rows'].append(row)
            result = row['test_result']
            failed = result['failure_ids']
            mutant_detected = (row['exit'] == 1 and result['tests'] == 3 and result['hooks_restored']
                and result['errors'] == result['skips'] == result['expected_failures'] == result['unexpected_successes'] == 0
                and set(result['success_ids']) == {POSITIVE}
                and all(any(f == name or f.startswith(name + ' (') for f in failed) for name in EXPIRY)
                and result['failures'] == 4
                and all('policy refusal must send zero bytes' in trace for trace in result['failure_traces'])
                and all(any(f == name or f.startswith(name + ' (') for name in EXPIRY) for f in failed))
            restored, clean = test_child(folder, 'restored-candidate', 'behavior')
            receipt['rows'].append(restored)
            receipt['mutant_detected'] = mutant_detected
            ok = mutant_detected and clean
        elif args.case == 'isolation':
            for label in ('fresh-prefix-one', 'fresh-prefix-two'):
                row, clean = test_child(folder, label, 'isolation')
                receipt['rows'].append(row)
                if not clean:
                    raise RuntimeError('isolated suite failed')
            ok = True
        elif args.case == 'credential-scan':
            manifest = json.loads((REPO.parent / 'SOURCE-MANIFEST.json').read_text())
            runtime = manifest['runtime']
            interpreter, scanner = runtime['credential_scan_python3'], runtime['detect_secrets']
            if not all(Path(x).is_absolute() and Path(x).is_file() for x in (interpreter, scanner)):
                raise RuntimeError('operator scanner runtime unavailable')
            scan, raw = run_child(folder, 'scanner', [interpreter, scanner, 'scan', '--all-files',
                                                      '--exclude-files', '^.git/', '--no-verify'])
            receipt['rows'].append(scan)
            report = json.loads(raw)
            if scan['exit'] != 0 or not isinstance(report['results'], dict):
                raise RuntimeError('scanner failed')
            # Check exactly the real scanner stdout, not a regenerated report.
            check, text = run_child(folder, 'secret-report', [interpreter,
                     str(REPO / 'tools/check_secret_report.py'), scan['stdout']])
            receipt['rows'].append(check)
            ok = check['exit'] == 0 and 'potential secrets: 0;' in text
        elif args.case == 'public-tree':
            row, text = run_child(folder, 'public-tree', [str(REPO / '.venv/bin/python3'),
                                                        str(REPO / 'tools/check_public_tree.py')])
            receipt['rows'].append(row)
            report = json.loads(text)
            ok = row['exit'] == 0 and report['scanned_files'] > 0 and report['findings'] == []
        if receipt['source_sha256'] != {name: digest(REPO / name) for name in FILES}:
            raise RuntimeError('source changed during acceptance')
    except Exception as error:
        receipt['error'] = type(error).__name__ + ': ' + str(error)
        ok = False
    receipt['passed'] = ok
    receipt['exit'] = 0 if ok else 1
    save(folder / 'receipt.json', receipt)
    print(json.dumps({'passed': ok, 'receipt': str(folder / 'receipt.json')}))
    return receipt['exit']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=CASES)
    parser.add_argument('--output-root')
    parser.add_argument('--worker', choices=('behavior', 'compatibility', 'mutant', 'isolation'))
    parser.add_argument('--fixture-root')
    parser.add_argument('--worker-result')
    args = parser.parse_args()
    if args.worker:
        if not args.fixture_root or not args.worker_result or args.case:
            parser.error('worker needs explicit fixture and result paths')
        return worker(args)
    if not args.case or not args.output_root:
        parser.error('case and output root required')
    return acceptance(args)


if __name__ == '__main__':
    raise SystemExit(main())
