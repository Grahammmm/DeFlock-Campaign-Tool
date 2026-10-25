"""Bounded offline FC10 acceptance; every invocation retains a fresh UUID row.

No product imports occur in this runner. Pure children install scoped guards and
child-local temporary policy before importing product code. Local pattern scans
are not private-data clearance, semantic acceptance, shipping CI or publication.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

BASE = ('17da706588467184' 'f0923e2516be3319d' '577539a')
REPO = Path(__file__).resolve().parents[1]
AUTHORED = (
    'tests/records/test_fc10_preparation_revision_closure.py',
    'tests/fixtures/fc10-preparation-revision-closure/cases.json',
    'tools/fc10_preparation_revision_closure_acceptance.py',
)
UNCHANGED = (
    'campaign_tool/records/detectors/preparation.py',
    'tests/records/test_detector_preparation.py',
    'tools/check_secret_report.py', 'tools/check_public_tree.py',
    'campaign_tool/records/public_scan.py', '.gitignore',
)
LIMITS = [
    'REVIEW REQUIRED; test-only trusted-host assertions, not provider authentication or qualification.',
    'No semantic, legal, human or live acceptance; all source/live holds preserved.',
    'Scoped compatibility only; final exact-head shipping CI and transport remain OWED to operator.',
    'Pattern scans only; manual privacy review remains required; no private export clearance.',
    'Scoped offline patches cover the admitted pure call graph, not a hostile-code sandbox.',
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
    require(path.is_absolute() and '..' not in path.parts and str(path) == os.path.abspath(path),
            'noncanonical path')
    for part in (path, *path.parents):
        require(not part.is_symlink(), 'unexpected symlink: ' + str(part))
    if exists:
        require(path.exists(), 'missing path: ' + str(path))
    return path


class Row:
    def __init__(self, args):
        # Only the manifest and validated candidate are read before product-free
        # checks. No box-specific path literals are needed in committed source.
        self.manifest_path = canonical_path(args.source_manifest)
        self.manifest = json.loads(self.manifest_path.read_text())
        m = self.manifest
        require(m['schema'] == 'agentorange.source-manifest/v1', 'source schema')
        require(Path(m['repo']) == REPO and Path(m['write_root']) == REPO.parent, 'source roots')
        require(m['head'] == BASE and m['clean'] is True, 'staged base')
        canonical_path(REPO)
        require(self.manifest_path == REPO.parent / 'SOURCE-MANIFEST.json', 'source manifest location')
        out_base = canonical_path(args.out_base, exists=False)
        require(out_base == REPO.parent / 'out', 'out base must be candidate-local')
        out_base.mkdir(mode=0o700, exist_ok=True)
        require(out_base.is_dir(), 'out base must be a directory')
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
        require(self.git('rev-parse', BASE + '^{tree}').strip() == m['tree'], 'base tree')
        self.run(['git', 'merge-base', '--is-ancestor', BASE, self.head])
        self.repo_python = REPO / '.venv/bin/python3'
        runtime = m['runtime']
        require((REPO / '.venv').is_symlink(), 'standard venv link missing')
        require((REPO / '.venv').resolve() == Path(runtime['repo_venv']).resolve(), 'standard venv link target')
        require(Path(runtime['repo_venv']).is_dir(), 'approved venv unavailable')
        expected = Path(runtime['credential_scan_python3']) if self.phase == 'credentials' else self.repo_python
        require(Path(sys.executable).absolute() == expected, 'wrong row interpreter')
        self.identity = {p: sha(REPO / p) for p in (*AUTHORED, *UNCHANGED)}
        for relative in UNCHANGED:
            data = self.run(['git', 'show', BASE + ':' + relative]).stdout
            require(hashlib.sha256(data).hexdigest() == self.identity[relative], 'changed protected source: ' + relative)
        changed = set(self.git('diff', '--name-only', BASE, '--').splitlines())
        require(changed <= set(AUTHORED), 'diff exceeds exclusive write set')
        self.facts['authored_hashes'] = {p: self.identity[p] for p in AUTHORED}
        self.facts['unchanged_hashes'] = {p: self.identity[p] for p in UNCHANGED}
        self.facts['fixture_spec_sha256'] = self.identity[AUTHORED[1]]
        self.facts['source_manifest_sha256'] = sha(self.manifest_path)
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
        return self.run(['git', *args], cwd).stdout.decode()

    def inspect_sources(self):
        # Broad product-source static audit, retained before importing anything.
        # Active call graph is prepare/canonical_joins/evaluate plus pinned pure
        # fixture context and in-memory ledger. Resolver/store/OCR calls are not
        # used. Callable defaults are reported, never evaluated by this audit.
        defaults = []
        imports = []
        for source in sorted((REPO / 'campaign_tool/records').rglob('*.py')):
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
        dump(self.root / 'import-default-audit.json', {'imports': imports, 'callable_defaults': defaults,
             'policy': 'No filesystem-reading defaults in admitted pure path; scoped guards precede product imports.'})
        return {'imports': len(imports), 'callable_defaults': defaults}

    def tests(self, label, mode='full', *, mutate=False, cwd=REPO):
        temp = self.root / (label + '-temporary')
        temp.mkdir(mode=0o700)
        summary = self.root / (label + '-tests.json')
        argv = [str(self.repo_python), str(REPO / AUTHORED[0]), '--temporary-root', str(temp),
                '--summary', str(summary), '--mode', mode]
        if mutate:
            argv.extend(['--omit-descriptor', 'support'])
        self.run(argv, cwd, expected=(1,) if mutate else (0,), label=label)
        data = json.loads(summary.read_text())
        require(not data['errors'] and not data['skipped'], 'test errors or skips')
        require(data['tests_run'] == len(data['test_ids']) and len(set(data['test_ids'])) == len(data['test_ids']),
                'test count/id mismatch')
        if mutate:
            expected = {'test_matrix_support_' + key for key in ('revision', 'code_sha256', 'receipt_sha256')}
            actual = {entry['id'].split('.')[-1] for entry in data['failures']}
            require(data['tests_run'] == 15 and actual == expected and len(data['failures']) == 3,
                    'negative control did not isolate support identity assertions')
            for entry in data['failures']:
                key = entry['id'].split('test_matrix_support_', 1)[1]
                require('descriptor identity must change: support.' + key in entry['traceback'],
                        'mutation failed an unrelated prerequisite')
            # All 15 prepare pairs reached the deciding check, including the
            # three killed changes, with source/payload/descriptor checks true.
            raw = (self.root / self.calls[-1]['stdout']).read_text()
            observations = [json.loads(v[len('OBSERVATION '):]) for v in raw.splitlines() if v.startswith('OBSERVATION ')]
            require(len(observations) == 15 and all(v['status'] == 'prepared' and v['inputs_unchanged'] and
                    v['source_unchanged'] and v['descriptors_exact'] for v in observations), 'mutation prerequisites')
        else:
            require(data['successful'] and not data['failures'], 'positive tests failed')
            if mode != 'compatibility':
                expected_count = 24 if mode == 'discovery' else 23
                require(data['tests_run'] == expected_count, 'required tests not executed')
                spec = json.loads((REPO / AUTHORED[1]).read_text())
                require(data['dimensions'] == spec['dimensions'] and data['matrix_cells'] == spec['descriptor_paths'],
                        'frozen fixture dimensions not fully covered')
        if mode == 'compatibility':
            require(data['tests_run'] > 0 and not data['matrix_cells'] and not data['dimensions'],
                    'compatibility must report only its actual executed tests')
        self.facts[label] = data
        return data

    def suite(self):
        self.tests('unmodified-baseline')
        self.tests('injected-control', 'matrix', mutate=True)
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
                strings = [n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            else:
                strings = [source]
            for value in strings:
                if re.search(r'/(?:home/[^/\s]+/|Users/[^/\s]+/|workspace/|tmp/)', value):
                    findings.append(relative)
        require(not findings, 'host-only authored literal')
        self.facts['host_only_literals'] = findings

    def credentials(self):
        runtime = self.manifest['runtime']
        scanner = Path(runtime['detect_secrets'])
        require(scanner.parent == Path(runtime['credential_scan_python3']).parent and scanner.is_file(),
                'scanner runtime mismatch')
        # os.walk's default followlinks=False is verified in installed scanner;
        # inspect candidate ancestry without following staged dependency links.
        symlinks = []
        for root, dirs, files in os.walk(REPO, followlinks=False):
            if Path(root) == REPO:
                dirs[:] = [v for v in dirs if v != '.git']
            for name in [*dirs, *files]:
                path = Path(root) / name
                if path.is_symlink():
                    symlinks.append(str(path.relative_to(REPO)))
        require(symlinks == ['.venv'], 'unexpected scanner boundary symlink')
        self.facts['scanner_boundary_symlinks_not_followed'] = symlinks
        scan = self.run([str(scanner), 'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify'],
                        label='detect-secrets')
        report = self.root / 'secret-report.json'
        report.write_bytes(scan.stdout)
        require(type(json.loads(scan.stdout)['results']) is dict, 'non-JSON scanner report')
        result = self.run([runtime['credential_scan_python3'], str(REPO / 'tools/check_secret_report.py'), str(report)],
                          label='check-secret-report')
        self.facts['credential_scan'] = {'scan_exit': scan.returncode, 'check_exit': result.returncode,
                                       'report_sha256': sha(report), 'results_paths': len(json.loads(scan.stdout)['results'])}

    def inventory(self):
        status = self.git('status', '--porcelain=v1', '--untracked-files=all')
        require(not status, 'candidate index/tracked/untracked state not clean')
        self.run(['git', 'diff', '--exit-code', self.head, '--'])
        self.run(['git', 'diff', '--cached', '--exit-code', self.head, '--'])
        tracked = self.git('ls-files', '-z').split('\0')
        visible = self.git('ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0')
        require(set(visible) - set(tracked) <= {'.venv'}, 'scan-visible untracked material')
        require(set(AUTHORED) <= set(tracked), 'authored bytes not committed')
        return {'status': status, 'tracked': sorted(v for v in tracked if v),
                'scan_visible': sorted(v for v in visible if v), 'venv_target': str((REPO / '.venv').resolve())}

    def public_tree(self):
        self.facts['source_inventory_before'] = self.inventory()
        clone = self.root / 'committed-tree'
        self.run(['git', 'clone', '--local', '--no-hardlinks', '--no-checkout', '--no-recurse-submodules',
                  str(REPO), str(clone)], label='clone-committed-tree')
        self.run(['git', 'checkout', '--detach', self.head], clone, label='checkout-exact-head')
        require(self.git('rev-parse', 'HEAD', cwd=clone).strip() == self.head, 'clone head mismatch')
        require(self.git('rev-parse', 'HEAD^{tree}', cwd=clone).strip() == self.tree, 'clone tree mismatch')
        require(not (clone / '.venv').exists(), 'clone runtime link forbidden')
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
        # Every child has been waited for. No command/output writer runs after
        # this freeze. Manifest inventory covers every regular output (including
        # the local clone), not just a same-count subset of selected files.
        if error is None:
            require(self.git('rev-parse', 'HEAD').strip() == self.head, 'source head changed')
            require(self.git('rev-parse', 'HEAD^{tree}').strip() == self.tree, 'source tree changed')
            require(all(sha(REPO / p) == value for p, value in self.identity.items()), 'source bytes changed')
            require(sha(self.manifest_path) == self.facts['source_manifest_sha256'], 'source manifest changed')
            require((REPO / '.venv').is_symlink() and
                    (REPO / '.venv').resolve() == Path(self.manifest['runtime']['repo_venv']).resolve(),
                    'runtime link changed')
        receipt = {'schema': 'fc10.acceptance-row/v1', 'phase': self.phase,
                   'verdict': 'PASS_REVIEW_REQUIRED' if error is None else 'FAIL', 'error': error,
                   'head': self.head, 'tree': self.tree, 'runner_interpreter': sys.executable,
                   'runner_argv': [sys.executable, *sys.argv], 'runner_cwd': str(Path.cwd()),
                   'commands': self.calls, 'facts': self.facts, 'limits': LIMITS}
        dump(self.root / 'receipt.json', receipt)
        files = {}
        for path in sorted(self.root.rglob('*')):
            require(not path.is_symlink(), 'output symlink cannot be sealed')
            if path.is_file():
                relative = path.relative_to(self.root).as_posix()
                require(relative not in files and '..' not in Path(relative).parts, 'output alias')
                files[relative] = {'sha256': sha(path), 'bytes': path.stat().st_size}
        manifest = self.root / 'evidence-manifest.json'
        dump(manifest, {'schema': 'fc10.evidence-manifest/v1', 'head': self.head, 'tree': self.tree,
                        'files': files, 'manifest_self_excluded': True})
        actual = {p.relative_to(self.root).as_posix() for p in self.root.rglob('*') if p.is_file() and p != manifest}
        require(actual == set(files), 'omitted output')
        require(all(sha(self.root / name) == entry['sha256'] for name, entry in files.items()), 'output hash mismatch')
        print(json.dumps({'phase': self.phase, 'verdict': receipt['verdict'], 'head': self.head,
                          'tree': self.tree, 'receipt': str(self.root / 'receipt.json'),
                          'manifest': str(manifest), 'manifest_file_sha256': sha(manifest),
                          'output_files': len(files), 'error': error}, sort_keys=True), flush=True)


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
