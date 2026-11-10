"""RF0925-3: a Unicode case-equivalent duration unit must not crash the digest.

U+017F LATIN SMALL LETTER LONG S matches the plural ``s`` under the case-insensitive
``DURATION`` regex. The matched unit must convert like its ASCII spelling instead of
raising ``KeyError`` and aborting the aggregate detectors and ``build_digest``.
Synthetic text only; the model transport is an in-memory opener.
"""
from copy import deepcopy
from fractions import Fraction
import hashlib
import io
import json
import unittest

from campaign_tool import law
from campaign_tool.digest.build import build_digest
from campaign_tool.digest.detectors import DURATION, detect_retention, run_detectors
from campaign_tool.digest.model import ModelConfig, ModelError

LONG_S = "\u017f"
KELVIN = "\u212a"
NOTE = "Veh. Code 2413 binds CHP; for other agencies this is a reference point, not the governing period."
# Independent expectations: (unit stem, days per unit as an exact fraction, numeric type).
FACTORS = (("day", Fraction(1), int), ("week", Fraction(7), int), ("month", Fraction(30), int),
           ("year", Fraction(365), int), ("hour", Fraction(1, 24), float))
SPELLINGS = (lambda s: s, str.upper, str.title, lambda s: s[:-1] + s[-1].upper() if s else s)
PAGE = {"page": 7}
LOCATOR = {"page": 7, "member": "synthetic.pdf", "nested": {"members": ["a"], "deeper": {"path": "x"}}}


def unit(text, locator=None):
    return {"locator": deepcopy(PAGE if locator is None else locator), "text": text}


def digest_json(value):
    return json.dumps(value, sort_keys=True)


def same_days(actual, expected):
    """Exact days; hours are a correctly rounded float quotient, everything else an int."""
    return actual == (float(expected) if isinstance(actual, float) else expected)


def comparison(days):
    return "exceeds_chp_60_days" if days > 60 else "within_chp_60_days"


