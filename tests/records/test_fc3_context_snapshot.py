"""FC3-CONTEXT-SNAPSHOT: applicability results are detached snapshots.

Synthetic, source-bound fixtures only (shared vocabulary from
tests.records.test_context_revision); no files, models, network or corpus.
A returned applicability result, and every row returned by
compare_applicability, must share no mutable descendant with the input
context, rules or bases, or with another returned result. Values, hashes,
classifications and reasons are unchanged; only aliasing is removed.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest

from campaign_tool.records import context_revision as cr
from tests.records.test_context_revision import basis, context, period, rule


CASES = (
    "source_bound_positive_equal_values",
    "mutate_result_nested_evidence",
    "mutate_context_after_return",
    "independent_repeated_results",
    "multiple_compare_rows_detached",
    "excluded_and_unresolved_controls",
    "revision_hash_unchanged",
    "inputs_unchanged_during_call",
)


def labelled_sha(label):
    """Synthetic 64-hex identity derived from labelled fixture bytes."""
    return hashlib.sha256(("fc3-context-snapshot-fixture:" + label).encode("utf-8")).hexdigest()


# Replacement values of every JSON and non-JSON-ish type a caller might write.
MALFORMED_VALUES = (None, 0, -1, 1.5, True, False, "", "changed locator", [], ["x"], {}, {"k": "v"},
                    labelled_sha("replacement"))


def _not_apply(b):
    b["scope"] = "does_not_apply"
    b["scope_checks"]["jurisdiction"]["status"] = "does_not_apply"
    return b


def positive_scenarios():
    """(label, rule_id, rules, context, bases, expected classification, applicability)."""
    out = []
    r = rule()
    c = context()
    out.append(("conduct_applies", r["rule_id"], [r], c, [basis(r, c)], "LEGAL_REVIEW_REQUIRED", "applies"))
    r = rule()
    c = context()
    out.append(("conduct_does_not_apply", r["rule_id"], [r], c, [_not_apply(basis(r, c))],
                "NOT_APPLICABLE", "does_not_apply"))
    r = rule()
    c = context({"receipt": [period("2020-01-01", event_kind="request_received")]})
    out.append(("receipt_applies", r["rule_id"], [r], c, [basis(r, c, "receipt", "request_received")],
                "LEGAL_REVIEW_REQUIRED", "applies"))
    r = rule("2016-01-01", "2020-12-31")
    c = context({"retention": [period("2018-01-01", "2019-01-01", "detection_retention")]})
    out.append(("retention_applies", r["rule_id"], [r], c, [basis(r, c, "retention", "detection_retention")],
                "LEGAL_REVIEW_REQUIRED", "applies"))
    old, new = rule("2016-01-01", "2020-12-31"), rule("2021-01-01", duty="New duty")
    c = context({"conduct": [period("2021-01-01")]})
    out.append(("historical_selects_new", old["rule_id"], [old, new], c, [basis(old, c), basis(new, c)],
                "LEGAL_REVIEW_REQUIRED", "applies"))
    return out


def unresolved_scenarios():
    """(label, rule_id, rules, context, bases, expected reasons, excluded rules or None)."""
    out = []
    r, c = rule(), context()
    out.append(("rule_version_missing", "xx-absent", [r], c, [basis(r, c)], ["rule_version_missing"], None))
    r, c = rule(), context()
    out.append(("basis_missing", r["rule_id"], [r], c, [], ["missing_or_ambiguous_applicability_basis"], None))
    r, c = rule("2021-01-01"), context()
    out.append(("excluded_by_effective_interval", r["rule_id"], [r], c, [basis(r, c)],
                ["historical_version_or_coverage_unresolved"], [r]))
    r = rule("2016-01-01", "2020-06-30")
    c = context({"conduct": [period("2020-06-01", "2020-07-15")]})
    out.append(("crosses_rule_versions", r["rule_id"], [r], c, [basis(r, c)],
                ["event_period_crosses_rule_versions"], None))
    r = rule()
    c = context({"conduct": [period(), period("2019-01-01")]})
    out.append(("ambiguous_dates", r["rule_id"], [r], c, [basis(r, c)], ["event_date_missing_or_ambiguous"], None))
    r, c = rule(), context()
    b = basis(r, c)
    b["scope"] = "does_not_apply"
    out.append(("scope_unresolved", r["rule_id"], [r], c, [b], ["jurisdiction_role_or_scope_unresolved"], None))
    return out


def mutable_nodes(value, path="$"):
    """Every dict/list reachable from value as (path, object id)."""
    found = []
    if isinstance(value, dict):
        found.append((path, id(value)))
        for key, child in value.items():
            found.extend(mutable_nodes(child, path + "." + str(key)))
    elif isinstance(value, (list, tuple)):
        if isinstance(value, list):
            found.append((path, id(value)))
        for index, child in enumerate(value):
            found.extend(mutable_nodes(child, "%s[%d]" % (path, index)))
    return found


def _container(result, path):
    node = result
    for step in path[:-1]:
        node = node[step]
    return node, path[-1]


# Nested positions inside a successful result's event_period, including new keys.
RESULT_TARGETS = (
    ("event_period", "evidence", 0, "locator"),
    ("event_period", "evidence", 0, "source_sha256"),
    ("event_period", "evidence", 0, "occurrence_id"),
    ("event_period", "evidence", 0, "injected"),
    ("event_period", "evidence", 0),
    ("event_period", "evidence"),
    ("event_period", "start"),
    ("event_period", "end"),
    ("event_period", "event_kind"),
    ("event_period", "injected"),
)

# Structural edits that do not assign a scalar.
RESULT_OPERATIONS = (
    ("evidence_append", lambda res: res["event_period"]["evidence"].append(
        {"source_sha256": labelled_sha("appended"), "locator": "appended row"})),
    ("evidence_extend_duplicate", lambda res: res["event_period"]["evidence"].extend(
        copy.deepcopy(res["event_period"]["evidence"]))),
    ("evidence_insert", lambda res: res["event_period"]["evidence"].insert(0, "not a ref")),
    ("evidence_clear", lambda res: res["event_period"]["evidence"].clear()),
    ("evidence_pop", lambda res: res["event_period"]["evidence"].pop()),
    ("evidence_ref_clear", lambda res: res["event_period"]["evidence"][0].clear()),
    ("evidence_ref_pop_locator", lambda res: res["event_period"]["evidence"][0].pop("locator")),
    ("evidence_ref_update", lambda res: res["event_period"]["evidence"][0].update(
        locator="updated", source_sha256=labelled_sha("updated"))),
    ("period_clear", lambda res: res["event_period"].clear()),
    ("period_pop_evidence", lambda res: res["event_period"].pop("evidence")),
    ("period_update", lambda res: res["event_period"].update(start="1999-01-01", end="1999-12-31")),
    ("reasons_append", lambda res: res["reasons"].append("injected_reason")),
)


def result_mutations():
    for path in RESULT_TARGETS:
        for value in MALFORMED_VALUES:
            def assign(res, path=path, value=value):
                node, key = _container(res, path)
                node[key] = copy.deepcopy(value)
            yield "set%s=%r" % (list(path), value), assign
    for label, operation in RESULT_OPERATIONS:
        yield label, operation


def input_mutations(role):
    """Edits to context, rules or bases made after a result was returned."""
    targets = (
        ("context", ("dates", role, 0, "evidence", 0, "locator")),
        ("context", ("dates", role, 0, "evidence", 0, "source_sha256")),
        ("context", ("dates", role, 0, "evidence", 0)),
        ("context", ("dates", role, 0, "evidence")),
        ("context", ("dates", role, 0, "start")),
        ("context", ("dates", role, 0, "event_kind")),
        ("context", ("dates", role, 0)),
        ("context", ("evidence_context", "occurrence_ids", 0)),
        ("rules", (0, "sources", 0, "sha256")),
        ("rules", (0, "effective_from")),
        ("bases", (0, "version_evidence", 0, "locator")),
        ("bases", (0, "scope_checks", "jurisdiction", "evidence")),
        ("bases", (0, "scope")),
    )
    for which, path in targets:
        for value in MALFORMED_VALUES:
            def assign(inputs, which=which, path=path, value=value):
                node, key = _container(inputs[which], path)
                node[key] = copy.deepcopy(value)
            yield "%s%s=%r" % (which, list(path), value), assign
    yield "context_evidence_append", lambda inputs: inputs["context"]["dates"][role][0]["evidence"].append(
        {"source_sha256": labelled_sha("context-appended"), "locator": "context row"})
    yield "context_evidence_clear", lambda inputs: inputs["context"]["dates"][role][0]["evidence"].clear()
    yield "context_period_clear", lambda inputs: inputs["context"]["dates"][role][0].clear()
    yield "context_role_clear", lambda inputs: inputs["context"]["dates"][role].clear()
    yield "rules_clear", lambda inputs: inputs["rules"].clear()
    yield "bases_clear", lambda inputs: inputs["bases"].clear()


class FC3ContextSnapshotTests(unittest.TestCase):
    def assertDetached(self, result, *others):
        mine = dict((ident, path) for path, ident in mutable_nodes(result))
        for index, other in enumerate(others):
            shared = [(mine[ident], path) for path, ident in mutable_nodes(other) if ident in mine]
            self.assertEqual(shared, [], "result shares mutable objects with input/result #%d" % index)

    def assertJsonCompatible(self, result):
        self.assertIs(type(result), dict)
        self.assertEqual(json.loads(cr.canonical(result)), result)

        def walk(value):
            if isinstance(value, dict):
                self.assertTrue(all(type(key) is str for key in value))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
            else:
                self.assertIn(type(value), (str, int, float, bool, type(None)))
        walk(result)

    def expected_positive(self, rule_id, rules, c, bases, classification, applicability):
        """Pre-mutation values built independently of the resolver's internals."""
        rule_id_matches = [r for r in rules if r["rule_id"] == rule_id]
        chosen = None
        for b in bases:
            for r in rule_id_matches:
                if b["rule_revision_sha256"] == cr.rule_revision(r):
                    role_period = c["dates"][b["event_role"]][0]
                    start = role_period["start"]
                    end = role_period["end"] or start
                    if r["effective_from"] <= start and (r["effective_to"] is None or end <= r["effective_to"]):
                        chosen = (r, b)
        if chosen is None:
            self.fail("fixture has no covering rule/basis pair")
        r, b = chosen
        return {
            "rule_id": rule_id, "classification": classification, "applicability": applicability,
            "context_sha256": cr.fingerprint(c), "rule_revision_sha256": cr.rule_revision(r),
            "event_role": b["event_role"], "event_period": copy.deepcopy(c["dates"][b["event_role"]][0]),
            "basis_sha256": cr.fingerprint(b),
            "reasons": ["source_bound_scope_requires_legal_review" if applicability == "applies"
                        else "source_bound_scope_exclusion"],
        }

    def test_source_bound_positive_equal_values(self):
        for label, rule_id, rules, c, bases, classification, applicability in positive_scenarios():
            with self.subTest(scenario=label):
                result = cr.resolve_applicability(rule_id, rules, c, bases)
                self.assertEqual(result, self.expected_positive(rule_id, rules, c, bases,
                                                                classification, applicability))
                self.assertJsonCompatible(result)
                tuple_result = cr.resolve_applicability(rule_id, rules, c, tuple(bases))
                self.assertEqual(tuple_result, result)
                compared = cr.compare_applicability([rule_id, rule_id], rules, c, bases)
                self.assertEqual(compared["comparisons"], [result])
                self.assertEqual(compared["classification"], "LEGAL_REVIEW_REQUIRED")
                self.assertJsonCompatible(compared)

    def test_mutate_result_nested_evidence(self):
        for label, rule_id, rules, c, bases, _, _ in positive_scenarios():
            role = cr.resolve_applicability(rule_id, rules, c, bases)["event_role"]
            for mutation, apply in result_mutations():
                with self.subTest(scenario=label, mutation=mutation):
                    inputs = copy.deepcopy({"rules": rules, "context": c, "bases": bases})
                    snapshot = copy.deepcopy(inputs)
                    result = cr.resolve_applicability(rule_id, inputs["rules"], inputs["context"], inputs["bases"])
                    before = copy.deepcopy(result)
                    self.assertDetached(result, inputs)
                    apply(result)
                    self.assertEqual(inputs, snapshot)
                    self.assertEqual(cr.fingerprint(inputs["context"]), before["context_sha256"])
                    self.assertEqual(inputs["context"]["dates"][role][0], before["event_period"])
                    again = cr.resolve_applicability(rule_id, inputs["rules"], inputs["context"], inputs["bases"])
                    self.assertEqual(again, before)

    def test_mutate_context_after_return(self):
        for label, rule_id, rules, c, bases, _, _ in positive_scenarios():
            role = cr.resolve_applicability(rule_id, rules, c, bases)["event_role"]
            for mutation, apply in input_mutations(role):
                with self.subTest(scenario=label, mutation=mutation):
                    inputs = copy.deepcopy({"rules": rules, "context": c, "bases": bases})
                    result = cr.resolve_applicability(rule_id, inputs["rules"], inputs["context"], inputs["bases"])
                    rows = cr.compare_applicability([rule_id], inputs["rules"], inputs["context"], inputs["bases"])
                    before, rows_before = copy.deepcopy(result), copy.deepcopy(rows)
                    apply(inputs)
                    self.assertEqual(result, before)
                    self.assertEqual(rows, rows_before)
                    self.assertEqual(result["context_sha256"], cr.fingerprint(c))

    def test_independent_repeated_results(self):
        for label, rule_id, rules, c, bases, _, _ in positive_scenarios():
            for mutation, apply in result_mutations():
                with self.subTest(scenario=label, mutation=mutation):
                    first = cr.resolve_applicability(rule_id, rules, c, bases)
                    second = cr.resolve_applicability(rule_id, rules, c, bases)
                    row = cr.compare_applicability([rule_id], rules, c, bases)["comparisons"][0]
                    self.assertEqual(first, second)
                    self.assertDetached(first, second, row)
                    self.assertDetached(second, row)
                    before = copy.deepcopy(second)
                    apply(first)
                    self.assertEqual(second, before)
                    self.assertEqual(row, before)

    def test_multiple_compare_rows_detached(self):
        first, second = rule(), rule(rule_id="xx-second")
        variants = []
        c = context()
        variants.append(("two_applies", [first, second], c, [basis(first, c), basis(second, c)]))
        c = context()
        variants.append(("applies_and_does_not_apply", [first, second], c,
                         [basis(first, c), _not_apply(basis(second, c))]))
        c = context({"conduct": [period("2018-01-01")],
                     "retention": [period("2018-01-01", "2019-01-01", "detection_retention")]})
        variants.append(("conduct_and_retention", [first, second], c,
                         [basis(first, c), basis(second, c, "retention", "detection_retention")]))
        for label, rules, c, bases in variants:
            for mutation, apply in result_mutations():
                with self.subTest(variant=label, mutation=mutation):
                    inputs = copy.deepcopy({"rules": rules, "context": c, "bases": bases})
                    snapshot = copy.deepcopy(inputs)
                    out = cr.compare_applicability(["xx-second", "xx-synthetic", "xx-second"], inputs["rules"],
                                                   inputs["context"], inputs["bases"])
                    rows = out["comparisons"]
                    self.assertEqual([row["rule_id"] for row in rows], ["xx-second", "xx-synthetic"])
                    self.assertEqual(out["classification"], "LEGAL_REVIEW_REQUIRED")
                    for index, row in enumerate(rows):
                        single = cr.resolve_applicability(row["rule_id"], inputs["rules"], inputs["context"],
                                                          inputs["bases"])
                        self.assertEqual(row, single)
                        self.assertDetached(row, inputs, single, *rows[index + 1:])
                    before = copy.deepcopy(out)
                    apply(rows[0])
                    self.assertEqual(rows[1], before["comparisons"][1])
                    self.assertEqual(inputs, snapshot)
                    apply(rows[1])
                    self.assertEqual(inputs, snapshot)
                    again = cr.compare_applicability(["xx-second", "xx-synthetic"], inputs["rules"],
                                                     inputs["context"], inputs["bases"])
                    self.assertEqual(again, before)

    def test_excluded_and_unresolved_controls(self):
        for label, rule_id, rules, c, bases, reasons, excluded in unresolved_scenarios():
            with self.subTest(scenario=label):
                inputs = copy.deepcopy({"rules": rules, "context": c, "bases": bases})
                snapshot = copy.deepcopy(inputs)
                result = cr.resolve_applicability(rule_id, inputs["rules"], inputs["context"], inputs["bases"])
                expected = {"rule_id": rule_id, "classification": cr.UNRESOLVED, "applicability": "unknown",
                            "context_sha256": cr.fingerprint(c), "rule_revision_sha256": None,
                            "event_role": None, "event_period": None, "reasons": reasons}
                if excluded is not None:
                    expected["excluded_revision_sha256s"] = [cr.rule_revision(r) for r in excluded]
                self.assertEqual(result, expected)
                self.assertJsonCompatible(result)
                self.assertDetached(result, inputs)
                before = copy.deepcopy(result)
                result["reasons"].append("injected")
                if excluded is not None:
                    result["excluded_revision_sha256s"].append(labelled_sha("excluded"))
                self.assertEqual(inputs, snapshot)
                self.assertEqual(cr.resolve_applicability(rule_id, inputs["rules"], inputs["context"],
                                                          inputs["bases"]), before)
                out = cr.compare_applicability([rule_id], inputs["rules"], inputs["context"], inputs["bases"])
                self.assertEqual(out["classification"], cr.UNRESOLVED)
                self.assertEqual(out["status"], "blocked")
                self.assertEqual(out["comparisons"], [before])
                self.assertDetached(out, inputs)
        empty = cr.compare_applicability([], [rule()], context())
        self.assertEqual(empty["reasons"], ["no_matched_duties_is_not_inapplicability"])
        self.assertEqual(empty["classification"], cr.UNRESOLVED)

    def test_revision_hash_unchanged(self):
        for label, rule_id, rules, c, bases, _, _ in positive_scenarios():
            with self.subTest(scenario=label):
                inputs = copy.deepcopy({"rules": rules, "context": c, "bases": bases})
                values = {"subject_sha256": labelled_sha("subject"),
                          "extraction": {"receipt_sha256": labelled_sha("receipt")},
                          "versions": {key: key + "-v1" for key in cr.VERSION_KEYS},
                          "rule_context": {"status": "reviewed"}}
                previous = cr.build_revision(context=inputs["context"], rules=inputs["rules"],
                                             applicability_bases=inputs["bases"], **values)
                context_hash = cr.fingerprint(inputs["context"])
                for mutation, apply in result_mutations():
                    result = cr.resolve_applicability(rule_id, inputs["rules"], inputs["context"], inputs["bases"])
                    row = cr.compare_applicability([rule_id], inputs["rules"], inputs["context"],
                                                   inputs["bases"])["comparisons"][0]
                    apply(result)
                    apply(row)
                    self.assertEqual(cr.fingerprint(inputs["context"]), context_hash, mutation)
                current = cr.build_revision(context=inputs["context"], rules=inputs["rules"],
                                            applicability_bases=inputs["bases"], **values)
                self.assertEqual(current, previous)
                self.assertEqual(cr.invalidation_plan(previous, current)["stages"], [])
                self.assertEqual(cr.fingerprint(inputs["context"]), context_hash)

    def test_inputs_unchanged_during_call(self):
        scenarios = [(s[0], s[1], s[2], s[3], s[4]) for s in positive_scenarios()]
        scenarios += [(s[0], s[1], s[2], s[3], s[4]) for s in unresolved_scenarios()]
        for label, rule_id, rules, c, bases in scenarios:
            for container in (list, tuple):
                with self.subTest(scenario=label, bases=container.__name__):
                    inputs = copy.deepcopy({"rules": rules, "context": c, "bases": bases})
                    canonical_before = {key: cr.canonical(value) for key, value in inputs.items()}
                    ids_before = mutable_nodes(inputs)
                    call_bases = container(inputs["bases"])
                    result = cr.resolve_applicability(rule_id, inputs["rules"], inputs["context"], call_bases)
                    out = cr.compare_applicability([rule_id], inputs["rules"], inputs["context"], call_bases)
                    self.assertEqual({key: cr.canonical(value) for key, value in inputs.items()},
                                     canonical_before)
                    self.assertEqual(mutable_nodes(inputs), ids_before)
                    self.assertDetached(result, inputs, out)
                    self.assertDetached(out, inputs)


