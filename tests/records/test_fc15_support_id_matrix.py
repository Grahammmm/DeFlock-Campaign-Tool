"""Independently Sol-authored fictional FC15 identity matrix; no live inputs.

Import/discovery is inert. Product imports occur only inside scoped offline
mocks. Script mode redirects temporary defaults before importing product code.
"""
import argparse
import ast
from contextlib import ExitStack, contextmanager
import copy
import hashlib
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc15-support-id-matrix/cases.json"
REPRESENTATIONS = ("text_span", "text_pointer", "retained_data_pointer")
IDENTITIES = ("unit_id", "original_sha256", "import_id", "artifact_sha256", "text_sha256")
COMPATIBILITY = (
    "tests.records.test_understanding.GroundedEvidenceTests",
    "tests.records.test_understanding.EvidenceTests.test_quote_citation_and_claim_round_trip",
)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def denied(*args, **kwargs):
    raise RuntimeError("FC15 offline sentinel: effectful interface prohibited")


@contextmanager
def offline():
    # Scoped mocks, never permanent audit hooks or discovery-time patches.
    with ExitStack() as stack:
        for target in ("socket.socket", "socket.create_connection", "socket.getaddrinfo",
                       "sqlite3.connect", "subprocess.Popen", "os.system"):
            stack.enter_context(patch(target, side_effect=denied))
        yield


