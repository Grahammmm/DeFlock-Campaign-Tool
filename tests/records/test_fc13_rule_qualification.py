"""Fictional, test-only qualification table. Import/discovery has no ambient hooks.

The fixture and assertions are independently authored Sol material, not planner
probe adaptations. Existing producer vocabulary is not integration qualification.
"""
import argparse
import copy
from contextlib import ExitStack, contextmanager
import hashlib
import inspect
import io
import itertools
import json
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
FIXTURE = REPO / "tests/fixtures/fc13-rule-qualification/cases.json"
DETECTOR_NAMES = (
    "search-before-training", "purpose-quality", "external-sharing",
    "retention-over-policy", "audit-gap", "cpra-deadline", "volume-anomaly",
)
SEARCH_NAMES = {"search-before-training", "purpose-quality", "volume-anomaly"}


def fixture():
    data = json.loads(FIXTURE.read_text())
    check = unittest.TestCase()
    check.assertEqual(data["schema"], "fc13-rule-qualification/v1")
    check.assertEqual(set(data["detectors"]), set(DETECTOR_NAMES))
    check.assertEqual(data["bindings"], ["bound", "unbound"])
    check.assertEqual(data["version_flags"], ["true", "false", "integer_one", "null", "missing"])
    check.assertEqual(data["applicabilities"], ["yes", "no", "unknown", "missing"])
    check.assertEqual(data["expected_matrix_cases"], 280)
    return data


def dimensions():
    data = fixture()
    return list(itertools.product(DETECTOR_NAMES, data["bindings"],
                                  data["version_flags"], data["applicabilities"]))


def h(label):
    return hashlib.sha256(label.encode()).hexdigest()


def denied(*args, **kwargs):
    raise RuntimeError("fc13_offline_operation_not_admitted")


@contextmanager
def offline():
    """Scoped sentinels before product imports; never a hostile-code sandbox."""
    connect = sqlite3.connect

    def memory_only(database, *args, **kwargs):
        if database != ":memory:":
            return denied()
        return connect(database, *args, **kwargs)

    with ExitStack() as stack:
        for target in ("socket.socket", "socket.create_connection", "socket.getaddrinfo",
                       "subprocess.Popen", "os.system"):
            stack.enter_context(patch(target, denied))
        stack.enter_context(patch("sqlite3.connect", memory_only))
        yield


def context(name):
    """One record per source, inventory authored before any detector output."""
    from campaign_tool.records.detectors import pipeline
    from campaign_tool.records.detectors.core import digest, canonical

    spec = copy.deepcopy(fixture()["detectors"][name])
    subject, policy_hash = h("fc13 fictional subject " + name), h("fc13 fictional policy " + name)
    primary = {"original_sha256": subject, "locator": {"row": 3},
               "event_id": "fictional-event", "record_type": spec["record_type"],
               "event_at": spec["event_at"], "fields": spec["fields"]}
    records = [primary]
    if name == "search-before-training":
        records.append({"original_sha256": h("fc13 fictional training"), "locator": {"row": 8},
                        "event_id": "fictional-training", "record_type": "training-roster",
                        "fields": {"user_id": "fictional-user",
                                   "training_completed_at": spec["training_completed_at"]}})
    records.append({"original_sha256": policy_hash, "locator": {"page": 2},
                    "event_id": "fictional-policy", "record_type": "policy", "fields": {}})
    sources, mapped = [], []
    for index, record in enumerate(records):
        binding = {"unit_id": "fictional-unit-" + str(index),
                   "original_sha256": record["original_sha256"],
                   "locator": copy.deepcopy(record["locator"]), "artifact_sha256": digest(record)}
        sources.append(binding)
        mapped.append({"record": record, "source": {key: binding[key] for key in
                                                    ("unit_id", "artifact_sha256")}})
    rule = {"detector": name, "agency_id": "fictional-agency", "rule_id": "fictional-rule",
            "version": "fictional-rule-v1", "source_sha256": policy_hash,
            "effective_from": "2020-01-01", "effective_to": "2021-01-01",
            "parameters": spec["parameters"], "version_verified": True, "applicability": "yes"}
    rules = {"version": "fictional-pack-v1", "entries": [rule]}
    if name == "cpra-deadline":
        table_hash = digest(spec["holiday_table"])
        rule["parameters"]["holiday_table_sha256"] = table_hash
        rules["holiday_tables"] = {table_hash: spec["holiday_table"]}
    con = sqlite3.connect(":memory:")
    try:
        con.executescript("""
            CREATE TABLE joins(id TEXT, original_sha256 TEXT, status TEXT,
                               agency_id TEXT, request_id TEXT, evidence TEXT);
            CREATE TABLE agencies(id TEXT);
            CREATE TABLE requests(id TEXT, agency_id TEXT);
            INSERT INTO agencies VALUES('fictional-agency');
        """)
        for index, source in enumerate(sources):
            proof = {"source_sha256": source["original_sha256"],
                     "locator": canonical(source["locator"])}
            con.execute("INSERT INTO joins VALUES(?,?,?,?,?,?)",
                        (str(index), source["original_sha256"], "typed", "fictional-agency",
                         None, json.dumps(proof)))
        joins = pipeline.canonical_joins(con, sources)
    finally:
        con.close()
    return {"subject_sha256": subject, "source_bindings": sources, "joins": joins,
            "normalized": {"schema": pipeline.NORMALIZED_SCHEMA, "version": "fictional-normalizer-v1",
                           "complete": True, "source_inventory_sha256": digest(sources), "units": mapped},
            "rules": rules, "config": {"as_of": "2020-02-20"}}


