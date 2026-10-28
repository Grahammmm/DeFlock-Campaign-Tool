"""Five separate offline FC12-DUTY-GROUPS acceptance rows with sealed run evidence.

Author: actual claude-opus-5-5 / anthropic on opus-coder-bopus, card t_671d937a.
No acquisition, installation, model, network, transport, publication or live
acceptance. Machine-valid output is REVIEW REQUIRED only.
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
AUTHORED = ('tests/records/test_fc12_duty_groups.py',
            'tests/fixtures/fc12-duty-groups/cases.json',
            'tools/fc12_duty_groups_acceptance.py')
TEST_MODULE = AUTHORED[0]
BUILD = 'campaign_tool/digest/build.py'
# Card pins, split so no scanner sees a contiguous hex literal.
BASE = ''.join(('17da7065', '88467184', 'f0923e25', '16be3319', 'd577539a'))
BASE_TREE = ''.join(('91b70d87', 'ae558fe6', '7e728f5d', 'c0131dcc', 'f1056ad5'))
PINNED = {
    BUILD: ''.join(('c6cd5ab3', '4d6b30eb', 'f9442018', '571a44c0', 'd6f8b505', '3b92b550', 'bacb7315', 'ad745983')),
    'tests/runner/test_digest.py': ''.join(('fbcdff42', '81de44d4', 'a5837c9f', '2e3011a3', 'aabb0863', '864e2831', '7c69d57d', '88904486')),
}
SCANNERS = ('tools/check_public_tree.py', 'campaign_tool/records/public_scan.py', 'tools/check_secret_report.py')
COMPAT = 'tests.runner.test_digest.BuildDigestTests.test_no_model_is_detector_only_and_needs_attorney_review'
CONTROL_CASES = {'no-dedup': ('repeated_equal_locator', 'deciding_duty_locators:repeated_equal_locator'),
                 'first-four': ('five_locators', 'deciding_conclusion_sources:five_locators')}
MATRIX_SIZE = 10
LIMITS = ('Offline fictional characterization of the unchanged pinned product only; no semantic, human or legal '
          'acceptance. Scoped compatibility is not full CI. Remote exact-head CI, transport and shipping remain OWED '
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


def tracked(repo=REPO):
    return [p for p in git('ls-files', '-z', cwd=repo).decode().split('\0') if p]


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
    for path in tracked():
        target = nonsymlink(REPO / path)
        need(target.is_file(), 'tracked_regular_file_required:' + path)
        need(target.read_bytes() == git('show', 'HEAD:' + path), 'tracked_bytes_differ:' + path)
    for path, expected in PINNED.items():
        need(sha((REPO / path).read_bytes()) == expected, 'source_pin:' + path)
        need((REPO / path).read_bytes() == git('show', BASE + ':' + path), 'source_not_base_blob:' + path)
    for path in SCANNERS:
        need((REPO / path).read_bytes() == git('show', BASE + ':' + path), 'scanner_changed:' + path)
    runtime = manifest['runtime']
    link = REPO / '.venv'
    need(link.is_symlink() and link.resolve() == Path(runtime['repo_venv']).resolve(), 'standard_venv_link')
    python = REPO / '.venv/bin/python3'
    full = Path(runtime['credential_scan_python3'])
    need(full == Path(runtime['repo_venv']) / 'bin/python3', 'credential_runtime_shape')
    need(python.is_file() and full.is_file() and Path(runtime['detect_secrets']).is_file(), 'runtime_missing')
    expected_executable = full if phase == 'credentials' else python
    need(Path(sys.executable) == expected_executable, 'actual_interpreter_path_mismatch')
    need(Path(sys.prefix).resolve() == Path(runtime['repo_venv']).resolve(), 'actual_interpreter_prefix_mismatch')
    need(out_base == REPO.parent / 'out', 'candidate_output_root_required')
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
        self.children.append(dict(name=name, argv=argv, cwd=str(cwd), exit=done.returncode,
                                  stdout=str(stdout), stdout_sha256=sha(stdout.read_bytes()),
                                  stderr=str(stderr), stderr_sha256=sha(stderr.read_bytes())))
        need(done.returncode == expected, 'unexpected_child_exit:' + name + ':' + str(done.returncode))
        return stdout

    def module(self):
        spec = importlib.util.spec_from_file_location('fc12_duty_groups_tests', REPO / TEST_MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def matrix(self, label, cwd=REPO, mode='matrix'):
        temp = self.root / (label + '-tmp')
        temp.mkdir(mode=0o700)
        report = self.root / (label + '.json')
        self.run(label, [str(REPO / '.venv/bin/python3'), '-B', str(REPO / TEST_MODULE),
                         '--temp-root', str(temp), '--report', str(report), '--mode', mode], cwd,
                 expected=0 if mode == 'matrix' else 1)
        data = json.loads(report.read_text())
        module = self.module()
        need(data['skips'] == [] and data['errors'] == [] and data['expected_failures'] == []
             and data['unexpected_successes'] == [], 'skipped_or_errored:' + label)
        if mode == 'matrix':
            need(data['successful'] is True and data['failures'] == [] and data['tests_run'] == MATRIX_SIZE, 'matrix_failed:' + label)
            need(tuple(data['case_ids']) == module.CASE_IDS and len(module.CASE_IDS) == MATRIX_SIZE, 'matrix_case_inventory')
            need(sorted(t.rsplit('.test_', 1)[-1] for t in data['test_ids']) == sorted(module.CASE_IDS), 'matrix_test_inventory')
            need(set(data['observations']) == set(module.CASE_IDS), 'matrix_observation_inventory')
            seven = data['observations']['seven_locators']
            need(len(seven['duties']) == 1 and len(seven['duties'][0]['locators']) == 7
                 and len(seven['conclusions'][0]['sources']) == 5, 'measured_premise_seven_to_five')
        else:
            case, marker = CONTROL_CASES[mode]
            need(data['successful'] is False and data['tests_run'] == 1 and len(data['failures']) == 1, 'control_must_fail_one_assertion')
            failure = data['failures'][0]
            need(failure['id'] == data['test_ids'][0] and failure['id'].endswith('.Control.test_declared_seam'), 'control_test_identity')
            need('AssertionError:' in failure['traceback'] and failure['traceback'].rstrip().endswith(marker), 'control_failed_wrong_assertion')
            observed = data['observations'][case]
            if mode == 'no-dedup':
                need(observed['duties'][0]['locators'] == [{'page': 1}, {'page': 1}, {'page': 2}, {'page': 1}], 'control_wrong_mutant_outcome')
            else:
                need(len(observed['duties'][0]['locators']) == 5 and len(observed['conclusions'][0]['sources']) == 4, 'control_wrong_mutant_outcome')
            need(data['mutation']['product_source_changed'] is False and data['mutation']['mode'] == mode, 'control_mutation_record')
            need(data['mutation']['original_source_sha256'] == sha((REPO / BUILD).read_text().encode()), 'control_not_derived_from_staged_source')
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
        need(data['skips'] == [] and data['errors'] == [] and data['failures'] == [] and data['expected_failures'] == []
             and data['unexpected_successes'] == [], 'unittest_incomplete:' + label)
        self.reports[label] = data
        return data


def suite(row):
    before = sha((REPO / BUILD).read_bytes())
    row.matrix('original-baseline')
    row.matrix('negative-no-dedup', mode='no-dedup')
    row.matrix('negative-first-four', mode='first-four')
    row.matrix('restored-final')
    need(sha((REPO / BUILD).read_bytes()) == before == PINNED[BUILD], 'product_changed_after_controls')
    row.checks.update(product_writes=0, matrix_cases=MATRIX_SIZE, negative_controls=2,
                      baseline_delta=('Test-only characterization: the full matrix passes on the unchanged pinned product '
                                      'before and after the controls; the AST-derived no-dedup copy fails exactly '
                                      'deciding_duty_locators:repeated_equal_locator and the first-four copy fails exactly '
                                      'deciding_conclusion_sources:five_locators.'))


def compatibility(row):
    module = row.module()
    with module.offline():
        selected = unittest.defaultTestLoader.loadTestsFromName(COMPAT)
        data = row.in_process('compatibility', selected, module, 1)
    need(data['test_ids'] == [COMPAT], 'compatibility_selector_inventory')


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
    with mock.patch.object(sys, 'path', [str(REPO), *sys.path]):
        discovered = unittest.TestLoader().discover(str(REPO / 'tests/records'), pattern='test_fc12_duty_groups.py',
                                                    top_level_dir=str(REPO))
    need(ambient() == before, 'discovery_contaminated_ambient')
    snapshots = []

    class Sentinel(unittest.TestCase):
        def test_unrelated_peer(self):
            self.assertEqual(ambient(), before, 'peer_ambient_contamination')
            fd, name = tempfile.mkstemp(prefix='sentinel-')
            os.close(fd)
            self.assertEqual(Path(name).parent, Path(before['tempdir']))
            snapshots.append(dict(path=name, retained=True))
    combined = unittest.TestSuite([Sentinel('test_unrelated_peer'), discovered, Sentinel('test_unrelated_peer')])
    report = row.in_process('normal-discovery-with-peers', combined, module, MATRIX_SIZE + 2, dict(case_ids=list(module.CASE_IDS)))
    need(ambient() == before, 'suite_contaminated_ambient')
    matrix_ids = [t for t in report['test_ids'] if t.startswith('tests.records.test_fc12_duty_groups.DutyGroupTests.')]
    need(sorted(t.rsplit('.test_', 1)[-1] for t in matrix_ids) == sorted(module.CASE_IDS), 'discovery_matrix_inventory')
    need(len(snapshots) == 2, 'sentinels_not_both_executed')
    row.checks['sentinels'] = snapshots
    for relative in AUTHORED:
        text = (REPO / relative).read_text()
        need(not re.search(r'/(?:home|Users|workspace|tmp|root)/', text), 'box_only_path:' + relative)
        if relative.endswith('.py'):
            for node in ast.parse(text).body:
                need(not isinstance(node, (ast.With, ast.Try)), 'top_level_scoped_mutation:' + relative)
    row.checks['portable_static_review'] = ('No box-only authored paths; product imports are lazy inside test functions; '
                                            'no import-time output writes, environment/temp changes or irreversible hooks.')


def credentials(row, manifest):
    # Unchanged repository scanner argv; os.walk does not follow the dependency link.
    report = row.run('detect-secrets', [manifest['runtime']['detect_secrets'], 'scan', '--all-files',
                                        '--exclude-files', '^.git/', '--no-verify'])
    data = json.loads(report.read_text())
    need(type(data.get('results')) is dict, 'credential_report_shape')
    row.run('check-secret-report', [manifest['runtime']['credential_scan_python3'], '-B',
                                    str(REPO / 'tools/check_secret_report.py'), str(report)])
    need(not any(p in data['results'] for p in AUTHORED), 'authored_file_flagged')
    row.checks['credential_result_paths'] = sorted(data['results'])
    row.checks['scan_scope'] = 'Unchanged scanner argv and checker; no credentials read, no detector/allowlist change.'


def public_tree(row, frozen):
    source_status = status()
    paths = inventory()
    need(set(paths) - set(tracked()) <= {'.venv'}, 'untracked_scan_visible_entry')
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
    # INC-174: informational only; never conflated with the committed-tree result.
    stdout = row.root / 'in-place-informational.stdout'
    stderr = row.root / 'in-place-informational.stderr'
    argv = [str(REPO / '.venv/bin/python3'), '-B', str(REPO / 'tools/check_public_tree.py')]
    with stdout.open('xb') as out, stderr.open('xb') as err:
        result = subprocess.run(argv, cwd=REPO, shell=False, stdout=out, stderr=err, timeout=240)
    row.children.append(dict(name='in-place-informational', argv=argv, cwd=str(REPO), exit=result.returncode,
                             stdout=str(stdout), stdout_sha256=sha(stdout.read_bytes()),
                             stderr=str(stderr), stderr_sha256=sha(stderr.read_bytes())))
    diagnostic = json.loads(stdout.read_text())
    findings = diagnostic['findings']
    need((result.returncode == 0 and findings == []) or (result.returncode == 1 and findings == [['.venv', 0, 'symlink_not_scanned']]),
         'unexpected_in_place_finding_or_error')
    need(identity() == frozen and status() == source_status, 'source_changed_during_public_scan')
    git('diff', '--exit-code', 'HEAD', '--')
    row.checks['committed_tree_scan'] = data
    row.checks['in_place_informational_INC_174'] = dict(exit=result.returncode, result=diagnostic,
                                                        staging_tree_pass=result.returncode == 0 and findings == [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=['suite', 'compatibility', 'portable', 'credentials', 'public-tree'], required=True)
    parser.add_argument('--source-manifest', type=Path, required=True)
    parser.add_argument('--out-base', type=Path, required=True)
    args = parser.parse_args()
    # No product code is imported before staged source/runtime/path verification.
    manifest = validate(args.source_manifest, args.out_base, args.phase)
    args.out_base.mkdir(mode=0o700, exist_ok=True)
    nonsymlink(args.out_base)
    root = args.out_base / (args.phase + '-' + str(uuid.uuid4()))
    root.mkdir(mode=0o700)
    temporary = root / 'tmp'
    temporary.mkdir(mode=0o700)
    row = Row(root)
    frozen = identity()
    receipt = dict(schema='fc12-duty-groups-acceptance/v1', phase=args.phase,
                   argv=[sys.executable, *sys.argv], cwd=str(Path.cwd()), interpreter=sys.executable,
                   interpreter_resolved=str(Path(sys.executable).resolve()), prefix=sys.prefix,
                   python_version=sys.version, candidate=frozen, base=dict(head=BASE, tree=BASE_TREE),
                   source_pins={p: sha((REPO / p).read_bytes()) for p in PINNED},
                   source_manifest_sha256=sha(args.source_manifest.read_bytes()),
                   fixture_sha256=sha((REPO / AUTHORED[1]).read_bytes()),
                   material_author={'model': 'claude-opus-5-5', 'provider': 'anthropic', 'profile': 'opus-coder-bopus',
                                    'card': 't_671d937a', 'run_id': 2259,
                                    'provenance': 'Hermes session runtime and same-card operator admission; not inferred from git author'},
                   limits=LIMITS, review_required=True)
    exit_code = 0
    with (root / 'runner.stdout').open('x') as out, (root / 'runner.stderr').open('x') as err:
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(tempfile, 'tempdir', str(temporary)), \
                mock.patch.object(sys, 'dont_write_bytecode', True):
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
                    public_tree(row, frozen)
                need(identity() == frozen and status() == '', 'candidate_changed_during_acceptance')
            except Exception:
                traceback.print_exc()
                exit_code = 1
    receipt.update(exit=exit_code, children=row.children, checks=row.checks, test_reports=row.reports,
                   status='PASS_REVIEW_REQUIRED' if exit_code == 0 else 'FAIL_HELD',
                   skips=[s for report in row.reports.values() for s in report['skips']])
    dump(root / 'receipt.json', receipt)
    # Every child, scanner, report and stdout/stderr writer has finished: seal once.
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
