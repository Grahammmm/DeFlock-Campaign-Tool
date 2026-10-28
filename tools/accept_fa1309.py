"""Bounded FA1309 acceptance; each invocation retains fresh external evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tests.fixtures.fa1309 import support

CASES = ('behavior', 'compatibility', 'negative-control', 'isolation', 'credential-scan', 'public-tree')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=CASES, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    args = parser.parse_args()
    output_root = support.safe_directory(args.output_root)
    if output_root.is_relative_to(REPO):
        parser.error('evidence must remain outside the repository')
    output = support.fresh(output_root)
    manifest = REPO.parent / 'SOURCE-MANIFEST.json'
    identity = {'source_manifest_file_sha256': support.digest(manifest),
                'interpreter': sys.executable, 'interpreter_resolved': str(Path(sys.executable).resolve()),
                'repo': str(REPO), 'case': args.case, 'cwd': str(Path.cwd()),
                'argv': [sys.executable, *sys.argv],
                'source_hashes': {str(p.relative_to(REPO)): support.digest(p) for p in
                     [REPO / 'campaign_tool/records/extract/ocr.py', REPO / 'tests/records/test_fa1309.py',
                      REPO / 'tools/accept_fa1309.py', REPO / 'tests/fixtures/fa1309/support.py',
                      REPO / 'tests/fixtures/fa1309/child.py']}}
    head = support.record_command(['git', 'rev-parse', 'HEAD'], REPO, output / 'head')
    diff = support.record_command(['git', 'diff', '--binary', 'HEAD'], REPO, output / 'diff')
    identity['head'] = head.stdout.strip()
    identity['dirty_diff_sha256'] = hashlib.sha256(diff.stdout.encode()).hexdigest()
    (output / 'identity.json').write_text(json.dumps(identity, indent=2) + '\n')
    if head.returncode or diff.returncode:
        return 1
    if args.case in CASES[:4]:
        child, child_root = support.spawn_suite(REPO, args.case, output / 'suite')
        result_file = child_root / 'result.json'
        if not result_file.is_file():
            verdict = {'exit': 1, 'reason': 'missing_result', 'child_exit': child.returncode}
        else:
            summary = json.loads(result_file.read_text())
            valid = (summary['tests'] > 0 and summary['tests'] == summary['selected']
                     and not any(summary[k] for k in ('failures', 'errors', 'skipped',
                             'unexpected_successes', 'expected_failures', 'attempts')))
            verdict = {'exit': int(child.returncode != 0 or not valid), 'child_exit': child.returncode,
                       'summary': summary, 'result': str(result_file)}
        print(child.stdout, end='')
        print(child.stderr, end='', file=sys.stderr)
    elif args.case == 'credential-scan':
        runtime = json.loads(manifest.read_text())['runtime']
        interpreter, scanner = runtime['credential_scan_python3'], runtime['detect_secrets']
        scan = support.record_command([interpreter, '-B', scanner, 'scan', '--all-files',
                          '--exclude-files', '^.git/', '--no-verify'], REPO, output / 'scanner')
        report = output / 'scanner/stdout.txt'
        # The exact scanner stdout is the report, never synthesized or filtered.
        checker = support.record_command([interpreter, '-B', str(REPO / 'tools/check_secret_report.py'),
                          str(report)], REPO, output / 'secret-checker')
        json.loads(report.read_text())
        valid = scan.returncode == 0 and checker.returncode == 0 and 'potential secrets: 0;' in checker.stdout
        verdict = {'exit': int(not valid), 'scan_exit': scan.returncode, 'checker_exit': checker.returncode}
        print(checker.stdout, end='')
        print(checker.stderr, end='', file=sys.stderr)
    else:
        check = support.record_command([str(REPO / '.venv/bin/python3'), '-B',
                         str(REPO / 'tools/check_public_tree.py')], REPO, output / 'public-tree')
        verdict = {'exit': int(check.returncode != 0), 'checker_exit': check.returncode}
        print(check.stdout, end='')
        print(check.stderr, end='', file=sys.stderr)
    (output / 'verdict.json').write_text(json.dumps(verdict, indent=2) + '\n')
    print(json.dumps({'evidence': str(output), **verdict}, sort_keys=True))
    return verdict['exit']


if __name__ == '__main__':
    raise SystemExit(main())
