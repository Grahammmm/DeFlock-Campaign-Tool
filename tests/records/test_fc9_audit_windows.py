"""FC9-AUDIT-WINDOWS: produced audit dates map to the correct bounded audit windows.

Synthetic, offline and pure. A finite, independently coded set-membership oracle
enumerates every subset of eight candidate produced dates over one frozen period
and compares exact ``core.evaluate`` audit-gap output. Adjacent windows are
open-left, closed-right ``(previous_due, due]``; the first window is clipped to
the inclusive period start. A produced-document gap is never treated as proof
that no actual audit happened.

Script mode accepts ``--tmp-root DIR`` (handled before any product import; a new
subdirectory becomes ``tempfile.tempdir``) and ``--coverage-out FILE``. Importing
this module under ordinary unittest discovery changes no ambient state.
Machine-valid output is REVIEW REQUIRED, never semantic, legal or live acceptance.
"""
import copy
import datetime
import hashlib
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

_HERE = Path(__file__).resolve()
REPO = _HERE.parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "fc9-audit-windows" / "cases.json"
DETECTOR = "audit-gap"
LIMIT_TEXT = "Missing produced audits do not establish that no audit occurred."


def _script_setup(argv):
    """Script mode only: consume private flags before product imports."""
    rest, coverage_out, index = [argv[0]], None, 1
    while index < len(argv):
        item = argv[index]
        if item in ("--tmp-root", "--coverage-out"):
            if index + 1 >= len(argv):
                raise SystemExit(item + " requires a value")
            value = argv[index + 1]
            if item == "--tmp-root":
                private = Path(value).resolve() / ("fc9-audit-windows-" + uuid.uuid4().hex)
                private.mkdir(parents=True, exist_ok=False)
                tempfile.tempdir = str(private)
            else:
                coverage_out = value
            index += 2
            continue
        rest.append(item)
        index += 1
    return rest, coverage_out


if __name__ == "__main__":
    _ARGV, _COVERAGE_OUT = _script_setup(sys.argv)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

from campaign_tool.records.detectors import core  # noqa: E402  (after script-mode setup)

DAY = datetime.timedelta(days=1)
COVERAGE = {}


def _cover(dimension, name):
    COVERAGE.setdefault(dimension, set()).add(name)


def load_cases():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


def _d(text):
    return datetime.date.fromisoformat(text)


def _period_days(start, end):
    days, value = [], start
    while value < end:
        days.append(value)
        value += DAY
    return days


# ------------------------------------------------------------------ oracle


def in_window(value, index, previous, due, start):
    """Test-only interval predicate: (previous, due]; the first window starts at the period start.

    Independent of core: membership is the clipped closed interval
    [max(start, previous + 1 day), due], except that the first window always
    begins at the inclusive period start when its previous boundary is not
    after the period start.
    """
    lower = start if (index == 0 and previous <= start) else max(start, previous + DAY)
    return lower <= value <= due


def _mutant_in_window(value, index, previous, due, start):
    """Declared synthetic fault: every window also includes its previous boundary."""
    return max(start, previous) <= value <= due


def oracle_windows(frozen):
    start, end = _d(frozen["period_start"]), _d(frozen["period_end_exclusive"])
    first, frequency = _d(frozen["first_due"]), frozen["frequency_days"]
    days = _period_days(start, end)
    dues = [value for value in days if value >= first and (value - first).days % frequency == 0]
    windows = {}
    for index, due in enumerate(dues):
        previous = due - datetime.timedelta(days=frequency)
        windows[due] = {value for value in days if in_window(value, index, previous, due, start)}
    return dues, windows


def oracle(produced, frozen):
    """Expected private values (or None when no gap) for the supplied produced dates."""
    dues, windows = oracle_windows(frozen)
    observed = sorted({_d(text) for text in produced})
    present = set(observed)
    missing = [due for due in dues if not windows[due] & present]
    if not missing:
        return None, 0
    severity = 3 if len(missing) == len(dues) else 2
    return {
        "period_start": frozen["period_start"], "period_end_exclusive": frozen["period_end_exclusive"],
        "due_dates": [due.isoformat() for due in dues],
        "missing_documentation_due_dates": [due.isoformat() for due in missing],
        "produced_dates": [value.isoformat() for value in observed],
        "absence_of_actual_audit_established": False,
    }, severity


