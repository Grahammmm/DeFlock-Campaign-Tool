"""Pure CSV/text row mapping over pinned extraction copies; never extraction.

The caller admits mappings and supplies the coordinator inventory independently.
Kind candidates do not select mappings. All occurrences remain in the existing
normalization envelope so the core can dedupe without losing source evidence.
This module has no filesystem, ledger, network, inference or acceptance API.
"""
import csv
import hashlib
import io
import json
import re

from . import evidence
from ..detectors.core import MAX_INPUT_BYTES, MAX_UNITS, canonical, digest, moment, reference
from ..detectors.persistence import _preflight
from ..detectors.pipeline import KNOWN_TYPES, NORMALIZED_SCHEMA, _sources

VERSION = "explicit-csv-text-rows-v1"
MAPPING_SCHEMA = "agency-row-mapping-v1"
TEXT_KINDS = {"text_line", "pdf_page"}
PAGE_OK = "ok"
FIELD_NAMES = frozenset({
    "user_id", "purpose", "training_completed_at", "access_enabled",
    "recipient_level", "recipient_jurisdiction", "retention_mode", "retention_days",
    "period_start", "period_end", "production_complete", "audit_dates",
    "request_received", "determination_state", "determination_at", "extension_claimed",
    "extension_days", "extension_notice_at", "extension_basis", "production_at",
})
TARGETS = FIELD_NAMES | {"event_id", "event_at"}


class RowsError(ValueError):
    """Stable refusal without source text in its message."""


def require(ok, reason):
    if not ok:
        raise RowsError(reason)


