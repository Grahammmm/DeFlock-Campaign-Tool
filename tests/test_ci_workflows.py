"""Regression coverage for CI-only scheduling, partitioning and result gates."""
from collections import Counter
import copy
import io
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from tools import ci_gate, ci_scope, ci_unittest

ROOT = Path(__file__).resolve().parents[1]


class ScopeTests(unittest.TestCase):
    def test_only_audited_prose_is_skippable(self):
        self.assertFalse(ci_scope.needs_tests('pull_request', 'feature/a',
                                             ['docs/ROADMAP.md', 'docs/ci/timings.md']))
        for path in ('README.md', 'docs/records-intake-import.json',
                     'docs/runbooks/T4B-TEST-APPLY.md', 'tests/fixture.md',
                     'campaign_tool/cli.py', '.github/workflows/records.yml',
                     'tools/ci_scope.py', 'requirements-records-test.txt',
                     'pyproject.toml', 'setup.py', 'workers/package-lock.json',
                     'docs/../campaign_tool/a.md', '/docs/a.md', 'unknown.md'):
            with self.subTest(path=path):
                self.assertTrue(ci_scope.needs_tests('pull_request', 'feature/a',
                                                    ['docs/ROADMAP.md', path]))

    def test_main_batch_dispatch_and_unknown_run(self):
        for event, ref in (('push', 'main'), ('push', 'batch/a'),
                           ('workflow_dispatch', ''), ('pull_request', 'batch/a'),
                           ('pull_request', 'main'), ('pull_request', '')):
            with self.subTest(event=event, ref=ref):
                self.assertTrue(ci_scope.needs_tests(event, ref, ['docs/ROADMAP.md']))
        for paths in (None, []):
            self.assertTrue(ci_scope.needs_tests('pull_request', 'feature/a', paths))

    def test_diff_is_untruncated_and_rename_safe(self):
        event = {'pull_request': {'base': {'sha': '1' * 40}, 'head': {'sha': '2' * 40}}}
        output = b'docs/a.md\0' * 1001 + b'campaign_tool/removed.py\0docs/renamed.md\0'
        with mock.patch.object(ci_scope.subprocess, 'run', return_value=mock.Mock(stdout=output)) as run:
            paths = ci_scope.changed_paths(event)
        self.assertEqual(len(paths), 1003)
        self.assertIn('--no-renames', run.call_args.args[0])
        self.assertIn('-z', run.call_args.args[0])
        self.assertTrue(ci_scope.needs_tests('pull_request', 'feature/a', paths))

    def test_unknown_diff_and_invalid_sha_fail_open(self):
        with mock.patch.object(ci_scope.subprocess, 'run') as run:
            self.assertIsNone(ci_scope.changed_paths({'pull_request': {'base': {'sha': '-bad'}}}))
            run.assert_not_called()
        event = {'pull_request': {'base': {'sha': '1' * 40}, 'head': {'sha': '2' * 40}}}
        with mock.patch.object(ci_scope.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['git'])):
            self.assertIsNone(ci_scope.changed_paths(event))


class GateTests(unittest.TestCase):
    def setUp(self):
        self.needs = {'scope': {'result': 'success', 'outputs': {'run-tests': 'true'}},
                      'suite': {'result': 'success'}, 'scan': {'result': 'success'}}

    def test_positive_and_explicit_prose_skip(self):
        self.assertTrue(ci_gate.accepted(self.needs, 'suite'))
        self.needs['scope']['outputs']['run-tests'] = 'false'
        self.needs['suite']['result'] = 'skipped'
        self.assertTrue(ci_gate.accepted(self.needs, 'suite'))

    def test_failure_cancelled_skipped_and_unknown_are_not_success(self):
        for name in self.needs:
            for state in ('failure', 'cancelled', 'skipped', '', 'neutral'):
                with self.subTest(name=name, state=state):
                    needs = copy.deepcopy(self.needs)
                    needs[name]['result'] = state
                    self.assertFalse(ci_gate.accepted(needs, 'suite'))

    def test_scope_failure_never_authorizes_a_skip(self):
        self.needs['scope']['outputs']['run-tests'] = 'false'
        self.needs['scope']['result'] = 'failure'
        self.needs['suite']['result'] = 'skipped'
        self.assertFalse(ci_gate.accepted(self.needs, 'suite'))

    def test_missing_or_malformed_results_fail_closed(self):
        for needs in ({}, {'suite': {}}, {'scope': None, 'suite': {}},
                      {'scope': {'result': 'success', 'outputs': {}}, 'suite': {}}):
            self.assertFalse(ci_gate.accepted(needs, 'suite'))


