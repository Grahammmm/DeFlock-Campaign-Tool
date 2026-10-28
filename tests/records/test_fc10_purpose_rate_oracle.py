"""Finite synthetic purpose-rate oracle; inert during ordinary discovery.

No adapter suppression, real records, persistence or semantic acceptance.
Script mode redirects temporary storage before any product import. All offline
patches, including the deliberate comparison mutation, have scoped lifetimes.
"""
import argparse
import ast
from contextlib import ExitStack, contextmanager
import copy
import hashlib
import importlib
import inspect

import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch


def specification():
    return json.loads((Path(__file__).resolve().parents[1] / "fixtures" /
                       "fc10-purpose-rate-oracle" / "cases.json").read_text())


def fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()


def oracle(total, bad):
    # Algebraically independent of the product's percentage expression.
    if 4 * bad > total:
        return 3
    if 20 * bad > total:
        return 2
    return 0


@contextmanager
def offline():
    def denied(*args, **kwargs):
        raise RuntimeError("offline purpose oracle forbids external side effects")
    with ExitStack() as stack:
        for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                            (socket.socket, "bind"), (socket.socket, "listen"),
                            (socket, "create_connection"), (socket, "getaddrinfo"),
                            (subprocess, "Popen")):
            stack.enter_context(patch.object(owner, name, denied))
        import os
        stack.enter_context(patch.object(os, "system", denied))
        yield


def population(groups, spec=None):
    spec = specification() if spec is None else spec
    units = []
    original = fingerprint("fc10 synthetic purpose export")
    for user, total, bad in groups:
        for index in range(total):
            units.append({"original_sha256": original, "locator": {"row": len(units) + 1},
                          "event_id": user + "-" + str(index), "record_type": "search-log",
                          "event_at": "2020-01-20T12:00:00+00:00",
                          "fields": {"user_id": user, "purpose": spec["bad_purposes"][0] if index < bad
                                     else spec["specific_purposes"][0]}})
    joins = [{"original_sha256": original, "status": "typed", "agency_id": "synthetic-agency"}]
    rule = {"detector": "purpose-quality", "agency_id": "synthetic-agency", "rule_id": "synthetic-rule",
            "version": "synthetic-v1", "source_sha256": fingerprint("fc10 synthetic purpose policy"),
            "effective_from": "2020-01-01", "effective_to": "2021-01-01",
            "parameters": {"generic_reasons": spec["generic_reasons"]}}
    return [units, joins, {"version": "synthetic-pack-v1", "entries": [rule]}, {}]


@contextmanager
def inclusive_control(core):
    """Compile just _aggregate with exactly one in-memory comparison changed."""
    source = textwrap.dedent(inspect.getsource(core._aggregate))
    tree = ast.parse(source)
    old = ast.dump(ast.parse(specification()["negative_control"]["comparison"], mode="eval").body)
    matches = [node for node in ast.walk(tree) if isinstance(node, ast.Compare) and ast.dump(node) == old]
    if len(matches) != 1:
        raise RuntimeError("control comparison must have exactly one source match")
    matches[0].ops = [ast.GtE()]
    namespace = dict(core.__dict__)
    exec(compile(ast.fix_missing_locations(tree), "<synthetic-inclusive-comparison>", "exec"), namespace)
    with patch.object(core, "_aggregate", namespace["_aggregate"]):
        yield {"source": source, "mutated_function": ast.unparse(tree), "comparisons_changed": len(matches)}


class PurposeRateOracleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(offline())
        self.core = importlib.import_module("campaign_tool.records.detectors.core")
        self.spec = specification()
        self.observations = []

    def evaluate(self, values):
        before = copy.deepcopy(values)
        result = self.core.evaluate("purpose-quality", *values)
        self.assertEqual(values, before, "inputs immutable during evaluation")
        return result

    def check_population(self, result, groups, label, explicit=None):
        counts = result["manifest"]["counts"]
        total = sum(n for _, n, _ in groups)
        self.assertEqual(counts["evaluated"], total, "all grouping prerequisites valid")
        self.assertEqual(counts["blocked"], 0)
        self.assertEqual(counts["skipped"], 0)
        self.assertEqual(counts["unique_records"], total)
        expected: dict[tuple[str, ...], tuple[int, int]] = {("overall",): (total, sum(b for _, _, b in groups))}
        expected.update({("user", u): (n, b) for u, n, b in groups})
        severities = {scope: oracle(n, b) for scope, (n, b) in expected.items()}
        if explicit is not None:
            for scope in expected:
                self.assertEqual(severities[scope], explicit, "frozen literal oracle check")
                severities[scope] = explicit
        hits = {tuple(h["observed_values_private"]["scope"]): h for h in result["hits"]}
        self.assertEqual(len(hits), len(result["hits"]), "no duplicate scope hits")
        for hit in result["hits"]:
            self.assertEqual(hit["signal"], "qualified_purpose_quality_rate")
            self.assertTrue(hit["triage_only"])
            self.assertFalse(hit["publication_ready"])
            self.assertTrue(hit["evidence"])
        observation = {"case": label, "expected": [{"scope": list(s), "total": n, "bad": b,
                         "severity": severities[s]} for s, (n, b) in expected.items()],
                       "actual": [{"severity": h["severity"], **h["observed_values_private"]}
                                  for h in result["hits"]], "counts": counts}
        self.observations.append(observation)
        self.assertEqual(set(hits), {s for s in expected if severities[s]}, "strict_threshold_scope_set")
        for scope, hit in hits.items():
            n, b = expected[scope]
            self.assertEqual(hit["severity"], severities[scope], "strict_threshold_severity")
            self.assertEqual(hit["observed_values_private"], {
                "scope": list(scope), "qualified_problem_observations": b, "qualified_observations": n,
                "blank_missing_redacted_excluded": True})
            self.assertEqual(len(hit["evidence"]), n)
        return hits

    def test_finite_total_bad_grid(self):
        for total in range(self.spec["grid"]["minimum_total"], self.spec["grid"]["maximum_total"] + 1):
            for bad in range(total + 1):
                with self.subTest(total=total, bad=bad):
                    groups = [("alpha", total, bad)]
                    self.check_population(self.evaluate(population(groups, self.spec)), groups, ["grid", total, bad])

    def test_differing_user_rates(self):
        # Exercise every per-user grid ratio beside a genuinely different peer.
        for total in range(1, self.spec["grid"]["maximum_total"] + 1):
            for bad in range(total + 1):
                with self.subTest(total=total, bad=bad):
                    groups = [("alpha", total, bad), ("beta", 7, 0 if bad else 7)]
                    self.check_population(self.evaluate(population(groups, self.spec)), groups,
                                          ["unequal_grid", total, bad])
        for case in self.spec["unequal_users"]:
            groups = [(u, *case[u]) for u in ("alpha", "beta")]
            self.check_population(self.evaluate(population(groups, self.spec)), groups, ["unequal_literal", case])

    def test_exact_five_and_twenty_five_percent(self):
        for case in self.spec["boundaries"]:
            with self.subTest(**case):
                groups = [("alpha", case["total"], case["bad"])]
                self.check_population(self.evaluate(population(groups, self.spec)), groups,
                                      ["boundary", case], explicit=case["severity"])

    def test_exact_five_percent_deciding(self):
        groups = [("alpha", 20, 1)]
        self.check_population(self.evaluate(population(groups, self.spec)), groups,
                              "independently_frozen_exact_five", explicit=0)

    def test_same_counts_different_members(self):
        groups = [("alpha", 20, 6), ("beta", 20, 1)]
        first = population(groups, self.spec)
        before = self.check_population(self.evaluate(first), groups, "members_before")
        changed = copy.deepcopy(first)
        # Swap bad/good members, holding every scope's numerator/denominator constant.
        for start, a, b in ((0, 0, 19), (20, 0, 19)):
            fields_a, fields_b = changed[0][start + a]["fields"], changed[0][start + b]["fields"]
            fields_a["purpose"], fields_b["purpose"] = fields_b["purpose"], fields_a["purpose"]
        after = self.check_population(self.evaluate(changed), groups, "members_after")
        self.assertEqual(set(before), set(after))
        for scope in before:
            self.assertEqual(before[scope]["observed_values_private"], after[scope]["observed_values_private"])
            self.assertNotEqual(before[scope]["semantic_observations_sha256"], after[scope]["semantic_observations_sha256"])
            self.assertNotEqual(before[scope]["dedupe_key"], after[scope]["dedupe_key"])

    def test_generic_casefold_and_case_number(self):
        for purpose in self.spec["bad_purposes"]:
            with self.subTest(purpose=purpose):
                groups = [("alpha", 1, 1)]
                values = population(groups, self.spec)
                values[0][0]["fields"]["purpose"] = purpose
                self.check_population(self.evaluate(values), groups, ["bad_purpose", purpose], explicit=3)

    def test_legitimate_specific_purpose(self):
        for purpose in self.spec["specific_purposes"]:
            with self.subTest(purpose=purpose):
                groups = [("alpha", 1, 0)]
                values = population(groups, self.spec)
                values[0][0]["fields"]["purpose"] = purpose
                self.check_population(self.evaluate(values), groups, ["specific_purpose", purpose], explicit=0)

    def test_duplicate_print_no_denominator_inflation(self):
        groups = [("alpha", 20, 5), ("beta", 4, 1)]
        values = population(groups, self.spec)
        baseline = self.check_population(self.evaluate(values), groups, "duplicate_baseline")
        # Duplicate bad AND good events for each user; all print references survive.
        for index in (0, 19, 20, 23):
            duplicate = copy.deepcopy(values[0][index])
            duplicate["locator"] = {"row": 100 + index}
            values[0].append(duplicate)
        result = self.evaluate(values)
        self.assertEqual(result["manifest"]["counts"]["duplicate_rows"], 4)
        self.assertEqual(result["manifest"]["counts"]["evaluated"], 24)
        after = {tuple(h["observed_values_private"]["scope"]): h for h in result["hits"]}
        self.assertEqual(set(after), set(baseline))
        for scope, hit in after.items():
            self.assertEqual(hit["observed_values_private"], baseline[scope]["observed_values_private"])
            self.assertEqual(hit["severity"], baseline[scope]["severity"])
            self.assertEqual(hit["dedupe_key"], baseline[scope]["dedupe_key"])
            expected = [u for u in values[0] if scope == ("overall",) or u["fields"]["user_id"] == scope[1]]
            self.assertEqual({json.dumps(r, sort_keys=True) for r in hit["evidence"]},
                             {json.dumps({"original_sha256": u["original_sha256"], "locator": u["locator"]}, sort_keys=True)
                              for u in expected})
        self.observations.append({"case": "duplicate_print", "counts": result["manifest"]["counts"],
                                  "evidence_per_scope": {str(s): len(h["evidence"]) for s, h in after.items()}})

    def test_blank_missing_redacted_outcomes_separate(self):
        groups = [("alpha", 20, 5), ("beta", 20, 1)]
        values = population(groups, self.spec)
        qualified = self.evaluate(values)
        for i, case in enumerate(self.spec["excluded"]):
            row = copy.deepcopy(values[0][0])
            row.update(event_id="excluded-" + case["label"], locator={"row": 200 + i})
            if case.get("omit"):
                row["fields"].pop("purpose")
            else:
                row["fields"]["purpose"] = copy.deepcopy(case["value"])
            values[0].append(row)
        result = self.evaluate(values)
        self.assertEqual(result["hits"], qualified["hits"], "unqualified rows do not enter either population")
        self.assertEqual(result["manifest"]["counts"]["evaluated"], 40)
        blocked = [r for r in result["manifest"]["outcomes"] if r["disposition"] == "blocked"]
        self.assertEqual(len(blocked), len(self.spec["excluded"]))
        for i, case in enumerate(self.spec["excluded"]):
            matching = [r for r in blocked if r["evidence"][0]["locator"] == {"row": 200 + i}]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]["reason"], case["reason"])
        self.observations.append({"case": "excluded", "outcomes": blocked, "counts": result["manifest"]["counts"]})

    def test_immutable_inputs(self):
        groups = [("alpha", 20, 6), ("beta", 20, 1)]
        values = population(groups, self.spec)
        before = copy.deepcopy(values)
        result = self.evaluate(values)
        self.check_population(result, groups, "immutable")
        result["hits"][0]["evidence"][0]["locator"]["row"] = 999
        result["manifest"]["outcomes"][0]["evidence"][0]["locator"]["row"] = 998
        self.assertEqual(values, before, "outputs do not alias input objects")


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ids, self.cases, self.subtests = [], [], []

    def startTest(self, test):
        self.ids.append(test.id())
        super().startTest(test)

    def stopTest(self, test):
        self.cases.extend(getattr(test, "observations", []))
        super().stopTest(test)

    def addSubTest(self, test, subtest, err):
        self.subtests.append({"id": subtest.id(), "passed": err is None})
        super().addSubTest(test, subtest, err)


def run_recorded(suite, report):
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    data = {"tests_run": result.testsRun, "ids": result.ids, "failures": [(t.id(), msg) for t, msg in result.failures],
            "errors": [(t.id(), msg) for t, msg in result.errors], "skips": [(t.id(), why) for t, why in result.skipped],
            "subtests": result.subtests, "observations": result.cases, "observation_count": len(result.cases),
            "passed": result.wasSuccessful() and not result.skipped}
    if report is not None:
        Path(report).write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temp-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = args.temp_root.resolve(strict=True)
    tempfile.tempdir = str(root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    data = run_recorded(unittest.defaultTestLoader.loadTestsFromTestCase(PurposeRateOracleTests), args.report)
    return int(not data["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
