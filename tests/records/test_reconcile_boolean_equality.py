"""RF0844-5: boolean evidence is never equal to numeric policy values (and vice versa).

Every fixture is synthetic: invented policy text, invented source bytes and IDs. The
comparisons are private factual observations, never legal conclusions.
"""
import contextlib
import copy
import hashlib
import json
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import grounded_evidence
from campaign_tool.records.understanding import cli, evidence, reconcile

AGENCY = "example-agency"
ORIGINAL = hashlib.sha256(b"rf0844-5 synthetic original").hexdigest()
POLICY_TEXT = "Policy 7.2 requires the audit flag setting to match."
REQ_TEXT = "audit flag setting to match"
SCOPE = {"agency_id": AGENCY, "jurisdiction": "example-state", "regulated_role": "operator",
         "event_at": "2030-01-15"}
NO_CONFLICT = ("no_conflict_observed", "only_supplied_fields_no_global_compliance_claim")
CONFLICT = ("potential_conflict", "authenticated_factual_exception_and_terms_review_not_established")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def make_bundle(observed_values):
    """Unit 0 is policy text; unit i (1-based) is a JSON document {"v": observed_values[i-1]}."""
    texts = [POLICY_TEXT] + [json.dumps({"v": v}) for v in observed_values]
    receipt = {
        "schema": "format-extraction-v1", "original_sha256": ORIGINAL, "original_changed": False,
        "status": "complete", "parser": "synthetic", "parser_version": "0", "route": {"format": "pdf"},
        "pages": [{"page_no": i, "status": "ok"} for i in range(1, len(texts) + 1)],
        "units": [{"kind": "text", "locator": {"page": i}, "text": t, "data": {}}
                  for i, t in enumerate(texts, 1)],
    }
    return evidence.normalize_receipt(receipt, ORIGINAL, digest(evidence.canonical(receipt)))


def policy_versions(bundle, operator, expected, *, source_sha=ORIGINAL):
    rule = evidence.cite_text(bundle["units"][0], REQ_TEXT)
    return [{
        "version_id": "v1", "family_id": "audit", "jurisdiction": "example-state", "agency_id": "*",
        "regulated_roles": ["operator"], "effective_from": "2029-01-01", "issued_at": "2028-12-01",
        "retrieved_at": "2031-01-01", "supersession_status": "not_superseded_as_of_retrieval",
        "source": {"source_id": "synthetic-policy", "url": "https://example.invalid/policy",
                   "locator": "section 7.2", "verification_status": "verified",
                   "content_sha256": source_sha},
        "requirements": [{"requirement_id": "r1", "text": REQ_TEXT, "citations": [rule],
                          "test": {"field": "flag", "operator": operator, "expected": expected}}],
    }]


def make_fact(bundle, index, value, fact_id=None, **extra):
    cite = grounded_evidence.pointer_support(bundle["units"][index], "/v")
    return dict({"fact_id": fact_id or "f%d" % index, "field": "flag", "value": value,
                 "citations": [cite]}, **extra)


def run_compare(observed, operator, expected, **kw):
    bundle = make_bundle([observed])
    versions = policy_versions(bundle, operator, expected)
    facts = [make_fact(bundle, 1, observed)]
    result = reconcile.compare(facts, versions, bundle=bundle, **SCOPE)
    return result


def only_finding(result):
    assert len(result["findings"]) == 1, result
    f = result["findings"][0]
    return f["classification"], f["reason"]


def sourced(observed, operator, expected):
    return only_finding(run_compare(observed, operator, expected))


def flip(label):
    return CONFLICT if label == NO_CONFLICT else NO_CONFLICT


