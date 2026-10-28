"""FC9-CANDIDATE-CLOCKS: invalid source offsets and unrepresentable UTC instants stay unresolved.

Every value below and in tests/fixtures/fc9-candidate-clocks/cases.json is
invented: agencies, request numbers, message ids and timestamps describe no
real record, person, mailbox or source. Machine-valid results are REVIEW
REQUIRED, never semantic or human acceptance.

Script mode (portable row): ``python3 test_fc9_candidate_clocks.py --temp-root DIR
[--coverage-out FILE]``. Those two arguments are consumed before any product
import; ordinary unittest discovery imports this module without changing
sys.path, the environment or tempfile settings.
"""
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

if __name__ == "__main__":
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
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from datetime import datetime, timedelta, timezone  # noqa: E402

from campaign_tool.records.understanding import correspondence, document, evidence  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fc9-candidate-clocks" / "cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
AGENCY = CASES["agency_id"]
EXERCISED = set()
NC1_MARKER = "FC9-NC1-NULL-VALUE"
NC2_MARKER = "FC9-NC2-NO-OVERFLOW"

# Frozen-experiment requirement -> named tests that must run and pass.
REQUIREMENTS = {
    "R1_document_invalid_offsets_null_with_raw_citation": [
        "DocumentOffsetTests.test_analyze_offset_lexical_matrix",
        "DocumentOffsetTests.test_nc1_target_plus_01_60_value_null"],
    "R2_document_valid_controls_retained": [
        "DocumentOffsetTests.test_analyze_offset_lexical_matrix",
        "DocumentOffsetTests.test_analyze_retained_behaviors"],
    "R3_track_occurred_at_offsets_and_overflow_invalid_time": [
        "CorrespondenceClockTests.test_track_occurred_at_matrix",
        "CorrespondenceClockTests.test_nc2_target_occurred_at_high_overflow_no_escape"],
    "R4_track_valid_boundary_and_unknown_paths_retained": [
        "CorrespondenceClockTests.test_track_occurred_at_matrix"],
    "R5_promised_due_at_invalid_held_valid_retained": [
        "CorrespondenceClockTests.test_track_promised_due_at"],
    "R6_string_as_of_fails_closed_without_false_status": [
        "CorrespondenceClockTests.test_track_string_as_of"],
    "R7_aware_datetime_as_of_fails_closed_without_overflow": [
        "CorrespondenceClockTests.test_track_datetime_as_of"],
    "R8_rfc_date_metadata_unrepresentable_is_invalid_date_hold": [
        "RfcDateMetadataTests.test_events_from_bundle_and_track"],
    "R9_negative_control_lexical_bypass_detected": [
        "SyntheticNegativeControlTests.test_nc1_lexical_bypass_breaks_null_value_assertion"],
    "R10_negative_control_unguarded_conversion_detected": [
        "SyntheticNegativeControlTests.test_nc2_unguarded_conversion_breaks_no_overflow_assertion"],
}
VALID_CONTROLS = [
    ("DocumentOffsetTests", "test_analyze_offset_lexical_matrix"),
    ("DocumentOffsetTests", "test_analyze_retained_behaviors"),
    ("CorrespondenceClockTests", "test_track_occurred_at_matrix"),
    ("CorrespondenceClockTests", "test_track_promised_due_at"),
    ("RfcDateMetadataTests", "test_events_from_bundle_and_track"),
]


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _bundle(label, text, data=None):
    original = _digest(("fc9 candidate clocks synthetic " + label).encode())
    receipt = {
        "schema": "format-extraction-v1", "original_sha256": original, "original_changed": False,
        "status": "complete", "parser": "synthetic", "parser_version": "0", "route": {"format": "eml"},
        "pages": [{"page_no": 1, "status": "ok"}],
        "units": [{"kind": "text", "locator": {"page": 1}, "text": text, "data": data or {}}],
    }
    return evidence.normalize_receipt(receipt, original, _digest(evidence.canonical(receipt)))


def _request_number(case_id):
    return CASES["request_prefix"] + "-" + case_id.upper()


def _request_id(number):
    return AGENCY.casefold() + "::" + number.casefold()