class DurationUnicodeTests(unittest.TestCase):
    def setUp(self):
        self.package = law.load_package("us-ca")
        self.rule = next(r for r in self.package["rules"] if "2413" in r["citation"])

    def retention(self, text, units=None):
        return detect_retention(units if units is not None else [unit(text)], self.package)

    def expected_hit(self, locator, line, stated, days):
        return {"detector": "retention_period", "detector_version": "digest-detectors-1",
                "rule_id": self.rule["rule_id"], "citation": self.rule["citation"],
                "locator": locator, "excerpt": line.strip()[:240], "kind": "comparison",
                "detail": {"stated": stated, "stated_days": days, "reference_days": 60,
                           "comparison": comparison(days), "note": NOTE}}

    def test_all_five_long_s_plural_units_convert_without_aborting(self):
        for stem, per_unit, kind in FACTORS:
            for spell in SPELLINGS:
                for number in (0, 1, 90):
                    unit_text = spell(stem) + LONG_S
                    stated = f"{number} {unit_text}"
                    line = f"Data is retained for {stated}."
                    with self.subTest(unit=unit_text, n=number):
                        self.assertIsNotNone(DURATION.search(line))  # the matcher admits it
                        days = Fraction(number) * per_unit
                        expected = [self.expected_hit({"page": 7}, line, stated, kind(days))]
                        hits = self.retention(line)
                        self.assertEqual(hits, expected)
                        self.assertIs(type(hits[0]["detail"]["stated_days"]), kind)
                        self.assertTrue(same_days(hits[0]["detail"]["stated_days"], days))
                        self.assertEqual([h for h in run_detectors([unit(line)], self.package)
                                          if h["detector"] == "retention_period"], expected)

    def test_matcher_admitted_case_equivalents_share_conversion(self):
        fixtures = (("1 day", 1), ("2 Days", 2), ("3 WEEK", 21), ("4 wee" + KELVIN + "s", 28),
                    ("5 wee" + KELVIN, 35), ("6 wee" + KELVIN + LONG_S, 42), ("7-month", 210),
                    ("8 - year", 2920), ("9 YEAR" + LONG_S, 3285), ("10-HOUR" + LONG_S, Fraction(10, 24)),
                    ("11\u00a0day" + LONG_S, 11))
        for stated, days in fixtures:
            line = f"Records are kept for {stated} only."
            with self.subTest(stated=stated):
                match = DURATION.search(line)
                self.assertIsNotNone(match)
                self.assertEqual(match.group(0), stated)
                hits = self.retention(line)
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]["detail"]["stated"], stated)  # original spelling retained
                self.assertTrue(same_days(hits[0]["detail"]["stated_days"], Fraction(days)))
        for text in ("kept 3 dayz", "kept 3 d\u0430ys", "kept 3 day\u0455", "kept 3 \uff44ays",
                     "kept 3 fortnight" + LONG_S, "kept 3 day" + LONG_S + LONG_S, "kept 3 dayss"):
            with self.subTest(unrecognized=text):
                self.assertIsNone(DURATION.search(text))
                self.assertEqual(self.retention(text), [])

    def test_numeric_refusals_remain_complete_with_unicode_units(self):
        refused = ("+30 day" + LONG_S, "-30 day" + LONG_S, "\u221230 day" + LONG_S, "\u00b130 day" + LONG_S,
                   "1.5 day" + LONG_S, "1,000 day" + LONG_S, "1/2 day" + LONG_S, "1:30 hour" + LONG_S,
                   "12345 day" + LONG_S, "30-60 day" + LONG_S, "30 - 60 day" + LONG_S,
                   "30\t\u2013 60 day" + LONG_S, "1 000 day" + LONG_S)
        for text in refused:
            with self.subTest(text=text):
                self.assertEqual(self.retention(f"Data is retained for {text}."), [])
        hits = self.retention("Retained for 1.5 day" + LONG_S + ", 30 - 60 day" + LONG_S
                              + ", then 2 week" + LONG_S + " and 3 month" + LONG_S + ".")
        self.assertEqual([(h["detail"]["stated"], h["detail"]["stated_days"]) for h in hits],
                         [("2 week" + LONG_S, 14), ("3 month" + LONG_S, 90)])
        # Unicode decimal digits already admitted on main stay admitted, with either unit spelling.
        for unit_text in ("days", "day" + LONG_S):
            with self.subTest(digits=unit_text):
                hits = self.retention("Data is retained for \u0663\u0660 " + unit_text + ".")
                self.assertEqual([h["detail"]["stated_days"] for h in hits], [30])

    def test_threshold_factors_and_zero_preserve_reference_contract(self):
        cases = ((60, "day"), (61, "day"), (8, "week"), (9, "week"), (2, "month"), (3, "month"),
                 (1, "year"), (1440, "hour"), (1441, "hour"), (0, "day"), (0, "year"))
        per_unit = {stem: factor for stem, factor, _ in FACTORS}
        for number, stem in cases:
            with self.subTest(n=number, unit=stem):
                days = Fraction(number) * per_unit[stem]
                (hit,) = self.retention(f"Data kept {number} {stem}{LONG_S}.")
                self.assertEqual(hit["detail"]["comparison"], "exceeds_chp_60_days" if days > 60 else "within_chp_60_days")
                self.assertTrue(same_days(hit["detail"]["stated_days"], days))
                self.assertEqual(hit["detail"]["reference_days"], 60)
                self.assertEqual(hit["detail"]["note"], NOTE)
                self.assertEqual(hit["rule_id"], self.rule["rule_id"])
                ascii_hit = self.retention(f"Data kept {number} {stem}s.")[0]
                for key in ("rule_id", "citation", "kind", "detector", "detector_version"):
                    self.assertEqual(hit[key], ascii_hit[key])
                self.assertEqual({k: v for k, v in hit["detail"].items() if k != "stated"},
                                 {k: v for k, v in ascii_hit["detail"].items() if k != "stated"})
                self.assertIs(type(hit["detail"]["stated_days"]), type(ascii_hit["detail"]["stated_days"]))

    def test_original_spelling_and_source_provenance_never_change(self):
        line = "Data is retained for 90 day" + LONG_S + "."
        units = [unit("Intro\r" + line + "\u2028Audit performed.", LOCATOR),
                 {"locator": {"line": 17, "page": 2, "nested": {"k": [1, {"v": 2}]}}, "text": line},
                 {"locator": {"sheet": "S", "row": 4}, "text": "Retention: " + "x" * 300 + " 2 year" + LONG_S}]
        before = deepcopy(units)
        hits = [h for h in run_detectors(units, self.package) if h["detector"] == "retention_period"]
        self.assertEqual(units, before)
        self.assertEqual([h["locator"] for h in hits],
                         [{**LOCATOR, "line": 2}, {"line": 17, "page": 2, "nested": {"k": [1, {"v": 2}]}},
                          {"sheet": "S", "row": 4}])
        self.assertEqual([h["excerpt"] for h in hits], [line, line, units[2]["text"][:240]])
        self.assertEqual(len(hits[2]["excerpt"]), 240)
        self.assertEqual([h["detail"]["stated"] for h in hits[:2]], ["90 day" + LONG_S] * 2)
        self.assertEqual([h["detail"]["stated_days"] for h in hits[:2]], [90, 90])
        run_detectors(units, self.package)  # repeated calls leave the input untouched
        self.assertEqual(units, before)

    def test_aggregate_and_real_no_model_digest_finish_other_families(self):
        text = ("Intro\nData is retained for 90 day" + LONG_S + ".\nRecords shared with the FBI.\n"
                "An audit log is reviewed.\nPublic hearing on this agenda item.")
        hits = run_detectors([unit(text)], self.package)
        detectors = [h["detector"] for h in hits]
        for name in ("retention_period", "out_of_state_or_federal_sharing", "audit_log_mention",
                     "public_hearing_mention"):
            self.assertIn(name, detectors)
        units = [unit(text, LOCATOR)]
        before = deepcopy(units)
        digest = build_digest("a" * 64, units, self.package, "us-ca", "draft", "redacted_cloud")
        self.assertEqual(units, before)
        retention = [h for h in digest["hits"] if h["detector"] == "retention_period"]
        self.assertEqual([(h["locator"], h["excerpt"], h["detail"]["stated"], h["detail"]["stated_days"])
                          for h in retention],
                         [({**LOCATOR, "line": 2}, "Data is retained for 90 day" + LONG_S + ".", "90 day" + LONG_S, 90)])
        self.assertIn("Data is retained for 90 day" + LONG_S + ".", [s["text"] for s in digest["statements"]])
        self.assertTrue(digest["conclusions"])
        self.assertEqual({c["confidence"] for c in digest["conclusions"]}, {"needs_attorney_review"})
        self.assertIn("No model configured: narrative fields are detector output only.", digest["limitations"])
        # Controls: no duration, empty units and a package without the retention rule.
        plain = build_digest("a" * 64, [unit("Audit performed.")], self.package, "us-ca", "draft", "redacted_cloud")
        self.assertEqual([h for h in plain["hits"] if h["detector"] == "retention_period"], [])
        empty = build_digest("a" * 64, [], self.package, "us-ca", "draft", "redacted_cloud")
        self.assertEqual(empty["conclusions"][0]["confidence"], "needs_attorney_review")
        bare = dict(self.package, rules=[r for r in self.package["rules"] if "2413" not in r["citation"]])
        (hit,) = detect_retention([unit("Data retained for 90 day" + LONG_S)], bare)
        self.assertIsNone(hit["rule_id"])
        self.assertIsNone(hit["citation"])
        self.assertEqual(hit["detail"]["stated_days"], 90)


