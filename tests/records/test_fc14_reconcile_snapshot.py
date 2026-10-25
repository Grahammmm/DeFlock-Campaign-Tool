"""Independently authored, fictional FC14 return-boundary experiments.

Import is inert under ordinary unittest discovery. Script setup alone redirects
storage and installs scoped offline sentinels before importing product code.
"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc14-reconcile-snapshot/cases.json"
PRODUCT = "campaign_tool/records/understanding/reconcile.py"
OBSERVATIONS = []


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def read_fixture():
    return json.loads(FIXTURE.read_text())


def modules():
    from campaign_tool.records.understanding import evidence, reconcile
    return evidence, reconcile


def baseline_module():
    fixture = read_fixture()
    done = subprocess.run(["git", "show", "".join(fixture["baseline_head_parts"]) + ":" + PRODUCT],
                          cwd=REPO, capture_output=True, check=True, timeout=30)
    expected = "".join(fixture["baseline_source_sha256_parts"])
    if hashlib.sha256(done.stdout).hexdigest() != expected:
        raise ValueError("baseline_source_pin_mismatch")
    mod = types.ModuleType("campaign_tool.records.understanding.fc14_baseline")
    mod.__package__ = "campaign_tool.records.understanding"
    exec(compile(done.stdout, "<git-verified-fc14-baseline>", "exec"), mod.__dict__)
    return mod


def inputs(branch=None, pointer="default", reverse=False):
    e, _ = modules()
    f = read_fixture()
    branch = branch or f["branches"][0]
    original = hashlib.sha256(b"fictional FC14 original, not a record").hexdigest()
    receipt_hash = hashlib.sha256(b"fictional FC14 receipt, not a receipt file").hexdigest()
    units = [e.unit({"kind": "text", "locator": copy.deepcopy(f["locator"]),
                     "text": text, "data": {}}, original, receipt_hash, ordinal)
             for ordinal, text in enumerate((f["rule_text"], f["fact_text"]), 1)]
    bundle = e.bundle(units, {original: {"review_acceptance": False, "synthetic": {"tags": ["only"]}}},
                      [{"original_sha256": original, "receipt_sha256": receipt_hash,
                        "declared_unit_count": 2, "normalized_unit_count": 2}])
    rule_cite = e.cite_text(units[0], f["rule_text"])
    cites = [e.cite_text(units[1], quote) for quote in ("retention is 12 days", "Alternate evidence: 12 days")]
    req = {"requirement_id": "synthetic-rule", "text": f["rule_text"], "citations": [rule_cite],
           "test": {"field": "days", "operator": branch.get("operator", "at_most"), "expected": 20}}
    if branch.get("unmapped"):
        req.pop("test")
    source = {"source_id": "fictional-policy", "url": "https://example.invalid/fc14",
              "locator": "fictional section 1", "content_sha256": original,
              "verification_status": branch.get("verification_status", "verified")}
    if pointer == "explicit":
        req["source_pointer"] = {**source, "details": {"sections": ["one", {"page": 1}]}}
    version = {"version_id": "synthetic-v1", "family_id": "synthetic-retention",
               "jurisdiction": f["scope"]["jurisdiction"], "agency_id": "*",
               "regulated_roles": ["operator"], "effective_from": "2029-01-01",
               "issued_at": "2028-12-01", "retrieved_at": "2031-01-01",
               "supersession_status": "not_superseded_as_of_retrieval", "source": source,
               "requirements": [req], "exceptions": [], "incorporated_terms": []}
    facts = [{"fact_id": fact_id, "field": "days", "value": branch["value"],
              "citations": list(reversed(cites)) if reverse else list(cites),
              "counterevidence": branch.get("counterevidence", False),
              "exception_applies": branch.get("exception_applies", False),
              "metadata": {"labels": ["fictional"]}}
             for fact_id in (["z", "a"] if reverse else ["a", "z"])]
    scope = dict(f["scope"], event_at=branch.get("event_at", f["scope"]["event_at"]))
    return {"facts": facts, "versions": [version], "bundle": bundle, **scope}


class SnapshotTests(unittest.TestCase):
    def compare(self, data):
        return modules()[1].compare(**data)

    def snapshot_assertion(self):
        data = inputs()
        result = self.compare(data)
        self.assertEqual(result["findings"][0]["classification"], "no_conflict_observed")
        before = canonical(result)
        data["facts"][0]["citations"][0]["quote"] = "caller edit"
        data["facts"][0]["citations"][0]["locator"]["nested"]["labels"].append("caller edit")
        self.assertEqual(before, canonical(result), "FC14_INPUT_SNAPSHOT_CHANGED")

    def test_citation_input_to_output(self):
        self.snapshot_assertion()

    def test_citation_output_to_input(self):
        data = inputs()
        before = canonical(data)
        result = self.compare(data)
        cite = result["findings"][0]["citations"][0]
        cite["quote"] = "result edit"
        cite["span"]["start_byte"] = 99
        cite["locator"]["nested"]["coordinates"].append(99)
        result["findings"].append({"synthetic": True})
        self.assertEqual(before, canonical(data), "FC14_CALLER_INPUT_CHANGED")

    def test_explicit_rule_pointer(self):
        for direction in ("input_to_output", "output_to_input"):
            with self.subTest(direction=direction):
                data = inputs(pointer="explicit")
                result = self.compare(data)
                self.assertEqual(result["findings"][0]["rule_selection"], "selected")
                self.assertEqual(result["findings"][0]["classification"], "no_conflict_observed")
                before = canonical(result if direction == "input_to_output" else data)
                pointer = (data["versions"][0]["requirements"][0]["source_pointer"]
                           if direction == "input_to_output" else result["findings"][0]["rule_source"])
                pointer["details"]["sections"][1]["page"] = 99
                pointer["details"]["sections"].append("edited")
                self.assertEqual(before, canonical(result if direction == "input_to_output" else data),
                                 "FC14_EXPLICIT_POINTER_CHANGED")

    def test_default_pointer_control(self):
        data = inputs()
        result = self.compare(data)
        pointer = result["findings"][0]["rule_source"]
        self.assertEqual(pointer, data["versions"][0]["source"])
        self.assertIsNot(pointer, data["versions"][0]["source"])
        before = canonical(result)
        data["versions"][0]["source"]["locator"] = "caller edit"
        self.assertEqual(before, canonical(result))
        original_inputs = canonical(data)
        pointer["locator"] = "output edit"
        self.assertEqual(original_inputs, canonical(data))

    def test_independent_calls(self):
        for pointer in read_fixture()["pointer_modes"]:
            with self.subTest(pointer=pointer):
                data = inputs(pointer=pointer)
                first, second = self.compare(data), self.compare(data)
                self.assertEqual(first, second)
                before = canonical(second)
                first["findings"][0]["citations"][0]["quote"] = "first edit"
                first["findings"][0]["citations"][0]["locator"]["nested"]["labels"].append("first")
                if pointer == "explicit":
                    first["findings"][0]["rule_source"]["details"]["sections"].append("first")
                self.assertEqual(before, canonical(second), "FC14_OTHER_CALL_CHANGED")

    def test_classification_branches(self):
        for branch in read_fixture()["branches"]:
            for pointer in read_fixture()["pointer_modes"]:
                with self.subTest(branch=branch["name"], pointer=pointer):
                    data = inputs(branch, pointer)
                    before = canonical(data)
                    result = self.compare(data)
                    self.assertEqual(before, canonical(data))
                    finding = result["findings"][0]
                    self.assertEqual(finding["classification"], branch["classification"])
                    self.assertEqual(finding["reason"], branch["reason"])
                    frozen = canonical(result)
                    data["facts"][0]["citations"][0]["quote"] = "edited"
                    self.assertEqual(frozen, canonical(result), "FC14_BRANCH_SNAPSHOT_CHANGED")

    def test_empty_invalid_versions(self):
        for shape in ("empty", "invalid_date", "no_requirements"):
            with self.subTest(shape=shape):
                data = inputs()
                if shape == "empty":
                    data["versions"] = []
                elif shape == "invalid_date":
                    # Policy validation preserves this legitimate diagnostic payload by reference.
                    data["versions"][0]["issued_at"] = {"nested": ["invalid date"]}
                else:
                    data["versions"][0]["requirements"] = []
                result = self.compare(data)
                if shape == "invalid_date":
                    self.assertEqual(result["status"], "invalid_versions")
                    self.assertEqual(result["gaps"][0]["code"], "invalid_date")
                elif shape == "empty":
                    self.assertEqual(result["findings"], [])
                else:
                    self.assertEqual(result["findings"][0]["reason"], "requirements_not_transcribed")
                before = canonical(result)
                if shape == "invalid_date":
                    data["versions"][0]["issued_at"]["nested"].append("caller edit")
                data["bundle"]["units"][0]["locator"]["nested"]["labels"].append("caller edit")
                self.assertEqual(before, canonical(result), "FC14_EMPTY_INVALID_SNAPSHOT_CHANGED")
                before_inputs = canonical(data)
                if shape == "invalid_date":
                    result["gaps"][0]["value"]["nested"].append("output edit")
                self.assertEqual(before_inputs, canonical(data))

    def test_unresolved_version_source(self):
        for direction in ("input_to_output", "output_to_input"):
            with self.subTest(direction=direction):
                data = inputs(read_fixture()["branches"][5])
                data["versions"][0]["source"]["extra"] = {"pages": [1]}
                result = self.compare(data)
                self.assertEqual(result["findings"][0]["classification"], "applicability_unresolved")
                before = canonical(result if direction == "input_to_output" else data)
                target = (data["versions"][0]["source"] if direction == "input_to_output"
                          else result["findings"][0]["rule_source"])
                target["extra"]["pages"].append(2)
                self.assertEqual(before, canonical(result if direction == "input_to_output" else data))

    def test_mutable_graph_disjoint(self):
        def nodes(value):
            found = set()
            def visit(item):
                if isinstance(item, (dict, list)) and id(item) not in found:
                    found.add(id(item))
                    for child in (item.values() if isinstance(item, dict) else item):
                        visit(child)
            visit(value)
            return found
        for branch in read_fixture()["branches"]:
            for pointer in read_fixture()["pointer_modes"]:
                with self.subTest(branch=branch["name"], pointer=pointer):
                    data = inputs(branch, pointer)
                    first, second = self.compare(data), self.compare(data)
                    self.assertEqual(nodes(data) & nodes(first), set())
                    self.assertEqual(nodes(first) & nodes(second), set())

    def test_identity_preserved(self):
        baseline = baseline_module()
        for branch in read_fixture()["branches"]:
            for pointer in read_fixture()["pointer_modes"]:
                for reverse in (False, True):
                    with self.subTest(branch=branch["name"], pointer=pointer, reverse=reverse):
                        data = inputs(branch, pointer, reverse)
                        expected = baseline.compare(**data)
                        result = self.compare(data)
                        self.assertEqual(canonical(expected), canonical(result))
                        self.assertEqual(result["findings"][0]["finding_identity_version"], "content-bound-v2")
                        self.assertEqual(result["findings"][0]["fact_ids"], [] if branch.get("unmapped") else ["a", "z"])
                        OBSERVATIONS.append({"branch": branch["name"], "pointer": pointer, "reverse": reverse,
                                             "baseline": expected, "candidate": result})
        self.assertEqual(canonical(self.compare(inputs(reverse=True))), canonical(self.compare(inputs())))

    def test_no_semantic_acceptance(self):
        for branch in read_fixture()["branches"]:
            with self.subTest(branch=branch["name"]):
                data = inputs(branch)
                result = self.compare(data)
                data["facts"][0]["citations"][0]["quote"] = "accepted by caller"
                data["bundle"]["source_context"].clear()
                self.assertEqual(result["legal_conclusions"], 0)
                self.assertIs(result["review_acceptance"], False)
                for finding in result["findings"]:
                    self.assertIs(finding["review_acceptance"], False)
                    self.assertEqual(finding["legal_assessment"], "NOT_ASSESSED")


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ids, self.dimensions = [], []

    def startTest(self, test):
        self.ids.append(test.id())
        super().startTest(test)

    def addSubTest(self, test, subtest, err):
        self.dimensions.append({"test": test.id(), "dimension": str(subtest), "passed": err is None})
        super().addSubTest(test, subtest, err)


def script_main():
    import argparse
    import contextlib
    import socket
    import sqlite3
    import urllib.request
    parser = argparse.ArgumentParser()
    parser.add_argument("--temporary-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--mode", choices=("matrix", "negative", "compatibility", "discovery"), default="matrix")
    args = parser.parse_args()
    root = args.temporary_root
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("fresh_absolute_temporary_root_required")
    if not args.report.is_absolute() or args.report.exists():
        raise ValueError("fresh_absolute_report_required")
    os.environ["TMPDIR"] = str(root)
    tempfile.tempdir = str(root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    with contextlib.ExitStack() as stack:
        for obj, name in ((socket, "socket"), (socket, "create_connection"), (socket, "getaddrinfo"),
                          (sqlite3, "connect"), (urllib.request, "urlopen")):
            stack.enter_context(mock.patch.object(obj, name, side_effect=RuntimeError("fc14_offline_sentinel")))
        loader = unittest.TestLoader()
        discovery_checks = None
        if args.mode == "compatibility":
            suite = loader.loadTestsFromName("tests.records.test_understanding.ReconcileTests")
        elif args.mode == "discovery":
            before = (dict(os.environ), tempfile.tempdir, sys.addaudithook, socket.socket, sqlite3.connect,
                      tuple(sys.path))
            class Peer(unittest.TestCase):
                def runTest(self):
                    self.assertEqual(dict(os.environ), before[0])
                    self.assertEqual(tempfile.tempdir, before[1])
                    self.assertIs(sys.addaudithook, before[2])
                    self.assertIs(socket.socket, before[3])
                    self.assertIs(sqlite3.connect, before[4])
                    self.assertEqual(tuple(sys.path), before[5])
                    with tempfile.NamedTemporaryFile() as handle:
                        self.assertEqual(Path(handle.name).parent, root)
            peer_before = Peer()
            peer_after = Peer()
            suite = unittest.TestSuite([peer_before])
            # Execute a genuine unrelated peer before discovery, then compare immediately.
            pre_result = unittest.TextTestRunner(verbosity=2).run(suite)
            if not pre_result.wasSuccessful():
                raise RuntimeError("pre_discovery_peer_failed")
            discovered = loader.discover(str(REPO / "tests/records"), pattern=Path(__file__).name,
                                         top_level_dir=str(REPO))
            suite = unittest.TestSuite([discovered, peer_after])
            discovery_checks = {"before_peer_tests": pre_result.testsRun, "pattern": Path(__file__).name}
        elif args.mode == "negative":
            _, reconciliation = modules()
            original = reconciliation.compare
            def reconnected(*pos, **kw):
                result = original(*pos, **kw)
                result["findings"][0]["citations"][0] = kw["facts"][0]["citations"][0]
                return result
            stack.enter_context(mock.patch.object(reconciliation, "compare", reconnected))
            suite = unittest.TestSuite([SnapshotTests("test_citation_input_to_output")])
        else:
            suite = loader.loadTestsFromTestCase(SnapshotTests)
        result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
        report = {"mode": args.mode, "tests_run": result.testsRun, "test_ids": result.ids,
                  "dimensions": result.dimensions, "skipped": [(t.id(), reason) for t, reason in result.skipped],
                  "failures": [(t.id(), text) for t, text in result.failures],
                  "errors": [(t.id(), text) for t, text in result.errors],
                  "successful": result.wasSuccessful(), "observations": OBSERVATIONS,
                  "discovery_checks": discovery_checks}
        args.report.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(script_main())