def _track(testcase, events, **kwargs):
    try:
        return correspondence.track(events, **kwargs)
    except OverflowError as exc:  # the only escape this card forbids
        testcase.fail(f"{NC2_MARKER}: OverflowError escaped track(): {exc}")


def _analyze(testcase, bundle):
    try:
        return document.analyze(bundle)
    except OverflowError as exc:
        testcase.fail(f"{NC2_MARKER}: OverflowError escaped analyze(): {exc}")


class DocumentOffsetTests(unittest.TestCase):
    def _check_mentions(self, cases):
        lines = [f"Mention {case['id']}: {case['raw']}" for case in cases]
        bundle = _bundle("document " + ",".join(c["id"] for c in cases), "\n".join(lines) + "\n")
        result = _analyze(self, bundle)
        self.assertFalse(result["review_acceptance"])
        self.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")
        by_raw = {}
        for mention in result["date_mentions"]:
            by_raw.setdefault(mention["raw"], []).append(mention)
        for case in cases:
            with self.subTest(case=case["id"]):
                found = by_raw.get(case["raw"], [])
                self.assertEqual(len(found), 1, "exact raw mention must be retained once")
                mention, expect = found[0], case["expect"]
                self.assertEqual(mention["value"], expect["value"], f"{NC1_MARKER}: {case['id']}")
                self.assertEqual(mention["time_basis"], expect["time_basis"])
                self.assertEqual(mention.get("parse_status"), expect["parse_status"])
                self.assertEqual(mention["citation"]["quote"], case["raw"])
                evidence.validate_citation(mention["citation"], bundle)
                self.assertEqual(mention["semantic_assessment"], "NOT_ASSESSED")
                EXERCISED.add(case["id"])

    def test_analyze_offset_lexical_matrix(self):
        cases = [c for c in CASES["document_mentions"] if c["dimension"] == "offset_lexical"]
        self.assertEqual(len(cases), 24)
        self._check_mentions(cases)

    def test_analyze_retained_behaviors(self):
        self._check_mentions([c for c in CASES["document_mentions"] if c["dimension"] == "retained_behavior"])

    def test_nc1_target_plus_01_60_value_null(self):
        target = CASES["negative_controls"]["lexical_bypass_case"]
        self._check_mentions([c for c in CASES["document_mentions"] if c["id"] == target])


