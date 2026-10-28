"""Pure synthetic FC9 identity regression; safe to import under discovery."""
from copy import deepcopy
import ast
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "fc9-request-identity" / "cases.json"
# Script bootstrap only; ordinary discovery imports do not alter sys.path/tempdir.
if __name__ == "__main__":
    if "--temporary-root" in sys.argv:
        position = sys.argv.index("--temporary-root")
        temporary_root = Path(sys.argv[position + 1]).resolve(strict=True)
        tempfile.tempdir = str(temporary_root)
        del sys.argv[position:position + 2]
    sys.path.insert(0, str(REPO))


def fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def tracker():
    # Lazy import permits the runner to establish its temporary root first.
    from campaign_tool.records.understanding import correspondence
    return correspondence


def events_for(cases):
    events = []
    for case in cases:
        label = case["label"]
        request = dict(agency_id=case["agency"], request_number=case["number"],
                       event_type="request", occurred_at="2026-10-01T12:00:00Z",
                       message_id=case.get("message", label + "|request"),
                       occurrence_id=label + ":request", items_requested=[label + "-done", label + "-missing"])
        duplicate = dict(request, occurrence_id=label + ":duplicate")
        delivery = dict(agency_id=case["agency"], request_number=case["number"],
                        event_type="partial_delivery", occurred_at="2026-10-02T12:00:00Z",
                        message_id=label + "|delivery", occurrence_id=label + ":delivery",
                        items_delivered=[label + "-done"], due_at="2026-10-10T12:00:00Z",
                        due_kind="promised_delivery")
        events.extend([request, duplicate, delivery])
    return events


def run_cases(cases, reverse=False):
    events = events_for(cases)
    return tracker().track(list(reversed(events)) if reverse else events, as_of=fixture()["as_of"])


