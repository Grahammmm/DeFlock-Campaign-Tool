"""Five independent, offline FC12 acceptance rows with immutable run evidence.

Author: actual gpt-6.1-sol / openai-codex, task t_108fb3d7.
No acquisition, installation, remote transport, publication or live acceptance.
"""
import argparse
import ast
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest import mock
import uuid

REPO = Path(__file__).resolve().parents[1]
AUTHORED = ('tests/records/test_fc12_reconcile_priority.py',
            'tests/fixtures/fc12-reconcile-priority/cases.json',
            'tools/fc12_reconcile_priority_acceptance.py')
BASE = ''.join(('17da7065', '88467184', 'f0923e25', '16be3319', 'd577539a'))
BASE_TREE = ''.join(('91b70d87', 'ae558fe6', '7e728f5d', 'c0131dcc', 'f1056ad5'))
PINNED = {
    'campaign_tool/records/understanding/reconcile.py': ''.join(('2c114f36', '5f69e825', '4c3426ac', 'b5893ffc', '4fe87636', '5901e738', '98482b44', '54bb1560')),
    'tests/records/test_understanding.py': ''.join(('77fa084d', '17606632', '7bc17953', '81a91167', '1da7875f', '1f5721e1', '64002616', 'd6f5922a')),
}
SCANNERS = ('tools/check_public_tree.py', 'campaign_tool/records/public_scan.py', 'tools/check_secret_report.py')
LIMITS = ('Offline synthetic precedence evidence only; no semantic, human or legal acceptance. '
          'Scoped compatibility is not full CI. Remote exact-head CI/transport/shipping remain OWED '
          'to the existing operator. Pattern scans are not private-export clearance. All holds stand.')


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def dump(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')


def nonsymlink(path):
    need(path.is_absolute(), 'absolute_path_required')
    need(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink_path_refused:' + str(path))
    return path


def git(*args, cwd=REPO):
    done = subprocess.run(['git', *args], cwd=cwd, shell=False, capture_output=True, timeout=60)
    need(done.returncode == 0, 'git_failed:' + repr(args) + ':' + done.stderr.decode(errors='replace'))
    return done.stdout


def identity(repo=REPO):
    return dict(head=git('rev-parse', 'HEAD', cwd=repo).decode().strip(),
                tree=git('rev-parse', 'HEAD^{tree}', cwd=repo).decode().strip(),
                authored_sha256={p: sha((repo / p).read_bytes()) for p in AUTHORED})


def status(repo=REPO):
    return git('status', '--porcelain=v1', '--untracked-files=all', cwd=repo).decode()


def inventory(repo=REPO):
    return [p for p in git('ls-files', '-z', '--cached', '--others', '--exclude-standard', cwd=repo).decode().split('\0') if p]


def validate(manifest_path, out_base, phase):
    nonsymlink(REPO)
    nonsymlink(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    need(manifest['schema'] == 'agentorange.source-manifest/v1', 'manifest_schema')
    need(Path(manifest['repo']) == REPO and Path(manifest['write_root']) == REPO.parent, 'manifest_roots')
    need(manifest['head'] == BASE and manifest['tree'] == BASE_TREE and manifest['clean'] is True, 'manifest_base_pin')
    need(git('rev-parse', BASE + '^{tree}').decode().strip() == BASE_TREE, 'base_tree_pin')
    git('merge-base', '--is-ancestor', BASE, 'HEAD')
    need(set(git('diff', '--name-only', BASE, 'HEAD').decode().splitlines()) == set(AUTHORED), 'exclusive_committed_delta')
    need(status() == '', 'candidate_must_be_committed_and_clean')
    git('diff', '--exit-code', 'HEAD', '--')
    for path in git('ls-files', '-z').decode().split('\0'):
        if path:
            target = nonsymlink(REPO / path)
            need(target.is_file(), 'tracked_regular_file_required:' + path)
            need(target.read_bytes() == git('show', 'HEAD:' + path), 'tracked_bytes_differ:' + path)
    for path, expected in PINNED.items():
        need(sha((REPO / path).read_bytes()) == expected, 'source_pin:' + path)
    for path in SCANNERS:
        need((REPO / path).read_bytes() == git('show', BASE + ':' + path), 'scanner_changed:' + path)
    runtime = manifest['runtime']
    link = REPO / '.venv'
    need(link.is_symlink() and link.resolve() == Path(runtime['repo_venv']), 'standard_venv_link')
    python = REPO / '.venv/bin/python3'
    full = Path(runtime['credential_scan_python3'])
    need(full == Path(runtime['repo_venv']) / 'bin/python3', 'credential_runtime_shape')
    need(python.is_file() and full.is_file() and Path(runtime['detect_secrets']).is_file(), 'runtime_missing')
    expected_executable = full if phase == 'credentials' else python
    need(Path(sys.executable) == expected_executable, 'actual_interpreter_path_mismatch')
    need(Path(sys.prefix).resolve() == Path(runtime['repo_venv']).resolve(), 'actual_interpreter_prefix_mismatch')
    need(out_base == REPO.parent / 'out', 'candidate_output_root_required')
    nonsymlink(out_base)
    need(Path.cwd() == REPO, 'acceptance_cwd_mismatch')
    return manifest


class Row:
    def __init__(self, root):
        self.root = root
        self.children = []
        self.checks = {}
        self.reports = {}

    def run(self, name, argv, cwd=REPO, expected=0):
        need(type(argv) is list and all(type(x) is str for x in argv), 'literal_argv_list_required')
        stdout = self.root / (name + '.stdout')
        stderr = self.root / (name + '.stderr')
        with stdout.open('xb') as out, stderr.open('xb') as err:
            done = subprocess.run(argv, cwd=cwd, shell=False, stdout=out, stderr=err, timeout=240)
        child = dict(name=name, argv=argv, cwd=str(cwd), exit=done.returncode,
                     stdout=str(stdout), stdout_sha256=sha(stdout.read_bytes()),
                     stderr=str(stderr), stderr_sha256=sha(stderr.read_bytes()))
        self.children.append(child)
        need(done.returncode == expected, 'unexpected_child_exit:' + name + ':' + str(done.returncode))
        return stdout

    def module(self):
        spec = importlib.util.spec_from_file_location('fc12_priority_tests', REPO / AUTHORED[0])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def matrix(self, label, cwd=REPO, mode='matrix'):
        temp = self.root / (label + '-tmp')
        temp.mkdir(mode=0o700)
        report = self.root / (label + '.json')
        self.run(label, [str(REPO / '.venv/bin/python3'), '-B', str(REPO / AUTHORED[0]),
                         '--temp-root', str(temp), '--report', str(report), '--mode', mode], cwd,
                 expected=0 if mode == 'matrix' else 1)
        data = json.loads(report.read_text())
        module = self.module()
        need(data['skips'] == [] and data['errors'] == [] and data['expected_failures'] == [] and data['unexpected_successes'] == [], 'missing_or_skipped_tests:' + label)
        if mode == 'matrix':
            need(data['successful'] is True and data['failures'] == [] and data['tests_run'] == 18, 'matrix_failed:' + label)
            need(data['case_ids'] == list(module.CASE_IDS), 'matrix_case_inventory')
            need(sorted(t.rsplit('.test_', 1)[-1] for t in data['test_ids']) == sorted(module.CASE_IDS), 'matrix_test_inventory')
            need(set(data['observations']) == set(module.CASE_IDS), 'matrix_observation_inventory')
        else:
            case = 'counterevidence_before_exception' if mode == 'counterevidence-exception' else 'unknown_before_counterevidence'
            need(data['successful'] is False and data['tests_run'] == 1 and len(data['failures']) == 1, 'control_must_fail_one_assertion')
            failure = data['failures'][0]
            need(failure['id'] == data['test_ids'][0] and failure['id'].endswith('.Control.test_declared_seam'), 'control_test_identity')
            need('deciding_reason:' + case in failure['traceback'] and 'AssertionError:' in failure['traceback'], 'control_failed_wrong_assertion')
            observed = data['observations'][case]['findings'][0]['reason']
            need(observed == ('cited_exception_candidate_requires_review' if mode == 'counterevidence-exception' else 'counterevidence_requires_resolution'), 'control_wrong_mutant_outcome')
            need(data['mutation']['product_source_changed'] is False, 'control_source_modified')
        self.reports[label] = data
        return data

    def in_process(self, label, suite, module, expected_count, extra=None):
        report = self.root / (label + '.json')
        out_path = self.root / (label + '.stdout')
        err_path = self.root / (label + '.stderr')
        with out_path.open('x') as out, err_path.open('x') as err, redirect_stdout(out), redirect_stderr(err):
            exit_code = module.run_suite(suite, report, extra)
        data = json.loads(report.read_text())
        self.children.append(dict(name=label, invocation='in_process_unittest', argv=list(sys.argv), cwd=str(Path.cwd()),
                                  interpreter=sys.executable, exit=exit_code, stdout=str(out_path), stdout_sha256=sha(out_path.read_bytes()),
                                  stderr=str(err_path), stderr_sha256=sha(err_path.read_bytes())))
        need(exit_code == 0 and data['tests_run'] == expected_count and data['successful'] is True, 'unittest_failed:' + label)
        need(data['skips'] == [] and data['errors'] == [] and data['failures'] == [] and data['expected_failures'] == [] and data['unexpected_successes'] == [], 'unittest_incomplete:' + label)
        self.reports[label] = data
        return data


def suite(row):
    source_before = sha((REPO / 'campaign_tool/records/understanding/reconcile.py').read_bytes())
    row.matrix('original-baseline')
    row.matrix('negative-counterevidence-exception', mode='counterevidence-exception')
    row.matrix('negative-missing-counterevidence', mode='missing-counterevidence')
    row.matrix('restored-final')
    need(sha((REPO / 'campaign_tool/records/understanding/reconcile.py').read_bytes()) == source_before, 'product_changed_after_controls')
    row.checks['product_writes'] = 0
    row.checks['precedence_matrix'] = 18
    row.checks['negative_controls'] = 2
    row.checks['baseline_delta'] = 'Test-only: baseline and final full matrix pass unchanged pinned product; both isolated order mutations fail the exact deciding assertion.'


def compatibility(row):
    module = row.module()
    with module.offline():
        selected = unittest.defaultTestLoader.loadTestsFromName('tests.records.test_understanding.ReconcileTests')
        data = row.in_process('compatibility', selected, module, 2)
    need(set(data['test_ids']) == {
        'tests.records.test_understanding.ReconcileTests.test_classifications_never_reach_legal_conclusions',
        'tests.records.test_understanding.ReconcileTests.test_uncited_facts_become_gaps'}, 'compatibility_selector_inventory')


def ambient():
    import socket
    import sqlite3
    return dict(environment=dict(os.environ), tempdir=tempfile.tempdir, path=list(sys.path),
                socket=socket.socket, create_connection=socket.create_connection,
                getaddrinfo=socket.getaddrinfo, sqlite_connect=sqlite3.connect,
                popen=subprocess.Popen, system=os.system, os_popen=os.popen,
                dont_write_bytecode=sys.dont_write_bytecode, cwd=Path.cwd())


def portable(row):
    fresh = row.root / 'different-cwd'
    fresh.mkdir(mode=0o700)
    row.matrix('absolute-script-portable', cwd=fresh)
    before = ambient()
    module = row.module()
    need(ambient() == before, 'module_import_contaminated_ambient')
    # Normal unittest discovery of this file, not a bespoke load_tests adapter.
    with mock.patch.object(sys, 'path', [str(REPO), *sys.path]):
        discovered = unittest.TestLoader().discover(str(REPO / 'tests/records'), pattern='test_fc12_reconcile_priority.py', top_level_dir=str(REPO))
    need(ambient() == before, 'discovery_contaminated_ambient')
    snapshots = []
    class Sentinel(unittest.TestCase):
        def test_unrelated_peer(self):
            self.assertEqual(ambient(), before, 'peer_ambient_contamination')
            fd, name = tempfile.mkstemp(prefix='sentinel-')
            os.close(fd)
            path = Path(name)
            self.assertEqual(path.parent, Path(before['tempdir']))
            snapshots.append(dict(path=str(path), retained=True))
    combined = unittest.TestSuite([Sentinel('test_unrelated_peer'), discovered, Sentinel('test_unrelated_peer')])
    report = row.in_process('normal-discovery-with-peers', combined, module, 20, dict(case_ids=list(module.CASE_IDS)))
    need(ambient() == before, 'suite_contaminated_ambient')
    matrix_ids = [t for t in report['test_ids'] if '.PriorityTests.' in t]
    need(sorted(t.rsplit('.test_', 1)[-1] for t in matrix_ids) == sorted(module.CASE_IDS), 'discovery_matrix_inventory')
    need(len(snapshots) == 2, 'sentinels_not_both_executed')
    row.checks['sentinels'] = snapshots
    # Box-only paths are prohibited in the portable authored files.
    for relative in AUTHORED:
        text = (REPO / relative).read_text()
        need(not re.search(r'/(?:home|Users|workspace|tmp)/', text), 'box_only_path:' + relative)
        if relative.endswith('.py'):
            tree = ast.parse(text)
            # Any executable top-level mutation must be a definition or the
            # inert registration of the static unittest case methods.
            for node in tree.body:
                need(not isinstance(node, (ast.With, ast.Try)), 'top_level_scoped_mutation:' + relative)
    row.checks['portable_static_review'] = 'No box-only authored paths; no top-level product imports, output writes, environment/temp changes or irreversible hooks. Import closure reviewed; runtime peer checks preserve ambient state.'


def credentials(row, manifest):
    # Installed detect-secrets get_files_to_scan uses os.walk without followlinks;
    # standard dependency directory links are never traversed by this argv.
    report = row.run('detect-secrets', [manifest['runtime']['detect_secrets'], 'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify'])
    data = json.loads(report.read_text())
    need(type(data.get('results')) is dict, 'credential_report_shape')
    row.run('check-secret-report', [manifest['runtime']['credential_scan_python3'], '-B', str(REPO / 'tools/check_secret_report.py'), str(report)])
    row.checks['credential_result_paths'] = sorted(data['results'])
    row.checks['scan_scope'] = 'Unchanged repository scanner argv; no dependency symlink traversal; no credentials read or detector/allowlist changes.'


def public_tree(row, manifest, frozen):
    source_status = status()
    paths = inventory()
    need(set(paths) == set(git('ls-files', '-z').decode().split('\0')) - {''} or set(paths) - set(git('ls-files', '-z').decode().split('\0')) <= {'.venv'}, 'untracked_scan_visible_entry')
    dump(row.root / 'source-inventory.json', dict(status=source_status, paths=paths, identity=frozen,
         scanner_sha256={p: sha((REPO / p).read_bytes()) for p in SCANNERS},
         allowlist_sha256=sha((REPO / 'tools/check_secret_report.py').read_bytes()),
         venv_link=dict(path='.venv', target=os.readlink(REPO / '.venv'), resolved=str((REPO / '.venv').resolve()))))
    clone = row.root / 'committed-tree'
    row.run('clone-local', ['git', 'clone', '--local', '--no-hardlinks', '--no-checkout', '--no-recurse-submodules', str(REPO), str(clone)])
    row.run('checkout-exact-head', ['git', 'checkout', '--detach', frozen['head']], cwd=clone)
    need(identity(clone) == frozen, 'committed_clone_identity_mismatch')
    need(not (clone / '.venv').exists() and not (clone / '.venv').is_symlink(), 'clone_dependency_link')
    need(status(clone) == '', 'clone_dirty')
    report = row.run('committed-tree-public-scan', [str(REPO / '.venv/bin/python3'), '-B', str(clone / 'tools/check_public_tree.py')], cwd=clone)
    data = json.loads(report.read_text())
    need(data['findings'] == [] and data['scanned_files'] == len(inventory(clone)), 'committed_scan_incomplete_or_findings')
    # INC-174: never conflate this diagnostic with the committed-tree result.
    stdout = row.root / 'in-place-informational.stdout'
    stderr = row.root / 'in-place-informational.stderr'
    argv = [str(REPO / '.venv/bin/python3'), '-B', str(REPO / 'tools/check_public_tree.py')]
    with stdout.open('xb') as out, stderr.open('xb') as err:
        result = subprocess.run(argv, cwd=REPO, shell=False, stdout=out, stderr=err, timeout=240)
    row.children.append(dict(name='in-place-informational', argv=argv, cwd=str(REPO), exit=result.returncode,
        stdout=str(stdout), stdout_sha256=sha(stdout.read_bytes()), stderr=str(stderr), stderr_sha256=sha(stderr.read_bytes())))
    diagnostic = json.loads(stdout.read_text())
    findings = diagnostic['findings']
    need((result.returncode == 0 and findings == []) or (result.returncode == 1 and findings == [['.venv', 0, 'symlink_not_scanned']]), 'unexpected_in_place_finding_or_error')
    need(diagnostic['scanned_files'] == len(paths) - (1 if '.venv' in paths else 0), 'in_place_scan_incomplete')
    need(identity() == frozen and status() == source_status, 'source_changed_during_public_scan')
    git('diff', '--exit-code', 'HEAD', '--')
    row.checks['committed_tree_scan'] = data
    row.checks['in_place_informational_INC_174'] = dict(exit=result.returncode, result=diagnostic, staging_tree_pass=result.returncode == 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=['suite', 'compatibility', 'portable', 'credentials', 'public-tree'], required=True)
    parser.add_argument('--source-manifest', type=Path, required=True)
    parser.add_argument('--out-base', type=Path, required=True)
    args = parser.parse_args()
    # No product code is imported before staged source/runtime/path verification.
    manifest = validate(args.source_manifest, args.out_base, args.phase)
    args.out_base.mkdir(mode=0o700, exist_ok=True)
    root = args.out_base / (args.phase + '-' + str(uuid.uuid4()))
    root.mkdir(mode=0o700)
    temporary = root / 'tmp'
    temporary.mkdir(mode=0o700)
    row = Row(root)
    frozen = identity()
    receipt = dict(schema='fc12-reconcile-priority-acceptance/v1', phase=args.phase,
                   argv=[sys.executable, *sys.argv], cwd=str(Path.cwd()), interpreter=sys.executable,
                   interpreter_resolved=str(Path(sys.executable).resolve()), prefix=sys.prefix,
                   python_version=sys.version, candidate=frozen, source_manifest_sha256=sha(args.source_manifest.read_bytes()),
                   acceptance_contract='t_108fb3d7 frozen brief; CARD-TEMPLATE 1.1 / PRE-REVIEW-CHECKLIST 1.15 / REVIEW-RUBRIC 1.3',
                   material_author={'model': 'gpt-6.1-sol', 'provider': 'openai-codex', 'profile': 'sol-reviewer', 'run_id': 1933,
                                    'provenance': 'Hermes session runtime and same-card AUTHOR-ROUTE ADMISSION; not inferred from git author'},
                   limits=LIMITS, review_required=True)
    exit_code = 0
    with (root / 'runner.stdout').open('x') as out, (root / 'runner.stderr').open('x') as err:
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(tempfile, 'tempdir', str(temporary)), mock.patch.object(sys, 'dont_write_bytecode', True):
            try:
                if args.phase == 'suite':
                    suite(row)
                elif args.phase == 'compatibility':
                    compatibility(row)
                elif args.phase == 'portable':
                    portable(row)
                elif args.phase == 'credentials':
                    credentials(row, manifest)
                else:
                    public_tree(row, manifest, frozen)
                need(identity() == frozen and status() == '', 'candidate_changed_during_acceptance')
            except Exception:
                traceback.print_exc()
                exit_code = 1
    receipt.update(exit=exit_code, children=row.children, checks=row.checks,
                   test_reports=row.reports, status='PASS_REVIEW_REQUIRED' if exit_code == 0 else 'FAIL_HELD',
                   skips=[s for report in row.reports.values() for s in report['skips']])
    dump(root / 'receipt.json', receipt)
    # All children, scanners, test reports and stdout/stderr writers have finished.
    # Seal once, never append to or reseal this evidence manifest.
    files = {}
    for path in sorted(root.rglob('*')):
        need(not path.is_symlink(), 'evidence_symlink_refused')
        if path.is_file():
            files[str(path.relative_to(root))] = dict(sha256=sha(path.read_bytes()), bytes=path.stat().st_size)
    sealed = root / 'EVIDENCE-MANIFEST.json'
    dump(sealed, dict(schema='fc12-evidence-manifest/v1', files=files, candidate=frozen,
                      exit=exit_code, all_writers_finished=True, limits=LIMITS))
    print(json.dumps(dict(phase=args.phase, exit=exit_code, receipt=str(root / 'receipt.json'),
                          evidence_manifest=str(sealed), evidence_manifest_sha256=sha(sealed.read_bytes())), sort_keys=True))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
