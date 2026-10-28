"""FC-DOC-COVERAGE: incomplete page inventories are partial document understanding.

Every fixture below is invented for these tests. Hashes are derived at run time
from labelled synthetic bytes; nothing here describes a real record or source.
The tests exercise the production API ``document.analyze`` only.
"""
import hashlib
import tracemalloc
import unittest

from campaign_tool.records.understanding import document, evidence


def digest(data):
    return hashlib.sha256(data).hexdigest()


ORIGINAL = digest(b"fc-doc-coverage synthetic original document")

PAGE_TEXT = {
    1: "Public records request R-0101\nAgency: Example Agency\nDate: 2030-02-01\n"
       "Policy 200.1 requires retention of audit log entries for 30 days.",
    2: "Invoice amount due for the synthetic records search is listed here.",
    3: "Department: Example Department\nThe synthetic third page closes the file.",
}


def text_unit(page, data=None):
    return {"kind": "text", "locator": {"page": page}, "text": PAGE_TEXT[page], "data": data or {}}


def visual_unit(page):
    """A visual-only page attempt: no text, so it cannot establish text coverage."""
    return {"kind": "visual_page", "locator": {"page": page}, "text": "", "data": {}}


def receipt(pages, units):
    return {
        "schema": "format-extraction-v1",
        "original_sha256": ORIGINAL,
        "original_changed": False,
        "status": "complete",
        "parser": "synthetic",
        "parser_version": "0",
        "route": {"format": "pdf"},
        "pages": pages,
        "units": units,
    }


def build(pages, units, **context):
    """Normalize through the real receipt path; pages must be a list there."""
    raw = receipt(pages, units)
    return evidence.normalize_receipt(raw, ORIGINAL, digest(evidence.canonical(raw)),
                                      extra_context=context or None)


def build_raw(pages, units, **context):
    """Bind a hand-shaped source context (for non-list page inventories)."""
    receipt_sha = digest(b"fc-doc-coverage synthetic receipt " + repr(pages).encode())
    bound = [evidence.unit(raw, ORIGINAL, receipt_sha, ordinal) for ordinal, raw in enumerate(units, 1)]
    ctx = {"format": "pdf", "native_status": "complete", "pages": pages,
           "declared_unit_count": len(units), "normalized_unit_count": len(bound),
           "normalization_gaps": [], "review_acceptance": False, "semantic_acceptance": False, **context}
    source = {"original_sha256": ORIGINAL, "receipt_sha256": receipt_sha, "attempt_kind": "native",
              "declared_unit_count": len(units), "normalized_unit_count": len(bound)}
    return evidence.bundle(bound, {ORIGINAL: ctx}, [source])


def inventory(declared=None, count_status="absent", shape="list", entries=0, inventoried=0,
              in_range=None, missing=None, duplicate=0, invalid=0, out_of_range=0, status="unknown_total"):
    return {"declared_page_count": declared, "count_status": count_status, "inventory_shape": shape,
            "supplied_page_entry_count": entries, "inventoried_page_count": inventoried,
            "in_range_page_count": in_range, "missing_page_count": missing,
            "duplicate_page_count": duplicate, "invalid_page_identifier_count": invalid,
            "out_of_range_page_count": out_of_range, "status": status}


def row(result):
    rows = [r for r in result["coverage"] if r["original_sha256"] == ORIGINAL]
    if len(rows) != 1:
        raise AssertionError(f"expected one coverage row, got {len(rows)}")
    return rows[0]