def assert_missing_matches(actual_missing, expected_missing):
    """The independent missing-date assertion used by every comparison and the control."""
    if list(actual_missing) != list(expected_missing):
        raise AssertionError("missing documentation due dates differ: core={} oracle={}".format(
            list(actual_missing), list(expected_missing)))


# ------------------------------------------------------------------ inputs


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def build(frozen, produced, fields=None, rule=None, event_at=None):
    unit = {"original_sha256": _sha("fc9 synthetic audit production"), "locator": {"row": 1},
            "event_id": "fc9-audit-1", "event_at": event_at or frozen["event_at"], "record_type": "audit",
            "fields": {"period_start": frozen["period_start"], "period_end": frozen["period_end_exclusive"],
                       "production_complete": True, "audit_dates": copy.deepcopy(produced)}}
    unit["fields"].update(fields or {})
    entry = {"detector": DETECTOR, "agency_id": "fc9-synthetic-agency", "rule_id": "fc9-synthetic-audit-rule",
             "version": "fc9-synthetic-v1", "source_sha256": _sha("fc9 synthetic audit policy"),
             "effective_from": frozen["rule_effective_from"], "effective_to": frozen["rule_effective_to"],
             "parameters": {"frequency_days": frozen["frequency_days"], "first_due": frozen["first_due"]}}
    entry.update(rule or {})
    joins = [{"original_sha256": unit["original_sha256"], "status": "typed", "agency_id": "fc9-synthetic-agency"}]
    return [unit], joins, {"version": "fc9-synthetic-pack-v1", "entries": [entry]}, {}


def observe(frozen, produced, **kwargs):
    return core.evaluate(DETECTOR, *build(frozen, produced, **kwargs))


def subsets(candidates):
    for size in range(len(candidates) + 1):
        for combination in itertools.combinations(candidates, size):
            yield list(combination)


def _subset_name(candidates, subset):
    return "".join("1" if value in subset else "0" for value in candidates)


def required_coverage():
    """Every generated case name the suite must execute, derived from the fixture."""
    cases = load_cases()
    candidates = cases["candidate_produced_dates"]
    names = [_subset_name(candidates, subset) for subset in subsets(candidates)]
    return {
        "fixture_integrity": ["frozen_period", "hand_windows", "oracle_windows"],
        "subset": sorted(names),
        "duplicate_reversed": sorted(names),
        "named": sorted(case["name"] for case in cases["named_cases"]),
        "control": sorted(case["name"] for case in cases["blocking_controls"]),
        "malformed": sorted(case["name"] for case in cases["malformed_cases"]),
        "negative_control": ["previous_boundary_included", "restored_positive"],
    }


def coverage_summary():
    return {dimension: sorted(names) for dimension, names in sorted(COVERAGE.items())}


def case_dimensions():
    required = required_coverage()
    return {dimension: len(names) for dimension, names in sorted(required.items())}


# ------------------------------------------------------------------ control


def run_negative_control():
    """Inject the declared predicate fault, catch only the missing-date assertion, restore, rerun."""
    cases = load_cases()
    frozen, control = cases["frozen"], cases["negative_control"]
    produced = control["produced"]
    module = sys.modules[__name__]
    hit = observe(frozen, produced)["hits"][0]
    core_missing = hit["observed_values_private"]["missing_documentation_due_dates"]
    baseline_expected, _ = oracle(produced, frozen)
    record = {
        "injected_behavior": control["injected"],
        "produced": produced,
        "baseline_observation": {"core_missing": core_missing,
                                 "oracle_missing": baseline_expected["missing_documentation_due_dates"]},
    }
    original = module.in_window
    module.in_window = _mutant_in_window
    try:
        mutant_expected, _ = oracle(produced, frozen)
        mutant_missing = mutant_expected["missing_documentation_due_dates"] if mutant_expected else []
        record["mutant_oracle_missing"] = mutant_missing
        try:
            assert_missing_matches(core_missing, mutant_missing)
            record["caught"] = False
        except AssertionError as error:
            record["caught"] = True
            record["caught_assertion"] = str(error)
        disagreements = 0
        candidates = cases["candidate_produced_dates"]
        for subset in subsets(candidates):
            got = observe(frozen, subset)["hits"]
            got_missing = got[0]["observed_values_private"]["missing_documentation_due_dates"] if got else []
            want, _ = oracle(subset, frozen)
            if got_missing != (want["missing_documentation_due_dates"] if want else []):
                disagreements += 1
        record["mutant_enumeration_disagreements"] = disagreements
    finally:
        module.in_window = original
    record["restored"] = module.in_window is original
    restored_expected, restored_severity = oracle(produced, frozen)
    record["final_candidate_output"] = {"core_missing": core_missing,
                                        "oracle_missing": restored_expected["missing_documentation_due_dates"],
                                        "oracle_severity": restored_severity, "core_severity": hit["severity"]}
    try:
        assert_missing_matches(core_missing, restored_expected["missing_documentation_due_dates"])
        record["restored_positive_ok"] = restored_severity == hit["severity"]
    except AssertionError as error:
        record["restored_positive_ok"] = False
        record["restored_assertion"] = str(error)
    return record


