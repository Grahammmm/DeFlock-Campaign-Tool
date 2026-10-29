"""Independently authored fictional FC15 tests: Sol 6.1 / OpenAI Codex.

Imports are inert under ordinary unittest discovery. Product imports and offline
sentinels are scoped to each test; no environment/tempfile/audit-hook mutation.
"""
import ast
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import inspect
import io
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import textwrap
import unittest
from unittest.mock import patch
import urllib.request

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc15-policy-empty-requirements/cases.json"
CASE_METHODS = {
    "selected_empty": "test_selected_empty",
    "exceptions_only": "test_exceptions_only",
    "facts_not_rules": "test_facts_not_rules",
    "mixed_families": "test_mixed_families",
    "nonempty_positive": "test_nonempty_positive",
    "replay_preservation": "test_replay_preservation",
}


def deny_effect(*args, **kwargs):
    raise RuntimeError("fc15_offline_effect_forbidden")


@contextmanager
def offline():
    # No target is contacted or opened as a negative control.
    with ExitStack() as stack:
        for obj, name in ((socket, "socket"), (socket, "create_connection"),
                          (socket, "getaddrinfo"), (sqlite3, "connect"),
                          (subprocess, "Popen"), (urllib.request, "urlopen")):
            stack.enter_context(patch.object(obj, name, deny_effect))
        yield


class EmptyRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.effects = ExitStack()
        self.addCleanup(self.effects.close)
        self.effects.enter_context(offline())
        # The inspected import closure is stdlib-only and has no effectful
        # bound filesystem defaults; all producer data below stays in memory.
        from campaign_tool.records.understanding import evidence, policy, reconcile
        from campaign_tool.records import grounded_evidence
        self.evidence, self.policy, self.reconcile = evidence, policy, reconcile
        self.grounded = grounded_evidence
        self.fixture = json.loads(FIXTURE.read_text())
        self.scope = self.fixture["scope"]
        self.assertEqual(set(self.fixture["cases"]), set(CASE_METHODS))
        self.original = hashlib.sha256(b"fictional FC15 original, not a record").hexdigest()
        text = "\n".join(self.fixture[k] for k in
                         ("requirement_text", "exception_text", "term_text"))
        receipt = {
            "schema": "format-extraction-v1", "original_sha256": self.original,
            "original_changed": False, "status": "complete", "parser": "synthetic",
            "parser_version": "0", "route": {"format": "pdf"},
            "pages": [{"page_no": 1, "status": "ok"}, {"page_no": 2, "status": "ok"}],
            "units": [
                {"kind": "text", "locator": {"page": 1}, "text": text, "data": {}},
                {"kind": "text", "locator": {"page": 2},
                 "text": self.fixture["fact_text"], "data": {}},
            ],
        }
        self.bundle = evidence.normalize_receipt(receipt, self.original,
                                                 evidence.sha(evidence.canonical(receipt)))
        self.rule_cite = evidence.cite_text(self.bundle["units"][0],
                                           self.fixture["requirement_text"])
        self.fact_cite = grounded_evidence.pointer_support(self.bundle["units"][1],
                                                           "/retention_days")

    def version(self, *, omitted=False, positive=False):
        v = deepcopy(self.fixture["version"])
        v["source"]["content_sha256"] = self.original
        if not omitted:
            v["requirements"] = []
        if positive:
            v.update(version_id="full-v1", family_id="full-family")
            v["requirements"] = [{
                "requirement_id": "fictional-retention", "text": self.fixture["requirement_text"],
                "citations": [deepcopy(self.rule_cite)],
                "test": {"field": "retention_days", "operator": "equals", "expected": 30},
            }]
        return v

    def facts(self):
        return [{"fact_id": "fictional-fact-1", "field": "retention_days", "value": 30,
                 "agency_id": self.scope["agency_id"], "citations": [deepcopy(self.fact_cite)]}]

    def compare(self, versions, facts):
        before = deepcopy((versions, facts, self.bundle, self.scope))
        out = self.reconcile.compare(facts, versions, bundle=self.bundle, **self.scope)
        self.assertEqual((versions, facts, self.bundle, self.scope), before)
        self.assertEqual(out["status"], "derived_findings_needing_review")
        self.assertEqual(out["gaps"], [])
        self.assertIs(out["review_acceptance"], False)
        self.assertEqual(out["legal_conclusions"], 0)
        for finding in out["findings"]:
            self.assertEqual(finding["legal_assessment"], "NOT_ASSESSED")
            self.assertIs(finding["review_acceptance"], False)
        return out

    def require_gap(self, v):
        before = deepcopy(v)
        checked = self.policy.validate_versions([v])
        self.assertEqual(checked["issues"], [])
        self.assertEqual(checked["gaps"], [])
        out = self.policy.requirements_for([v], **self.scope)
        self.assertEqual(v, before)
        self.assertEqual(out["status"], "selected")
        self.assertEqual(out["selection"]["status"], "selected")
        self.assertEqual(out["selection"]["version"], v)
        self.assertEqual(out["requirements"], [])
        self.assertIs(out["legal_conclusion"], False)
        self.assertEqual(out["gaps"], [{"code": "requirements_not_transcribed",
                                         "version_id": v["version_id"]}],
                         "requirements_for_exact_gap")
        return out

    def require_empty_finding(self, findings):
        self.assertEqual(findings, [{
            "family_id": "empty-family", "requirement_id": None,
            "classification": "missing_evidence", "reason": "requirements_not_transcribed",
            "rule_selection": "selected", "citations": [],
            "legal_assessment": "NOT_ASSESSED", "review_acceptance": False,
        }], "selected_empty_exact_finding")

    def require_positive(self, out):
        self.assertEqual(len(out["findings"]), 1)
        f = out["findings"][0]
        self.assertEqual((f["family_id"], f["version_id"], f["requirement_id"],
                          f["classification"], f["reason"], f["rule_selection"]),
                         ("full-family", "full-v1", "fictional-retention",
                          "no_conflict_observed", "only_supplied_fields_no_global_compliance_claim",
                          "selected"))
        self.assertEqual(f["fact_ids"], ["fictional-fact-1"])
        self.assertEqual(f["citations"], [self.fact_cite])
        self.assertEqual(f["rule_source"], self.version(positive=True)["source"])

    def test_selected_empty(self):
        for omitted, case in zip((True, False), self.fixture["cases"]["selected_empty"]):
            with self.subTest(case=case):
                v = self.version(omitted=omitted)
                self.require_gap(v)
                self.require_empty_finding(self.compare([v], [])["findings"])

    def test_exceptions_only(self):
        for case in self.fixture["cases"]["exceptions_only"]:
            with self.subTest(case=case):
                v = self.version()
                for field, key in (("exceptions", "exception"), ("incorporated_terms", "term")):
                    if case in (key, "both"):
                        quote = self.fixture[key + "_text"]
                        v[field] = [{"text": quote, "citations": [
                            self.evidence.cite_text(self.bundle["units"][0], quote)]}]
                selected = self.require_gap(v)
                for field in ("exceptions", "incorporated_terms"):
                    self.assertEqual(selected[field], [dict(item, version_id=v["version_id"],
                        source_pointer=v["source"]) for item in v.get(field, [])])
                self.require_empty_finding(self.compare([v], self.facts())["findings"])

    def test_facts_not_rules(self):
        for omitted, case in zip((True, False), self.fixture["cases"]["facts_not_rules"]):
            with self.subTest(case=case):
                v = self.version(omitted=omitted)
                facts = self.facts()
                extra = self.grounded.pointer_support(self.bundle["units"][1], "/other_observation")
                facts.append({"fact_id": "fictional-fact-2", "field": "other_observation",
                              "value": "present", "citations": [extra]})
                for fact in facts:
                    for cite in fact["citations"]:
                        self.grounded.validate_support(cite, self.bundle)
                self.require_gap(v)
                self.require_empty_finding(self.compare([v], facts)["findings"])

    def test_mixed_families(self):
        with self.subTest(case="empty-and-transcribed"):
            empty, full = self.version(), self.version(positive=True)
            self.require_gap(empty)
            selected = self.policy.requirements_for([full], **self.scope)
            self.assertEqual(selected["gaps"], [])
            self.assertEqual(len(selected["requirements"]), 1)
            out = self.compare([full, empty], self.facts())
            self.require_empty_finding([f for f in out["findings"] if f["family_id"] == "empty-family"])
            positive = dict(out, findings=[f for f in out["findings"] if f["family_id"] == "full-family"])
            self.require_positive(positive)
            self.assertEqual(len(out["findings"]), 2)

    def test_nonempty_positive(self):
        with self.subTest(case="complete-cited-positive"):
            v = self.version(positive=True)
            self.assertTrue(self.policy.validate_versions([v])["valid"])
            selected = self.policy.requirements_for([v], **self.scope)
            self.assertEqual(selected["gaps"], [])
            self.assertIs(selected["legal_conclusion"], False)
            self.require_positive(self.compare([v], self.facts()))

    def test_replay_preservation(self):
        empty, full = self.version(), self.version(positive=True)
        facts = self.facts()
        facts.append(dict(deepcopy(facts[0]), fact_id="fictional-peer-fact"))
        with self.subTest(case="permuted-inputs"):
            first = self.compare([empty, full], facts)
            replay = self.compare([full, empty], list(reversed(facts)))
            # Compare whole semantic findings, including stable IDs and citations.
            self.assertEqual(first, replay)
            self.require_gap(empty)
        with self.subTest(case="source-hold"):
            held = deepcopy(full)
            held["source"]["verification_status"] = "unverified"
            out = self.compare([empty, held], self.facts())
            self.require_empty_finding(out["findings"][:1])
            f = out["findings"][1]
            self.assertEqual((f["classification"], f["reason"]),
                             ("applicability_unresolved", "rule_source_or_requirement_span_unbound"))
        with self.subTest(case="applicability-hold"):
            held = deepcopy(full)
            held["effective_from"] = "2030-07-01"
            out = self.compare([empty, held], self.facts())
            self.require_empty_finding(out["findings"][:1])
            f = out["findings"][1]
            self.assertEqual((f["classification"], f["reason"]),
                             ("applicability_unresolved", "not_in_force"))

    def isolated_without_branch(self, function, condition):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        hits = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                and ast.unparse(n.test) == condition]
        self.assertEqual(len(hits), 1, "isolated_mutant_branch_identity")
        target = hits[0]
        class RemoveOnlyTarget(ast.NodeTransformer):
            def visit_If(self, node):
                return None if node is target else self.generic_visit(node)
        mutated = ast.fix_missing_locations(RemoveOnlyTarget().visit(tree))
        namespace = dict(function.__globals__)
        exec(compile(mutated, "<fc15-isolated-synthetic-mutant>", "exec"), namespace)
        return namespace[function.__name__]

    def run_control(self, obj, name, replacement, message):
        output = io.StringIO()
        negative = EmptyRequirementsTests("test_selected_empty")
        with patch.object(obj, name, replacement):
            result = unittest.TextTestRunner(stream=output, verbosity=2).run(negative)
            positive = unittest.TextTestRunner(stream=output, verbosity=2).run(
                EmptyRequirementsTests("test_nonempty_positive"))
        print("CONTROL", name, output.getvalue())
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.skipped, [])
        self.assertEqual(len(result.failures), 2)  # omitted and explicit subcases
        for test, trace in result.failures:
            self.assertIn("test_selected_empty", test.id())
            self.assertIn(message, trace)
            self.assertIn("AssertionError", trace)
        self.assertTrue(positive.wasSuccessful(), output.getvalue())
        self.assertEqual(positive.testsRun, 1)
        self.assertEqual(positive.skipped, [])
        restored = unittest.TextTestRunner(stream=output, verbosity=2).run(
            EmptyRequirementsTests("test_selected_empty"))
        self.assertTrue(restored.wasSuccessful(), output.getvalue())
        print("RESTORED", name, output.getvalue())

    def test_deciding_compare_control(self):
        mutant = self.isolated_without_branch(self.reconcile.compare, "not requirements")
        self.run_control(self.reconcile, "compare", mutant, "selected_empty_exact_finding")

    def test_independent_gap_control(self):
        mutant = self.isolated_without_branch(self.policy.requirements_for,
                                              "not result['requirements']")
        self.run_control(self.policy, "requirements_for", mutant, "requirements_for_exact_gap")


