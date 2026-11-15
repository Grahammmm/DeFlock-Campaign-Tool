"""Synthetic acceptance for advisory refusal-reply warnings."""

import unittest

from campaign_tool.records.denial_reply_check import check


REDACTION = ("Plate numbers and personal identifiers of members of the public "
             "may be redacted; please produce every other recorded field.")
BASIS = "Please provide a redaction log."
LETTER = REDACTION + "\n" + BASIS


class DenialReplyCheckTests(unittest.TestCase):
    def check(self, body, **kwargs):
        return check(body, subject_agency="Alpha PD", **kwargs)

    def assert_warning(self, body, code, line=3, **kwargs):
        warnings = self.check(body, **kwargs)
        self.assertEqual([(w["code"], w["line"]) for w in warnings], [(code, line)])
        self.assertEqual(set(warnings[0]), {"code", "message", "line"})
        self.assertTrue(warnings[0]["message"])
        return warnings[0]

    def test_checker_own_assertion_and_attribution(self):
        message = ("Former CPRA section cited as current law; ask the agency for the current section "
                   "(renumbered 2023-01-01).")
        body = LETTER + "\nCurrent law Government Code 6254 authorizes this denial.\n§6255 also."
        warnings = self.check(body)
        self.assertEqual(warnings, [
            {"code": "former_cpra_citation", "message": message, "line": 3},
            {"code": "statute_citation", "message": "needs counsel clearance", "line": 3}])
        for quote in ("> §6254 CANARY", "  > Government Code 6254 CANARY",
                      "former §6254 CANARY", "HISTORICAL Government Code 6254 CANARY",
                      "pre-2023 Gov. Code section 6255 CANARY",
                      'The agency stated these reasons, as written:\n- "§6254 CANARY" (denied)\n'):
            with self.subTest(quote=quote):
                quoted = LETTER + "\n" + quote
                old = self.check(quoted)
                self.assertEqual([w["code"] for w in old], ["statute_citation"])
                own = quoted + "\n\nOur authority is §6253(c)."
                warnings = self.check(own)
                former = [w for w in warnings if w["code"] == "former_cpra_citation"]
                self.assertEqual(former, [{"code": "former_cpra_citation", "message": message,
                                           "line": own.count("\n") + 1}])
                self.assertNotIn("CANARY", str(warnings))
        for prefix in ("notformer ", "historically ", "former wording; current law ", "former §6253; now "):
            warnings = self.check(LETTER + "\n" + prefix + "§6254")
            self.assertIn("former_cpra_citation", [w["code"] for w in warnings])
        warnings = self.check(LETTER + "\n> Preserve D1 under §6254")
        self.assertEqual([w["code"] for w in warnings],
                         ["internal_id", "preservation_demand", "statute_citation"])

    def test_compliant(self):
        self.assertEqual(self.check(LETTER), [])
        for basis in ("field-by-field", "field-specific", "REDACTION LOG"):
            with self.subTest(basis=basis):
                self.assertEqual(self.check(REDACTION + "\n" + basis), [])
        self.assertEqual(self.check(LETTER.upper().replace("MAY BE", "MAY\nBE")), [])

    def test_missing_content(self):
        for core in ("may be redacted", "produce every other"):
            with self.subTest(core=core):
                self.assert_warning(LETTER.replace(core, "omitted"),
                                    "redaction_paragraph_missing", 1)
        self.assert_warning(REDACTION, "field_basis_missing", 1)
        self.assertEqual([w["code"] for w in self.check("")],
                         ["field_basis_missing", "redaction_paragraph_missing"])

    def test_forbidden_phrases_and_lines(self):
        cases = {
            "purpose_paragraph": ("our objective", "we seek to determine",
                                  "not alleging misconduct", "the purpose of this request"),
            "preservation_demand": ("preserve", "preservation", "litigation hold"),
            "statute_citation": ("§", "Gov. Code", "Government Code", "Civ. Code", "Penal Code"),
            "internal_id": ("D123", "M45", "INBOX:67", "Sent:89"),
        }
        for code, phrases in cases.items():
            for phrase in phrases:
                with self.subTest(code=code, phrase=phrase):
                    warning = self.assert_warning(LETTER + "\n" + phrase.upper(), code)
                    if code == "statute_citation":
                        self.assertIn("needs counsel clearance", warning["message"])
        self.assert_warning(LETTER + "\n\nwe seek\nto determine", "purpose_paragraph", 4)

    def test_agencies(self):
        warning = self.assert_warning(LETTER + "\nBETA PD.", "precedent_agency",
                                      other_agency_names=("Beta PD",))
        self.assertIn("another agency is named; allowed only when it is the subject of the ask",
                      warning["message"])
        for names, suffix in (
            (("Alpha PD", " alpha pd "), "Alpha PD"),
            (("Beta PD",), "SuperBeta PD Beta PDish"),
            (("", " "), "Alpha PD"),
            ((), "Beta PD"),
            (("Beta (PD)",), "Beta XPD"),
        ):
            with self.subTest(names=names, suffix=suffix):
                self.assertEqual(self.check(LETTER + "\n" + suffix,
                                            other_agency_names=names), [])
        self.assert_warning(LETTER + "\nBeta (PD)", "precedent_agency",
                            other_agency_names=("Beta (PD)",))

    def test_token_boundaries(self):
        self.assertEqual(self.check(LETTER + "\nD12x XM34 INBOX:4x Sent:5x"), [])

    def test_preservation_wordings(self):
        for text in ("Please ensure all responsive records are preserved.",
                     "Records must be preserved pending review.",
                     "We ask that you continue preserving these records.",
                     "Please preserve all records.",
                     "A preservation notice applies.",
                     "Place a litigation hold."):
            with self.subTest(text=text):
                self.assert_warning(LETTER + "\n" + text, "preservation_demand")

    def test_length_boundary(self):
        for count in (1300, 1301, 1400):
            body = LETTER + " word" * (count - len(LETTER.split()))
            with self.subTest(count=count):
                if count == 1300:
                    self.assertEqual(self.check(body), [])
                else:
                    self.assert_warning(body, "too_long", 1)

    def test_order_and_duplicate_matches(self):
        body = LETTER + "\nPreserve D1 our objective\nD2 preserve"
        expected = [("internal_id", 3), ("preservation_demand", 3), ("purpose_paragraph", 3)]
        for _ in range(2):
            self.assertEqual([(w["code"], w["line"]) for w in self.check(body)], expected)
        warnings = self.check(LETTER + "\nD1\npreserve")
        self.assertEqual([(w["code"], w["line"]) for w in warnings],
                         [("internal_id", 3), ("preservation_demand", 4)])
