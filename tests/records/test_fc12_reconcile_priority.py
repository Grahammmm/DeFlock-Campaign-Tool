"""Fictional, source-bound precedence matrix. Discovery is deliberately inert.

Author: actual gpt-6.1-sol / openai-codex, card t_108fb3d7.
No model, network, original-record or canonical-store input is used.
"""
import argparse
import ast
from contextlib import ExitStack, contextmanager
import copy
import hashlib
import importlib
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
FIXTURE = REPO / 'tests/fixtures/fc12-reconcile-priority/cases.json'
CASE_IDS = (
    'selection_before_binding', 'binding_before_field', 'field_before_no_fact',
    'no_fact', 'unknown_before_counterevidence', 'unknown_variants',
    'counterevidence_before_distinct_and_exception', 'counterevidence_before_exception',
    'counterevidence_without_exception', 'distinct_before_exception',
    'distinct_without_exception', 'exception_before_failure', 'configured_true_boundary',
    'configured_false', 'unconfigured_operator', 'known_with_unknown',
    'duplicate_equal_values', 'reverse_input_order',
)


def denied(*args, **kwargs):
    raise RuntimeError('fc12_offline_operation_forbidden')


@contextmanager
def offline():
    """Scoped guards BEFORE product imports, restored even on failure."""
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
    if value['schema'] != 'fc12-reconcile-priority-fixture/v1' or value['fictional'] is not True:
        raise ValueError('fictional_fixture_required')
    if tuple(c['id'] for c in value['cases']) != CASE_IDS:
        raise ValueError('frozen_case_inventory_mismatch')
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def frozen_citations(data, case):
    """Independent identity oracle from frozen preimages, not product helpers."""
    subject = digest(data['source_preimage'].encode())
    receipt = dict(case['receipt'], original_sha256=subject)
    imported = digest(['receipt-bound-derivative-v1', subject, digest(receipt), 'native'])
    out = []
    coordinates = [case['expected']['rule_citation'], *case['expected']['citations']]
    for ordinal, (raw, coordinate) in enumerate(zip(receipt['units'], coordinates), 1):
        artifact = digest(raw)
        out.append(dict(coordinate, unit_id=digest([imported, ordinal, artifact]),
                        original_sha256=subject, import_id=imported,
                        artifact_sha256=artifact, text_sha256=digest(raw['text'].encode())))
    return out


def build_case(data, case):
    """Only real normalization, citation, selection and comparison APIs."""
    from campaign_tool.records.understanding import evidence, policy, reconcile
    from campaign_tool.records import grounded_evidence
    receipt = copy.deepcopy(case['receipt'])
    subject = digest(data['source_preimage'].encode())
    receipt['original_sha256'] = subject
    bundle = evidence.normalize_receipt(receipt, subject, digest(receipt))
    rule_cite = evidence.cite_text(bundle['units'][0], data['requirement'])
    facts = copy.deepcopy(case['facts'])
    for i, fact in enumerate(facts):
        if type(fact['counterevidence']) is not bool or type(fact['exception_applies']) is not bool:
            raise ValueError('fixture_flags_must_be_explicit_boolean')
        fact['citations'] = [grounded_evidence.pointer_support(bundle['units'][i+1], '/value')]
    changes = case['changes']
    test = dict(field='interval_days', operator=changes.get('operator', 'at_most'), expected=30)
    if changes.get('missing_field'):
        del test['field']
    versions = [dict(version_id='fictional-v1', family_id='fictional-interval',
                     jurisdiction='fictional-state', agency_id='fictional-fc12',
                     regulated_roles=['operator'], effective_from='2029-01-01',
                     issued_at='2028-12-01', retrieved_at='2031-01-01',
                     supersession_status='not_superseded_as_of_retrieval',
                     source=dict(source_id='fictional-fc12-policy', url='https://example.invalid/fc12',
                                 locator='section 12', verification_status='verified',
                                 content_sha256=subject),
                     requirements=[dict(requirement_id='fictional-r12', text=data['requirement'],
                                        citations=[rule_cite], test=test)])]
    if changes.get('unbound'):
        # Keep the exact valid citation; only source verification is unavailable.
        versions[0]['source']['verification_status'] = 'unverified'
    scope = copy.deepcopy(data['scope'])
    if changes.get('selection_unavailable'):
        scope['event_at'] = '2028-06-01'
    if changes.get('reverse'):
        facts.reverse()
    checked = policy.validate_versions(versions)
    if not checked['valid'] or checked['gaps']:
        raise ValueError('fixture_policy_invalid_or_incomplete: ' + repr(checked))
    return facts, versions, bundle, scope, rule_cite


