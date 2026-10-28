"""Fictional FC13 matrix, authored by actual gpt-6.1-sol / openai-codex.

Ordinary discovery is inert: no environment/default mutation or audit hooks.
Script setup processes its temporary root and offline mocks before product imports.
"""
import copy
import hashlib
import json
from pathlib import Path
import unittest

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc13-receipt-positions/cases.json"
CASES = json.loads(FIXTURE.read_text())["cases"]


def product():
    from campaign_tool.records.understanding import evidence
    return evidence


def digest(label):
    return hashlib.sha256(("FC13 fictional " + label).encode()).hexdigest()


def rehash(value):
    ev = product()
    body = {k: v for k, v in value.items() if k != "snapshot_sha256"}
    return {**body, "snapshot_sha256": ev.sha(body)}


def build(case):
    ev = product()
    units, receipts, context = [], [], {}
    for row in case["receipts"]:
        subject, receipt = digest(row["original"]), digest(row["receipt"])
        for index, ordinal in enumerate(row["positions"], 1):
            # Payloads differ even when positions repeat: never a duplicate-unit test.
            raw = {"kind": "text", "locator": {"fictional_row": index},
                   "text": "Invented row %d" % index, "data": {}}
            units.append(ev.unit(raw, subject, receipt, ordinal))
        receipts.append({"original_sha256": subject, "receipt_sha256": receipt,
                         "attempt_kind": "native", "declared_unit_count": row["declared"],
                         "normalized_unit_count": len(row["positions"])})
        context[subject] = {"review_acceptance": False, "semantic_acceptance": False}
    return rehash({"schema": ev.VERSION, "units": units,
                   "source_context": context, "source_receipts": receipts})


class PositionTests(unittest.TestCase):
    def check_case(self, case):
        ev = product()
        value = build(case)
        before = copy.deepcopy(value)
        # Qualify all identities and receipt counts independently before the deciding gate.
        self.assertEqual(len({u["unit_id"] for u in value["units"]}), len(value["units"]))
        for u in value["units"]:
            raw = {k: u[k] for k in ("kind", "locator", "text", "data")}
            self.assertEqual(u, ev.unit(raw, u["original_sha256"], u["receipt_sha256"], u["ordinal"]))
        bound = {}
        for u in value["units"]:
            key = (u["original_sha256"], u["receipt_sha256"])
            bound[key] = bound.get(key, 0) + 1
        ev._check_receipt_counts({(r["original_sha256"], r["receipt_sha256"]): r
                                  for r in value["source_receipts"]}, bound)
        if case["expected"] == "accept":
            self.assertIs(ev.validate_bundle(value), value)
        else:
            with self.assertRaises(ValueError) as caught:
                ev.validate_bundle(value)
            self.assertEqual(caught.exception.args, (case["expected"],))
        self.assertEqual(value, before)

    def test_native_gapped_normalization(self):
        ev = product()
        raw = {"schema": "format-extraction-v1", "original_sha256": digest("A"),
               "original_changed": False, "pages": [], "units": [
                   {"kind": "text", "locator": {"page": 1}, "text": "Invented first", "data": {}},
                   {"kind": "text", "locator": {"page": 2}, "text": None, "data": {}},
                   {"kind": "text", "locator": {"page": 3}, "text": "Invented last", "data": {}}]}
        value = ev.normalize_receipt(raw, digest("A"), ev.sha(raw))
        self.assertEqual([u["ordinal"] for u in value["units"]], [1, 3])
        self.assertEqual(value["source_receipts"][0]["declared_unit_count"], 3)
        self.assertFalse(value["source_context"][digest("A")]["semantic_acceptance"])
        self.assertIs(ev.validate_bundle(value), value)

    def rejection(self, value, code):
        with self.assertRaises(ValueError) as caught:
            product().validate_bundle(value)
        self.assertEqual(caught.exception.args, (code,))

    def test_existing_duplicate_unit(self):
        value = build(CASES[0])
        value["units"].append(copy.deepcopy(value["units"][0]))
        value["source_receipts"][0].update(declared_unit_count=4, normalized_unit_count=4)
        self.rejection(rehash(value), "duplicate_unit")

    def test_existing_hash_checks(self):
        value = build(CASES[0])
        value["units"][0]["text_sha256"] = digest("forged")
        self.rejection(rehash(value), "unit_binding_mismatch")
        value = build(CASES[0])
        value["units"][0]["text"] = "changed"
        self.rejection(value, "bundle_snapshot_mismatch")

    def test_count_shapes_precede_positions(self):
        for change, code in [({"declared_unit_count": True}, "receipt_count_type"),
                             ({"declared_unit_count": -1}, "receipt_count_negative"),
                             ({"normalized_unit_count": 1}, "receipt_count_mismatch")]:
            with self.subTest(change=change):
                value = build(next(c for c in CASES if c["id"] == "duplicate"))
                value["source_receipts"][0].update(change)
                self.rejection(rehash(value), code)
        value = build(next(c for c in CASES if c["id"] == "beyond_limit"))
        del value["source_receipts"][0]["declared_unit_count"]
        self.rejection(rehash(value), "receipt_count_missing")

    def test_existing_exact_positive_ordinal(self):
        ev = product()
        for ordinal in [0, -1, True, 1.0, "1", None]:
            with self.subTest(ordinal=ordinal):
                value = build(CASES[0])
                value["units"][0]["ordinal"] = ordinal
                self.rejection(rehash(value), "unit_ordinal")


for _case in CASES:
    def _test(self, case=_case):
        self.check_case(case)
    setattr(PositionTests, "test_case_" + _case["id"], _test)


if __name__ == "__main__":
    import argparse
    import os
    import socket
    import sys
    import tempfile
    from unittest import mock

    parser = argparse.ArgumentParser()
    parser.add_argument("--temporary-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.temporary_root.resolve(strict=True)
    if not root.is_dir():
        raise SystemExit("temporary_root_required")
    os.environ["TMPDIR"] = str(root)
    tempfile.tempdir = str(root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))

    def offline(*args, **kwargs):
        raise RuntimeError("offline_network_forbidden")

    with mock.patch.object(socket.socket, "connect", offline), \
         mock.patch.object(socket.socket, "connect_ex", offline), \
         mock.patch.object(socket.socket, "bind", offline), \
         mock.patch.object(socket, "getaddrinfo", offline):
        unittest.main(argv=[sys.argv[0]], verbosity=2)
