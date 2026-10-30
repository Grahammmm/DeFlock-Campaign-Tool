"""Synthetic qualified native workbook units; no workbook files or live stores."""
import unittest

from campaign_tool.records.understanding import rows
from campaign_tool.records.detectors.core import canonical
from tests.test_understanding_rows import args_for, h, producer_json, reasons, STAMP

COLUMNS = [("ID", "event_id", "str"), ("When", "event_at", "str"),
           ("Reason", "purpose", "str"), ("Days", "retention_days", "int"),
           ("Enabled", "access_enabled", "bool")]
LETTERS = ["A", "C", "Z", "AA", "AB"]
HOLD = "workbook_cached_values_and_objects_not_reviewed"


def row(number, values, sheet="Log"):
    cells = [{"cell": f"{letter}{number}", "value": value,
              "type": "b" if type(value) is bool else "n" if type(value) is int else "s"}
             for letter, value in zip(LETTERS, values)]
    return {"kind": "workbook_row", "locator": {"sheet": sheet, "row": number},
            "text": producer_json(cells), "data": {"cells": cells}}


def fixture(issues=True):
    receipt = {"schema": "format-extraction-v1", "original_sha256": h("synthetic-xlsx"),
               "original_changed": False, "status": "complete", "review_status": "not_reviewed",
               "issues": [{"code": HOLD}] if issues else [], "pages": [], "units": [
                   {"kind": "workbook_sheet", "locator": {"sheet": "Log"}, "text": "Log",
                    "data": {"state": "visible"}},
                   row(1, [c[0] for c in COLUMNS]),
                   row(2, ["event-1", STAMP, "specific", 30, True]),
                   row(3, ["event-2", STAMP, "other", 0, False])]}
    mapping = {"schema": rows.MAPPING_SCHEMA, "version": "synthetic-xlsx-v1",
               "artifact_sha256": receipt["original_sha256"], "agency_id": "agency-A",
               "record_type": "search-log", "unit_kind": "workbook_row", "sheet": "Log",
               "delimiter": ",", "columns": [{"name": n, "target": t, "type": c}
                                            for n, t, c in COLUMNS]}
    return receipt, mapping


def normalize(receipt, mapping):
    # Rebind the text and receipt/inventory hashes after synthetic mutations.
    for unit in receipt["units"]:
        if unit["kind"] == "workbook_row":
            unit["text"] = producer_json(unit["data"]["cells"])
    return rows.normalize(**args_for(receipt, mapping))


