"""A refused primary payload must leave no issued citation references (synthetic data)."""
from copy import deepcopy
import io
import json
import unittest

from campaign_tool import law
from campaign_tool.digest.model import ModelConfig, ModelError, build_user_message, narrative
from campaign_tool.digest.model_input import ModelInputError, ModelSources


LOCATOR = {"page": 7}
FIXED = "model input or locator references failed validation"
DOCUMENT = {"scope": "document"}
FIELDS = ("dates", "statements", "omissions", "counterevidence", "conclusions")


def make_units():
    return [{"locator": deepcopy(LOCATOR), "text": "alpha line\nbeta line"}]


def hit(line=2, **over):
    value = {"detector": "retention_period", "rule_id": None, "locator": {**LOCATOR, "line": line},
             "excerpt": "beta line", "kind": "duration", "detail": {"stated_days": 90}}
    value.update(over)
    return value


def rule(**over):
    value = {"rule_id": "r1", "citation": "Synthetic Code 1", "duty": "Retain records."}
    value.update(over)
    return value


def document(reference):
    """A schema-shaped model output citing ``reference`` from every holder."""
    return {"scope": "Synthetic", "actors": [],
            "dates": [{"date": "2025-01-01", "locator": deepcopy(reference), "note": "n"}],
            "statements": [{"text": "s", "locator": deepcopy(reference)}],
            "omissions": [{"text": "o", "locator": deepcopy(reference)}],
            "counterevidence": [{"text": "c", "locator": deepcopy(reference)}],
            "conclusions": [{"text": "k", "confidence": "needs_attorney_review",
                             "sources": [{"locator": deepcopy(reference), "rule_id": None}]}]}


def accepted(sources, reference):
    try:
        sources.restore_output(document(reference))
    except ModelInputError:
        return False
    return True


def view(sources):
    """Which opaque references currently restore."""
    return {"document": accepted(sources, DOCUMENT), "line1": accepted(sources, {"unit": 0, "line": 1}),
            "line2": accepted(sources, {"unit": 0, "line": 2}), "whole": accepted(sources, {"unit": 0})}


NOTHING = {"document": False, "line1": False, "line2": False, "whole": False}
LINE2_ONLY_PLUS_DOCUMENT = {"document": True, "line1": False, "line2": True, "whole": False}
FULL = {"document": True, "line1": True, "line2": True, "whole": True}
DOCUMENT_ONLY = {"document": True, "line1": False, "line2": False, "whole": False}