def check_case(test, data, case, compare=None, observations=None):
    from campaign_tool.records.understanding import reconcile
    from campaign_tool.records import grounded_evidence
    facts, versions, bundle, scope, rule_cite = build_case(data, case)
    frozen = copy.deepcopy((facts, versions, bundle))
    expected = case['expected']
    bound_citations = frozen_citations(data, case)
    expected = dict(expected, rule_citation=bound_citations[0], citations=bound_citations[1:])
    test.assertEqual(rule_cite, expected['rule_citation'], 'frozen_rule_citation')
    for f in facts:
        support = grounded_evidence.validate_support(f['citations'][0], bundle)
        test.assertEqual(support['structural_support'], 'verified_exact_retained_bytes')
        test.assertIs(support['review_acceptance'], False)
        test.assertEqual(support['semantic_assessment'], 'NOT_ASSESSED')
    test.assertEqual([c for f in sorted(facts, key=lambda f: f['fact_id']) for c in f['citations']], expected['citations'], 'frozen_fact_citations')
    result = (reconcile.compare if compare is None else compare)(facts, versions, bundle=bundle, **scope)
    if observations is not None:
        observations[case['id']] = result
    test.assertEqual(len(result['findings']), 1, 'exact_finding_count')
    finding = result['findings'][0]
    # The deciding assertion is first, with an exact named marker for controls.
    test.assertEqual(finding['reason'], expected['reason'], 'deciding_reason:' + case['id'])
    for key in ('classification', 'fact_ids', 'citations', 'counterevidence', 'exceptions', 'rule_selection'):
        test.assertEqual(finding[key], expected[key], 'frozen_' + key + ':' + case['id'])
    test.assertEqual(result['gaps'], [])
    test.assertEqual(result['status'], 'derived_findings_needing_review')
    test.assertEqual(result['schema'], 'records-evidence-reconciliation-v1')
    test.assertEqual(result['snapshot_sha256'], bundle['snapshot_sha256'])
    test.assertEqual(result['context'], scope)
    test.assertEqual(type(result['legal_conclusions']), int)
    test.assertEqual(result['legal_conclusions'], 0)
    test.assertIs(result['review_acceptance'], False)
    test.assertIs(finding['review_acceptance'], False)
    test.assertIs(finding['derived'], True)
    test.assertEqual(finding['legal_assessment'], 'NOT_ASSESSED')
    test.assertEqual(finding['event_key'], 'request-level')
    test.assertEqual(finding['agency_id'], scope['agency_id'])
    test.assertEqual(finding['request_number'], scope['request_number'])
    test.assertEqual(finding['family_id'], 'fictional-interval')
    test.assertEqual(finding['version_id'], 'fictional-v1')
    test.assertEqual(finding['requirement_id'], 'fictional-r12')
    test.assertEqual(finding['requirement_text'], data['requirement'])
    test.assertEqual(finding['rule_source'], versions[0]['source'])
    test.assertRegex(finding['finding_id'], r'\A[0-9a-f]{64}\Z')
    test.assertEqual(finding['finding_identity_version'], 'content-bound-v2')
    test.assertEqual((facts, versions, bundle), frozen, 'inputs_unchanged')
    return result


def mutated_compare(seam):
    """Mechanically derived AST; change ONLY two branch positions in compare.

    No product globals, modules or source files are patched. All validation and
    source/applicability guards remain before the altered precedence seam.
    """
    from campaign_tool.records.understanding import reconcile
    original = inspect.getsource(reconcile.compare)
    module = ast.parse(original)
    chains = [n for n in ast.walk(module) if isinstance(n, ast.If) and ast.unparse(n.test) == 'version is None']
    if len(chains) != 1:
        raise ValueError('mutation_decision_chain_not_unique')
    chain = chains[0]
    nodes = []
    cursor = chain
    while True:
        nodes.append(cursor)
        if len(cursor.orelse) == 1 and isinstance(cursor.orelse[0], ast.If):
            cursor = cursor.orelse[0]
        else:
            tail = cursor.orelse
            break
    if len(nodes) != 7:
        raise ValueError('mutation_decision_chain_shape_changed')
    tests_and_bodies = [(copy.deepcopy(n.test), copy.deepcopy(n.body)) for n in nodes]
    left, right = (4, 6) if seam == 'counterevidence-exception' else (3, 4)
    tests_and_bodies[left], tests_and_bodies[right] = tests_and_bodies[right], tests_and_bodies[left]
    for node, (condition, body) in zip(nodes, tests_and_bodies):
        node.test, node.body = condition, body
    nodes[-1].orelse = tail
    ast.fix_missing_locations(module)
    namespace = dict(vars(reconcile))
    exec(compile(module, '<fc12-synthetic-' + seam + '>', 'exec'), namespace)
    return namespace['compare'], dict(seam=seam, original_source_sha256=hashlib.sha256(original.encode()).hexdigest(),
                                      mutated_ast_sha256=hashlib.sha256(ast.dump(module).encode()).hexdigest(),
                                      swapped_branch_positions=[left, right], product_source_changed=False)


class PriorityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(offline())
        self.data = fixture()


def case_method(case_id):
    def method(self):
        case = next(c for c in self.data['cases'] if c['id'] == case_id)
        check_case(self, self.data, case)
    return method


for _case_id in CASE_IDS:
    setattr(PriorityTests, 'test_' + _case_id, case_method(_case_id))


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
    parser.add_argument('--mode', choices=['matrix', 'counterevidence-exception', 'missing-counterevidence'], default='matrix')
    args = parser.parse_args()
    root = args.temp_root.absolute()
    if not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('fresh_nonsymlink_temp_root_required')
    # Standalone policy is scoped to this process; discovery never invokes main.
    with mock.patch.object(tempfile, 'tempdir', str(root)), offline():
        data = fixture()
        observations = {}
        if args.mode == 'matrix':
            class Observed(PriorityTests):
                pass
            for case_id in CASE_IDS:
                def method(self, cid=case_id):
                    check_case(self, data, next(c for c in data['cases'] if c['id'] == cid), observations=observations)
                setattr(Observed, 'test_' + case_id, method)
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(Observed)
            extra = dict(mode=args.mode, case_ids=list(CASE_IDS), observations=observations)
        else:
            compare, mutation = mutated_compare(args.mode)
            case_id = 'counterevidence_before_exception' if args.mode == 'counterevidence-exception' else 'unknown_before_counterevidence'
            class Control(unittest.TestCase):
                def test_declared_seam(self):
                    check_case(self, data, next(c for c in data['cases'] if c['id'] == case_id), compare, observations)
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(Control)
            extra = dict(mode=args.mode, case_ids=[case_id], observations=observations, mutation=mutation)
        return run_suite(suite, args.report, extra)


if __name__ == '__main__':
    raise SystemExit(main())