# ------------------------------------------------------------------------------------------
# Acceptance-runner gate regressions (review round 1). These exercise the real gate code of
# tools/accept_fc3_context_snapshot.py with a fake recorder: no process is started.

MISSING = object()


def load_runner():
    path = Path(__file__).resolve().parents[2] / "tools" / "accept_fc3_context_snapshot.py"
    spec = importlib.util.spec_from_file_location("fc3_acceptance_runner_under_test", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load acceptance runner from %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRecorder:
    """Stands in for the runner's Recorder; behaviour(label, cwd) -> (exit, payload, stdout)."""

    def __init__(self, run_dir, behaviour):
        self.run_dir = run_dir
        self.behaviour = behaviour
        self.children = []
        self.git_calls = []

    def git(self, *args):
        self.git_calls.append(list(args))
        return ""

    def child(self, label, argv, cwd, env=None, timeout=1800):
        argv = [str(item) for item in argv]
        exit_code, payload, stdout = self.behaviour(label, Path(cwd))
        if "--child-result" in argv and payload is not MISSING:
            target = Path(argv[argv.index("--child-result") + 1])
            target.write_text(json.dumps(payload), "utf-8")
        record = {"label": label, "argv": argv, "cwd": str(cwd), "exit": exit_code}
        self.children.append(record)
        return record, types.SimpleNamespace(returncode=exit_code, stdout=stdout, stderr=b"")


class FC3AcceptanceRunnerGateTests(unittest.TestCase):
    TEST_MODES = ("focused", "compatibility", "negative", "portability")
    BAD_EXITS = (1, 2, 37, 255, -9, None)
    ERROR_VALUES = ("Traceback (most recent call last): synthetic", "", None, {"detail": "x"}, ["x"], 0)

    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner()

    def suite(self, failing=()):
        r = self.runner
        outcomes = {}
        for case in r.CASES + r.GATE_CASES:
            status = "fail" if case in failing else "pass"
            outcomes[r.case_test_id(case)] = {"status": status, "subtests_failed": 1 if failing else 0}
        return {"tests_run": r.FOCUSED_TESTS, "failures": len(failing), "errors": 0, "skipped": 0,
                "expected_failures": 0, "unexpected_successes": 0, "subtests_passed": 5,
                "outcomes": outcomes, "was_successful": not failing}

    def green_payload(self, label, root):
        r = self.runner
        payload = {"repo_root": str(root), "sentinel_hits": [], "refused_subprocesses": [],
                   "foreign_modules": [], "unredirected_constants": [], "subprocesses": [],
                   "loaded_repo_modules": {
                       "campaign_tool.records.context_revision": str(root / r.PRODUCTION_REL),
                       r.FOCUSED_MODULE: str(root / r.TEST_REL),
                       "tests.records.test_context_revision": str(root / "tests/records/test_context_revision.py")}}
        if label == "compatibility":
            payload["suite"] = {"tests_run": r.COMPATIBILITY_EXPECTED_TESTS, "failures": 0, "errors": 0,
                                "skipped": 0, "expected_failures": 0, "unexpected_successes": 0,
                                "outcomes": {}, "was_successful": True}
        elif label == "negative":
            payload.update(broken_phase=self.suite(failing=r.NEGATIVE_MUST_FAIL), restored_phase=self.suite(),
                           wrapper_installed=True, restored_identity=True,
                           source_sha256_before="same", source_sha256_after="same",
                           reference_before=[{"scenario": "s"}], reference_after=[{"scenario": "s"}])
        else:
            payload["suite"] = self.suite()
        return payload

    def run_mode(self, mode, behaviour):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = FakeRecorder(Path(tmp), behaviour)
            receipt = {}
            problems = self.runner.MODES[mode](recorder, Path(tmp), receipt)
            return problems, receipt, recorder

    def test_runner_accepts_clean_child(self):
        for mode in self.TEST_MODES:
            with self.subTest(mode=mode):
                problems, receipt, recorder = self.run_mode(
                    mode, lambda label, cwd: (0, self.green_payload(label, cwd), b""))
                self.assertEqual(problems, [])
                self.assertTrue(recorder.children)
                self.assertTrue(all(child["exit"] == 0 for child in recorder.children))
        problems, receipt, _ = self.run_mode("public-tree", lambda label, cwd: (0, MISSING, b""))
        self.assertEqual(problems, [])
        self.assertEqual(receipt["public_tree_exit"], 0)

    def test_runner_rejects_nonzero_child_exit(self):
        for mode in self.TEST_MODES:
            for bad in self.BAD_EXITS:
                with self.subTest(mode=mode, exit=bad):
                    problems, _, _ = self.run_mode(
                        mode, lambda label, cwd, bad=bad: (bad, self.green_payload(label, cwd), b""))
                    self.assertTrue(problems, "green result with child exit %r was accepted" % (bad,))
                    self.assertTrue(any("exit %r" % (bad,) in item for item in problems), problems)
        with self.subTest(mode="portability", variant="one_of_two_children"):
            problems, _, _ = self.run_mode("portability", lambda label, cwd: (
                37 if label == "portability-relocated" else 0, self.green_payload(label, cwd), b""))
            self.assertTrue(any("portability-relocated: test child exit 37" in item for item in problems))
            self.assertFalse(any("portability-original: test child exit" in item for item in problems))
        for bad in (1, 37):
            with self.subTest(mode="public-tree", exit=bad):
                problems, _, _ = self.run_mode("public-tree", lambda label, cwd, bad=bad: (bad, MISSING, b""))
                self.assertTrue(problems)
            with self.subTest(mode="credentials", exit=bad):
                problems, _, _ = self.run_mode("credentials", lambda label, cwd, bad=bad: (
                    bad, MISSING, b'{"version": "synthetic", "results": {}}'))
                self.assertTrue(problems)

    def test_runner_rejects_child_level_error(self):
        for mode in self.TEST_MODES:
            for value in self.ERROR_VALUES:
                with self.subTest(mode=mode, error=value):
                    def behaviour(label, cwd, value=value):
                        payload = self.green_payload(label, cwd)
                        payload["error"] = value
                        return 0, payload, b""
                    problems, _, _ = self.run_mode(mode, behaviour)
                    self.assertTrue(any("child-level error" in item for item in problems), problems)
            for variant, payload in (("missing", MISSING), ("list", ["not", "a", "mapping"]),
                                     ("string", "green"), ("null", None)):
                with self.subTest(mode=mode, result=variant):
                    problems, _, _ = self.run_mode(mode, lambda label, cwd, payload=payload: (0, payload, b""))
                    self.assertTrue(problems, "exit 0 with %s result was accepted" % variant)
        with self.subTest(mode="negative", variant="restored_phase_red_exit_zero"):
            def red_restored(label, cwd):
                payload = self.green_payload(label, cwd)
                payload["restored_phase"] = self.suite(failing=("mutate_result_nested_evidence",))
                return 0, payload, b""
            problems, _, _ = self.run_mode("negative", red_restored)
            self.assertTrue(any(item.startswith("restored:") for item in problems), problems)

    def test_runner_receipt_binds_brief_identity(self):
        r = self.runner
        headers = []
        with tempfile.TemporaryDirectory() as tmp:
            for check in r.CHECKS:
                headers.append(r.receipt_header(types.SimpleNamespace(check=check), Path(tmp)))
        self.assertEqual(len(headers), 6)
        for header in headers:
            brief = header["brief"]
            self.assertEqual(brief, headers[0]["brief"])
            self.assertEqual(brief["name"], "deflock-FC3-CONTEXT-SNAPSHOT.brief")
            self.assertEqual(len(brief["sha256"]), 64)
            self.assertEqual(bytes.fromhex(brief["sha256"]).hex(), brief["sha256"])
            # Admitted digest anchors (operator admission record); short, low-entropy fragments.
            self.assertTrue(brief["sha256"].startswith("1f6ccbe9"))
            self.assertTrue(brief["sha256"].endswith("b6645ea6"))
            self.assertTrue(brief["packet_manifest_sha256"].startswith("4e18724b"))
            self.assertEqual(header["card"], "t_cf694e2f")
        source = Path(r.__file__).read_text("utf-8")
        self.assertIn("receipt = receipt_header(args, run_dir)", source)


if __name__ == "__main__":
    unittest.main()