class WorkbookRowsTests(unittest.TestCase):
    def test_cited_typed_rows_and_preserved_receipt_hold(self):
        for held in (True, False):
            with self.subTest(held=held):
                receipt, mapping = fixture(held)
                output = normalize(receipt, mapping)
                self.assertEqual(output["normalized"]["complete"], not held)
                self.assertEqual(output["upstream"], [{"artifact_sha256": receipt["original_sha256"],
                                 "reason": "upstream_not_clear"}] if held else [])
                self.assertTrue(all(d["status"] == "mapped" for d in output["diagnostics"]))
                records = sorted((i["record"] for i in output["normalized"]["units"]
                                  if i["record"]["record_type"] != "other"), key=lambda r: r["event_id"])
                for number, record in enumerate(records, 2):
                    self.assertEqual(record["locator"], {"sheet": "Log", "row": number})
                    self.assertEqual(record["event_id"], f"event-{number - 1}")
                    self.assertEqual(record["event_at"], STAMP)
                    self.assertEqual(record["fields"], {
                        "purpose": {"state": "observed", "value": "specific" if number == 2 else "other"},
                        "retention_days": {"state": "observed", "value": 30 if number == 2 else 0},
                        "access_enabled": {"state": "observed", "value": number == 2}})
                self.assertEqual(len(records), 2)
                self.assertEqual(len(output["events"]), 2)
                for item in output["normalized"]["units"]:
                    if item["record"]["record_type"] == "other":
                        self.assertEqual(item["record"]["event_id"], item["source"]["unit_id"])
                self.assertTrue(output["review_required"])
                for flag in ("qualified", "activation_allowed", "publication_ready"):
                    self.assertIs(output[flag], False)
                self.assertEqual(canonical(output), canonical(normalize(receipt, mapping)))

    def test_hidden_sheet_marks_every_row_and_keeps_run_incomplete(self):
        for state in ("hidden", "veryHidden"):
            with self.subTest(state=state):
                receipt, mapping = fixture(False)
                receipt["units"][0]["data"]["state"] = state
                output = normalize(receipt, mapping)
                self.assertFalse(output["normalized"]["complete"])
                self.assertEqual(output["upstream"], [{"artifact_sha256": receipt["original_sha256"],
                                                      "reason": "workbook_hidden_sheet"}])
                by_id = {d["unit_id"]: d for d in output["diagnostics"]}
                for item in output["normalized"]["units"]:
                    if "row" in item["record"]["locator"]:
                        self.assertIs(by_id[item["source"]["unit_id"]]["hidden_sheet"], True)
                self.assertTrue(all(d["status"] == "mapped" for d in output["diagnostics"]))

    def test_exact_header_labels_order_and_column_binding(self):
        for change in ("order", "label", "missing", "duplicate"):
            with self.subTest(change=change):
                receipt, mapping = fixture(False)
                header = receipt["units"][1]
                if change == "order":
                    cells = header["data"]["cells"]
                    cells[0]["value"], cells[1]["value"] = cells[1]["value"], cells[0]["value"]
                elif change == "label":
                    header["data"]["cells"][0]["value"] = "id"
                elif change == "missing":
                    receipt["units"].remove(header)
                else:
                    receipt["units"].append(header)
                output = normalize(receipt, mapping)
                self.assertIn("header_mapping_mismatch", reasons(output))
                self.assertFalse(output["normalized"]["complete"])
        receipt, mapping = fixture(False)
        # Serialized order is not column order; wide and sparse header letters bind exactly.
        for unit in receipt["units"][1:]:
            unit["data"]["cells"].reverse()
        self.assertTrue(normalize(receipt, mapping)["normalized"]["complete"])

    def test_unmapped_and_absent_cells(self):
        receipt, mapping = fixture(False)
        receipt["units"][2]["data"]["cells"].append({"cell": "B2", "value": "extra", "type": "s"})
        output = normalize(receipt, mapping)
        self.assertIn("unmapped_columns", reasons(output))
        self.assertFalse(output["normalized"]["complete"])
        receipt, mapping = fixture(False)
        receipt["units"][2]["data"]["cells"].pop(2)
        output = normalize(receipt, mapping)
        record = next(i["record"] for i in output["normalized"]["units"]
                      if i["record"]["event_id"] == "event-1")
        self.assertEqual(record["fields"]["purpose"], {"state": "missing"})
        self.assertEqual(record["fields"]["retention_days"], {"state": "observed", "value": 30})
        self.assertTrue(output["normalized"]["complete"])

    def test_cell_qualification_defence(self):
        for mutation in ({"type": "f"}, {"cell": "A3"}, {"extra": True},
                         {"cell": "a2"}, {"value": None}):
            for index in (1, 2):
                with self.subTest(mutation=mutation, index=index):
                    receipt, mapping = fixture(False)
                    receipt["units"][index]["data"]["cells"][0].update(mutation)
                    output = normalize(receipt, mapping)
                    self.assertIn("workbook_cell_unqualified", reasons(output))
                    self.assertFalse(output["normalized"]["complete"])

    def test_native_cell_types_never_coerced(self):
        for index, value, codec in [(3, True, "int"), (3, "30", "int"), (3, 30.0, "int"),
                                    (4, 1, "bool"), (4, "true", "bool"), (2, 7, "str"),
                                    (2, "[]", "json")]:
            with self.subTest(value=value, codec=codec):
                receipt, mapping = fixture(False)
                receipt["units"][2]["data"]["cells"][index]["value"] = value
                mapping["columns"][index]["type"] = codec
                output = normalize(receipt, mapping)
                self.assertIn("cell_type_invalid", reasons(output))
                self.assertFalse(output["normalized"]["complete"])

    def test_json_column_is_invalid_even_when_cell_is_absent(self):
        receipt, mapping = fixture(False)
        mapping["columns"][2]["type"] = "json"
        receipt["units"][2]["data"]["cells"].pop(2)
        output = normalize(receipt, mapping)
        record = next(i for i in output["normalized"]["units"]
                      if i["record"]["event_id"] == "event-1")
        diagnostic = next(d for d in output["diagnostics"] if d["unit_id"] == record["source"]["unit_id"])
        self.assertIn("cell_type_invalid", diagnostic["reasons"])

    def test_string_blank_and_redacted_states(self):
        for value, state in [("", "blank"), ("  ", "blank"), ("[redacted]", "redacted")]:
            receipt, mapping = fixture(False)
            receipt["units"][2]["data"]["cells"][2]["value"] = value
            output = normalize(receipt, mapping)
            record = next(i["record"] for i in output["normalized"]["units"]
                          if i["record"]["event_id"] == "event-1")
            self.assertEqual(record["fields"]["purpose"], {"state": state})
            self.assertTrue(output["normalized"]["complete"])

    def test_sheet_unit_required_and_other_sheet_rows_retained(self):
        receipt, mapping = fixture(False)
        receipt["units"].pop(0)
        output = normalize(receipt, mapping)
        self.assertEqual(reasons(output), {"workbook_sheet_unbound"})
        self.assertFalse(output["normalized"]["complete"])
        receipt, mapping = fixture(False)
        receipt["units"].extend([
            {"kind": "workbook_sheet", "locator": {"sheet": "Other"}, "text": "Other",
             "data": {"state": "visible"}},
            row(1, [c[0] for c in COLUMNS], "Other"),
            row(2, ["other-event", STAMP, "other", 1, True], "Other")])
        output = normalize(receipt, mapping)
        self.assertEqual(len(output["normalized"]["units"]), len(receipt["units"]))
        self.assertEqual(reasons(output), {"unit_mapping_unresolved"})
        self.assertFalse(output["normalized"]["complete"])
        self.assertEqual(sum(d["status"] == "unresolved" for d in output["diagnostics"]), 2)

    def test_mapping_sheet_is_workbook_only_and_comma_required(self):
        for key, value, reason in [("unit_kind", "csv_row", "mapping_shape"),
                                  ("sheet", "", "mapping_sheet"), ("sheet", "s" * 32, "mapping_sheet"),
                                  ("sheet", 1, "mapping_sheet"), ("delimiter", "|", "mapping_delimiter")]:
            with self.subTest(key=key, value=value):
                receipt, mapping = fixture(False)
                mapping[key] = value
                with self.assertRaisesRegex(rows.RowsError, "^" + reason + "$"):
                    normalize(receipt, mapping)

    def test_workbook_events_keep_existing_conflict_and_timestamp_rules(self):
        for conflict in (False, True):
            receipt, mapping = fixture(False)
            receipt["units"][3] = row(3, ["event-1", STAMP, "changed" if conflict else "specific", 30, True])
            output = normalize(receipt, mapping)
            self.assertEqual(output["normalized"]["complete"], not conflict)
            self.assertEqual(output["events"][0]["duplicate_rows"], 1)
            self.assertEqual(len(output["events"][0]["sources"]), 2)
            if conflict:
                self.assertEqual(reasons(output), {"conflicting_duplicate_event"})
        receipt, mapping = fixture(False)
        receipt["units"][2]["data"]["cells"][1]["value"] = "2020-01-01"
        output = normalize(receipt, mapping)
        self.assertIn("event_timezone_or_time_unresolved", reasons(output))
        self.assertFalse(output["normalized"]["complete"])


if __name__ == "__main__":
    unittest.main()
