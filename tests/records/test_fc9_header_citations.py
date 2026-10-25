"""FC9-HEADER-CITATIONS: metadata-only correspondence headers cite their retained data path.

All fixtures are fictional data committed in
tests/fixtures/fc9-header-citations/cases.json. No mailbox, container, ledger,
network or real record is read. Machine-valid output is REVIEW REQUIRED only.

Script mode accepts ``--tmp-root PATH`` before any product import; ordinary
unittest discovery imports this module without changing tempfile settings,
the environment or ``sys.path``.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "fc9-header-citations" / "cases.json"

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--tmp-root":
        _tmp_root = Path(sys.argv[2])
        _tmp_root.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(_tmp_root)
        del sys.argv[1:3]
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

from campaign_tool.records import grounded_evidence  # noqa: E402
from campaign_tool.records.understanding import correspondence, evidence  # noqa: E402

DATA_SCHEMA = "records-grounded-data-evidence-v1"
TEXT_SCHEMA = "records-grounded-evidence-v1"
BINDING_KEYS = ("unit_id", "original_sha256", "import_id", "artifact_sha256", "text_sha256", "locator")


def load_cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


CASES = load_cases()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def receipt_bundle(label, headers, text=None):
    """Return a receipt-bound bundle with one unit: body text plus data.headers."""
    original = _sha(("fc9-header-citations:" + label).encode("utf-8"))
    receipt = {
        "schema": "format-extraction-v1", "original_sha256": original, "original_changed": False,
        "status": "complete", "parser": "synthetic", "parser_version": "0", "route": {"format": "eml"},
        "pages": [{"page_no": 1, "status": "ok"}],
        "units": [{"kind": "message", "locator": {"part": "message", "label": label},
                   "text": CASES["body_text"] if text is None else text,
                   "data": {"headers": copy.deepcopy(headers)}}],
    }
    return evidence.normalize_receipt(receipt, original, _sha(evidence.canonical(receipt)))


def merged_bundle(*bundles):
    units = [unit for item in bundles for unit in item["units"]]
    context = {}
    for item in bundles:
        context.update(item["source_context"])
    receipts = [row for item in bundles for row in item["source_receipts"]]
    return evidence.bundle(units, context, receipts)


def case_named(group, name):
    return next(case for case in CASES[group] if case["name"] == name)


def events_for(module, bundle):
    return module.events_from_bundle(bundle, CASES["agency_id"], [CASES["request_number"]])


def assert_data_citation(tc, cite, bundle, pointer, value):
    unit = bundle["units"][0] if len(bundle["units"]) == 1 else next(
        u for u in bundle["units"] if u["unit_id"] == cite["unit_id"])
    tc.assertIsInstance(cite, dict)
    tc.assertEqual(cite["schema"], DATA_SCHEMA)
    tc.assertEqual(cite["json_pointer"], pointer)
    tc.assertEqual(json.loads(cite["quote"]), value)
    for key in BINDING_KEYS:
        tc.assertEqual(cite[key], unit[key], key)
    tc.assertEqual(cite["data_sha256"], _sha(evidence.canonical(unit["data"])))
    tc.assertEqual(cite["span_basis"], "canonical_retained_unit_data")
    tc.assertEqual(grounded_evidence.validate_support(cite, bundle)["structural_support"],
                   "verified_exact_retained_data_projection")


def check_addressable(tc, case, module=correspondence):
    """Exact pointer/quote/binding plus date/ID result through events_from_bundle then track."""
    bundle = receipt_bundle(case["name"], case["headers"])
    events = events_for(module, bundle)
    tc.assertEqual(len(events), 1)
    event = events[0]
    unit = bundle["units"][0]
    tc.assertEqual(event["metadata_holds"], [])
    tc.assertEqual(event["source_original_sha256"], unit["original_sha256"])
    tc.assertEqual(event["source_id"], unit["import_id"])
    tc.assertEqual(event["request_numbers"], [CASES["request_number"]])
    tc.assertEqual(event["message_id"], case["message_id"])
    tc.assertEqual(event["message_date"], CASES["date_normalized"])
    tc.assertEqual(event["message_date_status"], "explicit_offset")
    assert_data_citation(tc, event["message_date_citation"], bundle, case["date_pointer"], CASES["date_value"])
    assert_data_citation(tc, event["message_id_citation"], bundle, case["id_pointer"], case["message_id"])
    tc.assertIn(event["message_date_citation"], event["citations"])
    tc.assertIn(event["message_id_citation"], event["citations"])
    tracked = module.track(events, as_of=CASES["as_of"], bundle=bundle)
    tc.assertEqual(tracked["holds"], [])
    tc.assertEqual(len(tracked["requests"]), 1)
    row = tracked["requests"][0]["timeline"][0]
    tc.assertEqual(row["message_date"], CASES["date_normalized"])
    tc.assertEqual(row["message_date_status"], "explicit_offset")
    tc.assertEqual(row["time_status"], "message_date_explicit_offset")
    tc.assertEqual(row["time_source"], "message_date")
    tc.assertEqual(row["message_date_citation"]["json_pointer"], case["date_pointer"])
    tc.assertEqual(row["message_id_citation"]["json_pointer"], case["id_pointer"])
    tc.assertEqual(row["deduplication_key"], "message:" + case["message_id"].casefold())
    tc.assertEqual(tracked["semantic_assessment"], "NOT_ASSESSED")
    tc.assertIs(tracked["review_acceptance"], False)
    return event, tracked


def legacy_pointer_rows(original):
    """Synthetic control: restore the pinned /headers/<index>/1 pointer at the selection seam."""
    def rows(unit):
        return [(index, key, value, "/headers/%d/1" % index) for index, key, value, _ in original(unit)]
    return rows


def run_negative_control(module=correspondence):
    """Inject the legacy pointer for a dictionary fixture; only the expected failure counts."""
    tc = unittest.TestCase()
    dict_case = case_named("addressable_cases", "dict_scalar")
    pair_case = case_named("addressable_cases", "pair_array")
    original = module._header_pairs
    observed = {"injected": "header pointer selection returns /headers/<index>/1 for every row",
                "dict_case": dict_case["name"], "pair_case": pair_case["name"]}
    with mock.patch.object(module, "_header_pairs", legacy_pointer_rows(original)):
        try:
            check_addressable(tc, dict_case, module)
            observed["dict_under_injection"] = "passed"
            observed["detected"] = False
        except AssertionError as exc:
            observed["dict_under_injection"] = "assertion_failed: " + str(exc).splitlines()[0][:240]
            observed["detected"] = True
        except ValueError as exc:
            if str(exc) != "evidence_pointer_missing":
                raise
            observed["dict_under_injection"] = "value_error: evidence_pointer_missing"
            observed["detected"] = True
        check_addressable(tc, pair_case, module)
        observed["pair_under_injection"] = "passed"
    observed["restored_is_original"] = module._header_pairs is original
    check_addressable(tc, dict_case, module)
    check_addressable(tc, pair_case, module)
    observed["restored_dict_and_pair"] = "passed"
    return observed


class AddressableHeaderTests(unittest.TestCase):
    def test_every_addressable_representation_cites_exact_pointer(self):
        self.assertGreaterEqual(len(CASES["addressable_cases"]), 8)
        for case in CASES["addressable_cases"]:
            with self.subTest(case=case["name"]):
                check_addressable(self, case)

    def test_dict_scalar(self):
        check_addressable(self, case_named("addressable_cases", "dict_scalar"))

    def test_dict_list(self):
        check_addressable(self, case_named("addressable_cases", "dict_list"))

    def test_dict_object(self):
        check_addressable(self, case_named("addressable_cases", "dict_object"))

    def test_dict_object_empty_value_uses_raw(self):
        check_addressable(self, case_named("addressable_cases", "dict_object_empty_value_uses_raw"))

    def test_dict_list_object(self):
        check_addressable(self, case_named("addressable_cases", "dict_list_object"))

    def test_lower_case_dict_keys(self):
        check_addressable(self, case_named("addressable_cases", "dict_key_needs_no_escape_but_casefolds"))

    def test_pair_array_compatibility_positive(self):
        check_addressable(self, case_named("addressable_cases", "pair_array"))

    def test_object_list(self):
        check_addressable(self, case_named("addressable_cases", "object_list"))

    def test_body_only_text_has_no_header_values(self):
        for case in CASES["addressable_cases"]:
            with self.subTest(case=case["name"]):
                self.assertNotIn(CASES["date_value"], CASES["body_text"])
                self.assertNotIn(case["message_id"], CASES["body_text"])
                self.assertNotIn("%", CASES["request_number"])


class PointerTokenTests(unittest.TestCase):
    def test_pointer_token_escapes_tilde_then_slash(self):
        for key, token in CASES["pointer_token_cases"]:
            with self.subTest(key=key):
                self.assertEqual(correspondence._json_pointer_token(key), token)

    def test_escaped_tokens_resolve_in_retained_data(self):
        for key, token in CASES["pointer_token_cases"]:
            with self.subTest(key=key):
                bundle = receipt_bundle("token:" + token, {key: "synthetic value"})
                support = grounded_evidence.data_pointer_support(bundle["units"][0], "/headers/" + token)
                self.assertEqual(json.loads(support["quote"]), "synthetic value")

    def test_header_rows_carry_escaped_pointers(self):
        case = CASES["escaped_key_cases"][0]
        bundle = receipt_bundle(case["name"], case["headers"])
        rows = {key: pointer for _, key, _, pointer in correspondence._header_pairs(bundle["units"][0])}
        for key, pointer in case["pointers"].items():
            with self.subTest(key=key):
                self.assertEqual(rows[key], pointer)
                support = grounded_evidence.data_pointer_support(bundle["units"][0], pointer)
                self.assertEqual(json.loads(support["quote"]), case["headers"][key])
        check_addressable(self, case)

    def test_raw_header_string_rows_have_no_pointer(self):
        case = case_named("unresolved_cases", "raw_header_strings")
        bundle = receipt_bundle(case["name"], case["headers"])
        rows = correspondence._header_pairs(bundle["units"][0])
        self.assertEqual([row[3] for row in rows], [None, None])
        self.assertEqual([row[1] for row in rows], ["Date", "Message-ID"])

    def test_missing_pointer_path_is_unlocatable_not_fatal(self):
        bundle = receipt_bundle("missing-path", {"Date": CASES["date_value"]})
        unit = bundle["units"][0]
        self.assertIsNone(correspondence._exact_header_citation(
            unit, CASES["date_value"], bundle, pointer="/headers/Absent"))
        self.assertIsNone(correspondence._exact_header_citation(
            unit, CASES["date_value"], bundle, pointer="/headers/0/1"))

    def test_pointed_scalar_must_equal_value(self):
        bundle = receipt_bundle("value-mismatch", {"Date": CASES["date_value"]})
        unit = bundle["units"][0]
        self.assertIsNone(correspondence._exact_header_citation(
            unit, "Wed, 05 Feb 2031 09:00:00 +0000", bundle, pointer="/headers/Date"))
        bundle = receipt_bundle("non-scalar", {"Date": [CASES["date_value"]]})
        self.assertIsNone(correspondence._exact_header_citation(
            bundle["units"][0], CASES["date_value"], bundle, pointer="/headers/Date"))
        cite = correspondence._exact_header_citation(
            bundle["units"][0], CASES["date_value"], bundle, pointer="/headers/Date/0")
        self.assertEqual(cite["json_pointer"], "/headers/Date/0")


class UnresolvedHeaderTests(unittest.TestCase):
    def check_unresolved(self, case):
        bundle = receipt_bundle(case["name"], case["headers"])
        events = events_for(correspondence, bundle)
        self.assertEqual(len(events), 1)
        event = events[0]
        reasons = sorted(hold["reason"] for hold in event["metadata_holds"])
        self.assertEqual(reasons, sorted(case["holds"]))
        for hold in event["metadata_holds"]:
            self.assertEqual(hold["original_sha256"], bundle["units"][0]["original_sha256"])
            self.assertLessEqual(len(hold["reason"]), 96)
        self.assertEqual(event["message_id"], case["message_id"])
        if "date_header_unlocatable" in case["holds"]:
            self.assertIsNone(event["message_date"])
            self.assertIsNone(event["message_date_citation"])
            self.assertEqual(event["message_date_status"], "unknown")
        if "message_id_header_unlocatable" in case["holds"]:
            self.assertIsNone(event["message_id"])
            self.assertIsNone(event["message_id_citation"])
        if case.get("id_pointer"):
            assert_data_citation(self, event["message_id_citation"], bundle, case["id_pointer"], case["message_id"])
        if case.get("date_pointer"):
            assert_data_citation(self, event["message_date_citation"], bundle, case["date_pointer"],
                                 CASES["date_value"])
        tracked = correspondence.track(events, as_of=CASES["as_of"], bundle=bundle)
        self.assertEqual(sorted(hold["reason"] for hold in tracked["holds"]), sorted(case["holds"]))
        row = tracked["requests"][0]["timeline"][0]
        if "date_header_unlocatable" in case["holds"]:
            self.assertIsNone(row["message_date"])
            self.assertNotEqual(row["message_date_status"], "explicit_offset")
            self.assertIsNone(row["time_sort"])
        if "message_id_header_unlocatable" in case["holds"]:
            self.assertIsNone(row["message_id_citation"])
            self.assertTrue(row["deduplication_key"].startswith("sha256:"))
        return event, tracked

    def test_raw_header_strings_hold_and_stay_unknown(self):
        self.check_unresolved(case_named("unresolved_cases", "raw_header_strings"))

    def test_stripped_dict_value_holds(self):
        self.check_unresolved(case_named("unresolved_cases", "stripped_dict_value"))

    def test_stripped_pair_value_holds(self):
        self.check_unresolved(case_named("unresolved_cases", "stripped_pair_value"))

    def test_unresolved_original_does_not_abort_valid_peer(self):
        held = case_named("unresolved_cases", "raw_header_strings")
        peer = case_named("addressable_cases", "dict_scalar")
        held_bundle = receipt_bundle(held["name"], held["headers"])
        peer_bundle = receipt_bundle(peer["name"], peer["headers"], text=CASES["peer_body_text"])
        bundle = merged_bundle(held_bundle, peer_bundle)
        events = events_for(correspondence, bundle)
        self.assertEqual(len(events), 2)
        by_original = {event["source_original_sha256"]: event for event in events}
        held_event = by_original[held_bundle["units"][0]["original_sha256"]]
        peer_event = by_original[peer_bundle["units"][0]["original_sha256"]]
        self.assertIsNone(held_event["message_date"])
        self.assertIsNone(held_event["message_id"])
        self.assertEqual(sorted(h["reason"] for h in held_event["metadata_holds"]), sorted(held["holds"]))
        self.assertEqual(peer_event["metadata_holds"], [])
        self.assertEqual(peer_event["message_date"], CASES["date_normalized"])
        assert_data_citation(self, peer_event["message_date_citation"], bundle, peer["date_pointer"],
                             CASES["date_value"])
        assert_data_citation(self, peer_event["message_id_citation"], bundle, peer["id_pointer"],
                             peer["message_id"])
        tracked = correspondence.track(events, as_of=CASES["as_of"], bundle=bundle)
        rows = tracked["requests"][0]["timeline"]
        self.assertEqual(len(rows), 2)
        dated = [row for row in rows if row["message_date"]]
        self.assertEqual([row["message_date"] for row in dated], [CASES["date_normalized"]])
        self.assertEqual(dated[0]["message_id_citation"]["json_pointer"], peer["id_pointer"])
        reasons = sorted(hold["reason"] for hold in tracked["holds"])
        self.assertEqual(reasons, sorted(held["holds"] + ["event_order_uncertain"]))


class ConflictAndBoundaryTests(unittest.TestCase):
    def run_case(self, case):
        bundle = receipt_bundle(case["name"], case["headers"])
        events = events_for(correspondence, bundle)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(sorted(h["reason"] for h in event["metadata_holds"]), sorted(case["holds"]))
        self.assertEqual(event["message_id"], case["message_id"])
        tracked = correspondence.track(events, as_of=CASES["as_of"], bundle=bundle)
        self.assertEqual(sorted(h["reason"] for h in tracked["holds"]), sorted(case["holds"]))
        return bundle, event, tracked

    def test_contradictory_duplicate_dates_hold(self):
        _, event, tracked = self.run_case(case_named("conflict_cases", "dict_list_contradictory_dates"))
        self.assertIsNone(event["message_date"])
        self.assertIsNone(event["message_date_citation"])
        self.assertIsNone(tracked["requests"][0]["timeline"][0]["message_date"])

    def test_contradictory_duplicate_ids_hold(self):
        _, event, tracked = self.run_case(case_named("conflict_cases", "pair_array_contradictory_ids"))
        self.assertIsNone(event["message_id"])
        self.assertIsNone(event["message_id_citation"])
        self.assertEqual(event["message_date"], CASES["date_normalized"])

    def test_same_instant_duplicates_are_one_date(self):
        _, event, tracked = self.run_case(case_named("conflict_cases", "dict_list_same_instant_is_not_conflict"))
        self.assertEqual(event["message_date"], CASES["date_normalized"])
        self.assertEqual(event["message_date_citation"]["schema"], DATA_SCHEMA)
        self.assertIn(event["message_date_citation"]["json_pointer"], ("/headers/Date/0", "/headers/Date/1"))
        self.assertEqual(tracked["requests"][0]["timeline"][0]["time_status"], "message_date_explicit_offset")

    def test_missing_date_and_id_stay_unknown(self):
        _, event, tracked = self.run_case(case_named("boundary_cases", "missing_date_and_id"))
        self.assertIsNone(event["message_date"])
        self.assertEqual(event["message_date_status"], "unknown")
        self.assertIsNone(event["message_id_citation"])
        self.assertEqual(tracked["requests"][0]["timeline"][0]["time_status"], "time_unknown")

    def test_timezone_unknown_clock_is_cited_but_not_ordered(self):
        case = case_named("boundary_cases", "timezone_unknown_clock")
        bundle, event, tracked = self.run_case(case)
        self.assertIsNone(event["message_date"])
        self.assertEqual(event["message_date_status"], "timezone_unknown")
        assert_data_citation(self, event["message_date_citation"], bundle, case["date_pointer"],
                             "Tue, 04 Feb 2031 10:15:00 -0000")
        row = tracked["requests"][0]["timeline"][0]
        self.assertIsNone(row["time_sort"])
        self.assertEqual(row["time_status"], "message_date_timezone_unknown")


class MixedUnlocatableTests(unittest.TestCase):
    """An uncited same-field value is never resolved and never silently dropped."""

    def check_mixed(self, case):
        bundle = receipt_bundle(case["name"], case["headers"])
        events = events_for(correspondence, bundle)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(sorted(h["reason"] for h in event["metadata_holds"]), sorted(case["holds"]))
        self.assertEqual(event["message_id"], case["message_id"])
        if case["message_id"] is None:
            self.assertIsNone(event["message_id_citation"])
        else:
            assert_data_citation(self, event["message_id_citation"], bundle, case["id_pointer"],
                                 case["message_id"])
        tracked = correspondence.track(events, as_of=CASES["as_of"], bundle=bundle)
        self.assertEqual(sorted(h["reason"] for h in tracked["holds"]), sorted(case["holds"]))
        row = tracked["requests"][0]["timeline"][0]
        if case["date_resolved"]:
            self.assertEqual(event["message_date"], CASES["date_normalized"])
            self.assertEqual(event["message_date_status"], "explicit_offset")
            assert_data_citation(self, event["message_date_citation"], bundle, case["date_pointer"],
                                 CASES["date_value"])
            self.assertEqual(row["time_status"], "message_date_explicit_offset")
        else:
            self.assertIsNone(event["message_date"])
            self.assertIsNone(event["message_date_citation"])
            self.assertEqual(event["message_date_status"], "unknown")
            self.assertIsNone(row["message_date"])
            self.assertIsNone(row["time_sort"])
            self.assertNotEqual(row["time_status"], "message_date_explicit_offset")
        if case["message_id"] is None:
            self.assertIsNone(row["message_id_citation"])
            self.assertTrue(row["deduplication_key"].startswith("sha256:"))
        else:
            self.assertEqual(row["deduplication_key"], "message:" + case["message_id"].casefold())
        return bundle, event

    def test_mixed_pair_raw_contradictory_dates_stay_unknown(self):
        self.check_mixed(case_named("mixed_unlocatable_cases", "mixed_pair_raw_contradictory_dates"))

    def test_mixed_pair_raw_contradictory_ids_stay_unknown(self):
        self.check_mixed(case_named("mixed_unlocatable_cases", "mixed_pair_raw_contradictory_ids"))

    def test_stripped_dict_list_contradictory_dates_stay_unknown(self):
        self.check_mixed(case_named("mixed_unlocatable_cases", "stripped_dict_list_contradictory_dates"))

    def test_mixed_agreeing_date_keeps_cited_value(self):
        self.check_mixed(case_named("mixed_unlocatable_cases", "mixed_pair_raw_agreeing_date_positive"))

    def test_mixed_agreeing_id_keeps_cited_value(self):
        self.check_mixed(case_named("mixed_unlocatable_cases", "mixed_pair_raw_agreeing_id_positive"))

    def test_mixed_conflict_original_keeps_valid_peer(self):
        held = case_named("mixed_unlocatable_cases", "mixed_pair_raw_contradictory_dates")
        peer = case_named("addressable_cases", "dict_scalar")
        held_bundle = receipt_bundle(held["name"], held["headers"])
        peer_bundle = receipt_bundle(peer["name"], peer["headers"], text=CASES["peer_body_text"])
        bundle = merged_bundle(held_bundle, peer_bundle)
        events = events_for(correspondence, bundle)
        self.assertEqual(len(events), 2)
        by_original = {event["source_original_sha256"]: event for event in events}
        held_event = by_original[held_bundle["units"][0]["original_sha256"]]
        peer_event = by_original[peer_bundle["units"][0]["original_sha256"]]
        self.assertIsNone(held_event["message_date"])
        self.assertEqual(held_event["message_id"], held["message_id"])
        self.assertEqual(peer_event["metadata_holds"], [])
        self.assertEqual(peer_event["message_date"], CASES["date_normalized"])
        assert_data_citation(self, peer_event["message_date_citation"], bundle, peer["date_pointer"],
                             CASES["date_value"])
        tracked = correspondence.track(events, as_of=CASES["as_of"], bundle=bundle)
        dated = [row for row in tracked["requests"][0]["timeline"] if row["message_date"]]
        self.assertEqual([row["message_date"] for row in dated], [CASES["date_normalized"]])
        self.assertEqual(dated[0]["message_id_citation"]["json_pointer"], peer["id_pointer"])


class TextBackedCompatibilityTests(unittest.TestCase):
    def check_text_backed(self, case):
        bundle = receipt_bundle(case["name"], case["headers"], text=case["text"])
        events = events_for(correspondence, bundle)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["metadata_holds"], [])
        self.assertEqual(event["message_id"], case["message_id"])
        self.assertEqual(event["message_date"], CASES["date_normalized"])
        for cite, value in ((event["message_date_citation"], CASES["date_value"]),
                            (event["message_id_citation"], case["message_id"])):
            self.assertEqual(cite["schema"], TEXT_SCHEMA)
            self.assertIsNone(cite["json_pointer"])
            self.assertEqual(cite["quote"], value)
            start = case["text"].encode("utf-8").find(value.encode("utf-8"))
            self.assertEqual(cite["span"], {"start_byte": start, "end_byte": start + len(value.encode("utf-8"))})
        tracked = correspondence.track(events, as_of=CASES["as_of"], bundle=bundle)
        self.assertEqual(tracked["holds"], [])
        self.assertEqual(tracked["requests"][0]["timeline"][0]["time_status"], "message_date_explicit_offset")

    def test_text_backed_dict_headers_keep_text_span(self):
        self.check_text_backed(case_named("text_backed_cases", "text_backed_dict"))

    def test_text_backed_raw_strings_keep_text_span(self):
        self.check_text_backed(case_named("text_backed_cases", "text_backed_raw_strings"))


class NegativeControlTests(unittest.TestCase):
    def test_declared_negative_control_legacy_pointer_seam(self):
        observed = run_negative_control()
        self.assertTrue(observed["detected"], observed)
        self.assertEqual(observed["pair_under_injection"], "passed")
        self.assertTrue(observed["restored_is_original"])
        self.assertEqual(observed["restored_dict_and_pair"], "passed")


if __name__ == "__main__":
    unittest.main()
