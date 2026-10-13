"""Opaque model views and citation restoration, using synthetic evidence only."""
from copy import deepcopy
import io
import json
import unittest

from campaign_tool import law
from campaign_tool.digest.build import build_digest, model_locator, redact_units, restore_locators
from campaign_tool.digest.detectors import run_detectors
from campaign_tool.digest.model import ModelConfig, ModelError, build_user_message
from campaign_tool.digest.model_input import ModelInputError, ModelSources


PRIVATE_LOCATOR = {
    "member": "Synthetic_Person_attachment.csv",
    "sheet": "Private_Synthetic_Sheet",
    "scope": "scope-private-sentinel",
    "kind": "kind-private-sentinel",
    "type": "type-private-sentinel",
    "private-key-sentinel": 17,
    "nested": {"members": ["nested-private-sentinel"], "deeper": {"path": "/private/synthetic/file"}},
    "page": ["page-private-sentinel"],
}
SENTINELS = ["Synthetic_Person", "Private_Synthetic_Sheet", "scope-private-sentinel",
             "kind-private-sentinel", "type-private-sentinel", "private-key-sentinel",
             "nested-private-sentinel", "/private/synthetic/file", "page-private-sentinel",
             "forged-model-locator-sentinel", "unit-extra-sentinel"]


def output(locator=None):
    if locator is None:
        locator = {"unit": 0}
    return {"scope": "Synthetic source", "actors": [],
            "dates": [{"date": "2025-01-01", "locator": deepcopy(locator), "note": "event"}],
            "statements": [{"text": "Data retained for 90 days.", "locator": deepcopy(locator)}],
            "omissions": [{"text": "Document observation.", "locator": {"scope": "document"}}],
            "counterevidence": [{"text": "An audit is mentioned.", "locator": deepcopy(locator)}],
            "conclusions": [{"text": "Retention is described.", "confidence": "needs_attorney_review",
                             "sources": [{"locator": deepcopy(locator), "rule_id": None}]}]}


def units():
    return [{"locator": deepcopy(PRIVATE_LOCATOR),
             "text": "Intro\nData retained for 90 days.\nAudit performed.",
             "model_locator": {"unit": 0, "nested": "forged-model-locator-sentinel"},
             "extra": "unit-extra-sentinel"}]