class DirectEqualityTests(unittest.TestCase):
    def test_boolean_number_equality_is_distinct_in_both_directions(self):
        pairs = [(True, 1), (True, 1.0), (False, 0), (False, 0.0)]
        for boolean, number in pairs:
            for observed, expected in ((number, boolean), (boolean, number)):
                with self.subTest(observed=observed, expected=expected):
                    eq = {"operator": "equals", "expected": expected}
                    ne = {"operator": "not_equals", "expected": expected}
                    self.assertIs(reconcile.test_value(observed, eq), False)
                    self.assertIs(reconcile.test_value(observed, ne), True)
                    self.assertEqual(sourced(observed, "equals", expected), CONFLICT)
                    self.assertEqual(sourced(observed, "not_equals", expected), NO_CONFLICT)

    def test_nested_boolean_number_equality_is_distinct(self):
        differing = [
            ({"enabled": 1}, {"enabled": True}),
            ({"enabled": True}, {"enabled": 1.0}),
            ({"enabled": 0}, {"enabled": False}),
            ([1, 2], [True, 2]),
            ([{"on": 1}], [{"on": True}]),
            ([{"on": False}], [{"on": 0.0}]),
            ({"a": [{"b": {"c": [0]}}]}, {"a": [{"b": {"c": [False]}}]}),
            ({"a": [1, {"b": True}]}, {"a": [1, {"b": 1}]}),
        ]
        self.assertEqual(len(differing), 8)
        for observed, expected in differing:
            for a, b in ((observed, expected), (expected, observed)):
                with self.subTest(observed=a, expected=b):
                    self.assertIs(reconcile.test_value(a, {"operator": "equals", "expected": b}), False)
                    self.assertIs(reconcile.test_value(a, {"operator": "not_equals", "expected": b}), True)
                    self.assertEqual(sourced(a, "equals", b), CONFLICT)
                    self.assertEqual(sourced(a, "not_equals", b), NO_CONFLICT)
        equal_peers = [({"a": [1, {"b": True}]}, {"a": [1.0, {"b": True}]}),
                       ({"x": True, "y": 0}, {"y": 0, "x": True}),
                       ({"n": None, "l": [None]}, {"l": [None], "n": None})]
        for a, b in equal_peers:
            with self.subTest(equal=a):
                self.assertIs(reconcile.test_value(a, {"operator": "equals", "expected": b}), True)
                self.assertIs(reconcile.test_value(a, {"operator": "not_equals", "expected": b}), False)
                self.assertEqual(sourced(a, "equals", b), NO_CONFLICT)
        unequal = [([1, 2], [2, 1]), ([1, 2], [1, 2, 3]), ({"a": 1}, {"a": 1, "b": 1}),
                   ({"a": 1}, {"b": 1}), ({"a": [True]}, {"a": []}), ([True], {"0": True}),
                   ({"a": True}, [True])]
        for a, b in unequal:
            with self.subTest(unequal=a, other=b):
                self.assertIs(reconcile.test_value(a, {"operator": "equals", "expected": b}), False)
                self.assertIs(reconcile.test_value(a, {"operator": "not_equals", "expected": b}), True)
        deep_a = deep_b = True
        deep_c = 1
        for _ in range(40):
            deep_a, deep_b, deep_c = [deep_a], [deep_b], [deep_c]
        self.assertIs(reconcile.test_value(deep_a, {"operator": "equals", "expected": deep_b}), True)
        self.assertIs(reconcile.test_value(deep_a, {"operator": "equals", "expected": deep_c}), False)
        self.assertIs(reconcile.test_value(deep_a, {"operator": "not_equals", "expected": deep_c}), True)

    def test_other_json_equalities_keep_existing_results(self):
        cases = [(True, True, True), (False, False, True), (True, False, False), (False, True, False),
                 (1, 1, True), (1, 1.0, True), (1.0, 1, True), (0, -0.0, True), (0.0, -0.0, True),
                 (1, 2, False), (1.5, 1.5, True), (1.5, 2.5, False), ("1", 1, False), (1, "1", False),
                 ("true", True, False), ("a", "a", True), ("a", "b", False), (None, None, True),
                 ({"a": None}, {"a": None}, True), ({"a": None}, {"a": 0}, False),
                 ([None, "x"], [None, "x"], True), ([], [], True), ({}, {}, True), ([], {}, False)]
        for observed, expected, equal in cases:
            with self.subTest(observed=observed, expected=expected):
                self.assertIs(reconcile.test_value(observed, {"operator": "equals", "expected": expected}), equal)
                self.assertIs(reconcile.test_value(observed, {"operator": "not_equals", "expected": expected}), not equal)
        for observed, expected, equal in ((True, True, True), (1, 1.0, True), (0, -0.0, True),
                                          (True, False, False), (1, 2, False), ("a", "a", True),
                                          ("1", 1, False), ({"a": [None]}, {"a": [None]}, True)):
            with self.subTest(sourced=observed, expected=expected):
                want = NO_CONFLICT if equal else CONFLICT
                self.assertEqual(sourced(observed, "equals", expected), want)
                self.assertEqual(sourced(observed, "not_equals", expected), flip(want))
        # Operators outside the repair keep their existing behaviour, including bool exclusion.
        self.assertIsNone(reconcile.test_value(True, {"operator": "at_most", "expected": 1}))
        self.assertIsNone(reconcile.test_value(1, {"operator": "at_least", "expected": True}))
        self.assertIs(reconcile.test_value(1, {"operator": "at_most", "expected": 1.0}), True)
        self.assertIsNone(reconcile.test_value(1, {"operator": "unknown", "expected": 1}))
        self.assertIs(reconcile.test_value(False, {"operator": "nonempty"}), True)
        self.assertIs(reconcile.test_value("Abc", {"operator": "contains", "expected": "b"}), True)

    def test_inputs_are_never_rewritten(self):
        observed, expected = {"a": [1, {"b": 0}]}, {"a": [True, {"b": False}]}
        before = copy.deepcopy((observed, expected))
        reconcile.test_value(observed, {"operator": "equals", "expected": expected})
        self.assertEqual(json.dumps((observed, expected)), json.dumps(before))
        self.assertIs(type(observed["a"][0]), int)
        self.assertIs(type(expected["a"][0]), bool)


