"""Synthetic-only seven-detector fixtures. No corpus, IO, models or network."""
import copy
import hashlib
import importlib
import unittest

from campaign_tool.records.detectors import DETECTORS, evaluate, run_all
from campaign_tool.records.detectors.core import canonical, digest


def h(value):
    return hashlib.sha256(value.encode()).hexdigest()


def fixture(detector):
    original = h("synthetic original " + detector)
    unit = {"original_sha256": original, "locator": {"row": 1}, "event_id": "event-1",
            "event_at": "2020-01-20T12:00:00+00:00", "record_type": "search-log",
            "fields": {"user_id": "synthetic-user", "purpose": "case follow-up with supplied context"}}
    params = {}
    if detector == "purpose-quality":
        unit["fields"]["purpose"] = "investigation"
        params = {"generic_reasons": ["investigation", "inv", "test"]}
    elif detector == "external-sharing":
        unit["record_type"] = "sharing-list"
        unit["fields"] = {"access_enabled": True, "recipient_level": "federal", "recipient_jurisdiction": "ZZ"}
        params = {"home_jurisdiction": "AA"}
    elif detector == "retention-over-policy":
        unit["record_type"] = "retention-setting"
        unit["fields"] = {"retention_mode": "days", "retention_days": 31}
        params = {"maximum_days": 30, "unit": "calendar_days"}
    elif detector == "audit-gap":
        unit["record_type"] = "audit"
        unit["fields"] = {"period_start": "2020-01-01", "period_end": "2020-01-20",
                          "production_complete": True, "audit_dates": []}
        params = {"frequency_days": 7, "first_due": "2020-01-07"}
    elif detector == "cpra-deadline":
        unit["record_type"] = "request"
        unit["event_at"] = "2020-01-01T12:00:00+00:00"
        unit["fields"] = {"request_received": "2020-01-01", "determination_state": "pending",
                          "extension_claimed": False, "production_at": "2020-01-02"}
        table = {"version": "synthetic-holidays-v1", "coverage_start": "2020-01-01",
                 "coverage_end": "2020-12-31", "dates": []}
        params = {"counting": "calendar", "exclude_received_day": True, "determination_days": 10,
                  "extension_days_max": 14, "reminder_days": 3, "weekend_days": [5, 6],
                  "roll_due": "next_business_day", "holiday_table_sha256": digest(table),
                  "extension_allowed_bases": ["synthetic-basis"], "extension_notice_deadline": "base_due",
                  "extension_calculation": "from_base_due"}
    elif detector == "volume-anomaly":
        unit["event_at"] = "2020-01-20T02:00:00+00:00"
        params = {"timezone_offset_minutes": 0, "daily_count_threshold": 100,
                  "off_hours_threshold": 0, "business_hours": [9, 17]}
    rule = {"detector": detector, "agency_id": "synthetic-agency", "rule_id": "synthetic-rule",
            "version": "synthetic-v1", "source_sha256": h("synthetic policy"),
            "effective_from": "2020-01-01", "effective_to": "2021-01-01", "parameters": params}
    units = [unit]
    if detector == "search-before-training":
        training = copy.deepcopy(unit)
        training.update(original_sha256=h("synthetic roster"), event_id="training-1",
                        record_type="training-roster", locator={"row": 9})
        training["fields"] = {"user_id": "synthetic-user", "training_completed_at": "2020-01-21T12:00:00+00:00"}
        units.append(training)
    joins = [{"original_sha256": item["original_sha256"], "status": "typed", "agency_id": "synthetic-agency"}
             for item in units]
    rules = {"version": "synthetic-pack-v1", "entries": [rule]}
    if detector == "cpra-deadline":
        rules["holiday_tables"] = {digest(table): table}
    return units, joins, rules, {"as_of": "2020-01-20"}


