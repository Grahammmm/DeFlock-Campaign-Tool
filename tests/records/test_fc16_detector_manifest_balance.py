"""FC16 detector manifest balance: a single-population accounting oracle.

Test-only qualification of campaign_tool.records.detectors.core.evaluate. Synthetic
inputs only; no records, IO beyond reading the committed fixture, network, models or
persistence. Expected event groups, references and counts come from the committed
fixture and an independent key/reference derivation, not from the product grouping
function. Machine-valid output means REVIEW REQUIRED, not factual or legal acceptance.

Author: claude-opus-5-5 / anthropic (opus-coder-bopus).
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
_TEMPORARY_ROOT = None
if __name__ == "__main__":
    # Script mode: consume --temporary-root before any product import and bind the
    # derived repository root (never another tree) so the product package resolves.
    if len(sys.argv) >= 3 and sys.argv[1] == "--temporary-root":
        _TEMPORARY_ROOT = Path(sys.argv[2]).absolute()
        del sys.argv[1:3]
        if not _TEMPORARY_ROOT.is_dir():
            raise SystemExit("temporary root must be an existing directory")
        import tempfile as _tempfile
        _tempfile.tempdir = str(_TEMPORARY_ROOT)
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

import contextlib
import copy
import hashlib
import json
import socket
import subprocess
import unittest
from unittest import mock

from campaign_tool.records.detectors import DETECTORS, evaluate

FIXTURE = _HERE.parents[1] / "fixtures" / "fc16_detector_manifest_balance" / "cases.json"
CASES = json.loads(FIXTURE.read_text())
ORACLE = CASES["oracle"]
CASE_TESTS = {
    "mixed_outcomes": "test_mixed_outcomes",
    "multiplicity": "test_multiplicity",
    "outside_interval": "test_outside_interval",
    "conflict": "test_conflict",
    "empty_and_order": "test_empty_and_order",
    "deciding_negative_control": "test_deciding_negative_control",
}
COUNT_NAMES = ("input_rows", "unique_records", "duplicate_rows", "eligible", "evaluated", "skipped", "blocked")


class OfflineSentinel(RuntimeError):
    pass


def _refuse(*args, **kwargs):
    raise OfflineSentinel("network or subprocess use is refused inside FC16 tests")


@contextlib.contextmanager
def offline():
    """Scoped, restorable sentinels; not a hostile-code sandbox."""
    with contextlib.ExitStack() as stack:
        for target, name in ((socket, "socket"), (socket, "create_connection"), (socket, "getaddrinfo"),
                             (subprocess, "Popen"), (subprocess, "run")):
            stack.enter_context(mock.patch.object(target, name, _refuse))
        yield


def canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def oracle_digest(value):
    return hashlib.sha256(canon(value).encode()).hexdigest()


def seed_sha(seed):
    return hashlib.sha256(seed.encode()).hexdigest()


def needs_support(detector):
    return "support" in CASES["detectors"][detector]


def family(detector):
    return "with_support" if needs_support(detector) else "standard"


def make_unit(detector, role, event_id=None, locator=None):
    spec = CASES["detectors"][detector]
    meta = CASES["roles"][role]
    unit = {"original_sha256": seed_sha("fc16 synthetic original " + detector + " " + role),
            "locator": copy.deepcopy(locator if locator is not None else meta["locator"]),
            "event_id": event_id or meta["event_id"]}
    if role == "irrelevant":
        source = meta
    elif role == "support":
        source = spec["support"]
    else:
        source = spec
    unit["record_type"] = source["record_type"]
    unit["fields"] = copy.deepcopy(source["fields"])
    unit["event_at"] = meta.get("event_at", source["event_at"]) if role != "support" else source["event_at"]
    if meta.get("omit") == "event_at":
        del unit["event_at"]
    return unit


def make_rules(detector):
    spec = CASES["detectors"][detector]
    rule_meta = CASES["rule"]
    parameters = copy.deepcopy(spec["parameters"])
    rules = {"version": CASES["rules_version"]}
    if "holiday_table" in spec:
        table = copy.deepcopy(spec["holiday_table"])
        table_hash = oracle_digest(table)
        del parameters["holiday_table"]
        parameters["holiday_table_sha256"] = table_hash
        rules["holiday_tables"] = {table_hash: table}
    rules["entries"] = [{"detector": detector, "agency_id": CASES["agency_id"], "rule_id": rule_meta["rule_id"],
                         "version": rule_meta["version"], "source_sha256": seed_sha(rule_meta["source_seed"]),
                         "effective_from": rule_meta["effective_from"], "effective_to": rule_meta["effective_to"],
                         "parameters": parameters}]
    return rules


def make_joins(units):
    joins, seen = [], set()
    for unit in units:
        if unit["original_sha256"] not in seen:
            seen.add(unit["original_sha256"])
            joins.append({"original_sha256": unit["original_sha256"], "status": "typed",
                          "agency_id": CASES["agency_id"]})
    return joins


def ref(unit):
    return {"original_sha256": unit["original_sha256"], "locator": unit["locator"]}


def expected_outcome(units, disposition, reason):
    """Independent group oracle: typed agency, record type and event id; sorted unique refs."""
    first = units[0]
    refs = {canon(ref(unit)): ref(unit) for unit in units}
    # The manifest event key is the digest of the canonical group-key string.
    group_key = canon([CASES["agency_id"], first["record_type"], first["event_id"]])
    return {"event_key": oracle_digest(group_key),
            "evidence": [refs[k] for k in sorted(refs)], "duplicate_rows": len(units) - 1,
            "disposition": disposition, "reason": reason}


def oracle_entry(name):
    return ORACLE["mixed_outcomes"]["outcomes"][name]


class Scenario:
    """Physical rows plus the literal expected unique groups they should form."""

    def __init__(self, detector):
        self.detector = detector
        self.groups = {}
        self.rows = []

    def add(self, role, units, disposition, reason):
        self.groups[role] = (units, disposition, reason)
        self.rows.extend(units)
        return self

    def base(self, roles=("evaluated", "blocked", "irrelevant")):
        for role in roles:
            entry = oracle_entry(role)
            self.add(role, [make_unit(self.detector, role)], entry["disposition"], entry["reason"])
        if needs_support(self.detector):
            entry = oracle_entry("support")
            self.add("support", [make_unit(self.detector, "support")], entry["disposition"], entry["reason"])
        return self

    def inputs(self, reverse=False):
        units = copy.deepcopy(self.rows)
        joins = make_joins(units)
        if reverse:
            units.reverse()
            joins.reverse()
        return units, joins, make_rules(self.detector), copy.deepcopy(CASES["config"])

    def expected(self):
        return {outcome["event_key"]: outcome for outcome in
                (expected_outcome(units, d, r) for units, d, r in self.groups.values())}

    def run(self, reverse=False):
        return evaluate(self.detector, *self.inputs(reverse))


def mixed(detector):
    return Scenario(detector).base()


def with_extra(detector, extra, shape):
    scenario = Scenario(detector).base(roles=("blocked", "irrelevant"))
    first = make_unit(detector, "evaluated")
    occurrences = [first]
    for index in range(extra):
        if shape == "exact_copy":
            occurrences.append(copy.deepcopy(first))
        else:
            occurrences.append(make_unit(detector, "evaluated",
                                         locator={"row": CASES["extra_occurrence_first_row"] + index}))
    entry = oracle_entry("evaluated")
    scenario.add("evaluated", occurrences, entry["disposition"], entry["reason"])
    return scenario


def check_row_balance(case, manifest, expected_counts, physical_rows):
    label = "row_balance: "
    counts = manifest["counts"]
    outcomes = manifest["outcomes"]
    case.assertEqual(physical_rows, expected_counts["input_rows"], label + "fixture physical rows")
    case.assertEqual(counts["input_rows"], physical_rows, label + "input_rows must equal supplied physical rows")
    case.assertEqual(counts["input_rows"], counts["unique_records"] + counts["duplicate_rows"],
                     label + "input_rows must equal unique_records plus duplicate_rows")
    case.assertEqual(counts["evaluated"] + counts["skipped"] + counts["blocked"], counts["unique_records"],
                     label + "outcome totals must equal unique_records")
    case.assertEqual(len(outcomes), counts["unique_records"], label + "outcome length must equal unique_records")
    case.assertEqual(sum(row["duplicate_rows"] for row in outcomes), counts["duplicate_rows"],
                     label + "per-outcome duplicate_rows must sum to duplicate_rows")
    case.assertEqual(sum(len(row["evidence"]) <= row["duplicate_rows"] + 1 for row in outcomes), len(outcomes),
                     label + "references never exceed physical occurrences")
    for name in COUNT_NAMES:
        case.assertEqual(counts[name], expected_counts[name], label + "count " + name)


def check_outcome_membership(case, manifest, expected):
    label = "outcome_membership: "
    outcomes = manifest["outcomes"]
    keys = [row["event_key"] for row in outcomes]
    case.assertEqual(len(keys), len(set(keys)), label + "each unique event appears exactly once")
    case.assertEqual(sorted(keys), sorted(expected), label + "exact unique event membership")
    for row in outcomes:
        case.assertEqual(row, expected[row["event_key"]], label + "exact outcome for " + row["event_key"])


def check_hit_projection(case, result):
    label = "hit_projection: "
    manifest, hits = result["manifest"], result["hits"]
    case.assertEqual(manifest["counts"]["hits"], len(manifest["hit_keys"]), label + "hits count vs hit_keys")
    case.assertEqual(len(manifest["hit_keys"]), len(hits), label + "hit_keys vs returned hits")
    case.assertEqual(manifest["hit_keys"], [hit["dedupe_key"] for hit in hits], label + "hit_keys identity")
    case.assertEqual(manifest["stage_promotions"], ORACLE["invariants"]["stage_promotions"], label + "stage_promotions")
    case.assertIs(manifest["publication_ready"], ORACLE["invariants"]["publication_ready"], label + "publication")
    case.assertEqual(manifest["schema"], ORACLE["invariants"]["schema"], label + "schema")
    for hit in hits:
        case.assertIs(hit["publication_ready"], False, label + "hit publication")
        case.assertIs(hit["triage_only"], True, label + "hit triage")


def outcome_for(manifest, units):
    key = expected_outcome(units, None, None)["event_key"]
    matches = [row for row in manifest["outcomes"] if row["event_key"] == key]
    if len(matches) != 1:
        raise AssertionError("outcome_membership: expected exactly one outcome for " + key)
    return matches[0]


class DetectorManifestBalanceTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.enterContext(offline())

    def assert_balanced(self, scenario, result, counts):
        check_row_balance(self, result["manifest"], counts, len(scenario.rows))
        check_outcome_membership(self, result["manifest"], scenario.expected())
        check_hit_projection(self, result)

    def test_mixed_outcomes(self):
        for detector in DETECTORS:
            with self.subTest(case="mixed_outcomes", detector=detector):
                scenario = mixed(detector)
                result = scenario.run()
                manifest = result["manifest"]
                self.assert_balanced(scenario, result, ORACLE["mixed_outcomes"]["counts"][family(detector)])
                reasons = sorted((row["disposition"], row["reason"]) for row in manifest["outcomes"])
                literal = sorted((oracle_entry(role)["disposition"], oracle_entry(role)["reason"])
                                 for role in scenario.groups)
                self.assertEqual(reasons, literal, "exact classified reasons, not only totals")
                for role, (units, _, _) in scenario.groups.items():
                    self.assertEqual(len(outcome_for(manifest, units)["evidence"]), oracle_entry(role)["references"])
                # Pure core may retain hits beside a blocked record.
                self.assertEqual(len(result["hits"]), CASES["detectors"][detector]["expected_hits"])
                self.assertEqual(manifest["counts"]["blocked"], 1)
                evaluated_ref = ref(scenario.groups["evaluated"][0][0])
                for hit in result["hits"]:
                    self.assertIn(evaluated_ref, hit["evidence"])
                    self.assertNotIn(ref(scenario.groups["blocked"][0][0]), hit["evidence"])
                    self.assertNotIn(ref(scenario.groups["irrelevant"][0][0]), hit["evidence"])
                if needs_support(detector):
                    support_ref = ref(scenario.groups["support"][0][0])
                    self.assertEqual(outcome_for(manifest, scenario.groups["support"][0])["reason"],
                                     "not_detector_input_type")
                    self.assertTrue(all(support_ref in hit["evidence"] for hit in result["hits"]),
                                    "valid roster support stays separate yet supports the hit")

    def test_multiplicity(self):
        table = ORACLE["multiplicity"]["extra_occurrences"]
        for detector in DETECTORS:
            baseline = with_extra(detector, 0, "distinct_locator").run()
            for shape in ORACLE["multiplicity"]["shapes"]:
                for extra in sorted(table, key=int):
                    with self.subTest(case="multiplicity", detector=detector, shape=shape, extra=int(extra)):
                        scenario = with_extra(detector, int(extra), shape)
                        result = scenario.run()
                        manifest = result["manifest"]
                        self.assert_balanced(scenario, result, table[extra][family(detector)])
                        occurrences = scenario.groups["evaluated"][0]
                        outcome = outcome_for(manifest, occurrences)
                        self.assertEqual(outcome["duplicate_rows"], int(extra))
                        self.assertEqual(len(outcome["evidence"]), table[extra]["references"][shape])
                        for unit in occurrences:
                            self.assertIn(ref(unit), outcome["evidence"], "each occurrence reference available")
                            for hit in result["hits"]:
                                self.assertIn(ref(unit), hit["evidence"])
                        self.assertEqual(manifest["counts"]["eligible"], baseline["manifest"]["counts"]["eligible"],
                                         "occurrences do not inflate unique eligibility")
                        self.assertEqual(manifest["hit_keys"], baseline["manifest"]["hit_keys"])

    def test_outside_interval(self):
        entry = ORACLE["outside_interval"]["outcome"]
        for detector in DETECTORS:
            with self.subTest(case="outside_interval", detector=detector):
                reference = mixed(detector).run()
                scenario = mixed(detector).add("outside", [make_unit(detector, "outside")],
                                               entry["disposition"], entry["reason"])
                result = scenario.run()
                manifest = result["manifest"]
                self.assert_balanced(scenario, result, ORACLE["outside_interval"]["counts"][family(detector)])
                outcome = outcome_for(manifest, scenario.groups["outside"][0])
                self.assertEqual((outcome["disposition"], outcome["reason"]), ("skipped", "rule_not_effective"))
                self.assertEqual(len(outcome["evidence"]), entry["references"])
                self.assertEqual(manifest["counts"]["eligible"], reference["manifest"]["counts"]["eligible"] + 1)
                others = [row for row in manifest["outcomes"] if row["event_key"] != outcome["event_key"]]
                self.assertEqual(others, reference["manifest"]["outcomes"], "other outcomes unchanged")
                self.assertEqual(manifest["hit_keys"], reference["manifest"]["hit_keys"])

    def test_conflict(self):
        entry = ORACLE["conflict"]["outcome"]
        for detector in DETECTORS:
            with self.subTest(case="conflict", detector=detector):
                reference = mixed(detector).run()
                patch = CASES["detectors"][detector]["conflict_patch"]
                first = make_unit(detector, "evaluated")
                changed = make_unit(detector, "evaluated", locator={"row": CASES["extra_occurrence_first_row"]})
                self.assertIn(patch["field"], changed["fields"])
                self.assertNotEqual(changed["fields"][patch["field"]], patch["value"])
                changed["fields"][patch["field"]] = copy.deepcopy(patch["value"])
                scenario = Scenario(detector).base(roles=("blocked", "irrelevant"))
                scenario.add("evaluated", [first, changed], entry["disposition"], entry["reason"])
                result = scenario.run()
                manifest = result["manifest"]
                self.assert_balanced(scenario, result, ORACLE["conflict"]["counts"][family(detector)])
                outcome = outcome_for(manifest, [first])
                self.assertEqual((outcome["disposition"], outcome["reason"]),
                                 ("blocked", "conflicting_duplicate_event"))
                self.assertEqual(outcome["duplicate_rows"], entry["duplicate_rows"])
                self.assertEqual(len(outcome["evidence"]), entry["references"])
                self.assertEqual(len(result["hits"]), ORACLE["conflict"]["expected_hits"])
                others = [row for row in manifest["outcomes"] if row["event_key"] != outcome["event_key"]]
                expected_others = [row for row in reference["manifest"]["outcomes"]
                                   if row["event_key"] != outcome["event_key"]]
                self.assertEqual(others, expected_others, "other outcomes unchanged")

    def test_empty_and_order(self):
        spec = ORACLE["empty_and_order"]
        order_free = set(spec["order_free_manifest_fields"])
        for detector in DETECTORS:
            for variant in spec["variants"]:
                with self.subTest(case="empty_and_order", detector=detector, variant=variant):
                    if variant == "empty":
                        result = evaluate(detector, [], [], make_rules(detector), copy.deepcopy(CASES["config"]))
                        check_hit_projection(self, result)
                        counts = dict(result["manifest"]["counts"])
                        self.assertEqual(counts, spec["empty_counts"])
                        self.assertEqual(result["manifest"]["outcomes"], [])
                        self.assertEqual(result["hits"], [])
                    elif variant == "all_irrelevant":
                        scenario = Scenario(detector)
                        for index in range(spec["all_irrelevant_units"]):
                            unit = make_unit(detector, "irrelevant", event_id="irrelevant-" + str(index + 1),
                                             locator={"row": 50 + index})
                            unit["original_sha256"] = seed_sha("fc16 synthetic irrelevant " + str(index))
                            scenario.add("irrelevant-" + str(index), [unit], "skipped", "not_detector_input_type")
                        result = scenario.run()
                        expected = {k: v for k, v in spec["all_irrelevant_counts"].items() if k != "hits"}
                        self.assert_balanced(scenario, result, expected)
                        self.assertEqual(result["manifest"]["counts"]["hits"], spec["all_irrelevant_counts"]["hits"])
                        self.assertEqual(result["hits"], [])
                    elif variant in ("reverse_order", "reverse_order_with_duplicates"):
                        scenario = mixed(detector) if variant == "reverse_order" else \
                            with_extra(detector, 3, "distinct_locator")
                        forward, backward = scenario.run(), scenario.run(reverse=True)
                        counts = (ORACLE["mixed_outcomes"]["counts"] if variant == "reverse_order" else
                                  ORACLE["multiplicity"]["extra_occurrences"]["3"])[family(detector)]
                        for result in (forward, backward):
                            self.assert_balanced(scenario, result, counts)
                        self.assertEqual(backward["hits"], forward["hits"])
                        self.assertEqual(backward["manifest"]["outcomes"], forward["manifest"]["outcomes"])
                        self.assertEqual({k: v for k, v in backward["manifest"].items() if k not in order_free},
                                         {k: v for k, v in forward["manifest"].items() if k not in order_free})
                        self.assertNotEqual(backward["manifest"]["input_sha256"], forward["manifest"]["input_sha256"])
                    else:
                        scenario = with_extra(detector, 1, "distinct_locator")
                        values = scenario.inputs()
                        before = canon(values)
                        first = evaluate(detector, *values)
                        second = evaluate(detector, *values)
                        third = evaluate(detector, *values)
                        self.assertEqual(canon(values), before, "inputs not mutated")
                        self.assertEqual(first, second)
                        self.assertEqual(second, third)
                        self.assert_balanced(scenario, first,
                                             ORACLE["multiplicity"]["extra_occurrences"]["1"][family(detector)])

    def test_deciding_negative_control(self):
        controls = ORACLE["negative_controls"]
        for detector in DETECTORS:
            with self.subTest(case="deciding_negative_control", detector=detector,
                              control="duplicates_counted_as_unique"):
                control = controls["duplicates_counted_as_unique"]
                scenario = with_extra(detector, control["extra_occurrences"], "distinct_locator")
                counts = ORACLE["multiplicity"]["extra_occurrences"][str(control["extra_occurrences"])][family(detector)]
                genuine = scenario.run()["manifest"]
                mutant = copy.deepcopy(genuine)
                # Isolated count projection copy: physical rows counted as unique records.
                mutant["counts"]["unique_records"] = mutant["counts"]["input_rows"]
                mutant.pop("manifest_sha256")
                mutant["manifest_sha256"] = oracle_digest(mutant)
                with self.assertRaises(AssertionError) as raised:
                    check_row_balance(self, mutant, counts, len(scenario.rows))
                self.assertIn("row_balance: input_rows must equal unique_records plus duplicate_rows",
                              str(raised.exception))
                check_outcome_membership(self, mutant, scenario.expected())  # unrelated prerequisite stays valid
                self.assertEqual(genuine["counts"]["unique_records"], counts["unique_records"])
            with self.subTest(case="deciding_negative_control", detector=detector,
                              control="blocked_outcome_omitted"):
                scenario = mixed(detector)
                genuine = scenario.run()["manifest"]
                mutant = copy.deepcopy(genuine)
                omitted = outcome_for(mutant, scenario.groups[controls["blocked_outcome_omitted"]["omitted_role"]][0])
                self.assertEqual(omitted["disposition"], "blocked")
                mutant["outcomes"] = [row for row in mutant["outcomes"] if row is not omitted]
                for name in ("evaluated", "skipped", "blocked"):
                    mutant["counts"][name] = sum(row["disposition"] == name for row in mutant["outcomes"])
                mutant.pop("manifest_sha256")
                mutant["manifest_sha256"] = oracle_digest(mutant)
                with self.assertRaises(AssertionError) as raised:
                    check_outcome_membership(self, mutant, scenario.expected())
                self.assertIn("outcome_membership: exact unique event membership", str(raised.exception))
            with self.subTest(case="deciding_negative_control", detector=detector, control="genuine_rerun"):
                # Restore: the genuine positives pass every gate after the mutants.
                for scenario, counts in ((with_extra(detector, 3, "distinct_locator"),
                                          ORACLE["multiplicity"]["extra_occurrences"]["3"][family(detector)]),
                                         (mixed(detector), ORACLE["mixed_outcomes"]["counts"][family(detector)])):
                    self.assert_balanced(scenario, scenario.run(), counts)


class _ScriptResult(unittest.TextTestResult):
    records = {"test_ids": [], "subcases": []}

    def startTest(self, test):
        self.records["test_ids"].append(test.id())
        super().startTest(test)

    def addSubTest(self, test, subtest, err):
        self.records["subcases"].append({"id": subtest.id(), "success": err is None})
        super().addSubTest(test, subtest, err)


def _script_main():
    suite = unittest.TestLoader().loadTestsFromTestCase(DetectorManifestBalanceTests)
    result = unittest.TextTestRunner(verbosity=2, resultclass=_ScriptResult).run(suite)
    summary = dict(_ScriptResult.records, tests_run=result.testsRun, skipped=len(result.skipped),
                   failures=len(result.failures), errors=len(result.errors),
                   successful=result.wasSuccessful(), case_to_test=CASE_TESTS)
    if _TEMPORARY_ROOT is not None:
        (_TEMPORARY_ROOT / "fc16-script-summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(_script_main())
