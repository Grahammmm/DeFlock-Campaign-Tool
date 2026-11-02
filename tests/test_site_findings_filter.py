"""RF1510-3: findings index filter (data attributes, hidden form, first-party findings/filter.js).

Built from a temp copy of the fictional example plus synthetic findings; no network.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import site as site_module
from campaign_tool.site import EXTRA_CSS, FILTER_JS, Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE_SLUG = "cedar-policy-posted"
POLICE, SHERIFF = "ca-cedar-police", "ca-cedar-county-sheriff"
# slug, agency_id, classification, event_date (the example finding is police / documented_fact / 2026-01-15)
EXTRA = [("synthetic-two", POLICE, "apparent_conflict", "2025-06-01"),
         ("synthetic-three", SHERIFF, "information_gap", "2026-03-02"),
         ("synthetic-four", SHERIFF, "documented_fact", "2025-11-20"),
         ("synthetic-five", None, "apparent_conflict", "2026-04-01"),
         ("synthetic-six", SHERIFF, "information_gap", "2025-02-02")]
SCRIPT_TAG = re.compile(r'^<script defer src="/(?:vendor/maplibre-gl|site-data|agency-cards|app|findings/filter)\.js"></script>$')


class FindingsFilterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-findings-filter-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.findings = self.root / "content" / "findings"

    def add(self, rows):
        base = json.loads((self.findings / f"{BASE_SLUG}.json").read_text(encoding="utf-8"))
        for number, (slug, agency, classification, event_date) in enumerate(rows, start=2):
            document = dict(base, slug=slug, id=f"fnd_{number:016d}", title=f"Synthetic finding {slug}", corrections=[],
                            agency_id=agency, classification=classification, event_date=event_date)
            if classification == "apparent_conflict":  # review gate: a conflict names its rule, duty and exceptions
                document.update(rule_version="synthetic-v1", duty="Synthetic duty.", exceptions=["Synthetic exception."])
            (self.findings / f"{slug}.json").write_text(json.dumps(document), encoding="utf-8")

    def build(self):
        written = {p.relative_to(self.public).as_posix() for p in build_site(self.root)}
        return written, self.read("findings/index.html")

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def options(self, html, key):
        found = re.search(rf'<label for="filter-{key}">[^<]+</label><select id="filter-{key}" name="{key}">(.*?)</select>', html)
        return found and re.findall(r'<option value="([^"]*)">([^<]*)</option>', found.group(1))

    def items(self, html):
        return re.findall(r'<li data-agency="([^"]*)" data-classification="([^"]*)" data-year="([^"]*)">', html)

    def test_three_findings_no_form_no_script_but_attributes(self):
        self.add(EXTRA[:2])
        written, html = self.build()
        self.assertNotIn("finding-filter", html)
        self.assertNotIn("filter.js", html)
        self.assertNotIn("filter-empty", html)
        self.assertNotIn("findings/filter.js", written)
        self.assertEqual(sorted(self.items(html)), sorted([(POLICE, "documented_fact", "2026"),
                                                           (POLICE, "apparent_conflict", "2025"),
                                                           (SHERIFF, "information_gap", "2026")]))

    def test_four_findings_with_no_varying_dimension_get_no_form(self):
        self.add([(f"same-{n}", POLICE, "documented_fact", f"2026-0{n}-01") for n in range(2, 5)])
        written, html = self.build()
        self.assertEqual(len(self.items(html)), 4)
        self.assertNotIn("<form", html)
        self.assertNotIn("findings/filter.js", written)

    def test_six_findings_three_selects_ordered_and_count(self):
        self.add(EXTRA)
        written, html = self.build()
        self.assertIn("findings/filter.js", written)
        self.assertEqual(self.read("findings/filter.js"), FILTER_JS)
        self.assertLess(len(FILTER_JS.encode("utf-8")), 2048)
        self.assertIn('<script defer src="/findings/filter.js"></script>', html)
        self.assertIn('<form class="finding-filter" id="finding-filter" aria-label="Filter findings" hidden>', html)
        self.assertEqual(self.options(html, "agency"), [
            ("all", "All agencies"), (SHERIFF, "Cedar County Sheriff&#x27;s Office (fictional)"),
            (POLICE, "Cedar Police Department (fictional)"), ("none", "No agency named")])
        self.assertEqual(self.options(html, "classification"), [
            ("all", "All classifications"), ("documented_fact", "Documented fact"),
            ("apparent_conflict", "Apparent conflict"), ("information_gap", "Information gap")])
        self.assertEqual(self.options(html, "year"), [("all", "All years"), ("2026", "2026"), ("2025", "2025")])
        self.assertIn('<p class="filter-count" id="filter-count" aria-live="polite">Showing 6 of 6</p></form>', html)
        self.assertIn('<p class="filter-empty" id="filter-empty" hidden>No published finding matches these filters.</p>', html)
        self.assertLess(html.index("finding-filter"), html.index('<ul class="finding-list">'))
        items = self.items(html)
        self.assertEqual(len(items), 6)
        self.assertIn(("none", "apparent_conflict", "2026"), items)
        listed = html.split('<ul class="finding-list">')[1].split("</ul>")[0]
        self.assertEqual(listed.count("<li"), 6)  # the no-JS page is still the full list, nothing hidden
        self.assertNotRegex(html, r"<li[^>]* hidden")

    def test_home_page_and_other_lists_byte_identical_without_attributes(self):
        self.add(EXTRA)
        self.build()
        with_attrs = {name: self.read(name) for name in ("index.html", "agencies.html", "sources.html", "corrections.html")}
        index_with = self.read("findings/index.html")
        real = Site.findings_list
        with mock.patch.object(Site, "findings_list", lambda s, f, *a, filter_attrs=False, **k: real(s, f, *a, **k)):
            self.build()
        for name, html in with_attrs.items():
            self.assertEqual(self.read(name), html, name)
            self.assertNotIn("data-agency", html, name)
        self.assertNotEqual(self.read("findings/index.html"), index_with)
        self.assertEqual(self.items(self.read("findings/index.html")), [])

    def test_one_year_omits_year_select(self):
        self.add([(slug, agency, code, "2026" + day[4:]) for slug, agency, code, day in EXTRA])
        _, html = self.build()
        self.assertIsNone(self.options(html, "year"))
        self.assertNotIn("filter-year", html)
        self.assertEqual(len(self.options(html, "agency")), 4)
        self.assertEqual(len(self.options(html, "classification")), 4)

    def test_agency_id_and_name_escaped_in_attributes(self):
        self.add(EXTRA)
        hostile, name = 'ca-x"><script>alert(1)</script>', 'Sheriff <b>"quoted"</b>'
        real = site_module.load_content

        def hostile_model(root):
            model = real(root)
            for agency in model["agencies"]:
                if agency["agency_id"] == SHERIFF:
                    agency.update(agency_id=hostile, name=name)
            for finding in model["findings"]:
                if finding["agency_id"] == SHERIFF:
                    finding["agency_id"] = hostile
            return model
        with mock.patch.object(site_module, "load_content", hostile_model):
            _, html = self.build()
        self.assertNotIn("<script>alert", html)
        self.assertNotIn('"quoted"', html)
        escaped = "ca-x&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"
        self.assertEqual(sum(1 for item in self.items(html) if item[0] == escaped), 3)
        self.assertIn((escaped, "Sheriff &lt;b&gt;&quot;quoted&quot;&lt;/b&gt;"), self.options(html, "agency"))

    def test_build_check_csp_unchanged_no_inline_script_print_rules(self):
        from tests.test_site_build import EXPECTED_CSP
        self.add(EXTRA)
        self.build()
        self.assertIn("Content-Security-Policy: " + EXPECTED_CSP + "\n", self.read("_headers"))
        for page in self.public.rglob("*.html"):
            html = page.read_text(encoding="utf-8")
            tags = re.findall(r"<script\b[^>]*>.*?</script>", html, flags=re.S)
            self.assertEqual(html.count("<script"), len(tags), page.name)
            for tag in tags:
                self.assertRegex(tag, SCRIPT_TAG, page.name)
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                                capture_output=True, text=True, cwd=REPO_ROOT, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("findings/filter.js", result.stdout)
        screen, printed = EXTRA_CSS.split("@media print{")
        self.assertIn(".finding-filter[hidden]{display:none}", screen)
        hide = re.search(r"\n([^{}\n]+)\{display:none!important\}", printed).group(1).split(",")
        for selector in (".finding-filter", ".filter-count", ".filter-empty"):
            self.assertIn(selector, hide)
        self.assertIn("\n.finding-list li[hidden]{display:list-item}\n", printed)


if __name__ == "__main__":
    unittest.main()
