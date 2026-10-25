"""FC13: explicit correspondence types and competing text markers keep their priority.

Test-only coverage of the existing lexical classifier in
``campaign_tool.records.understanding.correspondence``. The frozen order is
withdrawal, partial_delivery, delivery, promise, determination, clarification,
acknowledgment, request; a recognized explicit ``event_type`` (or ``kind``)
wins over text. Every input is invented synthetic text from
``tests/fixtures/fc13-correspondence-priority/cases.json``. Labels are
conservative lexical candidates only: nothing here claims to classify intent or
prove a request's real state, and every tracker output stays review-only.

Script mode (``python3 <abs path to this file> --tmp-root <abs dir>``) consumes
its temporary-root argument, redirects temporary storage and installs an
offline socket sentinel BEFORE product imports. Ordinary unittest import does
none of that and leaves the process environment untouched.
"""
from datetime import datetime, timedelta
import itertools
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


def _script_bootstrap():
    """Script-mode only setup; never runs under unittest discovery."""
    import os
    import socket
    import tempfile

    args = sys.argv[1:]
    if "--tmp-root" not in args:
        raise SystemExit("script mode requires --tmp-root <absolute directory>")
    position = args.index("--tmp-root")
    if position + 1 >= len(args):
        raise SystemExit("--tmp-root needs a value")
    root = Path(args[position + 1])
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise SystemExit("--tmp-root must be an existing absolute non-symlink directory")
    del args[position:position + 2]
    sys.argv[1:] = args
    os.environ["TMPDIR"] = str(root)
    tempfile.tempdir = str(root)

    def _offline(*_args, **_kwargs):
        raise OSError("fc13 offline sentinel: network access is not permitted")

    socket.socket.connect = _offline
    socket.socket.connect_ex = _offline
    socket.create_connection = _offline
    socket.getaddrinfo = _offline
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


if __name__ == "__main__":
    _script_bootstrap()

from campaign_tool.records.understanding import correspondence  # noqa: E402

FIXTURE = (Path(__file__).resolve().parents[2] / "tests" / "fixtures"
           / "fc13-correspondence-priority" / "cases.json")
GROUPS = ("single_positive", "pairs", "neutral", "case_variants", "near_misses",
          "explicit", "unknown_explicit", "field_precedence")


def _strict_object(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate fixture key: " + key)
        seen[key] = value
    return seen


def load_cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"), object_pairs_hook=_strict_object)


CASES = load_cases()


def case_event(case):
    """The classifier input for one case: text-only cases carry just ``text``."""
    if "event" in case:
        return dict(case["event"])
    return {"text": case["text"]}


def all_cases(data=None):
    data = CASES if data is None else data
    return [case for group in GROUPS for case in data[group]]


def track_events(cases):
    """One unique request per case with a unique fixed-offset timestamp."""
    base = datetime.fromisoformat(CASES["timestamp_base"])
    events = []
    for index, case in enumerate(cases):
        event = case_event(case)
        event.update({
            "agency_id": CASES["agency_id"],
            "request_number": f"{CASES['request_number_prefix']}{index + 1:04d}",
            "occurred_at": (base + timedelta(minutes=index)).isoformat(),
            "event_id": "fc13-event-" + case["id"],
        })
        events.append(event)
    return events


class LabelAssertions:
    def assert_label(self, case, actual):
        self.assertEqual(actual, case["expected"],
                         f"{case['id']}: expected={case['expected']!r} actual={actual!r}")

    def check_group(self, group):
        cases = CASES[group]
        self.assertEqual(len(cases), CASES["expected_counts"][group])
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assert_label(case, correspondence._classify(case_event(case)))
        return len(cases)


class FrozenContractTests(unittest.TestCase):
    def test_marker_order_matches_frozen_brief(self):
        self.assertEqual([label for label, _ in correspondence.MARKERS], CASES["marker_order"])
        self.assertEqual(sorted(correspondence.TYPES), CASES["explicit_types"])
        self.assertEqual(set(CASES["marker_order"]) | {"other"}, correspondence.TYPES)

    def test_fixture_matrix_is_complete_and_unique(self):
        counts = CASES["expected_counts"]
        self.assertEqual(set(counts), set(GROUPS) | {"total"})
        for group in GROUPS:
            self.assertEqual(len(CASES[group]), counts[group], group)
        cases = all_cases()
        self.assertEqual(len(cases), counts["total"])
        ids = [case["id"] for case in cases]
        self.assertEqual(len(ids), len(set(ids)))
        order = CASES["marker_order"]
        self.assertEqual(len(order), 8)
        expected_pairs = set(itertools.permutations(order, 2))
        seen_pairs = [(case["first"], case["second"]) for case in CASES["pairs"]]
        self.assertEqual(len(seen_pairs), 56)
        self.assertEqual(set(seen_pairs), expected_pairs)
        for case in CASES["pairs"]:
            winner = min(case["first"], case["second"], key=order.index)
            self.assertEqual(case["expected"], winner, case["id"])
            self.assertEqual(case["text"],
                             CASES["phrases"][case["first"]] + " " + CASES["phrases"][case["second"]])
        self.assertEqual({case["expected"] for case in CASES["single_positive"]}, set(order))
        self.assertEqual({case["expected"] for case in CASES["case_variants"]}, set(order))
        explicit_types = {case["expected"] for case in CASES["explicit"]}
        self.assertEqual(explicit_types, set(CASES["explicit_types"]))
        control = CASES["negative_control"]
        selected = [case for case in CASES["pairs"] if case["id"] == control["selected_case"]]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["expected"], control["expected"])
        self.assertNotEqual(control["expected"], control["mutant_label"])
        pair_ids = {case["id"] for case in CASES["pairs"]}
        for extra in control["track_flips_beyond_pairs"]:
            self.assertIn(extra, ids)
            self.assertNotIn(extra, pair_ids)

    def test_each_phrase_hits_only_its_own_marker(self):
        for label, phrase in CASES["phrases"].items():
            with self.subTest(label=label):
                hits = [name for name, pattern in correspondence.MARKERS if pattern.search(phrase)]
                self.assertEqual(hits, [label])
        for case in CASES["single_positive"] + CASES["case_variants"]:
            with self.subTest(case=case["id"]):
                hits = [name for name, pattern in correspondence.MARKERS
                        if pattern.search(case["text"])]
                self.assertEqual(hits, [case["expected"]])
        for case in CASES["near_misses"]:
            with self.subTest(case=case["id"]):
                hits = [name for name, pattern in correspondence.MARKERS
                        if pattern.search(case["text"])]
                self.assertEqual(hits, [])


