"""Synthetic provenance wording, escaping and rendered-link contracts."""
import copy
import hashlib
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from campaign_tool.cli import read_config
from campaign_tool.content import ContentError, load_content
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
        linked = 0
        self.assertIn("Every document we cite, and where it came from.", library)
        for path in public.rglob("*.html"):
            html = path.read_text()
            if path == public / "data.html":
                # Only independently rehashed public downloads may have full checksums; never source identities.
                descriptor = json.loads((public / "data/datapackage.json").read_text())
                for resource in descriptor["resources"]:
                    digest = hashlib.sha256((public / "data" / resource["path"]).read_bytes()).hexdigest()
                    self.assertEqual(resource["hash"], "sha256:" + digest)
                    self.assertEqual(html.count(digest), 2)
                    html = html.replace(digest, "PUBLIC-DOWNLOAD-CHECKSUM")
            self.assertNotRegex(html, r"[0-9a-fA-F]{40,}")
            for target in re.findall(r'href="(/sources/[0-9a-f]{12}\.html)"[^>]*>Record no\.', html):
                self.assertTrue((public / target[1:]).is_file(), target)  # DF1103-11: each record link has a built page
                linked += 1
            for prefix in re.findall(r'Record no\. <code>([^<]+)</code>', html):
                self.assertRegex(prefix, r"^[0-9a-f]{12}$")
        self.assertGreaterEqual(linked, 1)
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
        self.assertNotIn('href="/sources/', html)
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


WEEK_FORMS = ("2026-W04T10:00:00+00:00", "2026W04T100000Z", "2026-W04 10:00:00Z")


class ReceiptTimestampTests(unittest.TestCase):
    """Every received_at the loader admits renders its written calendar date (omitted-weekday week dates included)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-receipt-dates-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.sources = self.root / "content" / "sources.json"

    def write_receipts(self, value, second=None):
        library = json.loads(self.sources.read_text())
        library[0]["received_at"] = value
        library[1]["received_at"] = second if second is not None else value
        self.sources.write_text(json.dumps(library, indent=2) + "\n")

    def loaded_sentence(self, value, public_url=None):
        self.write_receipts(value)
        entry = load_content(self.root)["sources"][0]
        self.assertEqual(entry["received_at"], value)  # stored value is preserved, not canonicalised
        return source_line(dict(entry, public_url=public_url), SOURCE, "Cedar PD")[0]

    def test_week_only_receipts_admitted_by_loader_render_calendar_date(self):
        for value in WEEK_FORMS:
            with self.subTest(value=value):
                self.assertIn(", received 19 Jan 2026 from Cedar PD.", self.loaded_sentence(value))

    def test_week_year_boundary_uses_calendar_year_not_week_year(self):
        for value in ("2020-W01T10:00:00Z", "2020-W01-1T10:00:00Z"):
            with self.subTest(value=value):
                self.assertIn(", received 30 Dec 2019 from Cedar PD.", self.loaded_sentence(value))

    def test_receipt_date_stays_in_written_offset(self):
        cases = (("2026-01-25T23:30:00-08:00", "25 Jan 2026"), ("2026-01-25T00:30:00+09:00", "25 Jan 2026"),
                 ("2026-W04T23:30:00-08:00", "19 Jan 2026"), ("2026-W04T00:30:00+09:00", "19 Jan 2026"))
        for value, shown in cases:
            with self.subTest(value=value):
                self.assertIn(f"received {shown} from", self.loaded_sentence(value))

    def test_week_receipt_renders_on_all_source_surfaces_and_real_build(self):
        self.write_receipts("2026-W04T10:00:00+00:00", "2026W04T100000Z")
        before = self.sources.read_bytes()
        build_site(self.root)
        public = self.root / "public"
        self.assertEqual(self.sources.read_bytes(), before)
        library = (public / "sources.html").read_text()
        self.assertEqual(library.count("received 19 Jan 2026 from"), 2)
        record = (public / "sources" / ("cafecafecafe.html")).read_text()
        self.assertIn("received 19 Jan 2026 from", record)
        finding = (public / "findings" / "cedar-policy-posted.html").read_text()
        self.assertIn("received 19 Jan 2026 from", finding)
        for html in (library, record, finding):
            self.assertIn(EXPLAINER, html)
        for target in re.findall(r'href="(/sources/[0-9a-f]{12}\.html)"', library + finding):
            self.assertTrue((public / target[1:]).is_file(), target)

    def test_legacy_receipts_and_optional_provenance_are_byte_identical(self):
        legacy = ("2026-01-25T10:00:00+00:00", "20260125T100000Z", "2026-W04-7T10:00:00Z", "2026-01-25T10:00:00",
                  "2026-01-25")
        for value in legacy:
            with self.subTest(value=value):
                entry = dict(ENTRY, received_at=value)
                self.assertEqual(
                    source_line(entry, SOURCE, "Cedar PD"),
                    ("From a document we hold: Policy, received 25 Jan 2026 from Cedar PD. A public copy is linked."
                     " Where: page:2. Rule R1.", "cafecafecafe"))
        hostile = '<b>"&\''
        sentence, record = source_line(dict(ENTRY, public_url=None), dict(SOURCE, locator=hostile, rule_id=hostile), None)
        self.assertEqual(sentence, "From a document we hold: Policy, received 25 Jan 2026. The original is kept privately."
                                   f" Where: {e(hostile)}. Rule {e(hostile)}.")
        self.assertEqual(source_line(None, SOURCE, "Cedar PD")[0],
                         "From a document we hold: Policy. Not yet listed in the source library. Where: page:2. Rule R1.")
        self.assertEqual(record, "cafecafecafe")

    def test_malformed_receipts_are_still_refused_and_leave_public_tree_untouched(self):
        build_site(self.root)
        public = self.root / "public"
        tree = {p.relative_to(public): p.read_bytes() for p in public.rglob("*") if p.is_file()}
        for value in ("2026-W99T10:00:00Z", "2026-W54T10:00:00Z", "not a date", "2026-W04T10:00:00+99:00"):
            with self.subTest(value=value):
                self.write_receipts(value)
                before = self.sources.read_bytes()
                with self.assertRaises(ContentError):
                    load_content(self.root)
                with self.assertRaises(ContentError):
                    build_site(self.root)
                self.assertEqual(self.sources.read_bytes(), before)
                self.assertEqual(tree, {p.relative_to(public): p.read_bytes() for p in public.rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
