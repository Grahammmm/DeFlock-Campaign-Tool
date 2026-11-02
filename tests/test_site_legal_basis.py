"""Finding page "Legal basis" section (DF1507-02): the finding's own rule_version/duty/exceptions and each cited rule's
law-package citation with the package status label; only rules the finding cites.

Synthetic package only, written under a temporary jurisdictions base (``law.JURISDICTIONS`` patched); the shipped
us-ca package serves only as a schema-valid shape whose rules are all replaced. Hex fixtures are generated at test
time. Run from the repository root: python3 -B -m unittest tests.test_site_legal_basis -v
"""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import law, site
from campaign_tool.cli import read_config
from campaign_tool.content import load_content
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SLUG = "cedar-policy-posted"
CITATIONS = {"ca-alpha": "Synthetic Code sec. 1 & 2", "ca-beta": "Synthetic Code sec. 9",
             "ca-uncited": "Synthetic Uncited Code sec. 77"}


def synthetic_package(status="draft"):
    package = copy.deepcopy(law.load_package("us-ca"))
    template = package["rules"][0]
    package["rules"] = [dict(copy.deepcopy(template), rule_id=rule_id, citation=citation, effective_from="2020-01-01",
                             effective_to=None, duty="PACKAGE-DUTY-TEXT", exceptions=["PACKAGE-EXCEPTION-TEXT"],
                             remedy="PACKAGE-REMEDY-TEXT")
                        for rule_id, citation in CITATIONS.items()]
    for scope in package["request_scopes"]:
        scope["rule_ids"] = ["ca-alpha"]
    package.update(status=status, reviewed_by=["Synthetic Reviewer"] if status == "reviewed" else [],
                   reviewed_at="2026-02-03" if status == "reviewed" else None)
    return package


def sha(n):
    return hashlib.sha256(f"synthetic-df1507-02-{n}".encode()).hexdigest()


class LegalBasisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-legal-basis-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name) / "jurisdictions"
        (self.base / "us-ca").mkdir(parents=True)
        self.write_package(synthetic_package())
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        patcher = mock.patch.object(law, "JURISDICTIONS", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_package(self, package):
        (self.base / "us-ca" / "package.json").write_text(json.dumps(package), encoding="utf-8")

    def finding(self, source_rules=(None, None, None), **changes):
        path = EXAMPLE / "content" / "findings" / f"{SLUG}.json"  # always the pristine example as the base
        document = json.loads(path.read_text(encoding="utf-8"))
        document["sources"] = [{"sha256": sha(i), "locator": f"page:{i + 1}", "title": "Synthetic record",
                                "rule_id": rule} for i, rule in enumerate(source_rules)]
        document.update(event_date="2021-04-01", **changes)
        (self.root / "content" / "findings" / f"{SLUG}.json").write_text(json.dumps(document), encoding="utf-8")

    def page(self):
        self.assertTrue(build_site(self.root))
        return (self.root / "public" / "findings" / f"{SLUG}.html").read_text(encoding="utf-8")

    def section(self, html):
        start = html.index('<section class="legal-basis">')
        return html[start:html.index("</section>", start) + len("</section>")]

    def test_two_cited_rules_and_all_fields_sorted_with_draft_label(self):
        self.finding(rule_ids=["ca-beta"], source_rules=(None, "ca-alpha", None), rule_version="2021-01",
                     duty="Post the policy", exceptions=["Ongoing investigation", "Court order"])
        html = self.page()
        part = self.section(html)
        self.assertIn('<h3 id="legal-basis">Legal basis</h3>', part)
        self.assertIn("<p>Rule version: 2021-01</p><p>Duty: Post the policy</p>", part)
        self.assertIn("<p>Exceptions:</p><ul><li>Ongoing investigation</li><li>Court order</li></ul>", part)
        self.assertIn("<ul><li><code>ca-alpha</code> — Synthetic Code sec. 1 &amp; 2</li>"
                      "<li><code>ca-beta</code> — Synthetic Code sec. 9</li></ul>", part)
        self.assertIn('<p class="notice">Law package status: draft, not legally reviewed.</p>', part)
        self.assertNotIn("reviewed on", part)
        # placed between Sources and Limitations; the source suffix rule mention stays in Sources
        self.assertLess(html.index("<h3>Sources</h3>"), html.index('id="legal-basis"'))
        self.assertLess(html.index('id="legal-basis"'), html.index("<h3>Limitations</h3>"))

    def test_reviewed_package_shows_review_date_and_no_draft_label(self):
        self.write_package(synthetic_package(status="reviewed"))
        self.finding(rule_ids=["ca-alpha"])
        part = self.section(self.page())
        self.assertIn('<p class="notice">Law package status: reviewed on 2026-02-03.</p>', part)
        self.assertNotIn("draft", part)
        self.assertNotIn("not legally reviewed", part)

    def test_no_rule_and_no_fields_is_byte_identical_and_never_reads_the_package(self):
        self.finding()
        with mock.patch.object(law, "load_package", side_effect=AssertionError("package read")) as loader:
            built = self.page()
            with mock.patch.object(site.Site, "legal_basis", lambda self, finding: ""):
                golden = self.page()  # finding_html without the new section: the page as built before DF1507-02
        loader.assert_not_called()
        self.assertEqual(built.encode("utf-8"), golden.encode("utf-8"))
        self.assertNotIn("legal-basis", built)
        self.assertNotIn("Law package status", built)

    def test_exceptions_as_string_and_hostile_duty_are_escaped(self):
        self.finding(duty='Post "it" & keep it', exceptions="Only sealed records & drafts")
        part = self.section(self.page())
        self.assertIn("<p>Duty: Post &quot;it&quot; &amp; keep it</p>", part)
        self.assertIn("<p>Exceptions: Only sealed records &amp; drafts</p>", part)
        self.assertNotIn("Rule version", part)
        # the loader refuses angle brackets; the renderer still escapes a hand-built finding
        model = load_content(self.root)
        finding = dict(model["findings"][0], duty='<script>alert("x")</script> & more',
                       exceptions="Only <b>sealed</b> records")
        part = site.Site(self.root, read_config(self.root), model).legal_basis(finding)
        self.assertNotIn("<script>", part)
        self.assertNotIn("<b>", part)
        self.assertIn("<p>Duty: &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; more</p>", part)
        self.assertIn("<p>Exceptions: Only &lt;b&gt;sealed&lt;/b&gt; records</p>", part)

    def test_uncited_rule_and_package_rule_text_never_appear(self):
        self.finding(rule_ids=["ca-alpha"])
        html = self.page()
        part = self.section(html)
        self.assertIn("<code>ca-alpha</code>", part)
        for absent in ("ca-uncited", CITATIONS["ca-uncited"], "ca-beta", CITATIONS["ca-beta"],
                       "PACKAGE-DUTY-TEXT", "PACKAGE-EXCEPTION-TEXT", "PACKAGE-REMEDY-TEXT"):
            self.assertNotIn(absent, html)
        self.assertNotIn("Duty:", part)
        self.assertNotIn("Exceptions:", part)

    def test_fields_without_a_cited_rule_omit_rule_list_and_package_label(self):
        self.finding(rule_version="v3", exceptions=[])
        with mock.patch.object(law, "load_package", side_effect=AssertionError("package read")) as loader:
            part = self.section(self.page())
        loader.assert_not_called()
        self.assertEqual(part, '<section class="legal-basis"><h3 id="legal-basis">Legal basis</h3>'
                               "<p>Rule version: v3</p></section>")


if __name__ == "__main__":
    unittest.main()