class FcDocCoverageTests(unittest.TestCase):
    def check(self, result, gaps, partial, inv, pages):
        self.assertEqual(result["gaps"], gaps)
        self.assertIs(result["partial_understanding"], partial)
        coverage = row(result)
        self.assertEqual(coverage.get("page_inventory"), inv)
        self.assertEqual(coverage["declared_page_count"], inv["declared_page_count"])
        self.assertEqual([p["page"] for p in coverage["pages"]], pages)
        self.assertFalse(result["review_acceptance"])
        self.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")

    def test_complete_two_page_inventory_is_clean(self):
        result = document.analyze(build([{"page_no": 1}, {"page_no": 2}], [text_unit(1), text_unit(2)], page_count=2))
        self.check(result, [], False,
                   inventory(2, "declared", "list", 2, 2, 2, 0, status="complete"), [1, 2])

    def test_one_of_two_pages_is_partial(self):
        # The measured baseline probe shape: declared two, inventoried one, one text unit.
        result = document.analyze(build([{"page_no": 1}], [text_unit(1)], page_count=2))
        self.check(result, ["page_inventory_incomplete"], True,
                   inventory(2, "declared", "list", 1, 1, 1, 1, status="incomplete"), [1])

    def test_missing_middle_page(self):
        result = document.analyze(build([1, 3], [text_unit(1), text_unit(3)], page_count=3))
        self.check(result, ["page_inventory_incomplete"], True,
                   inventory(3, "declared", "list", 2, 2, 2, 1, status="incomplete"), [1, 3])

    def test_missing_trailing_page_for_each_count_key(self):
        for key in ("declared_page_count", "page_count", "expected_page_count"):
            with self.subTest(key=key):
                result = document.analyze(build([{"page": 1}, {"page": 2}], [text_unit(1), text_unit(2)], **{key: 3}))
                self.check(result, ["page_inventory_incomplete"], True,
                           inventory(3, "declared", "list", 2, 2, 2, 1, status="incomplete"), [1, 2])

    def test_missing_whole_inventory_keeps_existing_gap(self):
        result = document.analyze(build([], [text_unit(1)], page_count=2))
        self.check(result, ["page_inventory_missing"], True,
                   inventory(2, "declared", "list", 0, 0, 0, 2, status="missing"), [])

    def test_duplicate_page_numbers(self):
        result = document.analyze(build([1, {"page_no": 1}], [text_unit(1)], page_count=2))
        self.check(result, ["page_inventory_incomplete", "page_inventory_invalid"], True,
                   inventory(2, "declared", "list", 2, 1, 1, 1, duplicate=1, status="inconsistent"), [1])
        full = document.analyze(build([1, 2, 2], [text_unit(1), text_unit(2)], page_count=2))
        self.check(full, ["page_inventory_invalid"], True,
                   inventory(2, "declared", "list", 3, 2, 2, 0, duplicate=1, status="inconsistent"), [1, 2])

    def test_page_above_declared_total(self):
        units = [text_unit(1), text_unit(2), text_unit(3)]
        result = document.analyze(build([1, 2, 3], units, page_count=2))
        # The supplied page stays visible as supplied; nothing is invented or dropped.
        self.check(result, ["page_inventory_invalid"], True,
                   inventory(2, "declared", "list", 3, 3, 2, 0, out_of_range=1, status="inconsistent"), [1, 2, 3])
        only_high = document.analyze(build([3], [text_unit(3)], page_count=2))
        self.check(only_high, ["page_inventory_incomplete", "page_inventory_invalid"], True,
                   inventory(2, "declared", "list", 1, 1, 0, 2, out_of_range=1, status="inconsistent"), [3])

    def test_malformed_page_identifiers(self):
        for bad in (True, False, 0, -1, "2", 2.0, None, [2], {"page_no": None}, {"page_no": True},
                    {"other": 2}, {"page": "1"}):
            with self.subTest(bad=bad):
                result = document.analyze(build([1, bad, 2], [text_unit(1), text_unit(2)], page_count=2))
                self.check(result, ["page_inventory_invalid"], True,
                           inventory(2, "declared", "list", 3, 2, 2, 0, invalid=1, status="inconsistent"), [1, 2])

    def test_malformed_or_conflicting_declared_count(self):
        units = [text_unit(1), text_unit(2)]
        cases = [{"page_count": v} for v in (True, False, 0, -2, "2", 2.0, [2], {"n": 2})]
        cases.append({"declared_page_count": 2, "page_count": 3})
        cases.append({"page_count": 2, "expected_page_count": "2"})
        for counts in cases:
            with self.subTest(counts=counts):
                result = document.analyze(build([1, 2], units, **counts))
                self.check(result, ["page_count_invalid"], True,
                           inventory(None, "invalid", "list", 2, 2, None, None, status="inconsistent"), [1, 2])
        agreeing = document.analyze(build([1, 2], units, declared_page_count=2, page_count=2, expected_page_count=2))
        self.check(agreeing, [], False, inventory(2, "declared", "list", 2, 2, 2, 0, status="complete"), [1, 2])

    def test_extremely_large_declared_count_is_not_allocated(self):
        huge = 10 ** 15
        bundle = build([1, 2], [text_unit(1), text_unit(2)], page_count=huge)
        tracemalloc.start()
        try:
            result = document.analyze(bundle)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 * 1024 * 1024)
        self.check(result, ["page_inventory_incomplete"], True,
                   inventory(huge, "declared", "list", 2, 2, 2, huge - 2, status="incomplete"), [1, 2])

    def test_integer_page_inventory_is_bounded(self):
        ok = document.analyze(build_raw(2, [text_unit(1), text_unit(2)], page_count=2))
        self.check(ok, [], False, inventory(2, "declared", "count", 2, 2, 2, 0, status="complete"), [1, 2])
        for bad in (10 ** 12, document.MAX_PAGE_INVENTORY + 1, True, 0, -3, "2"):
            with self.subTest(pages=bad):
                bundle = build_raw(bad, [text_unit(1)])
                tracemalloc.start()
                try:
                    result = document.analyze(bundle)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, 8 * 1024 * 1024)
                self.check(result, ["page_inventory_invalid"], True,
                           inventory(None, "absent", "invalid", 0, 0, None, None, invalid=1, status="inconsistent"), [])

    def test_no_declared_count_remains_unknown(self):
        result = document.analyze(build([1], [text_unit(1)]))
        self.check(result, [], False, inventory(entries=1, inventoried=1), [1])
        explicit_none = document.analyze(build([1], [text_unit(1)], page_count=None))
        self.check(explicit_none, [], False, inventory(entries=1, inventoried=1), [1])
        absent = document.analyze(build_raw(None, [text_unit(1)]))
        self.check(absent, [], False, inventory(shape="absent"), [])

    def test_page_without_text_unit_is_not_an_inventory_gap(self):
        result = document.analyze(build([1, 2], [text_unit(1)], page_count=2))
        self.check(result, ["page_without_text_unit"], True,
                   inventory(2, "declared", "list", 2, 2, 2, 0, status="complete"), [1, 2])
        self.assertEqual([p["text_coverage"] for p in row(result)["pages"]], ["text_unit_present", "no_text_unit"])

    def test_visual_and_ocr_flags_are_preserved_with_inventory_gap(self):
        pages = [{"page_no": 1, "visual_review_complete": True, "needs_visual_review": True}]
        result = document.analyze(build(pages, [text_unit(1, {"ocr": True})], page_count=2))
        self.check(result, ["claimed_visual_review_unverified", "ocr_visual_review_required",
                            "page_inventory_incomplete"], True,
                   inventory(2, "declared", "list", 1, 1, 1, 1, status="incomplete"), [1])
        page = row(result)["pages"][0]
        self.assertEqual(page["visual_review"], "not_established")
        self.assertEqual(page["ocr_visual_review"], "required")
        self.assertTrue(page["visual_check_required"])

    def test_visual_only_pages_are_unverified_and_partial(self):
        # Visual-only attempts carry no text: they never establish text or visual review.
        units = [visual_unit(1), visual_unit(2)]
        complete = document.analyze(build([1, 2], units, page_count=2))
        self.check(complete, ["page_without_text_unit"], True,
                   inventory(2, "declared", "list", 2, 2, 2, 0, status="complete"), [1, 2])
        for page in row(complete)["pages"]:
            with self.subTest(page=page["page"]):
                self.assertEqual(page["text_coverage"], "no_text_unit")
                self.assertEqual(page["text_unit_count"], 0)
                self.assertEqual(page["text_sources"], [])
                self.assertEqual(page["evidence_attempts"], ["visual"])
                self.assertEqual(page["visual_attempt_count"], 1)
                self.assertEqual(page["visual_review"], "not_established")
                self.assertEqual(page["ocr_visual_review"], "not_established")
        self.assertEqual(row(complete)["visual_review"], "not_established")
        incomplete = document.analyze(build([{"page_no": 1, "visual_review_complete": True}],
                                            [visual_unit(1)], page_count=3))
        self.check(incomplete, ["claimed_visual_review_unverified", "page_inventory_incomplete",
                                "page_without_text_unit"], True,
                   inventory(3, "declared", "list", 1, 1, 1, 2, status="incomplete"), [1])
        page = row(incomplete)["pages"][0]
        self.assertEqual((page["visual_attempt_count"], page["visual_review"]), (1, "not_established"))
        # Native and OCR text controls on the same visual page keep their own sources.
        mixed = document.analyze(build([1, 2], [visual_unit(1), text_unit(1), visual_unit(2), text_unit(2, {"ocr": True})],
                                       page_count=2))
        self.check(mixed, ["ocr_visual_review_required"], True,
                   inventory(2, "declared", "list", 2, 2, 2, 0, status="complete"), [1, 2])
        self.assertEqual([(p["text_sources"], p["evidence_attempts"], p["visual_attempt_count"]) for p in row(mixed)["pages"]],
                         [(["native"], ["native", "visual"], 1), (["ocr"], ["ocr", "visual"], 1)])

    def test_conflicting_page_aliases_are_invalid_in_both_directions(self):
        units = [text_unit(1), text_unit(2)]
        for pages in ([{"page": 1, "page_no": 999}, {"page": 2, "page_no": 2}],
                      [{"page_no": 1, "page": 999}, {"page_no": 2, "page": 2}],
                      [{"page": 1, "page_no": 2}, {"page": 2}]):
            with self.subTest(pages=pages):
                result = document.analyze(build(pages, units, page_count=2))
                # The contradictory entry is not trusted, so neither of its numbers is inventoried.
                self.check(result, ["page_inventory_incomplete", "page_inventory_invalid"], True,
                           inventory(2, "declared", "list", 2, 1, 1, 1, invalid=1, status="inconsistent"), [2])

    def test_malformed_alternate_page_alias_is_invalid(self):
        units = [text_unit(1), text_unit(2)]
        for bad in (True, False, 0, -1, "bad", "1", 1.0, None, [1]):
            for first in ({"page": 1, "page_no": bad}, {"page_no": 1, "page": bad}):
                with self.subTest(entry=first):
                    result = document.analyze(build([first, {"page_no": 2}], units, page_count=2))
                    self.check(result, ["page_inventory_incomplete", "page_inventory_invalid"], True,
                               inventory(2, "declared", "list", 2, 1, 1, 1, invalid=1, status="inconsistent"), [2])

    def test_agreeing_and_single_page_aliases_are_clean(self):
        units = [text_unit(1), text_unit(2)]
        for pages in ([{"page": 1, "page_no": 1, "status": "s1"}, {"page": 2, "page_no": 2, "status": "s2"}],
                      [{"page": 1, "status": "s1"}, {"page_no": 2, "status": "s2"}],
                      [{"page_no": 1, "status": "s1"}, {"page": 2, "status": "s2"}]):
            with self.subTest(pages=pages):
                result = document.analyze(build(pages, units, page_count=2))
                self.check(result, [], False, inventory(2, "declared", "list", 2, 2, 2, 0, status="complete"), [1, 2])
                self.assertEqual([p["extraction_status"] for p in row(result)["pages"]], ["s1", "s2"])

    def test_page_metadata_uses_the_same_resolved_identifier(self):
        # A rejected contradictory entry must not lend its metadata to another page.
        pages = [{"page": 1, "page_no": 2, "status": "rejected"}, {"page": 2, "status": "kept"}]
        result = document.analyze(build(pages, [text_unit(1), text_unit(2)], page_count=2))
        self.assertEqual([(p["page"], p["extraction_status"]) for p in row(result)["pages"]], [(2, "kept")])

    def test_citations_unchanged_by_inventory_gaps(self):
        units = [text_unit(1), text_unit(2)]
        complete = build([1, 2], units, page_count=2)
        incomplete = build([1, 2], units, page_count=3)
        unknown = build([1, 2], units)
        self.assertEqual([u["unit_id"] for u in complete["units"]], [u["unit_id"] for u in incomplete["units"]])
        results = [document.analyze(b) for b in (complete, incomplete, unknown)]
        for key in ("classifications", "entity_candidates", "date_mentions", "cross_record_key_candidates",
                    "candidate_totals", "candidate_omissions"):
            with self.subTest(key=key):
                self.assertEqual(results[0][key], results[1][key])
                self.assertEqual(results[0][key], results[2][key])
        self.assertTrue(results[0]["classifications"])
        for bundle, result in zip((complete, incomplete, unknown), results):
            for item in result["classifications"] + result["entity_candidates"] + result["date_mentions"]:
                evidence.validate_citation(item["citation"], bundle)
            self.assertEqual([(f["unit_id"], f["kind"]) for f in result["summary_facts"]],
                             [(f["unit_id"], f["kind"]) for f in results[0]["summary_facts"]])
        self.assertEqual(results[0]["gaps"], [])
        self.assertEqual(results[1]["gaps"], ["page_inventory_incomplete"])
        self.assertEqual(results[2]["gaps"], [])

    def test_unit_bearing_original_without_context_is_unknown(self):
        receipt_sha = digest(b"fc-doc-coverage synthetic orphan receipt")
        other = digest(b"fc-doc-coverage synthetic orphan original")
        bound = [evidence.unit(text_unit(1), other, receipt_sha, 1)]
        source = {"original_sha256": other, "receipt_sha256": receipt_sha, "attempt_kind": "native",
                  "declared_unit_count": 1, "normalized_unit_count": 1}
        # validate_bundle requires context for every unit; exercise the orphan branch directly.
        bundle = {"units": bound, "source_context": {}, "source_receipts": [source]}
        rows, gaps = document._coverage(bundle)
        self.assertEqual(gaps, ["source_context_missing"])
        self.assertEqual(rows[0]["page_inventory"], inventory(shape="absent"))
        self.assertIsNone(rows[0]["declared_page_count"])


if __name__ == "__main__":
    unittest.main()
