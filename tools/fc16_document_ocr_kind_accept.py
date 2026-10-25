"""Five bounded offline acceptance rows, with immutable per-invocation evidence."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

BASE = ''.join(('32eda30545fd0e5a', '1d83723939362d50', '8ead8361'))
TREE = ''.join(('a9a693454f', '4db42b3b32', '35f40e3228', '46efa27dcc'))
PINS = {
    'campaign_tool/records/understanding/document.py': ''.join(('f3a48098d27ad710', '61781d2eaacfde80', '3f3f1be108f2a4fc', 'e3085f12dacd9a27')),
    'tests/records/test_fc_doc_coverage.py': ''.join(('ceadbfa869eca626', '4a04077cf098260f', '5c2a8fb7dde9142c', '668052e8fb21a257')),
}
OWNED = ['campaign_tool/records/understanding/document.py',
         'tests/records/test_fc16_document_ocr_kind.py',
         'tests/fixtures/fc16_document_ocr_kind/cases.json',
         'tools/fc16_document_ocr_kind_accept.py']
CHECKERS = ['tools/check_secret_report.py', 'tools/check_public_tree.py',
            'campaign_tool/records/public_scan.py']


def need(ok, message):
    if not ok:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


class Row:
    def __init__(self, args):
        self.repo = args.repo.absolute()
        self.manifest = args.manifest.absolute()
        self.args = args
        self.steps = []
        self.details = {}
        out = args.out_base.absolute()
        need(self.repo == Path(__file__).resolve().parents[1], 'repo must be this recorder repository')
        need(out != self.repo and self.repo not in out.parents, 'evidence must be outside repo')
        need(out == self.repo.parent / 'out', 'out must be admitted candidate out root')
        out.mkdir(exist_ok=True)
        self.out = out / (args.phase + '-' + str(uuid.uuid4()))
        self.out.mkdir()

    def run(self, label, argv, cwd=None, exits=(0,)):
        cwd = self.repo if cwd is None else cwd
        argv = [str(v) for v in argv]
        p = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           shell=False, timeout=180)
        stdout = self.out / (label + '.stdout')
        stderr = self.out / (label + '.stderr')
        stdout.write_bytes(p.stdout)
        stderr.write_bytes(p.stderr)
        self.steps.append({'label': label, 'argv': argv, 'cwd': str(cwd), 'exit': p.returncode,
                           'stdout': str(stdout), 'stdout_sha256': digest(p.stdout),
                           'stderr': str(stderr), 'stderr_sha256': digest(p.stderr)})
        need(p.returncode in exits, label + ' unexpected exit ' + str(p.returncode))
        return p.stdout

    def git(self, label, *argv):
        return self.run(label, ['git', *argv])

    def identity(self, label):
        return self.git(label, 'rev-parse', 'HEAD', 'HEAD^{tree}').decode().splitlines()

    def inventory(self, label):
        return self.git(label, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').decode().split('\0')

    def preflight(self):
        need(not self.manifest.is_symlink(), 'manifest symlink')
        raw = self.manifest.read_bytes()
        self.m = json.loads(raw)
        need(self.m['schema'] == 'agentorange.source-manifest/v1', 'manifest schema')
        need(Path(self.m['repo']) == self.repo and Path(self.m['write_root']) == self.repo.parent,
             'manifest repo/root')
        need(self.m['head'] == BASE and self.m['tree'] == TREE, 'admitted base identity')
        need(self.git('base-tree', 'rev-parse', BASE + '^{tree}').decode().strip() == TREE, 'base tree')
        self.initial = self.identity('initial-identity')
        self.git('descendant', 'merge-base', '--is-ancestor', BASE, self.initial[0])
        for path, expected in PINS.items():
            need(digest(self.git('pin-' + Path(path).stem, 'show', BASE + ':' + path)) == expected, 'source pin ' + path)
            if self.initial[0] == BASE:
                need(digest((self.repo / path).read_bytes()) == expected, 'untouched baseline ' + path)
        self.python = self.repo / '.venv/bin/python3'
        rt = self.m['runtime']
        need((self.repo / '.venv').is_symlink(), 'standard venv link required')
        need((self.repo / '.venv').resolve() == Path(rt['repo_venv']).resolve(), 'venv target')
        need(self.python.is_file(), 'staged interpreter missing')
        need(Path(rt['credential_scan_python3']).is_file() and Path(rt['detect_secrets']).is_file(), 'scan runtime missing')
        expected_python = Path(rt['credential_scan_python3']) if self.args.phase == 'credentials' else self.python
        need(os.path.abspath(sys.executable) == str(expected_python), 'literal interpreter route mismatch')
        self.details['manifest_sha256'] = digest(raw)
        self.details['initial_manifest'] = self.m
        self.details['source_hashes'] = {p: digest((self.repo / p).read_bytes()) for p in OWNED + CHECKERS}
        for path in CHECKERS:
            need((self.repo / path).read_bytes() == self.git('checker-' + Path(path).stem, 'show', BASE + ':' + path), 'checker changed')
        names = [p for p in self.inventory('inventory-before') if p]
        for name in names:
            p = self.repo / name
            need(not p.is_symlink() or name == '.venv', 'unexpected scan-visible symlink')
        self.baseline = self.initial[0] == BASE
        if not self.baseline:
            self.clean('status-before')
            changed = set(self.git('changed-paths', 'diff', '--name-only', BASE, self.initial[0]).decode().splitlines())
            need(changed == set(OWNED), 'final diff must own exactly four declared paths')
            for p in OWNED:
                need((self.repo / p).read_bytes() == self.git('head-' + Path(p).stem, 'show', 'HEAD:' + p), 'worktree differs from HEAD')
                need((self.repo / p).read_bytes() == self.git('index-' + Path(p).stem, 'show', ':' + p), 'index differs from HEAD')
        self.git('complete-diff', 'diff', '--binary', BASE, self.initial[0])

    def clean(self, label):
        raw = self.git(label, 'status', '--porcelain=v1', '--untracked-files=all').decode()
        need(not raw.strip(), 'final tree must be clean')
        tracked = set(self.git(label + '-tracked', 'ls-files', '-z').decode().split('\0'))
        names = set(self.inventory(label + '-visible')) - {''}
        need(names - tracked <= {'.venv'}, 'untracked scan-visible entry')

    def tests(self, label, mode, cwd=None):
        test = self.repo / OWNED[1]
        report = self.out / (label + '.tests.json')
        argv = [self.python, '-B', test, '--mode', mode, '--temp-root', self.out / (label + '-tmp'), '--report', report]
        expected = (1,) if mode == 'baseline' else (0,)
        self.run(label, argv, cwd=cwd, exits=expected)
        data = json.loads(report.read_text())
        need(data['skips'] == 0 and not data['errors'], 'test errors/skips')
        if mode == 'baseline':
            need(data['count'] == 2 and len(data['failures']) == 4, 'precise baseline failures required')
            need(all('test_kind_only' in t and 'ocr-global-warning' in msg for t, msg in data['failures']), 'baseline failed wrong gate')
            need(not any('test_explicit_control' in t for t, _ in data['failures']), 'ordinary control must pass')
        else:
            need(data['success'] and not data['failures'], 'tests incomplete')
            if mode in ('matrix', 'discovery'):
                tree = ast.parse(test.read_text())
                methods = {n.name for c in tree.body if isinstance(c, ast.ClassDef) and c.name == 'OcrKindTests'
                           for n in c.body if isinstance(n, ast.FunctionDef) and n.name.startswith('test_')}
                seen = {v.rsplit('.', 1)[-1] for v in data['tests']}
                need(methods <= seen and data['count'] == len(methods) + (2 if mode == 'discovery' else 0), 'matrix incomplete')
                fixture = json.loads((self.repo / OWNED[2]).read_text())
                for c in fixture['cases'] + fixture['predicate_cases']:
                    need(any("case='" + c['id'] + "'" in s['id'] and s['passed'] for s in data['subcases']), 'missing case ' + c['id'])
        self.details[label] = data

    def execute(self):
        phase = self.args.phase
        if phase == 'suite':
            self.tests('baseline' if self.baseline else 'matrix', 'baseline' if self.baseline else 'matrix')
        elif phase == 'compatibility':
            self.tests('compatibility', 'compatibility')
        elif phase == 'portable':
            need(not self.baseline, 'portable requires final committed candidate')
            cwd = self.out / 'unrelated-cwd'
            cwd.mkdir()
            self.tests('absolute-script', 'matrix', cwd)
            self.tests('discovery-with-peers', 'discovery', cwd)
            for path in OWNED[1:]:
                raw = (self.repo / path).read_text()
                prefixes = tuple('/' + v + '/' for v in ('home', 'Users', 'workspace'))
                need(not any(v in raw for v in prefixes), 'machine-specific path ' + path)
        elif phase == 'credentials':
            need(not self.baseline, 'credentials requires final candidate')
            # os.walk does not follow directory links. Refuse any file links;
            # only the manifest-bound directory .venv link is admitted.
            for root, dirs, files in os.walk(self.repo, followlinks=False):
                if Path(root) == self.repo and '.git' in dirs:
                    dirs.remove('.git')
                for name in dirs + files:
                    p = Path(root) / name
                    need(not p.is_symlink() or p == self.repo / '.venv', 'scanner would encounter nonstandard symlink')
            rt = self.m['runtime']
            output = self.run('detect-secrets', [rt['detect_secrets'], 'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify'])
            report = self.out / 'scanner.json'
            report.write_bytes(output)
            parsed = json.loads(output)
            need(isinstance(parsed['results'], dict), 'invalid scanner JSON')
            self.run('check-secret-report', [rt['credential_scan_python3'], self.repo / 'tools/check_secret_report.py', report])
            self.details['scanner_hits'] = sum(len(v) for v in parsed['results'].values())
        elif phase == 'public-tree':
            need(not self.baseline, 'public-tree requires final candidate')
            informational = self.run('in-place', [self.python, self.repo / 'tools/check_public_tree.py'], exits=(0, 1))
            info = json.loads(informational)
            need(info.get('findings') in ([], [['.venv', 0, 'symlink_not_scanned']]), 'unexpected in-place findings')
            need(self.steps[-1]['exit'] == int(bool(info['findings'])), 'in-place exit inconsistent')
            self.details['in_place_informational'] = info
            clone = self.out / 'committed-tree'
            self.run('clone', ['git', 'clone', '--local', '--no-hardlinks', '--no-checkout', '--no-recurse-submodules', self.repo, clone])
            self.run('checkout', ['git', 'checkout', '--detach', self.initial[0]], cwd=clone)
            identity = self.run('clone-identity', ['git', 'rev-parse', 'HEAD', 'HEAD^{tree}'], cwd=clone).decode().splitlines()
            need(identity == self.initial and not (clone / '.venv').exists(), 'clone identity/runtime')
            for path in OWNED + CHECKERS:
                need(digest((clone / path).read_bytes()) == self.details['source_hashes'][path], 'clone bytes ' + path)
            report = json.loads(self.run('public-tree', [self.python, clone / 'tools/check_public_tree.py'], cwd=clone))
            need(report.get('findings') == [] and type(report.get('scanned_files')) is int, 'public tree findings/JSON')
            self.details['public_tree'] = report

    def finish(self):
        need(self.identity('final-identity') == self.initial, 'identity changed during row')
        need(digest(self.manifest.read_bytes()) == self.details['manifest_sha256'], 'manifest changed')
        for path, expected in self.details['source_hashes'].items():
            need(digest((self.repo / path).read_bytes()) == expected, 'source changed during row')
        if not self.baseline:
            self.clean('status-after')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', required=True, choices=('suite', 'compatibility', 'portable', 'credentials', 'public-tree'))
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--out-base', type=Path, required=True)
    args = parser.parse_args()
    row = Row(args)
    error = None
    try:
        row.preflight()
        row.execute()
        row.finish()
    except Exception as exc:
        error = str(exc)
    result = {'schema': 'fc16-acceptance/v1', 'phase': args.phase,
              'argv': sys.argv, 'cwd': str(Path.cwd()), 'interpreter': sys.executable,
              'identity': getattr(row, 'initial', None), 'steps': row.steps,
              'details': row.details, 'exit': int(error is not None), 'error': error,
              'status': 'FAILED' if error else ('EXPECTED_RED_BASELINE' if row.baseline and args.phase == 'suite' else ('BASELINE_COMPATIBILITY_PASS' if row.baseline else 'REVIEW_REQUIRED'))}
    # All children have exited. Bind every retained raw output and test report.
    result['files'] = {str(p.relative_to(row.out)): digest(p.read_bytes())
                       for p in row.out.rglob('*') if p.is_file() and 'committed-tree' not in p.relative_to(row.out).parts}
    save(row.out / 'evidence.json', result)
    print(json.dumps({'evidence': str(row.out / 'evidence.json'), 'exit': result['exit'], 'status': result['status'], 'error': error}))
    return result['exit']


if __name__ == '__main__':
    raise SystemExit(main())
