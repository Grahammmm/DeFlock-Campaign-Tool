"""FC13 fictional in-memory revision contract; independently Sol-authored.

Ordinary discovery is inert: no product imports, environment edits, path edits,
network hooks, or temporary files at module scope. Script mode configures its
explicit temporary root and scoped offline mocks before importing product code.
"""
import argparse
from contextlib import ExitStack, contextmanager
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
from typing import cast
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc13-structured-revisions/cases.json"
CASE_IDS = ("baseline", "normalization_version", "source_artifact", "agency_metadata",
            "rules_version", "config_annotation")
REVISION_KEYS = ("normalization_sha256", "source_bindings_sha256", "joins_sha256",
                 "rules_sha256", "config_sha256")
EVENTS = []


def canonical_local(value):
    # Independent test oracle: never calls pipeline.digest/core.canonical.
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def digest_local(value):
    return hashlib.sha256(canonical_local(value).encode("utf-8")).hexdigest()


def label_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextmanager
def offline_scope():
    """Scoped, restored sentinels, not a hostile-code sandbox."""
    connect = sqlite3.connect

    def memory_only(database, *args, **kwargs):
        if database != ":memory:":
            raise AssertionError("FC13 requires in-memory SQLite")
        return connect(database, *args, **kwargs)

    with ExitStack() as stack:
        for target in ("socket.socket", "socket.create_connection", "socket.getaddrinfo",
                       "subprocess.Popen", "os.system"):
            stack.enter_context(mock.patch(target, side_effect=AssertionError("FC13 offline sentinel")))
        stack.enter_context(mock.patch("sqlite3.connect", side_effect=memory_only))
        yield


def fixture_data():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if data["schema"] != "fc13-structured-revisions/v1":
        raise ValueError("fixture schema")
    if tuple(case["id"] for case in data["cases"]) != CASE_IDS:
        raise ValueError("missing/reordered/extra fixture cases")
    return data


