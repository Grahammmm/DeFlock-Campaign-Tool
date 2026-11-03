"""Five separate offline acceptance rows. No install, live IO or publication.

Child argv always use shell=False. Each invocation preserves unique raw output;
committed-tree cloning is local scratch only, never a deployment/export.
"""
import argparse
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest.mock import patch
import uuid
from contextlib import closing

REPO = Path(__file__).resolve().parents[1]

AUTHORED = ['tests/records/test_fc9_rows_detector_handoff.py',
            'tests/fixtures/fc9-rows-detector-handoff/cases.json',
            'tools/fc9_rows_detector_handoff_acceptance.py']
TEST_MODULE = 'tests.records.test_fc9_rows_detector_handoff'
PINS = ['campaign_tool/records/understanding/rows.py',
        'campaign_tool/records/detectors/pipeline.py',
        'tests/test_understanding_rows.py',
        'tests/records/test_structured_detector_pipeline.py']
CHECKERS = ['tools/check_public_tree.py', 'tools/check_secret_report.py',
            'campaign_tool/records/public_scan.py']


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def file_hashes(repo, paths):
    return {p: sha((repo / p).read_bytes()) for p in paths}


def child(mode, row):
    """No permanent hooks or environment overrides; child safety before imports."""
    row.mkdir(mode=0o700, parents=True, exist_ok=False)
    temporary = row / 'temporary'
    temporary.mkdir(mode=0o700)
    tempfile.tempdir = str(temporary)
    sys.path.insert(0, str(REPO))
    stream = io.StringIO()
    before_env = dict(os.environ)
    before_temp = tempfile.tempdir
    before_path = list(sys.path)
    before_audit = sys.addaudithook
    results = []
    with ExitStack() as stack:
        def forbidden(*args, **kwargs):
            raise RuntimeError('offline fixture forbids network/process access')
        stack.enter_context(patch.object(socket.socket, 'connect', forbidden))
        stack.enter_context(patch.object(socket, 'create_connection', forbidden))
        stack.enter_context(patch.object(subprocess, 'Popen', forbidden))
        real_connect = sqlite3.connect
        def memory_only(database, *args, **kwargs):
            require(database == ':memory:', 'synthetic in-memory SQLite only')
            return real_connect(database, *args, **kwargs)
        stack.enter_context(patch.object(sqlite3, 'connect', memory_only))
        class Sentinel(unittest.TestCase):
            def runTest(self):
                self.assertEqual(dict(os.environ), before_env)
                self.assertEqual(tempfile.tempdir, before_temp)
                self.assertEqual(sys.path, before_path)
                self.assertIs(sys.addaudithook, before_audit)
                self.assertTrue(tempfile.gettempdir() == before_temp)
                with closing(sqlite3.connect(':memory:')) as con, con:
                    self.assertEqual(con.execute('SELECT 7').fetchone(), (7,))
        runner = unittest.TextTestRunner(stream=stream, verbosity=2)
        if mode == 'discovery':
            results.append(runner.run(unittest.TestSuite([Sentinel()])))
            suite = unittest.defaultTestLoader.discover(str(REPO / 'tests' / 'records'),
                pattern='test_fc9_rows_detector_handoff.py', top_level_dir=str(REPO))
            # Assert the import itself is inert, before setUp/tearDown can hide it.
            results.append(runner.run(unittest.TestSuite([Sentinel()])))
        elif mode == 'compatibility':
            suite = unittest.defaultTestLoader.loadTestsFromNames([
                'tests.test_understanding_rows.RowTests',
                'tests.records.test_structured_detector_pipeline.StructuredPipelineTests'])
        else:
            suite = unittest.defaultTestLoader.loadTestsFromName(TEST_MODULE)
        results.append(runner.run(suite))
        if mode == 'discovery':
            results.append(runner.run(unittest.TestSuite([Sentinel()])))
    print(stream.getvalue(), file=sys.stderr)
    report = {'mode': mode, 'tests_run': sum(r.testsRun for r in results),
              'failures': sum(len(r.failures) for r in results),
              'errors': sum(len(r.errors) for r in results),
              'skipped': sum(len(r.skipped) for r in results),
              'successful': all(r.wasSuccessful() and not r.skipped for r in results)}
    write_json(row / 'unittest.json', report)
    require(report['successful'], 'unittest failure; retained raw output')
    return 0