class SupportIdentityMatrix(unittest.TestCase):
    def setUp(self):
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        self.guards.enter_context(offline())
        self.ge = importlib.import_module("campaign_tool.records.grounded_evidence")
        self.ev = importlib.import_module("campaign_tool.records.understanding.evidence")
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(self.fixture["representations"], list(REPRESENTATIONS))
        self.assertEqual(self.fixture["identities"], list(IDENTITIES))
        self.assertIs(self.fixture["fictional"], True)

    def material(self, representation):
        f = self.fixture
        raw = {"kind": "text", "locator": {"page": 4}, "text": f["text"], "data": {}}
        if representation == "text_pointer":
            raw["text"] = f["json_text"]
        elif representation == "retained_data_pointer":
            raw["kind"] = "structured"
            raw["data"] = copy.deepcopy(f["retained_data"])
        target = self.ev.unit(raw, digest("fictional original"), digest("fictional receipt"), 1)
        peers = [self.ev.unit({"kind": "text", "locator": {"page": n},
                               "text": "Unrelated fictional peer " + str(n), "data": {}},
                              target["original_sha256"], target["receipt_sha256"], n)
                 for n in (2, 3)]
        units = [target, *peers]
        bundle = self.ev.bundle(units, {target["original_sha256"]: {"fictional": True}},
                                [{"original_sha256": target["original_sha256"],
                                  "receipt_sha256": target["receipt_sha256"],
                                  "declared_unit_count": 3, "normalized_unit_count": 3}])
        if representation == "text_span":
            start = raw["text"].encode().index(f["span_quote"].encode())
            support = self.ge.span_support(target, start, start + len(f["span_quote"].encode()))
        elif representation == "text_pointer":
            support = self.ge.pointer_support(target, f["pointer"])
        else:
            support = self.ge.data_pointer_support(target, f["pointer"])
        # Oracle is declared fictional text, not a saved copy of product output.
        self.assertEqual(support["quote"], f["span_quote"] if representation == "text_span" else f["pointer_quote"])
        self.ev.validate_bundle(bundle)
        return bundle, target, peers, support

    def positive(self, support, bundle, representation):
        before = copy.deepcopy((support, bundle))
        expected = {"structural_support": "verified_exact_retained_data_projection"
                    if representation == "retained_data_pointer" else "verified_exact_retained_bytes",
                    "semantic_assessment": "NOT_ASSESSED", "review_acceptance": False,
                    "snapshot_sha256": bundle["snapshot_sha256"]}
        result = self.ge.validate_support(support, bundle)
        self.assertEqual(result, expected)
        self.assertIs(result["review_acceptance"], False)
        self.assertEqual(self.ge.validate_support(support, bundle), expected)
        self.assertEqual((support, bundle), before)

    def rejection(self, function, support, bundle, code, label):
        before = copy.deepcopy((support, bundle))
        for _ in range(2):
            try:
                function(support, bundle)
            except ValueError as error:
                self.assertEqual(str(error), code, label)
            else:
                self.fail(label + ": expected precise ValueError " + code)
        self.assertEqual((support, bundle), before, label + ": inputs changed")

    def forged(self, support, key):
        changed = copy.deepcopy(support)
        replacement = digest("fictional replacement for " + key)
        self.assertNotEqual(replacement, support[key])
        changed[key] = replacement
        self.assertEqual([k for k in support if support[k] != changed[k]], [key])
        return changed

    def test_valid_representations(self):
        for rep in REPRESENTATIONS:
            with self.subTest(rep=rep):
                bundle, _, _, support = self.material(rep)
                self.positive(support, bundle, rep)
                self.assertEqual(self.ev.validate_citation(support, bundle),
                                 self.ge.validate_support(support, bundle))
                claim = self.ev.claim(bundle, self.fixture["claim"], [support])
                self.assertEqual(claim["supports"], [support])
                self.assertEqual(claim["snapshot_sha256"], bundle["snapshot_sha256"])
                self.assertEqual(claim["semantic_assessment"], "NOT_ASSESSED")
                self.assertIs(claim["review_acceptance"], False)
                self.ev.validate_claim(claim, bundle)

    def test_five_bindings(self):
        for rep in REPRESENTATIONS:
            for key in IDENTITIES:
                with self.subTest(rep=rep, key=key):
                    bundle, _, _, support = self.material(rep)
                    forged = self.forged(support, key)
                    code = "evidence_unit_not_current" if key == "unit_id" else "evidence_binding_mismatch"
                    self.rejection(self.ge.validate_support, forged, bundle, code, "IDENTITY_REASON")
                    self.rejection(self.ev.validate_citation, forged, bundle, code, "WRAPPER_IDENTITY_REASON")
                    self.positive(support, bundle, rep)

    def test_locator_and_membership(self):
        for rep in REPRESENTATIONS:
            for change in ("locator", "missing", "duplicate"):
                with self.subTest(rep=rep, change=change):
                    bundle, target, peers, support = self.material(rep)
                    altered = copy.deepcopy(bundle)
                    cite = copy.deepcopy(support)
                    if change == "locator":
                        cite["locator"]["page"] += 1
                        self.assertIs(type(cite["locator"]["page"]), int)
                        code = "evidence_binding_mismatch"
                    elif change == "missing":
                        altered["units"] = copy.deepcopy(peers)
                        code = "evidence_unit_not_current"
                    else:
                        altered["units"].append(copy.deepcopy(target))
                        code = "evidence_unit_not_current"
                    self.assertEqual(cite["quote"], support["quote"])
                    # Grounded API consumes current-reader shape directly; deliberately
                    # do not send duplicate membership through bundle's earlier gate.
                    self.rejection(self.ge.validate_support, cite, altered, code, "MEMBERSHIP_REASON")
                    self.positive(support, bundle, rep)

    def test_peer_order(self):
        for rep in REPRESENTATIONS:
            with self.subTest(rep=rep):
                bundle, target, peers, support = self.material(rep)
                before = copy.deepcopy(bundle)
                for order in ([peers[0], target, peers[1]], [peers[1], peers[0], target],
                              [target, peers[1], peers[0]]):
                    reordered = dict(bundle, units=copy.deepcopy(order))
                    self.positive(support, reordered, rep)
                for order in (peers, list(reversed(peers))):
                    self.rejection(self.ge.validate_support, support, dict(bundle, units=order),
                                   "evidence_unit_not_current", "PEERS_CANNOT_REPLACE_TARGET")
                self.assertEqual(bundle, before)

    def test_no_partial_claim(self):
        for rep in REPRESENTATIONS:
            for key in IDENTITIES:
                for order in ("valid_first", "forged_first"):
                    with self.subTest(rep=rep, key=key, order=order):
                        bundle, _, peers, support = self.material(rep)
                        peer = self.ge.span_support(peers[0], 0, 9)
                        forged = self.forged(support, key)
                        code = "evidence_unit_not_current" if key == "unit_id" else "evidence_binding_mismatch"
                        for valid in ([support], [peer], [peer, support]):
                            citations = [*valid, forged] if order == "valid_first" else [forged, *valid]
                            before = copy.deepcopy((bundle, citations))
                            for _ in range(2):
                                with self.assertRaisesRegex(ValueError, "^" + code + "$"):
                                    self.ge.derived_claim(bundle, digest("fictional context"), self.fixture["claim"], citations)
                                with self.assertRaisesRegex(ValueError, "^" + code + "$"):
                                    self.ev.claim(bundle, self.fixture["claim"], citations)
                            self.assertEqual((bundle, citations), before)
                        good = self.ge.derived_claim(bundle, digest("fictional context"), self.fixture["claim"], [support, peer])
                        self.assertEqual(good["semantic_assessment"], "NOT_ASSESSED")
                        self.assertIs(good["review_acceptance"], False)
                        self.positive(support, bundle, rep)

    def mutant(self, kind):
        tree = ast.parse(textwrap.dedent(inspect.getsource(self.ge.validate_support)))
        count = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "_need":
                continue
            if len(node.args) != 2 or not isinstance(node.args[1], ast.Constant):
                continue
            code = node.args[1].value
            if kind == "identity" and code == "evidence_binding_mismatch":
                predicate = node.args[0]
                self.assertIsInstance(predicate, ast.BoolOp)
                self.assertIsInstance(predicate.values[0], ast.Call)
                self.assertEqual(predicate.values[0].func.id, "all")
                # Retain locator equality and every other statement.
                predicate.values[0] = ast.Constant(value=True)
                count += 1
            elif kind == "membership" and code == "evidence_unit_not_current":
                node.args[0] = ast.Constant(value=True)
                count += 1
        self.assertEqual(count, 1)
        namespace = dict(self.ge.__dict__)
        exec(compile(ast.fix_missing_locations(tree), "<fc15-isolated-" + kind + ">", "exec"), namespace)
        return namespace["validate_support"]

    def test_control_identity_comparison(self):
        for rep in REPRESENTATIONS:
            with self.subTest(rep=rep):
                bundle, _, _, support = self.material(rep)
                mutant = self.mutant("identity")
                forged = self.forged(support, "original_sha256")
                # Later exact-support equality still rejects. The identity-specific
                # assertion, not just generic rejection, must detect the removed gate.
                with self.assertRaisesRegex(self.failureException, "IDENTITY_REASON"):
                    self.rejection(mutant, forged, bundle, "evidence_binding_mismatch", "IDENTITY_REASON")
                mutant(support, bundle)
                wrong_quote = dict(support, quote=support["quote"] + " forged")
                self.rejection(mutant, wrong_quote, bundle, "evidence_token_or_quote_mismatch", "QUOTE_GATE_RETAINED")
                self.rejection(self.ge.validate_support, forged, bundle, "evidence_binding_mismatch", "IDENTITY_REASON")
                self.positive(support, bundle, rep)

    def test_control_unique_membership(self):
        for rep in REPRESENTATIONS:
            with self.subTest(rep=rep):
                bundle, target, _, support = self.material(rep)
                mutant = self.mutant("membership")
                duplicated = copy.deepcopy(bundle)
                duplicated["units"].append(copy.deepcopy(target))
                with self.assertRaisesRegex(self.failureException, "UNIQUE_MEMBERSHIP"):
                    self.rejection(mutant, support, duplicated, "evidence_unit_not_current", "UNIQUE_MEMBERSHIP")
                mutant(support, bundle)
                wrong_quote = dict(support, quote=support["quote"] + " forged")
                self.rejection(mutant, wrong_quote, bundle, "evidence_token_or_quote_mismatch", "QUOTE_GATE_RETAINED")
                self.rejection(self.ge.validate_support, support, duplicated, "evidence_unit_not_current", "UNIQUE_MEMBERSHIP")
                self.positive(support, bundle, rep)


