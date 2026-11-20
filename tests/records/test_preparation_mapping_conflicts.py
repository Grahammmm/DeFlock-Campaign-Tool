"""Synthetic regressions for the host preparation mapping boundary."""
from copy import deepcopy
import unittest

from campaign_tool.records.detectors.core import digest
from campaign_tool.records.detectors.pipeline import evaluate_structured
from campaign_tool.records.detectors.preparation import Preparation, prepare
from tests.records.test_detector_preparation import SyntheticProvider, fixture


def repeated_mapping(*, conflict):
    snapshot, inputs = fixture()
    units = inputs.normalized["units"]
    repeat = deepcopy(units[0])
    record = repeat["record"]
    if conflict:
        # Keep the verified source and event identity/date, but change the record.
        record["fields"]["purpose"] = "synthetic case follow-up"
    else:
        # An exact canonical repeat need not have the same insertion order.
        repeat["record"] = dict(reversed(record.items()))
    units.append(repeat)
    payload = digest(inputs.normalized)
    inputs.descriptors["normalization"]["payload_sha256"] = payload
    dates = inputs.descriptors["dates"]
    dates["normalization_sha256"] = payload
    dates["events_sha256"] = digest({
        repeat["source"]["unit_id"]: [
            {key: record[key] for key in ("event_id", "event_at")} for _ in range(2)
        ],
    })
    return prepare(snapshot, provider=SyntheticProvider(inputs))


class PreparationMappingConflictTests(unittest.TestCase):
    def test_distinct_records_for_one_source_block_preparation(self):
        # Counting only covered unit IDs used to let this reach the evaluator.
        self.assertEqual(repeated_mapping(conflict=True),
                         Preparation("blocked", "normalized_source_record_conflict"))

    def test_canonical_repeat_prepares_and_counts_as_duplicate(self):
        # Rejecting or removing all repeats would lose downstream duplicate accounting.
        result = repeated_mapping(conflict=False)
        self.assertEqual(result.status, "prepared", result.reason)
        entry = evaluate_structured(**result.prepared.kwargs)["structured"]["purpose-quality"]
        self.assertEqual(entry["status"], "evaluated", entry["reason"])
        self.assertEqual(entry["manifest"]["counts"]["duplicate_rows"], 1)