class CorrespondenceClockTests(unittest.TestCase):
    def _check_occurred(self, cases):
        events = []
        for case in cases:
            event = {"agency_id": AGENCY, "request_number": _request_number(case["id"]),
                     "event_type": "request", "text": "Synthetic request " + case["id"]}
            if case["raw"] is not None:
                event["occurred_at"] = case["raw"]
            events.append(event)
        result = _track(self, events, as_of=CASES["track_as_of"])
        self.assertFalse(result["review_acceptance"])
        self.assertEqual(result["as_of_status"], "explicit_offset")
        requests = {row["request_id"]: row for row in result["requests"]}
        for case in cases:
            with self.subTest(case=case["id"]):
                row = requests[_request_id(_request_number(case["id"]))]["timeline"][0]
                self.assertEqual(row["time_status"], case["expect"]["time_status"])
                self.assertEqual(row["time_sort"], case["expect"]["time_sort"])
                self.assertEqual(row["time_source"], "event_time")
                self.assertEqual(row["occurrences"][0]["occurred_at"], case["raw"], "raw timestamp is not repaired")
                EXERCISED.add(case["id"])

    def test_track_occurred_at_matrix(self):
        cases = CASES["occurred_at"]
        self.assertEqual(sum(c["dimension"] == "offset_lexical" for c in cases), 24)
        self._check_occurred(cases)

    def test_nc2_target_occurred_at_high_overflow_no_escape(self):
        target = CASES["negative_controls"]["unguarded_conversion_case"]
        self._check_occurred([c for c in CASES["occurred_at"] if c["id"] == target])

    def test_track_promised_due_at(self):
        events = [{"agency_id": AGENCY, "request_number": _request_number(case["id"]), "event_type": "promise",
                   "occurred_at": "2030-03-01T00:00:00Z", "due_kind": "promised_delivery", "due_at": case["due_at"]}
                  for case in CASES["promised_due_at"]]
        result = _track(self, events, as_of=CASES["track_as_of"])
        self.assertFalse(result["review_acceptance"])
        for case in CASES["promised_due_at"]:
            with self.subTest(case=case["id"]):
                request_id = _request_id(_request_number(case["id"]))
                holds = [h["reason"] for h in result["holds"] if h.get("request_id") == request_id]
                actions = [a for a in result["due_actions"] if a["request_id"] == request_id]
                expect = case["expect"]
                if expect["hold"]:
                    self.assertIn(expect["hold"], holds)
                    self.assertEqual(actions, [], "no follow-up action for an unresolved due time")
                else:
                    self.assertEqual(len(actions), 1)
                    for key, value in expect["action"].items():
                        self.assertEqual(actions[0][key], value)
                    self.assertTrue(actions[0]["draft_only"])
                timeline = result["requests"]
                row = next(r for r in timeline if r["request_id"] == request_id)["timeline"][0]
                self.assertEqual(row["due_at"], case["due_at"], "raw due_at is not repaired")
                EXERCISED.add(case["id"])

    def _check_as_of(self, case, as_of):
        number = _request_number(case["id"])
        events = [{"agency_id": AGENCY, "request_number": number, "event_type": "promise",
                   "occurred_at": "2030-03-01T00:00:00Z", "due_kind": "promised_delivery",
                   "due_at": CASES["as_of_due_at"]}]
        result = _track(self, events, as_of=as_of)
        self.assertFalse(result["review_acceptance"])
        self.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")
        self.assertEqual(result["as_of"], case["expect"]["as_of"])
        self.assertEqual(result["as_of_status"], case["expect"]["as_of_status"])
        self.assertEqual([a["status"] for a in result["due_actions"]], [case["expect"]["due_status"]])
        if case["expect"]["as_of_status"] != "explicit_offset":
            self.assertNotIn(result["due_actions"][0]["status"], {"overdue", "upcoming"})
        EXERCISED.add(case["id"])

    def test_track_string_as_of(self):
        for case in CASES["as_of_strings"]:
            with self.subTest(case=case["id"]):
                self._check_as_of(case, case["as_of"])

    def test_track_datetime_as_of(self):
        for case in CASES["as_of_datetimes"]:
            with self.subTest(case=case["id"]):
                value = datetime.fromisoformat(case["local"])
                if case["offset_minutes"] is not None:
                    value = value.replace(tzinfo=timezone(timedelta(minutes=case["offset_minutes"])))
                self._check_as_of(case, value)


class RfcDateMetadataTests(unittest.TestCase):
    def test_events_from_bundle_and_track(self):
        for case in CASES["rfc_dates"]:
            with self.subTest(case=case["id"]):
                number = _request_number(case["id"])
                message_id = f"<{case['id']}@synthetic.invalid>"
                headers = [["Message-ID", message_id]]
                lines = ["Message-ID: " + message_id]
                if case["date"] is not None:
                    headers.append(["Date", case["date"]])
                    lines.append("Date: " + case["date"])
                text = "\n".join(lines) + f"\n\nWe received your request {number}."
                bundle = _bundle("rfc " + case["id"], text, {"headers": headers})
                try:
                    events = correspondence.events_from_bundle(bundle, AGENCY, [number])
                except OverflowError as exc:
                    self.fail(f"{NC2_MARKER}: OverflowError escaped events_from_bundle(): {exc}")
                self.assertEqual(len(events), 1)
                event, expect = events[0], case["expect"]
                self.assertEqual(event["message_date"], expect["message_date"])
                self.assertEqual(event["message_date_status"], expect["message_date_status"])
                hold_reasons = [h["reason"] for h in event["metadata_holds"]]
                if expect["metadata_hold"]:
                    self.assertIn(expect["metadata_hold"], hold_reasons)
                else:
                    self.assertEqual(hold_reasons, [])
                if case["date"] is not None:
                    self.assertEqual(event["message_date_citation"]["quote"], case["date"],
                                     "exact source-backed raw Date is cited")
                result = _track(self, events, as_of=CASES["track_as_of"], bundle=bundle)
                self.assertFalse(result["review_acceptance"])
                row = result["requests"][0]["timeline"][0]
                self.assertEqual(row["time_status"], expect["time_status"])
                self.assertEqual(row["time_sort"], expect["time_sort"])
                self.assertEqual(row["message_date"], expect["message_date"])
                track_holds = [h["reason"] for h in result["holds"]]
                if expect["metadata_hold"]:
                    self.assertIn(expect["metadata_hold"], track_holds)
                EXERCISED.add(case["id"])


