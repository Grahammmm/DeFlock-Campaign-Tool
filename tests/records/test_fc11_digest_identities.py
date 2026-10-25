"""FC11 private-digest identity token grammar experiment; imports are discovery-inert.

Synthetic only: the fixture describes fictional digests. No real records, model,
network, ledger or publication is used. Every case keeps all other required
fields valid so the root hash, conclusion-source hash or jurisdiction token is
the deciding gate. Malformed source checks use the SAME malformed root and source
so the original-binding equality gate cannot mask grammar acceptance.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc11-digest-identities/cases.json"
MODULE = "tests.records.test_fc11_digest_identities"
KILL_MESSAGE = "accepted_malformed_identity_token:"
# The deciding grammar tests plus the valid-token controls; run nested for controls/baselines.
CASE_TESTS = ("test_root_hash_grammar", "test_matching_root_source_pair_grammar",
              "test_source_schema_grammar", "test_jurisdiction_grammar", "test_valid_exact_tokens",
              "test_mismatch_control", "test_ordinary_matching_source", "test_confidence_unchanged")
SOURCE_PATH = ("properties", "conclusions", "items", "properties", "sources", "items")
PATTERN_PATHS = (("properties", "sha256", "pattern"), ("properties", "jurisdiction", "pattern"),
                 SOURCE_PATH + ("properties", "sha256", "pattern"))
STRICT_END = "\\Z"
PERMISSIVE_END = "$"


def expect(condition, reason):
    if not condition:
        raise AssertionError(reason)


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    expect(data["schema"] == "fc11-digest-identities/v1" and data["synthetic_only"] is True,
           "synthetic_fixture_required")
    return data


def product():
    # Delayed until the dedicated runner/child has redirected tempfile.tempdir.
    from campaign_tool.digest import schema
    return schema


def law_validate():
    from campaign_tool.law import validate
    return validate


def mutate(token, mutation):
    op = mutation["op"]
    if op == "suffix":
        return token + mutation["text"]
    if op == "prefix":
        return mutation["text"] + token
    if op == "upper":
        return token.upper()
    if op == "drop_last":
        return token[:-1]
    if op == "replace_last":
        return token[:-1] + mutation["text"]
    expect(op == "literal", "known_mutation_op:" + op)
    return mutation["text"]


def original_sha256(data):
    return hashlib.sha256(data["base"]["original_text"].encode("utf-8")).hexdigest()


def empty(schema, data):
    base = data["base"]
    return schema.empty_digest(original_sha256(data), base["jurisdiction"], base["law_package_version"],
                               base["privacy_tier"], base["detector_version"])


def populated(schema, data, source_sha=None):
    document = empty(schema, data)
    spec = data["populated"]
    for key in ("model_id", "scope", "actors", "dates", "statements"):
        document[key] = copy.deepcopy(spec[key])
    document["conclusions"] = [{"text": c["text"], "confidence": c["confidence"], "sources": [
        {"sha256": source_sha or document["sha256"], "locator": copy.deepcopy(c["locator"]),
         "rule_id": c["rule_id"]}]} for c in spec["conclusions"]]
    return document


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True).encode("ascii")


def hash_cases(data):
    return [(target, m) for target in ("root", "pair", "source") for m in data["hash_mutations"]]


def case_id(target, mutation):
    return target + ":" + mutation["id"]


def node(tree, path):
    for part in path:
        tree = tree[part]
    return tree


class DigestIdentityTests(unittest.TestCase):
    def setUp(self):
        self.coverage = []
        self.data = fixture()
        self.schema = product()
        self.validate = law_validate()
        self.valid_hash = original_sha256(self.data)

    def expect_rejected(self, identifier, call, before_value, where, value):
        before = encoded(before_value)
        try:
            call()
        except ValueError as error:
            self.assertTrue(str(error).startswith(where + ": " + repr(value) + " does not match pattern "),
                            identifier + ": unexpected error " + str(error))
        else:
            self.fail(KILL_MESSAGE + identifier)
        self.assertEqual(encoded(before_value), before, identifier + ": input mutated")
        self.coverage.append("case:" + identifier)

    def malformed_hash(self, mutation):
        value = mutate(self.valid_hash, mutation)
        self.assertNotEqual(value, self.valid_hash)
        return value

    def check_hash_target(self, target):
        where = self.data["hash_targets"][target]["where"]
        for mutation in self.data["hash_mutations"]:
            identifier = case_id(target, mutation)
            with self.subTest(case=identifier):
                value = self.malformed_hash(mutation)
                if target == "root":
                    document = empty(self.schema, self.data)
                    self.schema.validate_digest(copy.deepcopy(document))
                    document["sha256"] = value
                    self.expect_rejected(identifier, lambda: self.schema.validate_digest(document), document,
                                         where, value)
                elif target == "pair":
                    document = populated(self.schema, self.data)
                    self.schema.validate_digest(copy.deepcopy(document))
                    document["sha256"] = value
                    document["conclusions"][0]["sources"][0]["sha256"] = value
                    self.expect_rejected(identifier, lambda: self.schema.validate_digest(document), document,
                                         where, value)
                else:
                    source = copy.deepcopy(populated(self.schema, self.data)["conclusions"][0]["sources"][0])
                    self.validate(copy.deepcopy(source), self.schema.SOURCE)
                    source["sha256"] = value
                    self.expect_rejected(identifier, lambda: self.validate(source, self.schema.SOURCE), source,
                                         where, value)
                self.coverage.append(mutation["dimension"])

    def test_root_hash_grammar(self):
        self.check_hash_target("root")

    def test_matching_root_source_pair_grammar(self):
        self.check_hash_target("pair")

    def test_source_schema_grammar(self):
        # The digest's nested source schema IS the module SOURCE object; validate it directly.
        self.assertIs(node(self.schema.DIGEST_SCHEMA, SOURCE_PATH), self.schema.SOURCE)
        self.check_hash_target("source")

    def test_jurisdiction_grammar(self):
        where = self.data["jurisdiction_target"]["where"]
        valid = self.data["base"]["jurisdiction"]
        for mutation in self.data["jurisdiction_mutations"]:
            identifier = "jurisdiction:" + mutation["id"]
            with self.subTest(case=identifier):
                value = mutate(valid, mutation)
                self.assertNotEqual(value, valid)
                document = empty(self.schema, self.data)
                self.schema.validate_digest(copy.deepcopy(document))
                document["jurisdiction"] = value
                self.expect_rejected(identifier, lambda: self.schema.validate_digest(document), document,
                                     where, value)
                self.coverage.append(mutation["dimension"])

    def test_valid_exact_tokens(self):
        self.assertEqual(len(self.valid_hash), 64)
        self.assertTrue(self.valid_hash.isascii() and self.valid_hash == self.valid_hash.lower())
        document = empty(self.schema, self.data)
        before = encoded(document)
        self.assertIs(self.schema.validate_digest(document), document)
        self.assertEqual(encoded(document), before)
        self.assertEqual(document["jurisdiction"], "us-ca")
        source = populated(self.schema, self.data)["conclusions"][0]["sources"][0]
        self.assertIsNone(self.validate(source, self.schema.SOURCE))
        for token in ("0" * 64, "f" * 64, "0123456789abcdef" * 4):
            other = populated(self.schema, self.data, source_sha=token)
            other["sha256"] = token
            self.assertIs(self.schema.validate_digest(other), other)
        for token in ("aa-zz", "us-ny", "zz-aa"):
            other = empty(self.schema, self.data)
            other["jurisdiction"] = token
            self.assertIs(self.schema.validate_digest(other), other)
        self.coverage.append("valid_exact")

    def test_mismatch_control(self):
        # A valid root plus a DIFFERENT valid source stays rejected by the original-binding gate.
        other = hashlib.sha256(self.data["other_original_text"].encode("utf-8")).hexdigest()
        self.assertNotEqual(other, self.valid_hash)
        document = populated(self.schema, self.data, source_sha=other)
        before = encoded(document)
        with self.assertRaises(ValueError) as caught:
            self.schema.validate_digest(document)
        self.assertEqual(str(caught.exception), self.data["mismatch_control"]["error"])
        self.assertEqual(encoded(document), before)
        self.coverage.append("mismatch_control")

    def test_ordinary_matching_source(self):
        document = populated(self.schema, self.data)
        before = encoded(document)
        returned = self.schema.validate_digest(document)
        self.assertIs(returned, document)
        self.assertEqual(encoded(document), before)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["conclusions"][0]["sources"][0]["sha256"], document["sha256"])
        self.coverage.append("ordinary_matching_source")

    def test_confidence_unchanged(self):
        spec = self.data["confidence"]
        self.assertEqual(self.schema.CONFIDENCE, spec["accepted"])
        for label in spec["accepted"]:
            document = populated(self.schema, self.data)
            document["conclusions"][0]["confidence"] = label
            self.assertIs(self.schema.validate_digest(document), document)
        for label in spec["rejected"]:
            document = populated(self.schema, self.data)
            document["conclusions"][0]["confidence"] = label
            with self.assertRaises(ValueError) as caught:
                self.schema.validate_digest(document)
            self.assertTrue(str(caught.exception).startswith(spec["rejected_error_prefix"]))
        self.coverage.append("confidence_unchanged")

    def test_inputs_and_schema_unchanged(self):
        schema_before = encoded(self.schema.DIGEST_SCHEMA)
        model_before = encoded(self.schema.MODEL_OUTPUT_SCHEMA)
        counts = self.data["expected_case_counts"]
        self.assertEqual(len(hash_cases(self.data)), counts["hash"])
        self.assertEqual(len(self.data["jurisdiction_mutations"]), counts["jurisdiction"])
        # Run every deciding case once more purely for bytes-unchanged accounting.
        for target, mutation in hash_cases(self.data):
            value = mutate(self.valid_hash, mutation)
            if target == "source":
                subject = copy.deepcopy(populated(self.schema, self.data)["conclusions"][0]["sources"][0])
                subject["sha256"] = value
                call = (lambda s: lambda: self.validate(s, self.schema.SOURCE))(subject)
            else:
                subject = empty(self.schema, self.data) if target == "root" else populated(self.schema, self.data)
                subject["sha256"] = value
                if target == "pair":
                    subject["conclusions"][0]["sources"][0]["sha256"] = value
                call = (lambda d: lambda: self.schema.validate_digest(d))(subject)
            before = encoded(subject)
            with self.assertRaises(ValueError):
                call()
            self.assertEqual(encoded(subject), before)
        self.assertEqual(encoded(self.schema.DIGEST_SCHEMA), schema_before)
        self.assertEqual(encoded(self.schema.MODEL_OUTPUT_SCHEMA), model_before)
        self.assertEqual(self.schema.DIGEST_SCHEMA["properties"]["schema_version"], {"const": 1})
        self.assertNotIn("sha256", self.schema.MODEL_OUTPUT_SCHEMA["properties"])
        self.coverage.append("inputs_unchanged")

    def test_anchor_negative_control_and_restore(self):
        declared, declared_source = self.schema.DIGEST_SCHEMA, self.schema.SOURCE
        control = permissive_control(declared)
        with mock.patch.object(self.schema, "DIGEST_SCHEMA", control), \
                mock.patch.object(self.schema, "SOURCE", node(control, SOURCE_PATH)):
            mutant = nested_result()
        killed = sorted(killed_ids(mutant))
        expected = expected_kills(self.data)
        self.assertEqual(mutant.errors, [])
        self.assertEqual(mutant.testsRun, len(CASE_TESTS))
        self.assertEqual(killed, expected)
        self.assertEqual(len(mutant.failures), len(expected))
        self.coverage.append("negative_control_killed")
        self.assertIs(self.schema.DIGEST_SCHEMA, declared)
        self.assertIs(self.schema.SOURCE, declared_source)
        restored = nested_result()
        # Retained per-run observation (row-local tempdir under the runner); never cleaned up.
        save(Path(tempfile.mkdtemp(prefix="fc11-control-")) / "control-observation.json", {
            "label": "INJECTED_CONTROL_INFORMATIONAL",
            "control_patterns": [node(control, p) for p in PATTERN_PATHS],
            "candidate_patterns": [node(declared, p) for p in PATTERN_PATHS],
            "mutant": result_summary(mutant), "restored": result_summary(restored)})
        self.assertEqual((restored.failures, restored.errors, restored.skipped), ([], [], []))
        self.assertEqual(restored.testsRun, len(CASE_TESTS))
        self.coverage.append("negative_control_restored")


def permissive_control(declared):
    """Mechanically derived copy: ONLY the three strict end anchors become permissive ``$``."""
    control = copy.deepcopy(declared)
    for path in PATTERN_PATHS:
        parent = node(control, path[:-1])
        pattern = parent[path[-1]]
        expect(pattern.endswith(STRICT_END) and pattern.count(STRICT_END) == 1, "strict_anchor_expected")
        parent[path[-1]] = pattern[:-len(STRICT_END)] + PERMISSIVE_END
    restored = copy.deepcopy(control)
    for path in PATTERN_PATHS:
        parent = node(restored, path[:-1])
        parent[path[-1]] = parent[path[-1]][:-len(PERMISSIVE_END)] + STRICT_END
    expect(encoded(restored) == encoded(declared), "control_changes_more_than_end_anchors")
    return control


def expected_kills(data):
    kills = [case_id(t, m) for t, m in hash_cases(data) if m["new_gate"]]
    kills += ["jurisdiction:" + m["id"] for m in data["jurisdiction_mutations"] if m["new_gate"]]
    return sorted(kills)


def nested_result():
    suite = unittest.TestSuite(DigestIdentityTests(name) for name in CASE_TESTS)
    result = unittest.TestResult()
    suite.run(result)
    return result


def killed_ids(result):
    killed = []
    for _test, text in result.failures:
        line = [part for part in text.splitlines() if KILL_MESSAGE in part]
        expect(len(line) == 1, "unexpected_control_failure:" + text[-300:])
        killed.append(line[0].split(KILL_MESSAGE, 1)[1].strip())
    return killed


def result_summary(result):
    return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "killed_case_ids": sorted(killed_ids(result)) if not result.errors else None,
            "failure_messages": [text.strip().splitlines()[-1] for _t, text in result.failures],
            "error_messages": [text.strip().splitlines()[-1] for _t, text in result.errors]}


def run_against(schema_module):
    """Run the deciding case tests against an alternative schema module (pinned-original baseline)."""
    with mock.patch.object(sys.modules[__name__], "product", lambda: schema_module):
        return nested_result()


class CoverageResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dimensions = set()
        self.case_ids = set()

    def addSuccess(self, test):
        for entry in getattr(test, "coverage", []):
            (self.case_ids if entry.startswith("case:") else self.dimensions).add(entry.split("case:", 1)[-1])
        super().addSuccess(test)


def run_suite(report_path, discovery=False):
    loader = unittest.TestLoader()
    suite = (loader.discover(str(REPO / "tests/records"), pattern="test_fc11_digest_identities.py",
                             top_level_dir=str(REPO)) if discovery
             else loader.loadTestsFromName(MODULE + ".DigestIdentityTests"))
    with (Path(report_path).parent / "unittest.txt").open("x") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=CoverageResult).run(suite)
    data = fixture()
    required = set(data["required_dimensions"])
    expected_ids = {case_id(t, m) for t, m in hash_cases(data)}
    expected_ids |= {"jurisdiction:" + m["id"] for m in data["jurisdiction_mutations"]}
    summary = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "skipped": len(result.skipped), "dimensions": sorted(result.dimensions),
               "required_dimensions": sorted(required), "missing_dimensions": sorted(required - result.dimensions),
               "case_ids_passed": len(result.case_ids), "case_ids_expected": len(expected_ids),
               "missing_case_ids": sorted(expected_ids - result.case_ids),
               "expected_negative_control_kills": expected_kills(data)}
    save(report_path, summary)
    return int(not result.wasSuccessful() or bool(result.skipped) or bool(summary["missing_dimensions"])
               or bool(summary["missing_case_ids"]))


def sentinel_discovery(report_path):
    # Snapshot BEFORE ordinary discovery, then run an unrelated peer before/after.
    old_env, old_temp, old_path = dict(os.environ), tempfile.tempdir, list(sys.path)
    import socket
    import subprocess
    old_hooks = (socket.socket.connect, socket.create_connection, socket.getaddrinfo, subprocess.Popen, os.system)
    sentinel_file = Path(tempfile.gettempdir()) / "sentinel-fixture.txt"
    sentinel_file.write_text("unrelated sentinel")

    class Sentinel(unittest.TestCase):
        def runTest(self):
            self.assertEqual(dict(os.environ), old_env)
            self.assertEqual(tempfile.tempdir, old_temp)
            self.assertEqual(sys.path, old_path)
            # Discovery neither installs nor removes process-level hooks.
            self.assertEqual((socket.socket.connect, socket.create_connection, socket.getaddrinfo,
                              subprocess.Popen, os.system), old_hooks)
            self.assertEqual(sentinel_file.read_text(), "unrelated sentinel")
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(Path(directory).parent, Path(old_temp))
    before = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Sentinel()]))
    code = run_suite(report_path, discovery=True)
    after = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Sentinel()]))
    expect(before.wasSuccessful() and after.wasSuccessful(), "discovery_peer_contamination")
    return code


class OfflineGuardRefusal(RuntimeError):
    pass


class OfflineGuard:
    """Script-child-only scoped refusal of network, listener and child-process calls.

    Installed by main() before the product import; never at module import, so ordinary
    unittest discovery stays inert. Probes refuse before any socket, DNS or process syscall.
    """
    PROBE_ADDRESS = ("192.0.2.1", 9)  # RFC 5737 documentation range; never contacted

    def __init__(self):
        import http.client
        import socket
        import subprocess
        import urllib.request
        self.modules = (socket, urllib.request, http.client, subprocess)
        self.targets = [(socket.socket, "connect"), (socket.socket, "connect_ex"), (socket.socket, "bind"),
                        (socket.socket, "listen"), (socket.socket, "sendto"), (socket, "create_connection"),
                        (socket, "create_server"), (socket, "getaddrinfo"), (urllib.request, "urlopen"),
                        (http.client.HTTPConnection, "connect"), (subprocess, "Popen"), (os, "system")]
        self.saved, self.blocked, self.probes, self.report = [], [], [], {}

    def _wrap(self, label):
        def guarded(*_args, **_kwargs):
            self.blocked.append(label)
            raise OfflineGuardRefusal("offline_guard_refused:" + label)
        return guarded

    def __enter__(self):
        preloaded = sorted(name for name in sys.modules if name.split(".")[0] in ("campaign_tool", "tests"))
        expect(not preloaded, "product_or_test_imported_before_guard:" + ",".join(preloaded))
        for owner, name in self.targets:
            self.saved.append((owner, name, getattr(owner, name)))
            setattr(owner, name, self._wrap(getattr(owner, "__name__", "?") + "." + name))
        sock, request, client, subprocess = self.modules
        for label, call in (("socket.create_connection", lambda: sock.create_connection(self.PROBE_ADDRESS, 0.01)),
                            ("socket.getaddrinfo", lambda: sock.getaddrinfo("fc11.invalid", 9)),
                            ("socket.create_server", lambda: sock.create_server(("127.0.0.1", 0))),
                            ("urllib.request.urlopen", lambda: request.urlopen("http://192.0.2.1:9/", timeout=0.01)),
                            ("http.client.HTTPConnection.connect",
                             lambda: client.HTTPConnection(*self.PROBE_ADDRESS, timeout=0.01).connect()),
                            ("subprocess.Popen", lambda: subprocess.Popen(["true"])),
                            ("os.system", lambda: os.system("true"))):
            try:
                call()
            except OfflineGuardRefusal as error:
                self.probes.append({"probe": label, "refused": str(error)})
            else:
                raise AssertionError("offline_guard_probe_not_refused:" + label)
        expect(len(self.blocked) == len(self.probes) == 7, "offline_guard_probe_accounting")
        self.blocked = []
        self.report = {"installed_before_imports": True, "preloaded_guarded_modules": preloaded,
                       "patched": [getattr(o, "__name__", "?") + "." + n for o, n in self.targets],
                       "self_probes": list(self.probes), "permitted_children": 0}
        return self

    def __exit__(self, *exc):
        for owner, name, original in reversed(self.saved):
            setattr(owner, name, original)
        self.report.update(restored=all(getattr(o, n) is orig for o, n, orig in self.saved),
                           unexpected_blocked=list(self.blocked),
                           guarded_modules_imported_under_guard=sorted(
                               name for name in sys.modules if name.split(".")[0] in ("campaign_tool", "tests")))
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("suite", "discovery"), default="suite")
    parser.add_argument("--temp-root", required=True)
    args = parser.parse_args()
    temp = Path(args.temp_root)
    expect(temp.is_absolute() and ".." not in temp.parts and temp.is_dir(), "literal_existing_temp_root")
    tempfile.tempdir = str(temp)  # fresh isolated script process; precedes product import
    guard = OfflineGuard()
    with guard:  # offline boundary precedes the product/test import graph
        sys.path.insert(0, str(REPO))
        report = temp / "suite-summary.json"
        code = sentinel_discovery(report) if args.mode == "discovery" else run_suite(report)
    save(temp / "offline-guard.json", guard.report)
    if not guard.report["restored"] or guard.report["unexpected_blocked"]:
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
