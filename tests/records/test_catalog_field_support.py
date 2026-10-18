"""Catalog source-locator regressions; synthetic text, no intake or promotions."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from campaign_tool.records.run import Pipeline


class CatalogFieldSupportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="catalog-field-support-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.subject = hashlib.sha256(b"synthetic original").hexdigest()
        # Do not construct Pipeline: that prepares a native intake root.
        self.pipeline = Pipeline.__new__(Pipeline)
        self.pipeline.root = SimpleNamespace(sub=lambda name: self.root)
        self.pipeline._headers = lambda subject: {}
        self.pipeline._original = lambda subject: {"mime_detected": "application/pdf"}
        self.cards = []
        self.stage = {"catalog": SimpleNamespace(process=self.capture)}

    def capture(self, path, expected_sha256, *, author_id):
        raw = path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), expected_sha256)
        self.assertEqual(author_id, "records-cataloger")
        self.cards.append(json.loads(raw))
        # This observer does not accept a ledger stage or certify a catalog.
        return {"status": "pending"}

    def unit(self, page, text, kind="page"):
        return {"unit_id": f"synthetic-unit-{page}", "text": text,
                "locator_text": f"page:{page}", "unit_type": kind,
                "artifact_sha256": hashlib.sha256(text.encode()).hexdigest()}

    def catalog(self, units):
        self.pipeline._units = lambda subject: units
        result = self.pipeline.stage_catalog(self.subject, self.stage)
        self.assertEqual(result["status"], "pending")
        card = self.cards[-1]
        source = {unit["unit_id"]: unit for unit in units}
        proofs = {proof["unit_id"]: proof for proof in card["supports"]}
        self.assertEqual(len(proofs), len(card["supports"]))
        for identity, proof in proofs.items():
            self.assertEqual(proof["source_sha256"], self.subject)
            self.assertEqual(proof["artifact_sha256"], source[identity]["artifact_sha256"])
            self.assertEqual(proof["locator"], source[identity]["locator_text"])
            self.assertTrue(proof["quote"])
            self.assertIn(proof["quote"], source[identity]["text"])
            self.assertLessEqual(len(proof["quote"]), 400)
        for identities in card["field_support"].values():
            self.assertTrue(set(identities).issubset(proofs))
        return card, proofs

    def test_later_page_classification_cites_that_page(self):
        card, proofs = self.catalog([self.unit(1, "Cover sheet"), self.unit(2, "Retention policy")])
        self.assertEqual(card["metadata"]["doc_type"], "policy")
        self.assertEqual(card["field_support"]["doc_type"], ["synthetic-unit-2"])
        self.assertEqual(card["field_support"]["summary"], ["synthetic-unit-1"])
        self.assertEqual(set(proofs), {"synthetic-unit-1", "synthetic-unit-2"})

    def test_type_quote_includes_late_hint_in_same_unit(self):
        card, proofs = self.catalog([self.unit(1, "Cover material. " * 80 + "POLICY details")])
        self.assertEqual(card["metadata"]["doc_type"], "policy")
        self.assertIn("POLICY", proofs["synthetic-unit-1"]["quote"])

    def test_unicode_case_expansion_keeps_original_quote_coordinates(self):
        card, proofs = self.catalog([self.unit(1, "\u0130" * 500 + "POLICY")])
        self.assertEqual(card["metadata"]["doc_type"], "policy")
        self.assertEqual(proofs["synthetic-unit-1"]["quote"], "POLICY")

    def test_hint_priority_preserved_with_its_actual_source(self):
        card, _ = self.catalog([self.unit(1, "Invoice"), self.unit(2, "Agreement"), self.unit(3, "Policy")])
        self.assertEqual(card["metadata"]["doc_type"], "policy")
        self.assertEqual(card["field_support"]["doc_type"], ["synthetic-unit-3"])

    def test_existing_fifty_unit_classification_bound_is_preserved(self):
        units = [self.unit(page, "Cover material") for page in range(1, 51)]
        card, proofs = self.catalog(units + [self.unit(51, "Policy")])
        self.assertEqual(card["metadata"]["doc_type"], "other")
        self.assertNotIn("synthetic-unit-51", proofs)

    def test_mail_header_without_text_support_uses_body_title(self):
        self.pipeline._headers = lambda subject: {"subject": "Unsupported header title"}
        self.pipeline._original = lambda subject: {"mime_detected": "message/rfc822"}
        card, _ = self.catalog([self.unit(1, "Produced records attached.", "email_body")])
        self.assertEqual(card["metadata"]["title"], "Produced records attached.")
        self.assertEqual(card["metadata"]["doc_type"], "correspondence")

    def test_mail_title_present_in_later_unit_cites_its_source(self):
        title = "Synthetic response title"
        self.pipeline._headers = lambda subject: {"subject": title}
        self.pipeline._original = lambda subject: {"mime_detected": "message/rfc822"}
        card, proofs = self.catalog([self.unit(1, "Produced records attached.", "email_body"),
                                    self.unit(2, "Source heading: " + title)])
        self.assertEqual(card["metadata"]["title"], title)
        self.assertEqual(card["field_support"]["title"], ["synthetic-unit-2"])
        self.assertIn(title, proofs["synthetic-unit-2"]["quote"])

    def test_identical_replay_keeps_card_bytes_and_unique_supports(self):
        units = [self.unit(1, "Policy document")]
        self.catalog(units)
        before = (self.root / (self.subject + "-card.json")).read_bytes()
        self.catalog(units)
        self.assertEqual((self.root / (self.subject + "-card.json")).read_bytes(), before)

    def test_empty_text_keeps_existing_inapplicable_path(self):
        self.pipeline._units = lambda subject: [self.unit(1, "  \n")]
        calls = []
        self.pipeline._inapplicable = lambda *args: calls.append(args) or {"status": "inapplicable"}
        result = self.pipeline.stage_catalog(self.subject, self.stage)
        self.assertEqual(result, {"status": "inapplicable"})
        self.assertEqual(calls, [(self.subject, self.stage, "catalog", "no_text_units")])
        self.assertEqual(self.cards, [])