class PrecedenceTests(unittest.TestCase):
    def finding(self, facts, versions, bundle, **scope):
        result = reconcile.compare(facts, versions, bundle=bundle, **dict(SCOPE, **scope))
        self.assertIs(result["review_acceptance"], False)
        self.assertEqual(result["legal_conclusions"], 0)
        return result["findings"][0]

    def test_reconciliation_precedence_and_identity_are_preserved(self):
        bundle = make_bundle([1, True])
        versions = policy_versions(bundle, "equals", True)
        one = make_fact(bundle, 1, 1)
        yes = make_fact(bundle, 2, True)
        # Baseline: 1 versus required true is now a potential conflict; true is not.
        self.assertEqual(self.finding([one], versions, bundle)["classification"], "potential_conflict")
        self.assertEqual(self.finding([yes], versions, bundle)["classification"], "no_conflict_observed")
        # Conflicting true and 1 observations in the same event still disagree.
        f = self.finding([one, yes], versions, bundle)
        self.assertEqual((f["classification"], f["reason"]),
                         ("contradictory_evidence", "same_event_field_disagrees"))
        # Missing / unknown evidence wins over any comparison.
        unknown = dict(make_fact(bundle, 1, "unknown"))
        f = self.finding([unknown], versions, bundle)
        self.assertEqual((f["classification"], f["reason"]), ("missing_evidence", "missing_or_NA_is_not_violation"))
        f = self.finding([], versions, bundle)
        self.assertEqual(f["classification"], "missing_evidence")
        # Counterevidence and cited exceptions keep precedence over the comparison.
        f = self.finding([make_fact(bundle, 1, 1, counterevidence=True)], versions, bundle)
        self.assertEqual(f["classification"], "contradictory_evidence")
        self.assertEqual(f["reason"], "counterevidence_requires_resolution")
        f = self.finding([make_fact(bundle, 1, 1, exception_applies=True)], versions, bundle)
        self.assertEqual(f["classification"], "exception_applies")
        # An unbound rule source keeps winning.
        unbound = policy_versions(bundle, "equals", True, source_sha=hashlib.sha256(b"other").hexdigest())
        f = self.finding([one], unbound, bundle)
        self.assertEqual((f["classification"], f["reason"]),
                         ("applicability_unresolved", "rule_source_or_requirement_span_unbound"))
        # An unresolved selection (out-of-scope event date) keeps winning.
        f = self.finding([one], versions, bundle, event_at="2020-01-01")
        self.assertEqual(f["classification"], "applicability_unresolved")
        # Unconfigured operator remains not_assessed.
        odd = policy_versions(bundle, "unknown", True)
        self.assertEqual(self.finding([one], odd, bundle)["classification"], "not_assessed")
        # Identity: true and 1 keep different finding IDs; reorder/repeat preserve outputs.
        id_one = self.finding([one], versions, bundle)["finding_id"]
        id_yes = self.finding([yes], versions, bundle)["finding_id"]
        self.assertNotEqual(id_one, id_yes)
        forward = reconcile.compare([one, yes], versions, bundle=bundle, **SCOPE)
        backward = reconcile.compare([yes, one], versions, bundle=bundle, **SCOPE)
        again = reconcile.compare([one, yes], versions, bundle=bundle, **SCOPE)
        self.assertEqual(evidence.canonical(forward), evidence.canonical(backward))
        self.assertEqual(evidence.canonical(forward), evidence.canonical(again))
        self.assertIs(type(one["value"]), int)
        self.assertIs(type(yes["value"]), bool)


