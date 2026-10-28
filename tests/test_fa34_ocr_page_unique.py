"""FA34: one OCR receipt per page, refused structurally before any OCR/vision evidence is read.

Synthetic only. A1-A3 and A6 drive the real ``_accepted_ocr_pages`` helper with in-memory
loader doubles; A5 uses the real per-page OCR fixture, enrollment and CAS closure with read
spies; A4 is an isolated binder double that proves the gate detects last-page-wins binding.
Passing here is REVIEW REQUIRED evidence, not semantic acceptance or a review/publish grant.
"""
import contextlib
import copy
import hashlib
import importlib
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import extraction_ledger as enrollment
from campaign_tool.records import extraction_routes as routes
from campaign_tool.records import extraction_validation as validation
from tests.records import test_extraction_ledger as fixture_module

REPO = Path(__file__).resolve().parents[1]
WATCHED = ("campaign_tool/records/extraction_validation.py", "campaign_tool/records/ocr_closure.py",
           "campaign_tool/records/extraction_ledger.py", "campaign_tool/records/extraction_routes.py",
           "tests/records/ocr_fixtures.py", "tests/records/test_extraction_ledger.py",
           "tests/records/test_extraction_validation.py")
BINDING = "ocr_receipts_binding"
SHAPE = "ocr_receipt_entry_shape"


