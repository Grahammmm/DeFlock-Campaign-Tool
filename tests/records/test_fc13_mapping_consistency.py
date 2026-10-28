"""FC13: one extracted source unit maps to one canonical normalized record.

Synthetic, in-memory only: no records, extraction, network, model or durable
ledger. The frozen matrix lives in tests/fixtures/fc13-mapping-consistency.
Script mode (portable acceptance): python3 <absolute path> --temp-root <dir>.
Importing this module under ordinary discovery changes no ambient state.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import types
import unittest

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "fc13-mapping-consistency" / "cases.json"
SPEC = json.loads(FIXTURE.read_text(encoding="utf-8"))
CONFLICT = SPEC["conflict_reason"]
CONTROL = SPEC["negative_control"]
MARKER = CONTROL["assertion_marker"]
CASES = {case["id"]: case for case in SPEC["cases"]}
if len(CASES) != len(SPEC["cases"]):
    raise ValueError("duplicate FC13 case identity")

# The single comparison statement the declared negative control disables.
COMPARISON = ('            require(first_records.setdefault(source["unit_id"], record_text) == record_text,\n'
              '                    "normalized_source_record_conflict")\n')
DISABLED = '            first_records.setdefault(source["unit_id"], record_text)\n'


def h(value):
    return hashlib.sha256(value.encode()).hexdigest()


def api():
    """Product and existing-fixture imports happen only when a test runs."""
    from campaign_tool.records.detectors import pipeline
    from campaign_tool.records.detectors.core import DETECTORS, digest
    from tests.records import test_structured_detector_pipeline as existing
    return pipeline, DETECTORS, digest, existing


def reorder(value):
    """Same JSON value with reversed key insertion order at every level."""
    if isinstance(value, dict):
        return {key: reorder(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [reorder(item) for item in value]
    return value


def place(units, target, row, placement):
    if placement == "append":
        units.append(row)
    elif placement == "prepend":
        units.insert(0, row)
    elif placement == "adjacent":
        units.insert(target + 1, row)
    else:
        raise ValueError("unknown FC13 placement")


def build(case, module):
    """Return evaluate_structured kwargs with a fresh in-memory join snapshot."""
    _, _, digest, existing = api()
    args, joins = existing.context(case["detector"])
    con = existing.ledger(joins, args["source_bindings"])
    try:
        units = args["normalized"]["units"]
        target = 0 if case["target"] == "subject" else len(units) - 1
        mutation = case["mutation"] or {}
        field, value = mutation.get("field"), mutation.get("value")
        if case["operation"] == "duplicate_mapping":
            row = copy.deepcopy(units[target])
            record = row["record"]
            if field == "key_order":
                row["record"] = reorder(record)
            elif field in ("event_id", "record_type", "event_at"):
                record[field] = value
            elif field == "fields.retention_days":
                record["fields"]["retention_days"] = value
            elif field == "source.unit_id":
                row["source"]["unit_id"] = value
                record["event_id"] = mutation["and_event_id"]
            elif field == "source.artifact_sha256":
                row["source"]["artifact_sha256"] = h(value)
                record["event_id"] = mutation["and_event_id"]
            elif field is not None:
                raise ValueError("unknown FC13 duplicate mutation")
            place(units, target, row, case["placement"])
        elif case["operation"] == "independent_source":
            record = copy.deepcopy(units[target]["record"])
            if field == "locator":
                record["locator"] = copy.deepcopy(value)
            elif field is not None:
                raise ValueError("unknown FC13 independent-source mutation")
            source = copy.deepcopy(args["source_bindings"][target])
            source.update(unit_id="fc13-independent-unit", locator=copy.deepcopy(record["locator"]),
                          artifact_sha256=digest(record))
            args["source_bindings"].append(source)
            args["normalized"]["source_inventory_sha256"] = digest(args["source_bindings"])
            place(units, target, {"record": record, "source": {key: source[key] for key in
                                                                ("unit_id", "artifact_sha256")}},
                  case["placement"])
        elif case["operation"] != "none":
            raise ValueError("unknown FC13 operation")
        args["joins"] = module.canonical_joins(con, args["source_bindings"])
    finally:
        con.close()
    return args


def assert_conflict(test, output, primary):
    """Deciding assertion: every structured entry blocked by the source conflict."""
    _, detectors, _, _ = api()
    entries = output["structured"]
    test.assertEqual(sorted(entries), sorted(detectors))
    for name in [primary, *(other for other in detectors if other != primary)]:
        entry = entries[name]
        test.assertEqual((entry["status"], entry["reason"]), ("blocked", CONFLICT),
                         MARKER + ": " + name + " was not blocked by the source record conflict")
        test.assertEqual(entry["hits"], [])
        test.assertEqual(entry["hit_count"], 0)
        test.assertEqual(entry["suppressed_hit_count"], 0)
        test.assertIsNone(entry["manifest"])
        test.assertIs(entry["publication_ready"], False)
    test.assertEqual(output["stage_promotions"], 0)
    test.assertIs(output["publication_ready"], False)
    test.assertIsNone(output["revisions"]["joins_sha256"])


class MappingConsistencyCases(unittest.TestCase):
    def evaluate(self, case, module=None):
        pipeline = api()[0]
        module = module or pipeline
        return module.evaluate_structured(**build(case, module))

    def normal_hits(self, detector):
        normal = {"id": "normal", "detector": detector, "target": "subject",
                  "operation": "none", "placement": None, "mutation": None}
        return self.evaluate(normal)["structured"][detector]["hits"]

    def check_case(self, case):
        output = self.evaluate(case)
        expect, detector = case["expect"], case["detector"]
        self.assertEqual(output, self.evaluate(case), "deterministic repeated evaluation")
        if expect["outcome"] == "conflict":
            assert_conflict(self, output, detector)
            return
        for entry in output["structured"].values():
            self.assertNotEqual(entry["reason"], CONFLICT)
        if expect["outcome"] == "blocked":
            self.assertEqual({(e["status"], e["reason"]) for e in output["structured"].values()},
                             {("blocked", expect["reason"])})
            return
        self.assertEqual(expect["outcome"], "evaluated")
        entry = output["structured"][detector]
        self.assertEqual((entry["status"], entry["reason"]), ("evaluated", "supplied_context_evaluated"))
        counts = entry["manifest"]["counts"]
        for key in ("evaluated", "input_rows", "unique_records", "duplicate_rows"):
            if key in expect:
                self.assertEqual(counts[key], expect[key], key)
        self.assertEqual(counts["blocked"], 0)
        self.assertGreater(entry["hit_count"], 0)
        if "hit_count" in expect:
            self.assertEqual(entry["hit_count"], expect["hit_count"])
        if "references" in expect:
            self.assertEqual(len(entry["hits"][0]["evidence"]), expect["references"])
        if expect.get("hits_equal_normal"):
            self.assertEqual(entry["hits"], self.normal_hits(detector))
        self.assertEqual(output["stage_promotions"], 0)
        self.assertIs(output["publication_ready"], False)

    def test_fixture_matrix_is_complete(self):
        _, detectors, _, _ = api()
        dimensions = {case["dimension"] for case in SPEC["cases"]}
        self.assertEqual(dimensions, set(SPEC["dimensions"]))
        for kind in ("normal_one_record_per_source", "exact_duplicate_mapping", "same_source_conflict"):
            self.assertEqual({c["detector"] for c in SPEC["cases"] if c["dimension"] == kind}, set(detectors), kind)
        conflicts = {(c["mutation"]["field"], c["placement"]) for c in SPEC["cases"]
                     if c["dimension"] == "same_source_conflict" and c["target"] == "subject"
                     and c["detector"] == "retention-over-policy"}
        self.assertEqual(conflicts, {(field, placement)
                                     for field in ("event_id", "record_type", "fields.retention_days", "event_at")
                                     for placement in ("append", "prepend", "adjacent")})
        for name in [CONTROL["case"], *CONTROL["restored_positive_cases"]]:
            self.assertIn(name, CASES)

    def test_negative_control_disables_only_record_comparison(self):
        pipeline = api()[0]
        original = Path(pipeline.__file__).read_text(encoding="utf-8")
        self.assertEqual(original.count(COMPARISON), 1, "comparison statement must exist exactly once")
        mutated = original.replace(COMPARISON, DISABLED)
        before, after = original.splitlines(), mutated.splitlines()
        self.assertEqual(len(before) - 1, len(after))
        index = before.index(COMPARISON.splitlines()[0])
        self.assertEqual(before[:index] + before[index + 2:], after[:index] + after[index + 1:],
                         "every other statement is unchanged")
        # Isolated in-memory module from the product source; never written to disk.
        mutant = types.ModuleType("campaign_tool.records.detectors.fc13_synthetic_mutant")
        mutant.__package__ = "campaign_tool.records.detectors"
        exec(compile(mutated, "<fc13-synthetic-mutant>", "exec"), mutant.__dict__)
        case = CASES[CONTROL["case"]]
        injected = self.evaluate(case, mutant)
        retention = injected["structured"][case["detector"]]
        self.assertEqual(retention["status"], "evaluated")
        self.assertEqual(retention["manifest"]["counts"]["evaluated"], CONTROL["mutant_expectation"]["evaluated"])
        self.assertEqual(retention["hit_count"], CONTROL["mutant_expectation"]["hit_count"])
        self.assertIsNotNone(injected["revisions"]["joins_sha256"], "unrelated prerequisites stayed valid")
        with self.assertRaises(AssertionError) as killed:
            assert_conflict(self, injected, case["detector"])
        self.assertIn(MARKER, str(killed.exception))
        restored = {}
        for name in CONTROL["restored_positive_cases"]:
            self.check_case(CASES[name])
            restored[name] = "passed"
        print("FC13_CONTROL " + json.dumps({
            "control": CONTROL["id"], "case": case["id"], "changed_statement_lines": [index + 1, index + 2],
            "mutant": {"status": retention["status"], "reason": retention["reason"],
                       "evaluated": retention["manifest"]["counts"]["evaluated"],
                       "hit_count": retention["hit_count"]},
            "killed_assertion": str(killed.exception), "restored": restored}, sort_keys=True), flush=True)


def _case_test(case):
    def test(self):
        self.check_case(case)
    return test


for _case in SPEC["cases"]:
    _name = "test_case_" + _case["id"].replace("-", "_")
    if hasattr(MappingConsistencyCases, _name):
        raise ValueError("duplicate FC13 test name")
    setattr(MappingConsistencyCases, _name, _case_test(_case))
del _case, _name


def _script(argv):
    """Portable script mode: redirect temp storage and go offline BEFORE product imports."""
    import argparse
    import os
    import socket
    import subprocess
    import tempfile

    parser = argparse.ArgumentParser()
    parser.add_argument("--temp-root", required=True)
    args, rest = parser.parse_known_args(argv)
    root = Path(args.temp_root)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise SystemExit("--temp-root must be an existing absolute non-symlink directory")
    tempfile.tempdir = str(root)
    os.environ["TMPDIR"] = str(root)

    def offline(*_args, **_kwargs):
        raise RuntimeError("FC13 tests are offline")

    for owner, name in ((socket, "socket"), (socket, "create_connection"), (socket, "getaddrinfo"),
                        (subprocess, "Popen"), (os, "system")):
        setattr(owner, name, offline)
    sys.dont_write_bytecode = True
    here = Path(__file__).resolve().parent
    sys.path[:] = [entry for entry in sys.path if not entry or Path(entry).resolve() != here]
    sys.path.insert(0, str(REPO))
    ran = []

    class Recording(unittest.TextTestResult):
        def startTest(self, test):
            ran.append(test.id())
            super().startTest(test)

    program = unittest.main(module="__main__", argv=[sys.argv[0], *rest], exit=False,
                            testRunner=unittest.TextTestRunner(resultclass=Recording, verbosity=2))
    result = program.result
    from campaign_tool.records.detectors import pipeline as imported
    product = Path(imported.__file__).resolve()
    print("FC13_SUMMARY " + json.dumps({
        "testsRun": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "skips": len(result.skipped), "test_ids": ran,
        "failed_ids": sorted(test.id() for test, _ in result.failures),
        "error_ids": sorted(test.id() for test, _ in result.errors),
        "dimensions": SPEC["dimensions"], "case_ids": sorted(CASES),
        "pipeline_path": str(product), "pipeline_sha256": hashlib.sha256(product.read_bytes()).hexdigest(),
        "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest()}, sort_keys=True), flush=True)
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(_script(sys.argv[1:]))
