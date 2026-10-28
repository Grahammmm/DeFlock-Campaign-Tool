"""Fictional producer-shaped CSV receipts; no canonical or acquisition work.

Authored by gpt-6.1-sol / openai-codex for FC15. Schema/identity formulas
are independently implemented from the pinned producer and evidence contracts;
no tests or planner probes are imported. Ordinary discovery has no global hooks.
"""
import argparse
from contextlib import contextmanager, ExitStack
import copy
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc15-row-header-cardinality/cases.json"
CASE_IDS = (
    "one_matching", "zero_headers", "two_equal",
    "two_conflicting_matching_first", "two_conflicting_matching_last", "foreign_header",
)


def sha(raw):
    return hashlib.sha256(raw if isinstance(raw, bytes) else raw.encode()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def producer(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


@contextmanager
def offline_scope(temporary_root=None):
    """Reversible sentinels precede product imports, including ledger closure.

    Static closure: rows -> evidence -> grounded_evidence -> retained_units;
    rows -> persistence -> ledger.store -> migrations.v001; pipeline -> core.
    Those modules have no filesystem-bound defaults or import-time IO. SQLite,
    subprocess and sockets are nevertheless denied throughout their use here.
    These scoped mocks are not a hostile-code sandbox.
    """
    def denied(*args, **kwargs):
        raise RuntimeError("fc15_offline_sentinel")
    with ExitStack() as stack:
        for owner, name in ((socket, "socket"), (socket, "create_connection"),
                            (socket, "getaddrinfo"), (sqlite3, "connect"),
                            (subprocess, "Popen"), (os, "system")):
            stack.enter_context(patch.object(owner, name, denied))
        stack.enter_context(patch.object(sys, "dont_write_bytecode", True))
        if temporary_root is not None:
            root = Path(temporary_root).resolve(strict=True)
            stack.enter_context(patch.dict(os.environ, {"TMPDIR": str(root)}))
            stack.enter_context(patch.object(tempfile, "tempdir", str(root)))
        yield


def fixture():
    value = json.loads(FIXTURE.read_text())
    unittest.TestCase().assertEqual(value["schema"], "fc15-row-header-cardinality/v1")

    unittest.TestCase().assertEqual(value["cases"], {
        "one_matching": ["matching"], "zero_headers": [],
        "two_equal": ["matching", "matching"],
        "two_conflicting_matching_first": ["matching", "conflicting"],
        "two_conflicting_matching_last": ["conflicting", "matching"], "foreign_header": []})
    return value


def artifact(headers, *, subject, config):
    """Producer folder.py CSV shape, with distinct retained physical positions."""
    original = sha(subject)
    units = []
    for position, label in enumerate(headers, 1):
        names = [c["name"] for c in config["columns"]]
        if label == "conflicting":
            names = list(reversed(names))
        units.append({"kind": "csv_header", "locator": {
            "record": position, "line_start": position, "line_end": position},
            "text": producer(names), "data": {}})
    position = len(units) + 1
    values = copy.deepcopy(config["values"])
    text = producer(values)
    units.append({"kind": "csv_row", "locator": {
        "record": position, "line_start": position, "line_end": position},
        "text": text, "data": {"values": values, "presence": ["present"] * len(values),
        "duplicate_of_record": None, "row_sha256": sha(text), "column_mapping": "positional"}})
    receipt = {"schema": "format-extraction-v1", "original_sha256": original,
               "original_changed": False, "status": "complete", "review_status": "not_reviewed",
               "issues": [], "pages": [], "units": units}
    raw = (producer(receipt) + "\n").encode()
    receipt_hash = sha(raw)
    import_hash = sha(canonical(["receipt-bound-derivative-v1", original, receipt_hash, "native"]))
    inventory = []
    for ordinal, unit in enumerate(units, 1):
        payload_hash = sha(canonical(unit))
        inventory.append({"unit_id": sha(canonical([import_hash, ordinal, payload_hash])),
                          "original_sha256": original, "artifact_sha256": payload_hash,
                          "locator": copy.deepcopy(unit["locator"])})
    mapping = {"schema": "agency-row-mapping-v1", "version": "fictional-v1",
               "artifact_sha256": original, "agency_id": subject, "record_type": "search-log",
               "unit_kind": "csv_row", "delimiter": ",", "columns": copy.deepcopy(config["columns"])}
    return {"receipts": [{"artifact_sha256": original, "derived_receipt_sha256": receipt_hash,
                           "derived_receipt": raw}], "source_bindings": inventory, "mappings": [mapping]}


def case_args(case):
    config = fixture()
    args = artifact(config["cases"][case], subject="fictional-A", config=config)
    if case == "foreign_header":
        other = artifact(["matching"], subject="fictional-B", config=config)
        for key in args:
            args[key] += other[key]
    return {"run_id": "fictional-fc15", **args}


class HeaderCardinalityTests(unittest.TestCase):
    def setUp(self):
        self.scope = offline_scope()
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.rows = importlib.import_module("campaign_tool.records.understanding.rows")

    def check_preservation(self, args, before, result):
        self.assertEqual(args, before)
        self.assertEqual(result, self.rows.normalize(**args))
        self.assertEqual(result["source_bindings"], args["source_bindings"])
        expected = {s["unit_id"]: s for s in args["source_bindings"]}
        self.assertEqual(len(result["normalized"]["units"]), len(expected))
        self.assertEqual(len(result["diagnostics"]), len(expected))
        self.assertEqual({d["unit_id"] for d in result["diagnostics"]}, set(expected))
        for item in result["normalized"]["units"]:
            source = expected[item["source"]["unit_id"]]
            self.assertEqual(item["source"], {k: source[k] for k in ("unit_id", "artifact_sha256")})
            self.assertEqual(item["record"]["original_sha256"], source["original_sha256"])
            self.assertEqual(item["record"]["locator"], source["locator"])
        self.assertEqual(result["source_receipts"], sorted([
            {k: r[k] for k in ("artifact_sha256", "derived_receipt_sha256")}
            for r in args["receipts"]], key=lambda r: r["artifact_sha256"]))
        self.assertEqual(result["upstream"], [])
        self.assertIs(result["review_required"], True)
        for key in ("qualified", "activation_allowed", "publication_ready"):
            self.assertIs(result[key], False)
        self.assertEqual(result["stage_promotions"], 0)

    def check_case(self, case):
        args = case_args(case)
        before = copy.deepcopy(args)
        result = self.rows.normalize(**args)
        diagnostics = {d["unit_id"]: d for d in result["diagnostics"]}

        expected_events = []
        for mapping, pin in zip(args["mappings"], args["receipts"]):
            receipt = json.loads(pin["derived_receipt"])
            sources = [s for s in args["source_bindings"]
                       if s["original_sha256"] == mapping["artifact_sha256"]]
            for unit, source in zip(receipt["units"], sources):
                uid = source["unit_id"]
                item = next(i for i in result["normalized"]["units"] if i["source"]["unit_id"] == uid)
                diagnostic = diagnostics[uid]
                self.assertEqual(diagnostic["agency_id"], mapping["agency_id"])
                is_row = unit["kind"] == "csv_row"
                mapped = ((case == "one_matching" or mapping["agency_id"] == "fictional-B")
                          if is_row else json.loads(unit["text"]) == [c["name"] for c in mapping["columns"]])
                self.assertEqual(diagnostic["reasons"], [] if mapped else ["header_mapping_mismatch"],
                                 msg=case + ":cardinality")
                self.assertEqual(diagnostic["status"], "mapped" if mapped else "unresolved")
                record = {"original_sha256": source["original_sha256"], "locator": source["locator"],
                          "record_type": "unknown", "event_id": None, "event_at": None, "fields": {}}
                cells = {}
                if mapped and is_row:
                    values = fixture()["values"]
                    record.update(record_type="search-log", event_id=values[0], event_at=values[1],
                                  fields={"purpose": {"state": "observed", "value": values[2]}})
                    cells = {c["target"]: {"state": "observed", "value": v}
                             for c, v in zip(mapping["columns"], values)}
                    expected_events.append({"identity": [mapping["agency_id"], "search-log", values[0]],
                        "status": "mapped", "reason": "same_semantic_event", "duplicate_rows": 0,
                        "sources": [{k: source[k] for k in ("unit_id", "artifact_sha256")}]})
                elif mapped:
                    record.update(record_type="other", event_id=uid)
                self.assertEqual(item["record"], record)
                self.assertEqual(diagnostic["cells"], cells)
        self.assertIs(result["normalized"]["complete"], case == "one_matching")
        self.assertEqual(result["events"], sorted(expected_events, key=lambda e: e["identity"]))
        self.check_preservation(args, before, result)
        return result

    def test_one_matching(self):
        self.check_case("one_matching")

    def test_zero_headers(self):
        self.check_case("zero_headers")

    def test_two_equal(self):
        self.check_case("two_equal")

    def test_two_conflicting(self):
        for case in ("two_conflicting_matching_first", "two_conflicting_matching_last"):
            with self.subTest(case=case):
                self.check_case(case)

    def test_foreign_header(self):
        self.check_case("foreign_header")

    def test_preservation(self):
        for case in CASE_IDS:
            with self.subTest(case=case):
                self.check_case(case)

    def test_deciding_cardinality_control(self):
        # Copy every statement, changing only the cardinality comparison.
        source = inspect.getsource(self.rows._normalize)
        self.assertEqual(source.count("len(bound_headers) == 1"), 1)
        namespace = dict(vars(self.rows))
        exec(compile(source.replace("len(bound_headers) == 1", "len(bound_headers) >= 1"),
                     "<fc15-isolated-cardinality-control>", "exec"), namespace)
        with patch.object(self.rows, "_normalize", namespace["_normalize"]):
            with self.subTest(control="weakened_one_matching_green"):
                self.check_case("one_matching")
            # The precise row diagnostic assertion, NOT a fixture/import failure.
            with self.subTest(control="weakened_two_equal_named_assertion_fails"):
                with self.assertRaisesRegex(AssertionError, "two_equal:cardinality") as caught:
                    self.check_case("two_equal")
                print("CONTROL_EXPECTED_ASSERTION " + str(caught.exception), file=sys.stderr)
        with self.subTest(control="restored_one_matching_green"):
            self.check_case("one_matching")
        with self.subTest(control="restored_two_equal_green"):
            self.check_case("two_equal")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--temporary-root", type=Path, required=True)
    options = parser.parse_args()
    sys.path.insert(0, str(REPO))
    with offline_scope(options.temporary_root):
        unittest.main(argv=[str(Path(__file__).resolve()), "-v"])
