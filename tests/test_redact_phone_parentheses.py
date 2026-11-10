"""Compact parenthesized US phone numbers are removed whole before model serialization (synthetic data only)."""
import io
import json
import unittest
from copy import deepcopy

from campaign_tool import law
from campaign_tool.digest.build import build_digest, redact_units
from campaign_tool.digest.model import ModelConfig
from campaign_tool.digest.model_input import ModelSources
from campaign_tool.digest.redact import PATTERNS, Redactor, redact
from campaign_tool.finding_updates import UpdateError, validate_update_note

COMPACT = ["(805)555-0100", "(805)555.0100", "(805)555 0100", "+1(805)555-0100", "1(805)555-0100",
           "+1 (805)555-0100"]
PHONE = dict(PATTERNS)["PHONE"]


def _spans(text):
    return [m.span() for m in PHONE.finditer(text)]


class CompactPhoneTests(unittest.TestCase):
    def test_compact_parenthesized_phone_entire_span_is_removed(self):
        for value in COMPACT:
            for template, expected in (("{} was called.", "[PHONE-1] was called."),
                                       ("Call {} today.", "Call [PHONE-1] today."),
                                       ("Call {}", "Call [PHONE-1]")):
                with self.subTest(value=value, template=template):
                    result = redact(template.format(value))
                    self.assertEqual(result.text, expected)
                    self.assertEqual(result.counts, {"PHONE": 1})

    def test_legacy_formats_and_ordinary_text_remain_compatible(self):
        for value in ("(805) 555-0100", "805-555-0100", "805.555.0100", "+1 805 555 0100", "1-805-555-0100",
                      "8055550100", "805) 555-0100"):
            with self.subTest(value=value):
                result = redact("Contact " + value + " today.")
                self.assertEqual(result.text, "Contact [PHONE-1] today.")
                self.assertEqual(result.counts, {"PHONE": 1})
        for text in ("Filed 2026-11-10 for $1,500.00 under Cal. Gov. Code 7923.000 in version 1.2.3.",
                     "Section (805)555 applies.", "See (805)5550100 and (80)555-0100 and (805555-0100."):
            with self.subTest(text=text):
                self.assertEqual(redact(text).text, text)
                self.assertEqual(redact(text).counts, {})

    def test_compact_phone_respects_existing_boundaries(self):
        for text in ("x(805)555-0100", "7(805)555-0100", "_(805)555-0100", "(805)555-01001", "(805)555-0100x",
                     "(805)555-0100-1", "(805)555-0100_", "-(805)555-0100", ".(805)555-0100"):
            with self.subTest(text=text):
                self.assertEqual(_spans(text), [], text)
        self.assertEqual(_spans("Call (805)555-0100."), [(5, 18)])
        self.assertEqual(_spans("+1(805)555-0100"), [(0, 15)])
        self.assertEqual(_spans("+1 (805)555-0100,"), [(0, 16)])
        self.assertEqual(_spans("a (805)555-0100"), [(2, 15)])

    def test_allowlist_denylist_and_overlap_union_for_new_format(self):
        allowed = redact("Call (805)555-0100 now.", allowlist=["(805)555-0100"])
        self.assertEqual((allowed.text, allowed.counts), ("Call (805)555-0100 now.", {}))
        allowed = redact("Call +1(805)555-0100 now.", allowlist=["+1(805)555-0100"])
        self.assertEqual((allowed.text, allowed.counts), ("Call +1(805)555-0100 now.", {}))
        denied = redact("Call (805)555-0100 now.", denylist=["call (805)555"])
        self.assertEqual((denied.text, denied.counts), ("[DENYLIST-1] now.", {"DENYLIST": 1}))
        denied = redact("Call (805)555-0100 now.", denylist=["555-0100 now"])
        self.assertEqual((denied.text, denied.counts), ("Call [PHONE-1].", {"PHONE": 1}))

    def test_shared_tokens_reserved_placeholders_and_idempotence(self):
        redactor = Redactor()
        first = redactor.redact("A (805)555-0100 and (805)555-0101.")
        second = redactor.redact("B (805)555-0100.")
        self.assertEqual(first.text, "A [PHONE-1] and [PHONE-2].")
        self.assertEqual(second.text, "B [PHONE-1].")
        self.assertEqual(set(vars(first)), {"text", "counts"})
        reserved = redact("Literal [PHONE-1] then +1(805)555-0100.")
        self.assertEqual(reserved.text, "Literal [PHONE-1] then [PHONE-2].")
        again = redact(first.text)
        self.assertEqual((again.text, again.counts), (first.text, {}))

    def test_model_payloads_remove_compact_phone_without_moving_evidence(self):
        text = "Call +1(805)555-0100 or (805)555-0100.\nData retained for 30 days."
        units = [{"locator": {"page": 1, "private": "secret-path"}, "text": text},
                 {"locator": {"page": 2}, "text": "Again (805)555-0100."}]
        before = deepcopy(units)
        redacted, counts = redact_units(units)
        self.assertEqual(redacted[0]["text"], "Call [PHONE-1] or [PHONE-2].\nData retained for 30 days.")
        self.assertEqual(redacted[1]["text"], "Again [PHONE-2].")
        self.assertEqual(counts, {"PHONE": 3})
        self.assertEqual(redacted[0]["text"].splitlines()[1], "Data retained for 30 days.")
        sources = ModelSources(units)
        source = sources.source_payload()
        self.assertEqual(source["record_text"][0],
                         '{"unit": 0}\tCall [PHONE-1] or [PHONE-2].\nData retained for 30 days.')
        self.assertEqual(source["record_text"][1], '{"unit": 1}\tAgain [PHONE-2].')
        challenge = sources.digest_payload({
            "scope": "Call (805)555-0100", "actors": ["+1(805)555-0100"],
            "dates": [{"date": "x", "note": "(805)555 0100"}], "statements": [{"text": "1(805)555-0100"}],
            "omissions": [{"text": "(805)555.0100"}], "counterevidence": [{"text": "+1 (805)555-0100"}],
            "conclusions": [{"text": "(805)555-0100", "confidence": "likely", "sources": []}]})
        self.assertNotIn("805", json.dumps(challenge))
        captured = []

        def opener(request, **kwargs):
            captured.append(request.data.decode())
            body = {"scope": "Synthetic source", "actors": [], "dates": [], "omissions": [],
                    "counterevidence": [], "statements": [], "conclusions": []}
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(body)}}]}).encode())

        digest = build_digest("a" * 64, units, law.load_package("us-ca"), "us-ca", "draft", "redacted_cloud",
                              model_config=ModelConfig("https://model.example.invalid/v1"), opener=opener)
        self.assertEqual(len(captured), 1)
        self.assertNotIn("805", captured[0])
        self.assertNotIn("secret-path", captured[0])
        payload = json.loads(json.loads(captured[0])["messages"][1]["content"])
        self.assertEqual(payload["record_text"][0], source["record_text"][0])
        self.assertEqual(digest["redaction_counts"], {"PHONE": 3})
        self.assertEqual(units, before)

    def test_finding_update_note_inherits_phone_refusal_without_echo(self):
        for note in ("reached the office at (805)555-0100 afterwards", "reached +1(805)555-0100 afterwards"):
            with self.subTest(note=note):
                with self.assertRaises(UpdateError) as caught:
                    validate_update_note(note, "update")
                self.assertEqual(str(caught.exception), "update: update note contains private information")
                self.assertNotIn("555", str(caught.exception))
        ordinary = "the agency replied that records are retained for 30 days"
        self.assertEqual(validate_update_note(ordinary, "update"), ordinary)


if __name__ == "__main__":
    unittest.main()