class IdentityTests(unittest.TestCase):
    def measured(self):
        return fixture()["cases"][:2]

    def assert_count(self, result):
        self.assertEqual(len(result["requests"]), 2, "measured request count")

    def assert_ownership(self, result):
        owners = {(r["agency_id"].casefold(), r["request_number"].casefold()): r["requested_items"]
                  for r in result["requests"]}
        expected = {(c["agency"].casefold(), c["number"].casefold()):
                    [c["label"] + "-done", c["label"] + "-missing"] for c in self.measured()}
        self.assertEqual(owners, expected, "measured item ownership")

    def test_measured_request_count(self):
        for reverse in (False, True):
            self.assert_count(run_cases(self.measured(), reverse))

    def test_measured_item_ownership(self):
        for reverse in (False, True):
            self.assert_ownership(run_cases(self.measured(), reverse))

    def test_frozen_encoding_all_fixture_cases(self):
        for case in fixture()["cases"]:
            with self.subTest(case=case["label"]):
                self.assertEqual(tracker()._id(case["agency"], case["number"]), case["expected_id"])

    def test_all_ownership_timeline_occurrences_due_actions_and_drafts_both_orders(self):
        cases = fixture()["cases"]
        for reverse in (False, True):
            result = run_cases(cases, reverse)
            self.assertEqual(result["holds"], [])
            self.assertFalse(result["review_acceptance"])
            self.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")
            self.assertEqual(len(result["requests"]), len(cases))
            self.assertEqual(len(result["due_actions"]), len(cases))
            rows = {row["request_id"]: row for row in result["requests"]}
            for case in cases:
                with self.subTest(case=case["label"], reverse=reverse):
                    row = rows[case["expected_id"]]
                    label = case["label"]
                    self.assertEqual((row["agency_id"], row["request_number"]), (case["agency"], case["number"]))
                    self.assertEqual(row["requested_items"], [label + "-done", label + "-missing"])
                    self.assertEqual(row["delivered_items"], [label + "-done"])
                    self.assertEqual(row["outstanding_items"], [label + "-missing"])
                    self.assertEqual(row["status"], "partially_delivered")
                    self.assertEqual(row["occurrence_count"], 3)
                    self.assertEqual([r["event_type"] for r in row["timeline"]], ["request", "partial_delivery"])
                    self.assertEqual([r["occurrence_count"] for r in row["timeline"]], [2, 1])
                    occurrences = {o["occurrence_id"] for r in row["timeline"] for o in r["occurrences"]}
                    self.assertEqual(occurrences, {label + ":request", label + ":duplicate", label + ":delivery"})
                    action, = [a for a in result["due_actions"] if a["request_id"] == row["request_id"]]
                    self.assertEqual(action["due_at"], "2026-10-10T12:00:00Z")
                    self.assertEqual(action["status"], "overdue")
                    self.assertEqual(action["basis"]["event_deduplication_key"], "message:" + label + "|delivery")
                    self.assertTrue(action["draft_only"])
                    draft = row["private_followup_draft"]
                    self.assertEqual(draft["request_id"], row["request_id"])
                    self.assertEqual(draft["text"], "Please confirm the status of these requested item categories: " + label + "-missing.")
                    self.assertFalse(draft["send"])
                    self.assertTrue(draft["requires_human_review"])

    def test_collision_free_stable_replay_and_reordering(self):
        cases = fixture()["cases"]
        first = run_cases(cases)
        self.assertEqual(first, run_cases(cases))
        self.assertEqual(first, run_cases(cases, True))
        ids = [r["request_id"] for r in first["requests"]]
        self.assertEqual(len(set(ids)), len(cases))
        self.assertEqual(set(ids), {c["expected_id"] for c in cases})

    def test_case_whitespace_and_unicode_variants_still_group(self):
        for agency, number, variant_agency, variant_number in (
                ("Agency A", "REQ 1", "  aGeNcY\t A  ", " req\n1 "),
                ("Straße", "RÉF", " STRASSE ", "réf")):
            first = dict(agency_id=agency, request_number=number, message_id="variant|1",
                         occurrence_id="one", occurred_at="2026-10-01T12:00:00Z", event_type="request")
            second = dict(first, agency_id=variant_agency, request_number=variant_number, occurrence_id="two")
            for events in ([first, second], [second, first]):
                result = tracker().track(events, as_of=fixture()["as_of"])
                self.assertEqual(result["holds"], [])
                row, = result["requests"]
                self.assertEqual(row["occurrence_count"], 2)
                self.assertEqual(len(row["timeline"]), 1)
                self.assertEqual(row["request_id"], tracker()._id(agency, number))
                self.assertEqual(row["agency_id"], tracker()._s(events[0]["agency_id"]))
                self.assertEqual(row["request_number"], tracker()._s(events[0]["request_number"]))

    def test_pipe_group_key_separation(self):
        left, right = [c for c in fixture()["cases"] if c["label"].startswith("pipe-")]
        # The legacy raw-ID + pipe key aliases these otherwise valid messages.
        # The frozen correction uses encoded IDs AND a tuple internally.
        left_id = left["agency"].casefold() + "::" + left["number"].casefold()
        right_id = right["agency"].casefold() + "::" + right["number"].casefold()
        self.assertEqual(left_id + "|message:" + left["message"], right_id + "|message:" + right["message"])
        self.assertNotEqual((left_id, "message:" + left["message"]), (right_id, "message:" + right["message"]))
        assignments = [node for node in ast.walk(ast.parse(inspect.getsource(tracker().track)))
                       if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "group_key" for target in node.targets)]
        self.assertEqual(len(assignments), 1)
        value = assignments[0].value
        if not isinstance(value, ast.Tuple):
            self.fail("group_key must use the frozen tuple construction")
        self.assertEqual([node.id for node in value.elts if isinstance(node, ast.Name)], ["request_id", "event_key"])
        for reverse in (False, True):
            result = run_cases([left, right], reverse)
            self.assertEqual(result["holds"], [])
            self.assertEqual(len(result["requests"]), 2)
            self.assertEqual([r["occurrence_count"] for r in result["requests"]], [3, 3])

    def test_old_identity_negative_control_count_and_ownership_then_restore(self):
        module = tracker()
        positive = run_cases(self.measured())
        self.assert_count(positive)
        self.assert_ownership(positive)
        with patch.object(module, "_id", lambda agency, number: agency.casefold() + "::" + number.casefold()):
            observed = run_cases(self.measured())
            self.assertEqual(observed["holds"], [])
            self.assertEqual(len(observed["requests"]), 1)
            with self.assertRaisesRegex(AssertionError, "measured request count"):
                self.assert_count(observed)
            with self.assertRaisesRegex(AssertionError, "measured item ownership"):
                self.assert_ownership(observed)
            ordinary = [c for c in fixture()["cases"] if c["label"].startswith("ordinary-")]
            self.assertEqual(run_cases(ordinary), self._ordinary_positive(ordinary, module))
        self.assertEqual(run_cases(self.measured()), positive)
        print("FC9_CONTROL " + json.dumps({"injected_behavior": "old _id delimiter concatenation", "actual_baseline_request_count": len(observed["requests"]), "actual_baseline_requested_items": observed["requests"][0]["requested_items"], "killed_assertions": ["measured request count", "measured item ownership"], "final_candidate_request_count": len(positive["requests"])}, sort_keys=True))

    def _ordinary_positive(self, cases, module):
        # Frozen expected IDs in the fixture are independent of the encoder.
        with patch.object(module, "_id", lambda agency, number: next(c["expected_id"] for c in cases if c["agency"] == agency and c["number"] == number)):
            return run_cases(cases)

    def test_empty_and_malformed_identities_preserve_holds(self):
        self.assertEqual(tracker().track([], as_of=fixture()["as_of"])["requests"], [])
        inputs = [None, {}, {"agency_id": None, "request_number": "r"},
                  {"agency_id": "a", "request_number": 12},
                  {"agency_id": "a", "request_number": " "},
                  {"agency_id": "a", "request_numbers": []},
                  {"agency_id": "a", "request_numbers": ["x", "y"]}]
        before = deepcopy(inputs)
        result = tracker().track(inputs, as_of=fixture()["as_of"])
        self.assertEqual(inputs, before)
        self.assertEqual(result["requests"], [])
        self.assertEqual([h["reason"] for h in result["holds"]], ["event_shape_invalid"] + ["agency_or_request_number_missing"] * 4 + ["request_number_missing", "request_number_ambiguous"])

    def test_ordinary_identifiers_unchanged(self):
        ordinary = [c for c in fixture()["cases"] if c["label"].startswith("ordinary-")]
        candidate = run_cases(ordinary)
        with patch.object(tracker(), "_id", lambda agency, number: agency.casefold() + "::" + number.casefold()):
            self.assertEqual(run_cases(ordinary), candidate)


if __name__ == "__main__":
    unittest.main(verbosity=2)
