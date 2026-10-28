"""Fictional mutation regressions. Ordinary discovery has no ambient effects.

Script setup alone adjusts the module-derived import path and temporary root.
The acceptance runner supplies offline sentinels before any product import.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fc14-tracker-snapshot" / "cases.json"
CASES = ("event_citation_input", "event_citation_output", "policy_deadline_basis",
         "metadata_citations", "caller_unverified_mode", "two_calls", "holds_and_empty", "never_send")


class TrackerSnapshotTests(unittest.TestCase):
    def setUp(self):
        from campaign_tool.records.understanding import correspondence, evidence
        self.c, self.e = correspondence, evidence
        self.f = json.loads(FIXTURE.read_text())
        self.assertEqual(self.f["cases"], list(CASES))
        self.assertTrue(self.f["fictional"])

    def inputs(self, *, headers=None, text=None):
        text = self.f["promise"] if text is None else text
        receipt = {"schema": "format-extraction-v1", "original_sha256": self.e.sha(b"FICTIONAL FC14 ORIGINAL"),
                   "original_changed": False, "pages": [], "status": "extracted",
                   "units": [{"kind": "text", "locator": {"part": ["synthetic", 1]},
                              "text": text, "data": {"headers": headers or []}}]}
        bundle = self.e.normalize_receipt(receipt, receipt["original_sha256"], self.e.sha(receipt))
        citation = self.e.cite_text(bundle["units"][0], self.f["promise"])
        event = {"agency_id": self.f["agency_id"], "request_number": self.f["request_number"],
                 "event_type": "promise", "occurred_at": self.f["occurred_at"],
                 "due_at": self.f["due_values"][0], "due_kind": "promised_delivery",
                 "citations": [citation]}
        self.e.validate_citation(citation, bundle)
        return [event], bundle

    def track(self, events, bundle=None, rules=None):
        return self.c.track(events, as_of=self.f["as_of"], bundle=bundle, deadline_rules=rules)

    def valid(self, result):
        self.assertEqual(result["holds"], [])
        self.assertEqual(len(result["requests"]), 1)
        self.assertEqual(result["requests"][0]["request_id"], "fictional-office::syn-14")
        self.assertFalse(result["review_acceptance"])
        self.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")
        return result["requests"][0]["timeline"][0]

    def assert_input_snapshot(self, tracker=None):
        events, bundle = self.inputs()
        result = (tracker or self.track)(events, bundle)
        self.valid(result)
        before = deepcopy(result)
        events[0]["citations"][0]["quote"] = "MUTATED AFTER VALID TRACK"
        self.assertEqual(result["requests"][0]["timeline"][0]["citations"],
                         before["requests"][0]["timeline"][0]["citations"], "timeline_citation_snapshot")
        self.assertEqual(result, before, "complete_result_snapshot")

    def test_event_citation_input(self):
        for due, status in zip(self.f["due_values"], self.f["dimensions"]["event_citation_input"]):
            with self.subTest(due=due, status=status):
                events, bundle = self.inputs()
                events[0]["due_at"] = due
                result = self.track(events, bundle)
                row = self.valid(result)
                self.assertEqual(row["citations"], events[0]["citations"])
                self.assertEqual(result["due_actions"][0]["status"], status)
                before = deepcopy(result)
                events[0]["citations"][0]["quote"] = "CHANGED"
                events[0]["citations"][0]["span"]["start_byte"] = 99
                events[0]["citations"][0]["locator"]["part"].append("changed")
                events[0]["citations"].append({"new": []})
                self.assertEqual(result["requests"][0]["timeline"], before["requests"][0]["timeline"], "timeline_snapshot")
                self.assertEqual(result["due_actions"], before["due_actions"], "promised_delivery_basis_snapshot")
                self.assertEqual(result, before)

    def test_event_citation_output(self):
        for target in self.f["dimensions"]["event_citation_output"]:
            with self.subTest(target=target):
                events, bundle = self.inputs()
                before = deepcopy((events, bundle))
                result = self.track(events, bundle)
                row = self.valid(result)
                citations = row["citations"] if target == "timeline" else result["due_actions"][0]["basis"]["citations"]
                citations[0]["quote"] = "CHANGED OUTPUT"
                citations[0]["locator"]["part"].append("changed")
                citations.clear()
                self.assertEqual((events, bundle), before, "caller_and_bundle_unchanged")

    def rule(self, citation):
        return {"agency_id": self.f["agency_id"], "request_number": self.f["request_number"],
                "jurisdiction": "synthetic", "regulated_role": "records_office", "event_at": "2026-10-18",
                "version_id": "fictional-v1", "requirement_id": "fictional-r1", "basis": "Fictional deadline basis.",
                "determination_due_at": self.f["due_values"][0], "delivery_due_at": self.f["due_values"][1],
                "citations": [deepcopy(citation)], "policy_versions": [{
                    "version_id": "fictional-v1", "family_id": "fictional-family", "jurisdiction": "synthetic",
                    "agency_id": self.f["agency_id"], "regulated_roles": ["records_office"],
                    "effective_from": "2026-01-01", "effective_to": "2027-01-01",
                    "issued_at": "2026-01-01", "retrieved_at": "2026-10-19",
                    "source": {"source_id": "fictional", "url": "https://synthetic.invalid/policy",
                               "locator": "section 1", "verification_status": "synthetic"},
                    "requirements": [{"requirement_id": "fictional-r1", "description": "Fictional only",
                                      "parameters": {"days": [10, 20]}}]}]}

    def test_policy_deadline_basis(self):
        for direction in self.f["dimensions"]["policy_deadline_basis"]:
            with self.subTest(direction=direction):
                events, bundle = self.inputs()
                rule = self.rule(events[0]["citations"][0])
                original = deepcopy((events, bundle, rule))
                result = self.track(events, bundle, [rule])
                self.valid(result)
                actions = [a for a in result["due_actions"] if a.get("source") == "explicit_validated_policy_version"]
                self.assertEqual([a["deadline_kind"] for a in actions], ["determination", "delivery"])
                self.assertEqual([a["status"] for a in actions], ["overdue", "upcoming"])
                for action in actions:
                    self.assertEqual(action["basis"]["requirement"]["parameters"], {"days": [10, 20]})
                    self.assertEqual(action["basis"]["citations"], rule["citations"])
                before = deepcopy(result)
                if direction == "input":
                    rule["policy_versions"][0]["requirements"][0]["parameters"]["days"].append(999)
                    rule["citations"][0]["quote"] = "CHANGED POLICY INPUT"
                    rule["citations"][0]["locator"]["part"].append("input")
                    self.assertEqual(result, before, "policy_input_snapshot")
                else:
                    actions[0]["basis"]["requirement"]["parameters"]["days"].append(999)
                    actions[1]["basis"]["citations"][0]["locator"]["part"].append("output")
                    self.assertEqual((events, bundle, rule), original, "policy_output_detached")

    def test_metadata_citations(self):
        for mode in self.f["dimensions"]["metadata_citations"]:
            with self.subTest(mode=mode):
                headers = [["Date", self.f["header_date"]], ["Message-ID", self.f["header_id"]]]
                text = self.f["promise"]
                if mode == "text_header":
                    text += "\nDate: " + self.f["header_date"] + "\nMessage-ID: " + self.f["header_id"]
                _, bundle = self.inputs(headers=headers, text=text)
                events = self.c.events_from_bundle(bundle, self.f["agency_id"], [self.f["request_number"]])
                self.assertEqual(len(events), 1)
                original = deepcopy((events, bundle))
                first = self.track(events, bundle)
                row = self.valid(first)
                self.assertEqual(row["message_date"], self.f["occurred_at"])
                self.assertEqual(row["time_source"], "message_date")
                self.assertIsNone(row["occurrences"][0]["occurred_at"])
                self.assertNotIn("agency_received_at", row)
                self.assertEqual(row["deduplication_key"], "message:" + self.f["header_id"])
                for key in ("message_date_citation", "message_id_citation"):
                    self.assertEqual(row[key], events[0][key])
                    self.e.grounded_evidence.validate_support(row[key], bundle)
                before = deepcopy(first)
                events[0]["message_date_citation"]["quote"] = "CHANGED DATE"
                events[0]["message_id_citation"]["span"]["end_byte"] = 999
                bundle["units"][0]["locator"]["part"].append("changed bundle")
                self.assertEqual(first, before, "metadata_input_snapshot")
                events, bundle = deepcopy(original)
                second = self.track(events, bundle)
                second_row = self.valid(second)
                second_row["message_date_citation"]["quote"] = "CHANGED OUTPUT"
                second_row["message_id_citation"]["locator"]["part"].append("output")
                self.assertEqual((events, bundle), original, "metadata_output_detached")

    def test_caller_unverified_mode(self):
        events, _ = self.inputs()
        events[0]["message_id"] = self.f["header_id"]
        events[0]["citations"] = [{"unchecked": {"values": [None, True, 7, "α"]}}]
        before_input = deepcopy(events)
        result = self.track(events)
        row = self.valid(result)
        self.assertIsNone(result["snapshot_sha256"])
        self.assertEqual(row["deduplication_key"], "message:" + self.f["header_id"])
        self.assertEqual(row["citations"], events[0]["citations"])
        frozen = deepcopy(result)
        events[0]["citations"][0]["unchecked"]["values"].append("input mutation")
        self.assertEqual(result, frozen, "unchecked_input_snapshot")
        events = deepcopy(before_input)
        output = self.track(events)
        output["requests"][0]["timeline"][0]["citations"][0]["unchecked"]["values"].clear()
        self.assertEqual(events, before_input, "unchecked_output_snapshot")
        events[0]["citations"] = []
        self.assertEqual(self.valid(self.track(events))["citations"], [])

    def test_two_calls(self):
        events, bundle = self.inputs()
        first, second = self.track(events, bundle), self.track(events, bundle)
        frozen = deepcopy(second)
        self.assertEqual(first, second)
        first["requests"][0]["timeline"][0]["citations"][0]["quote"] = "CHANGED FIRST CALL"
        self.assertEqual(second, frozen, "earlier_call_independent")
        self.assertEqual(self.track(events, bundle), frozen, "later_call_independent")

    def test_holds_and_empty(self):
        empty = self.track([])
        self.assertEqual(empty["requests"], [])
        self.assertEqual(empty["due_actions"], [])
        self.assertEqual(empty["holds"], [])
        events, bundle = self.inputs()
        later = deepcopy(events[0])
        later.update(occurred_at="2026-10-19T09:00:00Z", due_at=self.f["due_values"][1])
        result = self.track([later, {"agency_id": "fictional"}, events[0], None], bundle)
        request = result["requests"][0]
        self.assertEqual(request["status"], "awaiting_promised_delivery")
        self.assertEqual(request["contradictions"], ["multiple_distinct_promises_require_review"])
        self.assertEqual([r["time_sort"] for r in request["timeline"]],
                         ["2026-10-18T09:00:00+00:00", "2026-10-19T09:00:00+00:00"])
        self.assertEqual(result["holds"], [{"index": 1, "reason": "agency_or_request_number_missing"},
                                            {"index": 3, "reason": "event_shape_invalid"}])
        self.assertEqual([a["status"] for a in result["due_actions"]], ["overdue", "upcoming"])

    def test_never_send(self):
        for kind in self.f["dimensions"]["never_send"]:
            with self.subTest(kind=kind):
                events, bundle = self.inputs()
                events[0]["event_type"] = kind
                result = self.track(events, bundle)
                draft = result["requests"][0]["private_followup_draft"]
                self.assertIs(draft["send"], False)
                self.assertIs(draft["requires_human_review"], True)
                self.assertEqual(draft["purpose"], "private_review_draft")
                self.assertIs(result["review_acceptance"], False)
                self.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")
                self.assertTrue(all(a["draft_only"] for a in result["due_actions"]))
                if kind == "withdrawal":
                    self.assertEqual(result["requests"][0]["status"], "withdrawal_candidate_requires_review")
                    self.assertEqual(result["holds"], [{"request_id": "fictional-office::syn-14", "reason": "withdrawal_candidate_requires_review"}])

    def test_declared_alias_mutant(self):
        # All real validation still executes, then only this returned list is reconnected.
        real = self.c.track
        def mutant(events, **kwargs):
            result = real(events, **kwargs)
            result["requests"][0]["timeline"][0]["citations"] = events[0]["citations"]
            return result
        with patch.object(self.c, "track", mutant):
            with self.assertRaisesRegex(AssertionError, "timeline_citation_snapshot"):
                self.assert_input_snapshot()
        self.assert_input_snapshot()
        print("CONTROL: reconnected timeline citation alias killed by timeline_citation_snapshot; restored positive passed")


if __name__ == "__main__":
    # Derive imports from this file, never the caller's cwd or a custom module path flag.
    import argparse
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument("--temp-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.fc14_tracker_snapshot_acceptance import worker
    raise SystemExit(worker("suite", args.temp_root, args.report))
