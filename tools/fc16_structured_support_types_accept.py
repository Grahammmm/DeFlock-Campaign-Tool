"""Five bounded offline FC16 acceptance rows; receipts are always fresh."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
OWNED = ('tests/records/test_fc16_structured_support_types.py',
         'tests/fixtures/fc16_structured_support_types/cases.json',
         'tools/fc16_structured_support_types_accept.py')
PINS = ('campaign_tool/records/detectors/pipeline.py',
        'campaign_tool/records/detectors/core.py',
        'tests/records/test_structured_detector_pipeline.py')
CHECKERS = ('tools/check_public_tree.py', 'campaign_tool/records/public_scan.py',
            'tools/check_secret_report.py')



def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def plain(path):
    path = Path(os.path.abspath(path))
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink_path:' + str(path))
    return path


def git(repo, *args):
    result = subprocess.run(['git', *args], cwd=repo, capture_output=True, check=False, timeout=120)
    require(result.returncode == 0, 'git_failure:' + repr(args) + ':' + result.stderr.decode(errors='replace'))
    return result.stdout


def identity(repo):
    return {'head': git(repo, 'rev-parse', 'HEAD').decode().strip(),
            'tree': git(repo, 'rev-parse', 'HEAD^{tree}').decode().strip(),
            'status': git(repo, 'status', '--porcelain=v1', '--untracked-files=all').decode(),
            'hashes': {name: sha((repo / name).read_bytes()) for name in (*PINS, *CHECKERS, *OWNED)},
            'scan_paths': git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').decode().split('\0')}


def run(row, label, argv, cwd, allowed=(0,)):
    """No shell, no environment override, child fully joined before receipt."""
    prefix = Path(row['directory']) / label
    result = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True, timeout=300, check=False)
    stdout, stderr = prefix.with_suffix('.stdout'), prefix.with_suffix('.stderr')
    stdout.write_bytes(result.stdout)
    stderr.write_bytes(result.stderr)
    entry = {'label': label, 'argv': [str(a) for a in argv], 'cwd': str(cwd),
             'exit': result.returncode, 'stdout': str(stdout), 'stderr': str(stderr),
             'stdout_sha256': sha(result.stdout), 'stderr_sha256': sha(result.stderr),
             'complete': True}
    row['children'].append(entry)
    require(result.returncode in allowed, 'child_failed:' + label + ':' + str(result.returncode))
    return result


def matrix_report(path, *, discovery=False, compatibility=False):
    data = json.loads(path.read_text())
    require(data['success'] and data['ambient_state_restored'] and data['skips'] == 0 and
            data['errors'] == 0 and data['failures'] == 0,
            'test_report_failed')
    if compatibility:
        require(data['tests'] == 18, 'compatibility_count_changed')
    else:
        require(data['tests'] == (8 if discovery else 6), 'frozen_test_count')
        expected = {'test_inventory', 'test_forbidden_types', 'test_allowed_support',
                    'test_primary_control', 'test_order_and_failure', 'test_deciding_control'}
        # Six methods constitute the frozen matrix. The exact count is checked below.
        actual = {value.rsplit('.', 1)[-1] for value in data['test_ids'] if 'SupportTypesTests.' in value}
        require(actual == expected, 'frozen_test_ids')
        require(len(data['subcases']) == 37 and all(s['passed'] for s in data['subcases']), 'frozen_subcases')
        require(len(data['controls']) == 1 and 'isolation_reason:' in
                data['controls'][0]['expected_assertion'], 'deciding_control_missing')
    return data


def test_child(row, repo, python, mode, label, cwd=None):
    directory = Path(row['directory'])
    report = directory / (label + '.tests.json')
    result = run(row, label, [python, repo / OWNED[0], '--temporary-root', directory / (label + '-temp'),
                              '--mode', mode, '--report', report], cwd or repo)
    data = matrix_report(report, discovery=mode == 'discovery', compatibility=mode == 'compatibility')
    row.setdefault('tests', []).append({'path': str(report), 'sha256': sha(report.read_bytes()), **data})
    return result


def execute(args, row):
    repo = plain(args.repo)
    require(repo == ROOT, 'repo_argument_must_match_script')
    manifest_path = plain(args.manifest)
    require(manifest_path == repo.parent / 'SOURCE-MANIFEST.json', 'designated_manifest_required')
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    require(manifest['schema'] == 'agentorange.source-manifest/v1', 'manifest_schema')
    require(manifest['repo'] == str(repo) and Path(manifest['write_root']) == repo.parent, 'manifest_repo')
    # The designated operator-owned manifest is the admission input. Preserve its
    # FILE hash in every receipt; reviewers compare it to the admitted packet.
    base, tree = manifest['head'], manifest['tree']
    for value in (base, tree):
        require(type(value) is str and len(value) == 40 and
                all(c in '0123456789abcdef' for c in value), 'invalid_manifest_identity')
    require(manifest['clean'] is True, 'manifest_base_not_clean')
    require(git(repo, 'rev-parse', base + '^{tree}').decode().strip() == tree, 'base_tree')
    git(repo, 'merge-base', '--is-ancestor', base, 'HEAD')
    runtime = manifest['runtime']
    link = repo / '.venv'
    require(link.is_symlink() and link.resolve() == Path(runtime['repo_venv']).resolve(), 'runtime_link')
    python = str(repo / '.venv/bin/python3')
    expected_python = runtime['credential_scan_python3'] if args.phase == 'credentials' else python
    require(os.path.abspath(sys.executable) == expected_python, 'wrong_interpreter')
    require(Path(runtime['detect_secrets']).parent == Path(runtime['credential_scan_python3']).parent,
            'scanner_interpreter_pair')
    require(Path(runtime['credential_scan_python3']).is_file() and Path(runtime['detect_secrets']).is_file(),
            'missing_runtime')
    start = identity(repo)
    expected_pins = {name: sha(git(repo, 'show', base + ':' + name)) for name in PINS}
    require({name: start['hashes'][name] for name in PINS} == expected_pins, 'source_pin_drift')
    changed = git(repo, 'diff', '--name-only', base, 'HEAD').decode().splitlines()
    require(set(changed) <= set(OWNED), 'out_of_scope_commit')
    require(set(git(repo, 'diff', 'HEAD', '--name-only').decode().splitlines()) <= set(OWNED),
            'out_of_scope_worktree_or_index_change')
    # No existing source/checker changes, including uncommitted bytes.
    require(not git(repo, 'diff', '--name-only', '--', *PINS, *CHECKERS), 'readonly_source_modified')
    for name in CHECKERS:
        require(sha(git(repo, 'show', base + ':' + name)) == start['hashes'][name], 'checker_drift')
    require(set(start['scan_paths']) - {''} - set(git(repo, 'ls-files', '-z').decode().split('\0')) <=
            set(OWNED) | {'.venv'}, 'unexpected_scan_visible_path')
    row.update(manifest_file_sha256=sha(manifest_raw), manifest=manifest, initial=start,
               interpreter=sys.executable, invocation_argv=sys.argv, invocation_cwd=str(Path.cwd()))
    directory = Path(row['directory'])
    diff = git(repo, 'diff', '--binary', base, 'HEAD')
    (directory / 'candidate.diff').write_bytes(diff)
    row['complete_diff_sha256'] = sha(diff)
    if args.phase == 'suite':
        test_child(row, repo, python, 'suite', 'suite')
    elif args.phase == 'compatibility':
        test_child(row, repo, python, 'compatibility', 'compatibility')
        row['baseline_delta'] = 'No product or original-test edits; exact pinned selector.'
    elif args.phase == 'portable':
        unrelated = directory / 'unrelated-cwd'
        unrelated.mkdir()
        test_child(row, repo, python, 'suite', 'absolute-script', unrelated)
        test_child(row, repo, python, 'discovery', 'discovery', unrelated)
        for name in OWNED:
            data = (repo / name).read_text()
            require(not any('/' + part in data for part in ('home/', 'Users/', 'workspace/')), 'machine_specific_path')
        row['machine_specific_paths'] = []
    elif args.phase == 'credentials':
        result = run(row, 'scanner', [runtime['detect_secrets'], 'scan', '--all-files',
                                    '--exclude-files', '^.git/', '--no-verify'], repo)
        report = directory / 'scanner.stdout'
        data = json.loads(result.stdout)
        require(isinstance(data.get('results'), dict), 'scanner_json')
        run(row, 'secret-check', [runtime['credential_scan_python3'], repo / 'tools/check_secret_report.py', report], repo)
    elif args.phase == 'public-tree':
        require(not start['status'], 'candidate_not_clean')
        tracked = set(git(repo, 'ls-files', '-z').decode().split('\0'))
        for name in OWNED:
            require(name in tracked, 'authored_untracked')
            require(sha(git(repo, 'show', 'HEAD:' + name)) == start['hashes'][name], 'head_bytes_differ')
            require(sha(git(repo, 'show', ':' + name)) == start['hashes'][name], 'index_bytes_differ')
        clone = directory / 'committed-tree'
        run(row, 'clone', ['git', 'clone', '--local', '--no-hardlinks', '--no-checkout',
                          '--no-recurse-submodules', repo, clone], directory)
        run(row, 'checkout', ['git', 'checkout', '--detach', start['head']], clone)
        require(not (clone / '.venv').exists(), 'clone_runtime_present')
        require(git(clone, 'rev-parse', 'HEAD').decode().strip() == start['head'], 'clone_head')
        require(git(clone, 'rev-parse', 'HEAD^{tree}').decode().strip() == start['tree'], 'clone_tree')
        for name in OWNED:
            require(sha((clone / name).read_bytes()) == start['hashes'][name], 'clone_authored_hash')
        result = run(row, 'committed-public-tree', [python, clone / 'tools/check_public_tree.py'], clone)
        require(json.loads(result.stdout)['findings'] == [], 'committed_findings')
        result = run(row, 'in_place_informational', [python, repo / 'tools/check_public_tree.py'], repo, (0, 1))
        findings = json.loads(result.stdout)['findings']
        require((result.returncode == 0 and findings == []) or
                (result.returncode == 1 and findings == [['.venv', 0, 'symlink_not_scanned']]), 'in_place_findings')
        row['in_place_scope'] = 'INC-174 informational only; committed-tree result is separate.'
    final = identity(repo)
    require(final == start, 'identity_changed_during_row')
    require(manifest_path.read_bytes() == manifest_raw, 'manifest_changed')
    row['final'] = final
    row['fixture_sha256'] = final['hashes'][OWNED[1]]
    row['acceptance'] = 'REVIEW REQUIRED; not semantic/legal/human acceptance or release authority'
    row['success'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('suite', 'compatibility', 'portable', 'credentials', 'public-tree'), required=True)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--out-base', type=Path, required=True)
    args = parser.parse_args()
    out = plain(args.out_base)
    require(out == ROOT.parent / 'out' and ROOT not in out.parents, 'output_outside_admitted_root')
    out.mkdir(exist_ok=True)
    directory = out / (args.phase + '-' + str(uuid.uuid4()))
    directory.mkdir()
    row = {'schema': 'fc16-structured-support-types-accept/v1', 'phase': args.phase,
           'directory': str(directory), 'children': [], 'success': False}
    try:
        execute(args, row)
    except Exception as error:
        row['error'] = type(error).__name__ + ':' + str(error)
    target = directory / 'receipt.json'
    target.write_text(json.dumps(row, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'receipt': str(target), 'sha256': sha(target.read_bytes()),
                      'success': row['success'], 'error': row.get('error')}))
    return int(not row['success'])


if __name__ == '__main__':
    raise SystemExit(main())
