"""Five bounded offline acceptance rows for FC16-LAW-SUMMARY-SNAPSHOT.

Fresh evidence only. This is not legal acceptance, full CI or shipping authority.
All child commands use explicit argv, shell=False and checked terminal exits.
Machine pass is PASS_REVIEW_REQUIRED, never semantic or legal acceptance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import traceback
import uuid

OWNED = ('campaign_tool/law.py', 'tests/records/test_fc16_law_summary_snapshot.py',
         'tests/fixtures/fc16_law_summary_snapshot/cases.json', 'tools/fc16_law_summary_snapshot_accept.py')
TEST = OWNED[1]
FIXTURE = OWNED[2]
# Admission identities live in the supplied manifest, not duplicate scanner
# exceptions in the public tree. Its exact Git tree binds these source bytes.
PIN_PATHS = ('campaign_tool/law.py', 'tests/test_law.py',
             'campaign_tool/_resources/jurisdictions/us-ca/package.json')
CHECKERS = ('tools/check_public_tree.py', 'campaign_tool/records/public_scan.py', 'tools/check_secret_report.py',
            'docs/records-gates-import.json', 'docs/records-intake-import.json', 'container/flock-ocr/pins.json')
SUITE_TESTS = ('test_deciding_control', 'test_empty_and_order', 'test_fixture_case_map',
               'test_projection', 'test_repeat', 'test_two_way')
BASELINE_RED = ('test_repeat', 'test_two_way')
BASELINE_GREEN = ('test_empty_and_order', 'test_projection')
COMPAT_COUNT = 7


def require(value, reason):
    if not value:
        raise ValueError(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def short(test_id):
    return test_id.rsplit('.', 1)[-1]


class Recorder:
    def __init__(self, args):
        self.args = args
        self.repo = args.repo.resolve(strict=True)
        require(self.repo == Path(__file__).resolve().parents[1], 'recorder_repo_mismatch')
        self.manifest_path = args.manifest.resolve(strict=True)
        self.manifest_raw = self.manifest_path.read_bytes()
        self.manifest = json.loads(self.manifest_raw)
        self.base = self.manifest['head']
        self.base_tree = self.manifest['tree']
        require(all(isinstance(pin, str) and re.fullmatch(r'[0-9a-f]{40}', pin)
                    for pin in (self.base, self.base_tree)), 'source_pin_format')
        self.out_base = args.out_base.resolve()
        require(self.repo != self.out_base and self.repo not in self.out_base.parents, 'evidence_inside_repo')
        require(self.out_base == Path(self.manifest['write_root']) / 'out', 'output_root_mismatch')
        self.out = self.out_base / (args.phase + '-' + uuid.uuid4().hex)
        self.out.mkdir(parents=True, exist_ok=False)
        self.rows = []
        self.py = str(self.repo / '.venv/bin/python3')
        self.report = {'schema': 'fc16-law-summary-snapshot-accept/v1', 'phase': args.phase,
                       'argv': sys.argv, 'cwd': str(Path.cwd()), 'interpreter': sys.executable,
                       'manifest_path': str(self.manifest_path), 'manifest_sha256': digest(self.manifest_raw),
                       'children': self.rows, 'status': 'INCOMPLETE'}

    def command(self, label, argv, cwd=None, allowed=(0,)):
        cwd = self.repo if cwd is None else cwd
        number = len(self.rows)
        row = {'label': label, 'argv': list(argv), 'cwd': str(cwd), 'shell': False}
        self.rows.append(row)
        try:
            run = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=180, shell=False)
            row['exit'] = run.returncode
            stdout, stderr = run.stdout, run.stderr
        except subprocess.TimeoutExpired as exc:
            row['exit'] = 'TIMEOUT'
            stdout, stderr = exc.stdout or b'', exc.stderr or b''
        for stream, raw in (('stdout', stdout), ('stderr', stderr)):
            path = self.out / f'{number:02d}-{label}-{stream}.txt'
            path.write_bytes(raw)
            row[stream] = {'path': str(path), 'sha256': digest(raw), 'bytes': len(raw)}
        require(row['exit'] in allowed, 'child_exit_' + label)
        return stdout

    def git(self, *args, cwd=None):
        return self.command('git-' + args[0], ['git', *args], cwd=cwd).decode().strip()

    def identity(self):
        return {'head': self.git('rev-parse', 'HEAD'), 'tree': self.git('rev-parse', 'HEAD^{tree}'),
                'owned_sha256': {p: digest((self.repo / p).read_bytes()) for p in OWNED},
                'checker_allowlist_sha256': {p: digest((self.repo / p).read_bytes()) for p in CHECKERS}}

    def clean(self):
        status = self.git('status', '--porcelain=v1', '--untracked-files=all')
        require(not status or status == '?? .venv', 'source_dirty')
        require(not self.git('diff', '--name-only'), 'worktree_diff')
        require(not self.git('diff', '--cached', '--name-only'), 'index_diff')
        inventory = self.git('ls-files', '--cached', '--others', '--exclude-standard')
        self.report['path_inventory'] = inventory.splitlines()
        untracked = self.git('ls-files', '--others', '--exclude-standard').splitlines()
        require(set(untracked) <= {'.venv'}, 'untracked_scan_visible')
        for relative in OWNED:
            committed = self.command('committed-file', ['git', 'show', 'HEAD:' + relative])
            indexed = self.command('indexed-file', ['git', 'show', ':' + relative])
            require(committed == indexed == (self.repo / relative).read_bytes(), 'owned_bytes_mismatch')

    def preflight(self):
        m = self.manifest
        require(m['schema'] == 'agentorange.source-manifest/v1', 'manifest_schema')
        require(Path(m['repo']) == self.repo and Path(m['write_root']) == self.repo.parent, 'manifest_roots')
        require(m['card'] == 'deflock-FC16-LAW-SUMMARY-SNAPSHOT', 'source_manifest_card')
        require(self.git('rev-parse', self.base + '^{tree}') == self.base_tree, 'source_tree_pin')
        self.command('base-ancestry', ['git', 'merge-base', '--is-ancestor', self.base, 'HEAD'])
        self.report['source_pins'] = {}
        for relative in PIN_PATHS:
            sha = digest(self.command('source-pin', ['git', 'show', self.base + ':' + relative]))
            self.report['source_pins'][relative] = sha
            if relative != OWNED[0]:
                require(digest((self.repo / relative).read_bytes()) == sha, 'unchanged_source_pin')
        changed = set(self.git('diff', '--name-only', self.base, 'HEAD').splitlines())
        require(changed == set(OWNED), 'scope_diff')
        self.command('complete-diff', ['git', 'diff', '--binary', self.base, 'HEAD'])
        law_diff = self.git('diff', '--unified=0', self.base, 'HEAD', '--', OWNED[0])
        hunks = [line for line in law_diff.splitlines() if line[:1] in '+-' and line[:3] not in ('+++', '---')]
        require(hunks == ['-        "reviewed_by": package["reviewed_by"],',
                          '+        "reviewed_by": list(package["reviewed_by"]),'], 'product_hunk_scope')
        self.report['product_hunk'] = hunks
        for relative in CHECKERS:
            require(self.command('checker-base', ['git', 'show', self.base + ':' + relative]) == (self.repo / relative).read_bytes(), 'checker_changed')
        fixture = json.loads((self.repo / FIXTURE).read_text(encoding='utf-8'))
        require(self.base.startswith(fixture['provenance']['source_commit_prefix']) and
                self.base_tree.startswith(fixture['provenance']['source_tree_prefix']), 'fixture_provenance')
        runtime = m['runtime']
        link = self.repo / '.venv'
        require(link.is_symlink() and link.resolve() == Path(runtime['repo_venv']), 'runtime_link')
        expected_python = runtime['credential_scan_python3'] if self.args.phase == 'credentials' else self.py
        require(Path(sys.executable).absolute() == Path(expected_python).absolute(), 'interpreter_path')
        for key in ('credential_scan_python3', 'detect_secrets'):
            require(Path(runtime[key]).is_file() and Path(runtime[key]).parent == Path(runtime['repo_venv']) / 'bin', 'runtime_path')
        self.clean()
        self.report['initial'] = self.identity()

    def base_clone(self):
        """Exact base-commit clone in this row, for original/baseline evidence only."""
        clone = self.out / 'base-clone'
        self.command('base-clone', ['git', 'clone', '--local', '--no-hardlinks', '--no-checkout',
                                    '--no-recurse-submodules', str(self.repo), str(clone)])
        self.command('base-checkout', ['git', 'checkout', '--detach', self.base], cwd=clone)
        require(self.git('rev-parse', 'HEAD', cwd=clone) == self.base, 'base_clone_head')
        require(self.git('rev-parse', 'HEAD^{tree}', cwd=clone) == self.base_tree, 'base_clone_tree')
        require(not (clone / '.venv').exists() and not (clone / '.venv').is_symlink(), 'base_clone_runtime_link')
        for relative, sha in self.report['source_pins'].items():
            require(digest((clone / relative).read_bytes()) == sha, 'base_clone_pin')
        return clone.resolve()

    def run_module(self, mode, cwd=None, extra=(), allowed=(0,)):
        root = self.out / ('temporary-' + mode)
        root.mkdir()
        target = self.out / ('tests-' + mode + '.json')
        argv = [self.py, '-B', str(self.repo / TEST), '--mode', mode,
                '--temporary-root', str(root), '--report', str(target), *extra]
        self.command(mode, argv, cwd=cwd, allowed=allowed)
        report = json.loads(target.read_text(encoding='utf-8'))
        require(not report['skips'] and not report['errors'], 'test_report_skips_or_errors')
        self.report.setdefault('test_reports', {})[mode] = {'path': str(target), 'sha256': digest(target.read_bytes()), **report}
        return report

    def check_case_map(self, report):
        case_map = json.loads((self.repo / FIXTURE).read_text(encoding='utf-8'))['case_map']
        seen = [(s['params'].get('case'), s['params'].get('subcase'), s['outcome']) for s in report['subcases']]
        expected = [(case, sub) for case, subs in case_map.items() for sub in subs]
        require(len(seen) == len(expected) == 23, 'subcase_count')
        require(sorted((c, s) for c, s, _ in seen) == sorted(expected), 'subcase_map')
        require(all(outcome == 'pass' for _, _, outcome in seen), 'subcase_outcome')
        self.report['case_to_subcase_map'] = {
            case: {sub: 'tests.records.test_fc16_law_summary_snapshot.LawSummarySnapshotTests.test_' + case
                   for sub in subs} for case, subs in case_map.items()}

    def suite(self):
        clone = self.base_clone()
        law_path = clone / 'campaign_tool/law.py'
        baseline = self.run_module('baseline', extra=('--law-path', str(law_path)), allowed=(1,))
        require(baseline['law_path'] == str(law_path), 'baseline_law_path')
        require(baseline['tests'] == 4 and not baseline['success'], 'baseline_shape')
        outcomes = {short(k): v for k, v in baseline['outcomes'].items()}
        require(outcomes == {**{t: 'fail' for t in BASELINE_RED}, **{t: 'pass' for t in BASELINE_GREEN}}, 'baseline_outcomes')
        failed = [s for s in baseline['subcases'] if s['outcome'] == 'fail']
        expected_failed = {('two_way', n) for n in ('caller_append', 'caller_replace', 'caller_remove', 'summary_append',
                                                    'summary_replace', 'summary_remove', 'empty_caller_append',
                                                    'empty_summary_append')}
        expected_failed |= {('repeat', n) for n in ('mutate_first', 'mutate_second', 'fresh_after')}
        require({(s['params']['case'], s['params']['subcase']) for s in failed} == expected_failed
                and len(failed) == len(expected_failed), 'baseline_failed_subcases')
        for s in failed:
            require(re.search(r'(two_way_\w+|repeat_\w+)_leak', s['message']) and
                    s['params']['subcase'].replace('empty_', '') in s['message'], 'baseline_failure_reason')
        passed = {(s['params']['case'], s['params']['subcase']) for s in baseline['subcases'] if s['outcome'] == 'pass'}
        require(passed == {('empty_and_order', 'empty_draft'), ('empty_and_order', 'ordered_overlay'),
                           ('projection', 'declared_package'), ('projection', 'overlay_package')}, 'baseline_equality_controls')
        self.report['baseline_red'] = {'law_path': str(law_path), 'base': self.base,
                                       'expected_failures': sorted('/'.join(x) for x in expected_failed),
                                       'passing_equality_controls': sorted('/'.join(x) for x in passed),
                                       'note': 'expected red on untouched base; not final acceptance'}
        report = self.run_module('suite')
        require(report['success'] and report['tests'] == len(SUITE_TESTS), 'suite_report')
        require(sorted(short(i) for i in report['ids']) == list(SUITE_TESTS), 'suite_ids')
        self.check_case_map(report)

    def compatibility(self):
        clone = self.base_clone()
        original = self.run_module('compatibility', extra=('--compat-root', str(clone)))
        self.report['test_reports']['compatibility-original'] = self.report['test_reports'].pop('compatibility')
        final_root = self.out / 'final'
        final_root.mkdir()
        target = final_root / 'tests-compatibility.json'
        temp = final_root / 'temporary-compatibility'
        temp.mkdir()
        self.command('compatibility-final', [self.py, '-B', str(self.repo / TEST), '--mode', 'compatibility',
                                             '--temporary-root', str(temp), '--report', str(target)])
        final = json.loads(target.read_text(encoding='utf-8'))
        self.report['test_reports']['compatibility-final'] = {'path': str(target), 'sha256': digest(target.read_bytes()), **final}
        for report in (original, final):
            require(report['success'] and report['tests'] == COMPAT_COUNT and not report['skips'], 'compatibility_report')
        require(original['ids'] == final['ids'], 'compatibility_ids')
        deltas = {i: (original['outcomes'][i], final['outcomes'][i]) for i in final['ids']
                  if original['outcomes'][i] != final['outcomes'][i]}
        require(not deltas, 'compatibility_deltas')
        self.report['compatibility_deltas'] = deltas

    def portable(self):
        cwd = self.out / 'unrelated-cwd'
        cwd.mkdir()
        report = self.run_module('suite', cwd=cwd)
        require(report['success'] and report['tests'] == len(SUITE_TESTS), 'portable_suite')
        self.check_case_map(report)
        report = self.run_module('discovery', cwd=cwd)
        require(report['success'] and report['tests'] == len(SUITE_TESTS) + 2, 'portable_discovery')
        require(short(report['ids'][0]) == 'runTest' and short(report['ids'][-1]) == 'runTest', 'peer_sentinels')
        self.check_case_map(report)
        findings = []
        for relative in OWNED[1:]:
            text = (self.repo / relative).read_text(encoding='utf-8')
            if re.search(r'/(?:home|Users|workspace|tmp)/', text):
                findings.append(relative)
        require(not findings, 'machine_specific_path')
        self.report['machine_path_findings'] = findings

    def credentials(self):
        runtime = self.manifest['runtime']
        raw = self.command('detect-secrets', [runtime['detect_secrets'], 'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify'])
        parsed = json.loads(raw)
        require(isinstance(parsed['results'], dict), 'scanner_json')
        path = self.out / 'detect-secrets.json'
        path.write_bytes(raw)
        self.command('check-secret-report', [runtime['credential_scan_python3'], '-B', str(self.repo / 'tools/check_secret_report.py'), str(path)])
        self.report['scanner_json_sha256'] = digest(raw)
        self.report['owned_scanner_results'] = {p: parsed['results'].get(p, []) for p in OWNED}

    def public_tree(self):
        clone = self.out / 'committed-tree'
        self.command('clone', ['git', 'clone', '--local', '--no-hardlinks', '--no-checkout', '--no-recurse-submodules', str(self.repo), str(clone)])
        self.command('checkout', ['git', 'checkout', '--detach', self.report['initial']['head']], cwd=clone)
        require(self.git('rev-parse', 'HEAD', cwd=clone) == self.report['initial']['head'], 'clone_head')
        require(self.git('rev-parse', 'HEAD^{tree}', cwd=clone) == self.report['initial']['tree'], 'clone_tree')
        require(not (clone / '.venv').exists() and not (clone / '.venv').is_symlink(), 'clone_runtime_link')
        for relative, sha in self.report['initial']['owned_sha256'].items():
            require(digest((clone / relative).read_bytes()) == sha, 'clone_owned_bytes')
        raw = self.command('committed-public-tree', [self.py, '-B', str(clone / 'tools/check_public_tree.py')], cwd=clone)
        result = json.loads(raw)
        require(result['findings'] == [] and result['scanned_files'] > 0, 'committed_tree_findings')
        raw = self.command('in-place-informational', [self.py, '-B', str(self.repo / 'tools/check_public_tree.py')], allowed=(0, 1))
        result = json.loads(raw)
        code = self.rows[-1]['exit']
        require((code == 0 and result['findings'] == []) or
                (code == 1 and result['findings'] == [['.venv', 0, 'symlink_not_scanned']]), 'in_place_findings')
        self.report['in_place_informational'] = {'exit': code, 'report': result, 'authority': 'INC-174; not a committed-tree result'}
        self.clean()

    def run(self):
        try:
            self.preflight()
            phase = self.args.phase
            if phase == 'suite':
                self.suite()
            elif phase == 'compatibility':
                self.compatibility()
            elif phase == 'portable':
                self.portable()
            elif phase == 'credentials':
                self.credentials()
            else:
                self.public_tree()
            self.report['final'] = self.identity()
            require(self.report['final'] == self.report['initial'], 'identity_changed')
            require(self.manifest_path.read_bytes() == self.manifest_raw, 'manifest_changed')
            self.clean()
            self.report['status'] = 'PASS_REVIEW_REQUIRED'
            code = 0
        except Exception:
            self.report['status'] = 'FAIL'
            self.report['error'] = traceback.format_exc()
            code = 1
        # All subprocess writers exited before hashing the complete retained files.
        files = {str(p.relative_to(self.out)): digest(p.read_bytes()) for p in sorted(self.out.rglob('*'))
                 if p.is_file() and not any(part in ('base-clone', 'committed-tree') for part in p.relative_to(self.out).parts)}
        self.report['raw_files_sha256'] = files
        path = self.out / 'receipt.json'
        path.write_text(json.dumps(self.report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'phase': self.args.phase, 'exit': code, 'status': self.report['status'],
                          'receipt': str(path), 'receipt_sha256': digest(path.read_bytes()),
                          'error': self.report.get('error')}))
        return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', required=True, choices=('suite', 'compatibility', 'portable', 'credentials', 'public-tree'))
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--out-base', required=True, type=Path)
    return Recorder(parser.parse_args()).run()


if __name__ == '__main__':
    raise SystemExit(main())
