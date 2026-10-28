"""Small offline FA1306 acceptance driver; receipts are external and per-invocation."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tests.fixtures.fa1306 import support as s

COMPATIBILITY = (
    'tests.records.test_mail_resolution_audit_closure.AuditClosureTests.test_append_failure_is_not_committable_and_outer_work_survives',
    'tests.records.test_original_resolution_mail.OriginalResolutionMailTests.test_transaction_required_and_gate_cannot_be_reused',
    'tests.records.test_original_resolution_mail.OriginalResolutionMailTests.test_rejected_missing_policy_original_and_missing_occurrence',
    'tests.records.test_original_resolution_mail.OriginalResolutionMailTests.test_append_negative_eligibility_checks_and_rollback',
    'tests.records.test_t4b_resolution_adversarial.TornAndCrashTests.test_failure_after_insert_does_not_leave_a_row_in_the_callers_transaction',
)
FILES = ('campaign_tool/records/ledger/original_resolution_mail.py', 'tests/records/test_fa1306.py',
         'tools/accept_fa1306.py', 'tests/fixtures/fa1306/support.py')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(root, name, value):
    (root / name).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def run_command(root, label, argv):
    proc = subprocess.run(argv, cwd=REPO, capture_output=True, timeout=180)
    (root / (label + '.stdout')).write_bytes(proc.stdout)
    (root / (label + '.stderr')).write_bytes(proc.stderr)
    record = dict(argv=argv, cwd=str(REPO), exit=proc.returncode,
                  stdout_sha256=sha(proc.stdout), stderr_sha256=sha(proc.stderr))
    save(root, label + '.json', record)
    return proc, record


def suite(root, selectors):
    selected = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in selectors)
    count = selected.countTestCases()
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(selected)
    raw = stream.getvalue()
    (root / 'tests.stderr').write_text(raw)
    sys.stderr.write(raw)
    modules = {name: str(Path(module.__file__).resolve()) for name, module in tuple(sys.modules.items())
               if name.startswith('campaign_tool') and getattr(module, '__file__', None)}
    origins_ok = all(Path(path).is_relative_to(REPO) for path in modules.values())
    record = dict(tests=result.testsRun, selected=count, modules=modules, origins_ok=origins_ok,
                  failures=[t.id() for t, _ in result.failures], errors=[t.id() for t, _ in result.errors],
                  skips=[t.id() for t, _ in result.skipped],
                  output_sha256=sha(raw.encode()), attempts=list(s.ATTEMPTS))
    save(root, 'result.json', record)
    return 0 if origins_ok and count > 0 and result.testsRun == count and result.wasSuccessful() and not result.skipped and not s.ATTEMPTS else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--case', choices=('behavior', 'compatibility', 'negative-control', 'isolation', 'credential-scan', 'public-tree'))
    group.add_argument('--child', help=argparse.SUPPRESS)
    parser.add_argument('--output-root', required=True)
    args = parser.parse_args()
    out = s.checked_directory(args.output_root)
    if args.child:
        s.bootstrap(out)
        if args.child == 'probe':
            from campaign_tool.records.ledger import original_resolution_mail as mail
            save(out, 'probe.json', dict(module=mail.__file__, attempts=s.ATTEMPTS,
                                        temp=tempfile.gettempdir(), bytecode_disabled=sys.dont_write_bytecode))
            return 0
        if args.child == 'behavior':
            selectors = ('tests.records.test_fa1306.Behavior',)
        elif args.child == 'compatibility':
            selectors = COMPATIBILITY
        elif args.child.startswith('tests.records.test_fa1306.Behavior.test_'):
            selectors = (args.child,)
        else:
            parser.error('unknown child selector')
        return suite(out, selectors)

    out.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='run-', dir=out))
    s.RUN = root
    temp = root / 'temporary'
    temp.mkdir()
    tempfile.tempdir = str(temp)
    os.environ['TMPDIR'] = str(temp)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO).decode().strip()
    diff = subprocess.check_output(['git', 'diff', '--binary', 'HEAD'], cwd=REPO)
    (root / 'dirty.patch').write_bytes(diff)
    identity = dict(head=head, dirty_diff_sha256=sha(diff),
                    files={name: sha((REPO / name).read_bytes()) for name in FILES},
                    executable=sys.executable, real_executable=str(Path(sys.executable).resolve()),
                    argv=sys.argv, cwd=str(Path.cwd()), repo=str(REPO))
    manifest = REPO.parent / 'SOURCE-MANIFEST.json'
    identity['source_manifest_sha256'] = sha(manifest.read_bytes())
    save(root, 'identity.json', identity)
    if args.case in ('behavior', 'compatibility'):
        argv = [sys.executable, '-B', str(Path(__file__).resolve()), '--child', args.case,
                '--output-root', str(root / 'child')]
        proc, record = run_command(root, 'child', argv)
        result = json.loads((root / 'child' / 'result.json').read_text())
        ok = proc.returncode == 0 and result['tests'] > 0 and not any(result[k] for k in ('errors', 'failures', 'skips', 'attempts'))
        save(root, 'acceptance.json', dict(case=args.case, command=record, result=result, passed=ok))
    elif args.case in ('negative-control', 'isolation'):
        label = 'GateControl' if args.case == 'negative-control' else 'Isolation'
        code = suite(root, ('tests.records.test_fa1306.' + label,))
        ok = code == 0
        save(root, 'acceptance.json', dict(case=args.case, passed=ok,
                                         result=json.loads((root / 'result.json').read_text())))
    elif args.case == 'credential-scan':
        runtime = json.loads(manifest.read_text())['runtime']
        scan_python = runtime['credential_scan_python3']
        scan, first = run_command(root, 'scan', [scan_python, runtime['detect_secrets'], 'scan', '--all-files',
                                                '--exclude-files', '^.git/', '--no-verify'])
        # This is the scanner's actual stdout, not a reconstructed report.
        report = root / 'scan.stdout'
        json.loads(scan.stdout)
        check, second = run_command(root, 'secret-check', [scan_python, str(REPO / 'tools/check_secret_report.py'), str(report)])
        ok = scan.returncode == check.returncode == 0 and b'potential secrets: 0;' in check.stdout
        save(root, 'acceptance.json', dict(case=args.case, passed=ok, commands=[first, second]))
    else:
        proc, record = run_command(root, 'public-tree', [str(REPO / '.venv/bin/python3'), str(REPO / 'tools/check_public_tree.py')])
        value = json.loads(proc.stdout)
        ok = proc.returncode == 0 and value['findings'] == [] and value['scanned_files'] > 0
        save(root, 'acceptance.json', dict(case=args.case, passed=ok, command=record, result=value))
    print(json.dumps(dict(case=args.case, passed=ok, output=str(root))))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