def negative(detector, values):
    units, joins, rules, config = values
    if detector == "search-before-training":
        units[1]["fields"]["training_completed_at"] = "2020-01-19T12:00:00+00:00"
    elif detector == "purpose-quality":
        units[0]["fields"]["purpose"] = "specific documented incident follow-up"
    elif detector == "external-sharing":
        units[0]["fields"].update(recipient_level="local", recipient_jurisdiction="AA")
    elif detector == "retention-over-policy":
        units[0]["fields"]["retention_days"] = 30
    elif detector == "audit-gap":
        units[0]["fields"]["audit_dates"] = ["2020-01-07", "2020-01-14"]
    elif detector == "cpra-deadline":
        units[0]["fields"].update(determination_state="made", determination_at="2020-01-10")
    else:
        units[0]["event_at"] = "2020-01-20T12:00:00+00:00"
    return values


class DetectorTests(unittest.TestCase):
    def test_purpose_strict_thresholds(self):
        for total, expected in ((4, 2), (20, 0)):
            values = fixture("purpose-quality")
            first = values[0][0]
            for n in range(1, total):
                other = copy.deepcopy(first)
                other.update(event_id="event-" + str(n + 1), locator={"row": n + 1})
                other["fields"]["purpose"] = "specific documented context"
                values[0].append(other)
            result = evaluate("purpose-quality", *values)
            self.assertEqual({hit["severity"] for hit in result["hits"]}, {expected} if expected else set())

    def test_purpose_blank_redacted_missing_not_numerator(self):
        for state in ("blank", "redacted", "missing"):
            values = fixture("purpose-quality")
            values[0][0]["fields"]["purpose"] = {"state": state}
            result = evaluate("purpose-quality", *values)
            self.assertEqual(result["hits"], [])
            self.assertEqual(result["manifest"]["counts"]["blocked"], 1)
            self.assertIn(state, result["manifest"]["outcomes"][0]["reason"])

    def test_conflicting_duplicate_events_block(self):
        values = fixture("purpose-quality")
        other = copy.deepcopy(values[0][0])
        other["fields"]["purpose"] = "specific different context"
        values[0].append(other)
        result = evaluate("purpose-quality", *values)
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["manifest"]["outcomes"][0]["reason"], "conflicting_duplicate_event")

    def test_cpra_holiday_hash_must_match(self):
        values = fixture("cpra-deadline")
        next(iter(values[2]["holiday_tables"].values()))["dates"].append("2020-01-13")
        result = evaluate("cpra-deadline", *values)
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["manifest"]["outcomes"][0]["reason"], "holiday_table_hash_mismatch")

    def test_cpra_valid_extension_and_weekend_roll(self):
        values = fixture("cpra-deadline")
        values[0][0]["fields"].update(extension_claimed=True, extension_days=14,
                                      extension_notice_at="2020-01-10", extension_basis="synthetic-basis")
        result = evaluate("cpra-deadline", *values)
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["manifest"]["counts"]["evaluated"], 1)

    def test_cpra_bad_extension_is_blocked_not_legal_conclusion(self):
        values = fixture("cpra-deadline")
        values[0][0]["fields"].update(extension_claimed=True, extension_days=99,
                                      extension_notice_at="2020-01-10", extension_basis="synthetic-basis")
        result = evaluate("cpra-deadline", *values)
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["manifest"]["counts"]["blocked"], 1)

    def test_cpra_production_never_substitutes_for_determination(self):
        result = evaluate("cpra-deadline", *fixture("cpra-deadline"))
        self.assertEqual(result["hits"][0]["severity"], 2)
        self.assertTrue(result["hits"][0]["observed_values_private"]["production_is_not_determination"])

    def test_cpra_as_of_required_not_wall_clock(self):
        values = fixture("cpra-deadline")
        values[3].clear()
        result = evaluate("cpra-deadline", *values)
        self.assertEqual(result["manifest"]["counts"]["blocked"], 1)

    def test_cpra_reminder_uses_supplied_as_of(self):
        values = fixture("cpra-deadline")
        values[3]["as_of"] = "2020-01-10"
        result = evaluate("cpra-deadline", *values)
        self.assertEqual(result["hits"][0]["severity"], 1)
        self.assertEqual(result["hits"][0]["observed_values_private"]["determination_due"], "2020-01-13")

    def test_untyped_join_is_blocked(self):
        values = fixture("retention-over-policy")
        values[1][0]["status"] = "hinted"
        result = evaluate("retention-over-policy", *values)
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["manifest"]["counts"]["blocked"], 1)

    def test_input_not_mutated(self):
        values = fixture("search-before-training")
        before = canonical(values)
        result = evaluate("search-before-training", *values)
        result["hits"][0]["evidence"][0]["locator"]["row"] = 999
        self.assertEqual(canonical(values), before)

    def test_run_all_repeatable(self):
        values = fixture("purpose-quality")
        self.assertEqual(run_all(*values), run_all(*values))

    def test_limits_fail_before_processing(self):
        with self.assertRaises(ValueError):
            evaluate("purpose-quality", [{}] * 5001, [], {}, {})


