"""Synthetic TSV coverage overrides at the follow-up batch boundary."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import followup_route
from tests.records.test_followup_route import DAY, ask, queue


class CoverageNumberBoundsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        run = self.root / "run-outputs" / "r1"
        outcomes = run / "outcomes"
        outcomes.mkdir(parents=True)
        requests = [{"agency_key": "alpha-pd", "request_id": rid, "events": [], "clocks": {}}
                    for rid in ("r1", "r2")]
        (run / "CURRENT-REQUEST-REGISTER.json").write_text(json.dumps({
            "schema": "records-request-register-v1", "as_of": DAY, "requests": requests,
        }), encoding="utf-8")
        (outcomes / "timeline.json").write_text(json.dumps({
            "schema": "records-timeline-v1", "run_id": "r1", "as_of": DAY, "requests": requests,
        }), encoding="utf-8")
        (outcomes / "response-items.json").write_text(json.dumps({
            "schema": "records-response-items-v1", "run_id": "r1", "requests": [
                dict(requests[0], asks=[ask(n, rating="withheld") for n in (1, 2, 10)]),
                dict(requests[1], asks=[ask(3, rating="withheld")]),
            ],
        }), encoding="utf-8")
        self.table = self.root / "followup-coverage" / "alpha-pd.tsv"
        self.table.parent.mkdir()
        self.enterContext(patch.object(followup_route.approval_queue, "load_queue", return_value=[
            queue(kind="appeal-denial"), dict(queue("q2", kind="appeal-denial"), request_id="r2"),
        ]))

    def route_batch(self, numbers):
        self.table.write_text("queue_id\task_numbers\tconfirmed_by\tnote\n"
                              f"q1\t{numbers}\tsynthetic-operator\t\n"
                              "q2\t3\tsynthetic-operator\t\n", encoding="utf-8")
        first, second = followup_route.route_run(self.root, "r1")
        # A refusal for the first request must not hide a later route in the same agency.
        self.assertEqual((first["request_id"], second["request_id"]), ("r1", "r2"))
        self.assertTrue(first["review_required"])
        self.assertEqual(second["handled_by"], "received_items")
        self.assertEqual(second["covered"], [self.coverage([3], "q2")])
        return first

    @staticmethod
    def coverage(numbers, queue_id="q1"):
        return {"pattern": "blanket_denial", "queue_id": queue_id, "items": numbers,
                "basis": "operator_table"}

    def assert_manual_refusal(self, row):
        self.assertEqual((row["handled_by"], row["reason"]), ("manual", "coverage_table_invalid"))
        self.assertIsNone(row["pattern"])
        self.assertIsNone(row["vehicle"])
        self.assertEqual((row["covered"], row["asks_for"], row["unreconciled"]), ([], [], []))

    def test_oversized_number_routes_to_manual_without_aborting_batch(self):
        # An unbounded int() conversion used to kill the batch instead of refusing one row.
        self.assert_manual_refusal(self.route_batch("9" * 5000))

    def test_invalid_lists_are_rejected_in_full(self):
        # Keeping only recognized/unique tokens would wrongly apply partial coverage.
        for label, value in (("oversized suffix", "1," + "9" * 5000),
                             ("duplicate", "1,10,1"), ("out of scope", "1,3"),
                             ("noncanonical", "1,02")):
            with self.subTest(label=label):
                self.assert_manual_refusal(self.route_batch(value))

    def test_valid_overrides_preserve_integer_coverage_and_residual_asks(self):
        # Use unsorted, multi-digit input to catch string-valued or overbroad coverage.
        for value, covered, residual in (("10,1", [1, 10], [2]), ("none", [], [1, 2, 10])):
            with self.subTest(value=value):
                row = self.route_batch(value)
                self.assertEqual((row["handled_by"], row["pattern"]), ("engine", "blanket_denial"))
                self.assertEqual(row["covered"], [self.coverage(covered)])
                self.assertEqual(row["asks_for"], residual)
