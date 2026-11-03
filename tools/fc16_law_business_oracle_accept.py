"""Five bounded offline acceptance rows for FC16-LAW-BUSINESS-ORACLE.

Receipts are new UUID directories outside the repository. This recorder does
not install runtimes, acquire sources, change product code or grant release.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
TEST = 'tests/records/test_fc16_law_business_oracle.py'
FIXTURE = 'tests/fixtures/fc16_law_business_oracle/cases.json'
RUNNER = 'tools/fc16_law_business_oracle_accept.py'
AUTHORED = (TEST, FIXTURE, RUNNER)
SOURCE = ('campaign_tool/__init__.py', 'campaign_tool/law.py', 'tests/test_law.py',
          'tools/check_public_tree.py', 'campaign_tool/records/__init__.py',
          'campaign_tool/records/public_scan.py', 'tools/check_secret_report.py')
CHECKERS = ('tools/check_public_tree.py', 'campaign_tool/records/public_scan.py',
            'tools/check_secret_report.py')


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    return sha(path.read_bytes())


def regular(path):
    path = path.absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink in source/evidence path')
    require(path.is_file(), 'missing regular file: ' + str(path))
    return path


class Recorder:
    def __init__(self, args):
        self.args = args
        self.repo = Path(args.repo).absolute()
        require(self.repo == ROOT, 'recorder must run from its own repository')
        self.out_base = Path(args.out_base).absolute()
        require(self.repo not in (self.out_base, *self.out_base.parents), 'output inside repository')
        require(self.out_base == self.repo.parent / 'out', 'output must be the staged fresh evidence directory')
        require(not any(p.is_symlink() for p in (self.out_base, *self.out_base.parents)), 'output ancestry symlink')
        self.out = self.out_base / (args.phase + '-' + uuid.uuid4().hex)
        self.out.mkdir(parents=True, exist_ok=False)
        self.receipt = {'schema': 'fc16.law-business-oracle.acceptance/v1', 'phase': args.phase,
                        'argv': [sys.executable, *sys.argv], 'cwd': str(Path.cwd()),
                        'interpreter': sys.executable, 'commands': [], 'status': 'INCOMPLETE',
                        'limits': 'Offline machine qualification only; REVIEW REQUIRED. No legal, factual, delivery or timeliness acceptance. Full repository CI and transport remain owed to the shipping operator.'}
        self.manifest_path = regular(Path(args.manifest))
        require(self.manifest_path == self.repo.parent / 'SOURCE-MANIFEST.json', 'unexpected source manifest')
        self.manifest_bytes = self.manifest_path.read_bytes()
        self.manifest = json.loads(self.manifest_bytes)
        self.receipt['source_manifest_sha256'] = sha(self.manifest_bytes)
        self.receipt['source_manifest'] = self.manifest

    def command(self, label, argv, cwd=None, expected=(0,)):
        cwd = self.repo if cwd is None else Path(cwd)
        command = {'label': label, 'argv': list(map(str, argv)), 'cwd': str(cwd), 'complete': False}
        self.receipt['commands'].append(command)
        prefix = f'{len(self.receipt["commands"]):03d}-{label}'
        try:
            child = subprocess.run(command['argv'], cwd=cwd, shell=False, capture_output=True, timeout=180)
            command.update(exit=child.returncode, complete=True)
            stdout, stderr = child.stdout, child.stderr
        except subprocess.TimeoutExpired as error:
            stdout, stderr = error.stdout or b'', error.stderr or b''
            command.update(error='child timeout', exit=None)
        for kind, data in (('stdout', stdout), ('stderr', stderr)):
            path = self.out / (prefix + '.' + kind)
            path.write_bytes(data)
            command[kind] = {'path': str(path), 'sha256': sha(data), 'bytes': len(data)}
        require(command['complete'], label + ': incomplete child')
        if expected is not None:
            require(command['exit'] in expected, label + ': unexpected exit ' + str(command['exit']))
        return command, stdout

    def git(self, *args, cwd=None):
        _, raw = self.command('git', ['git', *args], cwd=cwd)
        return raw.decode().strip()

    def identity(self):
        return {'head': self.git('rev-parse', 'HEAD'), 'tree': self.git('rev-parse', 'HEAD^{tree}'),
                'status': self.git('status', '--porcelain=v1', '--untracked-files=all'),
                'authored_sha256': {p: file_sha(regular(self.repo / p)) for p in AUTHORED},
                'source_sha256': {p: file_sha(regular(self.repo / p)) for p in SOURCE}}

    def preflight(self):
        m = self.manifest
        require(m['schema'] == 'agentorange.source-manifest/v1', 'source manifest schema')
        require(Path(m['repo']) == self.repo and Path(m['write_root']) == self.repo.parent, 'manifest roots')
        require(m['card'] == 'deflock-FC16-LAW-BUSINESS-ORACLE', 'manifest card')
        require(re.fullmatch('[a-f0-9]{40}', m['head']) is not None, 'source head syntax')
        require(re.fullmatch('[a-f0-9]{40}', m['tree']) is not None, 'source tree syntax')
        require(self.git('rev-parse', m['head'] + '^{tree}') == m['tree'], 'initial tree mismatch')
        self.git('merge-base', '--is-ancestor', m['head'], 'HEAD')
        runtime = m['runtime']
        link = self.repo / '.venv'
        require(link.is_symlink() and link.resolve() == Path(runtime['repo_venv']).resolve(), 'standard runtime link')
        python = Path(runtime['credential_scan_python3']) if self.args.phase == 'credentials' else link / 'bin/python3'
        require(Path(sys.executable).absolute() == python.absolute(), 'wrong row interpreter')
        require(Path(runtime['detect_secrets']).parent == Path(runtime['credential_scan_python3']).parent,
                'scanner not from credential runtime')
        require(Path(runtime['detect_secrets']).is_file(), 'scanner missing')
        for path in SOURCE:
            _, original = self.command('baseline-source', ['git', 'show', m['head'] + ':' + path])
            require(file_sha(regular(self.repo / path)) == sha(original), 'source/checker/allowlist drift: ' + path)
        # Bound the complete candidate diff and scan-visible paths before imports.
        changed = self.git('diff', '--name-only', m['head'], '--').splitlines()
        require(set(changed) <= set(AUTHORED), 'out-of-scope candidate diff')
        untracked = self.git('ls-files', '--others', '--exclude-standard').splitlines()
        require(set(untracked) <= set(AUTHORED) | {'.venv'}, 'untracked scan-visible entry')
        self.receipt['initial_identity'] = self.identity()
        _, diff = self.command('complete-diff', ['git', 'diff', '--binary', m['head'], '--'])
        self.receipt['complete_diff_sha256'] = sha(diff)
        self.receipt['authored_inventory'] = list(AUTHORED)
        self.receipt['runtime_link'] = {'path': '.venv', 'target': os.readlink(link)}
        self.receipt['baseline'] = self.receipt['initial_identity']['head'] == m['head']
        self.receipt['material_author'] = {'model': 'gpt-6.1-sol', 'provider': 'openai-codex',
                                           'profile': 'sol-reviewer', 'admission': 't_a6ec8643 operator admission 2026-10-25 07:11 PDT'}

    def tests(self, label, mode, cwd):
        report = self.out / (label + '.json')
        temporary = self.out / (label + '-temporary')
        argv = [sys.executable, '-B', str(self.repo / TEST), '--temporary-root', str(temporary),
                '--report', str(report), '--mode', mode]
        self.command(label, argv, cwd)
        data = json.loads(regular(report).read_text())
        require(data['success'] and data['skips'] == 0 and data['errors'] == 0 and data['failures'] == 0,
                label + ': failed or skipped test')
        require(all(e['status'] == 'pass' for e in data['events']), label + ': incomplete cases')
        if mode == 'compatibility':
            require(data['tests'] == 7, 'compatibility selector count changed')
            require(all(e['id'].startswith('tests.test_law.LawFunctionTests.') for e in data['events']),
                    'wrong compatibility selector')
        else:
            require(data['tests'] == 6, label + ': required named tests missing')
            require(data['subcases'] == 1221, label + ': complete subcase count changed')
            required = set(data['case_map'].values())
            actual = {'.'.join(e['id'].split('.')[-2:]) for e in data['events'] if e['kind'] == 'test'}
            require(actual == required, 'case-to-test coverage missing')
            # Bind all literal matrix IDs to actually executed subcases.
            fixture = json.loads((self.repo / FIXTURE).read_text())
            ids = {r['id'] for r in fixture['cases']}
            observed = set()
            for event in data['events']:
                if '.test_all_weekdays ' in event['id']:
                    match = re.search(r"\(case='([^']+)'\)$", event['id'])
                    if match is None:
                        raise ValueError('matrix subcase ID malformed')
                    observed.add(match[1])
            require(len(ids) == 448 and observed == ids, 'frozen matrix missing or duplicated')
            require({c['mutant'] for c in data['controls']} == {'counts_start', 'saturday_business'},
                    'deciding controls missing')
            require(all(c['restored_positive'] and 'frozen-date' in c['failure'] for c in data['controls']),
                    'control did not hit exact assertion and restore')
        self.receipt.setdefault('test_reports', []).append({'path': str(report), 'sha256': file_sha(report),
                                                           'tests': data['tests'], 'subcases': data['subcases'],
                                                           'skips': data['skips'], 'case_map': data['case_map'],
                                                           'controls': data['controls'], 'peer_sentinels': data['peer_sentinels']})

    def suite(self):
        self.tests('suite', 'suite', self.repo)

    def compatibility(self):
        self.receipt['selector'] = 'tests.test_law.LawFunctionTests'
        self.tests('compatibility', 'compatibility', self.repo)

    def portable(self):
        unrelated = self.out / 'unrelated-cwd'
        unrelated.mkdir()
        self.tests('absolute-script', 'suite', unrelated)
        self.tests('dedicated-discovery', 'discovery', unrelated)
        # Inspect only authored public text, without traversing runtime links.
        for name in AUTHORED:
            text = regular(self.repo / name).read_text()
            for pattern in (r'/(?:home|Users)/[A-Za-z0-9_.-]+', r'[A-Z]:\\[A-Za-z]'):
                require(re.search(pattern, text) is None, 'machine-specific path in ' + name)
        self.receipt['machine_path_inspection'] = 'all three authored files: no machine-specific path'

    def credentials(self):
        runtime = self.manifest['runtime']
        command, raw = self.command('detect-secrets', [runtime['detect_secrets'], 'scan', '--all-files',
                                                      '--exclude-files', '^.git/', '--no-verify'])
        report = Path(command['stdout']['path'])
        data = json.loads(raw)
        require(isinstance(data.get('results'), dict), 'invalid scanner JSON')
        self.command('check-secret-report', [runtime['credential_scan_python3'], '-B',
                                             str(self.repo / 'tools/check_secret_report.py'), str(report)])
        self.receipt['scanner_report'] = {'path': str(report), 'sha256': file_sha(report),
                                         'hit_count': sum(len(hits) for hits in data['results'].values())}

    def public_tree(self):
        identity = self.receipt['initial_identity']
        require(not self.git('diff', '--name-only', 'HEAD', '--'), 'uncommitted tracked content')
        require(not self.git('diff', '--cached', '--name-only', '--'), 'index not at HEAD')
        for name in AUTHORED:
            self.git('ls-files', '--error-unmatch', '--', name)
            _, head_bytes = self.command('head-authored', ['git', 'show', 'HEAD:' + name])
            _, index_bytes = self.command('index-authored', ['git', 'show', ':' + name])
            require(sha(head_bytes) == sha(index_bytes) == file_sha(self.repo / name), 'authored bytes not at HEAD')
        self.receipt['path_inventory'] = self.git('ls-files', '--cached', '--others', '--exclude-standard').splitlines()
        clone = self.out / 'committed-tree'
        self.command('clone', ['git', 'clone', '--local', '--no-hardlinks', '--no-checkout',
                                '--no-recurse-submodules', str(self.repo), str(clone)], self.out)
        self.command('checkout', ['git', 'checkout', '--detach', identity['head']], clone)
        require(self.git('rev-parse', 'HEAD', cwd=clone) == identity['head'], 'clone HEAD mismatch')
        require(self.git('rev-parse', 'HEAD^{tree}', cwd=clone) == identity['tree'], 'clone tree mismatch')
        require(not (clone / '.venv').exists(), 'runtime link in committed clone')
        for name, expected in identity['authored_sha256'].items():
            require(file_sha(regular(clone / name)) == expected, 'clone authored hash mismatch')
        for name in CHECKERS:
            require(file_sha(regular(clone / name)) == identity['source_sha256'][name], 'clone checker/allowlist mismatch')
        _, raw = self.command('committed-tree-check', [sys.executable, '-B', str(clone / 'tools/check_public_tree.py')], clone)
        data = json.loads(raw)
        require(isinstance(data.get('scanned_files'), int) and data.get('findings') == [], 'committed-tree findings or invalid report')
        self.receipt['committed_tree'] = {'path': str(clone), 'head': identity['head'], 'tree': identity['tree'], 'report': data}
        command, raw = self.command('in-place-informational', [sys.executable, '-B', str(self.repo / 'tools/check_public_tree.py')],
                                    expected=(0, 1))
        data = json.loads(raw)
        require(isinstance(data.get('scanned_files'), int), 'malformed in-place report')
        allowed = [] if command['exit'] == 0 else [['.venv', 0, 'symlink_not_scanned']]
        require(data.get('findings') == allowed, 'unrecognized in-place finding')
        self.receipt['in_place_informational'] = {'exit': command['exit'], 'report': data, 'incident': 'INC-174'}

    def finish(self):
        self.receipt['final_identity'] = self.identity()
        require(self.receipt['initial_identity'] == self.receipt['final_identity'], 'identity changed during row')
        require(self.manifest_path.read_bytes() == self.manifest_bytes, 'source manifest changed')
        for command in self.receipt['commands']:
            require(command['complete'], 'unfinished evidence child')
            for kind in ('stdout', 'stderr'):
                item = command[kind]
                path = regular(Path(item['path']))
                require(file_sha(path) == item['sha256'] and path.stat().st_size == item['bytes'], 'raw output changed')
        self.receipt['status'] = 'PASS_REVIEW_REQUIRED'

    def save(self):
        path = self.out / 'receipt.json'
        path.write_text(json.dumps(self.receipt, indent=2) + '\n')
        print(json.dumps({'phase': self.args.phase, 'status': self.receipt['status'],
                          'receipt': str(path), 'sha256': file_sha(path)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', required=True, choices=('suite', 'compatibility', 'portable', 'credentials', 'public-tree'))
    parser.add_argument('--repo', required=True)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--out-base', required=True)
    args = parser.parse_args()
    recorder = None
    try:
        recorder = Recorder(args)
        recorder.preflight()
        {'suite': recorder.suite, 'compatibility': recorder.compatibility, 'portable': recorder.portable,
         'credentials': recorder.credentials, 'public-tree': recorder.public_tree}[args.phase]()
        recorder.finish()
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        if recorder is not None:
            recorder.receipt.update(status='FAIL', error=str(error))
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if recorder is not None:
            recorder.save()


if __name__ == '__main__':
    raise SystemExit(main())
