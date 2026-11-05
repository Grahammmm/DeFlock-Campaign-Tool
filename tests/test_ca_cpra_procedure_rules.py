"""REC-LAW-1: CPRA procedural rules in the us-ca draft package. No legal sign-off."""
import unittest

from campaign_tool import law

SECTIONS = {
    "ca-gov-7922.600-duty-to-assist": "7922.600",
    "ca-gov-7922.540-written-denial": "7922.540",
    "ca-gov-7922.525-segregability": "7922.525",
    "ca-gov-7922.570-electronic-format": "7922.570",
    "ca-gov-7922.575-electronic-copy-cost": "7922.575",
    "ca-gov-7923.600-investigatory-records": "7923.600",
}


class CpraProcedureRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = law.load_package("us-ca")
        cls.rules = {r["rule_id"]: r for r in cls.package["rules"]}

    def test_each_rule_is_attorney_review_with_an_official_source(self):
        for rule_id, section in SECTIONS.items():
            rule = self.rules[rule_id]
            self.assertEqual(rule["review"], "needs_attorney_review", rule_id)
            self.assertIn("Gov. Code § " + section, rule["citation"])
            urls = [s["url"] for s in rule["sources"]]
            self.assertTrue(all(u.startswith("https://leginfo.legislature.ca.gov/") and "sectionNum=" + section in u
                                for u in urls) and urls, rule_id)

    def test_rules_are_in_force_for_current_events_and_not_before_operative_date(self):
        now = {r["rule_id"] for r in law.rules_in_force(self.package, "2026-11-05")}
        self.assertTrue(set(SECTIONS) <= now)
        before = {r["rule_id"] for r in law.rules_in_force(self.package, "2022-12-31")}
        self.assertFalse(set(SECTIONS) & before)

    def test_rule_text_states_the_procedural_duties(self):
        self.assertIn("names and titles", self.rules["ca-gov-7922.540-written-denial"]["duty"])
        self.assertIn("reasonably segregable", self.rules["ca-gov-7922.525-segregability"]["duty"])
        self.assertIn("direct cost", self.rules["ca-gov-7922.575-electronic-copy-cost"]["duty"])
        self.assertIn("describe the information technology", self.rules["ca-gov-7922.600-duty-to-assist"]["duty"].lower())
        investigatory = self.rules["ca-gov-7923.600-investigatory-records"]
        self.assertIn("investigatory or security files", investigatory["duty"])
        self.assertIn("not decided here", investigatory["duty"])

    def test_package_stays_draft(self):
        self.assertEqual(self.package["status"], "draft")
        self.assertEqual(self.package["reviewed_by"], [])


if __name__ == "__main__":
    unittest.main()