def forbidden(*args, **kwargs):
    raise RuntimeError("rf0844_offline_operation_forbidden")


class CliTests(unittest.TestCase):
    def test_cli_emits_corrected_private_results_without_mutating_inputs(self):
        for observed, expected, label in ((1, True, CONFLICT), (0, False, CONFLICT), (True, True, NO_CONFLICT)):
            with self.subTest(observed=observed, expected=expected):
                bundle = make_bundle([observed])
                value = {"bundle": bundle, "versions": policy_versions(bundle, "equals", expected),
                         "facts": [make_fact(bundle, 1, observed)], "scope": dict(SCOPE)}
                before = copy.deepcopy(value)
                input_raw = evidence.canonical(value)
                with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
                    for owner, name in ((socket, "socket"), (socket, "create_connection"),
                                        (sqlite3, "connect"), (subprocess, "Popen")):
                        stack.enter_context(mock.patch.object(owner, name, forbidden))
                    root = Path(tmp)
                    root.chmod(0o700)
                    out = root / "out"
                    first = cli.run("reconcile", value, digest(input_raw), str(out))
                    self.assertIs(first["reused"], False)
                    run_dir = Path(first["output"])
                    result_raw = (run_dir / "result.json").read_bytes()
                    result = json.loads(result_raw)
                    finding = result["findings"][0]
                    self.assertEqual((finding["classification"], finding["reason"]), label)
                    self.assertIs(result["review_acceptance"], False)
                    self.assertEqual(result["legal_conclusions"], 0)
                    self.assertEqual(finding["legal_assessment"], "NOT_ASSESSED")
                    self.assertIn(label[0] + ": r1", (run_dir / "findings.md").read_text())
                    receipt = json.loads((run_dir / "receipt.json").read_bytes())
                    self.assertEqual(receipt["bindings"]["result_sha256"], digest(result_raw))
                    self.assertEqual(receipt["bindings"]["code_pins"], cli.code_pins())
                    self.assertEqual(receipt["bindings"]["code_pins"]["reconcile.py"],
                                     digest(Path(reconcile.__file__).read_bytes()))
                    second = cli.run("reconcile", value, digest(input_raw), str(out))
                    self.assertIs(second["reused"], True)
                    self.assertEqual(second["run_id"], first["run_id"])
                    self.assertEqual((run_dir / "result.json").read_bytes(), result_raw)
                self.assertEqual(evidence.canonical(value), input_raw)
                self.assertEqual(json.dumps(value, sort_keys=True), json.dumps(before, sort_keys=True))
                # execute() returns detached containers: mutating them cannot reach the input.
                executed = cli.execute("reconcile", value)
                executed["findings"][0]["citations"].clear()
                self.assertEqual(evidence.canonical(value), input_raw)


if __name__ == "__main__":
    unittest.main()
