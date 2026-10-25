"""FC12-DUTY-GROUPS: detector-only digest duty grouping and first-five sources.

Author: actual claude-opus-5-5 / anthropic on opus-coder-bopus, card t_671d937a.
Test-only characterization of the unchanged pinned product. Fictional fixture,
no model, no network, no original records, no canonical store. Discovery is
inert: nothing at import time changes the environment, temp policy or writes.
"""
import argparse
import ast
from contextlib import ExitStack, contextmanager
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / 'tests/fixtures/fc12-duty-groups/cases.json'
CASE_IDS = (
    'zero_locators', 'one_locator', 'five_locators', 'six_locators', 'seven_locators',
    'repeated_equal_locator', 'repeats_before_cap', 'same_locator_two_rules',
    'null_rule_hits', 'mixed_mention_absence',
)
CONTROLS = {
    # mode: (case whose deciding assertion must fail, deciding marker prefix)
    'no-dedup': ('repeated_equal_locator', 'deciding_duty_locators:'),
    'first-four': ('five_locators', 'deciding_conclusion_sources:'),
}
FALLBACK_TEXT = 'No detector matched a law-package rule; the record needs manual review before any conclusion.'


def denied(*args, **kwargs):
    raise RuntimeError('fc12_offline_operation_forbidden')


@contextmanager
def offline():
    """Scoped guards applied before product imports and restored on exit."""
    with ExitStack() as stack:
        for owner, name in ((socket, 'socket'), (socket, 'create_connection'),
                            (socket, 'getaddrinfo'), (sqlite3, 'connect'),
                            (subprocess, 'Popen'), (os, 'system'), (os, 'popen')):
            stack.enter_context(mock.patch.object(owner, name, denied))
        stack.enter_context(mock.patch.object(sys, 'dont_write_bytecode', True))
        stack.enter_context(mock.patch.object(sys, 'path', [str(REPO), *sys.path]))
        yield


def fixture():
    value = json.loads(FIXTURE.read_text())
    if value['schema'] != 'fc12-duty-groups-fixture/v1' or value['fictional'] is not True:
        raise ValueError('fictional_fixture_required')
    if tuple(c['id'] for c in value['cases']) != CASE_IDS:
        raise ValueError('frozen_case_inventory_mismatch')
    return value


def original_sha256(case_id):
    """Fictional digested-original identity; independent of product helpers."""
    return hashlib.sha256(('fc12-duty-groups fictional original ' + case_id).encode()).hexdigest()


def check_case(test, data, case, build_digest=None, observations=None):
    if build_digest is None:
        from campaign_tool.digest import build_digest
    package = copy.deepcopy(data['packages'][case['package']])
    units = copy.deepcopy(case['units'])
    frozen_inputs = copy.deepcopy((package, units))
    sha = original_sha256(case['id'])
    expected = case['expected']
    digest = build_digest(sha, units, package, 'us-ca', 'fictional:fc12', 'redacted_cloud')
    if observations is not None:
        observations[case['id']] = dict(duties=digest['duties'], conclusions=digest['conclusions'])
    test.assertEqual((package, units), frozen_inputs, 'inputs_unchanged:' + case['id'])
    test.assertEqual(digest['sha256'], sha)
    test.assertIsNone(digest['model_id'])
    # Prerequisite: the detector hits really carry the frozen (repeated) locators.
    rule_hits = [[h['rule_id'], h['locator']] for h in digest['hits'] if h['rule_id']]
    test.assertEqual(rule_hits, expected['rule_hits'], 'frozen_rule_hits:' + case['id'])
    test.assertEqual(sum(1 for h in digest['hits'] if h['rule_id'] is None), expected['null_rule_hits'],
                     'frozen_null_rule_hits:' + case['id'])
    # Deciding assertion 1: grouping by rule_id with first-seen distinct locators, untruncated.
    test.assertEqual(digest['duties'], expected['duties'], 'deciding_duty_locators:' + case['id'])
    rule_ids = [d['rule_id'] for d in digest['duties']]
    test.assertEqual(len(rule_ids), len(set(rule_ids)), 'no_duplicate_duties:' + case['id'])
    # Deciding assertion 2: per-conclusion first-five source selection.
    sources = [[{'locator': s['locator'], 'rule_id': s['rule_id']} for s in c['sources']]
               for c in digest['conclusions']]
    test.assertEqual(sources, expected['conclusions'], 'deciding_conclusion_sources:' + case['id'])
    for conclusion in digest['conclusions']:
        test.assertEqual(conclusion['confidence'], 'needs_attorney_review')
        test.assertTrue(conclusion['sources'])
        for source in conclusion['sources']:
            test.assertEqual(source['sha256'], sha, 'source_hash_equals_original:' + case['id'])
            test.assertEqual(set(source), {'sha256', 'locator', 'rule_id'})
    for duty, conclusion in zip(digest['duties'], digest['conclusions']):
        test.assertEqual(conclusion['sources'], [dict(sha256=sha, locator=loc, rule_id=duty['rule_id'])
                                                 for loc in duty['locators'][:5]])
        test.assertIn(duty['citation'], conclusion['text'])
    if expected['duties']:
        test.assertEqual(len(digest['conclusions']), len(expected['duties']), 'one_conclusion_per_duty')
        test.assertNotIn(FALLBACK_TEXT, [c['text'] for c in digest['conclusions']])
    else:
        test.assertEqual(len(digest['conclusions']), 1, 'manual_review_fallback_only')
        test.assertEqual(digest['conclusions'][0]['text'], FALLBACK_TEXT)
    every_locator = [h['locator'] for h in digest['hits']]
    for conclusion in digest['conclusions']:
        for source in conclusion['sources']:
            test.assertTrue(source['locator'] == {'scope': 'document'} or source['locator'] in every_locator,
                            'no_invented_source:' + case['id'])
    if 'statements' in expected:
        test.assertEqual(len(digest['statements']), expected['statements'])
        test.assertEqual(len(digest['omissions']), expected['omissions'])
    test.assertIn('No model configured: narrative fields are detector output only.', digest['limitations'])
    return digest