class QualificationTests(unittest.TestCase):
    def setUp(self):
        guard = offline()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        from campaign_tool.records.detectors import pipeline
        self.pipeline = pipeline
        self.assertEqual(tuple(pipeline.DETECTORS), DETECTOR_NAMES)

    def blocked_reason(self, entry, expected):
        self.assertEqual(entry["reason"], expected, "qualification_blocked_reason")
        self.assertEqual(entry["status"], "blocked")
        self.assertIsNone(entry["manifest"])
        self.assertEqual(entry["hits"], [])
        self.assertEqual(entry["hit_count"], 0)
        self.assertEqual(entry["suppressed_hit_count"], 0)

    def evaluated(self, entry, name):
        self.assertEqual((entry["status"], entry["reason"]),
                         ("evaluated", "supplied_context_evaluated"))
        self.assertEqual(entry["hit_count"], fixture()["detectors"][name]["expected_hits"])
        self.assertEqual(len(entry["hits"]), entry["hit_count"])
        self.assertEqual(entry["manifest"]["counts"]["evaluated"], 1)
        self.assertEqual(entry["manifest"]["counts"]["blocked"], 0)

    def matrix(self, name, bound, version, applicability):
        args = context(name)
        rule = args["rules"]["entries"][0]
        if bound == "unbound":
            rule["source_sha256"] = h("fc13 absent fictional policy")
        if version == "missing":
            del rule["version_verified"]
        else:
            rule["version_verified"] = {"true": True, "false": False,
                                        "integer_one": 1, "null": None}[version]
        if applicability == "missing":
            del rule["applicability"]
        else:
            rule["applicability"] = applicability
        output = self.pipeline.evaluate_structured(**args)
        self.assertEqual(set(output["structured"]), set(DETECTOR_NAMES))
        self.assertTrue(all(output["revisions"].values()))
        self.assertEqual(output["stage_promotions"], 0)
        self.assertIs(output["publication_ready"], False)
        self.assertEqual(output["acceptance"], "supplied_evidence_triage_only")
        entry = output["structured"][name]
        if bound == "unbound":
            self.blocked_reason(entry, "rule_source_unbound")
        elif version != "true" or applicability != "yes":
            self.blocked_reason(entry, "rule_version_or_applicability_unresolved")
        else:
            self.evaluated(entry, name)
        for other, result in output["structured"].items():
            self.assertIs(result["publication_ready"], False)
            if result["manifest"] is not None:
                self.assertEqual(result["manifest"]["stage_promotions"], 0)
            if other == name:
                continue
            # Only search-shaped records also match other search detectors.
            # No rule for those detectors was supplied: they must not qualify.
            expected = (("blocked", "required_context_unresolved") if
                        name in SEARCH_NAMES and other in SEARCH_NAMES else
                        ("inapplicable", "no_matching_type_in_complete_normalization"))
            self.assertEqual((result["status"], result["reason"]), expected)
            self.assertEqual(result["hits"], [])
            self.assertEqual(result["hit_count"], 0)

    def negative_control(self, name):
        original = inspect.getsource(self.pipeline.evaluate_structured)
        old = 'entry.get("version_verified") is not True'
        new = 'entry.get("version_verified") != True'
        self.assertEqual(original.count(old), 1)
        changed = original.replace(old, new)
        self.assertEqual(changed.replace(new, old), original)
        namespace = dict(self.pipeline.__dict__)
        exec(compile(textwrap.dedent(changed), "<fc13-isolated-identity-mutant>", "exec"), namespace)
        mutant = namespace["evaluate_structured"]
        args = context(name)
        self.evaluated(mutant(**args)["structured"][name], name)
        args["rules"]["entries"][0]["version_verified"] = 1
        wrong = mutant(**args)["structured"][name]
        self.evaluated(wrong, name)  # Prove the exact wrong-result path, not a setup exception.
        probe = unittest.FunctionTestCase(
            lambda: self.blocked_reason(wrong, "rule_version_or_applicability_unresolved"),
            description="qualification_blocked_reason_integer_one_" + name)
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(probe)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.skipped, [])
        detail = result.failures[0][1]
        self.assertIn("qualification_blocked_reason", detail)
        # The probe must fail on exactly the reason the mutant produces, not on another field.
        self.assertIn("AssertionError: 'supplied_context_evaluated' != "
                      "'rule_version_or_applicability_unresolved'\n", detail)
        # Keep the probe's FAIL report in its own stream. Printed into the shared CI log it was
        # read as a real failure twice (MAINRED-DF, DFMAINRED-3) while the real one was elsewhere.
        print("DECLARED_NEGATIVE_CONTROL " + name + ": integer 1 mutant caught, as intended")
        self.blocked_reason(self.pipeline.evaluate_structured(**args)["structured"][name],
                            "rule_version_or_applicability_unresolved")
        args["rules"]["entries"][0]["version_verified"] = True
        self.evaluated(self.pipeline.evaluate_structured(**args)["structured"][name], name)


