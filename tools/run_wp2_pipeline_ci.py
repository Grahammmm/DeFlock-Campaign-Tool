"""One offline prepared-artifact admission test; not unattended pipeline readiness.

The workflow owns WP2's checkout. Four separate exact public dependency checkouts
are verified with WP5's existing trusted manifest/hashing helper. Generated
manifests, outputs and receipts are private and outside repository trees.

In-tree mode: a dependency whose marker file exists in the candidate checkout
must be passed as the candidate root. Its commit pin and distinctness checks are
skipped, and the manifest and receipt list it; all other guards still apply.
"""
import argparse
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REVISIONS = {
    'wp1': '4a356f8dd7e4a2ea64fa271ab07e1e48cce6ebe3', # pragma: allowlist secret - public commit
    'wp4': '853ba723af29492b2c836e2f9c8f86f53577eb7a', # pragma: allowlist secret - public commit
    'wp5': 'b12129ac016f0129f092e80befc209e94f7f7f09', # pragma: allowlist secret - public commit
    'pr19': '543c5eaee5022409e66df7a9f7c81fd9d9bfbf57', # pragma: allowlist secret - public commit
}
IN_TREE_MARKERS = {
    'wp1': 'campaign_tool/records/ledger/store.py',
    'wp4': 'campaign_tool/records/extraction_ledger.py',
    'wp5': 'campaign_tool/records/ledger_catalog.py',
    'pr19': 'campaign_tool/records/catalog_links.py',
}
TARGET = 'tests.records.test_pipeline_connection'


def unique(paths):
    """Order-preserving de-duplication; in-tree labels share the candidate root."""
    result = []
    for path in paths:
        if path not in result:
            result.append(path)
    return result


def in_tree_labels(candidate, roots):
    """Explicit file-based detection; a present marker forbids an external root."""
    labels = []
    for name in REVISIONS:
        present = (candidate / IN_TREE_MARKERS[name]).is_file()
        if roots[name] == candidate:
            if not present:
                raise ValueError('in-tree dependency marker missing: ' + name)
            labels.append(name)
        elif present:
            raise ValueError('dependency is in tree; pass the candidate root: ' + name)
    return labels


def check_distinct(candidate, roots, in_tree):
    pinned = [roots[name] for name in REVISIONS if name not in in_tree]
    if len(set(pinned) | {candidate}) != len(pinned) + 1:
        raise ValueError('pinned checkouts must be distinct from each other and the candidate')