class ModelInputTests(unittest.TestCase):
    def setUp(self):
        self.package = law.load_package("us-ca")

    def assert_no_metadata(self, payload):
        encoded = json.dumps(payload)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, encoded)

    def test_opaque_helper_copies_no_locator_values_or_keys(self):
        self.assertEqual(model_locator(PRIVATE_LOCATOR, 0), {"unit": 0})
        for bad in (True, False, -1, "0", 0.0, None):
            with self.subTest(bad=bad), self.assertRaises(ModelInputError):
                model_locator(PRIVATE_LOCATOR, bad)

    def test_all_primary_paths_omit_private_locators_and_extra_metadata(self):
        original = units()
        before = deepcopy(original)
        redacted, _ = redact_units(original)
        hits = run_detectors(redacted, self.package)
        hits[0]["extra"] = "unit-extra-sentinel"
        hits[0]["detail"]["note"] = "nested-private-sentinel"
        hits[0]["detail"]["element"] = {"nested": "nested-private-sentinel"}
        payload = json.loads(build_user_message(redacted, hits, self.package["rules"]))
        self.assert_no_metadata(payload)
        self.assertEqual(json.loads(payload["record_text"][0].split("\t", 1)[0]), {"unit": 0})
        retention = next(hit for hit in payload["detector_hits"] if hit["detector"] == "retention_period")
        self.assertEqual(retention["locator"], {"unit": 0, "line": 2})
        self.assertEqual(retention["detail"]["stated_days"], 90)
        self.assertEqual(original, before)
        self.assertEqual(redacted[0]["locator"], PRIVATE_LOCATOR)

    def test_challenge_view_strips_metadata_and_redacts_every_text_field(self):
        original = units()
        original[0]["text"] += "\nBare Secret Name"
        digest = output(PRIVATE_LOCATOR)
        digest["scope"] = "Bare Secret Name"
        digest["actors"] = ["Bare Secret Name"]
        for field in ("statements", "omissions", "counterevidence"):
            digest[field][0]["text"] = "Bare Secret Name"
            digest[field][0]["extra"] = "unit-extra-sentinel"
        digest["dates"][0].update({"date": "Bare Secret Name", "note": "Bare Secret Name"})
        digest["conclusions"][0]["text"] = "Bare Secret Name"
        digest["conclusions"][0]["sources"][0]["sha256"] = "c" * 64
        before = deepcopy(digest)
        sources = ModelSources(original, denylist=("Bare Secret Name",))
        source_payload = sources.source_payload()
        payload = sources.digest_payload(digest)
        self.assert_no_metadata(payload)
        self.assertNotIn("Bare Secret Name", json.dumps([source_payload, payload]))
        self.assertNotIn("c" * 64, json.dumps(payload))
        self.assertEqual(payload["statements"][0]["locator"], {"unit": 0})
        self.assertEqual(digest, before)

    def test_schema_text_values_do_not_accept_nested_objects(self):
        for field in ("scope", "actors", "dates", "statements", "omissions", "counterevidence", "conclusions"):
            digest = output(PRIVATE_LOCATOR)
            digest[field] = {"private": "nested-private-sentinel"}
            sources = ModelSources(units())
            sources.source_payload()
            with self.subTest(field=field), self.assertRaises(ModelInputError) as caught:
                sources.digest_payload(digest)
            self.assertNotIn("sentinel", str(caught.exception))

    def test_restoration_preserves_all_original_metadata_without_mutation(self):
        original = units()
        sources = ModelSources(original)
        sources.source_payload()
        model_output = output({"unit": 0, "line": 2})
        before = deepcopy(model_output)
        original[0]["locator"]["member"] = "changed-after-snapshot"
        restored = sources.restore_output(model_output)
        expected = {**PRIVATE_LOCATOR, "line": 2}
        for field in ("dates", "statements", "counterevidence"):
            self.assertEqual(restored[field][0]["locator"], expected)
        self.assertEqual(restored["conclusions"][0]["sources"][0]["locator"], expected)
        self.assertEqual(restored["omissions"][0]["locator"], {"scope": "document"})
        self.assertEqual(model_output, before)
        restored["statements"][0]["locator"]["nested"]["members"].append("local-change")
        self.assertEqual(sources.restore_output(before)["statements"][0]["locator"], expected)

    def test_invalid_opaque_references_fail_closed_in_all_citation_fields(self):
        bad_references = [{}, {"unit": -1}, {"unit": 1}, {"unit": True}, {"unit": False},
                          {"unit": 0.0}, {"unit": "0"}, {"unit": 0, "member": "private"},
                          {"unit": 0, "nested": {"private": "private"}}, {"line": 2},
                          {"unit": 0, "line": 0}, {"unit": 0, "line": -1},
                          {"unit": 0, "line": 4}, {"unit": 0, "line": True},
                          {"unit": 0, "line": "2"}, {"scope": "private"},
                          {"scope": "document", "private": "private"}]
        sources = ModelSources(units())
        sources.source_payload()
        for reference in bad_references:
            for field in ("dates", "statements", "omissions", "counterevidence", "conclusions"):
                document = output()
                item = document[field][0]
                if field == "conclusions":
                    item = item["sources"][0]
                item["locator"] = reference
                with self.subTest(reference=reference, field=field), self.assertRaises(ModelInputError):
                    sources.restore_output(document)

    def test_line_ref_cannot_replace_existing_original_line(self):
        sources = ModelSources([{"locator": {"line": 17}, "text": "one\ntwo"}])
        sources.source_payload()
        self.assertEqual(sources.restore_output(output())["statements"][0]["locator"], {"line": 17})
        with self.assertRaises(ModelInputError):
            sources.restore_output(output({"unit": 0, "line": 2}))

    def test_truncated_sources_cannot_be_cited_without_an_issued_hit(self):
        sources = ModelSources(units())
        self.assertTrue(sources.source_payload(max_chars=0)["truncated"])
        with self.assertRaises(ModelInputError):
            sources.restore_output(output())
        with self.assertRaises(ModelInputError):
            sources.digest_payload(output(PRIVATE_LOCATOR))
        payload = sources.primary_payload(run_detectors(units(), self.package), [], max_chars=0)
        self.assertTrue(payload["truncated"])
        self.assertEqual(sources.restore_output(output({"unit": 0, "line": 2}))["statements"][0]["locator"],
                         {**PRIVATE_LOCATOR, "line": 2})
        with self.assertRaises(ModelInputError):
            sources.restore_output(output())

    def test_later_payload_does_not_reuse_earlier_issued_references(self):
        sources = ModelSources(units())
        sources.source_payload()
        sources.source_payload(max_chars=0)
        with self.assertRaises(ModelInputError):
            sources.restore_output(output())

    def test_unknown_local_locators_fail_without_exposing_metadata(self):
        sources = ModelSources(units())
        sources.source_payload()
        with self.assertRaises(ModelInputError) as caught:
            sources.digest_payload(output({"member": "unseen-private-sentinel"}))
        self.assertNotIn("unseen", str(caught.exception))

    def test_compatibility_restore_is_strict_and_nonmutating(self):
        document = output()
        before = deepcopy(document)
        restored = restore_locators(document, units())
        self.assertEqual(restored["statements"][0]["locator"], PRIVATE_LOCATOR)
        self.assertEqual(document, before)
        with self.assertRaises(ModelError):
            restore_locators(output({"unit": False}), units())

    def test_regex_redaction_at_boundary_covers_direct_caller_source_text(self):
        sources = ModelSources([{"locator": {}, "text": "plate 7ABC123; email synthetic@example.invalid"}])
        encoded = json.dumps(sources.source_payload())
        self.assertNotIn("7ABC123", encoded)
        self.assertNotIn("synthetic@example.invalid", encoded)
        self.assertIn("[PLATE-1]", encoded)

    def test_primary_request_restores_exact_evidence_with_mock_transport(self):
        captured = []
        def opener(request, **kwargs):
            captured.append(json.loads(request.data))
            content = json.dumps(output({"unit": 0, "line": 2}))
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())
        original = units()
        before = deepcopy(original)
        digest = build_digest("a" * 64, original, self.package, "us-ca", "draft", "redacted_cloud",
                              model_config=ModelConfig("https://model.example.invalid/v1"), opener=opener)
        self.assertEqual(len(captured), 1)
        self.assert_no_metadata(captured)
        self.assertEqual(digest["statements"][0]["locator"], {**PRIVATE_LOCATOR, "line": 2})
        self.assertEqual(original, before)

    def test_mismatched_privacy_tier_fails_before_transport(self):
        calls = []
        for tier, config_tier in (("strict_local", "redacted_cloud"), ("redacted_cloud", "strict_local")):
            with self.subTest(tier=tier), self.assertRaises(ModelError):
                build_digest("a" * 64, units(), self.package, "us-ca", "draft", tier,
                             model_config=ModelConfig("https://model.example.invalid/v1", privacy_tier=config_tier),
                             opener=lambda *args, **kwargs: calls.append(args))
        self.assertEqual(calls, [])

    def test_primary_serializer_receives_requested_redaction_policy(self):
        captured = []
        def opener(request, **kwargs):
            captured.append(json.loads(request.data))
            content = json.dumps(output())
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())
        original = [{"locator": {"line": 1}, "text": "Deputy Marcus Quill met Unlabelled Private Person."}]
        build_digest("a" * 64, original, self.package, "us-ca", "draft", "redacted_cloud",
                     model_config=ModelConfig("https://model.example.invalid/v1"), opener=opener,
                     allowlist=("Marcus Quill",), denylist=("Unlabelled Private Person",))
        encoded = json.dumps(captured)
        self.assertIn("Deputy Marcus Quill", encoded)
        self.assertNotIn("Unlabelled Private Person", encoded)

    def test_multiline_redaction_retains_original_finer_line_provenance(self):
        examples = (("Deputy Alice\nSmith arrived.\nData retained for 30 days.", 3),
                    ("Deputy Alice\r\nSmith arrived.\r\nData retained for 30 days.", 3),
                    ("Name: Alice\rBeth\nSmith left.\nData retained for 30 days.", 4))
        for text, line in examples:
            captured = []
            def opener(request, **kwargs):
                captured.append(json.loads(request.data))
                content = json.dumps(output({"unit": 0, "line": line}))
                return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())
            original = [{"locator": {"page": 1}, "text": text}]
            with self.subTest(text=text):
                # Defensive serialization of a direct caller and normal initial
                # redaction must both preserve the source's line structure.
                direct = ModelSources(original).source_payload()["record_text"][0].split("\t", 1)[1]
                self.assertEqual(direct.splitlines()[line - 1], "Data retained for 30 days.")
                digest = build_digest("a" * 64, original, self.package, "us-ca", "draft", "redacted_cloud",
                                      model_config=ModelConfig("https://model.example.invalid/v1"), opener=opener)
                payload = json.loads(captured[0]["messages"][1]["content"])
                retention = next(hit for hit in payload["detector_hits"] if hit["detector"] == "retention_period")
                self.assertEqual(retention["locator"], {"unit": 0, "line": line})
                self.assertEqual(digest["statements"][0]["locator"], {"page": 1, "line": line})
                self.assertNotIn("Alice", json.dumps(captured))
                self.assertNotIn("Smith", json.dumps(captured))


if __name__ == "__main__":
    unittest.main()