class ShardTests(unittest.TestCase):
    @staticmethod
    def cases():
        result = []
        for name, count in (('module_a', 7), ('module_b', 4), ('module_c', 3),
                            ('module_d', 2), ('module_e', 1)):
            methods = {f'test_{index}': lambda self: None for index in range(count)}
            cls = type('Example', (unittest.TestCase,), {**methods, '__module__': name})
            result.extend(unittest.TestLoader().loadTestsFromTestCase(cls))
        return result

    def test_union_multiplicity_disjointness_and_module_fixtures(self):
        cases = self.cases()
        # Duplicate IDs are preserved as duplicate executions, never set-deduped.
        cases.append(cases[0])
        shards = ci_unittest.partition(cases, 4)
        self.assertEqual(Counter(t.id() for t in cases),
                         Counter(t.id() for shard in shards for t in shard))
        ownership = {}
        for index, shard in enumerate(shards):
            for case in shard:
                owner = ownership.setdefault(type(case).__module__, index)
                self.assertEqual(owner, index)
        self.assertEqual([[t.id() for t in s] for s in shards],
                         [[t.id() for t in s] for s in ci_unittest.partition(cases, 4)])

    def test_newly_discovered_test_cannot_disappear(self):
        cases = self.cases()
        original = Counter(t.id() for s in ci_unittest.partition(cases, 4) for t in s)
        cls = type('NewCase', (unittest.TestCase,), {'__module__': 'new_module',
                                                 'test_new': lambda self: None})
        added = cls('test_new')
        updated = Counter(t.id() for s in ci_unittest.partition(cases + [added], 4) for t in s)
        self.assertEqual(updated - original, Counter([added.id()]))

    def test_failures_are_not_hidden_by_sharding(self):
        class Failing(unittest.TestCase):
            def runTest(self):
                self.fail('intentional canary, must remain a failure')
        results = [unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite(shard))
                   for shard in ci_unittest.partition(self.cases() + [Failing()], 4)]
        self.assertEqual(sum(len(r.failures) for r in results), 1)
        self.assertFalse(all(r.wasSuccessful() for r in results))

    def test_custom_suite_is_rejected_instead_of_silently_flattened(self):
        class CustomSuite(unittest.TestSuite):
            pass
        with self.assertRaises(ValueError):
            list(ci_unittest.flatten(CustomSuite()))
        with self.assertRaises(ValueError):
            ci_unittest.partition([], 0)

    def test_discovery_import_error_fails_before_partition(self):
        loader = mock.Mock(errors=['intentional import failure'])
        loader.discover.return_value = unittest.TestSuite()
        with mock.patch.object(ci_unittest.unittest, 'TestLoader', return_value=loader):
            with self.assertRaisesRegex(RuntimeError, 'intentional import failure'):
                ci_unittest.discover()


class WorkflowTextContracts(unittest.TestCase):
    def test_no_workflow_level_path_filters_or_deployments(self):
        for path in (ROOT / '.github/workflows').glob('*.yml'):
            text = path.read_text()
            with self.subTest(path=path.name):
                self.assertNotIn('    paths:', text)
                self.assertNotIn('    paths-ignore:', text)
                self.assertNotIn('wrangler deploy', text)
                self.assertNotIn('npm publish', text)
                self.assertNotIn('continue-on-error: true', text)
                if '  workflow_call:' in text:
                    self.assertNotIn('concurrency:', text)  # caller owns it
                else:
                    self.assertIn("branches: [main, 'batch/**']", text)
                    self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", text)
                    self.assertIn('${{ github.workflow }}-${{ github.event_name }}-${{ github.ref }}-', text)
                    self.assertIn("'pr' || github.run_id", text)

    def test_matrix_tests_preserve_full_main_and_batch_policy(self):
        for path in (ROOT / '.github/workflows').glob('*.yml'):
            for line in path.read_text().splitlines():
                if 'python: ${{ fromJSON' in line:
                    with self.subTest(path=path.name):
                        self.assertIn("github.event_name == 'pull_request'", line)
                        self.assertIn("github.head_ref != 'main'", line)
                        self.assertIn("!startsWith(github.head_ref, 'batch/')", line)
                        self.assertIn("'[\"3.13\"]' || '[\"3.11\", \"3.12\", \"3.13\"]'", line)

    def test_cache_does_not_gate_install_and_tracks_dependency_inputs(self):
        for path in (ROOT / '.github/workflows').glob('*.yml'):
            text = path.read_text()
            if 'cache: pip' in text:
                with self.subTest(path=path.name):
                    self.assertIn('requirements*.txt', text)
                    self.assertIn('pyproject.toml', text)
                    self.assertIn('setup.py', text)
                    self.assertIn('pip ', text)
                    self.assertNotIn('cache-hit', text)
        text = (ROOT / '.github/workflows/workers.yml').read_text()
        self.assertIn('cache-dependency-path: workers/package-lock.json', text)
        self.assertIn('- run: npm ci', text)


if __name__ == '__main__':
    unittest.main()