def prepare(case):
    """Own synthetic context; no dependence on existing test fixture functions."""
    b = fixture_data()["baseline"]
    subject, policy = label_hash(b["subject_label"]), label_hash(b["policy_label"])
    sources = [
        {"unit_id": "retention-source", "original_sha256": subject, "locator": {"row": 1},
         "artifact_sha256": label_hash(b["artifact_label"])},
        {"unit_id": "policy-source", "original_sha256": policy, "locator": {"page": 1},
         "artifact_sha256": label_hash(b["policy_artifact_label"])},
    ]
    records = [
        {"original_sha256": subject, "locator": {"row": 1}, "event_id": b["event_id"],
         "event_at": b["event_at"], "record_type": "retention-setting",
         "fields": {"retention_mode": "days", "retention_days": b["retention_days"]}},
        {"original_sha256": policy, "locator": {"page": 1}, "event_id": "fc13-policy",
         "record_type": "policy", "fields": {}},
    ]
    normalized = {"schema": "structured-normalization-v1", "version": b["normalization_version"],
                  "complete": True, "units": [
                      {"record": record, "source": {key: source[key] for key in ("unit_id", "artifact_sha256")}}
                      for record, source in zip(records, sources)]}
    rules = {"version": b["rules_version"], "entries": [
        {"detector": "retention-over-policy", "agency_id": b["agency_id"],
         "rule_id": "fc13-fictional-rule", "version": "fc13-fictional-policy-v1",
         "source_sha256": policy, "effective_from": "2020-01-01", "effective_to": "2021-01-01",
         "parameters": {"maximum_days": b["maximum_days"], "unit": "calendar_days"},
         "version_verified": True, "applicability": "yes"}]}
    config = {"as_of": b["as_of"], "annotation": b["config_annotation"]}
    metadata = b["agency_metadata"]
    domain = case["domain"]
    if domain == "normalization":
        normalized["version"] = case["value"]
    elif domain == "source_bindings":
        sources[0]["artifact_sha256"] = label_hash(case["value"])
        normalized["units"][0]["source"]["artifact_sha256"] = sources[0]["artifact_sha256"]
    elif domain == "joins":
        metadata = case["value"]
    elif domain == "rules":
        rules["version"] = case["value"]
    elif domain == "config":
        config["annotation"] = case["value"]
    elif domain != "none":
        raise ValueError("unexpected revision domain")
    normalized["source_inventory_sha256"] = digest_local(sources)
    # Fixture-owned SQL only. The ordered independent revision expectation is
    # assembled from these literal rows, not from CanonicalJoins.revision_sha256.
    join = {"id": "fc13-join", "original_sha256": subject, "status": "typed",
            "agency_id": b["agency_id"], "request_id": b["request_id"],
            "evidence": canonical_local({"source_sha256": subject,
                                          "locator": canonical_local({"row": 1})})}
    revision_rows = [copy.deepcopy(join), {"agency": [b["agency_id"], metadata]},
                     {"request_id": b["request_id"], "agency_id": b["agency_id"]}]
    con = sqlite3.connect(":memory:")
    try:
        con.executescript("""
            CREATE TABLE joins(id TEXT, original_sha256 TEXT, status TEXT, agency_id TEXT,
                               request_id TEXT, evidence TEXT);
            CREATE TABLE agencies(id TEXT, metadata TEXT);
            CREATE TABLE requests(id TEXT, agency_id TEXT);
        """)
        con.execute("INSERT INTO joins VALUES(?,?,?,?,?,?)", tuple(join.values()))
        con.execute("INSERT INTO agencies VALUES(?,?)", (b["agency_id"], b["agency_metadata"]))
        if domain == "joins":
            con.execute("UPDATE agencies SET metadata=? WHERE id=?", (metadata, b["agency_id"]))
        con.execute("INSERT INTO requests VALUES(?,?)", (b["request_id"], b["agency_id"]))
        pipeline = importlib.import_module("campaign_tool.records.detectors.pipeline")
        joins = pipeline.canonical_joins(con, sources)
    finally:
        con.close()
    expected = {"normalization_sha256": digest_local(normalized),
                "source_bindings_sha256": digest_local(sources),
                "joins_sha256": digest_local(revision_rows),
                "rules_sha256": digest_local(rules), "config_sha256": digest_local(config)}
    args = dict(subject_sha256=subject, normalized=normalized, source_bindings=sources,
                joins=joins, rules=rules, config=config)
    return args, expected


def semantics(output):
    return {name: {"status": entry["status"], "reason": entry["reason"],
                   "hit_count": entry["hit_count"], "counts": entry["manifest"]["counts"],
                   "suppressed_hit_count": entry["suppressed_hit_count"]}
            for name, entry in output["structured"].items()}