def helper_for(wp5):
    path = wp5 / 'tools' / 'run_wp5_ci.py'
    spec = importlib.util.spec_from_file_location('_trusted_wp5_ci', path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    if Path(helper.__file__).resolve() != path.resolve():
        raise ValueError('WP5 helper origin mismatch')
    return helper, path


def origin(name, root):
    module = importlib.import_module(name)
    expected = root.joinpath(*name.split('.')).with_suffix('.py')
    if not module.__file__ or Path(module.__file__).resolve() != expected.resolve():
        raise ValueError('composition source origin mismatch: ' + name)


def worker(args):
    manifest_path = Path(args.manifest).resolve(strict=True)
    stat = manifest_path.stat()
    if stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
        raise ValueError('owner-private manifest required')
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    roots = {name: Path(path).resolve(strict=True) for name, path in manifest['roots'].items()}
    if set(roots) != set(REVISIONS) | {'wp2'}:
        raise ValueError('five explicit source roots required')
    candidate = Path(args.candidate_root).resolve(strict=True)
    if roots['wp2'] != candidate:
        raise ValueError('WP2 must be the actual candidate checkout')
    in_tree = in_tree_labels(candidate, roots)
    if in_tree != manifest.get('in_tree', []):
        raise ValueError('in-tree dependency labels changed')
    check_distinct(candidate, roots, in_tree)
    helper, helper_path = helper_for(roots['wp5'])
    helper_hash = helper.sha(helper_path.read_bytes())
    if 'helper_sha256' in manifest:
        helper.require(helper_hash == manifest['helper_sha256'], 'trusted helper changed')
    if 'revisions' in manifest:
        helper.require(manifest['revisions'] == {k: v for k, v in REVISIONS.items() if k not in in_tree},
                       'public dependency revisions mismatch')
    for name, root in roots.items():
        helper.require(helper.pins(root) == manifest['pins'][name], 'source pin mismatch: ' + name)
        if 'checkout_files' in manifest:
            helper.require(helper.checkout_files(root) == manifest['checkout_files'][name], 'checkout changed: ' + name)
    output = Path(args.output).resolve(strict=True)
    helper.outside_git(output)
    helper.require(output.stat().st_uid == os.geteuid() and not output.stat().st_mode & 0o077,
                   'owner-private output required')
    sys.path.insert(0, str(candidate))
    os.environ['RECORDS_COMPOSITION_MANIFEST'] = str(manifest_path)
    os.environ['RECORDS_COMPOSITION_OUTPUT'] = str(output)
    os.environ['TMPDIR'] = str(output)
    tempfile.tempdir = str(output)
    import campaign_tool.records
    campaign_tool.records.__path__[:] = unique(str(roots[k] / 'campaign_tool/records')
                                               for k in ('wp5', 'wp2', 'wp4', 'wp1', 'pr19'))
    import campaign_tool.records.intake
    intake = campaign_tool.records.intake
    intake.__path__[:] = unique([str(candidate / 'campaign_tool/records/intake'), *intake.__path__])
    import tests.records
    tests.records.__path__[:] = unique([str(candidate / 'tests/records'), str(roots['wp5'] / 'tests/records')])
    bindings = {
        TARGET: 'wp2',
        'tests.records.test_pipeline_composition': 'wp5',
        'campaign_tool.records.runner.pipeline': 'wp2',
        'campaign_tool.records.runner.canonical_mail': 'wp2',
        'campaign_tool.records.ledger.store': 'wp1',
        'campaign_tool.records.ledger.stages': 'wp1',
        'campaign_tool.records.extraction_ledger': 'wp4',
        'campaign_tool.records.extraction_validation': 'wp4',
        'campaign_tool.records.catalog_stage': 'wp5',
        'campaign_tool.records.catalog_links': 'pr19',
    }
    for name, label in bindings.items():
        origin(name, roots[label])
    suite = unittest.defaultTestLoader.loadTestsFromName(TARGET)
    helper.require(suite.countTestCases() == 1, 'exactly one connection target test required')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    for name, label in bindings.items():
        origin(name, roots[label])
    stable = all(helper.pins(root) == manifest['pins'][name] for name, root in roots.items())
    stable = stable and helper.sha(helper_path.read_bytes()) == helper_hash
    if 'checkout_files' in manifest:
        stable = stable and all(helper.checkout_files(root) == manifest['checkout_files'][name]
                                for name, root in roots.items())
    receipt = {'schema': 'wp2-prepared-connection-ci-v1', 'tests': result.testsRun,
               'failures': len(result.failures), 'errors': len(result.errors),
               'skips': len(result.skipped), 'expected_failures': len(result.expectedFailures),
               'unexpected_successes': len(result.unexpectedSuccesses), 'sources_unchanged': stable,
               'manifest_sha256': helper.sha(raw), 'helper_sha256': helper_hash,
               'origins': bindings, 'passed': helper.successful(result, 1) and stable,
               'public_commit_verification': 'revisions' in manifest and 'checkout_files' in manifest,
               'in_tree': in_tree,
               'release_ready': False, 'publication_ready': False, 'fresh_mailbox_coverage': False,
               'scope': 'synthetic prepared-artifact admission only'}
    helper.write_private(output / 'ci-test-result.json', receipt)
    print(json.dumps(receipt, sort_keys=True), flush=True)
    return 0 if receipt['passed'] else 1


def launch(args):
    candidate = Path(args.candidate_root).resolve(strict=True)
    requested = {name: Path(getattr(args, name)).resolve(strict=True) for name in REVISIONS}
    in_tree = in_tree_labels(candidate, requested)
    helper, helper_path = helper_for(requested['wp5'])
    roots = {name: helper.checked_root(requested[name], None if name in in_tree else revision)
             for name, revision in REVISIONS.items()}
    roots['wp2'] = helper.checked_root(candidate)
    check_distinct(candidate, roots, in_tree)
    revisions = {name: revision for name, revision in REVISIONS.items() if name not in in_tree}
    print('WP2 connection CI dependency sources: in-tree=' + (','.join(in_tree) or 'none')
          + ' pinned=' + (','.join(revisions) or 'none'), flush=True)
    parent = Path(args.output_parent).resolve(strict=True)
    helper.outside_git(parent)
    output = Path(tempfile.mkdtemp(prefix='wp2-connection-ci-', dir=parent))
    output.chmod(0o700)
    manifest = {'roots': {name: str(root) for name, root in roots.items()},
                'pins': {name: helper.pins(root) for name, root in roots.items()},
                'checkout_files': {name: helper.checkout_files(root) for name, root in roots.items()},
                'revisions': revisions, 'in_tree': in_tree,
                'wp2_revision': helper.git(candidate, 'rev-parse', 'HEAD'),
                'helper_sha256': helper.sha(helper_path.read_bytes())}
    manifest_path = output / 'manifest.json'
    helper.write_private(manifest_path, manifest)
    run_output = output / 'test';run_output.mkdir(mode=0o700)
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker',
               '--candidate-root', str(candidate), '--manifest', str(manifest_path),
               '--output', str(run_output)]
    process = subprocess.run(command, cwd=candidate, check=False, timeout=240)
    print('Private connection receipt:', run_output / 'ci-test-result.json', flush=True)
    return process.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-root', required=True)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--manifest', help=argparse.SUPPRESS)
    parser.add_argument('--output', help=argparse.SUPPRESS)
    parser.add_argument('--output-parent')
    for name in REVISIONS:
        parser.add_argument('--' + name)
    args = parser.parse_args()
    if args.worker:
        if not args.manifest or not args.output:
            raise ValueError('worker manifest and output required')
        return worker(args)
    if not args.output_parent or not all(getattr(args, name) for name in REVISIONS):
        raise ValueError('explicit pinned dependency roots and output parent required')
    return launch(args)


if __name__ == '__main__':
    raise SystemExit(main())
