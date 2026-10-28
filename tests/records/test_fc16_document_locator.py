"""FC16-DOCUMENT-LOCATOR: malformed unit page locators never claim document text coverage.

Every value comes from the committed synthetic fixture
tests/fixtures/fc16_document_locator/cases.json. Hashes are derived at run time
from labelled synthetic bytes; nothing here describes a real record or source.
The tests exercise the production path evidence.normalize_receipt followed by
document.analyze. A pass is machine validity only (REVIEW REQUIRED).

Run as a script, an optional leading ``--temp-root PATH`` is applied to the
tempfile default before any product import.
"""
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fc16_document_locator" / "cases.json"

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--temp-root":
        tempfile.tempdir = sys.argv[2]
        os.environ["TMPDIR"] = sys.argv[2]
        del sys.argv[1:3]
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

from campaign_tool.records.understanding import document, evidence  # noqa: E402

FROZEN_CASES = ("valid_aliases", "invalid_types", "contradiction", "mixed_and_nonpage")
CASE_MAP = {case: "test_" + case for case in FROZEN_CASES}
EXTRACTION_KEYS = ("classifications", "entity_candidates", "date_mentions", "summary_facts",
                   "cross_record_key_candidates", "candidate_totals", "candidate_omissions")


def load_cases():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


CASES = load_cases()
GAP = CASES["gap"]


def digest(data):
    return hashlib.sha256(data).hexdigest()


ORIGINAL = digest(CASES["provenance"]["original_label"].encode("utf-8"))


def raw_unit(locator, text_key):
    return {"kind": "text", "locator": copy.deepcopy(locator), "text": CASES["texts"][text_key], "data": {}}


def build(pages, page_count, units):
    """Normalize a synthetic format-extraction-v1 receipt through the real path."""
    raw = {"schema": "format-extraction-v1", "original_sha256": ORIGINAL, "original_changed": False,
           "status": "complete", "parser": "synthetic", "parser_version": "0", "route": {"format": "pdf"},
           "pages": copy.deepcopy(pages), "units": [raw_unit(u["locator"], u["text"]) for u in units]}
    extra = None if page_count is None else {"page_count": page_count}
    return evidence.normalize_receipt(raw, ORIGINAL, digest(evidence.canonical(raw)), extra_context=extra)


def coverage_row(result):
    rows = [r for r in result["coverage"] if r["original_sha256"] == ORIGINAL]
    if len(rows) != 1:
        raise AssertionError("expected one coverage row, got %d" % len(rows))
    return rows[0]


class FC16DocumentLocatorTests(unittest.TestCase):
    def check(self, bundle, expect, unit_count):
        snapshot = copy.deepcopy(bundle)
        result = document.analyze(bundle)
        row = coverage_row(result)
        # Counts first: this is the deciding conflicting-alias assertion.
        self.assertEqual([p["text_unit_count"] for p in row["pages"]], expect["counts"], "text_unit_count per page")
        self.assertEqual([p["page"] for p in row["pages"]], expect["pages"])
        self.assertEqual(result["gaps"], expect["gaps"])
        self.assertIs(result["partial_understanding"], expect["partial"])
        self.assertEqual([p["text_coverage"] for p in row["pages"]],
                         ["text_unit_present" if n else "no_text_unit" for n in expect["counts"]])
        # No source unit is rewritten or discarded, and the input bundle is untouched.
        self.assertEqual(row["unit_count"], unit_count)
        self.assertEqual(len(bundle["units"]), unit_count)
        self.assertEqual(bundle, snapshot)
        self.assertFalse(result["review_acceptance"])
        self.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")
        return result

    def test_fixture_declares_the_frozen_matrix(self):
        self.assertEqual(CASES["schema"], "fc16-document-locator-cases/v1")
        self.assertEqual(tuple(CASES["cases"]), FROZEN_CASES)
        self.assertEqual(GAP, "unit_page_locator_invalid")
        self.assertEqual(CASES["deciding_control"]["case"], "contradiction")

    def test_valid_aliases(self):
        for case in CASES["cases"]["valid_aliases"]:
            with self.subTest(subcase=case["id"]):
                self.check(build(case["pages"], case["page_count"], case["units"]), case["expect"], len(case["units"]))

    def test_invalid_types(self):
        spec = CASES["cases"]["invalid_types"]
        self.assertTrue(spec["bad_values"])
        for form in spec["forms"]:
            for bad in spec["bad_values"]:
                locator = dict(form["fixed"])
                locator[form["bad_key"]] = copy.deepcopy(bad)
                with self.subTest(form=form["id"], bad=json.dumps(bad)):
                    bundle = build(spec["pages"], spec["page_count"], [{"locator": locator, "text": spec["text"]}])
                    self.check(bundle, spec["expect"], 1)
                    # The malformed locator itself is preserved verbatim.
                    self.assertEqual(bundle["units"][0]["locator"], locator)

    def test_contradiction(self):
        control = CASES["deciding_control"]
        seen = set()
        for case in CASES["cases"]["contradiction"]:
            seen.add(case["id"])
            with self.subTest(subcase=case["id"]):
                bundle = build(case["pages"], case["page_count"], case["units"])
                self.check(bundle, case["expect"], len(case["units"]))
                self.assertEqual(bundle["units"][0]["locator"], case["units"][0]["locator"])
                self.assertEqual(list(bundle["units"][0]["locator"]), list(case["units"][0]["locator"]))
        self.assertIn(control["subcase"], seen)

    def test_mixed_and_nonpage(self):
        for case in CASES["cases"]["mixed_and_nonpage"]:
            with self.subTest(subcase=case["id"]):
                bundle = build(case["pages"], case["page_count"], case["units"])
                result = self.check(bundle, case["expect"], len(case["units"]))
                if not any(k in u["locator"] for u in case["units"] for k in ("page", "page_no")):
                    self.assertNotIn(GAP, result["gaps"])
                # Candidate extraction and citations do not depend on page coverage.
                with mock.patch.object(document, "_coverage", return_value=([], [])):
                    reference = document.analyze(copy.deepcopy(bundle))
                for key in EXTRACTION_KEYS:
                    self.assertEqual(result[key], reference[key], key)
                self.assertTrue(result["classifications"])
                self.assertEqual(len({c["unit_id"] for c in result["classifications"]}), len(case["units"]))
                for item in result["classifications"] + result["entity_candidates"] + result["date_mentions"]:
                    evidence.validate_citation(item["citation"], bundle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