class Row:
    def __init__(self, args):
        self.args = args
        base = Path(args.out_base)
        require(base.is_absolute() and '..' not in base.parts, 'literal absolute out-base required')
        require(not any(p.is_symlink() for p in (base, *base.parents)), 'output ancestry symlink')
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = base / (args.phase + '-' + str(uuid.uuid4()))
        self.root.mkdir(mode=0o700)
        temporary = self.root / 'temporary'
        temporary.mkdir(mode=0o700)
        tempfile.tempdir = str(temporary)
        self.receipt = {'schema': 'fc9-acceptance/v1', 'phase': args.phase,
            'argv': sys.argv, 'cwd': str(Path.cwd()), 'interpreter': sys.executable,
            'prefix': sys.prefix, 'run_root': str(self.root), 'commands': [],
            'acceptance': 'REVIEW REQUIRED', 'remote_ci': 'OWED',
            'semantic_legal_human_live_acceptance': False}
        self.sequence = 0

    def run(self, label, argv, cwd=REPO, allowed=(0,)):
        self.sequence += 1
        stem = str(self.sequence).zfill(2) + '-' + label
        result = subprocess.run([str(a) for a in argv], cwd=cwd, shell=False,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = self.root / (stem + '.stdout'), self.root / (stem + '.stderr')
        out.write_bytes(result.stdout)
        err.write_bytes(result.stderr)
        entry = {'label': label, 'argv': [str(a) for a in argv], 'cwd': str(cwd),
                 'shell': False, 'exit': result.returncode,
                 'stdout': str(out), 'stderr': str(err),
                 'stdout_sha256': sha(result.stdout), 'stderr_sha256': sha(result.stderr)}
        self.receipt['commands'].append(entry)
        write_json(self.root / 'receipt.json', self.receipt)
        require(result.returncode in allowed, label + ': unexpected exit ' + str(result.returncode))
        return result.stdout

    def git(self, *argv, cwd=REPO):
        return self.run('git', ['git', *argv], cwd=cwd).decode().strip()

    def identity(self):
        head = self.git('rev-parse', 'HEAD')
        tree = self.git('rev-parse', 'HEAD^{tree}')
        require(re.fullmatch('[0-9a-f]{40}', head) and re.fullmatch('[0-9a-f]{40}', tree), 'invalid git identity')
        status = self.git('status', '--porcelain=v1', '--untracked-files=all')
        paths_raw = self.run('path-inventory', ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'])
        paths = sorted(set(paths_raw.decode().split('\0')) - {''})
        untracked_raw = self.run('untracked', ['git', 'ls-files', '-z', '--others', '--exclude-standard'])
        untracked = set(untracked_raw.decode().split('\0')) - {''}
        require(not (untracked - {'.venv'}), 'untracked scan-visible files')
        require(status in ('', '?? .venv'), 'candidate tracked/index bytes changed')
        for p in AUTHORED:
            committed = self.run('head-bytes', ['git', 'show', head + ':' + p])
            indexed = self.run('index-bytes', ['git', 'show', ':' + p])
            require(committed == indexed == (REPO / p).read_bytes(), 'authored file not committed: ' + p)
        return {'head': head, 'tree': tree, 'status': status, 'paths': paths,
                'hashes': file_hashes(REPO, [*AUTHORED, *PINS, *CHECKERS])}

    def validate(self):
        path = Path(self.args.source_manifest)
        require(path.is_absolute() and path.is_file() and not path.is_symlink(), 'literal manifest required')
        raw = path.read_bytes()
        m = json.loads(raw)
        require(m['schema'] == 'agentorange.source-manifest/v1', 'manifest schema')
        base = m['head']
        require(re.fullmatch('[0-9a-f]{40}', base) and m['repo'] == str(REPO), 'staging repo/base mismatch')
        require(m['write_root'] == str(REPO.parent), 'staging write root mismatch')
        require(self.git('rev-parse', base + '^{tree}') == m['tree'], 'staging base tree mismatch')
        require(self.git('remote', 'get-url', 'origin') == m['remote'], 'staging remote mismatch')
        runtime = m['runtime']
        require((REPO / '.venv').is_symlink() and (REPO / '.venv').resolve() == Path(runtime['repo_venv']).resolve(), 'staged venv mismatch')
        expected_python = Path(runtime['credential_scan_python3']) if self.args.phase == 'credentials' else REPO / '.venv/bin/python3'
        # Both venv python symlinks may resolve to the system binary; check the
        # literal invocation and prefix as well, not just that shared binary.
        require(os.path.abspath(sys.executable) == str(expected_python), 'wrong literal interpreter')
        require(Path(sys.prefix).resolve() == Path(runtime['repo_venv']).resolve(), 'runtime prefix mismatch')
        pins = {p: sha(self.run('base-source', ['git', 'show', base + ':' + p])) for p in PINS}
        require(file_hashes(REPO, PINS) == pins, 'pinned product/compatibility source changed')
        identity = self.identity()
        require(identity['head'] != base, 'final acceptance requires committed candidate')
        self.git('merge-base', '--is-ancestor', base, identity['head'])
        changed = self.git('diff', '--name-only', base, identity['head']).splitlines()
        require(set(changed) == set(AUTHORED), 'write-set mismatch')
        message = self.git('log', '-1', '--format=%B')
        require('Signed-off-by:' in message and 'gpt-6.1-sol' in message and 'openai-codex' in message, 'author provenance/signoff missing')
        self.receipt.update(source_manifest_sha256=sha(raw), identity=identity,
                            fixture_sha256=identity['hashes'][AUTHORED[1]], source_pins=pins)
        self.manifest = m
        return identity

    def suite(self, mode='suite'):
        self.run(mode, [sys.executable, Path(__file__).resolve(), '--child', mode,
                       '--row-root', self.root / (mode + '-child')])
        report = json.loads((self.root / (mode + '-child') / 'unittest.json').read_text())
        require(report['successful'] and report['skipped'] == 0, 'missing successful test coverage')
        require(report['tests_run'] == (23 if mode == 'suite' else 41), 'unexpected test count')
        self.receipt['unittest'] = report
        if mode == 'suite':
            self.receipt['case_ids'] = self.case_ids(self.receipt['commands'][-1]['stdout'])

    def case_ids(self, stdout):
        ids = [json.loads(line[len('FC9_CASE '):])['id']
               for line in Path(stdout).read_text().splitlines() if line.startswith('FC9_CASE ')]
        fixture = json.loads((REPO / AUTHORED[1]).read_text())
        expected = sorted(d['name'] + '/' + v for d in fixture['detectors'] for v in fixture['variants'])
        require(sorted(ids) == expected, 'required case coverage missing or duplicated')
        return sorted(ids)

    def portable(self):
        foreign = self.root / 'foreign-cwd'
        foreign.mkdir(mode=0o700)
        script_tmp = self.root / 'script-temporary'
        script_tmp.mkdir(mode=0o700)
        self.run('absolute-script', [sys.executable, REPO / AUTHORED[0],
            '--temporary-root', script_tmp, '-v'], cwd=foreign)
        script_entry = self.receipt['commands'][-1]
        script_cases = self.case_ids(script_entry['stdout'])
        raw_err = Path(script_entry['stderr']).read_text()
        require('Ran 23 tests' in raw_err and raw_err.rstrip().endswith('OK'), 'script test count/result')
        self.run('discovery', [sys.executable, Path(__file__).resolve(), '--child', 'discovery',
            '--row-root', self.root / 'discovery-child'], cwd=foreign)
        discovery_cases = self.case_ids(self.receipt['commands'][-1]['stdout'])
        report = json.loads((self.root / 'discovery-child/unittest.json').read_text())
        require(report['successful'] and report['tests_run'] == 26 and report['skipped'] == 0, 'discovery/sentinel failure')
        require(script_cases == discovery_cases, 'portable case coverage mismatch')
        # Scan all authored payloads for box-only literal paths, no environment exemption.
        pattern = re.compile(rb'/(?:home|Users|workspace|tmp|var/tmp)/')
        require(all(not pattern.search((REPO / p).read_bytes()) for p in AUTHORED), 'host-specific authored literal')
        self.receipt.update(case_ids=script_cases, script_tests=23, discovery=report,
                            static_host_literal_check='PASS')

    def credentials(self):
        runtime = self.manifest['runtime']
        scanner = Path(runtime['detect_secrets'])
        require(scanner.is_absolute() and scanner.is_file(), 'installed scanner missing')
        # The executable wrapper must bind the same full-suite interpreter.
        require(scanner.read_text().splitlines()[0] == '#!' + runtime['credential_scan_python3'], 'scanner interpreter mismatch')
        raw = self.run('detect-secrets', [scanner, 'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify'])
        report = self.root / 'secret-report.json'
        report.write_bytes(raw)
        parsed = json.loads(raw)
        require(isinstance(parsed.get('results'), dict), 'scanner report shape')
        self.run('check-secret-report', [sys.executable, REPO / 'tools/check_secret_report.py', report])
        self.receipt['scanner_report_sha256'] = sha(raw)

    def public_tree(self, identity):
        destination = self.root / 'committed-tree'
        self.run('local-clone', ['git', 'clone', '--local', '--no-hardlinks', '--no-checkout',
            '--no-recurse-submodules', REPO, destination])
        self.run('detached-checkout', ['git', 'checkout', '--detach', identity['head']], cwd=destination)
        require(self.git('rev-parse', 'HEAD', cwd=destination) == identity['head'], 'clone head drift')
        require(self.git('rev-parse', 'HEAD^{tree}', cwd=destination) == identity['tree'], 'clone tree drift')
        compare = [*AUTHORED, *CHECKERS]
        # Existing allowlists live in the unchanged checker source itself.
        require(file_hashes(destination, compare) == file_hashes(REPO, compare), 'clone payload/checker/allowlist drift')
        require(not (destination / '.venv').exists() and not (destination / '.venv').is_symlink(), 'clone runtime path forbidden')
        raw = self.run('committed-public-tree', [sys.executable, destination / 'tools/check_public_tree.py'], cwd=destination)
        parsed = json.loads(raw)
        require(parsed.get('findings') == [] and parsed.get('scanned_files', 0) > 0, 'committed tree not clear')
        in_place_raw = self.run('in_place_informational-INC-174', [sys.executable, REPO / 'tools/check_public_tree.py'], allowed=(0, 1))
        in_place = json.loads(in_place_raw)
        findings = in_place.get('findings')
        code = self.receipt['commands'][-1]['exit']
        require((code == 0 and findings == []) or
                (code == 1 and findings == [['.venv', 0, 'symlink_not_scanned']]), 'unexpected staged diagnostic')
        require(in_place.get('scanned_files') == parsed['scanned_files'], 'incomplete staged scan')
        self.receipt.update(committed_tree=parsed,
            in_place_informational={'label': 'INC-174', 'exit': code, 'result': in_place,
                                   'staged_tree_pass': code == 0})

    def finish(self):
        artifacts = {}
        for path in sorted(self.root.rglob('*')):
            if path.is_file() and '.git' not in path.relative_to(self.root).parts and 'committed-tree' not in path.relative_to(self.root).parts and path.name != 'receipt.json':
                artifacts[str(path.relative_to(self.root))] = sha(path.read_bytes())
        self.receipt['artifact_sha256'] = artifacts
        write_json(self.root / 'receipt.json', self.receipt)
        print(json.dumps({'phase': self.args.phase, 'exit': 0 if self.receipt.get('passed') else 1,
                          'receipt': str(self.root / 'receipt.json'),
                          'receipt_sha256': sha((self.root / 'receipt.json').read_bytes()),
                          'tests': self.receipt.get('unittest'), 'case_ids': self.receipt.get('case_ids')}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=['suite', 'compatibility', 'portable', 'credentials', 'public-tree'])
    parser.add_argument('--source-manifest')
    parser.add_argument('--out-base')
    parser.add_argument('--child', choices=['suite', 'compatibility', 'discovery'])
    parser.add_argument('--row-root', type=Path)
    args = parser.parse_args()
    if args.child:
        require(args.row_root is not None and args.row_root.is_absolute(), 'child row-root required')
        return child(args.child, args.row_root)
    require(args.phase and args.source_manifest and args.out_base, 'phase/manifest/out-base required')
    row = Row(args)
    try:
        identity = row.validate()
        if args.phase in ('suite', 'compatibility'):
            row.suite(args.phase)
        elif args.phase == 'portable':
            row.portable()
        elif args.phase == 'credentials':
            row.credentials()
        else:
            row.public_tree(identity)
        require(row.identity() == identity, 'source identity changed during acceptance')
        row.receipt['passed'] = True
        return 0
    except Exception as error:
        row.receipt.update(passed=False, error_type=type(error).__name__, error=str(error))
        traceback.print_exc()
        return 1
    finally:
        row.finish()


if __name__ == '__main__':
    raise SystemExit(main())
