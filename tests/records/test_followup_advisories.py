"""Advice-only wording notes for follow-up drafts. Inline synthetic strings only."""
import unittest
from unittest import mock

from campaign_tool import legal_claims
from campaign_tool.records import denial_reply_check, followup_advisories as fa

MIXED = "The agency secretly shared data. Please respond under §6253."
MARKER = "PRIVATE-MARKER-XYZ"


class Notes(unittest.TestCase):
    def test_mixed_letter_has_intent_then_former_cpra(self):
        found = fa.notes(MIXED)
        self.assertEqual(len(found), 2)
        self.assertIn("(intent)", found[0])
        self.assertTrue(found[1].startswith("warning: follow-up draft: former_cpra_citation. Former CPRA section"))

    def test_not_law_tier_runs(self):
        found = fa.notes("SB 274 requires a retention limit.")
        self.assertTrue(any("(not-law)" in n for n in found), found)

    def test_negative_controls(self):
        for text in ("Please respond under Gov. Code § 7922.535.", "Please see former §6253 as then numbered.",
                     "Please send the records by the due date.", "See bare 6253 only.",
                     "The agency stated these reasons, as written:\n> cites §6253\n\nThank you."):
            self.assertFalse([n for n in fa.notes(text) if "former_cpra_citation" in n], text)
        self.assertEqual(fa.notes("Please send the records."), [])

    def test_scanner_faults_are_fixed_advice_and_independent(self):
        with mock.patch.object(legal_claims, "legal_notes", side_effect=RuntimeError(MARKER)):
            found = fa.notes(MIXED)
        self.assertEqual(found[0], legal_claims.ADVISORY_GUARD_ERROR)
        self.assertIn("former_cpra_citation. Former CPRA", found[1])
        with mock.patch.object(denial_reply_check, "check", side_effect=RuntimeError(MARKER)):
            found = fa.notes(MIXED)
        self.assertIn("(intent)", found[0])
        self.assertEqual(found[-1], fa.FORMER_GUARD_ERROR)
        self.assertNotIn(MARKER, "".join(found))


if __name__ == "__main__":
    unittest.main()
