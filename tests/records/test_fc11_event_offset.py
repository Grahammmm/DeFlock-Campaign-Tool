"""FC11-EVENT-OFFSET: impossible event/training offset components are refused before evaluation.

Every value below and in tests/fixtures/fc11-event-offset/cases.json is invented:
agencies, users, events, policies and timestamps describe no real record,
person or source. Machine-valid results are REVIEW REQUIRED, never semantic,
legal, human or live acceptance.

Script mode (portable row): ``python3 test_fc11_event_offset.py --temp-root DIR
[--coverage-out FILE]``. Those two arguments are consumed, and a scoped socket
guard is installed, before any product import; the guard is restored before
exit. Ordinary unittest discovery imports this module without changing
sys.path, the environment, sockets or tempfile settings.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

if __name__ == "__main__":
    import socket
    import tempfile
    import uuid

    _argv, _TEMP_ROOT, _COVERAGE_OUT = [sys.argv[0]], None, None
    _items = iter(sys.argv[1:])
    for _item in _items:
        if _item == "--temp-root":
            _TEMP_ROOT = Path(next(_items))
        elif _item == "--coverage-out":
            _COVERAGE_OUT = Path(next(_items))
        else:
            _argv.append(_item)
    if _TEMP_ROOT is not None:
        _tmp = _TEMP_ROOT / ("tmp-" + uuid.uuid4().hex)
        _tmp.mkdir(parents=True)
        tempfile.tempdir = str(_tmp)
    # Scoped offline guard for this standalone script process only, installed
    # before the first product import. Ordinary discovery never runs this block.
    _GUARD_TRIPS = []

    def _refuse(*args, **kwargs):
        _GUARD_TRIPS.append(repr(args)[:200])
        raise OSError("fc11 script mode: network access refused")
    _GUARDED = [(socket.socket, "connect"), (socket.socket, "connect_ex"), (socket.socket, "bind"),
                (socket, "create_connection"), (socket, "getaddrinfo")]
    _GUARD_ORIGINALS = [(owner, name, getattr(owner, name)) for owner, name in _GUARDED]
    for _owner, _name in _GUARDED:
        setattr(_owner, _name, _refuse)

    def _guards_active():
        return all(getattr(owner, name) is _refuse for owner, name in _GUARDED)

    class _ImportOrderProbe:
        """Inert meta-path seam: records guard state when a product module is first looked up."""
        seen = []

        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] == "campaign_tool":
                self.seen.append([name, _guards_active()])
            return None
    _GUARD_FACTS = {"installed_before_product_import": _guards_active() and not any(
        name.split(".")[0] == "campaign_tool" for name in sys.modules)}
    _PROBE = _ImportOrderProbe()
    sys.meta_path.insert(0, _PROBE)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from datetime import date, datetime, timedelta, timezone  # noqa: E402

from campaign_tool.records.detectors import core  # noqa: E402

if __name__ == "__main__":
    sys.meta_path.remove(_PROBE)
    _GUARD_FACTS.update({"import_observations": list(_PROBE.seen),
                         "every_product_lookup_saw_active_guard": bool(_PROBE.seen)
                         and all(active for _, active in _PROBE.seen),
                         "active_after_product_import": _guards_active()})

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fc11-event-offset" / "cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
AGENCY = CASES["agency_id"]
WALLS = CASES["walls"]
EXERCISED = set()
NC_MARKER = "FC11-NC-PLUS-00-99-BLOCKED"
SEARCH_REASON = "supplied_context_evaluated"

# Frozen-experiment requirement -> named tests that must run and pass.
REQUIREMENTS = {
    "R1_valid_offset_spellings_preserve_baseline_instants": [
        "MomentOffsetTests.test_valid_offsets_preserve_instants"],
    "R2_impossible_components_raise_event_time_invalid": [
        "MomentOffsetTests.test_invalid_offsets_blocked",
        "MomentOffsetTests.test_nc_target_plus_00_99_blocked"],
    "R3_timezone_required_and_malformed_reasons_retained": [
        "MomentOffsetTests.test_missing_timezone_and_malformed_reasons"],
    "R4_retention_valid_offsets_evaluated_with_hit": [
        "RetentionEvaluateTests.test_valid_offsets_evaluated_hit"],
    "R5_retention_invalid_offsets_blocked_no_hit": [
        "RetentionEvaluateTests.test_invalid_offsets_blocked_no_hit",
        "RetentionEvaluateTests.test_nc_target_plus_00_99_blocked_no_hit"],
    "R6_retention_missing_and_malformed_blocked_with_exact_reason": [
        "RetentionEvaluateTests.test_missing_and_malformed_blocked"],
    "R7_event_wall_date_rule_selection_retained": [
        "RetentionEvaluateTests.test_wall_date_rule_selection"],
    "R8_training_valid_offsets_compare_correctly": [
        "TrainingEvaluateTests.test_valid_training_offsets"],
    "R9_training_invalid_offsets_never_hit_or_clear": [
        "TrainingEvaluateTests.test_invalid_training_offsets_blocked"],
    "R10_training_missing_and_malformed_blocked": [
        "TrainingEvaluateTests.test_missing_and_malformed_training_blocked"],
    "R11_ordinary_plus_00_00_partner_valid": [
        "RetentionEvaluateTests.test_nc_partner_plus_00_00_evaluated"],
    "R12_negative_control_lexical_check_patched_out_detected": [
        "SyntheticNegativeControlTests.test_nc_lexical_check_removed_breaks_blocked_assertion"],
}
VALID_CONTROLS = [
    ("MomentOffsetTests", "test_valid_offsets_preserve_instants"),
    ("MomentOffsetTests", "test_missing_timezone_and_malformed_reasons"),
    ("RetentionEvaluateTests", "test_valid_offsets_evaluated_hit"),
    ("RetentionEvaluateTests", "test_nc_partner_plus_00_00_evaluated"),
    ("TrainingEvaluateTests", "test_valid_training_offsets"),
]


def _h(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _case(group, case_id):
    return next(case for case in CASES[group] if case["id"] == case_id)


def _expected(wall, case):
    """Independent instant: naive wall plus the frozen offset, not via the product parser."""
    offset = timedelta(microseconds=case["offset_microseconds"])
    return datetime.fromisoformat(wall).replace(tzinfo=timezone(offset)), offset


def _rule(detector, params):
    return {"detector": detector, "agency_id": AGENCY, "rule_id": "fc11-synthetic-rule",
            "version": "fc11-synthetic-v1", "source_sha256": _h("fc11 synthetic policy " + detector),
            "effective_from": "2020-01-01", "effective_to": "2021-01-01", "parameters": params}


def _joins(units):
    return [{"original_sha256": unit["original_sha256"], "status": "typed", "agency_id": AGENCY}
            for unit in units]


def retention_inputs(event_at):
    unit = {"original_sha256": _h("fc11 synthetic retention setting"), "locator": {"row": 1},
            "event_id": "fc11-retention-1", "event_at": event_at, "record_type": "retention-setting",
            "fields": {"retention_mode": "days", "retention_days": 31}}
    rules = {"version": "fc11-synthetic-pack-v1",
             "entries": [_rule("retention-over-policy", {"maximum_days": 30, "unit": "calendar_days"})]}
    return [unit], _joins([unit]), rules, {"as_of": "2020-01-20"}


def training_inputs(completed_at):
    search = {"original_sha256": _h("fc11 synthetic search log"), "locator": {"row": 1},
              "event_id": "fc11-search-1", "event_at": CASES["search_event_at"], "record_type": "search-log",
              "fields": {"user_id": "fc11-synthetic-user", "purpose": "synthetic specific purpose"}}
    roster = {"original_sha256": _h("fc11 synthetic roster"), "locator": {"row": 7},
              "event_id": "fc11-training-1", "event_at": CASES["search_event_at"],
              "record_type": "training-roster",
              "fields": {"user_id": "fc11-synthetic-user", "training_completed_at": completed_at}}
    rules = {"version": "fc11-synthetic-pack-v1", "entries": [_rule("search-before-training", {})]}
    units = [search, roster]
    return units, _joins(units), rules, {"as_of": "2020-01-20"}


def run(test, detector, values):
    """Evaluate through the real API; assert unchanged inputs and byte-identical repeatability."""
    before = copy.deepcopy(values)
    first = core.evaluate(detector, *values)
    test.assertEqual(values, before, "evaluate must not mutate caller inputs")
    second = core.evaluate(detector, *values)
    test.assertEqual(core.canonical(first), core.canonical(second), "repeatable output")
    return first


def target_outcome(test, result, event_id):
    kind = {"retention-over-policy": "retention-setting",
            "search-before-training": "search-log"}[result["manifest"]["detector"]]
    key = core.digest(core.canonical([AGENCY, kind, event_id]))
    rows = [row for row in result["manifest"]["outcomes"] if row["event_key"] == key]
    test.assertEqual(len(rows), 1, "exactly one target outcome")
    return rows[0]


def assert_blocked(test, result, event_id, reason, marker=""):
    outcome = target_outcome(test, result, event_id)
    test.assertEqual((outcome["disposition"], outcome["reason"]), ("blocked", reason),
                     marker + ": target must be blocked with " + reason)
    test.assertEqual(result["hits"], [], marker + ": no hit may be emitted for a blocked target")
    counts = result["manifest"]["counts"]
    test.assertEqual((counts["evaluated"], counts["blocked"], counts["hits"]), (0, 1, 0),
                     marker + ": not an evaluated or apparently clear target")


class MomentOffsetTests(unittest.TestCase):
    def test_valid_offsets_preserve_instants(self):
        for case in CASES["valid_offsets"]:
            with self.subTest(case=case["id"]):
                value = WALLS["event"] + case["suffix"]
                expected, offset = _expected(WALLS["event"], case)
                parsed = core.moment(value)
                baseline = datetime.fromisoformat(value.replace("Z", "+00:00"))
                self.assertEqual(parsed.utcoffset(), offset)
                self.assertEqual(parsed, expected)
                self.assertEqual((parsed, parsed.utcoffset()), (baseline, baseline.utcoffset()),
                                 "same instant and offset as the baseline parser")
                self.assertEqual(parsed.replace(tzinfo=None).isoformat(), WALLS["event"])
                self.assertEqual(parsed.date(), date(2020, 1, 20), "event wall date preserved")
                EXERCISED.add(case["id"])

    def test_invalid_offsets_blocked(self):
        for case in CASES["invalid_offsets"]:
            with self.subTest(case=case["id"]):
                value = WALLS["event"] + case["suffix"]
                with self.assertRaises(core.Blocked) as caught:
                    core.moment(value)
                self.assertEqual(str(caught.exception), "event_time_invalid")
                EXERCISED.add(case["id"])

    def test_nc_target_plus_00_99_blocked(self):
        case = _case("invalid_offsets", CASES["negative_control"]["target_case"])
        value = WALLS["event"] + case["suffix"]
        try:
            parsed = core.moment(value)
        except core.Blocked as exc:
            self.assertEqual(str(exc), "event_time_invalid", NC_MARKER + ": exact reason")
        else:
            self.fail(f"{NC_MARKER}: moment() normalized {case['suffix']} to {parsed.utcoffset()}")

    def test_missing_timezone_and_malformed_reasons(self):
        for case in CASES["missing_timezone"] + CASES["malformed"]:
            with self.subTest(case=case["id"]):
                with self.assertRaises(core.Blocked) as caught:
                    core.moment(case["value"])
                self.assertEqual(str(caught.exception), case["reason"])
                EXERCISED.add(case["id"])


class RetentionEvaluateTests(unittest.TestCase):
    def test_valid_offsets_evaluated_hit(self):
        for case in CASES["valid_offsets"]:
            with self.subTest(case=case["id"]):
                result = run(self, "retention-over-policy", retention_inputs(WALLS["event"] + case["suffix"]))
                outcome = target_outcome(self, result, "fc11-retention-1")
                self.assertEqual((outcome["disposition"], outcome["reason"]), ("evaluated", SEARCH_REASON))
                self.assertEqual(len(result["hits"]), 1)
                self.assertEqual(result["hits"][0]["observed_values_private"],
                                 {"setting_days": 31, "policy_days": 30})
                self.assertEqual(result["manifest"]["counts"]["blocked"], 0)

    def test_invalid_offsets_blocked_no_hit(self):
        for case in CASES["invalid_offsets"]:
            with self.subTest(case=case["id"]):
                result = run(self, "retention-over-policy", retention_inputs(WALLS["event"] + case["suffix"]))
                assert_blocked(self, result, "fc11-retention-1", "event_time_invalid")

    def test_nc_target_plus_00_99_blocked_no_hit(self):
        case = _case("invalid_offsets", CASES["negative_control"]["target_case"])
        result = run(self, "retention-over-policy", retention_inputs(WALLS["event"] + case["suffix"]))
        assert_blocked(self, result, "fc11-retention-1", "event_time_invalid", NC_MARKER)

    def test_nc_partner_plus_00_00_evaluated(self):
        case = _case("valid_offsets", CASES["negative_control"]["valid_partner"])
        result = run(self, "retention-over-policy", retention_inputs(WALLS["event"] + case["suffix"]))
        outcome = target_outcome(self, result, "fc11-retention-1")
        self.assertEqual(outcome["disposition"], "evaluated")
        self.assertEqual(len(result["hits"]), 1)

    def test_missing_and_malformed_blocked(self):
        for case in CASES["missing_timezone"] + CASES["malformed"]:
            with self.subTest(case=case["id"]):
                result = run(self, "retention-over-policy", retention_inputs(case["value"]))
                assert_blocked(self, result, "fc11-retention-1", case["reason"])

    def test_wall_date_rule_selection(self):
        for case in CASES["wall_date_rules"]:
            with self.subTest(case=case["id"]):
                result = run(self, "retention-over-policy", retention_inputs(case["event_at"]))
                outcome = target_outcome(self, result, "fc11-retention-1")
                self.assertEqual((outcome["disposition"], outcome["reason"]), (case["disposition"], case["reason"]))
                self.assertEqual(len(result["hits"]), case["hits"])
                EXERCISED.add(case["id"])


class TrainingEvaluateTests(unittest.TestCase):
    def test_valid_training_offsets(self):
        event = datetime.fromisoformat(CASES["search_event_at"])
        for case in CASES["valid_offsets"]:
            for wall_key, hits in (("training_after", 1), ("training_before", 0)):
                with self.subTest(case=case["id"], wall=wall_key):
                    expected, _ = _expected(WALLS[wall_key], case)
                    self.assertEqual(expected > event, bool(hits), "frozen walls bracket the search")
                    result = run(self, "search-before-training", training_inputs(WALLS[wall_key] + case["suffix"]))
                    outcome = target_outcome(self, result, "fc11-search-1")
                    self.assertEqual((outcome["disposition"], outcome["reason"]), ("evaluated", SEARCH_REASON))
                    self.assertEqual(len(result["hits"]), hits)
                    if hits:
                        values = result["hits"][0]["observed_values_private"]
                        self.assertEqual(values["earliest_supplied_completion"], expected.isoformat())

    def test_invalid_training_offsets_blocked(self):
        for case in CASES["invalid_offsets"]:
            for wall_key in ("training_after", "training_before"):
                with self.subTest(case=case["id"], wall=wall_key):
                    result = run(self, "search-before-training", training_inputs(WALLS[wall_key] + case["suffix"]))
                    assert_blocked(self, result, "fc11-search-1", "event_time_invalid")

    def test_missing_and_malformed_training_blocked(self):
        for case in CASES["missing_timezone"] + CASES["malformed"]:
            if not isinstance(case["value"], str) or not case["value"]:
                continue  # non-string or blank completion is a field-state refusal, not moment()
            with self.subTest(case=case["id"]):
                result = run(self, "search-before-training", training_inputs(case["value"]))
                assert_blocked(self, result, "fc11-search-1", case["reason"])


def _run_named(class_name, method):
    result = unittest.TestResult()
    unittest.TestSuite([globals()[class_name](method)]).run(result)
    return result


class SyntheticNegativeControlTests(unittest.TestCase):
    """Declared control: patch out only the new lexical offset check; datetime parsing stays."""

    def test_nc_lexical_check_removed_breaks_blocked_assertion(self):
        original = core._offset_components
        calls = []

        def removed(text):  # injected: the lexical component check does nothing
            calls.append(text)

        targets = [("MomentOffsetTests", "test_nc_target_plus_00_99_blocked"),
                   ("RetentionEvaluateTests", "test_nc_target_plus_00_99_blocked_no_hit")]
        with patch.object(core, "_offset_components", removed):
            killed = [_run_named(*target) for target in targets]
            partner = _run_named("RetentionEvaluateTests", "test_nc_partner_plus_00_00_evaluated")
        self.assertIs(core._offset_components, original, "patch restored")
        target_suffix = _case("invalid_offsets", CASES["negative_control"]["target_case"])["suffix"]
        self.assertTrue(any(text.endswith(target_suffix) for text in calls), "the removed seam was reached")
        for result in killed:
            self.assertEqual(result.errors, [], "an arbitrary error is not a killed mutation")
            self.assertEqual((result.testsRun, len(result.failures)), (1, 1), "injected defect detected")
            self.assertIn(NC_MARKER, result.failures[0][1])
        self.assertTrue(partner.wasSuccessful(), "ordinary +00:00 stays valid with datetime parsing retained")
        for target in targets:
            rerun = _run_named(*target)
            self.assertTrue(rerun.wasSuccessful(), rerun.failures + rerun.errors)
        for name in VALID_CONTROLS:
            control = _run_named(*name)
            self.assertTrue(control.wasSuccessful(), (name, control.failures + control.errors))


def fixture_case_ids():
    groups = ("valid_offsets", "invalid_offsets", "missing_timezone", "malformed", "wall_date_rules")
    return sorted(case["id"] for group in groups for case in CASES[group])


def case_dimensions():
    out = {}
    for group in ("valid_offsets", "invalid_offsets"):
        for case in CASES[group]:
            key = group + ":" + case["spelling"] + (":" + case["component"] + "-" + str(case["value"])
                                                     if "component" in case else "")
            out[key] = out.get(key, 0) + 1
    for group in ("missing_timezone", "malformed", "wall_date_rules"):
        out[group] = len(CASES[group])
    return dict(sorted(out.items()))


if __name__ == "__main__":
    _program = unittest.main(argv=_argv, exit=False, verbosity=2)
    _GUARD_FACTS["active_after_run"] = _guards_active()
    _GUARD_FACTS["trips"] = list(_GUARD_TRIPS)
    for _owner, _name, _original in _GUARD_ORIGINALS:
        setattr(_owner, _name, _original)
    _GUARD_FACTS["restored"] = all(getattr(owner, name) is original for owner, name, original in _GUARD_ORIGINALS)
    if _COVERAGE_OUT is not None:
        _COVERAGE_OUT.write_text(json.dumps({
            "tests_run": _program.result.testsRun,
            "failures": len(_program.result.failures), "errors": len(_program.result.errors),
            "skipped": len(_program.result.skipped),
            "exercised_case_ids": sorted(EXERCISED), "fixture_case_ids": fixture_case_ids(),
            "case_dimensions": case_dimensions(), "offline_guard": _GUARD_FACTS},
            indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.exit(0 if _program.result.wasSuccessful() and not _GUARD_TRIPS else 1)