def expected_subcases():
    simple = ("valid_representations", "peer_order", "control_identity_comparison", "control_unique_membership")
    rows = [("test_" + name, {"rep": rep}) for name in simple for rep in REPRESENTATIONS]
    rows += [("test_five_bindings", {"rep": rep, "key": key}) for rep in REPRESENTATIONS for key in IDENTITIES]
    rows += [("test_locator_and_membership", {"rep": rep, "change": change})
             for rep in REPRESENTATIONS for change in ("locator", "missing", "duplicate")]
    rows += [("test_no_partial_claim", {"rep": rep, "key": key, "order": order})
             for rep in REPRESENTATIONS for key in IDENTITIES for order in ("valid_first", "forged_first")]
    return rows


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_ids = []
        self.subcases = []

    def startTest(self, test):
        self.test_ids.append(test.id())
        super().startTest(test)

    def addSubTest(self, test, subtest, error):
        self.subcases.append({"test_id": test.id(), "method": test._testMethodName,
                              "params": dict(subtest.params), "success": error is None})
        super().addSubTest(test, subtest, error)


def ambient():
    return (dict(os.environ), tempfile.tempdir, socket.socket, socket.create_connection,
            socket.getaddrinfo, sqlite3.connect, subprocess.Popen, os.system)


def script_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("matrix", "compatibility", "discovery"), default="matrix")
    parser.add_argument("--temp-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = args.temp_root.absolute()
    if root == REPO or REPO in root.parents or not root.is_dir():
        parser.error("temporary root must exist outside repository")
    if any(p.is_symlink() for p in (root, *root.parents)):
        parser.error("temporary root symlink")
    # Only this dedicated process changes temporary/environment defaults.
    for key in ("TMPDIR", "TEMP", "TMP"):
        os.environ[key] = str(root)
    tempfile.tempdir = str(root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    baseline = ambient()
    loader = unittest.TestLoader()
    if args.mode == "compatibility":
        with offline():
            suite = loader.loadTestsFromNames(COMPATIBILITY)
        expected_tests = 2
    elif args.mode == "discovery":
        class SentinelBefore(unittest.TestCase):
            def runTest(self):
                self.assertEqual(ambient(), baseline)
        class SentinelAfter(unittest.TestCase):
            def runTest(self):
                self.assertEqual(ambient(), baseline)
        # Actually use unittest's file discovery; only this new file is selected.
        discovered = loader.discover(str(Path(__file__).parent), pattern=Path(__file__).name,
                                     top_level_dir=str(REPO))
        if ambient() != baseline:
            raise RuntimeError("discovery changed ambient state")
        suite = unittest.TestSuite([SentinelBefore(), discovered, SentinelAfter()])
        expected_tests = 9
    else:
        suite = loader.loadTestsFromTestCase(SupportIdentityMatrix)
        expected_tests = 7
    with offline() if args.mode == "compatibility" else ExitStack():
        result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    check = unittest.TestCase()
    check.assertEqual(loader.errors, [])
    check.assertEqual(ambient(), baseline)
    check.assertEqual(result.testsRun, expected_tests)
    check.assertEqual(result.skipped, [])
    check.assertEqual(result.expectedFailures, [])
    check.assertEqual(result.unexpectedSuccesses, [])
    if args.mode != "compatibility":
        actual = sorted(json.dumps([r["method"], r["params"]], sort_keys=True) for r in result.subcases)
        expected = sorted(json.dumps([name, params], sort_keys=True) for name, params in expected_subcases())
        check.assertEqual(actual, expected, "complete frozen case/subcase inventory")
        check.assertEqual(len(set(result.test_ids)), expected_tests)
    report = {"mode": args.mode, "test_ids": result.test_ids, "subcases": result.subcases,
              "tests_run": result.testsRun, "skips": len(result.skipped),
              "failures": len(result.failures), "errors": len(result.errors),
              "ambient_unchanged": ambient() == baseline, "success": result.wasSuccessful(),
              "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest()}
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(script_main())
