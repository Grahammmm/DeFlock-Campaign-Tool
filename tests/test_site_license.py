"""DF1422-13: site.json.license on every page and open-data file; cameras.geojson always ODbL with OSM attribution.

Synthetic content only (a temp copy of the fictional example); no network.
"""
import json
import shutil
import tempfile
import unittest
from html import escape
from pathlib import Path

from campaign_tool import law
from campaign_tool.content import LICENSES, ODBL, ContentError
from campaign_tool.site import build_site, preview_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMAS = {name: json.loads((REPO_ROOT / "schemas" / f"{name}-open-data.schema.json").read_text(encoding="utf-8"))
           for name in ("findings", "sources", "agencies", "meetings")}
OSM = "\u00a9 OpenStreetMap contributors"


class LicenseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-license-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.content = self.root / "content"

    def set_license(self, value, present=True):
        path = self.content / "site.json"
        site = json.loads(path.read_text(encoding="utf-8"))
        site.pop("license", None)
        if present:
            site["license"] = value
        path.write_text(json.dumps(site), encoding="utf-8")

    def build(self, value=None, present=True):
        self.set_license(value, present)
        build_site(self.root)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def refused(self, value):
        self.set_license(value)
        with self.assertRaises(ContentError) as caught:
            build_site(self.root)
        self.assertFalse(self.public.exists(), "a refused licence must write nothing")
        return str(caught.exception)

    def test_each_id_is_shown_everywhere_and_stamped_in_every_data_file(self):
        for license_id, (name, url) in LICENSES.items():
            with self.subTest(license_id):
                self.build({"id": license_id})
                for page in ("index.html", "about.html", "privacy.html", "findings/cedar-policy-posted.html"):
                    self.assertIn(f'<a href="{url}" rel="license">{escape(name)}</a>', self.read(page), page)
                self.assertIn(f"<p>Data and text: <a href=\"{url}\" rel=\"license\">{escape(name)}</a></p>", self.read("about.html"))
                self.assertIn("Text and data on this site are published under", self.read("about.html"))
                for kind in SCHEMAS:
                    doc = json.loads(self.read(f"data/{kind}.json"))
                    self.assertEqual((doc["license"], doc["license_url"]), (license_id, url), kind)
                    law.validate(doc, SCHEMAS[kind])

    def test_cc_by_choice_never_changes_the_camera_file_licence(self):
        for license_id in LICENSES:
            self.build({"id": license_id})
            cameras = json.loads(self.read("data/cameras.geojson"))
            self.assertEqual((cameras["license"], cameras["license_url"], cameras["attribution"]),
                             (ODBL["id"], ODBL["url"], OSM), license_id)
            self.assertEqual(cameras["attribution_url"], ODBL["attribution_url"])
            self.assertNotIn("creativecommons", self.read("data/cameras.geojson"))
        self.assertIn(f'<a href="{ODBL["url"]}" rel="license">ODbL</a>', self.read("about.html"))
        self.assertIn("Camera points: \u00a9 <a href=\"https://www.openstreetmap.org/copyright\"", self.read("about.html"))

    def test_camera_file_keeps_the_points_and_replaces_a_licence_it_arrived_with(self):
        source = self.content / "map" / "cameras.geojson"
        original = json.loads(source.read_text(encoding="utf-8"))
        source.write_text(json.dumps({**original, "license": "CC0-1.0", "attribution": "someone else"}), encoding="utf-8")
        self.build({"id": "CC0-1.0"})
        cameras = json.loads(self.read("data/cameras.geojson"))
        self.assertEqual((cameras["license"], cameras["attribution"]), (ODBL["id"], OSM))
        for key in ("license", "license_url", "attribution", "attribution_url"):
            cameras.pop(key)
            original.pop(key, None)
        self.assertTrue(cameras == original, "the points must be unchanged")

    def test_without_a_licence_only_the_camera_file_changes(self):
        self.build(present=False)
        pages = {str(p.relative_to(self.public)): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        for name, data in pages.items():
            if name.endswith(".html"):
                text = data.decode("utf-8")
                self.assertNotIn('rel="license"', text, name)
                self.assertNotIn("Data and text:", text, name)
                self.assertNotIn("(license:", text, name)
                self.assertNotIn("Camera points:", text, name)
        for kind in SCHEMAS:
            doc = json.loads(pages[f"data/{kind}.json"])
            self.assertNotIn("license", doc, kind)
            self.assertNotIn("license_url", doc, kind)
        boundary = self.content / "map" / "city-boundaries.geojson"
        self.assertEqual(pages["data/city-boundaries.geojson"], boundary.read_bytes())
        cameras = json.loads(pages["data/cameras.geojson"])  # the one intended change: ODbL members, whatever the site says
        self.assertEqual((cameras["license"], cameras["license_url"], cameras["attribution"]), (ODBL["id"], ODBL["url"], OSM))

    def test_a_licence_adds_only_the_licence_text_to_the_pages_and_nothing_to_the_csv(self):
        self.build(present=False)
        plain = {str(p.relative_to(self.public)): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.build({"id": "CC-BY-SA-4.0"})
        chosen = {str(p.relative_to(self.public)): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.assertEqual(sorted(plain), sorted(chosen))
        for name in ("data/findings.csv", "data/sources.csv", "data/cameras.geojson", "data/city-boundaries.geojson",
                     "feed.xml", "style.css", "app.js", "site-data.js"):
            self.assertEqual(plain[name], chosen[name], name)
        for name in ("data/findings.json", "data/sources.json", "data/agencies.json", "data/meetings.json"):
            before, after = json.loads(plain[name]), json.loads(chosen[name])
            self.assertEqual({k: v for k, v in after.items() if k not in ("license", "license_url")}, before, name)

    def test_unknown_or_malformed_licence_is_refused_by_field_name(self):
        for value in ({"id": "MIT"}, {"id": "cc-by-4.0"}, {"id": "CC-BY-4.0 "}, {"id": 4}, {"id": None}, {}):
            with self.subTest(value):
                self.assertIn("site.json.license", self.refused(value))
        self.assertIn("site.json.license.id", self.refused({"id": "MIT"}))
        self.assertIn("site.json.license: unknown field(s) ['url']", self.refused({"id": "CC0-1.0", "url": "https://x.invalid/"}))
        for value in ("CC0-1.0", ["CC0-1.0"], None):
            self.assertIn("site.json.license: expected an object", self.refused(value))

    def test_attribution_is_plain_one_line_text_of_at_most_200_characters(self):
        for text in ("<b>Credit</b>", "mail credit@example.invalid", "call 555 123 4567", "x" * 201, "", "   ", "two\nlines", 7, ["x"]):
            with self.subTest(text):
                self.assertIn("site.json.license.attribution", self.refused({"id": "CC-BY-4.0", "attribution": text}))
        edge = ("R&D \"x\" 'y' " * 20)[:200]
        self.assertEqual(len(edge), 200)
        self.build({"id": "CC-BY-4.0", "attribution": edge})
        self.assertIn(f", {escape(edge)}</p>", self.read("about.html"))
        self.assertIn(f"</a>, {escape(edge)}</p>", self.read("index.html"))
        self.assertNotIn(f", {edge}</p>", self.read("index.html"))

    def test_csv_links_carry_the_licence_name_and_the_csv_bytes_do_not_change(self):
        self.build(present=False)
        before = {name: (self.public / "data" / name).read_bytes() for name in ("findings.csv", "sources.csv")}
        name = LICENSES["CC0-1.0"][0]
        self.build({"id": "CC0-1.0"})
        for page, target in (("findings/index.html", "findings"), ("sources.html", "sources"), ("about.html", "findings"),
                             ("about.html", "sources")):
            self.assertIn(f'<a href="/data/{target}.csv" type="text/csv">Spreadsheet (CSV) (license: {escape(name)})</a>',
                          self.read(page), (page, target))
        for csv_name, data in before.items():
            self.assertEqual((self.public / "data" / csv_name).read_bytes(), data, csv_name)

    def test_labels_reword_the_licence_lines(self):
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "footer.license": "Reuse terms:", "license.csv_note": "[{name}]"}}), encoding="utf-8")
        self.build({"id": "CC0-1.0"})
        self.assertIn("<p>Reuse terms: <a href=", self.read("index.html"))
        self.assertIn(f"Spreadsheet (CSV) [{escape(LICENSES['CC0-1.0'][0])}]</a>", self.read("about.html"))

    def test_preview_matches_what_a_build_writes(self):
        self.build({"id": "CC-BY-4.0"})
        report = preview_site(self.root)
        self.assertEqual((report["added"], report["removed"], report["changed"]), ([], [], []))


if __name__ == "__main__":
    unittest.main()