class FailedIssuanceTests(unittest.TestCase):
    def assert_refused_everywhere(self, sources):
        self.assertEqual(view(sources), NOTHING)
        self.assertEqual(sources._issued, set())
        # Every citation holder, one at a time, refuses each reference.
        for reference in (DOCUMENT, {"unit": 0}, {"unit": 0, "line": 1}, {"unit": 0, "line": 2}):
            for field in FIELDS:
                output = {"scope": "Synthetic", "actors": [], "dates": [], "statements": [],
                          "omissions": [], "counterevidence": [], "conclusions": []}
                if field == "dates":
                    output[field] = [{"date": "d", "locator": deepcopy(reference)}]
                elif field == "conclusions":
                    output[field] = [{"text": "k", "confidence": "needs_attorney_review",
                                      "sources": [{"locator": deepcopy(reference), "rule_id": None}]}]
                else:
                    output[field] = [{"text": "t", "locator": deepcopy(reference)}]
                with self.subTest(reference=reference, field=field), self.assertRaises(ModelInputError) as caught:
                    sources.restore_output(output)
                self.assertEqual(str(caught.exception), FIXED)
        # Challenge serialization with original local locators also refuses.
        for local in (DOCUMENT, LOCATOR, {**LOCATOR, "line": 2}):
            with self.subTest(local=local), self.assertRaises(ModelInputError) as caught:
                sources.digest_payload(document(local))
            self.assertEqual(str(caught.exception), FIXED)

    def refuse(self, sources, hits, rules, max_chars=60000):
        with self.assertRaises(ModelInputError) as caught:
            sources.primary_payload(hits, rules, max_chars=max_chars)
        self.assertEqual(str(caught.exception), FIXED)

    def test_failed_primary_revokes_source_and_document_references(self):
        sources = ModelSources(make_units())
        self.refuse(sources, [hit()], [rule(), rule(duty=None)])
        self.assert_refused_everywhere(sources)
        for bad_rule in (rule(rule_id=None), rule(citation=3), rule(duty=None), "not-a-dict"):
            sources = ModelSources(make_units())
            self.refuse(sources, [], [bad_rule])
            self.assert_refused_everywhere(sources)

    def test_failed_hit_field_does_not_authorize_its_line(self):
        healthy = ModelSources(make_units())
        healthy.primary_payload([hit()], [], max_chars=0)
        self.assertEqual(view(healthy), LINE2_ONLY_PLUS_DOCUMENT)
        for name, value in (("excerpt", None), ("kind", None), ("detail", None), ("detector", 5)):
            sources = ModelSources(make_units())
            with self.subTest(field=name):
                self.refuse(sources, [hit(**{name: value})], [], max_chars=0)
                self.assert_refused_everywhere(sources)

    def test_later_bad_hit_or_rule_revokes_earlier_valid_hits(self):
        bad_hits = ("not-a-dict", hit(locator={"page": 99}), hit(excerpt=None), hit(locator=None))
        for max_chars in (0, 60000):
            for bad in bad_hits:
                sources = ModelSources(make_units())
                with self.subTest(max_chars=max_chars, bad=bad):
                    self.refuse(sources, [hit(line=1), bad], [], max_chars=max_chars)
                    self.assert_refused_everywhere(sources)
            for rules in ([rule(duty=None)], [rule(), rule(rule_id=None)]):
                sources = ModelSources(make_units())
                with self.subTest(max_chars=max_chars, rules=len(rules)):
                    self.refuse(sources, [hit(line=1), hit()], rules, max_chars=max_chars)
                    self.assert_refused_everywhere(sources)

    def test_failed_primary_invalidates_prior_success_not_restores_it(self):
        sources = ModelSources(make_units())
        sources.source_payload()
        self.assertEqual(view(sources), FULL)
        self.refuse(sources, [hit()], [rule(duty=None)], max_chars=0)
        self.assert_refused_everywhere(sources)

        # Invalid budget fails before source_payload would clear: primary still revokes.
        sources = ModelSources(make_units())
        sources.source_payload()
        for bad_budget in (-1, "5", True, 1.5):
            sources.source_payload()
            self.refuse(sources, [], [], max_chars=bad_budget)
            self.assert_refused_everywhere(sources)

        # Standalone source_payload keeps its validate-before-clear behaviour.
        sources = ModelSources(make_units())
        sources.source_payload()
        with self.assertRaises(ModelInputError):
            sources.source_payload(max_chars=-1)
        self.assertEqual(view(sources), FULL)

    def test_success_failure_success_cycles_recover_without_union(self):
        sources = ModelSources(make_units())
        sources.source_payload()
        self.assertEqual(view(sources), FULL)
        sources.primary_payload([hit()], [], max_chars=0)
        self.assertEqual(view(sources), LINE2_ONLY_PLUS_DOCUMENT)
        self.refuse(sources, [hit(line=1), hit(excerpt=None)], [], max_chars=0)
        self.assert_refused_everywhere(sources)
        sources.source_payload(max_chars=0)
        self.assertEqual(view(sources), DOCUMENT_ONLY)
        sources.primary_payload([hit(line=1)], [rule()])
        self.assertEqual(view(sources), FULL)
        self.refuse(sources, [], [rule(duty=None)])
        self.assert_refused_everywhere(sources)
        payload = sources.primary_payload([hit()], [rule()], max_chars=0)
        self.assertTrue(payload["truncated"])
        self.assertEqual(view(sources), LINE2_ONLY_PLUS_DOCUMENT)
        sources.source_payload()
        self.assertEqual(view(sources), FULL)

    def test_post_failure_challenge_and_every_citation_holder_refuse(self):
        original = make_units()
        before = deepcopy(original)
        digest = document(LOCATOR)
        digest_before = deepcopy(digest)
        sources = ModelSources(original)
        sources.source_payload()
        self.assertEqual(sources.digest_payload(digest)["statements"][0]["locator"], {"unit": 0})
        self.refuse(sources, [hit()], [rule(duty=None)])
        with self.assertRaises(ModelInputError) as caught:
            sources.digest_payload(digest)
        self.assertEqual(str(caught.exception), FIXED)
        # Omission-only document citations cannot slip through.
        only_document = {"scope": "s", "actors": [], "dates": [], "statements": [], "counterevidence": [],
                         "omissions": [{"text": "o", "locator": DOCUMENT}], "conclusions": []}
        with self.assertRaises(ModelInputError):
            sources.digest_payload(only_document)
        with self.assertRaises(ModelInputError):
            sources.restore_output(only_document)
        self.assertEqual(original, before)
        self.assertEqual(digest, digest_before)
        self.assertEqual(sources._units[0]["locator"], LOCATOR)

    def test_successful_payload_contents_unchanged(self):
        sources = ModelSources(make_units(), denylist=("beta",))
        payload = sources.primary_payload([hit(line=1), hit(), hit(locator=DOCUMENT)], [rule()])
        self.assertEqual([h["locator"] for h in payload["detector_hits"]],
                         [{"unit": 0, "line": 1}, {"unit": 0, "line": 2}, DOCUMENT])
        self.assertEqual(payload["document_locator"], DOCUMENT)
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["rules"], [rule()])
        self.assertEqual(payload["detector_hits"][0]["detail"], {"stated_days": 90})
        self.assertNotIn("page", json.dumps(payload))
        self.assertNotIn("beta", json.dumps(payload))
        self.assertEqual(view(sources), FULL)

        bounded = ModelSources(make_units())
        payload = bounded.primary_payload([hit(), hit()], [], max_chars=0)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["record_text"], [])
        self.assertEqual(view(bounded), LINE2_ONLY_PLUS_DOCUMENT)
        # Hit locators carry original lines; a duplicate hit issues the same reference.
        self.assertEqual(len(bounded._issued), 2)

    def test_wrappers_refuse_before_fake_transport_and_retry_privacy_holds(self):
        package = law.load_package("us-ca")
        calls = []

        def opener(request, **kwargs):
            calls.append(json.loads(request.data))
            content = json.dumps(document({"unit": 0, "line": 2}))
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

        units = [{"locator": deepcopy(LOCATOR), "text": "Secret Person wrote.\nData retained for 30 days."}]
        bad_hits = [hit(excerpt=None)]
        config = ModelConfig("https://model.example.invalid/v1")
        with self.assertRaises(ModelError):
            build_user_message(units, bad_hits, package["rules"], denylist=("Secret Person",))
        with self.assertRaises(ModelError):
            narrative(config, units, bad_hits, package["rules"], opener=opener, denylist=("Secret Person",))
        self.assertEqual(calls, [])

        good = [hit(excerpt="Data retained for 30 days.")]
        result = narrative(config, units, good, package["rules"], opener=opener, denylist=("Secret Person",))
        self.assertEqual(len(calls), 1)
        encoded = json.dumps(calls)
        self.assertNotIn("Secret Person", encoded)
        self.assertNotIn('"page"', encoded)
        self.assertEqual(result["statements"][0]["locator"], {**LOCATOR, "line": 2})


if __name__ == "__main__":
    unittest.main()
