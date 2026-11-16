"""Synthetic former-number grammar, original offsets and exact shared copy."""
import unittest

from campaign_tool.cpra_cites import CURRENT_CPRA_ASK, FORMER_CPRA_NOTE, find_former_cpra_cites


class FormerCpraCitesTests(unittest.TestCase):
    def test_explicit_former_cites_and_range_edges(self):
        for cite in ("§6253(c)", "Government Code 6254", "Gov. Code section 6255",
                     "gov code §§ 6250", "GOV. CODE SECTIONS 6276.48(a)(1)",
                     "section 6255", "sections 6250.00", "§ 6276.4800", "§6253.1",
                     "Government\tCode\n§ 6253", "§§6253"):
            with self.subTest(cite=cite):
                text = "é agency: " + cite + "; end"
                start = text.index(cite)
                self.assertEqual(find_former_cpra_cites(text), [(start, start + len(cite))])
        text = "§§6253 and 6254; Gov. Code 6255; §6250"
        spans = find_former_cpra_cites(text)
        self.assertEqual([text[a:b] for a, b in spans], ["§§6253", "Gov. Code 6255", "§6250"])
        self.assertTrue(all(left[1] <= right[0] for left, right in zip(spans, spans[1:])))

    def test_numbers_and_other_code_are_not_cpra(self):
        for text in (None, {}, 6254, "", "$6,254.00", "6250 Main St", "2026", "6254", "6253.1.2",
                     "§6249", "§6276.49", "§6277", "§16253", "§6253.1.2", "§6253,100",
                     "Gov. Code 6276.480001", "Gov. Code § 7922.535", "Gov. Code 6,254.00",
                     "xsection 6254", "x§6254", "§6254identifier", "Penal Code § 6254",
                     "Civil Code section 6255", "Civ. Code §§6253", "Penal Code\n§6254"):
            with self.subTest(text=text):
                self.assertEqual(find_former_cpra_cites(text), [])
        text = "Penal Code §6254; current assertion §6255"
        self.assertEqual([text[a:b] for a, b in find_former_cpra_cites(text)], ["§6255"])

    def test_shared_copy(self):
        self.assertEqual(FORMER_CPRA_NOTE, "The response cites a section number from before the Public Records Act "
                         "was renumbered on 2023-01-01; this reply asks the agency to name the current section.")
        self.assertEqual(CURRENT_CPRA_ASK, "Please identify the current section of the Public Records Act on which "
                         "the agency relies.")
