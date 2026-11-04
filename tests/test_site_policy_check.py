"""DF1207-06: an organizer-verified profile.policy_check records, per element Civ. Code 1798.90.51(b) lists for a usage and
privacy policy, whether the posted policy addresses it; the agency page shows it as an organizer's reading, never as a
compliance finding. Offline, synthetic agencies, example.org URLs only.
"""
import copy
import json
import re
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool import law
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.legal_claims import legal_claims
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)  # fixed site build date
IDS = ["authorized_purposes", "authorized_users_training", "security_monitoring", "sharing_restrictions",
       "custodian_title", "accuracy_correction", "retention_destruction"]
POLICY = {"url": "https://example.org/cedar/alpr-policy.pdf", "source_url": "https://example.org/cedar/policies",
          "checked_on": "2026-10-30"}
HEADING = "<h4>What the posted policy covers (organizer reading, not a legal review)</h4>"
FORBIDDEN = ("compl", "violat", "unlawful", "illegal", "fails to")
LABEL = "profile.policy_check"


def check(**changes):
    elements = {i: {"status": "not_found"} for i in IDS}
    elements["authorized_purposes"] = {"status": "addressed", "page": 3}
    elements["custodian_title"] = {"status": "addressed"}
    elements["accuracy_correction"] = {"status": "unclear"}
    value = {"checked_on": "2026-10-31", "policy_url": POLICY["url"], "elements": elements}
    value.update(changes)
    return value


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


class PolicyCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-policy-check-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "agencies.json"
        self.base = json.loads(self.path.read_text())
        self.police = next(a for a in self.base if a["agency_id"] == "ca-cedar-police")  # has sources, so an agency page
        self.label = f"content/agencies.json[{self.base.index(self.police)}].{LABEL}"

    def write(self, alpr=POLICY, policy_check=..., **profile):
        document = copy.deepcopy(self.base)
        entry = next(a for a in document if a["agency_id"] == "ca-cedar-police")
        if alpr is not ...:
            entry["profile"]["alpr_policy"] = alpr
        if policy_check is not ...:
            entry["profile"]["policy_check"] = policy_check
        entry["profile"].update(profile)
        self.path.write_text(json.dumps(document))

    def build(self):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root, today=TODAY)
        self.assertEqual(urlopen.call_count, 0)

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def section(self, page):
        return "<section" + page.split('<section class="policy-check"', 1)[1].split("</section>", 1)[0] + "</section>"

    def refusal(self, policy_check, alpr=POLICY):
        self.write(alpr, policy_check)
        with self.assertRaises(ContentError) as caught:
            self.build()
        self.assertIn(self.label, str(caught.exception))
        self.assertIn(str(caught.exception), content_problems(self.root))  # content-check names the same text
        return str(caught.exception)

    # 1
    def test_full_check_lists_seven_items_in_checklist_order_with_pages_and_law_status(self):
        self.write(policy_check=check())
        self.build()
        expected = [
            "Authorized purposes for using the system and collecting information: addressed (page 3)",
            "Job titles of the people authorized to use or access the system, and training requirements: not found in the posted policy",
            "Monitoring of security and of privacy-law requirements: not found in the posted policy",
            "Purposes, processes and restrictions for selling, sharing or transferring information: not found in the posted policy",
            "Title of the custodian or owner responsible for the system: addressed",
            "Measures for accuracy and error correction: unclear",
            "How long information is kept and how it is decided whether and when to destroy it: not found in the posted policy"]
        tail = ('<p>Checked 2026-10-31 against <a href="https://example.org/cedar/alpr-policy.pdf" rel="noopener noreferrer">'
                'the posted policy</a>. Law package: draft, not legally reviewed.</p></section>')
        for page in (self.read("agencies/cedar.html"), self.read("agencies.html")):
            html = self.section(page)
            self.assertEqual(html.count(HEADING), 1)
            self.assertEqual(re.findall(r"<li>(.*?)</li>", html), expected)
            self.assertTrue(html.endswith(tail), html[-200:])
        self.assertEqual(self.read("agencies.html").count('class="policy-check"'), 1)  # that card only
        self.assertEqual(list(law.load_checklist("us-ca")["elements"][i]["id"] for i in range(7)), IDS)

    # 2
    def test_policy_url_must_equal_the_verified_alpr_policy_url(self):
        self.assertIn(f"{self.label}.policy_url: must equal profile.alpr_policy.url",
                      self.refusal(check(policy_url="https://example.org/cedar/other.pdf")))
        self.assertIn(f"{self.label}.policy_url: needs profile.alpr_policy", self.refusal(check(), alpr=None))
        entry = {k: v for k, v in check().items() if k != "policy_url"}
        self.assertIn(f"{self.label}.policy_url: required", self.refusal(entry))

    # 3
    def test_element_problems_are_each_refused_by_field_and_content_check_lists_all(self):
        elements = check()["elements"]
        del elements["custodian_title"]  # missing id
        elements["invented_element"] = {"status": "addressed"}  # unknown id
        elements["security_monitoring"] = {"status": "not_found", "page": 2}  # page without addressed
        elements["authorized_users_training"] = {"status": "addressed", "page": 0}
        elements["sharing_restrictions"] = {"status": "addressed", "page": 10000}
        elements["retention_destruction"] = {"status": "compliant"}
        elements["authorized_purposes"] = {"status": "addressed", "page": True}
        message = self.refusal(check(elements=elements))
        for fragment in (f"{self.label}.elements.custodian_title: missing",
                         f"{self.label}.elements.invented_element: unknown element",
                         f"{self.label}.elements.security_monitoring.page: only allowed when status is addressed",
                         f"{self.label}.elements.authorized_users_training.page: must be a whole number from 1 to 9999",
                         f"{self.label}.elements.sharing_restrictions.page: must be a whole number from 1 to 9999",
                         f"{self.label}.elements.retention_destruction.status: choose one of",
                         f"{self.label}.elements.authorized_purposes.page: must be a whole number from 1 to 9999"):
            self.assertIn(fragment, message)
        self.assertEqual([l for l in content_problems(self.root) if self.label in l], [message])  # one line, all fields
        for page in (1, 9999):  # the edges are accepted
            self.write(policy_check=check(elements={**check()["elements"], "authorized_purposes": {"status": "addressed", "page": page}}))
            self.build()
        self.assertIn(f"{self.label}: unknown field 'score'", self.refusal(check(score=1)))
        self.assertIn(f"{self.label}.elements: must be an object", self.refusal(check(elements=[])))
        self.assertIn(f"{self.label}: must be an object", self.refusal("addressed"))

    # 4
    def test_checked_on_must_be_a_real_day_in_range(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.assertIn(f"{self.label}.checked_on: must not be after the build date", self.refusal(check(checked_on=tomorrow)))
        self.assertIn(f"{self.label}.checked_on: must not be before profile.alpr_policy.checked_on",
                      self.refusal(check(checked_on="2026-10-29")))
        self.assertIn(f"{self.label}.checked_on", self.refusal(check(checked_on="2026-02-30")))
        self.write(policy_check=check(checked_on=POLICY["checked_on"]))  # the same day is allowed
        self.build()

    # 5
    def test_checklist_resource_is_refused_when_not_bound_to_a_package_rule(self):
        base = self.tmp / "jurisdictions"
        (base / "us-ca").mkdir(parents=True)
        shutil.copy(REPO_ROOT / "jurisdictions" / "us-ca" / "package.json", base / "us-ca" / "package.json")
        source = json.loads((REPO_ROOT / "jurisdictions" / "us-ca" / "policy-checklist.json").read_text())

        def load(**changes):
            (base / "us-ca" / "policy-checklist.json").write_text(json.dumps({**source, **changes}))
            return law.load_checklist("us-ca", base)
        self.assertEqual(load()["rule_id"], "ca-civ-1798.90.51-usage-and-privacy-policy")
        self.assertEqual(load(status="reviewed")["status"], "reviewed")
        for changes, message in (({"rule_id": "ca-civ-no-such-rule"}, "rule_id"),
                                 ({"status": "approved"}, "status"),
                                 ({"elements": source["elements"] + [source["elements"][0]]}, "duplicate"),
                                 ({"elements": []}, "elements")):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                load(**changes)
        self.assertEqual(law.load_checklist("us-ca")["status"], "draft")
        with mock.patch.object(law, "JURISDICTIONS", base):  # a build whose checklist is not bound is refused
            (base / "us-ca" / "policy-checklist.json").write_text(json.dumps({**source, "rule_id": "ca-civ-no-such-rule"}))
            self.write(policy_check=check())
            with self.assertRaisesRegex(ContentError, "policy checklist could not be loaded"):
                self.build()

    # 6
    def test_rendered_section_has_no_compliance_wording_and_escapes_labels(self):
        self.write(policy_check=check())
        self.build()
        for page in (self.read("agencies/cedar.html"), self.read("agencies.html")):
            html = self.section(page)
            text = re.sub(r"<[^>]+>", " ", html)
            self.assertEqual(legal_claims(text), [])
            for word in FORBIDDEN:
                self.assertNotIn(word, html.lower())
        base = self.tmp / "jurisdictions"
        (base / "us-ca").mkdir(parents=True)
        shutil.copy(REPO_ROOT / "jurisdictions" / "us-ca" / "package.json", base / "us-ca" / "package.json")
        marked = json.loads((REPO_ROOT / "jurisdictions" / "us-ca" / "policy-checklist.json").read_text())
        marked["elements"][0]["label"] = "Purposes <b>& more"
        (base / "us-ca" / "policy-checklist.json").write_text(json.dumps(marked))
        with mock.patch.object(law, "JURISDICTIONS", base):
            self.build()
        html = self.section(self.read("agencies.html"))
        self.assertIn("<li>Purposes &lt;b&gt;&amp; more: addressed (page 3)</li>", html)
        self.assertNotIn("<b>", html)

    # 7
    def test_agency_without_policy_check_changes_no_page(self):
        self.write()
        self.build()
        plain = self.snapshot()
        self.assertFalse([n for n, b in plain.items() if b"policy-check" in b or b"posted policy covers" in b])
        self.write(policy_check=None)
        self.build()
        self.assertEqual(self.snapshot(), plain)
        self.write(policy_check=check())
        self.build()
        with_check = self.snapshot()
        changed = {n for n in plain.keys() | with_check.keys() if plain.get(n) != with_check.get(n)}
        self.assertEqual(changed, {"agencies.html", "agencies/cedar.html"})  # no card JS, open data, feed or other page
        for name, data in with_check.items():
            if name not in changed:
                self.assertNotIn(b"posted policy covers", data, name)

    # 8
    def test_two_builds_are_byte_identical(self):
        self.write(policy_check=check())
        self.build()
        first = self.snapshot()
        self.build()
        self.assertEqual(self.snapshot(), first)

    def test_an_unselected_agency_with_a_broken_check_is_skipped(self):
        self.write()
        document = json.loads(self.path.read_text())
        sheriff = next(a for a in document if a["agency_id"] == "ca-cedar-county-sheriff")
        sheriff.update(selected=False)
        sheriff["profile"]["policy_check"] = {"checked_on": "nope"}
        self.path.write_text(json.dumps(document))
        self.build()
        self.assertEqual(content_problems(self.root), [])


if __name__ == "__main__":
    unittest.main()