# ------------------------------------------------------------------ tests


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases()
        cls.frozen = cls.cases["frozen"]

    def assertExactAudit(self, produced, expected, severity):
        result = observe(self.frozen, produced)
        counts = result["manifest"]["counts"]
        self.assertEqual((counts["evaluated"], counts["blocked"], counts["skipped"]), (1, 0, 0))
        if expected is None:
            self.assertEqual(result["hits"], [])
            self.assertEqual(severity, 0)
            return result
        self.assertEqual(len(result["hits"]), 1)
        hit = result["hits"][0]
        values = hit["observed_values_private"]
        assert_missing_matches(values["missing_documentation_due_dates"], expected["missing_documentation_due_dates"])
        self.assertEqual(values, expected)
        self.assertIs(values["absence_of_actual_audit_established"], False)
        self.assertEqual(hit["severity"], severity)
        self.assertEqual((hit["detector"], hit["signal"]), (DETECTOR, self.frozen["signal"]))
        self.assertIs(hit["triage_only"], True)
        self.assertIs(hit["publication_ready"], False)
        self.assertIn(LIMIT_TEXT, hit["limits"])
        return result


class FixtureIntegrityTests(_Base):
    def test_frozen_period(self):
        self.assertIs(self.cases["synthetic"], True)
        self.assertEqual((self.frozen["period_start"], self.frozen["period_end_exclusive"],
                          self.frozen["first_due"], self.frozen["frequency_days"]),
                         ("2030-01-01", "2030-01-14", "2030-01-05", 4))
        self.assertEqual(self.cases["candidate_produced_dates"],
                         ["2030-01-%02d" % n for n in (1, 4, 5, 6, 8, 9, 13, 14)])
        _cover("fixture_integrity", "frozen_period")

    def test_hand_windows_partition_period(self):
        hand = {key: value for key, value in self.cases["hand_windows"].items() if key != "comment"}
        self.assertEqual(sorted(hand), self.cases["expected_due_dates"])
        members = [value for key in sorted(hand) for value in hand[key]]
        period = [value.isoformat() for value in _period_days(_d(self.frozen["period_start"]),
                                                              _d(self.frozen["period_end_exclusive"]))]
        self.assertEqual(sorted(members), period)
        self.assertEqual(len(members), len(set(members)))
        self.assertNotIn(self.frozen["period_end_exclusive"], members)
        _cover("fixture_integrity", "hand_windows")

    def test_oracle_windows_equal_hand_windows(self):
        dues, windows = oracle_windows(self.frozen)
        self.assertEqual([due.isoformat() for due in dues], self.cases["expected_due_dates"])
        hand = {key: value for key, value in self.cases["hand_windows"].items() if key != "comment"}
        self.assertEqual({due.isoformat(): sorted(v.isoformat() for v in members) for due, members in windows.items()},
                         hand)
        _cover("fixture_integrity", "oracle_windows")


