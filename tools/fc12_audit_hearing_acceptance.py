"""Bounded offline FC12 audit/hearing acceptance; each invocation keeps a fresh UUID row.

This runner imports no product code. Test children install scoped offline guards
and a child-local temporary root before product imports. Results are REVIEW
REQUIRED only: no semantic, legal, human or live acceptance, no shipping CI.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

REPO = Path(__file__).resolve().parents[1]
AUTHORED = (
    'tests/records/test_fc12_audit_hearing.py',
    'tests/fixtures/fc12-audit-hearing/cases.json',
    'tools/fc12_audit_hearing_acceptance.py',
)
UNCHANGED = (
    'campaign_tool/digest/detectors.py',
    'campaign_tool/digest/__init__.py',
    'tests/runner/test_digest.py',
    'tools/check_secret_report.py',
    'tools/check_public_tree.py',
    'campaign_tool/records/public_scan.py',
    '.gitignore',
)
CONTROLS = ('audit-never', 'hearing-never', 'swapped-names', 'spurious-absence')
META_TESTS = ('test_vocabulary_frozen', 'test_dimensions_covered', 'test_run_detectors_composition')
LIMITS = [
    'REVIEW REQUIRED; test-only textual-lead/absence-warning assertions, not proof an audit or hearing occurred.',
    'No semantic, legal, human or live acceptance; all source/live holds preserved.',
    'Scoped compatibility only (tests.runner.test_digest.DetectorTests); full discovery = operator-run; '
    'exact-head shipping CI and transport remain OWED to operator.',
    'Multiline separator semantics (FC6) and structured audit-every-N (FC9) excluded.',
    'Pattern scans only; scoped offline patches are not a hostile-code sandbox.',
]


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')


def canonical_path(path, *, exists=True):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts and str(path) == os.path.abspath(path), 'noncanonical path')
    for part in (path, *path.parents):
        require(not part.is_symlink(), 'unexpected symlink: ' + str(part))
    if exists:
        require(path.exists(), 'missing path: ' + str(path))
    return path


class Row:
    def __init__(self, args):
        self.manifest_path = canonical_path(args.source_manifest)
        require(self.manifest_path == REPO.parent / 'SOURCE-MANIFEST.json', 'source manifest location')
        self.manifest = json.loads(self.manifest_path.read_text())
        m = self.manifest
        require(m['schema'] == 'agentorange.source-manifest/v1', 'source schema')
        require(Path(m['repo']) == REPO and Path(m['write_root']) == REPO.parent, 'source roots')
        require(re.fullmatch(r'[a-f0-9]{40}', m['head']) and m['clean'] is True, 'staged base')
        self.base = m['head']
        canonical_path(REPO)
        out_base = canonical_path(args.out_base, exists=False)
        require(out_base == REPO.parent / 'out', 'out base must be candidate-local')
        out_base.mkdir(mode=0o700, exist_ok=True)
        require(out_base.is_dir() and not out_base.is_symlink(), 'out base must be a directory')
        self.root = out_base / (args.phase + '-' + str(uuid.uuid4()))
        self.root.mkdir(mode=0o700)
        self.temp = self.root / 'temporary'
        self.temp.mkdir(mode=0o700)
        tempfile.tempdir = str(self.temp)
        sys.dont_write_bytecode = True
        self.phase = args.phase
        self.calls = []
        self.facts = {}
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.tree = self.git('rev-parse', 'HEAD^{tree}').strip()
        require(self.git('rev-parse', self.base + '^{tree}').strip() == m['tree'], 'base tree')
        self.run(['git', 'merge-base', '--is-ancestor', self.base, self.head], label='base-ancestry')
        self.repo_python = REPO / '.venv/bin/python3'
        runtime = m['runtime']
        require((REPO / '.venv').is_symlink(), 'standard venv link missing')
        require((REPO / '.venv').resolve() == Path(runtime['repo_venv']).resolve(), 'standard venv link target')
        expected = Path(runtime['credential_scan_python3']) if self.phase == 'credentials' else self.repo_python
        require(Path(sys.executable).absolute() == expected, 'wrong row interpreter')
        self.identity = {p: sha(REPO / p) for p in (*AUTHORED, *UNCHANGED)}
        for relative in UNCHANGED:
            data = self.run(['git', 'show', self.base + ':' + relative], label='base-blob').stdout
            require(hashlib.sha256(data).hexdigest() == self.identity[relative], 'changed protected source: ' + relative)
        changed = set(self.git('diff', '--name-only', self.base, '--').splitlines())
        require(changed <= set(AUTHORED), 'diff exceeds exclusive write set')
        self.facts['diff_vs_base'] = sorted(changed)
        self.facts['authored_hashes'] = {p: self.identity[p] for p in AUTHORED}
        self.facts['unchanged_hashes'] = {p: self.identity[p] for p in UNCHANGED}
        self.facts['fixture_sha256'] = self.identity[AUTHORED[1]]
        self.facts['source_manifest_sha256'] = sha(self.manifest_path)
        self.spec = json.loads((REPO / AUTHORED[1]).read_text())
        self.facts['import_default_inspection'] = self.inspect_sources()
        print('ROW ' + str(self.root), flush=True)

    def run(self, argv, cwd=REPO, *, expected=(0,), label=None):
        require(type(argv) is list and all(type(v) is str for v in argv), 'literal argv list required')
        name = f'{len(self.calls):03d}-' + (label or 'command')
        result = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, shell=False)
        stdout = self.root / (name + '.stdout')
        stderr = self.root / (name + '.stderr')
        stdout.write_bytes(result.stdout)
        stderr.write_bytes(result.stderr)
        self.calls.append({'argv': argv, 'cwd': str(cwd), 'exit': result.returncode,
                           'interpreter': argv[0] if 'python' in Path(argv[0]).name else None,
                           'stdout': stdout.name, 'stdout_sha256': sha(stdout),
                           'stderr': stderr.name, 'stderr_sha256': sha(stderr)})
        require(result.returncode in expected, 'unexpected child exit: ' + name + ': ' + str(result.returncode))
        return result

    def git(self, *args, cwd=REPO):
        return self.run(['git', *args], cwd, label='git').stdout.decode()

    def inspect_sources(self):
        # Static import/default audit of the admitted call graph, before any import.
        imports, defaults = [], []
        for source in sorted((REPO / 'campaign_tool/digest').glob('*.py')):
            tree = ast.parse(source.read_text(), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    imports.append({'path': str(source.relative_to(REPO)), 'line': node.lineno,
                                    'import': ast.unparse(node)})
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for value in [*node.args.defaults, *[v for v in node.args.kw_defaults if v is not None]]:
                        if any(isinstance(v, ast.Call) for v in ast.walk(value)):
                            defaults.append({'path': str(source.relative_to(REPO)), 'function': node.name,
                                             'value': ast.unparse(value)})
        detector_imports = [i for i in imports if i['path'] == 'campaign_tool/digest/detectors.py']
        require([i['import'] for i in detector_imports] == ['import re'], 'detectors import closure changed')
        dump(self.root / 'import-default-audit.json', {'imports': imports, 'callable_defaults': defaults,
             'policy': 'detectors.py imports only re; scoped guards and child temp root precede product imports.'})
        return {'imports': len(imports), 'detectors_imports': ['import re'], 'callable_defaults': defaults}

    def positive_cells(self, families):
        return {'test_case_' + c['id'] + '__' + f for c in self.spec['cases'] for f in families if c['expect'][f]}

    def tests(self, label, mode='full', *, control=None, cwd=REPO):
        temp = self.root / (label + '-temporary')
        temp.mkdir(mode=0o700)
        summary = self.root / (label + '-tests.json')
        argv = [str(self.repo_python), str(REPO / AUTHORED[0]), '--temporary-root', str(temp),
                '--summary', str(summary), '--mode', mode]
        if control:
            argv.extend(['--control', control])
        self.run(argv, cwd, expected=(1,) if control else (0,), label=label)
        data = json.loads(summary.read_text())
        require(not data['errors'] and not data['skipped'], 'test errors or skips')
        require(data['tests_run'] == len(data['test_ids']) == len(set(data['test_ids'])), 'test count/id mismatch')
        stdout = (self.root / self.calls[-1]['stdout']).read_text()
        subcases = [json.loads(v[len('SUBCASE '):]) for v in stdout.splitlines() if v.startswith('SUBCASE ')]
        cells = 2 * len(self.spec['cases'])
        if mode != 'compatibility':
            require(len(self.spec['cases']) == 27 and cells == 54, 'frozen case count')
            require(data['tests_run'] == cells + len(META_TESTS) + (1 if mode == 'discovery' else 0),
                    'required tests not executed')
            require(data['case_family_cells'] == [[c['id'], f] for c in self.spec['cases'] for f in ('audit', 'hearing')],
                    'case matrix incomplete')
            require(data['dimensions'] == self.spec['dimensions'], 'frozen dimensions not covered')
            require(sorted((s['case'], s['family']) for s in subcases) ==
                    sorted((c, f) for c, f in data['case_family_cells']), 'subcase execution incomplete')
            names = {t.rsplit('.', 1)[-1] for t in data['test_ids']}
            require(set(META_TESTS) <= names, 'meta tests missing')
        if control:
            spec = self.spec['negative_controls'][control]
            expected_ids = self.positive_cells(spec['failing_family'])
            actual = {f['id'].rsplit('.', 1)[-1] for f in data['failures']}
            require(expected_ids and actual == expected_ids and len(data['failures']) == len(expected_ids),
                    'control failing set mismatch: ' + control)
            for failure in data['failures']:
                family = failure['id'].rsplit('__', 1)[1]
                require(spec['marker'] + family in failure['traceback'] and 'AssertionError' in failure['traceback'],
                        'control failed an unrelated prerequisite: ' + failure['id'])
            mutation = [json.loads(v[len('MUTATION '):]) for v in stdout.splitlines() if v.startswith('MUTATION ')]
            require(len(mutation) == 1 and mutation[0]['name'] == control == data['control']['name'], 'control identity')
            data['expected_failing_ids'] = sorted(expected_ids)
        else:
            require(data['successful'] and not data['failures'], 'positive tests failed')
        if mode == 'compatibility':
            require(data['tests_run'] > 0 and not data['case_family_cells'] and not subcases,
                    'compatibility must report only its actual executed tests')
            require(all(t.startswith('tests.runner.test_digest.DetectorTests.') for t in data['test_ids']),
                    'compatibility selector exceeded')
        data['subcase_count'] = len(subcases)
        self.facts[label] = data
        return data

    def suite(self):
        self.tests('unmodified-baseline')
        for control in CONTROLS:
            self.tests('control-' + control, control=control)
        self.tests('restored-positive')

    def compatibility(self):
        self.tests('compatibility', 'compatibility')

    def portable(self):
        cwd = self.root / 'unrelated-cwd'
        cwd.mkdir(mode=0o700)
        self.tests('portable-absolute-file', cwd=cwd)
        self.tests('ordinary-discovery-with-sentinels', 'discovery', cwd=cwd)
        findings = []
        for relative in AUTHORED:
            source = (REPO / relative).read_text()
            if relative.endswith('.py'):
                tree = ast.parse(source)
                strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            else:
                strings = [source]
            for value in strings:
                if re.search(r'/(?:home/[^/\s]+/|Users/[^/\s]+/|workspace/|tmp/)', value):
                    findings.append(relative)
        require(not findings, 'host-only authored literal')
        # No module-level writes or ambient mutation in the test module: only
        # definitions, constants and the generated setattr loop at top level.
        tree = ast.parse((REPO / AUTHORED[0]).read_text())
        top = [type(n).__name__ for n in tree.body]
        require(set(top) <= {'Expr', 'Import', 'ImportFrom', 'Assign', 'FunctionDef', 'ClassDef', 'For', 'If'},
                'unexpected module-level statement')
        self.facts['host_only_literals'] = findings
        self.facts['test_module_top_level'] = top

    def credentials(self):
        runtime = self.manifest['runtime']
        scanner = Path(runtime['detect_secrets'])
        require(scanner.parent == Path(runtime['credential_scan_python3']).parent and scanner.is_file(),
                'scanner runtime mismatch')
        symlinks = []
        for root, dirs, files in os.walk(REPO, followlinks=False):
            if Path(root) == REPO:
                dirs[:] = [v for v in dirs if v != '.git']
            for name in [*dirs, *files]:
                if (Path(root) / name).is_symlink():
                    symlinks.append(str((Path(root) / name).relative_to(REPO)))
        require(symlinks == ['.venv'], 'unexpected scanner boundary symlink')
        self.facts['scanner_boundary_symlinks_not_followed'] = symlinks
        scan = self.run([str(scanner), 'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify'],
                        label='detect-secrets')
        report = self.root / 'secret-report.json'
        report.write_bytes(scan.stdout)
        results = json.loads(scan.stdout)['results']
        require(type(results) is dict, 'non-JSON scanner report')
        check = self.run([runtime['credential_scan_python3'], str(REPO / 'tools/check_secret_report.py'), str(report)],
                         label='check-secret-report')
        self.facts['credential_scan'] = {'scan_exit': scan.returncode, 'check_exit': check.returncode,
                                         'report_sha256': sha(report), 'results_paths': len(results)}

    def inventory(self):
        status = self.git('status', '--porcelain=v1', '--untracked-files=all')
        require(not status, 'candidate index/tracked/untracked state not clean')
        self.run(['git', 'diff', '--exit-code', self.head, '--'], label='diff-worktree')
        self.run(['git', 'diff', '--cached', '--exit-code', self.head, '--'], label='diff-index')
        tracked = [v for v in self.git('ls-files', '-z').split('\0') if v]
        visible = [v for v in self.git('ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0') if v]
        require(set(visible) - set(tracked) <= {'.venv'}, 'scan-visible untracked material')
        require(set(AUTHORED) <= set(tracked), 'authored bytes not committed')
        return {'status': status, 'tracked': sorted(tracked), 'scan_visible': sorted(visible),
                'venv_target': str((REPO / '.venv').resolve())}

    def public_tree(self):
        self.facts['source_inventory_before'] = self.inventory()
        self.facts['scanner_hashes'] = {p: self.identity[p] for p in
                                        ('tools/check_public_tree.py', 'campaign_tool/records/public_scan.py')}
        clone = self.root / 'committed-tree'
        self.run(['git', 'clone', '--local', '--no-hardlinks', '--no-checkout', '--no-recurse-submodules',
                  str(REPO), str(clone)], label='clone-committed-tree')
        self.run(['git', 'checkout', '--detach', self.head], clone, label='checkout-exact-head')
        require(self.git('rev-parse', 'HEAD', cwd=clone).strip() == self.head, 'clone head mismatch')
        require(self.git('rev-parse', 'HEAD^{tree}', cwd=clone).strip() == self.tree, 'clone tree mismatch')
        require(not (clone / '.venv').exists() and not (clone / '.venv').is_symlink(), 'clone runtime link forbidden')
        for relative, expected in self.identity.items():
            require(sha(clone / relative) == expected, 'clone source hash mismatch: ' + relative)
        scan = self.run([str(self.repo_python), str(clone / 'tools/check_public_tree.py')], clone,
                        label='exact-committed-tree-public-scan')
        data = json.loads(scan.stdout)
        require(data['findings'] == [] and data['scanned_files'] > 0, 'public-tree findings/incomplete scan')
        self.facts['exact_committed_tree_scan'] = data
        info = self.run([str(self.repo_python), str(REPO / 'tools/check_public_tree.py')], expected=(0, 1),
                        label='in-place-informational-INC-174')
        info_data = json.loads(info.stdout)
        require((info.returncode == 0 and info_data['findings'] == []) or
                (info.returncode == 1 and info_data['findings'] == [['.venv', 0, 'symlink_not_scanned']]),
                'unpermitted in-place informational finding')
        require(info_data['scanned_files'] == data['scanned_files'], 'in-place incomplete scan')
        self.facts['in_place_informational'] = {'INC-174': True, 'exit': info.returncode, 'result': info_data}
        self.facts['source_inventory_after'] = self.inventory()
        require(self.facts['source_inventory_before'] == self.facts['source_inventory_after'], 'source status changed')

    def finish(self, error=None):
        # Every child has been waited for; nothing writes after this freeze.
        if error is None:
            require(self.git('rev-parse', 'HEAD').strip() == self.head, 'source head changed')
            require(self.git('rev-parse', 'HEAD^{tree}').strip() == self.tree, 'source tree changed')
            require(all(sha(REPO / p) == value for p, value in self.identity.items()), 'source bytes changed')
            require(sha(self.manifest_path) == self.facts['source_manifest_sha256'], 'source manifest changed')
        receipt = {'schema': 'fc12-audit-hearing.acceptance-row/v1', 'phase': self.phase,
                   'verdict': 'PASS_REVIEW_REQUIRED' if error is None else 'FAIL', 'error': error,
                   'base': self.base, 'head': self.head, 'tree': self.tree, 'runner_interpreter': sys.executable,
                   'runner_argv': [sys.executable, *sys.argv], 'runner_cwd': str(Path.cwd()),
                   'commands': self.calls, 'facts': self.facts, 'limits': LIMITS}
        dump(self.root / 'receipt.json', receipt)
        files = {}
        for path in sorted(self.root.rglob('*')):
            require(not path.is_symlink(), 'output symlink cannot be sealed')
            if path.is_file():
                relative = path.relative_to(self.root).as_posix()
                require(relative not in files, 'output alias')
                files[relative] = {'sha256': sha(path), 'bytes': path.stat().st_size}
        manifest = self.root / 'evidence-manifest.json'
        dump(manifest, {'schema': 'fc12-audit-hearing.evidence-manifest/v1', 'head': self.head, 'tree': self.tree,
                        'files': files, 'manifest_self_excluded': True})
        actual = {p.relative_to(self.root).as_posix() for p in self.root.rglob('*') if p.is_file() and p != manifest}
        require(actual == set(files), 'omitted output')
        require(all(sha(self.root / n) == e['sha256'] for n, e in files.items()), 'output hash mismatch')
        print(json.dumps({'phase': self.phase, 'verdict': receipt['verdict'], 'head': self.head, 'tree': self.tree,
                          'receipt': str(self.root / 'receipt.json'), 'manifest': str(manifest),
                          'manifest_file_sha256': sha(manifest), 'output_files': len(files), 'error': error},
                         sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', required=True, choices=('suite', 'compatibility', 'portable', 'credentials', 'public-tree'))
    parser.add_argument('--source-manifest', type=Path, required=True)
    parser.add_argument('--out-base', type=Path, required=True)
    args = parser.parse_args()
    row = Row(args)
    try:
        getattr(row, args.phase.replace('-', '_'))()
    except Exception as error:
        row.finish(type(error).__name__ + ': ' + str(error))
        return 1
    row.finish()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