def digest(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def file_hashes():
    return {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in WATCHED}


class SyntheticEvidence:
    """In-memory OCR/vision receipts keyed by receipt id; every read is recorded."""

    def __init__(self):
        self.ocr, self.vision, self.reads = {}, {}, []

    def page(self, page, label, status="visual_check_queued"):
        receipt_id, receipt_sha = digest("ocr-receipt:" + label), digest("ocr-receipt-bytes:" + label)
        self.ocr[receipt_id] = {"page": page, "sidecar": ("ocr text " + label).encode(), "loaded": {
            "receipt": {"receipt_id": receipt_id, "status": status, "blocked_reason": None,
                        "confidence": 87, "tool_versions": {"tesseract": "0.0-test"}},
            "receipt_sha256": receipt_sha}}
        return {"page": page, "receipt_id": receipt_id, "receipt_sha256": receipt_sha, "status": status}

    def vision_for(self, base, label):
        receipt_id, receipt_sha = digest("vision-receipt:" + label), digest("vision-receipt-bytes:" + label)
        self.vision[receipt_id] = {"page": base["page"], "sidecar": ("vision text " + label).encode(), "loaded": {
            "receipt": {"receipt_id": receipt_id, "status": routes.VISION_TEXT_STATE, "blocked_reason": None,
                        "engine": "local-vision-1", "model": "synthetic-model",
                        "base": {"receipt_id": base["receipt_id"], "receipt_sha256": base["receipt_sha256"]}},
            "receipt_sha256": receipt_sha}}
        return {"page": base["page"], "receipt_id": receipt_id, "receipt_sha256": receipt_sha,
                "status": routes.VISION_TEXT_STATE, "engine": "local-vision-1",
                "base_receipt_id": base["receipt_id"]}

    def _load(self, kind, table, page, receipt_id):
        self.reads.append((kind, page, receipt_id))
        item = table.get(receipt_id)
        if item is None or item["page"] != page:
            raise ValueError("synthetic_receipt_unavailable")
        return dict(copy.deepcopy(item["loaded"]), sidecar=item["sidecar"])

    def load_page(self, artifacts, subject, page, receipt_id, *, scratch_root=None):
        return self._load("ocr", self.ocr, page, receipt_id)

    def load_vision(self, artifacts, subject, page, receipt_id, *, scratch_root=None):
        return self._load("vision", self.vision, page, receipt_id)

    @contextlib.contextmanager
    def installed(self):
        with patch.object(validation.ocr_closure, "load_cas_page", self.load_page), \
                patch.object(validation.ocr_closure, "load_cas_vision", self.load_vision):
            yield self


def bind(entries, visions=(), *, ocr_ids=None, scratch=None):
    receipt = {"original_sha256": digest("synthetic original"), "ocr_output_root": "/synthetic/ocr-root",
               "ocr_receipts": entries}
    if visions:
        receipt["vision_receipts"] = list(visions)
    if ocr_ids is None:
        ocr_ids = sorted(e["receipt_id"] for e in entries)
    return validation._accepted_ocr_pages(receipt, ocr_ids, artifacts={}, scratch_root=scratch,
                                          vision_ids=sorted(v["receipt_id"] for v in visions))


class FA34OcrPageUniqueTests(unittest.TestCase):

    def refused(self, evidence, code, entries, visions=(), ocr_ids=None):
        with evidence.installed():
            with self.assertRaises(enrollment.ExtractionBindingError) as caught:
                bind(entries, visions, ocr_ids=ocr_ids)
        self.assertEqual(str(caught.exception), code)
        self.assertEqual(evidence.reads, [], "evidence was read before the structural refusal")

    def test_valid_round_trip(self):
        evidence = SyntheticEvidence()
        first, third = evidence.page(1, "p1"), evidence.page(3, "p3", status="ocr_text_unreviewed")
        for order in ([first, third], [third, first]):
            with self.subTest(order=[e["page"] for e in order]):
                evidence.reads.clear()
                with evidence.installed():
                    pages = bind(copy.deepcopy(order))
                self.assertEqual(sorted(pages), [1, 3])
                for page, entry in ((1, first), (3, third)):
                    self.assertEqual(pages[page]["receipt_id"], entry["receipt_id"])
                    self.assertEqual(pages[page]["receipt_sha256"], entry["receipt_sha256"])
                    self.assertEqual(pages[page]["summary"]["status"], entry["status"])
                self.assertEqual(pages[1]["text"], "ocr text p1")
                self.assertEqual(pages[3]["text"], "ocr text p3")
                self.assertEqual(len(evidence.reads), 2)
        vision = evidence.vision_for(third, "v3")
        with evidence.installed():
            pages = bind([first, third], [vision])
        self.assertEqual(pages[3]["receipt_id"], third["receipt_id"])
        self.assertEqual(pages[3]["text"], "vision text v3")
        self.assertEqual(pages[3]["summary"]["vision"]["base_receipt_id"], third["receipt_id"])
        self.assertEqual(pages[3]["summary"]["vision"]["receipt_id"], vision["receipt_id"])
        self.assertNotIn("vision", pages[1]["summary"])
        self.assertEqual(pages[1]["text"], "ocr text p1")

    def test_malformed_refusal(self):
        evidence = SyntheticEvidence()
        a, b, c = evidence.page(1, "a"), evidence.page(1, "b"), evidence.page(2, "c")
        same_id_other_page = dict(a, page=2)
        vision = evidence.vision_for(a, "va")
        duplicates = {
            "different_ids_one_page": ([a, b], None),
            "reversed_different_ids_one_page": ([b, a], None),
            "same_id_repeated_other_page": ([a, same_id_other_page], [a["receipt_id"]] * 2),
            "equal_duplicate_entries": ([a, dict(a)], [a["receipt_id"]] * 2),
            "reversed_equal_duplicates": ([dict(a), a], [a["receipt_id"]] * 2),
            "duplicate_after_valid_page": ([c, a, b], None),
        }
        for name, (entries, ocr_ids) in duplicates.items():
            for visions in ((), (vision,)):
                with self.subTest(case=name, vision=bool(visions)):
                    evidence.reads.clear()
                    self.refused(evidence, BINDING, copy.deepcopy(entries), visions, ocr_ids)
        valid = dict(c)
        malformed = {
            "string_entry": "not-an-entry", "none_entry": None, "list_entry": [2],
            "missing_status": {k: v for k, v in valid.items() if k != "status"},
            "extra_field": dict(valid, reviewed=True),
            "bool_page": dict(valid, page=True), "float_page": dict(valid, page=2.0),
            "string_page": dict(valid, page="2"), "zero_page": dict(valid, page=0),
            "negative_page": dict(valid, page=-2), "none_page": dict(valid, page=None),
            "none_receipt_id": dict(valid, receipt_id=None), "int_receipt_id": dict(valid, receipt_id=7),
            "short_receipt_id": dict(valid, receipt_id="abc"),
            "upper_receipt_id": dict(valid, receipt_id=valid["receipt_id"].upper()),
            "none_receipt_sha": dict(valid, receipt_sha256=None),
            "short_receipt_sha": dict(valid, receipt_sha256="0" * 10),
        }
        for name, bad in malformed.items():
            # The valid entry comes first so a lazy check would read its evidence before refusing.
            for entries in ([a, bad], [bad, a]):
                with self.subTest(case=name, first=entries[0] is a):
                    evidence.reads.clear()
                    ids = sorted(e["receipt_id"] for e in entries if isinstance(e, dict)
                                 and isinstance(e.get("receipt_id"), str))
                    self.refused(evidence, SHAPE, copy.deepcopy(entries), ocr_ids=ids)

    def test_boundary_replay(self):
        evidence = SyntheticEvidence()
        for name, receipt in {"missing": {}, "empty": {"ocr_receipts": []}, "none": {"ocr_receipts": None},
                              "mapping": {"ocr_receipts": {"1": "x"}}}.items():
            with self.subTest(case=name):
                receipt = dict(receipt, original_sha256=digest("synthetic original"),
                               ocr_output_root="/synthetic/ocr-root")
                with evidence.installed(), self.assertRaises(enrollment.ExtractionBindingError) as caught:
                    validation._accepted_ocr_pages(receipt, [], artifacts={}, scratch_root=None)
                self.assertEqual(str(caught.exception), BINDING)
                self.assertEqual(evidence.reads, [])
        single = evidence.page(5, "single")
        with evidence.installed():
            self.assertEqual(sorted(bind([single])), [5])
        sparse = [evidence.page(9, "nine", status="ocr_text_unreviewed"), evidence.page(2, "two"),
                  evidence.page(4, "four")]
        with evidence.installed():
            first = bind(copy.deepcopy(sparse))
            second = bind(copy.deepcopy(sparse))
        self.assertEqual(sorted(first), [2, 4, 9])
        self.assertEqual(first, second)
        for entry in sparse:
            self.assertEqual(first[entry["page"]]["receipt_id"], entry["receipt_id"])
            self.assertEqual(first[entry["page"]]["summary"]["status"], entry["status"])
        # The downstream status gate is unchanged: a non-text OCR page still refuses the original.
        blocked = evidence.page(6, "blocked", status="ocr_blocked")
        with evidence.installed(), self.assertRaises(enrollment.ExtractionBindingError) as caught:
            bind([single, blocked])
        self.assertEqual(str(caught.exception), "ocr_page_not_accepted:ocr_blocked")
        # So is the receipt-set binding: entries must name exactly the enrolled receipt ids.
        evidence.reads.clear()
        self.refused(evidence, BINDING, [single], ocr_ids=sorted([single["receipt_id"], digest("other")]))

    def test_preservation(self):
        before = file_hashes()
        scratch = Path(tempfile.gettempdir()).resolve()
        self.assertFalse(scratch == REPO or REPO in scratch.parents, "temporary output inside repository")
        attempts = []

        def refuse(*args, **kwargs):
            attempts.append(args[:2])
            raise OSError("fa34_test_offline")
        evidence = SyntheticEvidence()
        one, two = evidence.page(1, "keep-1"), evidence.page(2, "keep-2")
        duplicate = evidence.page(2, "keep-2-duplicate")
        with patch.object(socket.socket, "connect", refuse), patch.object(socket, "create_connection", refuse), \
                patch.object(subprocess, "run", refuse), patch.object(subprocess, "Popen", refuse), \
                patch.object(routes, "extract", refuse), evidence.installed():
            pages = bind([one, two])
            with self.assertRaises(enrollment.ExtractionBindingError) as caught:
                bind([one, two, duplicate])
        self.assertEqual(str(caught.exception), BINDING)
        self.assertEqual(attempts, [])
        self.assertTrue(all(kind == "ocr" for kind, _, _ in evidence.reads))
        for page in pages.values():
            self.assertEqual(set(page), {"receipt_id", "receipt_sha256", "text", "summary"})
            self.assertEqual(page["summary"]["status"], "visual_check_queued")
            self.assertFalse({"reviewed", "review_status", "publish", "published", "approved",
                              "owner_acknowledged"} & set(page["summary"]))
        self.assertEqual(file_hashes(), before)


def last_page_wins_binder(entries, load):
    """Isolated double of the pre-FA34 behaviour: every entry is read, later pages overwrite."""
    pages = {}
    for entry in entries:
        pages[entry["page"]] = load(entry)
    return pages


def binder_violations(binder):
    """Gate shared by A4: duplicate pages must refuse with the binding code before any read."""
    problems, reads = [], []

    def load(entry):
        reads.append(entry["receipt_id"])
        return entry["receipt_id"]
    one = {"page": 1, "receipt_id": digest("gate-one")}
    if binder([one], load) != {1: one["receipt_id"]} or reads != [one["receipt_id"]]:
        problems.append("single_receipt_not_bound")
    reads.clear()
    duplicate = [one, {"page": 1, "receipt_id": digest("gate-two")}]
    try:
        result = binder(duplicate, load)
    except enrollment.ExtractionBindingError as exc:
        if str(exc) != BINDING:
            problems.append("wrong_refusal_code")
    else:
        problems.append("accepted_duplicate_page:" + str(len(result)))
    if reads:
        problems.append("read_before_refusal:" + str(len(reads)))
    return problems


class FA34RegressionControlTests(unittest.TestCase):

    def test_regression_control(self):
        reads = []
        first, second = {"page": 1, "receipt_id": "first"}, {"page": 1, "receipt_id": "second"}
        accepted = last_page_wins_binder([first, second], lambda e: reads.append(e) or e["receipt_id"])
        self.assertEqual(accepted, {1: "second"})
        self.assertEqual(len(reads), 2)
        self.assertEqual(binder_violations(last_page_wins_binder),
                         ["accepted_duplicate_page:1", "read_before_refusal:2"])

        def unique_binder(entries, load):
            pages = [e["page"] for e in entries]
            validation.require(len(set(pages)) == len(pages), BINDING)
            return last_page_wins_binder(entries, load)
        self.assertEqual(binder_violations(unique_binder), [])


class FA34ConsumerContractTests(unittest.TestCase):
    setUpClass = classmethod(fixture_module.ExtractionLedgerTests.setUpClass.__func__)
    write = fixture_module.ExtractionLedgerTests.write
    ocr_bundle = fixture_module.ExtractionLedgerTests.ocr_bundle

    def setUp(self):
        fixture_module.ExtractionLedgerTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.stages = importlib.import_module("campaign_tool.records.ledger.stages")
        self.addCleanup(patch.stopall)
        patch.dict(self.stages._INSTALLED_VALIDATORS, {}, clear=True).start()
        patch.dict(self.stages._INSTALLED_PROFILES, {}, clear=True).start()

    def test_consumer_contract(self):
        source, receipt_path, result, _ = self.ocr_bundle()
        enrolled = self.adapter.enroll(original_path=source, receipt_path=receipt_path,
                                       receipt_sha256=enrollment.sha(receipt_path.read_bytes()))
        self.assertIn("ocr_visual_review_required", enrolled["gaps"])
        adapter_id = validation.install_extraction_validator(database=self.database,
            evidence_root=self.adapter.evidence_root, original_root=self.root)
        validator = self.stages._INSTALLED_VALIDATORS[adapter_id]
        calls, reads = [], []
        helper, load_page, load_vision = (validation._accepted_ocr_pages, validation.ocr_closure.load_cas_page,
                                          validation.ocr_closure.load_cas_vision)

        def capture(*args, **kwargs):
            calls.append((copy.deepcopy(args[0]), list(args[1]), kwargs))
            return helper(*args, **kwargs)

        def page_spy(*args, **kwargs):
            reads.append(("ocr", args[2], args[3]))
            return load_page(*args, **kwargs)

        def vision_spy(*args, **kwargs):
            reads.append(("vision", args[2], args[3]))
            return load_vision(*args, **kwargs)
        with patch.object(validation, "_accepted_ocr_pages", capture), \
                patch.object(validation.ocr_closure, "load_cas_page", page_spy), \
                patch.object(validation.ocr_closure, "load_cas_vision", vision_spy):
            raw, _, coverage = validator.evidence(enrolled["import_id"])
            envelope = json.loads(raw)
            self.assertEqual(envelope["ocr"]["pages"], [2])
            self.assertTrue(envelope["ocr"]["visual_review_pending"])
            self.assertEqual(coverage["denominator"], {"kind": "pages", "total": 2})
            entry = result["ocr_receipts"][0]
            self.assertEqual(reads, [("ocr", 2, entry["receipt_id"])])
            self.assertEqual(len(calls), 1)
            receipt, ocr_ids, kwargs = calls[0]
            self.assertEqual(ocr_ids, [entry["receipt_id"]])
            # Valid replay through the actual helper with the real CAS closure.
            reads.clear()
            pages = helper(copy.deepcopy(receipt), list(ocr_ids), **kwargs)
            self.assertEqual(sorted(pages), [2])
            self.assertEqual(pages[2]["receipt_id"], entry["receipt_id"])
            self.assertEqual(len(reads), 1)
            other = enrollment.sha(b"synthetic second receipt for page two")
            ambiguous = {
                "same_receipt_twice": ([entry, dict(entry)], [entry["receipt_id"]] * 2),
                "second_id_same_page": ([entry, dict(entry, receipt_id=other)], sorted([entry["receipt_id"], other])),
                "second_id_first": ([dict(entry, receipt_id=other), entry], sorted([entry["receipt_id"], other])),
            }
            for name, (entries, ids) in ambiguous.items():
                with self.subTest(case=name):
                    reads.clear()
                    changed = dict(copy.deepcopy(receipt), ocr_receipts=copy.deepcopy(entries))
                    with self.assertRaises(enrollment.ExtractionBindingError) as caught:
                        helper(changed, ids, **kwargs)
                    self.assertEqual(str(caught.exception), BINDING)
                    self.assertEqual(reads, [])
        # Evidence never promotes: the visual-review hold stays and no stage moves.
        subject = result["original_sha256"]
        with self.store.ledger(self.database, readonly=True) as con:
            holds = con.execute("SELECT page_no,needs_visual_review FROM page_state WHERE original_sha256=? "
                                "ORDER BY page_no", (subject,)).fetchall()
            stages = dict(con.execute("SELECT stage,status FROM stage_state WHERE original_sha256=?", (subject,)))
        self.assertEqual([tuple(h) for h in holds], [(1, 1), (2, 1)])
        self.assertEqual(set(stages.values()), {"pending"})
        self.assertIn("review", stages)


if __name__ == "__main__":
    unittest.main()
