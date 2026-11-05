"""SLO pilot policy rows (REC-DATA-2): the shipped register loads clean and holds the named sections."""
import unittest
from pathlib import Path

from campaign_tool.records import agency_policies as ap

REG = Path(__file__).resolve().parents[2] / "data" / "slo-pilot" / "agency-policies.csv"


class PilotRegisterTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.problems = ap.load(REG)

    def test_register_is_valid(self):
        self.assertEqual(self.problems, [])
        self.assertGreaterEqual(len(self.rows), 15)

    def test_named_sections_present_with_hash_and_page(self):
        for agency, pid, sec in [("slo-county-sheriff", "Policy 466", "466.5(e)"),
                                 ("grover-beach-pd", "Policy 428", "428.5(c)"),
                                 ("arroyo-grande-pd", "Policy 461", "461.5(c)"),
                                 ("morro-bay-pd", "Policy 435", "435.6(c)"),
                                 ("paso-robles-pd", "Policy 435", "435.6(c)")]:
            r = ap.find(self.rows, agency, pid, sec)
            self.assertIsNotNone(r, (agency, sec))
            self.assertRegex(r["doc_sha256"], r"^[0-9a-f]{64}$")
            self.assertGreaterEqual(int(r["page"]), 1)

    def test_sheriff_audit_quote(self):
        r = ap.find(self.rows, "slo-county-sheriff", "Policy 466", "466.5(e)")
        self.assertIn("first of each calendar year", r["quote"])
        self.assertEqual(ap.cite(r), "Policy 466 466.5(e), p. 2")


if __name__ == "__main__":
    unittest.main()
