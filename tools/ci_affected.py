"""Conservative changed-area selection. Unknown input always selects the full set."""


import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
# Source-test owners. Installed-wheel tests are a separate artifact contract.
# These modules are not also run by broad source discovery.
OWNERS = {
    'records-wp5': 'catalog_stage catalog_stage_drift ledger_catalog pipeline_composition',
    'records-recovery': 'recovery publication_review_bridge',
    'records-runner': 'mail_delta runner export_proof runner_wp1_overlay runner_reliability',
    'records-extraction': 'extraction_routes extraction_ledger extraction_validation ocr_closed_cas workbook_policy extraction_inherited_budget reconciliation_crash_composition',
    'records-detectors': 'detectors detector_persistence',
    'records-queue': 'queue_runtime queue queue_scope queue_integration',
    'records-imap': 'imap_intake imap_config_sources imap_failure_codes',
    'records-native-msg': 'mail_wire_intake native_completion rfc822_consumer',
    'records-controller-kernel-faults': 'controller_kernel_faults',
    'records-run': 'runtime run_slice publish run_pdf_ocr review_retirement publish_authority proposal_provenance context_revision context_runtime unattended_context unattended_resume run_safety native_extract t12_harness_strictness t13_receipt_tooling runtime_disk_preflight',
    'records-portal': 'portal_intake portal_legacy_approval',
}
PERMANENT = {'records-wp5', 'records-recovery', 'runner', 'campaign-install', 'workers'}
SOURCE_OWNERS = {
    'tests.records.test_' + suffix: owner
    for owner, suffixes in OWNERS.items() for suffix in suffixes.split()
}
SOURCE_OWNERS.update({
    'tests.test_records_portal' + suffix: 'records-portal'
    for suffix in ('', '_https', '_repairs', '_runner_contract', '_wp1', '_fetch_plan', '_clock_contract')
})
SOURCE_OWNERS.update({
    'tests.test_' + suffix: 'campaign-install'
    for suffix in ('campaign_install_resources', 'campaign_wheel_harness', 'cli', 'discovery', 'kit', 'law')
})
SOURCE_OWNERS.update({
    'tests.test_' + suffix: 'runner' for suffix in ('outbox', 'outbox_gateway', 'redact')
})
SOURCE_OWNERS['tests.test_review_fixture'] = 'workers'


def owner(name, tier):
    assigned = 'runner' if name.startswith('tests.runner.') else SOURCE_OWNERS.get(name, 'offline')
    return assigned if tier == 'full' or assigned in PERMANENT else 'offline'


def select(paths, graph, modules):
    selected = set()
    for path in paths:
        if not path.endswith('.py') or path.endswith('/__init__.py'):
            return set(modules)
        name = path[:-3].replace('/', '.')
        if name not in graph:
            return set(modules)
        affected = {name}
        while True:
            expanded = affected | {key for key, imports in graph.items() if imports & affected}
            if expanded == affected:
                break
            affected = expanded
        matches = affected & modules
        if not matches:
            return set(modules)
        selected.update(matches)
    return selected or set(modules)


def inventory():
    """Tracked files only, so generated build copies never enter discovery."""
    raw = subprocess.check_output(['git', 'ls-files', '-z', '*.py'], cwd=ROOT)
    files = {p[:-3].replace('/', '.'): ROOT / p
             for p in raw.decode().split('\0') if p and not p.endswith('/__init__.py')}
    modules = {name for name, path in files.items()
               if name.startswith('tests.') and path.name.startswith('test_')}
    if not modules:
        raise RuntimeError('no test modules discovered')
    return files, modules


def graph_for(files):
    graph = {}
    dynamic = set()
    for name, path in files.items():
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefix = name.split('.')[:-node.level] if node.level else []
                base = '.'.join(prefix + ([node.module] if node.module else []))
                imports.add(base)
                imports.update(base + '.' + alias.name for alias in node.names)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # CLI/dynamic imports are not represented by the import graph.
                if 'campaign_tool' in node.value or 'runner' in node.value:
                    dynamic.add(name)
        graph[name] = imports
    return graph, dynamic


def partition(names, files, count):
    """Largest-source-first packing; deterministic, disjoint, nonempty shards."""
    bins = [[] for _ in range(count)]
    weights = [0] * count
    for name in sorted(names, key=lambda n: (-files[n].stat().st_size, n)):
        index = min(range(count), key=lambda i: (weights[i], i))
        bins[index].append(name)
        weights[index] += files[name].stat().st_size
    return [sorted(bucket) for bucket in bins]


