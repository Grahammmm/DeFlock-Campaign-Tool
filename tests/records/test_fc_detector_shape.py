"""FC-DETECTOR-SHAPE: malformed detector identity fields fail the batch closed.

Synthetic-only. No corpus, files, IO, models or network. Identity fields
(unit ``record_type`` / ``original_sha256`` and join ``original_sha256`` /
``agency_id``) holding JSON containers must raise ``core.Blocked`` with a
stable code before any dictionary lookup, grouping or set membership, instead
of escaping as an uncaught ``TypeError``. Valid, missing, null and scalar
shapes keep their existing per-record manifest outcomes.
"""
import copy
import unittest

from campaign_tool.records.detectors import DETECTORS, evaluate, run_all
from campaign_tool.records.detectors import core
from campaign_tool.records.detectors.core import canonical
from tests.records.test_detectors import fixture, negative

MARKER = "synthetic-shape-marker"
CONTAINERS = (
    ("empty_list", []),
    ("list", ["search-log"]),
    ("dict", {"value": "search-log"}),
    ("nested_list", [["search-log", MARKER]]),
    ("nested_dict", {"outer": {"inner": [MARKER]}}),
    ("marker_list", [MARKER]),
)


class ShapeAssertions(unittest.TestCase):
    def assert_blocked(self, code, detector, values):
        before = canonical(values)
        with self.assertRaises(core.Blocked) as caught:
            evaluate(detector, *values)
        error = caught.exception
        self.assertIsInstance(error, ValueError)
        self.assertEqual(str(error), code)
        self.assertEqual(error.args, (code,))
        self.assertNotIn(MARKER, repr(error))
        self.assertNotIn("search-log", repr(error))
        self.assertEqual(canonical(values), before)


class UnitRecordTypeShapeTests(ShapeAssertions):
    def test_container_record_type_blocks_every_detector(self):
        for detector in DETECTORS:
            for label, value in CONTAINERS:
                with self.subTest(detector=detector, shape=label):
                    values = fixture(detector)
                    values[0][0]["record_type"] = copy.deepcopy(value)
                    self.assert_blocked("unit_record_type_invalid", detector, values)

    def test_measured_reproducer_purpose_quality_empty_list(self):
        values = fixture("purpose-quality")
        values[0][0]["record_type"] = []
        self.assert_blocked("unit_record_type_invalid", "purpose-quality", values)

    def test_malformed_second_row_after_valid_first_row(self):
        for detector in DETECTORS:
            for label, value in CONTAINERS:
                with self.subTest(detector=detector, shape=label):
                    values = fixture(detector)
                    other = copy.deepcopy(values[0][0])
                    other.update(event_id="event-shape-2", locator={"row": 2},
                                 record_type=copy.deepcopy(value))
                    values[0].append(other)
                    self.assert_blocked("unit_record_type_invalid", detector, values)

    def test_malformed_supporting_training_row_blocks(self):
        values = fixture("search-before-training")
        values[0][1]["record_type"] = ["training-roster"]
        self.assert_blocked("unit_record_type_invalid", "search-before-training", values)

    def test_run_all_never_returns_partial_hits(self):
        values = fixture("purpose-quality")
        other = copy.deepcopy(values[0][0])
        other.update(event_id="event-shape-2", locator={"row": 2}, record_type={"x": [MARKER]})
        values[0].append(other)
        before = canonical(values)
        with self.assertRaises(core.Blocked) as caught:
            run_all(*values)
        self.assertEqual(str(caught.exception), "unit_record_type_invalid")
        self.assertEqual(canonical(values), before)


class UnitOriginalHashShapeTests(ShapeAssertions):
    def test_container_original_hash_blocks_every_detector(self):
        for detector in DETECTORS:
            for label, value in CONTAINERS:
                with self.subTest(detector=detector, shape=label):
                    values = fixture(detector)
                    values[0][0]["original_sha256"] = copy.deepcopy(value)
                    self.assert_blocked("unit_original_hash_invalid", detector, values)

    def test_container_wrapping_valid_hash_blocks(self):
        values = fixture("retention-over-policy")
        valid = values[0][0]["original_sha256"]
        for label, wrapped in (("list", [valid]), ("dict", {"sha256": valid}), ("nested_list", [[valid]])):
            with self.subTest(shape=label):
                values = fixture("retention-over-policy")
                values[0][0]["original_sha256"] = copy.deepcopy(wrapped)
                self.assert_blocked("unit_original_hash_invalid", "retention-over-policy", values)

    def test_malformed_second_row_hash_after_valid_first_row(self):
        for detector in DETECTORS:
            with self.subTest(detector=detector):
                values = fixture(detector)
                other = copy.deepcopy(values[0][0])
                other.update(event_id="event-shape-2", locator={"row": 2},
                             original_sha256=[values[0][0]["original_sha256"]])
                values[0].append(other)
                self.assert_blocked("unit_original_hash_invalid", detector, values)


