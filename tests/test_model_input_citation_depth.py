"""Malformed opaque model citations refuse cleanly before any recursive copy.

Synthetic units and in-memory transport only; no sockets or listeners.
"""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import unittest
from unittest import mock

from campaign_tool import law
from campaign_tool.digest import model_input
from campaign_tool.digest.build import build_digest, redact_units, restore_locators
from campaign_tool.digest.detectors import run_detectors
from campaign_tool.digest.model import ModelConfig, ModelError, narrative
from campaign_tool.digest.model_input import ModelInputError, ModelSources
from campaign_tool.digest.schema import validate_model_output


FIXED = "model input or locator references failed validation"
MARKER = "synthetic-deep-marker"
FIELDS = ("dates", "statements", "omissions", "counterevidence", "conclusions")
LOCAL_LOCATOR = {"member": "synthetic_member.csv", "nested": {"keep": ["local-original-metadata"]}}


def deep(depth=600):
    node = {"marker": MARKER}
    for _ in range(depth):
        node = {"n": node}
    return node


def units():
    return [{"locator": deepcopy(LOCAL_LOCATOR), "text": "Intro\nData retained for 90 days.\nAudit performed."},
            {"locator": {"page": 2}, "text": "Second unit\nWith two lines."}]


def output(locator=None):
    locator = {"unit": 0} if locator is None else locator
    # Shallow copies: a deliberately deep rejected locator must not be copied
    # by this fixture helper either.
    return {"scope": "Synthetic source", "actors": [],
            "dates": [{"date": "2025-01-01", "locator": dict(locator), "note": "event"}],
            "statements": [{"text": "Data retained for 90 days.", "locator": dict(locator)}],
            "omissions": [{"text": "Document observation.", "locator": {"scope": "document"}}],
            "counterevidence": [{"text": "An audit is mentioned.", "locator": dict(locator)}],
            "conclusions": [{"text": "Retention is described.", "confidence": "needs_attorney_review",
                             "sources": [{"locator": dict(locator), "rule_id": None}]}]}


def contains_marker(value):
    """Iterative search, so the test helper itself never recurses deeply."""
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif node == MARKER:
            return True
    return False


def set_locator(document, field, reference, index=0):
    item = document[field][0]
    if field == "conclusions":
        item = item["sources"][0]
    item["locator"] = reference
    return document


def deep_document(field):
    """A real-JSON-decoded model reply whose only fault is a deep extra key."""
    document = set_locator(output(), field, {"unit": 0, "extra": deep()})
    return json.loads(json.dumps(document))


def reply(document):
    content = json.dumps(document)
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


