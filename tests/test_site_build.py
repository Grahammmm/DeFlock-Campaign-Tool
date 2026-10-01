"""End-to-end site build from the fictional example into a temporary directory.

Synthetic content only; no network. A passing build is a structural check,
not a publication approval.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from campaign_tool.content import ContentError, load_content
from campaign_tool.site import Site, build_site
from campaign_tool.cli import read_config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
PAGES = ["index.html", "agencies.html", "findings/index.html", "findings/cedar-policy-posted.html",
         "sources.html", "meetings.html", "about.html"]
FILES = PAGES + ["style.css", "_headers", "robots.txt", "sitemap.xml", "feed.xml",
                 "site-data.js", "agency-cards.js", "app.js", "data/cameras.geojson",
                 "data/city-boundaries.geojson"]
EXPECTED_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data: blob: https://tiles.example.invalid; "
                "connect-src 'self' https://tiles.example.invalid; font-src 'self'; worker-src blob:; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class SiteBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-site-build-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"

    def build(self):
        return build_site(self.root)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def test_all_pages_and_files_exist_and_nothing_else(self):
        written = {p.relative_to(self.public).as_posix() for p in self.build()}
        self.assertEqual(written, set(FILES))
        on_disk = {p.relative_to(self.public).as_posix() for p in self.public.rglob("*") if p.is_file()}
        self.assertEqual(on_disk, set(FILES))

    def test_no_inline_or_remote_scripts(self):
        self.build()
        for page in PAGES:
            html = self.read(page)
            for tag in re.findall(r"<script\b[^>]*>.*?</script>", html, flags=re.S):
                self.assertRegex(tag, r'^<script defer src="/(?:vendor/maplibre-gl|site-data|agency-cards|app)\.js"></script>$', page)
            self.assertEqual(html.count("<script"), html.count("</script>"), page)
            self.assertNotIn("<iframe", html)
            self.assertNotRegex(html, r'href="(?:javascript|data):', page)
            self.assertNotIn("<img", html)

    def test_csp_header_is_exact(self):
        self.build()
        headers = self.read("_headers")
        self.assertEqual(headers, "/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n"
                                  "  Permissions-Policy: camera=(), microphone=(), geolocation=()\n"
                                  "  Content-Security-Policy: " + EXPECTED_CSP + "\n")

    def test_csp_without_map_and_with_hosted_signup(self):
        shutil.rmtree(self.root / "content" / "map")
        site = json.loads((self.root / "content" / "site.json").read_text())
        site["signup"] = {"mode": "brevo_hosted", "form_html_allowlisted_url": "https://synthetic.sibforms.com/serve/x"}
        (self.root / "content" / "site.json").write_text(json.dumps(site))
        written = {p.relative_to(self.public).as_posix() for p in self.build()}
        self.assertNotIn("data/cameras.geojson", written)
        csp = re.search(r"Content-Security-Policy: (.*)\n", self.read("_headers")).group(1)
        self.assertEqual(csp, "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                              "connect-src 'self'; font-src 'self'; base-uri 'none'; "
                              "form-action https://synthetic.sibforms.com; frame-ancestors 'none'")
        index = self.read("index.html")
        self.assertIn('<form class="signup-form" method="post" action="https://synthetic.sibforms.com/serve/x">', index)
        self.assertNotIn('id="county-map"', index)
        self.assertIn('class="city-picker"', index)
        self.assertIn("function chooseCity", self.read("app.js"))
        self.assertNotIn("maplibregl", self.read("app.js"))

    def test_finding_page_contents(self):
        self.build()
        page = self.read("findings/cedar-policy-posted.html")
        for needle in ("Documented fact", "Verified against the record", 'datetime="2026-01-15"',
                       "sha256 cafecafecafe", "page:1-2", 'id="corrections"', "Corrected the policy date",
                       "This is not legal advice", 'href="/sources.html#cedar-police-records"'):
            self.assertIn(needle, page)
        self.assertNotIn("cafecafecafecafecafecafecafecafecafecafecafecafecafecafecafecafe", page)
        index = self.read("findings/index.html")
        self.assertIn('href="/findings/cedar-policy-posted.html"', index)

    def test_needs_attorney_review_finding_refuses_the_whole_build(self):
        path = self.root / "content" / "findings" / "cedar-policy-posted.json"
        finding = json.loads(path.read_text())
        finding["confidence"] = "needs_attorney_review"
        path.write_text(json.dumps(finding))
        with self.assertRaises(ContentError) as caught:
            self.build()
        self.assertIn("needs_attorney_review", str(caught.exception))
        self.assertFalse(self.public.exists())

    def test_missing_content_produces_unchanged_starter(self):
        shutil.rmtree(self.root / "content")
        written = {p.name for p in self.build()}
        self.assertEqual(written, {"index.html", "style.css", "_headers"})
        self.assertEqual({p.name for p in self.public.iterdir()}, {"index.html", "style.css", "_headers"})
        self.assertEqual(self.read("_headers"),
                         "/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n"
                         "  Content-Security-Policy: default-src 'none'; style-src 'self'; "
                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'\n")
        self.assertIn("This is a starter preview", self.read("index.html"))
        self.assertNotIn("<script", self.read("index.html"))

    def test_source_anchors_match_card_aliases_and_finding_links(self):
        self.build()
        model = load_content(self.root)
        config = Site(self.root, read_config(self.root), model).cards_config()
        sources = self.read("sources.html")
        self.assertIn('id="library"', sources)
        self.assertEqual(config["source_aliases"], {"cedar": "cedar-police-records"})
        for fragment in list(config["source_aliases"].values()) + [config["default_fragment"]]:
            self.assertIn(f'id="{fragment}"', sources)
        cards = self.read("agency-cards.js")
        self.assertIn('slug==="cedar"?"cedar-police-records"', cards)
        self.assertIn('"sources.html#"', cards)
        self.assertIn("sha256 cafecafecafe", sources)
        self.assertIn("sha256 beefbeefbeef", sources)

    def test_site_data_globals_and_controller_wiring(self):
        self.build()
        data = self.read("site-data.js")
        for name in ("var CITIES=", "var COUNTY_BOUNDS=[[0.0,0.0],[2.0,2.0]]",
                     'var MAP_STYLE="https://tiles.example.invalid/styles/fictional/style.json"',
                     'var CAMERA_DATA_URL="/data/cameras.geojson"', 'var BOUNDARY_DATA_URL="/data/county.geojson"'):
            self.assertIn(name, data)
        cities = json.loads(re.search(r"var CITIES=(\{.*\});", data).group(1))
        self.assertEqual(set(cities), {"cedar", "rural-area"})
        self.assertEqual(cities["cedar"]["bounds"], [[0.5, 0.5], [1.5, 1.5]])
        self.assertEqual(cities["cedar"]["name"], "Cedar")
        app = self.read("app.js")
        self.assertIn('fetch("data/city-boundaries.geojson")', app)
        self.assertIn("initMap();", app)
        self.assertIn('data-city="cedar"', self.read("index.html"))
        self.assertIn('id="map-selection-count"', self.read("index.html"))

    def test_city_without_bounds_is_omitted_from_picker_when_mapped(self):
        cities = self.root / "content" / "map" / "cities.json"
        cities.write_text(json.dumps({"cedar": {"bounds": [[0.5, 0.5], [1.5, 1.5]]}}))
        agencies_path = self.root / "content" / "agencies.json"
        agencies = json.loads(agencies_path.read_text())
        agencies.append({"agency_id": "ca-pine-police", "name": "Pine PD (fictional)", "kind": "police",
                         "jurisdiction_name": "Pine"})
        agencies_path.write_text(json.dumps(agencies))
        self.build()
        self.assertNotIn('data-city="pine-police"', self.read("index.html"))
        self.assertNotIn("pine-police", self.read("site-data.js"))
        self.assertIn("Pine PD (fictional)", self.read("agencies.html"))

    def test_vendor_maplibre_is_copied_only_when_present(self):
        vendor = self.root / "content" / "vendor"
        vendor.mkdir()
        (vendor / "maplibre-gl.js").write_text("/* synthetic stand-in, not the library */\n")
        (vendor / "maplibre-gl.css").write_text("/* synthetic */\n")
        written = {p.relative_to(self.public).as_posix() for p in self.build()}
        self.assertIn("vendor/maplibre-gl.js", written)
        index = self.read("index.html")
        self.assertIn('<script defer src="/vendor/maplibre-gl.js"></script>', index)
        self.assertIn('<link rel="stylesheet" href="/vendor/maplibre-gl.css">', index)
        self.assertLess(index.index("/vendor/maplibre-gl.js"), index.index("/app.js"))

    def test_feed_and_sitemap_are_well_formed(self):
        self.build()
        feed = ET.fromstring(self.read("feed.xml"))
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = feed.findall("a:entry", ns)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].find("a:id", ns).text, "urn:deflock-finding:fnd_0000000000000001")
        self.assertEqual(entries[0].find("a:link", ns).get("href"),
                         "https://cedar-records.example.invalid/findings/cedar-policy-posted.html")
        sitemap = ET.fromstring(self.read("sitemap.xml"))
        locs = [loc.text for loc in sitemap.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
        self.assertIn("https://cedar-records.example.invalid/findings/cedar-policy-posted.html", locs)
        self.assertIn("Sitemap: https://cedar-records.example.invalid/sitemap.xml", self.read("robots.txt"))

    def test_public_assets_allowlist_and_collisions(self):
        assets = self.root / "content" / "public-assets" / "img"
        assets.mkdir(parents=True)
        (assets / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        written = {p.relative_to(self.public).as_posix() for p in self.build()}
        self.assertIn("img/logo.svg", written)
        (assets / "notes.txt").write_text("x")
        with self.assertRaises(ContentError):
            self.build()
        (assets / "notes.txt").unlink()
        (self.root / "content" / "public-assets" / "index.html").write_text("x")
        with self.assertRaises(ContentError):
            self.build()

    def test_no_pilot_or_private_values_in_output(self):
        self.build()
        for path in self.public.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                for forbidden in ("San Luis Obispo", "deflockslo", "sibforms", "private/"):
                    self.assertNotIn(forbidden, text, path.name)

    def test_cli_build_prints_manifest_and_check_passes(self):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("index.html", result.stdout)
        self.assertIn("Public tree check passed", result.stdout)
        self.assertIn(f"{len(FILES)} files", result.stdout)

    def test_cli_check_fails_on_leaked_pattern(self):
        site = json.loads((self.root / "content" / "site.json").read_text())
        site["about_md"] = "Never publish paths like /workspace/" + "flockbot/work in copy."
        (self.root / "content" / "site.json").write_text(json.dumps(site))
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("pilot_host_path", result.stderr)
        self.assertIn("potential leak", result.stderr)


if __name__ == "__main__":
    unittest.main()