class StructuredRevisionTests(unittest.TestCase):
    def setUp(self):
        self.scope = ExitStack()
        self.addCleanup(self.scope.close)
        self.scope.enter_context(offline_scope())
        self.pipeline = importlib.import_module("campaign_tool.records.detectors.pipeline")

    def check_outer(self, output):
        self.assertEqual(output["result_sha256"], digest_local(
            {key: value for key, value in output.items() if key != "result_sha256"}), "wrong_outer_hash")

    def check_revisions(self, output, expected):
        self.assertEqual(set(output["revisions"]), set(REVISION_KEYS))
        for key in REVISION_KEYS:
            self.assertEqual(output["revisions"][key], expected[key], "wrong_revision:" + key)
        self.check_outer(output)

    def exercise(self, case):
        baseline, expected_base = prepare(fixture_data()["cases"][0])
        before = self.pipeline.evaluate_structured(**baseline)
        self.check_revisions(before, expected_base)
        args, expected = prepare(case)
        snapshot = copy.deepcopy(args)
        output = self.pipeline.evaluate_structured(**args)
        self.assertEqual(args, snapshot, "evaluation mutated caller inputs")
        self.check_revisions(output, expected)
        self.assertEqual(args["joins"].source_bindings_sha256, expected["source_bindings_sha256"])
        self.assertEqual(args["joins"].revision_sha256, expected["joins_sha256"])
        self.assertEqual(args["joins"].rows_json, baseline["joins"].rows_json,
                         "revision-only change altered the join relationship")
        ids = [row["unit_id"] for row in args["source_bindings"]]
        self.assertEqual(len(ids), len(set(ids)), "unique source mappings")
        self.assertEqual({row["source"]["unit_id"] for row in args["normalized"]["units"]}, set(ids))
        changed = {key for key in REVISION_KEYS if before["revisions"][key] != output["revisions"][key]}
        self.assertEqual(changed, set(case["changed_revisions"]))
        self.assertEqual(semantics(before), semantics(output))
        retained = output["structured"]["retention-over-policy"]
        self.assertEqual(retained["status"], "evaluated")
        self.assertEqual(retained["hit_count"], 1)
        self.assertEqual(retained["manifest"]["counts"]["eligible"], 1)
        self.assertEqual(retained["manifest"]["counts"]["evaluated"], 1)
        self.assertEqual(retained["manifest"]["counts"]["blocked"], 0)
        self.assertEqual(len(output["structured"]), 7)
        self.assertTrue(all(entry["status"] == "inapplicable" for name, entry in
                            output["structured"].items() if name != "retention-over-policy"))
        self.assertEqual(output["acceptance"], "supplied_evidence_triage_only")
        self.assertIs(output["publication_ready"], False)
        self.assertEqual(output["stage_promotions"], 0)
        if changed:
            self.assertNotEqual(before["result_sha256"], output["result_sha256"])
        else:
            self.assertEqual(before, output)
        repeated = self.pipeline.evaluate_structured(**args)
        self.assertEqual(output, repeated, "exact rerun")
        frozen = copy.deepcopy(output)
        # Mutating a nested returned reference must affect neither inputs nor
        # another returned result; rerunning must still reproduce frozen bytes.
        retained["hits"][0]["evidence"][0]["locator"]["row"] = 999
        output["revisions"]["config_sha256"] = "synthetic caller mutation"
        self.assertEqual(args, snapshot)
        self.assertEqual(repeated, frozen)
        self.assertEqual(self.pipeline.evaluate_structured(**args), frozen)
        # Conversely mutate every mutable input domain after evaluation.
        args["normalized"]["units"][0]["record"]["locator"]["row"] = 777
        args["source_bindings"][0]["locator"]["row"] = 888
        args["rules"]["entries"][0]["parameters"]["maximum_days"] = 99
        args["config"]["annotation"] = "mutated after return"
        self.assertEqual(repeated, frozen, "returned output aliases caller data")
        self.check_outer(repeated)
        EVENTS.append({"case": case["id"], "changed_revisions": sorted(changed),
                       "expected_revisions": expected, "actual_revisions": repeated["revisions"],
                       "result_sha256": repeated["result_sha256"], "semantics": semantics(repeated),
                       "invariants": fixture_data()["invariants"]})

    def test_90_stale_config_negative_control(self):
        data = fixture_data()
        baseline, _ = prepare(data["cases"][0])
        changed_case = next(case for case in data["cases"] if case["id"] == "config_annotation")
        args, expected = prepare(changed_case)
        real_api = self.pipeline.evaluate_structured
        prior = real_api(**baseline)
        positive = real_api(**args)
        self.check_revisions(positive, expected)

        def stale_config_wrapper(**kwargs):
            result = real_api(**kwargs)
            result["revisions"]["config_sha256"] = prior["revisions"]["config_sha256"]
            result["result_sha256"] = digest_local({k: v for k, v in result.items() if k != "result_sha256"})
            return result

        with mock.patch.object(self.pipeline, "evaluate_structured", side_effect=stale_config_wrapper):
            mutant = self.pipeline.evaluate_structured(**args)
            self.check_outer(mutant)
            self.assertEqual(semantics(mutant), semantics(positive))
            for key in REVISION_KEYS:
                if key != "config_sha256":
                    self.assertEqual(mutant["revisions"][key], expected[key])
            with self.assertRaisesRegex(AssertionError, "wrong_revision:config_sha256") as killed:
                self.check_revisions(mutant, expected)
        self.assertIs(self.pipeline.evaluate_structured, real_api)
        restored = self.pipeline.evaluate_structured(**args)
        self.check_revisions(restored, expected)
        self.assertEqual(restored, positive)
        event = {"control": data["negative_control"]["id"], "outer_hash_valid": True,
                 "specific_failure": str(killed.exception), "restored_positive": True,
                 "mutant": mutant, "positive": restored}
        EVENTS.append(event)
        print("DECIDING_CONTROL " + canonical_local(event))