class DetectorRepairTests(unittest.TestCase):
    def test_audit_previous_due_not_counted_twice(self):
        values = fixture("audit-gap")
        values[0][0]["fields"]["audit_dates"] = ["2020-01-07"]
        hit = evaluate("audit-gap", *values)["hits"][0]
        self.assertEqual(hit["severity"], 2)
        self.assertEqual(hit["observed_values_private"]["missing_documentation_due_dates"], ["2020-01-14"])

    def test_audit_clipped_start_is_inclusive(self):
        values = fixture("audit-gap")
        values[0][0]["fields"].update(period_start="2020-01-02", period_end="2020-01-08",
                                      audit_dates=["2020-01-02"])
        self.assertEqual(evaluate("audit-gap", *values)["hits"], [])

    def test_audit_first_window_start_at_previous_boundary(self):
        values = fixture("audit-gap")
        values[2]["entries"][0]["parameters"]["first_due"] = "2020-01-14"
        values[0][0]["fields"].update(period_start="2020-01-07", period_end="2020-01-15",
                                      audit_dates=["2020-01-07"])
        self.assertEqual(evaluate("audit-gap", *values)["hits"], [])

    def test_audit_start_on_due_cannot_cover_next_due(self):
        values = fixture("audit-gap")
        values[0][0]["fields"].update(period_start="2020-01-07", audit_dates=["2020-01-07"])
        hit = evaluate("audit-gap", *values)["hits"][0]
        self.assertEqual(hit["observed_values_private"]["missing_documentation_due_dates"], ["2020-01-14"])

    def test_changed_observation_changes_hit_identity(self):
        values = fixture("retention-over-policy")
        before = evaluate("retention-over-policy", *values)["hits"][0]
        values[0][0]["fields"]["retention_days"] = 32
        after = evaluate("retention-over-policy", *values)["hits"][0]
        self.assertNotEqual(before["dedupe_key"], after["dedupe_key"])
        self.assertNotEqual(before["semantic_observations_sha256"], after["semantic_observations_sha256"])

    def test_same_aggregate_counts_changed_purpose_changes_identity(self):
        values = fixture("purpose-quality")
        before = evaluate("purpose-quality", *values)["hits"]
        values[0][0]["fields"]["purpose"] = "test"
        after = evaluate("purpose-quality", *values)["hits"]
        self.assertEqual([x["observed_values_private"] for x in sorted(before, key=lambda x: str(x["observed_values_private"]))],
                         [x["observed_values_private"] for x in sorted(after, key=lambda x: str(x["observed_values_private"]))])
        self.assertTrue({x["dedupe_key"] for x in before}.isdisjoint(x["dedupe_key"] for x in after))

    def test_changed_rule_content_changes_identity_without_label_change(self):
        for key, value in (("effective_to", "2022-01-01"), ("source_sha256", h("different synthetic policy")),
                           ("parameters", {"maximum_days": 29, "unit": "calendar_days"})):
            with self.subTest(key=key):
                values = fixture("retention-over-policy")
                before = evaluate("retention-over-policy", *values)["hits"][0]
                values[2]["entries"][0][key] = value
                after = evaluate("retention-over-policy", *values)["hits"][0]
                self.assertNotEqual(before["dedupe_key"], after["dedupe_key"])
                self.assertNotEqual(before["rule_fingerprint_sha256"], after["rule_fingerprint_sha256"])

    def test_same_labeled_rule_intervals_do_not_mix_aggregate_contexts(self):
        for detector in ("purpose-quality", "volume-anomaly"):
            with self.subTest(detector=detector):
                values = fixture(detector)
                other = copy.deepcopy(values[0][0])
                other.update(event_id="event-2", event_at="2020-02-20T02:00:00+00:00", locator={"row": 2})
                values[0].append(other)
                first = values[2]["entries"][0]
                first["effective_to"] = "2020-02-01"
                second = copy.deepcopy(first)
                second.update(effective_from="2020-02-01", effective_to="2021-01-01")
                values[2]["entries"].append(second)
                hits = evaluate(detector, *values)["hits"]
                self.assertEqual(len(hits), 4 if detector == "purpose-quality" else 2)
                self.assertEqual(len({hit["rule_fingerprint_sha256"] for hit in hits}), 2)
                for hit in hits:
                    self.assertEqual(len(hit["evidence"]), 1)
                    count = "qualified_observations" if detector == "purpose-quality" else "unique_searches"
                    self.assertEqual(hit["observed_values_private"][count], 1)

    def test_duplicate_print_location_keeps_identity_and_retains_references(self):
        for detector in DETECTORS:
            with self.subTest(detector=detector):
                values = fixture(detector)
                before = evaluate(detector, *values)["hits"]
                other = copy.deepcopy(values[0][0])
                other["locator"] = {"row": 100}
                values[0].append(other)
                after = evaluate(detector, *values)["hits"]
                self.assertEqual([hit["dedupe_key"] for hit in before], [hit["dedupe_key"] for hit in after])
                self.assertTrue(all(any(ref["locator"] == {"row": 100} for ref in hit["evidence"]) for hit in after))

    def test_support_observation_changes_training_identity(self):
        values = fixture("search-before-training")
        before = evaluate("search-before-training", *values)["hits"][0]
        extra = copy.deepcopy(values[0][1])
        extra.update(event_id="training-2", locator={"row": 10})
        extra["fields"]["training_completed_at"] = "2020-01-22T12:00:00+00:00"
        values[0].append(extra)
        after = evaluate("search-before-training", *values)["hits"][0]
        self.assertEqual(before["observed_values_private"], after["observed_values_private"])
        self.assertNotEqual(before["dedupe_key"], after["dedupe_key"])

    def test_placeholder_locator_values_block(self):
        for locator in ({"page": None}, {"row": ""}, {"row": "  "}, {"row": True},
                        {"row": -1}, {"row": 1.5}, {"page": 1, "row": None}):
            with self.subTest(locator=locator):
                values = fixture("retention-over-policy")
                values[0][0]["locator"] = locator
                result = evaluate("retention-over-policy", *values)
                self.assertEqual(result["hits"], [])
                self.assertEqual(result["manifest"]["counts"]["blocked"], 1)
                self.assertEqual(result["manifest"]["outcomes"][0]["reason"], "exact_locator_value_invalid")

    def test_invalid_locator_paths_block(self):
        for path in ([], [None], [""], [1, []], [1, {}], [True], [0] * 33):
            with self.subTest(path=path):
                values = fixture("retention-over-policy")
                values[0][0]["locator"] = {"mime_path": path}
                result = evaluate("retention-over-policy", *values)
                self.assertEqual(result["hits"], [])
                self.assertEqual(result["manifest"]["outcomes"][0]["reason"], "exact_locator_path_invalid")

    def test_supported_locator_values_preserved_exactly(self):
        for locator in ({"page": 1}, {"sheet": "synthetic-sheet", "row": 2},
                        {"mime_traversal_index": 0}, {"mime_path": [0, 2, "body"]}):
            with self.subTest(locator=locator):
                values = fixture("retention-over-policy")
                values[0][0]["locator"] = locator
                self.assertEqual(evaluate("retention-over-policy", *values)["hits"][0]["locator"], locator)

    def test_locator_key_and_component_bounds_block(self):
        for locator in ({"": 1}, {" " : 1}, {"x" * 65: 1}, {"row": 2**63},
                        {"sheet": "x" * 1025}, {str(n): n for n in range(17)}, {"row": {"index": 1}}):
            with self.subTest(locator=locator):
                values = fixture("retention-over-policy")
                values[0][0]["locator"] = locator
                result = evaluate("retention-over-policy", *values)
                self.assertEqual(result["hits"], [])
                self.assertEqual(result["manifest"]["counts"]["blocked"], 1)

    def test_repaired_manifest_is_repeatable(self):
        values = fixture("audit-gap")
        values[0][0]["fields"]["audit_dates"] = ["2020-01-07"]
        before = evaluate("audit-gap", *values)
        self.assertEqual(before, evaluate("audit-gap", *values))
        self.assertEqual(before["manifest"]["detector_version"], "wp6-pure-detectors-v2")