def model_output(locator):
    return {"scope": "Synthetic source", "actors": [], "dates": [],
            "statements": [{"text": "Data retained for 90 day" + LONG_S + ".", "locator": deepcopy(locator)}],
            "omissions": [], "counterevidence": [],
            "conclusions": [{"text": "Retention is described.", "confidence": "needs_attorney_review",
                             "sources": [{"locator": deepcopy(locator), "rule_id": None}]}]}


class ModelCompositionTests(unittest.TestCase):
    def setUp(self):
        self.package = law.load_package("us-ca")
        self.line = "Data retained for 90 day" + LONG_S + "."

    def run_build(self, locator):
        captured = []

        def opener(request, **kwargs):
            captured.append(json.loads(request.data))
            content = json.dumps(model_output(locator))
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

        units = [{"locator": deepcopy(LOCATOR), "text": f"Intro\r{self.line}\rAudit performed."}]
        before = deepcopy(units)
        digest = build_digest("a" * 64, units, self.package, "us-ca", "draft", "redacted_cloud",
                              model_config=ModelConfig("https://model.example.invalid/v1"), opener=opener)
        self.assertEqual(units, before)
        return digest, captured

    def test_model_source_composition_keeps_unicode_evidence_and_restores_locator(self):
        digest, captured = self.run_build({"unit": 0, "line": 2})
        payload = json.loads(captured[0]["messages"][1]["content"])
        retention = [h for h in payload["detector_hits"] if h["detector"] == "retention_period"]
        self.assertEqual([(h["locator"], h["excerpt"], h["detail"]["stated_days"], h["detail"]["comparison"])
                          for h in retention],
                         [({"unit": 0, "line": 2}, self.line, 90, "exceeds_chp_60_days")])
        self.assertNotIn("synthetic.pdf", json.dumps(captured))  # only opaque references are issued
        self.assertEqual(digest["statements"][0]["locator"], {**LOCATOR, "line": 2})
        self.assertEqual(digest["conclusions"][0]["sources"][0]["locator"], {**LOCATOR, "line": 2})
        self.assertEqual(digest["conclusions"][0]["confidence"], "needs_attorney_review")
        for refused in ({"unit": 0, "line": 0}, {"unit": 0, "line": 4}, {"unit": 1, "line": 2},
                        {"unit": 0, "line": True}):
            with self.subTest(ref=refused), self.assertRaises(ModelError):
                self.run_build(refused)


# sha256 of json.dumps(run_detectors(units, us-ca), sort_keys=True), captured from main 929e84a5
# before the repair; these inputs already succeeded there.
LEGACY_UNITS = {
    "ascii": [unit("Intro\nData is retained for 90 days.\nFBI sharing; audit log kept 6 months.\nPublic hearing held.")],
    "kelvin": [unit("Intro\nData is retained for 90 wee" + KELVIN + "s.\nAudit performed.")],
}
LEGACY_SHA256 = {
    "ascii": "3de457d16eac24a75e85491c34ce3f90476e18f6c81e8b9d5a0d3370d6335585",  # pragma: allowlist secret
    "kelvin": "d3a71c8037fabb205f36a7ee2f24fef486496e3125e0e256eb43ce8b91f6430a",  # pragma: allowlist secret
}


class LegacyCompatibilityTests(unittest.TestCase):
    def test_previously_successful_ascii_and_unicode_outputs_are_byte_identical(self):
        package = law.load_package("us-ca")
        for label, units in LEGACY_UNITS.items():
            with self.subTest(case=label):
                digest = hashlib.sha256(digest_json(run_detectors(units, package)).encode()).hexdigest()
                self.assertEqual(digest, LEGACY_SHA256[label])


if __name__ == "__main__":
    unittest.main()
