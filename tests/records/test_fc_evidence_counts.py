"""FC-EVIDENCE-COUNTS: receipt counts must agree with the bound units.

Every fixture here is synthetic. Hashes are derived from labelled fixture
bytes, never from real records, and no original record is read.
"""
import copy
import hashlib
import unittest

from campaign_tool.records.understanding import evidence


def label_hash(label):
    return hashlib.sha256(("fc-evidence-counts fixture: " + label).encode()).hexdigest()


ORIGINAL_A = label_hash("original A")
ORIGINAL_B = label_hash("original B")
RECEIPT_A1 = label_hash("receipt A1")
RECEIPT_A2 = label_hash("receipt A2")
RECEIPT_B1 = label_hash("receipt B1")

COUNT_CODES = {
    "receipt_count_missing",
    "receipt_count_type",
    "receipt_count_negative",
    "receipt_count_exceeds_declared",
    "receipt_count_mismatch",
}


def raw_unit(n):
    return {"kind": "text", "locator": {"page": n}, "text": "Synthetic page %d text." % n, "data": {}}


def make_units(subject, receipt, count, attempt_kind="native"):
    return [evidence.unit(raw_unit(n), subject, receipt, n, attempt_kind) for n in range(1, count + 1)]


def receipt_row(subject, receipt, declared, normalized, attempt_kind="native"):
    return {"original_sha256": subject, "receipt_sha256": receipt, "attempt_kind": attempt_kind,
            "declared_unit_count": declared, "normalized_unit_count": normalized}


def context(declared, normalized, gaps=()):
    return {"format": "pdf", "native_status": "complete", "pages": [], "declared_unit_count": declared,
            "normalized_unit_count": normalized, "normalization_gaps": list(gaps), "parser": "synthetic",
            "parser_version": "0", "review_acceptance": False, "semantic_acceptance": False}


def snapshot(units, source_context, source_receipts):
    """Build an otherwise valid, correctly rehashed snapshot without validating it."""
    body = {"schema": evidence.VERSION,
            "units": sorted(units, key=lambda u: (u["original_sha256"], u["receipt_sha256"], u["ordinal"])),
            "source_context": source_context,
            "source_receipts": sorted(source_receipts, key=lambda r: (r["original_sha256"], r["receipt_sha256"]))}
    return {**body, "snapshot_sha256": evidence.sha(body)}


def rehash(value):
    body = {k: v for k, v in value.items() if k != "snapshot_sha256"}
    return {**body, "snapshot_sha256": evidence.sha(body)}


def two_unit():
    return snapshot(make_units(ORIGINAL_A, RECEIPT_A1, 2), {ORIGINAL_A: context(2, 2)},
                    [receipt_row(ORIGINAL_A, RECEIPT_A1, 2, 2)])


def native_receipt(units):
    return {"schema": "format-extraction-v1", "original_sha256": ORIGINAL_A, "original_changed": False,
            "status": "complete", "parser": "synthetic", "parser_version": "0", "route": {"format": "pdf"},
            "pages": [{"page_no": 1, "status": "ok"}], "units": units}


class CodeAssertions:
    def assert_code(self, value, code):
        with self.assertRaises(ValueError) as caught:
            evidence.validate_bundle(value)
        # Stable reason code only: no counts, hashes or source payload in diagnostics.
        self.assertEqual(caught.exception.args, (code,))
        self.assertEqual(str(caught.exception), code)


