"""Synthetic provenance wording, escaping and rendered-link contracts."""
import copy
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from campaign_tool.cli import read_config
from campaign_tool.content import load_content
from campaign_tool.provenance_text import EXPLAINER, source_line
from campaign_tool.site import Site, build_site, e

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/fictional-campaign"
SOURCE = {"title": "Policy", "sha256": "cafe" * 16, "locator": "page:2", "rule_id": "R1"}
ENTRY = dict(SOURCE, received_at="2026-01-25T10:00:00+00:00", public_url="https://example.invalid/policy")


class ProvenanceTextTests(unittest.TestCase):
    def test_receipt_public_private_and_unknown_agency(self):
        for agency in ("Cedar PD", None, ""):
            for public in (ENTRY["public_url"], None):
                with self.subTest(agency=agency, public=public):
                    entry = dict(ENTRY, public_url=public)
                    sentence, record = source_line(entry, SOURCE, agency)
                    expected = "From a document we hold: Policy, received 25 Jan 2026"
                    expected += " from Cedar PD" if agency else ""
                    expected += ". A public copy is linked." if public else ". The original is kept privately."
                    self.assertEqual(sentence, expected + " Where: page:2. Rule R1.")
                    self.assertEqual(record, "cafecafecafe")

    def test_uncatalogued_and_empty_optional_fields(self):
        sentence, record = source_line(None, dict(SOURCE, locator="", rule_id=None), None)
        self.assertEqual(sentence, "From a document we hold: Policy. Not yet listed in the source library.")
        self.assertNotIn("received", sentence)
        self.assertNotIn("Where:", sentence)
        self.assertEqual(record, "cafecafecafe")
        sentence, _ = source_line(None, SOURCE, "Not a receipt agency")
        self.assertTrue(sentence.endswith(" Where: page:2. Rule R1."))
        self.assertNotIn("Not a receipt agency", sentence)

    def test_hostile_parts_are_escaped_not_interpreted(self):
        hostile = '<script>" & \'\u202e'
        source = dict(SOURCE, title=hostile, locator=hostile, rule_id=hostile)
        for entry in (None, dict(ENTRY, title=hostile)):
            sentence, _ = source_line(entry, source, hostile)
            self.assertNotIn("<script", sentence)
            self.assertIn(e(hostile), sentence)
            self.assertNotIn('"', sentence)
            self.assertNotIn("'", sentence)


class ProvenancePagesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-provenance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.model = load_content(self.root)

    def site(self, model=None):
        return Site(self.root, read_config(self.root), model or self.model)

    def test_built_pages_have_short_records_resolving_links_and_one_explainer(self):
        build_site(self.root)
        public = self.root / "public"
        library = (public / "sources.html").read_text()
        ids = set(re.findall(r'\bid="([^"]+)"', library))
        self.assertIn("Every document we cite, and where it came from.", library)
        for path in public.rglob("*.html"):
            html = path.read_text()
            self.assertNotRegex(html, r"[0-9a-fA-F]{40,}")
            for fragment in re.findall(r'href="/sources.html#([^"]+)"[^>]*>Record no\.', html):
                self.assertIn(fragment, ids)
            for prefix in re.findall(r'Record no\. <code>([^<]+)</code>', html):
                self.assertRegex(prefix, r"^[0-9a-f]{12}$")
        pages = [library] + [(public / "findings" / (f["slug"] + ".html")).read_text()
                             for f in self.model["findings"]]
        for html in pages:
            self.assertEqual(html.count(EXPLAINER), 1)
            self.assertIn("From a document we hold:", html)
        self.assertNotRegex(EXPLAINER.lower(), r"proves authenticity|verified authentic|official")

    def test_missing_library_entry_has_no_record_link_or_invented_date(self):
        model = copy.deepcopy(self.model)
        model["sources"] = []
        html = self.site(model).finding_html(model["findings"][0])["html"]
        self.assertIn("Not yet listed in the source library.", html)
        self.assertIn('<p class="record-no">Record no. <code>cafecafecafe</code></p>', html)
        self.assertNotIn('href="/sources.html#', html)
        self.assertNotIn(", received ", html)

    def test_unknown_or_unselected_agency_does_not_invent_attribution(self):
        model = copy.deepcopy(self.model)
        model["agencies"] = []
        site = self.site(model)
        for page in (site.finding_html(model["findings"][0]), site.sources_html()):
            html = page["html"]
            self.assertIn(", received ", html)
            self.assertNotRegex(html, r", received [^.]+ from ")

    def test_hostile_rendered_sources_add_no_script_and_keep_record_links(self):
        model = copy.deepcopy(self.model)
        hostile = '<script>" & \'\u202e'
        for source in model["sources"] + model["findings"][0]["sources"]:
            source["title"] = hostile
            source["locator"] = hostile
        site = self.site(model)
        baseline = self.site()
        for html, old in ((site.sources_html(), baseline.sources_html()),
                          (site.finding_html(model["findings"][0]), baseline.finding_html(self.model["findings"][0]))):
            html, old = html["html"], old["html"]
            self.assertEqual(html.count("<script"), old.count("<script"))
            self.assertIn(e(hostile), html)
            self.assertIn("Record no. <code>cafecafecafe</code>", html)


if __name__ == "__main__":
    unittest.main()
