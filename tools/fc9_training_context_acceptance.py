"""Offline FC9 acceptance, one phase and fresh evidence directory per invocation."""
import argparse
import hashlib
import importlib
import io
import itertools
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

REPO = Path(__file__).resolve().parents[1]
TEST = 'tests/records/test_fc9_training_context.py'
FIXTURE = 'tests/fixtures/fc9-training-context/cases.json'
RUNNER = 'tools/fc9_training_context_acceptance.py'
AUTHORED = (TEST, FIXTURE, RUNNER)
UNCHANGED = ('campaign_tool/records/detectors/core.py', 'tests/records/test_detectors.py',
             'tools/check_secret_report.py', 'tools/check_public_tree.py',
             'campaign_tool/records/public_scan.py', '.gitignore')
SELECTORS = ('tests.records.test_detectors.DetectorTests',
             'tests.records.test_detectors.DetectorRepairTests')


def require(value, reason):
    if not value:
        raise ValueError(reason)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, data):
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


def case_dimensions():
    data = json.loads((REPO / FIXTURE).read_text())
    cases = [{**case, 'permutations': len(list(itertools.permutations(case['rosters'] +
               ([case['extra']] if case.get('extra') else []))))} for case in data['cases']]
    return {'cases': cases, 'controls': data['controls'], 'fixture_cases': len(cases),
            'permutation_evaluations': sum(case['permutations'] for case in cases)}