def _case_test(case_id):
    def test(self):
        case = next(case for case in fixture_data()["cases"] if case["id"] == case_id)
        self.exercise(case)
    return test


for _index, _case_id in enumerate(CASE_IDS):
    setattr(StructuredRevisionTests, "test_%02d_%s" % (_index, _case_id), _case_test(_case_id))


def ambient():
    return (dict(os.environ), tempfile.tempdir, list(sys.path), socket.socket,
            subprocess.Popen, sqlite3.connect, sys.addaudithook)


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ids = []

    def startTest(self, test):
        self.ids.append(test.id())
        super().startTest(test)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--temp-root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--mode", choices=("suite", "compatibility", "discovery"), default="suite")
    options = parser.parse_args()
    root = options.temp_root.resolve(strict=True)
    if not root.is_dir() or REPO == root or REPO in root.parents:
        raise ValueError("temporary root must be an existing unrelated directory")
    if options.receipt.resolve().parent != root:
        raise ValueError("receipt must be directly under temporary root")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))  # Script setup only; module-derived path.
    EVENTS.clear()
    with mock.patch.dict(os.environ, {key: str(root) for key in ("TMPDIR", "TMP", "TEMP")}), \
            mock.patch.object(tempfile, "tempdir", str(root)), offline_scope():
        loader = unittest.TestLoader()
        event_source = EVENTS
        if options.mode == "compatibility":
            selectors = [
                "tests.records.test_structured_detector_pipeline.StructuredPipelineTests.test_duplicates_determinism_revisions_and_deep_copy",
                "tests.records.test_structured_detector_pipeline.StructuredPipelineTests.test_bare_typed_join_and_stale_snapshot_block"]
            suite = loader.loadTestsFromNames(selectors)
        elif options.mode == "discovery":
            before = ambient()
            with mock.patch.object(sys, "addaudithook", side_effect=AssertionError("discovery installed audit hook")):
                during = ambient()
                discovered = loader.discover(str(REPO / "tests/records"),
                                             pattern="test_fc13_structured_revisions.py", top_level_dir=str(REPO))
                if ambient() != during:
                    raise AssertionError("discovery-time ambient contamination")
            if ambient() != before:
                raise AssertionError("discovery did not restore ambient state")
            imported = importlib.import_module("tests.records.test_fc13_structured_revisions")
            imported.EVENTS.clear()
            event_source = imported.EVENTS

            class Sentinel(unittest.TestCase):
                def runTest(self):
                    self.assertEqual(ambient(), before, "unrelated sentinel changed")
                    self.assertEqual(tempfile.gettempdir(), str(root))
            suite = unittest.TestSuite([Sentinel(), discovered, Sentinel()])
        else:
            suite = loader.loadTestsFromTestCase(StructuredRevisionTests)
        result = cast(RecordingResult, unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite))
        events = event_source
        receipt = {"argv": sys.argv, "cwd": str(Path.cwd()), "interpreter": sys.executable,
                   "mode": options.mode, "tests_run": result.testsRun, "test_ids": result.ids,
                   "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
                   "events": events, "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                   "successful": result.wasSuccessful() and not result.skipped}
        options.receipt.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return 0 if receipt["successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