def _run_named(class_name, method):
    result = unittest.TestResult()
    unittest.TestSuite([globals()[class_name](method)]).run(result)
    return result


class SyntheticNegativeControlTests(unittest.TestCase):
    """Declared synthetic controls: each injected defect must break exactly its named assertion."""

    def _expect_killed(self, result, marker):
        self.assertEqual(result.errors, [], "an arbitrary error is not a killed mutation")
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(result.failures), 1, "injected defect must be detected")
        self.assertIn(marker, result.failures[0][1])

    def _restore_and_rerun(self, target):
        rerun = _run_named(*target)
        self.assertTrue(rerun.wasSuccessful(), rerun.failures + rerun.errors)
        for name in VALID_CONTROLS:
            control = _run_named(*name)
            self.assertTrue(control.wasSuccessful(), (name, control.failures + control.errors))

    def test_nc1_lexical_bypass_breaks_null_value_assertion(self):
        target_case = next(c for c in CASES["document_mentions"]
                           if c["id"] == CASES["negative_controls"]["lexical_bypass_case"])
        original = document._offset_components_in_range
        calls = []

        def bypass(raw):  # injected: skip the lexical check only for the +01:60 fixture
            if raw == target_case["raw"]:
                calls.append(raw)
                return True
            return original(raw)

        with patch.object(document, "_offset_components_in_range", bypass):
            killed = _run_named("DocumentOffsetTests", "test_nc1_target_plus_01_60_value_null")
        self.assertEqual(calls, [target_case["raw"]], "bypass reached only the target fixture")
        self._expect_killed(killed, NC1_MARKER)
        self.assertIs(document._offset_components_in_range, original)
        self._restore_and_rerun(("DocumentOffsetTests", "test_nc1_target_plus_01_60_value_null"))

    def test_nc2_unguarded_conversion_breaks_no_overflow_assertion(self):
        original = correspondence._to_utc
        calls = []

        def unguarded(parsed):  # injected: the pre-fix conversion without the bounded policy
            calls.append(parsed.isoformat())
            return parsed.astimezone(timezone.utc)

        with patch.object(correspondence, "_to_utc", unguarded):
            killed = _run_named("CorrespondenceClockTests",
                                "test_nc2_target_occurred_at_high_overflow_no_escape")
        self.assertTrue(calls, "the injected seam was exercised")
        self._expect_killed(killed, NC2_MARKER)
        self.assertIs(correspondence._to_utc, original)
        self._restore_and_rerun(("CorrespondenceClockTests",
                                 "test_nc2_target_occurred_at_high_overflow_no_escape"))


def fixture_case_ids():
    groups = ("document_mentions", "occurred_at", "promised_due_at", "as_of_strings", "as_of_datetimes", "rfc_dates")
    return sorted(case["id"] for group in groups for case in CASES[group])


def case_dimensions():
    out = {}
    for group in ("document_mentions", "occurred_at", "promised_due_at", "as_of_strings", "as_of_datetimes", "rfc_dates"):
        for case in CASES[group]:
            key = group + ":" + case["dimension"]
            out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


if __name__ == "__main__":
    _program = unittest.main(argv=_argv, exit=False, verbosity=2)
    if _COVERAGE_OUT is not None:
        _COVERAGE_OUT.write_text(json.dumps({
            "tests_run": _program.result.testsRun,
            "failures": len(_program.result.failures), "errors": len(_program.result.errors),
            "skipped": len(_program.result.skipped),
            "exercised_case_ids": sorted(EXERCISED), "fixture_case_ids": fixture_case_ids(),
            "case_dimensions": case_dimensions()}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.exit(0 if _program.result.wasSuccessful() else 1)