class TimedResult(unittest.TextTestResult):
    """Per-module wall time; a loader _FailedTest counts under its own module."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seconds, self._start = {}, time.perf_counter()

    def startTest(self, test):
        self._start = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        module = type(test).__module__
        self.seconds[module] = self.seconds.get(module, 0.0) + time.perf_counter() - self._start


def slowest(seconds, limit=10):
    ranked = sorted(seconds.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [[module, round(value, 1)] for module, value in ranked]


def run_timed(suite, shard, stream=None):
    start = time.perf_counter()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=TimedResult).run(suite)
    print(json.dumps({'shard': shard, 'total_seconds': round(time.perf_counter() - start, 1),
                      'slowest': slowest(result.seconds)}, sort_keys=True), flush=True)
    return 0 if result.wasSuccessful() else 1


def self_test():
    """Exercise selector logic with synthetic modules, not repository tests."""
    import tempfile
    graph = {'app.alpha': set(), 'app.beta': {'app.alpha'},
             'tests.test_alpha': {'app.beta'}, 'tests.test_other': set()}
    modules = {'tests.test_alpha', 'tests.test_other'}
    assert select(['app/alpha.py'], graph, modules) == {'tests.test_alpha'}
    for path in ('unknown/file', 'app/missing.py', 'app/__init__.py', 'fixture.json'):
        assert select([path], graph, modules) == modules
    assert select([], graph, modules) == modules
    assert select(['app/alpha.py', 'unknown'], graph, modules) == modules
    assert owner('tests.records.test_queue', 'full') == 'records-queue'
    assert owner('tests.records.test_queue', 'pr') == 'offline'
    assert owner('tests.records.test_recovery', 'pr') == 'records-recovery'
    with tempfile.TemporaryDirectory() as directory:
        paths = {}
        for name, body in {'app.alpha': 'VALUE = 1', 'app.beta': 'from .alpha import VALUE',
                           'tests.test_alpha': 'from app.beta import VALUE',
                           'tests.test_other': 'VALUE = 2'}.items():
            path = Path(directory) / (name + '.py')
            path.write_text(body)
            paths[name] = path
        parsed, _ = graph_for(paths)
        assert select(['app/alpha.py'], parsed, modules) == {'tests.test_alpha'}
        bins = partition(set(paths), paths, 3)
        assert all(bins) and sum(map(len, bins)) == len(paths)
        assert set().union(*map(set, bins)) == set(paths)
        assert bins == partition(set(paths), paths, 3)
    print('affected selector: synthetic selection, fallback, ownership and sharding OK')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tier', choices=('pr', 'full'), default='pr')
    parser.add_argument('--base', default='HEAD^1')
    parser.add_argument('--shard', type=int, default=0)
    parser.add_argument('--shards', type=int, default=1)
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--inventory', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if not 0 <= args.shard < args.shards:
        parser.error('shard must be in [0, shards)')
    files, modules = inventory()
    if args.inventory:
        print(json.dumps({name: owner(name, args.tier) for name in sorted(modules)}, indent=2))
        return 0
    available = {name for name in modules if owner(name, args.tier) == 'offline'}
    selected = available
    if args.tier == 'pr':
        try:
            paths = subprocess.check_output(
                ['git', 'diff', '--name-only', '-z', args.base, 'HEAD'], cwd=ROOT).decode().split('\0')
            paths = [p for p in paths if p]
            graph, dynamic = graph_for(files)
            selected = select(paths, graph, available)
            # Shared/core changes are intentionally broad, even with static imports.
            if any(p.startswith('campaign_tool/records/ledger/') or p in (
                    'campaign_tool/records/runtime.py', 'campaign_tool/records/run_stages.py') for p in paths):
                selected = available
            elif any(not p.startswith('tests/') for p in paths):
                selected |= dynamic & available
        except (OSError, ValueError, SyntaxError, subprocess.CalledProcessError) as error:
            print(f'::warning::Affected mapping unavailable ({error}); full source selection', flush=True)
            selected = available
    # Every shard set covers this smoke contract once, even for very narrow changes.
    smoke = 'tests.test_comprehension_contract'
    selected.add(smoke)
    if len(selected) < args.shards:
        selected = available | {smoke}
    buckets = partition(selected, files, args.shards)
    names = buckets[args.shard]
    if not names:
        raise RuntimeError('empty shard; refusing silent success')
    print(json.dumps({'tier': args.tier, 'shard': args.shard, 'selected_modules': names}), flush=True)
    if args.list:
        return 0
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    if not suite.countTestCases():
        raise RuntimeError('empty test suite; refusing silent success')
    return run_timed(suite, args.shard)


if __name__ == '__main__':
    raise SystemExit(main())
