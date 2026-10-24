"""Synthetic source-shaped row tests; no originals, stores, subprocesses or IO."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from campaign_tool.records.understanding import rows
from campaign_tool.records.detectors.core import canonical, digest
from campaign_tool.records.detectors.pipeline import CanonicalJoins, evaluate_structured

STAMP = "2020-01-20T12:00:00+00:00"
COLUMNS = [("ID", "event_id", "str"), ("When", "event_at", "str"),
           ("Reason", "purpose", "str"), ("User", "user_id", "str")]


def h(value):
    return hashlib.sha256(value if type(value) is bytes else value.encode()).hexdigest()


def producer_json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def presence(value):
    # Independently frozen producer cases used by synthetic receipts.
    if value == "":
        return "empty"
    if not value.strip():
        return "whitespace"
    if value in {"[redacted]", "[withheld]", "REDACTED", "xxx", "██"}:
        return "redacted_marker"
    return "present"


def fixture(values=None, *, kind="csv_row", columns=COLUMNS, subject="synthetic-source", agency="agency-A"):
    values = [["event-1", STAMP, "investigation", "user-1"]] if values is None else values
    original = h(subject)
    raw_units = []
    if kind == "csv_row":
        raw_units.append({"kind": "csv_header", "locator": {"record": 1, "line_start": 1, "line_end": 1},
                          "text": producer_json([c[0] for c in columns]), "data": {}})
    for n, cells in enumerate(values, 2 if kind == "csv_row" else 1):
        if kind == "csv_row":
            text = producer_json(cells)
            data = {"values": cells, "presence": [presence(v) for v in cells], "duplicate_of_record": None,
                    "row_sha256": h(text), "column_mapping": "positional"}
            locator = {"record": n, "line_start": n, "line_end": n}
        else:
            text = "|".join(cells)
            data = {}
            locator = {"page": 2, "line": n} if kind == "pdf_page" else {"line": n}
        raw_units.append({"kind": kind, "locator": locator, "text": text, "data": data})
    receipt = {"schema": "format-extraction-v1", "original_sha256": original, "original_changed": False,
               "status": "complete", "review_status": "not_reviewed", "issues": [], "pages": [],
               "units": raw_units}
    mapping = {"schema": rows.MAPPING_SCHEMA, "version": "synthetic-mapping-v1", "artifact_sha256": original,
               "agency_id": agency, "record_type": "search-log", "unit_kind": kind, "delimiter": "|",
               "columns": [{"name": n, "target": t, "type": c} for n, t, c in columns]}
    return args_for(receipt, mapping), receipt


def args_for(receipt, mapping):
    raw = (producer_json(receipt) + "\n").encode()
    original, receipt_hash = receipt["original_sha256"], h(raw)
    sources = []
    # Construct inventory independently from the unit() implementation, following
    # the pre-existing receipt-bound-derivative identity contract (not rows.py).
    imp = h(json.dumps(["receipt-bound-derivative-v1", original, receipt_hash, "native"],
                       sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    for ordinal, unit in enumerate(receipt["units"], 1):
        artifact = h(json.dumps(unit, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        identity = h(json.dumps([imp, ordinal, artifact], separators=(",", ":")))
        sources.append({"unit_id": identity, "original_sha256": original,
                        "artifact_sha256": artifact, "locator": copy.deepcopy(unit["locator"])})
    return {"run_id": "synthetic-run-1115", "receipts": [{"artifact_sha256": original,
            "derived_receipt_sha256": receipt_hash, "derived_receipt": raw}],
            "source_bindings": sources, "mappings": [copy.deepcopy(mapping)]}


def records(output):
    return [i["record"] for i in output["normalized"]["units"] if i["record"]["record_type"] != "other"]


def reasons(output):
    return {r for d in output["diagnostics"] for r in d["reasons"]}


class RowTests(unittest.TestCase):
    def test_exact_csv_contract_identity_and_states(self):
        args, _ = fixture()
        before = copy.deepcopy(args)
        result = rows.normalize(**args)
        self.assertTrue(result["normalized"]["complete"])
        self.assertEqual(result["normalized"]["schema"], "structured-normalization-v1")
        self.assertEqual(result["normalized"]["source_inventory_sha256"], digest(args["source_bindings"]))
        self.assertEqual(result["source_bindings"], args["source_bindings"])
        source = args["source_bindings"][1]
        self.assertEqual(records(result), [{"original_sha256": h("synthetic-source"),
            "locator": {"record": 2, "line_start": 2, "line_end": 2}, "record_type": "search-log",
            "event_id": "event-1", "event_at": STAMP,
            "fields": {"purpose": {"state": "observed", "value": "investigation"},
                       "user_id": {"state": "observed", "value": "user-1"}}}])
        item = next(i for i in result["normalized"]["units"] if i["record"]["event_id"] == "event-1")
        self.assertEqual(item["source"], {k: source[k] for k in ("unit_id", "artifact_sha256")})
        self.assertEqual(result["source_receipts"][0]["derived_receipt_sha256"], args["receipts"][0]["derived_receipt_sha256"])
        self.assertEqual(args, before)
        self.assertEqual(result, rows.normalize(**args))
        result["source_bindings"][0]["locator"]["record"] = 999
        self.assertEqual(args, before)

    def test_producer_whitespace_and_redaction_vocabulary(self):
        for value, expected in [("", "blank"), ("  ", "blank"), ("[redacted]", "redacted"),
                                ("[withheld]", "redacted"), ("REDACTED", "redacted"),
                                ("xxx", "redacted"), ("██", "redacted")]:
            with self.subTest(value=value):
                args, _ = fixture([["event-1", STAMP, value, "user-1"]])
                result = rows.normalize(**args)
                self.assertEqual(records(result)[0]["fields"]["purpose"], {"state": expected})
                self.assertTrue(result["normalized"]["complete"])

    def test_text_missing_vs_blank_and_page_line_locators(self):
        for kind in ("text_line", "pdf_page"):
            args, _ = fixture([["event-1", STAMP, ""]], kind=kind)
            result = rows.normalize(**args)
            self.assertTrue(result["normalized"]["complete"])
            record = records(result)[0]
            self.assertEqual(record["fields"], {"purpose": {"state": "blank"}, "user_id": {"state": "missing"}})
            self.assertEqual(record["locator"], args["source_bindings"][0]["locator"])

    def test_identity_cell_states_not_inferred(self):
        for index, value, state, reason in [(0, "[redacted]", "redacted", "stable_event_identity_missing"),
                (0, "", "blank", "stable_event_identity_missing"),
                (1, "", "blank", "event_timezone_or_time_unresolved")]:
            cells = ["event-1", STAMP, "specific", "user-1"]
            cells[index] = value
            args, _ = fixture([cells], kind="text_line")
            result = rows.normalize(**args)
            self.assertFalse(result["normalized"]["complete"])
            self.assertIn(reason, reasons(result))
            key = "event_id" if index == 0 else "event_at"
            self.assertEqual(result["diagnostics"][0]["cells"][key], {"state": state})
            self.assertIsNone(records(result)[0][key])

    def test_unknown_mapping_and_columns_remain_unknown(self):
        for mutation in ("mapping", "column", "extra"):
            args, _ = fixture(kind="text_line")
            if mutation == "mapping":
                args["mappings"] = []
            elif mutation == "column":
                args["mappings"][0]["columns"][2]["target"] = None
            else:
                args["mappings"][0]["columns"] = args["mappings"][0]["columns"][:3]
            result = rows.normalize(**args)
            self.assertFalse(result["normalized"]["complete"])
            self.assertEqual(records(result)[0]["record_type"], "unknown")
            self.assertEqual(records(result)[0]["fields"], {})

    def test_unknown_target_and_type_are_refused(self):
        for key, value, reason in [("target", "guessed_field", "mapping_target"),
                                  ("type", "guessed", "mapping_cell_type")]:
            args, _ = fixture()
            args["mappings"][0]["columns"][2][key] = value
            with self.assertRaisesRegex(rows.RowsError, reason):
                rows.normalize(**args)

    def test_invalid_timezone_class_and_valid_offset(self):
        for stamp in ["2020-01-20T12:00:00", "2020-01-20", "2020-01-20T12:00:00+01:99",
                      "2020-01-20T12:00:00+24:00", "2020-13-20T12:00:00Z", "nonsense"]:
            args, _ = fixture([["event-1", stamp, "specific", "user-1"]])
            output = rows.normalize(**args)
            self.assertFalse(output["normalized"]["complete"])
            self.assertIn("event_timezone_or_time_unresolved", reasons(output))
        for stamp in (STAMP, "2020-01-20T12:00:00Z", "2020-01-20T12:00:00-07:30"):
            args, _ = fixture([["event-1", stamp, "specific", "user-1"]])
            self.assertTrue(rows.normalize(**args)["normalized"]["complete"])

    def test_duplicate_evidence_retained_and_conflicts_unresolved(self):
        for conflict in (False, True):
            cells = ["event-1", STAMP, "investigation", "user-1"]
            second = cells.copy()
            if conflict:
                second[2] = "specific"
            args, _ = fixture([cells, second])
            output = rows.normalize(**args)
            self.assertEqual(len(records(output)), 2)
            event = output["events"][0]
            self.assertEqual(event["identity"], ["agency-A", "search-log", "event-1"])
            self.assertEqual(event["duplicate_rows"], 1)
            self.assertEqual(len(event["sources"]), 2)
            self.assertEqual(output["normalized"]["complete"], not conflict)
            if conflict:
                self.assertEqual(reasons(output), {"conflicting_duplicate_event"})
                self.assertTrue(all(d["status"] == "unresolved" for d in output["diagnostics"] if d["cells"]))

    def test_distinct_agencies_types_and_cross_receipt_dedupe(self):
        left, _ = fixture(subject="left")
        right, _ = fixture(subject="right")
        for agency, kind, expected in [("agency-A", "search-log", 1), ("agency-B", "search-log", 2),
                                        ("agency-A", "network-search", 2)]:
            args = copy.deepcopy(left)
            other = copy.deepcopy(right)
            other["mappings"][0].update(agency_id=agency, record_type=kind)
            for key in ("receipts", "source_bindings", "mappings"):
                args[key] += other[key]
            output = rows.normalize(**args)
            self.assertEqual(len(output["events"]), expected)
            # Receipt/mapping enumeration cannot affect the output bytes.
            args["receipts"].reverse()
            args["mappings"].reverse()
            self.assertEqual(output, rows.normalize(**args))

    def test_header_missing_mismatched_and_workbook_never_accepted(self):
        for change in ("missing", "wrong", "workbook"):
            args, receipt = fixture()
            if change == "missing":
                receipt["units"] = receipt["units"][1:]
            elif change == "wrong":
                receipt["units"][0]["text"] = '["unmapped"]'
            else:
                receipt["units"][1]["kind"] = "workbook_row"
                receipt["units"][1]["locator"] = {"sheet": "search log", "row": 2}
            args = args_for(receipt, args["mappings"][0])
            output = rows.normalize(**args)
            self.assertFalse(output["normalized"]["complete"])
            self.assertIn("unit_mapping_unresolved" if change == "workbook" else "header_mapping_mismatch", reasons(output))

    def test_upstream_partial_issues_and_review_claims(self):
        for key, value in [("status", "partial"), ("status", "blocked"), ("issues", [{"code": "ocr_needed"}])]:
            args, receipt = fixture()
            receipt[key] = value
            output = rows.normalize(**args_for(receipt, args["mappings"][0]))
            self.assertFalse(output["normalized"]["complete"])
            self.assertEqual(output["upstream"][0]["reason"], "upstream_not_clear")
        args, receipt = fixture()
        receipt["review_status"] = "accepted"
        with self.assertRaisesRegex(rows.RowsError, "receipt_status_shape"):
            rows.normalize(**args_for(receipt, args["mappings"][0]))

    def test_tampering_hash_subject_and_inventory_fail_closed(self):
        for change, reason in [("receipt", "derived_receipt_hash_mismatch"), ("subject", "artifact_receipt_mismatch"),
                ("unit_hash", "source_inventory_binding_mismatch"), ("locator", "source_inventory_binding_mismatch"),
                ("missing_inventory", "source_inventory_binding_mismatch"),
                ("extra_inventory", "source_inventory_coverage_mismatch")]:
            args, _ = fixture()
            if change == "receipt":
                args["receipts"][0]["derived_receipt"] += b" "
            elif change == "subject":
                args["receipts"][0]["artifact_sha256"] = h("different-original")
            elif change == "unit_hash":
                args["source_bindings"][0]["artifact_sha256"] = h("different-unit")
            elif change == "locator":
                args["source_bindings"][0]["locator"] = {"record": 100}
            elif change == "missing_inventory":
                args["source_bindings"] = []
            else:
                extra = copy.deepcopy(args["source_bindings"][0])
                extra["unit_id"] = "not-supplied"
                args["source_bindings"].append(extra)
            with self.subTest(change=change), self.assertRaisesRegex(rows.RowsError, reason):
                rows.normalize(**args)

    def test_native_cell_metadata_mismatch_and_ragged_rows_unresolved(self):
        for key, value, reason in [("presence", ["present"] * 3, "csv_presence_binding"),
                ("values", ["other"], "csv_positional_binding"),
                ("row_sha256", h("wrong"), "csv_row_hash"),
                ("column_mapping", "ambiguous", "csv_positional_binding")]:
            args, receipt = fixture()
            receipt["units"][1]["data"][key] = value
            output = rows.normalize(**args_for(receipt, args["mappings"][0]))
            self.assertFalse(output["normalized"]["complete"])
            self.assertIn(reason, reasons(output))
        args, _ = fixture([["event-1", STAMP, "specific"]])
        self.assertIn("csv_ragged_row", reasons(rows.normalize(**args)))

    def test_explicit_typed_conversions_and_invalid_types(self):
        columns = COLUMNS[:2] + [("Days", "retention_days", "int"), ("Enabled", "access_enabled", "bool"),
                                ("Dates", "audit_dates", "json")]
        args, _ = fixture([["event-1", STAMP, "31", "true", '["2020-01-01"]']], columns=columns)
        fields = records(rows.normalize(**args))[0]["fields"]
        self.assertEqual(fields, {"retention_days": {"state": "observed", "value": 31},
            "access_enabled": {"state": "observed", "value": True},
            "audit_dates": {"state": "observed", "value": ["2020-01-01"]}})
        for index, value in [(2, "31.0"), (3, "yes"), (4, '{"not":"list"}')]:
            cells = ["event-1", STAMP, "31", "true", "[]"]
            cells[index] = value
            args, _ = fixture([cells], columns=columns)
            result = rows.normalize(**args)
            self.assertFalse(result["normalized"]["complete"])
            self.assertIn("cell_type_invalid", reasons(result))

    def test_malformed_and_bounded_inputs_typed_refusal(self):
        for change in ("nan", "malformed_mapping", "duplicate_source", "duplicate_mapping", "huge"):
            args, _ = fixture()
            if change == "nan":
                args["mappings"][0]["bad"] = float("nan")
            elif change == "malformed_mapping":
                args["mappings"] = [None]
            elif change == "duplicate_source":
                args["source_bindings"] *= 2
            elif change == "duplicate_mapping":
                args["mappings"] *= 2
            else:
                args["receipts"][0]["derived_receipt"] = b" " * (rows.MAX_INPUT_BYTES + 1)
            with self.subTest(change=change), self.assertRaises(rows.RowsError):
                rows.normalize(**args)

    def test_real_adapter_deduplication_and_missing_redacted_controls(self):
        cells = ["event-1", STAMP, "investigation", "user-1"]
        args, _ = fixture([cells, cells])
        result = rows.normalize(**args)
        subject = args["receipts"][0]["artifact_sha256"]
        policy = {"detector": "purpose-quality", "agency_id": "agency-A", "rule_id": "rule-1",
            "version": "synthetic-v1", "source_sha256": subject, "effective_from": "2020-01-01",
            "effective_to": "2021-01-01", "parameters": {"generic_reasons": ["investigation"]},
            "version_verified": True, "applicability": "yes"}
        def evaluate(output):
            joins = CanonicalJoins(digest(output["source_bindings"]), canonical([
                {"original_sha256": subject, "status": "typed", "agency_id": "agency-A"}]), h("synthetic-revision"))
            return evaluate_structured(subject_sha256=subject, normalized=output["normalized"],
                source_bindings=output["source_bindings"], joins=joins,
                rules={"version": "synthetic-v1", "entries": [policy]}, config={})
        evaluated = evaluate(result)["structured"]["purpose-quality"]
        self.assertEqual(evaluated["status"], "evaluated")
        self.assertEqual(evaluated["manifest"]["counts"]["evaluated"], 1)
        self.assertEqual(len(evaluated["hits"][0]["evidence"]), 2)
        args, _ = fixture([["event-1", STAMP, "[redacted]", "user-1"]])
        blocked = evaluate(rows.normalize(**args))["structured"]["purpose-quality"]
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["hits"], [])
        self.assertEqual(blocked["manifest"]["counts"]["blocked"], 1)
        args["mappings"] = []
        self.assertTrue(all(r["status"] == "blocked" for r in evaluate(rows.normalize(**args))["structured"].values()))

    def test_control_proves_source_binding_gate_decides(self):
        args, _ = fixture()
        self.assertTrue(rows.normalize(**args)["normalized"]["complete"])
        args["source_bindings"][1]["artifact_sha256"] = h("changed")
        with self.assertRaisesRegex(rows.RowsError, "source_inventory_binding_mismatch"):
            rows.normalize(**args)
        original_require = rows.require
        def disabled_binding_only(ok, reason):
            if reason != "source_inventory_binding_mismatch":
                original_require(ok, reason)
        with patch.object(rows, "require", disabled_binding_only):
            self.assertTrue(rows.normalize(**args)["normalized"]["complete"])
        # Gate is restored; the same synthetic input fails again.
        with self.assertRaisesRegex(rows.RowsError, "source_inventory_binding_mismatch"):
            rows.normalize(**args)

    def test_text_quoting_multiline_and_ambiguous_rows(self):
        args, receipt = fixture(kind="text_line")
        for text, expected, reason in [
                ('event-1|' + STAMP + '|"specific|case"|user-1', "specific|case", None),
                ('event-1|' + STAMP + '|"specific\ncase"|user-1', "specific\ncase", None),
                ('event-1|' + STAMP + '|specific|user-1\nsecond|row', None, "text_table_row_ambiguous"),
                ('event-1|' + STAMP + '|"unterminated|user-1', None, "text_table_parse")]:
            receipt["units"][0]["text"] = text
            output = rows.normalize(**args_for(receipt, args["mappings"][0]))
            if reason:
                self.assertFalse(output["normalized"]["complete"])
                self.assertIn(reason, reasons(output))
                self.assertEqual(records(output)[0]["fields"], {})
            else:
                self.assertTrue(output["normalized"]["complete"])
                self.assertEqual(records(output)[0]["fields"]["purpose"],
                                 {"state": "observed", "value": expected})

    def test_no_acceptance_or_activation_from_machine_valid_output(self):
        args, _ = fixture()
        result = rows.normalize(**args)
        self.assertTrue(result["review_required"])
        for field in ("qualified", "activation_allowed", "publication_ready"):
            self.assertIs(result[field], False)
        self.assertEqual(result["stage_promotions"], 0)


if __name__ == "__main__":
    unittest.main()
