"""Bounded offline FC15 five-row recorder; machine-valid means review required.
Author: gpt-6.1-sol / openai-codex. No product code is changed by this recorder.
"""
import argparse
import ast
import hashlib
import importlib.util
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
import uuid

ROOT = Path(__file__).resolve().parents[1]
TEST = 'tests/records/test_fc15_row_cross_source_dedup.py'
FIXTURE = 'tests/fixtures/fc15-row-cross-source-dedup/cases.json'
RUNNER = 'tools/fc15_row_cross_source_dedup_acceptance.py'
AUTHORED = [TEST, FIXTURE, RUNNER]
# The supplied operator source manifest is the authority for public source pins.
# Do not duplicate those metadata values as scanner-visible source literals.
BASE = None
SELECTORS = [
    'tests.test_understanding_rows.RowTests.test_duplicate_evidence_retained_and_conflicts_unresolved',
    'tests.test_understanding_rows.RowTests.test_exact_csv_contract_identity_and_states',
]
CHECKERS = ['tools/check_secret_report.py', 'tools/check_public_tree.py', 'campaign_tool/records/public_scan.py']


def need(condition, reason):
    if not condition:
        raise ValueError(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def git(*args, cwd=ROOT):
    return subprocess.check_output(['git', *args], cwd=cwd)


def safe(path):
    need(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink input/output ancestry')
    return path


def identity():
    return {'head': git('rev-parse', 'HEAD').decode().strip(),
            'tree': git('rev-parse', 'HEAD^{tree}').decode().strip(),
            'status': git('status', '--porcelain=v1', '--untracked-files=all').decode(),
            'authored_sha256': {p: sha((ROOT / p).read_bytes()) for p in AUTHORED},
            'checker_sha256': {p: sha((ROOT / p).read_bytes()) for p in CHECKERS},
            'diff_sha256': sha(git('diff', '--binary', BASE, 'HEAD'))}


def closure():
    """Static local import inventory, including package initializers and defaults."""
    pending = ['campaign_tool.records.understanding.rows']
    seen = {}
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        path = ROOT.joinpath(*name.split('.')).with_suffix('.py')
        package = False
        if not path.is_file():
            path = ROOT.joinpath(*name.split('.')) / '__init__.py'
            package = True
        need(path.is_file(), 'missing transitive source: ' + name)
        safe(path)
        tree = ast.parse(path.read_text())
        imports = []
        defaults = []
        context = name if package else name.rpartition('.')[0]
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defaults.extend(ast.unparse(v) for v in [*node.args.defaults, *node.args.kw_defaults] if v is not None)
            if isinstance(node, ast.Import):
                imports.extend(a.name for a in node.names if a.name.startswith('campaign_tool'))
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    base = '.'.join(context.split('.')[:len(context.split('.')) - node.level + 1])
                    full = '.'.join(filter(None, [base, node.module]))
                else:
                    full = node.module or ''
                if full.startswith('campaign_tool'):
                    imports.append(full)
                    for alias in node.names:
                        candidate = full + '.' + alias.name
                        if ROOT.joinpath(*candidate.split('.')).with_suffix('.py').is_file() or ROOT.joinpath(*candidate.split('.'), '__init__.py').is_file():
                            imports.append(candidate)
        seen[name] = {'path': str(path.relative_to(ROOT)), 'sha256': sha(path.read_bytes()),
                      'imports': sorted(set(imports)), 'defaults': defaults}
        pending.extend(imports)
        parts = name.split('.')
        pending.extend('.'.join(parts[:n]) for n in range(1, len(parts)))
    return seen


def child(mode, directory):
    # No product import before redirect and restorable sentinels.
    sys.dont_write_bytecode = True
    tempfile.tempdir = str(directory)
    for key in ('TMPDIR', 'TMP', 'TEMP'):
        os.environ[key] = str(directory)
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location('fc15_cross_source', ROOT / TEST)
    if spec is None or spec.loader is None:
        raise ValueError('test module loader unavailable')
    module = importlib.util.module_from_spec(spec)
    before_env, before_temp = dict(os.environ), tempfile.tempdir
    before_bytecode = sys.dont_write_bytecode
    import socket
    saved_socket = socket.socket
    # Authored module has only inert stdlib imports.
    spec.loader.exec_module(module)
    need(dict(os.environ) == before_env and tempfile.tempdir == before_temp and socket.socket is saved_socket,
         'module import changed ambient defaults')
    records = {'test_ids': [], 'subcases': [], 'case_to_test': module.CASE_TESTS}

    class Result(unittest.TextTestResult):
        def startTest(self, test):
            records['test_ids'].append(test.id())
            super().startTest(test)

        def addSubTest(self, test, subtest, err):
            records['subcases'].append({'id': subtest.id(), 'success': err is None})
            super().addSubTest(test, subtest, err)

    class Peer(unittest.TestCase):
        def runTest(self):
            self.assertEqual(dict(os.environ), before_env)
            self.assertEqual(tempfile.tempdir, before_temp)
            self.assertIs(socket.socket, saved_socket)
            self.assertIs(sys.dont_write_bytecode, before_bytecode)
            self.assertEqual(1 + 1, 2)

    loader = unittest.TestLoader()
    with module.offline(directory):
        if mode == 'compatibility':
            suite = loader.loadTestsFromNames(SELECTORS)
        else:
            suite = loader.loadTestsFromModule(module)
    if mode == 'peer':
        suite = unittest.TestSuite([Peer(), suite, Peer()])
    with module.offline(directory) if mode != 'peer' else module.ExitStack():
        result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    need(dict(os.environ) == before_env and tempfile.tempdir == before_temp and socket.socket is saved_socket,
         'test lifetime changed ambient defaults')
    records.update(tests_run=result.testsRun, skips=result.skipped,
                   failures=len(result.failures), errors=len(result.errors), successful=result.wasSuccessful())
    dump(directory / ('tests-' + mode + '.json'), records)
    need(result.wasSuccessful() and not result.skipped, 'required tests failed/skipped')
    expected = 2 if mode == 'compatibility' else 9 if mode == 'peer' else 7
    need(result.testsRun == expected, 'missing required test')
    expected_ids = SELECTORS if mode == 'compatibility' else [
        'fc15_cross_source.CrossSourceTests.' + name for name in sorted([
            *module.CASE_TESTS.values(), 'test_deciding_source_projection_control'])]
    measured_ids = [name for name in records['test_ids'] if not name.endswith('.Peer.runTest')]
    need(measured_ids == expected_ids, 'required test identity mismatch')
    if mode != 'compatibility':
        need(len(records['subcases']) == 26021, 'missing frozen permutation/control subcase')
        need(all(s['success'] for s in records['subcases']), 'failed required subcase')
    return 0


def main():
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=['suite', 'compatibility', 'portable', 'credentials', 'public-tree'])
    parser.add_argument('--source-manifest', type=Path)
    parser.add_argument('--out-base', type=Path)
    parser.add_argument('--child', choices=['suite', 'compatibility', 'peer'])
    parser.add_argument('--child-output', type=Path)
    options = parser.parse_args()
    if options.child:
        return child(options.child, safe(options.child_output.absolute()))
    need(options.phase and options.source_manifest and options.out_base, 'required arguments')
    manifest_path = safe(options.source_manifest.absolute())
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    need(manifest['schema'] == 'agentorange.source-manifest/v1', 'manifest schema')
    need(Path(manifest['repo']) == ROOT and Path(manifest['write_root']) == ROOT.parent, 'manifest repository')
    need(re.fullmatch(r'[0-9a-f]{40}', manifest['head']) is not None, 'initial full head required')
    BASE = manifest['head']
    need(git('rev-parse', BASE + '^{tree}').decode().strip() == manifest['tree'], 'initial tree')
    need(git('merge-base', BASE, 'HEAD').decode().strip() == BASE, 'candidate ancestry')
    safe(ROOT)
    need((ROOT / '.venv').is_symlink() and (ROOT / '.venv').resolve() == Path(manifest['runtime']['repo_venv']), 'manifest venv')
    expected_python = manifest['runtime']['credential_scan_python3'] if options.phase == 'credentials' else str(ROOT / '.venv/bin/python3')
    need(Path(sys.executable).absolute() == Path(expected_python).absolute(), 'runtime interpreter spelling')
    need(Path(sys.prefix).resolve() == Path(manifest['runtime']['repo_venv']).resolve(), 'runtime interpreter environment')
    for p in AUTHORED + CHECKERS:
        safe(ROOT / p)
    out_base = safe(options.out_base.absolute())
    need(out_base == ROOT.parent / 'out' and ROOT not in out_base.parents, 'exclusive output root')
    out_base.mkdir(exist_ok=True)
    directory = out_base / (options.phase + '-' + uuid.uuid4().hex)
    directory.mkdir()
    start = identity()
    receipt = {'phase': options.phase, 'argv': sys.argv, 'cwd': str(Path.cwd()),
        'interpreter': sys.executable, 'source_manifest_sha256': sha(manifest_raw),
        'identity': start, 'children': [], 'review_required': True, 'author': 'gpt-6.1-sol / openai-codex'}
    dump(directory / 'started.json', receipt)
    (directory / 'candidate.diff').write_bytes(git('diff', '--binary', BASE, 'HEAD'))
    (directory / 'source-manifest.json').write_bytes(manifest_raw)

    def run(label, argv, cwd=ROOT, allowed=(0,)):
        result = subprocess.run([str(v) for v in argv], cwd=cwd, capture_output=True, shell=False, timeout=600)
        stdout, stderr = directory / (label + '.stdout'), directory / (label + '.stderr')
        stdout.write_bytes(result.stdout)
        stderr.write_bytes(result.stderr)
        entry = {'label': label, 'argv': [str(v) for v in argv], 'cwd': str(cwd),
                 'exit': result.returncode, 'stdout': str(stdout), 'stderr': str(stderr),
                 'stdout_sha256': sha(result.stdout), 'stderr_sha256': sha(result.stderr)}
        receipt['children'].append(entry)
        dump(directory / 'progress.json', receipt)
        need(result.returncode in allowed, 'child failed: ' + label)
        return result

    try:
        dump(directory / 'import-closure.json', closure())
        # All existing checker bytes and product files must equal the pinned base.
        changes = git('diff', '--name-only', BASE, 'HEAD').decode().splitlines()
        need(set(changes) <= set(AUTHORED), 'complete candidate diff exceeds exclusive write set')
        for p in CHECKERS + ['campaign_tool/records/understanding/rows.py', 'tests/test_understanding_rows.py']:
            need((ROOT / p).read_bytes() == git('show', BASE + ':' + p), 'pinned source/checker drift')
        if start['head'] != BASE:
            need(not start['status'], 'final acceptance requires clean candidate')
        if options.phase in ('suite', 'portable', 'credentials', 'public-tree'):
            need(start['head'] != BASE and not start['status'], 'final candidate must be committed and clean')
        if options.phase in ('suite', 'compatibility'):
            run(options.phase, [sys.executable, __file__, '--child', options.phase, '--child-output', directory])
            receipt['test_results'] = json.loads((directory / ('tests-' + options.phase + '.json')).read_text())
        elif options.phase == 'portable':
            unrelated = directory / 'unrelated'
            unrelated.mkdir()
            run('absolute-script', [sys.executable, ROOT / TEST, '--temporary-root', directory], cwd=unrelated)
            run('peer-discovery', [sys.executable, __file__, '--child', 'peer', '--child-output', directory], cwd=unrelated)
            receipt['test_results'] = json.loads((directory / 'tests-peer.json').read_text())
            for p in AUTHORED:
                need(not re.search(r'/(?:home|Users|workspace)/', (ROOT / p).read_text()), 'box-only literal path')
        elif options.phase == 'credentials':
            scanner = Path(manifest['runtime']['detect_secrets'])
            need(scanner.parent == Path(sys.executable).parent and scanner.is_file(), 'scanner runtime')
            result = run('detect-secrets', [sys.executable, scanner, 'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify'])
            parsed = json.loads(result.stdout)
            need(isinstance(parsed.get('results'), dict), 'scanner JSON')
            run('check-secret-report', [sys.executable, ROOT / 'tools/check_secret_report.py', directory / 'detect-secrets.stdout'])
        else:
            need(not start['status'], 'dirty candidate')
            inventory = git('ls-files', '-z', '--cached', '--others', '--exclude-standard').decode().split('\0')
            need(set(git('ls-files', '--others', '--exclude-standard').decode().splitlines()) <= {'.venv'}, 'scan-visible untracked')
            need((ROOT / '.venv').resolve() == Path(manifest['runtime']['repo_venv']), 'informational symlink binding')
            dump(directory / 'path-inventory.json', inventory)
            for p in AUTHORED:
                need(git('show', 'HEAD:' + p) == (ROOT / p).read_bytes(), 'HEAD/file differs')
                need(git('show', ':' + p) == (ROOT / p).read_bytes(), 'index/file differs')
            clone = directory / 'committed-tree'
            run('local-clone', ['git', 'clone', '--local', '--no-hardlinks', '--no-checkout', '--no-recurse-submodules', ROOT, clone])
            run('checkout', ['git', 'checkout', '--detach', start['head']], cwd=clone)
            need(git('rev-parse', 'HEAD', cwd=clone).decode().strip() == start['head'], 'clone HEAD')
            need(git('rev-parse', 'HEAD^{tree}', cwd=clone).decode().strip() == start['tree'], 'clone tree')
            need(not (clone / '.venv').exists(), 'clone runtime link')
            for p in AUTHORED:
                need(sha((clone / p).read_bytes()) == start['authored_sha256'][p], 'clone authored hash')
            result = run('committed-tree-check', [sys.executable, clone / 'tools/check_public_tree.py'], cwd=clone)
            need(json.loads(result.stdout)['findings'] == [], 'committed-tree findings')
            result = run('in_place_informational', [sys.executable, ROOT / 'tools/check_public_tree.py'], allowed=(0, 1))
            findings = json.loads(result.stdout)['findings']
            need((result.returncode == 0 and findings == []) or
                 (result.returncode == 1 and findings == [['.venv', 0, 'symlink_not_scanned']]), 'unrecognized in-place outcome')
        need(identity() == start and manifest_path.read_bytes() == manifest_raw, 'source/candidate changed during row')
        receipt['exit'] = 0
    except Exception:
        receipt['exit'] = 1
        receipt['failure'] = traceback.format_exc()
    receipt['evidence_files'] = {str(p.relative_to(directory)): sha(p.read_bytes())
        for p in directory.iterdir() if p.is_file() and p.name not in ('receipt.json',)}
    dump(directory / 'receipt.json', receipt)
    print(json.dumps({'phase': options.phase, 'exit': receipt['exit'], 'receipt': str(directory / 'receipt.json'),
                      'receipt_sha256': sha((directory / 'receipt.json').read_bytes()),
                      'head': start['head']}))
    if receipt['exit']:
        print(receipt['failure'], file=sys.stderr)
    return receipt['exit']


if __name__ == '__main__':
    raise SystemExit(main())
