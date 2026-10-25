"""Five bounded offline FC16 acceptance rows; each preserves fresh raw evidence.

No product imports here. Runtime and unchanged-source checks precede children.
The test child installs scoped sentinels before importing the actual product.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
import uuid

OWNED = ('tests/records/test_fc16_document_summary_segments.py',
         'tests/fixtures/fc16_document_summary_segments/cases.json',
         'tools/fc16_document_summary_segments_accept.py')
CHECKERS = ('tools/check_public_tree.py', 'campaign_tool/records/public_scan.py',
            'tools/check_secret_report.py')
PINS = ('campaign_tool/records/understanding/document.py',
        'campaign_tool/records/understanding/evidence.py',
        'campaign_tool/records/grounded_evidence.py', 'tests/records/test_understanding.py')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def need(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def no_symlinks(path):
    need(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink path: '+str(path))


class Recorder:
    def __init__(self, args):
        self.args = args
        self.repo = args.repo.absolute()
        self.manifest_path = args.manifest.absolute()
        no_symlinks(self.repo)
        no_symlinks(self.manifest_path)
        self.manifest_raw = self.manifest_path.read_bytes()
        self.manifest = json.loads(self.manifest_raw)
        need(self.manifest['schema'] == 'agentorange.source-manifest/v1', 'manifest schema')
        need(Path(self.manifest['repo']) == self.repo == Path(__file__).resolve().parents[1], 'repo identity')
        # Admission pins live in the operator-supplied source manifest, whose
        # whole-file digest is bound in each receipt. Do not duplicate public
        # high-entropy identities into scanner-visible authored source.
        self.base = self.manifest['head']
        self.base_tree = self.manifest['tree']
        need(self.manifest['card'] == 'deflock-FC16-DOCUMENT-SUMMARY-SEGMENTS', 'admitted card identity')
        need(re.fullmatch(r'[0-9a-f]{40}', self.base) is not None and
             re.fullmatch(r'[0-9a-f]{40}', self.base_tree) is not None, 'admitted base identity format')
        root = Path(self.manifest['write_root'])
        need(self.repo == root/'repo' and self.manifest_path == root/'SOURCE-MANIFEST.json', 'staging layout')
        self.out_base = args.out_base.absolute()
        need(self.out_base == root/'out', 'out-base must be exclusive admitted evidence root')
        no_symlinks(self.out_base)
        self.out_base.mkdir(parents=True, exist_ok=True)
        self.out = self.out_base/(args.phase+'-'+str(uuid.uuid4()))
        self.out.mkdir(mode=0o700)
        self.steps = []
        self.data = {'schema': 'fc16-summary-segments-accept/v1', 'phase': args.phase,
                     'literal_invocation': [sys.executable, *sys.argv], 'cwd': str(Path.cwd()),
                     'interpreter': sys.executable, 'manifest_path': str(self.manifest_path),
                     'manifest_sha256': digest(self.manifest_raw), 'manifest': self.manifest,
                     'output': str(self.out), 'steps': self.steps, 'status': 'INCOMPLETE',
                     'limits': 'Offline synthetic qualification; REVIEW REQUIRED. Full repository CI and shipping remain OWED to the existing operator. No semantic/legal acceptance or hold release.'}
        self.save()

    def save(self):
        (self.out/'row.json').write_text(json.dumps(self.data, indent=2)+'\n', encoding='utf-8')

    def run(self, label, argv, cwd=None, allowed=(0,), timeout=180):
        argv = [str(a) for a in argv]
        cwd = Path(cwd or self.repo)
        entry = {'label': label, 'argv': argv, 'cwd': str(cwd), 'shell': False,
                 'complete': False, 'allowed_exits': list(allowed)}
        self.steps.append(entry)
        self.save()
        try:
            p = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout, shell=False)
            stdout, stderr, code = p.stdout, p.stderr, p.returncode
        except subprocess.TimeoutExpired as error:
            stdout, stderr, code = error.stdout or b'', error.stderr or b'', None
        out = self.out/(label+'.stdout')
        err = self.out/(label+'.stderr')
        out.write_bytes(stdout)
        err.write_bytes(stderr)
        entry.update({'exit': code, 'complete': code is not None,
                      'stdout': str(out), 'stdout_sha256': digest(stdout),
                      'stderr': str(err), 'stderr_sha256': digest(stderr)})
        self.save()
        need(code in allowed, 'child failed: '+label+' exit '+str(code))
        return stdout

    def git(self, label, *argv, cwd=None):
        return self.run(label, ['git', *argv], cwd=cwd)

    def identity(self, label):
        head = self.git(label+'-head', 'rev-parse', 'HEAD').decode().strip()
        tree = self.git(label+'-tree', 'rev-parse', 'HEAD^{tree}').decode().strip()
        status = self.git(label+'-status', 'status', '--porcelain=v1', '--untracked-files=all').decode()
        delta = self.git(label+'-diff', 'diff', '--binary', self.base)
        names = self.git(label+'-names', 'diff', '--name-only', self.base).decode().splitlines()
        need(set(names) <= set(OWNED), 'changed non-owned tracked path')
        # New untracked authored bytes are separately bound even on baseline.
        visible = self.git(label+'-inventory', 'ls-files', '-z', '--cached', '--others', '--exclude-standard')
        untracked = self.git(label+'-untracked', 'ls-files', '-z', '--others', '--exclude-standard').decode().split('\0')
        need(set(untracked)-{''} <= set(OWNED)|{'.venv'}, 'unexpected scan-visible untracked path')
        hashes = {}
        for name in (*OWNED, *PINS, *CHECKERS):
            path = self.repo/name
            no_symlinks(path)
            hashes[name] = digest(path.read_bytes())
        for name in (*PINS, *CHECKERS):
            baseline = self.git(label+'-baseline-'+name.replace('/','_'), 'show', self.base+':'+name)
            need(hashes[name] == digest(baseline), 'pinned source/checker changed: '+name)
        return {'head': head, 'tree': tree, 'status': status, 'complete_diff_sha256': digest(delta),
                'changed_tracked_paths': names, 'inventory_sha256': digest(visible), 'hashes': hashes}

    def preflight(self):
        runtime = self.manifest['runtime']
        link = self.repo/'.venv'
        need(link.is_symlink() and link.resolve() == Path(runtime['repo_venv']).resolve(), 'manifest runtime link')
        self.data['runtime_link'] = {'path': str(link), 'target': os.readlink(link), 'resolved': str(link.resolve())}
        expected = Path(runtime['credential_scan_python3']) if self.args.phase == 'credentials' else link/'bin/python3'
        need(Path(sys.executable).absolute() == expected.absolute(), 'literal interpreter mismatch')
        need(Path(sys.executable).is_file(), 'interpreter missing')
        self.git('ancestor', 'merge-base', '--is-ancestor', self.base, 'HEAD')
        need(self.git('base-tree', 'rev-parse', self.base+'^{tree}').decode().strip() == self.base_tree, 'base tree drift')
        self.data['initial_identity'] = self.identity('initial')
        self.data['baseline_test_only'] = self.data['initial_identity']['head'] == self.base
        fixture = json.loads((self.repo/OWNED[1]).read_text(encoding='utf-8'))
        need(fixture['schema'] == 'fc16-summary-segments/v1', 'fixture schema')
        expected = {f'body_{n}_{s}' for n in (11,12,511,512,513) for s in ('bare','period','bang','question')}
        expected |= {'newline','spaced_period','spaced_bang','spaced_question','punctuation_without_space',
                     'source_units_long','source_units_short','ascii_trim','multibyte_prefix','multibyte_body'}
        need({c['id'] for c in fixture['cases']} == expected and len(fixture['cases']) == len(expected), 'frozen dimension coverage')
        self.case_map = {c['id']: 'test_'+c['group']+'_'+c['id'] for c in fixture['cases']}
        self.case_map.update({'repeat_and_current_context': 'test_binding_repeat_and_current_context',
                             'minimum_11_mutant': 'test_control_minimum_11_mutant',
                             'adjacent_whitespace_mutant': 'test_control_adjacent_whitespace_mutant'})
        self.data['case_to_test_map'] = self.case_map
        self.save()

    def child_tests(self, label, mode, cwd):
        report_path = self.out/(label+'-tests.json')
        raw = self.run(label, [sys.executable, '-B', self.repo/OWNED[0], '--mode', mode,
                       '--temp-root', self.out/(label+'-temp'), '--report', report_path], cwd=cwd)
        report = json.loads(report_path.read_text())
        need(report['successful'] is True and report['ambient_restored'] is True, 'test status/default restoration')
        need(not report['skips'] and not report['errors'] and not report['failures'], 'skips/errors/failures')
        need(report['fixture_sha256'] == self.data['initial_identity']['hashes'][OWNED[1]], 'fixture test pin')
        ids = report['ids']
        need(len(ids) == report['tests_run'] and len(set(ids)) == len(ids), 'incomplete/duplicate test IDs')
        if mode == 'compatibility':
            expected = {'test_receipt_normalizes_to_pinned_bundle','test_tampered_bundle_is_rejected',
                        'test_receipt_binding_mismatch_is_rejected','test_duplicate_json_keys_are_rejected',
                        'test_quote_citation_and_claim_round_trip','test_json_pointer_support_is_exact'}
            need({i.rsplit('.',1)[-1] for i in ids} == expected and len(ids) == len(expected), 'exact compatibility selectors')
            need(report['selectors'] == ['tests.records.test_understanding.EvidenceTests',
                                         'tests.records.test_understanding.GroundedEvidenceTests'], 'selector drift')
        else:
            expected = set(self.case_map.values())
            actual = {i.rsplit('.',1)[-1] for i in ids if '.SegmentTests.' in i}
            need(actual == expected, 'missing frozen tests')
            need(len(ids) == len(expected)+(2 if mode == 'discovery' else 0), 'test/peer count')
            need(b'CONTROL minimum_11_mutant detected:' in raw and
                 b'CONTROL adjacent_whitespace_mutant detected:' in raw, 'deciding controls missing')
        self.data.setdefault('test_reports', []).append({'path': str(report_path),
            'sha256': digest(report_path.read_bytes()), 'tests_run': report['tests_run'],
            'ids': ids, 'subcases': report['subcases'], 'skips': report['skips'], 'mode': mode})
        self.save()

    def portable(self):
        unrelated = self.out/'unrelated-cwd'
        unrelated.mkdir()
        self.child_tests('absolute-script', 'suite', unrelated)
        self.child_tests('ordinary-discovery-peers', 'discovery', unrelated)
        # Do not encode machine-specific paths into portable authored material.
        findings = []
        machine_path = re.compile(r'/(?:home|Users|workspace|tmp)/')
        for name in OWNED:
            for number, line in enumerate((self.repo/name).read_text().splitlines(), 1):
                if machine_path.search(line):
                    findings.append([name, number])
        need(not findings, 'machine-specific authored path')
        self.data['portable_path_findings'] = findings

    def credentials(self):
        runtime = self.manifest['runtime']
        scanner = Path(runtime['detect_secrets'])
        need(scanner.parent == Path(runtime['credential_scan_python3']).parent and scanner.is_file(), 'scanner runtime')
        # Original CI arguments, no additional exclusions. Native scanner skips
        # directory symlinks; no traversal or scanner monkeypatch is installed.
        raw = self.run('detect-secrets', [scanner, 'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify'])
        parsed = json.loads(raw)
        need(isinstance(parsed.get('results'), dict), 'invalid native scanner JSON')
        self.data['scanner_json_sha256'] = digest(raw)
        self.run('check-secret-report', [sys.executable, '-B', self.repo/'tools/check_secret_report.py', self.out/'detect-secrets.stdout'])

    def public_tree(self):
        initial = self.data['initial_identity']
        need(not initial['status'], 'A5 requires committed clean authored files')
        for name in OWNED:
            committed = self.git('head-byte-'+Path(name).name, 'show', initial['head']+':'+name)
            indexed = self.git('index-byte-'+Path(name).name, 'show', ':'+name)
            need(digest(committed) == digest(indexed) == initial['hashes'][name], 'head/index/worktree mismatch')
        clone = self.out/'committed-tree'
        self.run('clone-local', ['git','clone','--local','--no-hardlinks','--no-checkout',
                 '--no-recurse-submodules', self.repo, clone])
        self.git('clone-checkout', 'checkout','--detach',initial['head'], cwd=clone)
        need(self.git('clone-head','rev-parse','HEAD',cwd=clone).decode().strip() == initial['head'], 'clone head')
        need(self.git('clone-tree','rev-parse','HEAD^{tree}',cwd=clone).decode().strip() == initial['tree'], 'clone tree')
        need(not (clone/'.venv').exists() and not (clone/'.venv').is_symlink(), 'clone runtime link forbidden')
        for name in (*OWNED,*CHECKERS):
            need(digest((clone/name).read_bytes()) == initial['hashes'][name], 'clone bytes: '+name)
        raw = self.run('committed-tree-public-scan', [sys.executable,'-B',clone/'tools/check_public_tree.py'], cwd=clone)
        report = json.loads(raw)
        need(report.get('findings') == [] and type(report.get('scanned_files')) is int and report['scanned_files'] > 0, 'clone findings/report')
        self.data['committed_tree_report'] = report
        raw = self.run('in_place_informational', [sys.executable,'-B',self.repo/'tools/check_public_tree.py'], allowed=(0,1))
        report = json.loads(raw)
        exit_code = self.steps[-1]['exit']
        need((exit_code == 0 and report.get('findings') == []) or
             (exit_code == 1 and report.get('findings') == [['.venv',0,'symlink_not_scanned']]), 'unrecognized in-place finding')
        self.data['in_place_informational'] = {'exit': exit_code, 'report': report, 'policy': 'INC-174; not a clean in-place pass when symlink finding is present'}

    def finish(self):
        final = self.identity('final')
        need(final == self.data['initial_identity'], 'identity changed while row was running')
        need(self.manifest_path.read_bytes() == self.manifest_raw, 'manifest changed')
        need(all(s['complete'] and s['exit'] in s['allowed_exits'] for s in self.steps), 'incomplete child')
        for step in self.steps:
            for stream in ('stdout','stderr'):
                need(digest(Path(step[stream]).read_bytes()) == step[stream+'_sha256'], 'late/missing child output')
        self.data['final_identity'] = final
        self.data['status'] = 'PASS_REVIEW_REQUIRED'
        self.data['exit'] = 0
        self.save()
        print(json.dumps({'phase': self.args.phase, 'exit': 0, 'head': final['head'], 'tree': final['tree'],
              'row': str(self.out/'row.json'), 'row_sha256': digest((self.out/'row.json').read_bytes()),
              'test_counts': [t['tests_run'] for t in self.data.get('test_reports', [])], 'status': self.data['status']}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', required=True, choices=('suite','compatibility','portable','credentials','public-tree'))
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--out-base', type=Path, required=True)
    args = parser.parse_args()
    recorder = Recorder(args)
    try:
        recorder.preflight()
        if args.phase == 'suite':
            recorder.child_tests('frozen-suite', 'suite', recorder.repo)
        elif args.phase == 'compatibility':
            recorder.child_tests('compatibility', 'compatibility', recorder.repo)
        elif args.phase == 'portable':
            recorder.portable()
        elif args.phase == 'credentials':
            recorder.credentials()
        else:
            recorder.public_tree()
        recorder.finish()
        return 0
    except Exception:
        recorder.data['status'] = 'FAIL'
        recorder.data['exit'] = 1
        recorder.data['error'] = traceback.format_exc()
        recorder.save()
        print(recorder.data['error'], file=sys.stderr)
        print('FAILED receipt: '+str(recorder.out/'row.json'), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
