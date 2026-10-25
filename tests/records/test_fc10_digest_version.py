"""FC10 private-digest schema_version domain experiment; imports are discovery-inert.

Synthetic only: the fixture describes fictional digests. No real records, model,
network, ledger or publication is used. Every case keeps all other required
fields valid so ``schema_version`` is the deciding gate.
"""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc10-digest-version/cases.json"
MODULE = "tests.records.test_fc10_digest_version"
KILL_MESSAGE = "accepted_non_integer_schema_version:"
CASE_TESTS = ("test_integer_one", "test_bool_true_false", "test_float_one_fraction_nonfinite",
              "test_null_string_array_object", "test_integer_zero_two")


def expect(condition, reason):
    if not condition:
        raise AssertionError(reason)


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    expect(data["schema"] == "fc10-digest-version/v1" and data["synthetic_only"] is True,
           "synthetic_fixture_required")
    return data


def product():
    # Delayed until the dedicated runner/child has redirected tempfile.tempdir.
    from campaign_tool.digest import schema
    return schema


def case_value(case):
    if case["kind"] == "float_text":
        return float(case["value"])
    expect(case["kind"] == "json", "known_case_kind")
    return copy.deepcopy(case["value"])


def original_sha256(data):
    return hashlib.sha256(data["base"]["original_text"].encode("utf-8")).hexdigest()


def empty(data):
    base = data["base"]
    return product().empty_digest(original_sha256(data), base["jurisdiction"], base["law_package_version"],
                                  base["privacy_tier"], base["detector_version"])


def populated(data):
    document = empty(data)
    spec = data["populated"]
    for key in ("model_id", "scope", "actors", "dates", "statements"):
        document[key] = copy.deepcopy(spec[key])
    document["conclusions"] = [{"text": c["text"], "confidence": c["confidence"], "sources": [
        {"sha256": document["sha256"], "locator": c["locator"], "rule_id": c["rule_id"]}]}
        for c in spec["conclusions"]]
    return document


def encoded(document):
    return json.dumps(document, sort_keys=True, allow_nan=True).encode("utf-8")


class DigestVersionTests(unittest.TestCase):
    def setUp(self):
        self.coverage = []
        self.data = fixture()
        self.schema = product()

    def cases(self, dimension, table="rejected"):
        found = [case for case in self.data[table] if case["dimension"] == dimension]
        self.assertTrue(found, "fixture has no cases for " + dimension)
        return found

    def baseline(self):
        document = empty(self.data)
        # Prerequisite: the unmodified ordinary digest is valid, so only schema_version can decide.
        self.assertIs(self.schema.validate_digest(document), document)
        return document

    def check_rejected(self, dimension):
        for case in self.cases(dimension):
            with self.subTest(case=case["id"]):
                document = self.baseline()
                document["schema_version"] = case_value(case)
                try:
                    self.schema.validate_digest(document)
                except ValueError as error:
                    self.assertEqual(str(error), case["error"])
                else:
                    self.fail(KILL_MESSAGE + case["id"])
        self.coverage.append(dimension)

    def test_integer_one(self):
        for case in self.cases("integer_one", "accepted"):
            document = self.baseline()
            document["schema_version"] = case_value(case)
            self.assertIs(type(document["schema_version"]), int)
            self.assertIs(self.schema.validate_digest(document), document)
        self.coverage.append("integer_one")

    def test_bool_true_false(self):
        self.check_rejected("bool_true_false")

    def test_float_one_fraction_nonfinite(self):
        self.assertTrue(any(not math.isfinite(case_value(c)) for c in self.cases("float_one_fraction_nonfinite")))
        self.check_rejected("float_one_fraction_nonfinite")

    def test_null_string_array_object(self):
        self.check_rejected("null_string_array_object")

    def test_integer_zero_two(self):
        self.check_rejected("integer_zero_two")

    def test_ordinary_empty_digest(self):
        document = empty(self.data)
        self.assertEqual(document["schema_version"], 1)
        self.assertIs(type(document["schema_version"]), int)
        self.assertIs(self.schema.validate_digest(document), document)
        self.coverage.append("ordinary_empty_digest")

    def test_populated_digest_with_current_source(self):
        document = populated(self.data)
        self.assertIs(self.schema.validate_digest(document), document)
        # The existing source-binding gate is unchanged.
        stale = populated(self.data)
        stale["conclusions"][0]["sources"][0]["sha256"] = hashlib.sha256(b"other fictional original").hexdigest()
        with self.assertRaisesRegex(ValueError, "source sha256 does not match"):
            self.schema.validate_digest(stale)
        self.coverage.append("populated_digest_with_current_source")

    def test_same_object_and_bytes_preserved(self):
        schema_before = encoded(self.schema.DIGEST_SCHEMA)
        model_before = encoded(self.schema.MODEL_OUTPUT_SCHEMA)
        for document in (empty(self.data), populated(self.data)):
            before = encoded(document)
            self.assertIs(self.schema.validate_digest(document), document)
            self.assertEqual(encoded(document), before)
        for case in self.data["rejected"]:
            document = self.baseline()
            document["schema_version"] = case_value(case)
            before = encoded(document)
            with self.assertRaises(ValueError):
                self.schema.validate_digest(document)
            self.assertEqual(encoded(document), before)
        self.assertEqual(encoded(self.schema.DIGEST_SCHEMA), schema_before)
        self.assertEqual(encoded(self.schema.MODEL_OUTPUT_SCHEMA), model_before)
        self.assertNotIn("schema_version", self.schema.MODEL_OUTPUT_SCHEMA["properties"])
        self.coverage.append("same_object_and_bytes_preserved")

    def test_declaration_negative_control_and_restore(self):
        declared = self.schema.DIGEST_SCHEMA
        self.assertEqual(declared["properties"]["schema_version"].get("const"), 1)
        control = copy.deepcopy(declared)
        # Remove ONLY the integer declaration; everything else is byte-identical.
        control["properties"]["schema_version"].pop("type", None)
        self.assertEqual(control["properties"]["schema_version"], {"const": 1})
        stripped = copy.deepcopy(declared)
        stripped["properties"].pop("schema_version")
        control_rest = copy.deepcopy(control)
        control_rest["properties"].pop("schema_version")
        self.assertEqual(encoded(control_rest), encoded(stripped))
        with mock.patch.object(self.schema, "DIGEST_SCHEMA", control):
            mutant = self.nested()
        killed = sorted(self.killed_ids(mutant))
        expected = sorted(c["id"] for c in self.data["rejected"] if c["new_gate"])
        self.assertTrue(expected)
        self.assertEqual(mutant.errors, [])
        self.assertEqual(killed, expected)
        self.assertEqual(len(mutant.failures), len(expected))
        self.coverage.append("negative_control_killed")
        self.assertIs(self.schema.DIGEST_SCHEMA, declared)
        restored = self.nested()
        # Retained per-run observation (row-local tempdir under the runner); never cleaned up.
        save(Path(tempfile.mkdtemp(prefix="fc10-control-")) / "control-observation.json", {
            "label": "INJECTED_CONTROL_INFORMATIONAL",
            "control_declaration": control["properties"]["schema_version"],
            "candidate_declaration": declared["properties"]["schema_version"],
            "mutant": {"tests_run": mutant.testsRun, "killed_case_ids": killed,
                       "failure_messages": [text.strip().splitlines()[-1] for _t, text in mutant.failures],
                       "errors": len(mutant.errors)},
            "restored": {"tests_run": restored.testsRun, "failures": len(restored.failures),
                         "errors": len(restored.errors), "skipped": len(restored.skipped)}})
        self.assertEqual((restored.failures, restored.errors, restored.skipped), ([], [], []))
        self.assertEqual(restored.testsRun, len(CASE_TESTS))
        self.coverage.append("negative_control_restored")

    def nested(self):
        suite = unittest.TestSuite(DigestVersionTests(name) for name in CASE_TESTS)
        result = unittest.TestResult()
        suite.run(result)
        return result

    @staticmethod
    def killed_ids(result):
        killed = []
        for _test, text in result.failures:
            line = [part for part in text.splitlines() if KILL_MESSAGE in part]
            expect(len(line) == 1, "unexpected_control_failure:" + text[-300:])
            killed.append(line[0].split(KILL_MESSAGE, 1)[1].strip())
        return killed


class CoverageResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dimensions = set()

    def addSuccess(self, test):
        self.dimensions.update(getattr(test, "coverage", []))
        super().addSuccess(test)


def run_suite(report_path, discovery=False):
    loader = unittest.TestLoader()
    suite = (loader.discover(str(REPO / "tests/records"), pattern="test_fc10_digest_version.py",
                             top_level_dir=str(REPO)) if discovery
             else loader.loadTestsFromName(MODULE + ".DigestVersionTests"))
    with (Path(report_path).parent / "unittest.txt").open("x") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=CoverageResult).run(suite)
    required = set(fixture()["required_dimensions"])
    summary = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "skipped": len(result.skipped), "dimensions": sorted(result.dimensions),
               "required_dimensions": sorted(required), "missing_dimensions": sorted(required - result.dimensions),
               "rejected_cases": len(fixture()["rejected"]), "accepted_cases": len(fixture()["accepted"])}
    save(report_path, summary)
    return int(not result.wasSuccessful() or bool(result.skipped) or bool(summary["missing_dimensions"]))


def sentinel_discovery(report_path):
    # Snapshot BEFORE ordinary discovery, then run an unrelated peer before/after.
    old_env, old_temp, old_path = dict(os.environ), tempfile.tempdir, list(sys.path)
    sentinel_file = Path(tempfile.gettempdir()) / "sentinel-fixture.txt"
    sentinel_file.write_text("unrelated sentinel")

    class Sentinel(unittest.TestCase):
        def runTest(self):
            self.assertEqual(dict(os.environ), old_env)
            self.assertEqual(tempfile.tempdir, old_temp)
            self.assertEqual(sys.path, old_path)
            self.assertEqual(sentinel_file.read_text(), "unrelated sentinel")
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(Path(directory).parent, Path(old_temp))
    before = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Sentinel()]))
    code = run_suite(report_path, discovery=True)
    after = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Sentinel()]))
    expect(before.wasSuccessful() and after.wasSuccessful(), "discovery_peer_contamination")
    return code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("suite", "discovery"), default="suite")
    parser.add_argument("--temp-root", required=True)
    args = parser.parse_args()
    temp = Path(args.temp_root)
    expect(temp.is_absolute() and ".." not in temp.parts and temp.is_dir(), "literal_existing_temp_root")
    tempfile.tempdir = str(temp)  # fresh isolated script process; precedes product import
    sys.path.insert(0, str(REPO))
    report = temp / "suite-summary.json"
    return sentinel_discovery(report) if args.mode == "discovery" else run_suite(report)


if __name__ == "__main__":
    raise SystemExit(main())