class CitationDepthTests(unittest.TestCase):
    def setUp(self):
        self.package = law.load_package("us-ca")

    def sources(self):
        sources = ModelSources(units())
        sources.source_payload()
        return sources

    def test_json_decodable_deep_extra_locator_refuses_every_citation_field(self):
        for field in FIELDS:
            document = deep_document(field)
            validate_model_output(document)  # the schema alone accepts it
            with self.subTest(field=field), self.assertRaises(ModelInputError) as caught:
                self.sources().restore_output(document)
            self.assertEqual(str(caught.exception), FIXED)
        shallow = set_locator(output(), "statements", {"unit": 0, "extra": 1})
        with self.assertRaises(ModelInputError) as caught:
            self.sources().restore_output(shallow)
        self.assertEqual(str(caught.exception), FIXED)

    def test_bad_later_reference_refuses_the_whole_restoration(self):
        for field in FIELDS:
            document = output({"unit": 0, "line": 2})
            if field == "conclusions":
                document[field][0]["sources"].append({"locator": {"unit": 0, "extra": deep()}, "rule_id": None})
            else:
                document[field].append({**deepcopy(document[field][0]), "locator": {"unit": 0, "extra": deep()}})
            with self.subTest(field=field), self.assertRaises(ModelInputError):
                self.sources().restore_output(document)

    def test_invalid_reference_is_not_copied_or_serialized(self):
        bad_references = [{"unit": 0, "extra": deep()}, {"unit": deep()}, {"unit": 0, "line": deep()},
                          {"unit": [deep()]}, {"unit": 0, "line": [deep()]}, {"scope": deep()},
                          {"scope": [deep()]}, {"scope": "document", "extra": deep()}]
        for reference in bad_references:
            for field in FIELDS:
                sources = self.sources()
                document = set_locator(output(), field, reference)
                keyed = []
                real_key = model_input._key

                def recording_key(locator):
                    keyed.append(locator)
                    return real_key(locator)

                def guarded_copy(value, *args):
                    # Local original metadata may be copied; the whole document
                    # and any rejected reference subtree may not.
                    if value is document or contains_marker(value):
                        raise AssertionError("rejected citation was copied")
                    return deepcopy(value, *args)

                with self.subTest(reference=list(reference), field=field), \
                        mock.patch.object(model_input, "_key", recording_key), \
                        mock.patch.object(model_input, "deepcopy", guarded_copy), \
                        self.assertRaises(ModelInputError):
                    sources.restore_output(document)
                for locator in keyed:
                    self.assertTrue(all(type(value) in (int, str) for value in locator.values()))

    def test_closed_reference_shape_scalar_and_issuance_matrix(self):
        bad = [{}, {"unit": -1}, {"unit": 2}, {"unit": True}, {"unit": False}, {"unit": 0.0}, {"unit": "0"},
               {"unit": None}, {"unit": [0]}, {"unit": {"a": 1}}, {"line": 2}, {"unit": 0, "extra": 1},
               {"unit": 0, "line": 0}, {"unit": 0, "line": -1}, {"unit": 0, "line": 9},
               {"unit": 0, "line": True}, {"unit": 0, "line": 2.0}, {"unit": 0, "line": "2"},
               {"unit": 0, "line": None}, {"unit": 0, "line": [2]}, {"scope": "private"},
               {"scope": None}, {"scope": 1}, {"scope": "document", "extra": 1}, {"scope": "document", "unit": 0},
               {1: "x"}, [], "unit", 0, True]
        sources = self.sources()
        for reference in bad:
            for field in FIELDS:
                document = set_locator(output(), field, reference)
                with self.subTest(reference=repr(reference), field=field), self.assertRaises(ModelInputError) as caught:
                    sources.restore_output(document)
                self.assertEqual(str(caught.exception), FIXED)
        restored = sources.restore_output(output({"unit": 0, "line": 2}))
        self.assertEqual(restored["statements"][0]["locator"], {**LOCAL_LOCATOR, "line": 2})
        self.assertEqual(restored["omissions"][0]["locator"], {"scope": "document"})
        self.assertEqual(sources.restore_output(output({"unit": 1}))["statements"][0]["locator"], {"page": 2})

    def test_invalid_reference_never_grants_issuance_and_unissued_units_refuse(self):
        sources = ModelSources(units())
        sources.source_payload(max_chars=0)  # nothing issued except the document sentinel
        for reference in ({"unit": 0, "extra": deep()}, {"unit": 0}, {"unit": 1, "line": 1}):
            with self.subTest(reference=list(reference)), self.assertRaises(ModelInputError):
                sources.restore_output(output(reference))
        with self.assertRaises(ModelInputError):
            sources.restore_output(output({"unit": 0}))
        self.assertEqual(sources.restore_output({**output({"scope": "document"})})["statements"][0]["locator"],
                         {"scope": "document"})
        # An exact line-only issued hit remains citable; a sibling line is not.
        hits = [{"detector": "retention_period", "rule_id": None, "locator": {**LOCAL_LOCATOR, "line": 2},
                 "excerpt": "x", "kind": "keyword", "detail": {}}]
        sources.primary_payload(hits, [], max_chars=0)
        self.assertEqual(sources.restore_output(output({"unit": 0, "line": 2}))["statements"][0]["locator"],
                         {**LOCAL_LOCATOR, "line": 2})
        with self.assertRaises(ModelInputError):
            sources.restore_output(output({"unit": 0, "line": 3}))

    def test_real_narrative_and_build_translate_deep_reference_refusal(self):
        config = ModelConfig("https://model.example.invalid/v1")
        calls = []

        def opener_for(document):
            def opener(request, **kwargs):
                calls.append(request)
                return io.BytesIO(reply(document))
            return opener

        original = units()
        redacted, _ = redact_units(original)
        hits = run_detectors(redacted, self.package)
        for field in FIELDS:
            opener = opener_for(deep_document(field))
            with self.subTest(field=field):
                with self.assertRaises(ModelError) as caught:
                    narrative(config, redacted, hits, self.package["rules"], opener=opener)
                self.assertEqual(str(caught.exception), FIXED)
                with self.assertRaises(ModelError) as caught:
                    build_digest("a" * 64, original, self.package, "us-ca", "draft", "redacted_cloud",
                                 model_config=config, opener=opener)
                self.assertEqual(str(caught.exception), FIXED)
        self.assertEqual(len(calls), 2 * len(FIELDS))
        digest = build_digest("a" * 64, original, self.package, "us-ca", "draft", "redacted_cloud",
                              model_config=config, opener=opener_for(output({"unit": 0, "line": 2})))
        self.assertEqual(digest["statements"][0]["locator"], {**LOCAL_LOCATOR, "line": 2})

    def test_compatibility_restore_and_repeated_calls_remain_detached(self):
        local = units()
        before = deepcopy(local)
        good = restore_locators(output(), local)
        self.assertEqual(good["statements"][0]["locator"], LOCAL_LOCATOR)
        for field in FIELDS:
            with self.subTest(field=field), self.assertRaises(ModelError) as caught:
                restore_locators(deep_document(field), local)
            self.assertEqual(str(caught.exception), "model locator references failed validation")
        good["statements"][0]["locator"]["nested"]["keep"].append("changed")
        again = restore_locators(output(), local)
        self.assertEqual(again["statements"][0]["locator"], LOCAL_LOCATOR)
        self.assertEqual(local, before)
        # Repeated source_payload truncation still clears earlier references.
        sources = ModelSources(units())
        sources.source_payload()
        self.assertEqual(sources.restore_output(output())["statements"][0]["locator"], LOCAL_LOCATOR)
        sources.source_payload(max_chars=0)
        with self.assertRaises(ModelInputError):
            sources.restore_output(output())

    def test_refusal_is_value_free_and_nonmutating(self):
        sources = self.sources()
        document = set_locator(output(), "statements", {"unit": 0, "secret-extra-key": deep()})
        document["conclusions"][0]["sources"][0]["locator"] = {"unit": 0}
        encoded = json.dumps(document)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertRaises(ModelInputError) as caught:
            sources.restore_output(document)
        text = repr(caught.exception) + str(caught.exception)
        for fragment in (MARKER, "secret-extra-key", "synthetic_member", "local-original-metadata", "Retention"):
            self.assertNotIn(fragment, text)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertEqual(json.dumps(document), encoded)
        self.assertIsNone(caught.exception.__cause__)
        valid = output()
        valid_before = deepcopy(valid)
        sources.restore_output(valid)
        self.assertEqual(valid, valid_before)

    def test_optional_null_and_legacy_payloads_are_unchanged(self):
        sources = self.sources()
        document = output()
        document["dates"][0]["locator"] = None
        del document["statements"][0]["locator"]
        document["omissions"][0]["locator"] = None
        restored = sources.restore_output(document)
        self.assertIsNone(restored["dates"][0]["locator"])
        self.assertNotIn("locator", restored["statements"][0])
        self.assertIsNone(restored["omissions"][0]["locator"])
        self.assertEqual(restored["counterevidence"][0]["locator"], LOCAL_LOCATOR)
        for bad_source in ({"locator": None, "rule_id": None}, {"rule_id": None}):
            document = output()
            document["conclusions"][0]["sources"] = [bad_source]
            with self.subTest(source=bad_source), self.assertRaises(ModelInputError):
                sources.restore_output(document)
        payload_before = json.dumps(ModelSources(units()).source_payload(), sort_keys=True)
        self.assertEqual(json.dumps(ModelSources(units()).source_payload(), sort_keys=True), payload_before)
        with self.assertRaises(ModelInputError):
            sources.restore_output([])


if __name__ == "__main__":
    unittest.main()