def make_case(detector, case):
    def test(self):
        values = fixture(detector)
        if case == "negative":
            values = negative(detector, values)
        elif case == "redacted":
            key = {"search-before-training": "user_id", "purpose-quality": "purpose",
                   "external-sharing": "recipient_jurisdiction", "retention-over-policy": "retention_days",
                   "audit-gap": "audit_dates", "cpra-deadline": "determination_state",
                   "volume-anomaly": "user_id"}[detector]
            values[0][0]["fields"][key] = {"state": "redacted"}
        elif case == "policy_boundary":
            values[2]["entries"][0]["effective_from"] = "2021-01-01"
            values[2]["entries"][0]["effective_to"] = "2022-01-01"
        elif case == "duplicate":
            before = evaluate(detector, *values)
            values[0].append(copy.deepcopy(values[0][0]))
        result = evaluate(detector, *values)
        if case == "positive":
            self.assertTrue(result["hits"])
            for hit in result["hits"]:
                self.assertTrue(hit["evidence"])
                self.assertIn("original_sha256", hit)
                self.assertIn("locator", hit)
                self.assertEqual(hit["rules_version"], "synthetic-pack-v1")
                self.assertTrue(hit["triage_only"])
                if detector == "volume-anomaly":
                    self.assertEqual(hit["severity"], 1)
        elif case in ("negative", "redacted", "policy_boundary"):
            self.assertEqual(result["hits"], [])
            if case == "redacted":
                self.assertGreater(result["manifest"]["counts"]["blocked"], 0)
            if case == "policy_boundary":
                self.assertTrue(any(x["reason"] == "rule_not_effective" for x in result["manifest"]["outcomes"]))
        elif case == "duplicate":
            self.assertEqual([h["dedupe_key"] for h in before["hits"]],
                             [h["dedupe_key"] for h in result["hits"]])
            self.assertEqual(len(before["hits"]), len(result["hits"]))
            self.assertEqual(result["manifest"]["counts"]["duplicate_rows"], 1)
        else:
            self.assertEqual(result, evaluate(detector, *values))
            wrapper = importlib.import_module("campaign_tool.records.detectors." + detector.replace("-", "_"))
            self.assertEqual(wrapper.detect(*values), result["hits"])
        counts = result["manifest"]["counts"]
        self.assertEqual(counts["evaluated"] + counts["skipped"] + counts["blocked"], counts["unique_records"])
        self.assertEqual(result["manifest"]["stage_promotions"], 0)
    return test


for _detector in DETECTORS:
    for _case in ("positive", "negative", "redacted", "policy_boundary", "duplicate", "repeat"):
        setattr(DetectorTests, "test_" + _detector.replace("-", "_") + "_" + _case, make_case(_detector, _case))


if __name__ == "__main__":
    unittest.main()