def _decode(raw):
    try:
        return evidence.decode(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RowsError("invalid_receipt_json") from None


def _mapping(value):
    workbook = type(value) is dict and value.get("unit_kind") == "workbook_row"
    keys = {"schema", "version", "artifact_sha256", "agency_id", "record_type",
            "unit_kind", "delimiter", "columns"} | ({"sheet"} if workbook else set())
    require(type(value) is dict and set(value) == keys, "mapping_shape")
    if workbook:
        require(type(value["sheet"]) is str and 1 <= len(value["sheet"]) <= 31, "mapping_sheet")
        require(value["delimiter"] == ",", "mapping_delimiter")
    require(value["schema"] == MAPPING_SCHEMA, "mapping_schema")
    for key in ("version", "agency_id"):
        require(type(value[key]) is str and 0 < len(value[key].strip()) <= 256,
                "mapping_identity")
    require(evidence.is_hash(value["artifact_sha256"]), "mapping_artifact_hash")
    require(value["record_type"] in KNOWN_TYPES - {"other"}, "mapping_record_type")
    require(value["unit_kind"] in TEXT_KINDS | {"csv_row", "workbook_row"}, "mapping_unit_kind")
    require(type(value["delimiter"]) is str and len(value["delimiter"]) == 1
            and value["delimiter"] not in '\r\n\x00"', "mapping_delimiter")
    columns = value["columns"]
    require(type(columns) is list and 0 < len(columns) <= 256, "mapping_columns")
    targets, names = set(), set()
    for column in columns:
        require(type(column) is dict and set(column) == {"name", "target", "type"},
                "mapping_column_shape")
        name, target = column["name"], column["target"]
        require(type(name) is str and 0 < len(name) <= 256 and name not in names,
                "mapping_column_identity")
        names.add(name)
        require(target is None or (type(target) is str and target in TARGETS
                and target not in targets), "mapping_target")
        if target is not None:
            targets.add(target)
        require(column["type"] in {"str", "int", "bool", "json"}, "mapping_cell_type")
        if target in {"event_id", "event_at"}:
            require(column["type"] == "str", "mapping_identity_type")
    return value


def _presence(value):
    # Exact producer vocabulary from intake/folder.py:209-215, no imports/IO.
    if value is None:
        return "missing_field"
    if value == "":
        return "empty"
    if not value.strip():
        return "whitespace"
    if re.fullmatch(r"(?i)\s*(?:\[?redacted\]?|\[?withheld\]?|x{3,}|\u2588+)\s*", value):
        return "redacted_marker"
    return "present"


def _cell(value, codec):
    if value is None:
        return {"state": "missing"}
    require(type(value) is str, "cell_not_text")
    if not value.strip():
        return {"state": "blank"}
    if _presence(value) == "redacted_marker":
        return {"state": "redacted"}
    try:
        converted = value
        if codec == "int":
            require(re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is not None, "cell_type_invalid")
            converted = int(value)
        elif codec == "bool":
            require(value in {"true", "false"}, "cell_type_invalid")
            converted = value == "true"
        elif codec == "json":
            converted = evidence.decode(value)
            require(type(converted) is list, "cell_type_invalid")
        return {"state": "observed", "value": converted}
    except (ValueError, TypeError, RecursionError):
        raise RowsError("cell_type_invalid") from None


def _workbook_cells(unit):
    cells = unit["data"].get("cells")
    require(type(cells) is list, "workbook_cell_unqualified")
    bound = {}
    row = unit["locator"].get("row")
    require(type(row) is int and row > 0, "workbook_cell_unqualified")
    for cell in cells:
        require(type(cell) is dict and set(cell) == {"cell", "value", "type"}
                and cell["type"] != "f" and cell["value"] is not None
                and type(cell["cell"]) is str, "workbook_cell_unqualified")
        match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", cell["cell"])
        require(match is not None and int(match[2]) == row and match[1] not in bound,
                "workbook_cell_unqualified")
        bound[match[1]] = cell["value"]
    return dict(sorted(bound.items(), key=lambda item: (len(item[0]), item[0])))


def _workbook_values(unit, mapping, headers, sheets):
    require(len(sheets) == 1 and sheets[0]["data"].get("state") in
            {"visible", "hidden", "veryHidden"}, "workbook_sheet_unbound")
    require(len(headers) == 1, "header_mapping_mismatch")
    header = _workbook_cells(headers[0])
    require(list(header.values()) == [c["name"] for c in mapping["columns"]]
            and all(type(v) is str for v in header.values()), "header_mapping_mismatch")
    cells = _workbook_cells(unit)
    require(set(cells) <= set(header), "unmapped_columns")
    return [cells.get(letter) for letter in header]


def _workbook_cell(value, codec):
    require(codec != "json", "cell_type_invalid")
    if value is None:
        return {"state": "missing"}
    require(codec in {"str", "int", "bool"} and
            type(value) is {"str": str, "int": int, "bool": bool}.get(codec), "cell_type_invalid")
    return _cell(value, codec) if codec == "str" else {"state": "observed", "value": value}


def _values(unit, mapping):
    if unit["kind"] == "csv_row":
        values = _decode(unit["text"])
        data = unit["data"]
        require(type(values) is list and all(type(v) is str for v in values), "csv_values_shape")
        require(data.get("values") == values and data.get("column_mapping") == "positional",
                "csv_positional_binding")
        expected = [_presence(v) for v in values]
        require(data.get("presence") == expected, "csv_presence_binding")
        # folder.py's js() uses ordinary JSON separators, unlike core canonical().
        raw = json.dumps(values, ensure_ascii=True, sort_keys=True, default=str)
        require(data.get("row_sha256") == hashlib.sha256(raw.encode()).hexdigest(), "csv_row_hash")
        return values
    try:
        parsed = list(csv.reader(io.StringIO(unit["text"], newline=""),
                                 delimiter=mapping["delimiter"], strict=True))
    except csv.Error:
        raise RowsError("text_table_parse") from None
    require(len(parsed) == 1, "text_table_row_ambiguous")
    return parsed[0]


def _page_clear(page):
    # Producer page vocabulary (extraction_routes.page_manifest): only "ok" is clear;
    # a present needs_visual_review must be exactly False. Unknown stays not clear.
    if not (type(page.get("status")) is str and page["status"] == PAGE_OK):
        return False
    return "needs_visual_review" not in page or page["needs_visual_review"] is False


def _normalize(*, run_id, receipts, source_bindings, mappings):
    """Return existing normalized/source-bindings shapes plus separate diagnostics.

    receipts: [{artifact_sha256, derived_receipt_sha256, derived_receipt: bytes}].
    Expected hashes and mappings are producer/owner inputs, not self-attestations.
    No authoritative inventory is generated from the worker's emitted units.
    Incomplete/unknown/conflicting inputs keep complete=false for all consumers.
    Explicit page entries must be objects; any page not "ok", or whose present
    needs_visual_review is not exactly False, keeps complete=false (rows kept).
    Integrity failures raise RowsError and yield no partially accepted output.
    """
    require(type(run_id) is str and 0 < len(run_id.strip()) <= 256, "run_id_required")
    require(type(receipts) is list and 0 < len(receipts) <= MAX_UNITS, "receipts_required")
    require(type(mappings) is list, "mappings_required")
    try:
        _preflight([], [], {}, {"mappings": mappings, "sources": source_bindings})
        sources = _sources(source_bindings)
        require(len(canonical([mappings, source_bindings]).encode()) <= MAX_INPUT_BYTES,
                "mapping_context_bound")
    except (ValueError, TypeError, KeyError, RecursionError):
        raise RowsError("invalid_mapping_or_inventory") from None
    by_original = {}
    for mapping in mappings:
        _mapping(mapping)
        key = mapping["artifact_sha256"]
        require(key not in by_original, "duplicate_mapping")
        by_original[key] = mapping
    units, receipt_refs, upstream, seen = [], [], [], set()
    byte_count = 0
    for pin in receipts:
        require(type(pin) is dict and set(pin) == {
            "artifact_sha256", "derived_receipt_sha256", "derived_receipt"}, "receipt_pin_shape")
        subject, expected, raw = (pin[k] for k in (
            "artifact_sha256", "derived_receipt_sha256", "derived_receipt"))
        require(evidence.is_hash(subject) and evidence.is_hash(expected), "receipt_pin_hash")
        require(type(raw) is bytes, "receipt_bytes_required")
        byte_count += len(raw)
        require(byte_count <= MAX_INPUT_BYTES, "receipt_byte_bound")
        require(hashlib.sha256(raw).hexdigest() == expected, "derived_receipt_hash_mismatch")
        require(subject not in seen, "duplicate_receipt_artifact")
        seen.add(subject)
        receipt = _decode(raw)
        require(type(receipt) is dict and receipt.get("schema") == "format-extraction-v1"
                and receipt.get("original_sha256") == subject
                and receipt.get("original_changed") is False, "artifact_receipt_mismatch")
        require(receipt.get("status") in {"complete", "partial", "blocked"}
                and receipt.get("review_status") == "not_reviewed"
                and type(receipt.get("issues")) is list
                and type(receipt.get("pages")) is list
                and type(receipt.get("units")) is list, "receipt_status_shape")
        require(len(units) + len(receipt["units"]) <= MAX_UNITS, "unit_count_bound")
        if receipt["status"] != "complete" or receipt["issues"]:
            upstream.append({"artifact_sha256": subject, "reason": "upstream_not_clear"})
        for page in receipt["pages"]:
            require(type(page) is dict, "receipt_page_shape")
        # Explicit page states only: an empty list keeps prior behaviour; no page count is inferred.
        if not all(_page_clear(page) for page in receipt["pages"]):
            upstream.append({"artifact_sha256": subject, "reason": "upstream_pages_not_clear"})
        for ordinal, raw_unit in enumerate(receipt["units"], 1):
            try:
                unit = evidence.unit(raw_unit, subject, expected, ordinal)
                reference(unit)
            except (ValueError, TypeError, KeyError, RecursionError):
                raise RowsError("extraction_unit_invalid") from None
            require(unit["unit_id"] in sources and sources[unit["unit_id"]] == {
                k: unit[k] for k in ("unit_id", "original_sha256", "artifact_sha256", "locator")},
                "source_inventory_binding_mismatch")
            units.append(unit)
        receipt_refs.append({"artifact_sha256": subject, "derived_receipt_sha256": expected})
    require(set(by_original) <= seen, "mapping_source_missing")
    require({u["unit_id"] for u in units} == set(sources), "source_inventory_coverage_mismatch")
    normalized, diagnostics, grouped = [], [], {}
    headers, workbook_headers, workbook_sheets = {}, {}, {}
    for unit in units:
        if unit["kind"] == "csv_header":
            headers.setdefault(unit["original_sha256"], []).append(unit)
        key = (unit["original_sha256"], unit["locator"].get("sheet"))
        if unit["kind"] == "workbook_sheet":
            workbook_sheets.setdefault(key, []).append(unit)
        elif unit["kind"] == "workbook_row" and unit["locator"].get("row") == 1:
            workbook_headers.setdefault(key, []).append(unit)
    hidden = set()
    for subject, mapping in by_original.items():
        if mapping["unit_kind"] == "workbook_row":
            key = (subject, mapping["sheet"])
            if any(u["data"].get("state") in {"hidden", "veryHidden"}
                   for u in workbook_sheets.get(key, [])):
                hidden.add(key)
                upstream.append({"artifact_sha256": subject, "reason": "workbook_hidden_sheet"})
    for unit in sorted(units, key=lambda u: u["unit_id"]):
        subject, uid = unit["original_sha256"], unit["unit_id"]
        mapping = by_original.get(subject)
        workbook = mapping is not None and mapping["unit_kind"] == "workbook_row"
        key = (subject, mapping["sheet"]) if workbook else None
        record = {"original_sha256": subject, "locator": unit["locator"],
                  "record_type": "unknown", "event_id": None, "event_at": None, "fields": {}}
        reasons, cell_states = [], {}
        agency = mapping["agency_id"] if mapping else None
        if mapping is None:
            reasons.append("mapping_unresolved")
        elif workbook and unit["kind"] == "workbook_sheet":
            record["record_type"], record["event_id"] = "other", uid
        elif workbook and unit["locator"].get("sheet") != mapping["sheet"]:
            reasons.append("unit_mapping_unresolved")
        elif unit["kind"] == "csv_header" and mapping["unit_kind"] == "csv_row":
            if _decode(unit["text"]) == [c["name"] for c in mapping["columns"]]:
                record["record_type"] = "other"  # Explicitly bound structural header only.
                record["event_id"] = uid
            else:
                reasons.append("header_mapping_mismatch")
        elif unit["kind"] != mapping["unit_kind"]:
            reasons.append("unit_mapping_unresolved")
        else:
            try:
                if unit["kind"] == "csv_row":
                    bound_headers = headers.get(subject, [])
                    require(len(bound_headers) == 1 and _decode(bound_headers[0]["text"]) ==
                            [c["name"] for c in mapping["columns"]], "header_mapping_mismatch")
                values = (_workbook_values(unit, mapping, workbook_headers.get(key, []),
                          workbook_sheets.get(key, [])) if workbook else _values(unit, mapping))
                names = [c["name"] for c in mapping["columns"]]
                if (workbook and unit["locator"]["row"] == 1) or (unit["kind"] in TEXT_KINDS and values == names):
                    # Only an exact declared header is structural, never an event.
                    record["record_type"] = "other"
                    record["event_id"] = uid
                else:
                    if unit["kind"] in TEXT_KINDS:
                        # A permutation of all exact labels is a mismatched header,
                        # not observed data. No trimming/case folding/label guessing.
                        require(not (len(values) == len(names) and set(values) == set(names)),
                                "header_mapping_mismatch")
                    require(len(values) <= len(mapping["columns"]), "unmapped_columns")
                    if unit["kind"] == "csv_row":
                        require(len(values) == len(mapping["columns"]), "csv_ragged_row")
                    require(all(c["target"] is not None for c in mapping["columns"]), "unmapped_columns")
                    record["record_type"] = mapping["record_type"]
                    for index, column in enumerate(mapping["columns"]):
                        state = (_workbook_cell if workbook else _cell)(
                            values[index] if index < len(values) else None, column["type"])
                        target = column["target"]
                        cell_states[target] = state
                        if target in {"event_id", "event_at"}:
                            record[target] = state.get("value") if state["state"] == "observed" else None
                        else:
                            record["fields"][target] = state
                    identity = record["event_id"]
                    require(type(identity) is str and 0 < len(identity.strip()) <= 256,
                            "stable_event_identity_missing")
                    try:
                        stamp = record["event_at"]
                        require(type(stamp) is str, "event_timezone_or_time_unresolved")
                        match = re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(Z|[+-]([0-9]{2}):([0-9]{2}))", stamp)
                        require(match is not None and (match[1] == "Z" or
                                (int(match[2]) <= 23 and int(match[3]) <= 59)),
                                "event_timezone_or_time_unresolved")
                        moment(record["event_at"])
                    except ValueError:
                        raise RowsError("event_timezone_or_time_unresolved") from None
            except RowsError as error:
                reasons.append(str(error))
        item = {"record": record, "source": {"unit_id": uid, "artifact_sha256": unit["artifact_sha256"]}}
        normalized.append(item)
        diagnostic = {"unit_id": uid, "agency_id": agency, "reasons": reasons, "cells": cell_states,
                      "status": "unresolved" if reasons else "mapped"}
        if unit["kind"] == "workbook_row" and (subject, unit["locator"].get("sheet")) in hidden:
            diagnostic["hidden_sheet"] = True
        diagnostics.append(diagnostic)
        if record["event_id"] is not None and record["record_type"] != "other":
            key = canonical([agency, record["record_type"], record["event_id"]])
            semantics = {k: v for k, v in record.items() if k not in {"original_sha256", "locator"}}
            grouped.setdefault(key, []).append((digest(semantics), item, diagnostic))
    events = []
    for key, occurrences in sorted(grouped.items()):
        conflict = len({row[0] for row in occurrences}) > 1
        if conflict:
            for _, _, diagnostic in occurrences:
                diagnostic["status"] = "unresolved"
                diagnostic["reasons"].append("conflicting_duplicate_event")
        events.append({"identity": json.loads(key), "status": "unresolved" if conflict or any(
            d["reasons"] for _, _, d in occurrences) else "mapped",
            "reason": "conflicting_duplicate_event" if conflict else "same_semantic_event",
            "duplicate_rows": len(occurrences) - 1,
            "sources": [item["source"] for _, item, _ in occurrences]})
    complete = not upstream and all(d["status"] == "mapped" for d in diagnostics)
    result = {"normalized": {"schema": NORMALIZED_SCHEMA, "version": VERSION,
              "complete": complete, "source_inventory_sha256": digest(source_bindings), "units": normalized},
              "source_bindings": source_bindings, "run_id": run_id,
              "mapping_sha256": digest(sorted(mappings, key=lambda m: m["artifact_sha256"])),
              "source_receipts": sorted(receipt_refs, key=lambda r: r["artifact_sha256"]),
              "diagnostics": diagnostics, "events": events, "upstream": sorted(upstream, key=canonical),
              "review_required": True, "qualified": False, "activation_allowed": False,
              "publication_ready": False, "stage_promotions": 0}
    require(len(canonical(result).encode()) <= MAX_INPUT_BYTES, "output_byte_bound")
    return json.loads(canonical(result))


def normalize(*, run_id, receipts, source_bindings, mappings):
    """See _normalize for the frozen input contract; malformed input fails closed."""
    try:
        return _normalize(run_id=run_id, receipts=receipts,
                          source_bindings=source_bindings, mappings=mappings)
    except RowsError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
        raise RowsError("invalid_rows_context") from None
