"""Bounded CA-01 through CA-07 draft regressions; no legal sign-off or live calls."""
import unittest
from pathlib import Path
from campaign_tool import law
from campaign_tool.records import law_package_check

ROOT = Path(__file__).resolve().parents[1]
SECTION_34090_6 = "ca-gov-34090.6-routine-recordings-destruction"
CPRA_2026 = "ca-gov-7922.535-determination-and-extension-2026"
COMMENT = "ca-civ-1798.90.55-public-comment-before-implementation"


class CaliforniaCorrectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = law.load_package("us-ca")
        cls.rules = {r["rule_id"]: r for r in cls.package["rules"]}

    def test_chp_recipient_scope_officer_qualification_and_cumulative_duties(self):
        rule = self.rules["ca-veh-2413-chp-lpr-retention-and-sharing"]
        self.assertIn("all law enforcement agencies, including CHP itself and recipient agencies", rule["actor"])
        self.assertIn("Any law enforcement agency, including CHP itself and recipient agencies", rule["duty"])
        self.assertIn("individual who is not a law enforcement officer", rule["duty"])
        self.assertIn("reasonably suspected", rule["duty"])
        exceptions = " ".join(rule["exceptions"])
        self.assertIn("cumulative, not alternatives", exceptions)
        self.assertNotIn("CHP only", exceptions)
        selected = {r["rule_id"] for r in law.rules_in_force(self.package, "2016-01-01")}
        self.assertTrue({"ca-veh-2413-chp-lpr-retention-and-sharing",
                         "ca-civ-1798.90.55-sharing-limits"} <= selected)

    def test_operator_and_end_user_policy_minimums_are_distinct(self):
        operator = self.rules["ca-civ-1798.90.51-usage-and-privacy-policy"]["duty"]
        end_user = self.rules["ca-civ-1798.90.53-end-user-usage-and-privacy-policy"]["duty"]
        self.assertIn("or collect ALPR information", operator)
        self.assertIn("ALPR-system custodian or owner responsible for implementing this section", operator)
        self.assertIn("information custodian or owner responsible for implementing this section", end_user)
        self.assertIn("periodic system-audit process", end_user)
        for text in (operator, end_user):
            self.assertIn("if and when to destroy", text)
            self.assertIn("if", text)
        self.assertNotIn("annual", end_user)

    def test_federal_definition_uses_eligibility_not_actual_receipt(self):
        rule = self.rules["ca-civ-1798.90.5-definitions"]
        exceptions = " ".join(rule["exceptions"])
        self.assertIn("other than a law enforcement agency", exceptions)
        self.assertIn("to whom information may be disclosed", exceptions)
        self.assertIn("Eligibility, not actual receipt", exceptions)
        self.assertIn("regulatory compliance oversight", exceptions)
        self.assertIn("unresolved", exceptions)
        self.assertEqual(rule["review"], "needs_attorney_review")

    def test_own_equipment_copying_conditions_are_not_blanket_permission(self):
        text = self.package["records_law"]["fee_basis"]
        for condition in ("disclosable", "without physical contact", "damage",
                          "unauthorized", "secured-network", "reasonable",
                          "orderly-function", "historic/high-value", "preservation"):
            self.assertIn(condition, text)

    def test_values_act_actor_status_activity_local_policy_and_reporting(self):
        rule = self.rules["ca-gov-7284.6-values-act-immigration-enforcement-limits"]
        self.assertIn("excluding CDCR", rule["actor"])
        self.assertIn("interpretive, not an established duty", rule["duty"])
        exceptions = " ".join(rule["exceptions"])
        for condition in ("agency-policy and local-law/policy", "activity-specific",
                          "specific-person criminal-history", "state law",
                          "primary purpose is not immigration enforcement",
                          "enforcement or investigative duties are primarily related to a violation of state or federal law unrelated to immigration enforcement",
                          "agency participation does not violate applicable local law or policy", "TRUTH Act",
                          "citizenship/immigration-status", "not general ALPR dissemination"):
            self.assertIn(condition, exceptions)
        self.assertIn("7284.6(c)-(d)", rule["remedy"])
        self.assertIn("7284.8 concerns guidance/model policies", rule["remedy"])
        self.assertEqual(rule["review"], "needs_attorney_review")

    def test_deadline_is_provisional_even_when_receipt_or_extension_is_assumed(self):
        self.assertIn("Provisional reminders only", law.HOLIDAY_NOTE)
        self.assertIn("holidays are not modeled", law.HOLIDAY_NOTE)
        # Jan 1 is a known current holiday; unchanged arithmetic does not adjust it.
        self.assertEqual(law.deadline(self.package, "2025-12-22").isoformat(), "2026-01-01")
        self.assertEqual(law.deadline(self.package, "2026-01-05", extension=True).isoformat(),
                         "2026-01-29")
        self.assertIn("provisional reminders only", self.package["records_law"]["appeal"])
        self.assertIn("send date is not proof of receipt", self.package["records_law"]["appeal"])
        self.assertIn("lawful extension notice", law.HOLIDAY_NOTE)

    def test_chp_report_deadline_has_fiscal_year_trigger_not_cpra_trigger(self):
        rule = self.rules["ca-veh-2413-chp-lpr-retention-and-sharing"]
        self.assertIn("90 days following completion of the fiscal year", rule["duty"])
        self.assertIn("10901(b)", rule["duty"])
        self.assertTrue(any("10901" in s["url"] for s in rule["sources"]))
        self.assertEqual(self.package["records_law"]["determination_days"], 10)

    def test_ab370_january_2026_boundary_does_not_invent_prior_law(self):
        before = {r["rule_id"] for r in law.rules_in_force(self.package, "2025-12-31")}
        after = {r["rule_id"] for r in law.rules_in_force(self.package, "2026-01-01")}
        self.assertNotIn(CPRA_2026, before)
        self.assertIn(CPRA_2026, after)
        rule = self.rules[CPRA_2026]
        self.assertIn("earlier CPRA versions are not supplied", rule["duty"])
        self.assertIn("AB 370", rule["citation"])
        exceptions = " ".join(rule["exceptions"])
        for condition in ("cyberattack", "nonelectronic", "unaffected",
                          "ends on restored access", "currently and directly",
                          "created during and related"):
            self.assertIn(condition, exceptions)
        self.assertEqual(rule["review"], "needs_attorney_review")

    def test_pre_2016_comment_uses_implementation_event_not_later_request(self):
        def selected(implementation_event):
            return {r["rule_id"] for r in law.rules_in_force(self.package, implementation_event)}
        self.assertNotIn(COMMENT, selected("2015-12-31"))
        self.assertIn(COMMENT, selected("2016-01-01"))
        docs = (ROOT / "docs" / "LAW-PACKAGES.md").read_text()
        self.assertIn("implementation event", docs)
        self.assertIn("encouragement is not a mandate", docs)
        self.assertIn("coverage gap", docs)
        # Guidance publication is not a substitute for the statute's 2016 start.
        guidance = "ca-oag-2023-dle-06-out-of-state-sharing-guidance"
        self.assertNotIn(guidance, selected("2023-10-26"))
        self.assertIn(guidance, selected("2023-10-27"))
        self.assertIn(COMMENT, selected("2016-01-01"))

    def test_34090_6_is_one_draft_cross_reference(self):
        # Contract: Gov. Code 34090.6 is carried once, sourced, quoted, draft and
        # review-required, with the city/county and ALPR questions left open.
        # Fails if the rule is absent, duplicated, loses its source date or note,
        # or the package fails the structural checker.
        checked, errors = law_package_check.check(ROOT / "jurisdictions" / "us-ca")
        self.assertEqual(errors, [])
        self.assertEqual(checked, self.package)
        matches = [r for r in self.package["rules"] if "34090.6" in r["citation"]]
        self.assertEqual([r["rule_id"] for r in matches], [SECTION_34090_6])
        rule = matches[0]
        self.assertEqual(rule["citation"], "Gov. Code § 34090.6")
        self.assertEqual((rule["effective_from"], rule["effective_to"]), ("2010-01-01", None))
        self.assertEqual(rule["review"], "needs_attorney_review")
        self.assertEqual(self.package["status"], "draft")
        self.assertEqual(len(rule["sources"]), 1)
        source = rule["sources"][0]
        self.assertEqual(source["url"], "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=GOV&sectionNum=34090.6")
        self.assertEqual(source["accessed"], "2026-11-19")
        self.assertIn("Stats. 2009, Ch. 88", source["title"])
        self.assertIn("the head of a department of a city or city and county, after one year, may destroy recordings of routine video monitoring", rule["duty"])
        self.assertIn("preserved until pending litigation is resolved", rule["duty"])
        notes = " ".join(rule["exceptions"])
        for open_question in ("ordinary county agency", "any particular ALPR data", "routine video monitoring"):
            self.assertIn(open_question, notes)
        self.assertIn("no retention period or duty", rule["duty"])

    def test_draft_gates_and_canonical_bundled_bytes_are_preserved(self):
        self.assertEqual(self.package["status"], "draft")
        self.assertEqual(self.package["reviewed_by"], [])
        self.assertIsNone(self.package["reviewed_at"])
        canonical = ROOT / "jurisdictions" / "us-ca" / "package.json"
        bundled = ROOT / "campaign_tool" / "_resources" / "jurisdictions" / "us-ca" / "package.json"
        self.assertEqual(canonical.read_bytes(), bundled.read_bytes())
        for name in ("policy-checklist.json",):  # DF1207-06: the checklist ships with the package
            self.assertEqual((ROOT / "jurisdictions" / "us-ca" / name).read_bytes(),
                             (ROOT / "campaign_tool" / "_resources" / "jurisdictions" / "us-ca" / name).read_bytes(), name)
        self.assertFalse(law.package_status("us-ca")[0] == "reviewed")


if __name__ == "__main__":
    unittest.main()