class ClassifyPriorityTests(LabelAssertions, unittest.TestCase):
    def test_single_category_positives(self):
        self.assertEqual(self.check_group("single_positive"), 34)

    def test_pair_priority_in_both_text_orders(self):
        self.assertEqual(self.check_group("pairs"), 56)

    def test_neutral_and_non_string_text_is_other(self):
        self.assertEqual(self.check_group("neutral"), 8)

    def test_case_variants_keep_their_label(self):
        self.assertEqual(self.check_group("case_variants"), 9)

    def test_boundary_near_misses_are_other(self):
        self.assertEqual(self.check_group("near_misses"), 16)

    def test_recognized_explicit_type_beats_competing_text(self):
        self.assertEqual(self.check_group("explicit"), 21)

    def test_unknown_explicit_type_falls_back_to_text(self):
        self.assertEqual(self.check_group("unknown_explicit"), 6)

    def test_event_type_versus_kind_precedence(self):
        self.assertEqual(self.check_group("field_precedence"), 8)


class TrackConsumerTests(LabelAssertions, unittest.TestCase):
    def test_track_timeline_event_type_matches_frozen_label(self):
        cases = all_cases()
        events = track_events(cases)
        snapshot = json.dumps(events, sort_keys=True, default=str)
        result = correspondence.track(events, as_of=CASES["as_of"])
        self.assertEqual(json.dumps(events, sort_keys=True, default=str), snapshot)
        self.assertEqual(result["schema"], correspondence.SCHEMA)
        self.assertIs(result["review_acceptance"], False)
        self.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")
        self.assertEqual(result["as_of_status"], "explicit_offset")
        self.assertEqual(result["due_actions"], [])
        by_number = {request["request_number"]: request for request in result["requests"]}
        self.assertEqual(len(by_number), len(cases))
        withdrawal_ids = set()
        for case, event in zip(cases, events):
            with self.subTest(case=case["id"]):
                request = by_number[event["request_number"]]
                self.assertEqual(len(request["timeline"]), 1)
                self.assertEqual(request["occurrence_count"], 1)
                row = request["timeline"][0]
                self.assert_label(case, row["event_type"])
                self.assertEqual(row["time_status"], "explicit_offset")
                self.assertEqual(row["occurrences"][0]["occurred_at"], event["occurred_at"])
                draft = request["private_followup_draft"]
                self.assertIs(draft["send"], False)
                self.assertIs(draft["requires_human_review"], True)
                self.assertEqual(draft["purpose"], "private_review_draft")
                self.assertEqual(request["semantic_assessment"], "NOT_ASSESSED")
                if case["expected"] == "withdrawal":
                    withdrawal_ids.add(request["request_id"])
                    self.assertEqual(row["withdrawal_review_status"], "unreviewed")
                    self.assertEqual(request["status"], "withdrawal_candidate_requires_review")
                else:
                    self.assertIsNone(row["withdrawal_review_status"])
                    self.assertNotEqual(request["status"], "withdrawal_candidate_requires_review")
        self.assertTrue(withdrawal_ids)
        self.assertEqual(sorted((hold["request_id"], hold["reason"]) for hold in result["holds"]),
                         sorted((rid, "withdrawal_candidate_requires_review") for rid in withdrawal_ids))


class NegativeControlTests(LabelAssertions, unittest.TestCase):
    def test_reversed_marker_sequence_fails_selected_pair_assertion(self):
        control = CASES["negative_control"]
        selected = next(case for case in CASES["pairs"] if case["id"] == control["selected_case"])
        original = correspondence.MARKERS
        with mock.patch.object(correspondence, "MARKERS", tuple(reversed(original))):
            wrong = correspondence._classify(case_event(selected))
            self.assertEqual(wrong, control["mutant_label"])
            with self.assertRaises(self.failureException) as caught:
                self.assert_label(selected, wrong)
            self.assertIn(f"{selected['id']}: expected={control['expected']!r} "
                          f"actual={control['mutant_label']!r}", str(caught.exception))
            flipped = [case["id"] for case in CASES["pairs"]
                       if correspondence._classify(case_event(case)) != case["expected"]]
            self.assertEqual(len(flipped), len(CASES["pairs"]))
            flipped_all = sorted(case["id"] for case in all_cases()
                                 if correspondence._classify(case_event(case)) != case["expected"])
            self.assertEqual(flipped_all, sorted(flipped + control["track_flips_beyond_pairs"]))
            for group in ("explicit", "field_precedence", "single_positive", "near_misses"):
                for case in CASES[group]:
                    with self.subTest(mutant_positive=case["id"]):
                        self.assert_label(case, correspondence._classify(case_event(case)))
        self.assertIs(correspondence.MARKERS, original)
        self.assert_label(selected, correspondence._classify(case_event(selected)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