class JoinIdentityShapeTests(ShapeAssertions):
    def test_container_join_identity_blocks(self):
        for detector in DETECTORS:
            for key in ("original_sha256", "agency_id"):
                for label, value in CONTAINERS:
                    with self.subTest(detector=detector, key=key, shape=label):
                        values = fixture(detector)
                        values[1][0][key] = copy.deepcopy(value)
                        self.assert_blocked("join_identity_invalid", detector, values)

    def test_malformed_join_after_valid_join(self):
        values = fixture("retention-over-policy")
        extra = copy.deepcopy(values[1][0])
        extra["agency_id"] = {"agency": MARKER}
        values[1].append(extra)
        self.assert_blocked("join_identity_invalid", "retention-over-policy", values)

    def test_untyped_join_with_container_identity_still_blocks(self):
        values = fixture("retention-over-policy")
        values[1][0].update(status="hinted", original_sha256=[MARKER])
        self.assert_blocked("join_identity_invalid", "retention-over-policy", values)


class PreservedShapeBehaviourTests(unittest.TestCase):
    def outcome(self, detector, values):
        result = evaluate(detector, *values)
        counts = result["manifest"]["counts"]
        self.assertEqual(counts["evaluated"] + counts["skipped"] + counts["blocked"], counts["unique_records"])
        return result

    def test_unknown_string_record_type_is_skipped(self):
        values = fixture("purpose-quality")
        values[0][0]["record_type"] = "synthetic-unknown-type"
        result = self.outcome("purpose-quality", values)
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["manifest"]["outcomes"][0]["disposition"], "skipped")
        self.assertEqual(result["manifest"]["outcomes"][0]["reason"], "not_detector_input_type")

    def test_null_and_missing_record_type_are_skipped(self):
        for mode in ("null", "missing"):
            with self.subTest(mode=mode):
                values = fixture("retention-over-policy")
                if mode == "null":
                    values[0][0]["record_type"] = None
                else:
                    del values[0][0]["record_type"]
                result = self.outcome("retention-over-policy", values)
                self.assertEqual(result["hits"], [])
                self.assertEqual(result["manifest"]["outcomes"][0]["reason"], "not_detector_input_type")

    def test_scalar_non_string_record_type_keeps_skip(self):
        for value in (7, 1.5, True):
            with self.subTest(value=value):
                values = fixture("retention-over-policy")
                values[0][0]["record_type"] = value
                result = self.outcome("retention-over-policy", values)
                self.assertEqual(result["manifest"]["outcomes"][0]["reason"], "not_detector_input_type")

    def test_missing_null_scalar_original_hash_keep_per_record_block(self):
        for mode, value in (("missing", None), ("null", None), ("int", 5), ("short", "abc"), ("upper", "A" * 64)):
            with self.subTest(mode=mode):
                values = fixture("retention-over-policy")
                if mode == "missing":
                    del values[0][0]["original_sha256"]
                else:
                    values[0][0]["original_sha256"] = value
                result = self.outcome("retention-over-policy", values)
                self.assertEqual(result["hits"], [])
                self.assertEqual(result["manifest"]["counts"]["blocked"], 1)
                self.assertEqual(result["manifest"]["outcomes"][0]["reason"], "original_hash_missing")

    def test_scalar_join_identity_keeps_existing_ignore(self):
        for key, value in (("original_sha256", 5), ("original_sha256", None), ("agency_id", 7), ("agency_id", None)):
            with self.subTest(key=key, value=value):
                values = fixture("retention-over-policy")
                values[1][0][key] = value
                result = self.outcome("retention-over-policy", values)
                self.assertEqual(result["hits"], [])
                self.assertEqual(result["manifest"]["outcomes"][0]["reason"],
                                 "typed_agency_join_missing_or_ambiguous")

    def test_non_object_join_rows_keep_existing_ignore(self):
        values = fixture("retention-over-policy")
        values[1].extend(["synthetic-row", 3, None, ["x"]])
        result = self.outcome("retention-over-policy", values)
        self.assertEqual(len(result["hits"]), 1)

    def test_typed_valid_units_seven_positives_and_negatives(self):
        for detector in DETECTORS:
            with self.subTest(detector=detector, case="positive"):
                result = self.outcome(detector, fixture(detector))
                self.assertTrue(result["hits"])
                self.assertEqual(result["manifest"]["counts"]["blocked"], 0)
            with self.subTest(detector=detector, case="negative"):
                result = self.outcome(detector, negative(detector, fixture(detector)))
                self.assertEqual(result["hits"], [])

    def test_inputs_not_mutated_on_success(self):
        for detector in DETECTORS:
            with self.subTest(detector=detector):
                values = fixture(detector)
                before = canonical(values)
                evaluate(detector, *values)
                self.assertEqual(canonical(values), before)

    def test_stable_codes_are_documented(self):
        for code in ("unit_record_type_invalid", "unit_original_hash_invalid", "join_identity_invalid"):
            self.assertIn(code, core.IDENTITY_SHAPE_CODES)
            self.assertIn(code, core.__doc__)


if __name__ == "__main__":
    unittest.main()
