"""Five bounded offline acceptance rows for FC16-LAW-DEADLINE-RANGE.

Fresh evidence only. This is not legal acceptance, full CI or shipping authority.
All child commands use explicit argv, shell=False and checked terminal exits.
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

OWNED = ('campaign_tool/law.py', 'tests/records/test_fc16_law_deadline_range.py',
         'tests/fixtures/fc16_law_deadline_range/cases.json', 'tools/fc16_law_deadline_range_accept.py')
TEST = OWNED[1]
# Admission identities live in the supplied manifest, not duplicate scanner
# exceptions in the public tree. Its exact Git tree binds these source bytes.
PIN_PATHS = ('campaign_tool/law.py', 'tests/test_law.py',
             'campaign_tool/_resources/schemas/law-package.schema.json')
CHECKERS = ('tools/check_public_tree.py', 'campaign_tool/records/public_scan.py', 'tools/check_secret_report.py',
            'docs/records-gates-import.json', 'docs/records-intake-import.json', 'container/flock-ocr/pins.json')


def require(value, reason):
    if not value:
        raise ValueError(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


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
        self.report = {'schema': 'fc16-law-deadline-range-accept/v1', 'phase': args.phase,
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
        require(m['card'] == 'deflock-FC16-LAW-DEADLINE-RANGE', 'source_manifest_card')
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
        for relative in CHECKERS:
            require(self.command('checker-base', ['git', 'show', self.base + ':' + relative]) == (self.repo / relative).read_bytes(), 'checker_changed')
        runtime = m['runtime']
        link = self.repo / '.venv'
        require(link.is_symlink() and link.resolve() == Path(runtime['repo_venv']), 'runtime_link')
        expected_python = runtime['credential_scan_python3'] if self.args.phase == 'credentials' else self.py
        require(Path(sys.executable).absolute() == Path(expected_python).absolute(), 'interpreter_path')
        for key in ('credential_scan_python3', 'detect_secrets'):
            require(Path(runtime[key]).is_file() and Path(runtime[key]).parent == Path(runtime['repo_venv']) / 'bin', 'runtime_path')
        self.clean()
        self.report['initial'] = self.identity()

    def tests(self, mode, cwd=None):
        root = self.out / ('temporary-' + mode)
        root.mkdir()
        target = self.out / ('tests-' + mode + '.json')
        argv = [self.py, '-B', str(self.repo / TEST), '--mode', mode,
                '--temporary-root', str(root), '--report', str(target)]
        self.command(mode, argv, cwd=cwd)
        report = json.loads(target.read_text())
        require(report['success'] and not report['skips'] and not report['errors'] and not report['failures'], 'test_report')
        expected = {'suite': 7, 'compatibility': 7, 'discovery': 9}[mode]
        require(report['tests'] == expected, 'test_count')
        if mode != 'compatibility':
            subcases = report['subcases']
            fixture = json.loads((self.repo / OWNED[2]).read_text())
            for row in fixture['rows']:
                matching = [s for s in subcases if "fixture_id='" + row['id'] + "'" in s['id']]
                require(len(matching) == 1 and matching[0]['outcome'] == 'pass', 'missing_frozen_subcase')
            require(len(subcases) == 340, 'subcase_count')
        self.report.setdefault('test_reports', {})[mode] = {'path': str(target), 'sha256': digest(target.read_bytes()), **report}

    def portable(self):
        cwd = self.out / 'unrelated-cwd'
        cwd.mkdir()
        self.tests('suite', cwd=cwd)
        self.tests('discovery', cwd=cwd)
        findings = []
        for relative in OWNED[1:]:
            text = (self.repo / relative).read_text()
            if re.search(r'/(?:home|Users|workspace)/', text):
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
            if phase in ('suite', 'compatibility'):
                self.tests(phase)
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
        files = {str(p.relative_to(self.out)): digest(p.read_bytes()) for p in self.out.iterdir() if p.is_file()}
        self.report['raw_files_sha256'] = files
        path = self.out / 'receipt.json'
        path.write_text(json.dumps(self.report, indent=2) + '\n')
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