def child(mode, root):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    temp = root / 'temporary'
    temp.mkdir()
    tempfile.tempdir = str(temp)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    # No product imports occurred before the output-local temporary redirect.
    loader = unittest.TestLoader()
    if mode == 'compatibility':
        suite = loader.loadTestsFromNames(SELECTORS)
    elif mode == 'suite':
        suite = loader.loadTestsFromName('tests.records.test_fc9_training_context')
    else:
        # Ordinary discovery surrounds an unrelated peer suite. Discovery itself
        # may not alter process environment, temp policy, or import machinery.
        environment = dict(os.environ)
        original_temp = tempfile.tempdir
        original_path = list(sys.path)
        original_meta = list(sys.meta_path)
        class Sentinel(unittest.TestCase):
            def test_unrelated_peer(self):
                self.assertEqual(dict(os.environ), environment)
                self.assertEqual(tempfile.tempdir, original_temp)
                self.assertEqual(sys.path, original_path)
                self.assertEqual(sys.meta_path, original_meta)
                with tempfile.TemporaryFile() as handle:
                    handle.write(b'synthetic unrelated peer')
                    handle.seek(0)
                    self.assertEqual(handle.read(), b'synthetic unrelated peer')
        before = unittest.TextTestRunner(verbosity=2).run(loader.loadTestsFromTestCase(Sentinel))
        require(before.wasSuccessful(), 'pre-discovery sentinel failed')
        suite = loader.discover(str(REPO / 'tests' / 'records'),
                                pattern='test_fc9_training_context.py', top_level_dir=str(REPO))
        require(dict(os.environ) == environment and tempfile.tempdir == original_temp and
                sys.path == original_path and sys.meta_path == original_meta,
                'discovery ambient contamination')
    class Result(unittest.TextTestResult):
        def startTest(self, test):
            ids.append(test.id())
            super().startTest(test)
    ids = []
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    if mode == 'discovery':
        after = unittest.TextTestRunner(verbosity=2).run(loader.loadTestsFromTestCase(Sentinel))
        require(after.wasSuccessful(), 'post-suite sentinel failed')
    summary = {'mode': mode, 'tests_run': result.testsRun, 'test_ids': ids,
               'skipped': len(result.skipped), 'errors': len(result.errors),
               'failures': len(result.failures), 'successful': result.wasSuccessful(),
               'executable': sys.executable, 'prefix': sys.prefix,
               'temporary_root': tempfile.tempdir}
    if mode != 'compatibility':
        summary['coverage'] = case_dimensions()
    if mode == 'discovery':
        summary['sentinels'] = {'before': before.testsRun, 'after': after.testsRun}
    dump(root / 'summary.json', summary)
    require(result.wasSuccessful() and not result.skipped and result.testsRun > 0, 'unittest failed or skipped')
    print(json.dumps({'child_summary': summary}, sort_keys=True))
    return 0


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.manifest_path = args.source_manifest.resolve(strict=True)
        self.manifest = json.loads(self.manifest_path.read_text())
        self.root = args.out_base.resolve() / (args.phase + '-' + str(uuid.uuid4()))
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / 'temporary').mkdir()
        tempfile.tempdir = str(self.root / 'temporary')
        sys.dont_write_bytecode = True
        self.commands = []
        self.report = {'phase': args.phase, 'argv': [sys.executable, *sys.argv],
                       'cwd': str(Path.cwd()), 'executable': sys.executable,
                       'prefix': sys.prefix, 'manifest_path': str(self.manifest_path),
                       'manifest_sha256': digest(self.manifest_path), 'status': 'FAIL',
                       'scope': 'REVIEW REQUIRED; no semantic, legal, human or live acceptance',
                       'commands': self.commands}

    def run(self, argv, cwd=REPO, label='command', expected=(0,)):
        argv = [str(item) for item in argv]
        number = len(self.commands)
        stem = f'{number:03d}-{label}'
        completed = subprocess.run(argv, cwd=cwd, shell=False, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, check=False)
        out, err = self.root / (stem + '.stdout'), self.root / (stem + '.stderr')
        out.write_bytes(completed.stdout)
        err.write_bytes(completed.stderr)
        record = {'argv': argv, 'cwd': str(cwd), 'shell': False, 'exit': completed.returncode,
                  'stdout': out.name, 'stderr': err.name,
                  'stdout_sha256': digest(out), 'stderr_sha256': digest(err)}
        self.commands.append(record)
        dump(self.root / (stem + '.json'), record)
        require(completed.returncode in expected, 'child failed: ' + label + ' (see ' + stem + ')')
        return completed.stdout.decode('utf-8')

    def git(self, *args, cwd=REPO):
        return self.run(['git', *args], cwd=cwd, label='git').strip()

    def identity(self):
        return {'head': self.git('rev-parse', 'HEAD'), 'tree': self.git('rev-parse', 'HEAD^{tree}'),
                'status': self.git('status', '--porcelain=v1', '--untracked-files=all'),
                'path_inventory': self.git('ls-files', '--cached', '--others', '--exclude-standard')}

    def validate(self):
        m = self.manifest
        require(m['schema'] == 'agentorange.source-manifest/v1', 'manifest schema')
        require(Path(m['repo']).resolve() == REPO and Path(m['write_root']).resolve() == REPO.parent,
                'manifest repository/write-root mismatch')
        require(self.args.out_base.resolve() == REPO.parent / 'out', 'unadmitted output base')
        require(re.fullmatch(r'[0-9a-f]{40}', m['head']) and re.fullmatch(r'[0-9a-f]{40}', m['tree']),
                'malformed source identity')
        require(self.git('remote', 'get-url', 'origin') == m['remote'], 'manifest repository origin mismatch')
        require(self.git('rev-parse', m['head'] + '^{tree}') == m['tree'], 'base tree mismatch')
        ident = self.identity()
        self.report['candidate'] = ident
        require(re.fullmatch(r'[0-9a-f]{40}', ident['head']) and ident['head'] != m['head'],
                'candidate must be committed descendant, not base')
        self.git('merge-base', '--is-ancestor', m['head'], ident['head'])
        require(not ident['status'], 'dirty or untracked candidate')
        changed = self.git('diff', '--name-only', m['head'], ident['head']).splitlines()
        require(set(changed) == set(AUTHORED), 'exclusive write set violated or missing')
        for relative in AUTHORED:
            committed = self.run(['git', 'show', ident['head'] + ':' + relative], label='head-bytes')
            require(committed.encode() == (REPO / relative).read_bytes(), 'HEAD bytes mismatch: ' + relative)
            indexed = self.run(['git', 'show', ':' + relative], label='index-bytes')
            require(indexed.encode() == (REPO / relative).read_bytes(), 'index bytes mismatch: ' + relative)
        self.report['file_hashes'] = {p: digest(REPO / p) for p in (*AUTHORED, *UNCHANGED)}
        for relative in UNCHANGED:
            raw = self.run(['git', 'show', m['head'] + ':' + relative], label='base-bytes')
            require(hashlib.sha256(raw.encode()).hexdigest() == digest(REPO / relative),
                    'baseline source changed: ' + relative)
        venv = REPO / '.venv'
        require(venv.is_symlink() and venv.resolve() == Path(m['runtime']['repo_venv']).resolve(),
                'standard venv link mismatch')
        expected_python = (Path(m['runtime']['credential_scan_python3']) if self.args.phase == 'credentials'
                           else REPO / '.venv' / 'bin' / 'python3')
        require(Path(sys.executable).absolute() == expected_python.absolute(), 'literal interpreter mismatch')
        require(Path(sys.prefix).resolve() == Path(m['runtime']['repo_venv']).resolve(), 'runtime prefix mismatch')
        self.report['runtime_binding'] = {'literal_expected_python': str(expected_python),
                                          'resolved_python': str(Path(sys.executable).resolve()),
                                          'venv_target': str(venv.resolve())}
        self.report['coverage_dimensions'] = case_dimensions()

    def tests(self, mode, label):
        destination = self.root / label
        stdout = self.run([sys.executable, REPO / RUNNER, '--child', mode,
                           '--temporary-root', destination], label=label)
        summary = json.loads((destination / 'summary.json').read_text())
        require(summary['successful'] and not summary['skipped'], 'child suite incomplete')
        if mode != 'compatibility':
            observations = [json.loads(line) for line in stdout.splitlines() if line.startswith('{')]
            observations = [row for row in observations if row.get('kind') == 'observation']
            ordinary = [row for row in observations if row['label'] == 'ordinary']
            dims = self.report['coverage_dimensions']
            require(len(ordinary) == dims['permutation_evaluations'], 'missing permutation observations')
            require({row['case'] for row in ordinary} == {c['id'] for c in dims['cases']}, 'missing case coverage')
            summary['observed_evaluations'] = len(ordinary)
        self.report[label] = summary
        return summary

    def portable(self):
        forbidden = re.compile(r'/(?:home|Users|workspace|tmp|var/tmp)/|[A-Za-z]:\\')
        require(all(not forbidden.search((REPO / p).read_text()) for p in AUTHORED),
                'host-specific literal in authored path')
        self.report['static_portable_paths'] = list(AUTHORED)
        foreign = self.root / 'foreign-cwd'
        foreign.mkdir()
        output = self.run([sys.executable, REPO / TEST, '--temporary-root', self.root / 'script-temporary', '-v'],
                          cwd=foreign, label='absolute-script')
        rows = [json.loads(line) for line in output.splitlines() if line.startswith('{')]
        ordinary = [row for row in rows if row.get('label') == 'ordinary']
        dims = self.report['coverage_dimensions']
        require(len(ordinary) == dims['permutation_evaluations'] and
                {row['case'] for row in ordinary} == {c['id'] for c in dims['cases']}, 'script case coverage mismatch')
        stderr = (self.root / self.commands[-1]['stderr']).read_text()
        count = re.search(r'Ran (\d+) tests?', stderr)
        require(count is not None and 'OK' in stderr, 'script test count missing')
        self.report['script'] = {'tests_run': int(count[1]), 'observed_evaluations': len(ordinary)}
        discovered = self.tests('discovery', 'discovery')
        require(discovered['tests_run'] == int(count[1]) and discovered['coverage'] == dims,
                'script/discovery coverage mismatch')

    def credentials(self):
        runtime = self.manifest['runtime']
        scanner = Path(runtime['detect_secrets'])
        require(scanner.is_file() and os.access(scanner, os.X_OK), 'scanner unavailable')
        # No dependency links are walked by this harness. The unchanged installed
        # scanner and repository CI arguments own scanning; failure is terminal.
        raw = self.run([scanner, 'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify'],
                       label='detect-secrets')
        data = json.loads(raw)
        require(isinstance(data.get('results'), dict), 'scanner did not produce genuine JSON')
        report_path = self.root / self.commands[-1]['stdout']
        self.run([sys.executable, REPO / 'tools/check_secret_report.py', report_path], label='secret-report-check')
        self.report['credential_scan'] = {'scanner': str(scanner), 'scanner_sha256': digest(scanner),
                                           'report': report_path.name, 'report_sha256': digest(report_path)}

    def public_tree(self):
        original = self.report['candidate']
        clone = self.root / 'committed-tree'
        self.run(['git', 'clone', '--local', '--no-hardlinks', '--no-checkout', '--no-recurse-submodules',
                  REPO, clone], label='committed-tree-clone')
        self.run(['git', 'checkout', '--detach', original['head']], cwd=clone, label='committed-tree-checkout')
        require(self.git('rev-parse', 'HEAD', cwd=clone) == original['head'] and
                self.git('rev-parse', 'HEAD^{tree}', cwd=clone) == original['tree'], 'clone identity mismatch')
        require(not (clone / '.venv').exists() and not (clone / '.venv').is_symlink(), 'runtime link in clone')
        hashes = {p: digest(clone / p) for p in (*AUTHORED, *UNCHANGED)}
        require(hashes == self.report['file_hashes'], 'clone checker/allowlist/source hashes mismatch')
        scanned = json.loads(self.run([sys.executable, clone / 'tools/check_public_tree.py'], cwd=clone,
                                     label='committed-tree-public-scan'))
        require(scanned.get('findings') == [] and scanned.get('scanned_files', 0) > 0 and
                scanned.get('scan_complete') is not False, 'committed-tree scan incomplete or findings')
        self.report['committed_tree'] = {'head': original['head'], 'tree': original['tree'],
                                         'hashes': hashes, 'scan': scanned}
        staged = json.loads(self.run([sys.executable, REPO / 'tools/check_public_tree.py'],
                                    label='in-place-informational', expected=(0, 1)))
        exit_code = self.commands[-1]['exit']
        allowed = (exit_code == 0 and staged.get('findings') == []) or (
            exit_code == 1 and staged.get('findings') == [['.venv', 0, 'symlink_not_scanned']])
        require(allowed and staged.get('scanned_files', 0) > 0 and staged.get('scan_complete') is not False,
                'unexpected staged scan outcome')
        self.report['in_place_informational'] = {'incident': 'INC-174', 'exit': exit_code, 'scan': staged,
                                                'not_a_staged_tree_pass': exit_code != 0}
        require(self.identity() == original, 'source identity/status changed during public scan')

    def execute(self):
        try:
            self.validate()
            phase = self.args.phase
            if phase in ('suite', 'compatibility'):
                self.tests(phase, phase)
            elif phase == 'portable':
                self.portable()
            elif phase == 'credentials':
                self.credentials()
            else:
                self.public_tree()
            require(self.identity() == self.report['candidate'], 'candidate changed during acceptance')
            require(digest(self.manifest_path) == self.report['manifest_sha256'], 'manifest changed during acceptance')
            require({p: digest(REPO / p) for p in (*AUTHORED, *UNCHANGED)} == self.report['file_hashes'],
                    'source bytes changed during acceptance')
            self.report['status'] = 'PASS_REVIEW_REQUIRED'
            code = 0
        except Exception as error:
            self.report['error'] = str(error)
            (self.root / 'failure.txt').write_text(traceback.format_exc())
            code = 1
        self.report['artifacts'] = {str(p.relative_to(self.root)): digest(p)
                                    for p in sorted(self.root.rglob('*'))
                                    if p.is_file() and 'committed-tree' not in p.relative_to(self.root).parts}
        dump(self.root / 'receipt.json', self.report)
        print(json.dumps({'status': self.report['status'], 'exit': code, 'receipt': str(self.root / 'receipt.json'),
                          'receipt_sha256': digest(self.root / 'receipt.json'),
                          'candidate': self.report.get('candidate'), 'error': self.report.get('error')}, sort_keys=True))
        return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('suite', 'compatibility', 'portable', 'credentials', 'public-tree'))
    parser.add_argument('--source-manifest', type=Path)
    parser.add_argument('--out-base', type=Path)
    parser.add_argument('--child', choices=('suite', 'compatibility', 'discovery'))
    parser.add_argument('--temporary-root', type=Path)
    args = parser.parse_args()
    if args.child:
        require(args.temporary_root is not None and args.phase is None, 'child argument mismatch')
        return child(args.child, args.temporary_root)
    require(args.phase is not None and args.source_manifest is not None and args.out_base is not None,
            'phase, source manifest and output base required')
    return Acceptance(args).execute()


if __name__ == '__main__':
    raise SystemExit(main())