if __name__ == "__main__":
    # Redirects are process-local, only in explicit script mode, before product imports.
    import argparse
    import os
    import sys
    import tempfile
    parser = argparse.ArgumentParser()
    parser.add_argument("--temporary-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not args.temporary_root.is_absolute() or not args.temporary_root.is_dir():
        raise SystemExit("temporary-root must be an existing absolute directory")
    tempfile.tempdir = str(args.temporary_root)
    sys.path.insert(0, str(REPO))
    class ScriptResult(unittest.TextTestResult):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.ids, self.subcases = [], []
        def startTest(self, test):
            self.ids.append(test.id())
            super().startTest(test)
        def addSubTest(self, test, subtest, err):
            self.subcases.append({"test_id": test.id(), "id": subtest.id(),
                                  "params": dict(subtest.params), "success": err is None})
            super().addSubTest(test, subtest, err)
    before = (dict(os.environ), tempfile.tempdir, socket.socket, sqlite3.connect, subprocess.Popen)
    result = unittest.TextTestRunner(verbosity=2, resultclass=ScriptResult).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(EmptyRequirementsTests))
    data = {"test_ids": result.ids, "subcases": result.subcases,
            "tests_run": result.testsRun, "skips": result.skipped,
            "failures": [(t.id(), trace) for t, trace in result.failures],
            "errors": [(t.id(), trace) for t, trace in result.errors],
            "success": result.wasSuccessful() and not result.skipped,
            "peer_state_unchanged": (dict(os.environ), tempfile.tempdir, socket.socket,
                                     sqlite3.connect, subprocess.Popen) == before}
    args.report.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    raise SystemExit(0 if data["success"] else 1)