def mutated_build_digest(mode):
    """Mechanically derived copy of the pinned build module, changed only at one seam.

    The product module, its globals and its source file are never patched; the
    copy executes in a fresh namespace bound to the same package for imports.
    """
    from campaign_tool.digest import build
    original = inspect.getsource(build)
    module = ast.parse(original)
    functions = {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}
    if mode == 'no-dedup':
        target = functions['_duties_from_hits']
        guards = [(parent, i, n) for parent in ast.walk(target) for field in ('body',)
                  for i, n in enumerate(getattr(parent, field, []))
                  if isinstance(n, ast.If) and ast.unparse(n.test) == "hit['locator'] not in entry['locators']"]
        if len(guards) != 1:
            raise ValueError('mutation_dedup_guard_not_unique')
        parent, index, node = guards[0]
        parent.body[index:index + 1] = node.body
        seam = '_duties_from_hits: locator dedup guard removed'
    elif mode == 'first-four':
        target = functions['_detector_only']
        slices = [n for n in ast.walk(target) if isinstance(n, ast.Subscript)
                  and ast.unparse(n) == "duty['locators'][:5]"]
        if len(slices) != 1:
            raise ValueError('mutation_first_five_slice_not_unique')
        slices[0].slice.upper = ast.Constant(4)
        seam = '_detector_only: first-five source slice changed to first-four'
    else:
        raise ValueError('unknown_mutation_mode')
    ast.fix_missing_locations(module)
    namespace = {'__name__': build.__name__, '__package__': build.__package__}
    exec(compile(module, '<fc12-synthetic-' + mode + '>', 'exec'), namespace)
    return namespace['build_digest'], dict(
        mode=mode, seam=seam, product_source_changed=False,
        original_source_sha256=hashlib.sha256(original.encode()).hexdigest(),
        mutated_ast_sha256=hashlib.sha256(ast.dump(module).encode()).hexdigest())


class DutyGroupTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(offline())
        self.data = fixture()


def case_method(case_id):
    def method(self):
        check_case(self, self.data, next(c for c in self.data['cases'] if c['id'] == case_id))
    return method


for _case_id in CASE_IDS:
    setattr(DutyGroupTests, 'test_' + _case_id, case_method(_case_id))


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_ids = []

    def startTest(self, test):
        self.test_ids.append(test.id())
        super().startTest(test)


def run_suite(suite, report_path, extra=None):
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    report = dict(test_ids=result.test_ids, tests_run=result.testsRun,
                  failures=[{'id': t.id(), 'traceback': text} for t, text in result.failures],
                  errors=[{'id': t.id(), 'traceback': text} for t, text in result.errors],
                  skips=[{'id': t.id(), 'reason': reason} for t, reason in result.skipped],
                  unexpected_successes=[t.id() for t in result.unexpectedSuccesses],
                  expected_failures=[t.id() for t, _ in result.expectedFailures],
                  successful=result.wasSuccessful(), **(extra or {}))
    with Path(report_path).open('x') as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write('\n')
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--temp-root', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--mode', choices=['matrix', *CONTROLS], default='matrix')
    args = parser.parse_args()
    root = args.temp_root.absolute()
    if not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('fresh_nonsymlink_temp_root_required')
    # Standalone temp policy is scoped to this process; discovery never calls main.
    with mock.patch.object(tempfile, 'tempdir', str(root)), offline():
        data = fixture()
        observations = {}
        if args.mode == 'matrix':
            class Observed(unittest.TestCase):
                pass
            for case_id in CASE_IDS:
                def method(self, cid=case_id):
                    check_case(self, data, next(c for c in data['cases'] if c['id'] == cid), observations=observations)
                setattr(Observed, 'test_' + case_id, method)
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(Observed)
            extra = dict(mode=args.mode, case_ids=list(CASE_IDS), observations=observations)
        else:
            mutant, mutation = mutated_build_digest(args.mode)
            case_id = CONTROLS[args.mode][0]

            class Control(unittest.TestCase):
                def test_declared_seam(self):
                    check_case(self, data, next(c for c in data['cases'] if c['id'] == case_id), mutant, observations)
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(Control)
            extra = dict(mode=args.mode, case_ids=[case_id], observations=observations, mutation=mutation)
        return run_suite(suite, args.report, extra)


if __name__ == '__main__':
    raise SystemExit(main())