def load_tests(loader, tests, pattern):
    # No product imports, global mocks, hooks, environment or tempfile changes.
    generated = type("FrozenMatrix", (QualificationTests,), {})
    for row in dimensions():
        method = "test_matrix__" + "__".join(value.replace("-", "_") for value in row)
        def test(self, row=row):
            self.matrix(*row)
        setattr(generated, method, test)
    for name in DETECTOR_NAMES:
        def control(self, name=name):
            self.negative_control(name)
        setattr(generated, "test_control__" + name.replace("-", "_"), control)
    return loader.loadTestsFromTestCase(generated)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--temporary-root", required=True, type=Path)
    args = parser.parse_args()
    if not args.temporary_root.is_absolute() or not args.temporary_root.is_dir():
        parser.error("temporary root must be an existing absolute invocation directory")
    # Script-only setup is before all product imports. Ordinary discovery is inert.
    import os
    for key in ("TMPDIR", "TEMP", "TMP"):
        os.environ[key] = str(args.temporary_root)
    tempfile.tempdir = str(args.temporary_root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    suite = load_tests(unittest.TestLoader(), None, None)
    planned = [test.id() for test in suite]

    class ScriptResult(unittest.TextTestResult):
        def __init__(self, *values, **kwargs):
            super().__init__(*values, **kwargs)
            self.executed_ids = []

        def startTest(self, test):
            self.executed_ids.append(test.id())
            super().startTest(test)

    result = unittest.TextTestRunner(verbosity=2, resultclass=ScriptResult).run(suite)
    if not isinstance(result, ScriptResult):
        raise TypeError("script recording result unavailable")
    receipt = {"test_ids": planned, "executed_ids": result.executed_ids,
               "tests_run": result.testsRun, "failures": len(result.failures),
               "errors": len(result.errors), "skipped": len(result.skipped)}
    with (args.temporary_root / "absolute-script-tests.json").open("x") as stream:
        json.dump(receipt, stream, indent=2)
    return int(not result.wasSuccessful() or bool(result.skipped) or result.testsRun != 287
               or len(set(planned)) != 287 or result.executed_ids != planned)


if __name__ == "__main__":
    raise SystemExit(main())
