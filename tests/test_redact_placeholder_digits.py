"""RF0725-6: unbounded decimal placeholders, using synthetic evidence only."""
from copy import deepcopy
import io
import json
import sys
import unittest

from campaign_tool import law
from campaign_tool.digest.build import build_digest, redact_units
from campaign_tool.digest.model import ModelConfig
from campaign_tool.digest.model_input import ModelSources
from campaign_tool.digest.redact import CATEGORIES, PLACEHOLDER, Redactor, redact
from campaign_tool.digest.schema import validate_digest


EMAIL = "synthetic@example.invalid"
OTHER = "another@example.invalid"
LONG_CASES = (("0" * 5000 + "7", "8"),
              ("9" * 5000, "1" + "0" * 5000),
              ("9" * 4300, "1" + "0" * 4300))


def literal(digits, category="EMAIL"):
    return f"[{category}-{digits}]"


def narrative(locator):
    return {"scope": "Synthetic evidence", "actors": [], "dates": [],
            "statements": [{"text": "Data retained for 90 days.", "locator": locator}],
            "omissions": [], "counterevidence": [],
            "conclusions": [{"text": "Retention needs review.",
                             "confidence": "needs_attorney_review",
                             "sources": [{"locator": locator, "rule_id": None}]}]}


class PlaceholderDigitTests(unittest.TestCase):
    def setUp(self):
        self.digit_limit = sys.get_int_max_str_digits()

    def tearDown(self):
        self.assertEqual(sys.get_int_max_str_digits(), self.digit_limit)

    def test_long_reservation_and_successor_width(self):
        for digits, successor in LONG_CASES + (("0" * 5000, "1"),):
            with self.subTest(width=len(digits), prefix=digits[:1]):
                token = literal(digits)
                redactor = Redactor()
                redactor.reserve(token)
                control = redactor.redact(token)
                self.assertEqual((control.text, control.counts), (token, {}))
                text = token + " contact " + EMAIL + " " + EMAIL
                result = redactor.redact(text)
                self.assertEqual(result.text, token + " contact " + literal(successor) + " " + literal(successor))
                self.assertEqual(result.counts, {"EMAIL": 2})
                self.assertEqual(vars(result).keys(), {"text", "counts"})
                self.assertNotIn(EMAIL, repr(result))
                self.assertEqual(redact(text), result)
                self.assertEqual(redact(result.text).text, result.text)
                self.assertEqual(redact(result.text).counts, {})

    def test_numeric_maximum_carry_and_category_isolation(self):
        cases = ((["0"], "1"), (["7", "0007", "6"], "8"),
                 (["9", "8"], "10"), (["99", "9"], "100"),
                 (["109", "99"], "110"), (["999"], "1000"),
                 (["1999", "1989", "999"], "2000"),
                 (["0" * 5000 + "7", "10", "09"], "11"),
                 (["8" + "0" * 4999, "7" + "9" * 4999], "8" + "0" * 4998 + "1"))
        for values, successor in cases:
            with self.subTest(successor_width=len(successor)):
                redactor = Redactor()
                for value in values:
                    redactor.reserve(literal(value))
                result = redactor.redact(EMAIL + " (805)555-0100")
                self.assertEqual(result.text, literal(successor) + " [PHONE-1]")
                self.assertEqual(result.counts, {"EMAIL": 1, "PHONE": 1})

    def test_unicode_and_mixed_decimal_spelling_is_preserved(self):
        for digits, successor in (("٠٠٧", "8"), ("９９", "100"),
                                  ("٠" * 5000 + "７", "8"), ("١９٩", "200")):
            with self.subTest(width=len(digits)):
                token = literal(digits)
                match = PLACEHOLDER.fullmatch(token)
                assert match is not None
                self.assertEqual(match.groups(), ("EMAIL", digits))
                result = redact(token + " " + EMAIL)
                self.assertEqual(result.text, token + " " + literal(successor))
                self.assertEqual(result.counts, {"EMAIL": 1})
        for invalid in ("[UNKNOWN-7]", "[EMAIL-+7]", "[EMAIL--7]", "[EMAIL-]", "[EMAIL-²]"):
            self.assertIsNone(PLACEHOLDER.fullmatch(invalid))
            self.assertEqual(redact(invalid + " " + EMAIL).text, invalid + " [EMAIL-1]")

    def test_shared_identity_survives_later_higher_reservation(self):
        redactor = Redactor()
        redactor.reserve(literal("0" * 5000 + "7"))
        self.assertEqual(redactor.redact(EMAIL).text, "[EMAIL-8]")
        redactor.reserve(literal("9" * 5000))
        self.assertEqual(redactor.redact(EMAIL.upper()).text, "[EMAIL-8]")
        self.assertEqual(redactor.redact(OTHER).text, literal("1" + "0" * 5000))
        redactor.reserve("[EMAIL-1]")
        self.assertEqual(redactor.redact(OTHER).text, literal("1" + "0" * 5000))

    def test_ordinary_all_category_outputs_and_policy_are_unchanged(self):
        examples = {"EMAIL": EMAIL, "SSN": "123-45-6789", "DOB": "DOB: 2000/01/02",
                    "PHONE": "(805)555-0100", "ADDRESS": "123 Main Street",
                    "NAME": "Deputy Alice Smith", "PLATE": "7ABC123", "DENYLIST": "quiet phrase"}
        self.assertEqual(set(examples), set(CATEGORIES))
        for category, text in examples.items():
            with self.subTest(category=category):
                prefix = {"DOB": "DOB: ", "NAME": "Deputy "}.get(category, "")
                token = literal("7", category)
                result = redact(token + " " + text + "; " + text, denylist=("quiet phrase",))
                self.assertEqual(result.text, token + " " + prefix + literal("8", category)
                                 + "; " + prefix + literal("8", category))
                self.assertEqual(result.counts, {category: 2})
        self.assertEqual(vars(redact("")), {"text": "", "counts": {}})
        token = literal("0" * 5000 + "7")
        text = token + " " + EMAIL
        self.assertEqual(redact(text, allowlist=(EMAIL,)).text, text)
        result = redact(text, allowlist=(EMAIL,), denylist=(EMAIL,))
        self.assertEqual((result.text, result.counts), (token + " [DENYLIST-1]", {"DENYLIST": 1}))
        # Placeholder-shaped regions remain subject to ordinary denylist policy.
        result = redact(token + " " + EMAIL, denylist=(token,))
        self.assertEqual((result.text, result.counts), ("[DENYLIST-1] [EMAIL-8]", {"EMAIL": 1, "DENYLIST": 1}))
        result = redact(token + " " + EMAIL + " tail", denylist=("invalid tail",))
        self.assertEqual((result.text, result.counts), (token + " [EMAIL-8]", {"EMAIL": 1}))

    def test_multiline_sensitive_spans_preserve_all_separators(self):
        token = literal("9" * 5000)
        for separator in ("\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                text = token + "\nDeputy Alice" + separator + "Smith arrived.\nEvidence follows."
                result = redact(text)
                self.assertEqual(result.text, token + "\nDeputy [NAME-1]" + separator + "  arrived.\nEvidence follows.")
                self.assertEqual(result.counts, {"NAME": 1})
                self.assertEqual(result.text.splitlines()[3], "Evidence follows.")

    def test_real_units_sources_and_detector_digest(self):
        package = law.load_package("us-ca")
        for digits, successor in LONG_CASES:
            with self.subTest(width=len(digits), prefix=digits[:1]):
                token = literal(digits)
                text = token + " contact " + EMAIL + "\nData retained for 90 days."
                original = [{"locator": {"page": 1, "member": "private-synthetic-locator", "nested": [1]}, "text": text},
                            {"locator": {"page": 2}, "text": "meet at 123"},
                            {"locator": {"page": 3}, "text": "Main Street tomorrow"}]
                before = deepcopy(original)
                units, counts = redact_units(original)
                expected = token + " contact " + literal(successor) + "\nData retained for 90 days."
                self.assertEqual(units[0]["text"], expected)
                self.assertEqual([u["text"] for u in units[1:]], ["meet at 123", "Main Street tomorrow"])
                self.assertEqual(counts, {"EMAIL": 1})
                units[0]["locator"]["nested"].append(2)
                self.assertEqual(original, before)
                sources = ModelSources(original)
                payload = sources.source_payload()
                self.assertFalse(payload["truncated"])
                self.assertEqual(payload["record_text"][0], '{"unit": 0}\t' + expected)
                self.assertNotIn("private-synthetic-locator", json.dumps(payload))
                self.assertNotIn(EMAIL, json.dumps(payload))
                original[0]["text"] = "changed after snapshot"
                self.assertEqual(sources.source_payload(), payload)
                digest = build_digest("a" * 64, before, package, "us-ca", "draft", "redacted_cloud")
                self.assertEqual(validate_digest(digest), digest)
                self.assertEqual(digest["redaction_counts"], {"EMAIL": 1})
                retention = next(hit for hit in digest["hits"] if hit["detector"] == "retention_period")
                self.assertEqual(retention["locator"]["line"], 2)
                self.assertTrue(all(c["confidence"] == "needs_attorney_review" for c in digest["conclusions"]))
                self.assertNotIn(EMAIL, json.dumps(digest))

    def test_challenge_reservation_and_primary_serialization(self):
        token = literal("9" * 5000)
        successor = literal("1" + "0" * 5000)
        original = [{"locator": {"page": 1, "member": "private-synthetic-locator"},
                     "text": token + " " + EMAIL + "\nData retained for 90 days."}]
        before = deepcopy(original)
        sources = ModelSources(original)
        sources.source_payload()
        document = narrative(original[0]["locator"])
        document["scope"] = literal("2" + "0" * 5000)
        document["actors"] = [OTHER]
        challenge = sources.digest_payload(document)
        self.assertEqual(challenge["scope"], document["scope"])
        self.assertEqual(challenge["actors"], [literal("2" + "0" * 4999 + "1")])
        captured = []

        def opener(request, **kwargs):
            captured.append(json.loads(request.data))
            content = json.dumps(narrative({"unit": 0, "line": 2}))
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

        digest = build_digest("a" * 64, original, law.load_package("us-ca"), "us-ca", "draft", "redacted_cloud",
                              model_config=ModelConfig("https://model.example.invalid/v1"), opener=opener)
        self.assertEqual(len(captured), 1)
        payload = json.loads(captured[0]["messages"][1]["content"])
        self.assertEqual(payload["record_text"][0], '{"unit": 0}\t' + token + " " + successor + "\nData retained for 90 days.")
        for private in (EMAIL, OTHER, "private-synthetic-locator"):
            self.assertNotIn(private, json.dumps([captured, challenge]))
        self.assertEqual(digest["statements"][0]["locator"], {**original[0]["locator"], "line": 2})
        self.assertEqual(validate_digest(digest), digest)
        self.assertEqual(digest["conclusions"][0]["confidence"], "needs_attorney_review")
        self.assertTrue(any("verify every locator" in text for text in digest["limitations"]))
        self.assertEqual(original, before)

    def test_non_string_failure_remains_private_and_keeps_limit(self):
        for value in (None, 7, {"private": EMAIL}, [EMAIL]):
            with self.subTest(kind=type(value).__name__), self.assertRaises(TypeError) as caught:
                redact(value)
            self.assertEqual(str(caught.exception), "redact expects str")
            self.assertNotIn(EMAIL, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