class ReceiptCountPositiveTests(CodeAssertions, unittest.TestCase):
    def test_two_unit_positive(self):
        value = two_unit()
        self.assertIs(evidence.validate_bundle(value), value)

    def test_zero_unit_receipt_is_valid(self):
        value = snapshot([], {ORIGINAL_A: context(0, 0)}, [receipt_row(ORIGINAL_A, RECEIPT_A1, 0, 0)])
        self.assertIs(evidence.validate_bundle(value), value)

    def test_native_zero_unit_receipt_is_valid(self):
        raw = native_receipt([])
        value = evidence.normalize_receipt(raw, ORIGINAL_A, evidence.sha(raw))
        self.assertEqual(value["units"], [])
        self.assertEqual(value["source_receipts"][0]["normalized_unit_count"], 0)

    def test_honest_partial_receipt_is_valid(self):
        broken = {"kind": "text", "locator": {"page": 3}, "text": 7, "data": {}}
        raw = native_receipt([raw_unit(1), raw_unit(2), broken])
        value = evidence.normalize_receipt(raw, ORIGINAL_A, evidence.sha(raw))
        row = value["source_receipts"][0]
        self.assertEqual((row["declared_unit_count"], row["normalized_unit_count"]), (3, 2))
        self.assertEqual(value["source_context"][ORIGINAL_A]["normalization_gaps"],
                         [{"code": "unit_normalization_failed", "ordinal": 3}])
        self.assertIs(evidence.validate_bundle(value), value)
        self.assertFalse(value["source_context"][ORIGINAL_A]["semantic_acceptance"])

    def test_hand_built_partial_receipt_is_valid(self):
        value = snapshot(make_units(ORIGINAL_A, RECEIPT_A1, 2),
                         {ORIGINAL_A: context(5, 2, [{"code": "unit_normalization_failed", "ordinal": 3}])},
                         [receipt_row(ORIGINAL_A, RECEIPT_A1, 5, 2)])
        self.assertIs(evidence.validate_bundle(value), value)

    def test_multi_original_multi_receipt_bundle(self):
        units = (make_units(ORIGINAL_A, RECEIPT_A1, 2) + make_units(ORIGINAL_A, RECEIPT_A2, 1, "ocr")
                 + make_units(ORIGINAL_B, RECEIPT_B1, 3))
        receipts = [receipt_row(ORIGINAL_A, RECEIPT_A1, 2, 2), receipt_row(ORIGINAL_A, RECEIPT_A2, 4, 1, "ocr"),
                    receipt_row(ORIGINAL_B, RECEIPT_B1, 3, 3), receipt_row(ORIGINAL_B, RECEIPT_A2, 0, 0)]
        value = snapshot(units, {ORIGINAL_A: context(2, 2), ORIGINAL_B: context(3, 3)}, receipts)
        self.assertIs(evidence.validate_bundle(value), value)

    def test_multi_receipt_counts_are_per_pair(self):
        units = make_units(ORIGINAL_A, RECEIPT_A1, 2) + make_units(ORIGINAL_B, RECEIPT_B1, 1)
        # Totals still match (3 == 2 + 1) but the pairs are swapped.
        receipts = [receipt_row(ORIGINAL_A, RECEIPT_A1, 2, 1), receipt_row(ORIGINAL_B, RECEIPT_B1, 2, 2)]
        value = snapshot(units, {ORIGINAL_A: context(2, 2), ORIGINAL_B: context(2, 1)}, receipts)
        self.assert_code(value, "receipt_count_mismatch")

    def test_native_normalizer_and_citation_round_trip(self):
        raw = native_receipt([raw_unit(1), raw_unit(2)])
        value = evidence.normalize_receipt(raw, ORIGINAL_A, evidence.sha(raw))
        self.assertIs(evidence.validate_bundle(value), value)
        cite = evidence.cite_text(value["units"][0], "page 1 text")
        self.assertEqual(evidence.validate_citation(cite, value)["semantic_assessment"], "NOT_ASSESSED")
        made = evidence.claim(value, "Synthetic page one exists.", [cite])
        self.assertFalse(evidence.validate_claim(made, value)["review_acceptance"])

    def test_validation_does_not_rewrite_receipts_or_ids(self):
        value = two_unit()
        before = copy.deepcopy(value)
        evidence.validate_bundle(value)
        self.assertEqual(value, before)
        self.assertEqual(evidence.canonical(value), evidence.canonical(before))