class EnumerationTests(_Base):
    def test_every_produced_subset_matches_oracle(self):
        candidates = self.cases["candidate_produced_dates"]
        for subset in subsets(candidates):
            name = _subset_name(candidates, subset)
            with self.subTest(subset=name):
                expected, severity = oracle(subset, self.frozen)
                self.assertExactAudit(subset, expected, severity)
                _cover("subset", name)

    def test_every_subset_duplicated_and_reversed_deduplicates(self):
        candidates = self.cases["candidate_produced_dates"]
        for subset in subsets(candidates):
            name = _subset_name(candidates, subset)
            variant = list(reversed(subset)) + subset
            with self.subTest(subset=name):
                expected, severity = oracle(variant, self.frozen)
                plain, _ = oracle(subset, self.frozen)
                self.assertEqual(expected, plain)
                self.assertExactAudit(variant, expected, severity)
                _cover("duplicate_reversed", name)


class NamedBoundaryTests(_Base):
    def test_named_cases_match_hand_expectations_and_oracle(self):
        for case in self.cases["named_cases"]:
            with self.subTest(case=case["name"]):
                expected, severity = oracle(case["produced"], self.frozen)
                self.assertEqual(severity, case["severity"])
                if case["severity"] == 0:
                    self.assertIsNone(expected)
                    self.assertEqual(case["missing"], [])
                else:
                    self.assertEqual(expected["missing_documentation_due_dates"], case["missing"])
                    self.assertEqual(expected["due_dates"], self.cases["expected_due_dates"])
                    self.assertEqual(expected["produced_dates"],
                                     case.get("observed", sorted(set(case["produced"]))))
                self.assertExactAudit(case["produced"], expected, severity)
                _cover("named", case["name"])


class BlockingControlTests(_Base):
    def test_controls_block_without_hit(self):
        positive = observe(self.frozen, [])
        self.assertEqual(positive["hits"][0]["severity"], 3)
        for case in self.cases["blocking_controls"]:
            with self.subTest(case=case["name"]):
                result = observe(self.frozen, [], fields=case.get("fields"), rule=case.get("rule"),
                                 event_at=case.get("event_at"))
                self.assertEqual(result["hits"], [])
                self.assertEqual(result["manifest"]["counts"]["blocked"], 1)
                self.assertEqual(result["manifest"]["outcomes"][0]["disposition"], "blocked")
                self.assertEqual(result["manifest"]["outcomes"][0]["reason"], case["reason"])
                _cover("control", case["name"])

    def test_malformed_produced_dates_block(self):
        for case in self.cases["malformed_cases"]:
            with self.subTest(case=case["name"]):
                result = observe(self.frozen, [], fields={"audit_dates": case["audit_dates"]})
                self.assertEqual(result["hits"], [])
                self.assertEqual(result["manifest"]["counts"]["blocked"], 1)
                self.assertEqual(result["manifest"]["outcomes"][0]["reason"], case["reason"])
                _cover("malformed", case["name"])


class NegativeControlTests(_Base):
    def test_previous_boundary_fault_is_caught_then_restored(self):
        record = run_negative_control()
        control = self.cases["negative_control"]
        self.assertEqual(record["baseline_observation"]["core_missing"], control["core_missing"])
        self.assertEqual(record["baseline_observation"]["oracle_missing"], control["core_missing"])
        self.assertEqual(record["mutant_oracle_missing"], control["mutant_oracle_missing"])
        self.assertIs(record["caught"], True)
        self.assertGreater(record["mutant_enumeration_disagreements"], 0)
        _cover("negative_control", "previous_boundary_included")
        self.assertIs(record["restored"], True)
        self.assertIs(sys.modules[__name__].in_window, in_window)
        self.assertIs(record["restored_positive_ok"], True)
        expected, severity = oracle(control["produced"], self.frozen)
        self.assertExactAudit(control["produced"], expected, severity)
        _cover("negative_control", "restored_positive")


if __name__ == "__main__":
    _program = unittest.main(argv=_ARGV, exit=False, verbosity=2)
    if _COVERAGE_OUT:
        with open(_COVERAGE_OUT, "x", encoding="utf-8") as _handle:
            json.dump({"coverage": coverage_summary(), "required": required_coverage(),
                       "tempdir": tempfile.gettempdir(), "tests_run": _program.result.testsRun},
                      _handle, indent=2, sort_keys=True)
    sys.exit(0 if _program.result.wasSuccessful() else 1)