class ReceiptCountRejectionTests(CodeAssertions, unittest.TestCase):
    def mutated(self, **changes):
        value = two_unit()
        value["source_receipts"][0].update(changes)
        return rehash(value)

    def test_wrong_normalized_count_with_recomputed_hash(self):
        value = self.mutated(normalized_unit_count=999, declared_unit_count=999)
        self.assert_code(value, "receipt_count_mismatch")

    def test_inflated_normalized_count_only_with_recomputed_hash(self):
        self.assert_code(self.mutated(normalized_unit_count=999), "receipt_count_exceeds_declared")

    def test_understated_normalized_count_with_recomputed_hash(self):
        self.assert_code(self.mutated(normalized_unit_count=1), "receipt_count_mismatch")

    def test_negative_count(self):
        self.assert_code(self.mutated(declared_unit_count=-1), "receipt_count_negative")
        self.assert_code(self.mutated(normalized_unit_count=-2), "receipt_count_negative")

    def test_bool_count(self):
        self.assert_code(self.mutated(declared_unit_count=True), "receipt_count_type")
        self.assert_code(self.mutated(normalized_unit_count=False), "receipt_count_type")

    def test_float_count(self):
        self.assert_code(self.mutated(declared_unit_count=2.0), "receipt_count_type")
        self.assert_code(self.mutated(normalized_unit_count=2.0), "receipt_count_type")

    def test_string_count(self):
        self.assert_code(self.mutated(declared_unit_count="2"), "receipt_count_type")
        self.assert_code(self.mutated(normalized_unit_count="2"), "receipt_count_type")

    def test_null_count(self):
        self.assert_code(self.mutated(normalized_unit_count=None), "receipt_count_type")

    def test_missing_counts(self):
        for key in ("declared_unit_count", "normalized_unit_count"):
            value = two_unit()
            del value["source_receipts"][0][key]
            self.assert_code(rehash(value), "receipt_count_missing")
        value = two_unit()
        del value["source_receipts"][0]["declared_unit_count"]
        del value["source_receipts"][0]["normalized_unit_count"]
        self.assert_code(rehash(value), "receipt_count_missing")

    def test_declared_smaller_than_normalized(self):
        self.assert_code(self.mutated(declared_unit_count=1), "receipt_count_exceeds_declared")

    def test_missing_unit(self):
        value = two_unit()
        value["units"] = value["units"][:1]
        self.assert_code(rehash(value), "receipt_count_mismatch")

    def test_zero_declared_receipt_with_units_present(self):
        self.assert_code(self.mutated(declared_unit_count=0, normalized_unit_count=0), "receipt_count_mismatch")

    def test_unbound_extra_receipt_claiming_units(self):
        value = two_unit()
        value["source_receipts"].append(receipt_row(ORIGINAL_A, RECEIPT_A2, 1, 1))
        self.assert_code(rehash(value), "receipt_count_mismatch")

    def test_duplicate_unit_check_is_preserved(self):
        value = two_unit()
        value["units"].append(copy.deepcopy(value["units"][0]))
        value["source_receipts"][0].update(declared_unit_count=3, normalized_unit_count=3)
        self.assert_code(rehash(value), "duplicate_unit")

    def test_unit_hash_check_is_preserved(self):
        value = two_unit()
        value["units"][0]["text_sha256"] = label_hash("forged text")
        self.assert_code(rehash(value), "unit_binding_mismatch")

    def test_outer_hash_check_is_preserved(self):
        value = two_unit()
        value["source_receipts"][0]["normalized_unit_count"] = 999
        self.assert_code(value, "bundle_snapshot_mismatch")

    def test_unit_without_receipt_is_preserved(self):
        value = two_unit()
        value["source_receipts"] = []
        self.assert_code(rehash(value), "unit_receipt_missing")

    def test_count_codes_are_stable(self):
        self.assertEqual(COUNT_CODES, set(getattr(evidence, "RECEIPT_COUNT_CODES", ())))


if __name__ == "__main__":
    unittest.main()
