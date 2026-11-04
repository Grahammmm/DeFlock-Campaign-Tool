"""DF1207-15: /search.html and the first-party search.js (built from a temp copy of the fictional example; no network)."""
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from campaign_tool import content as content_module
from campaign_tool.content import ContentError
from campaign_tool.labels import DEFAULTS, JS_DEFAULTS
from campaign_tool.site import GENERATED_TOP_LEVEL, SEARCH_JS, build_site, search_js

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
# main's CSP for the example (tests/test_site_build.py EXPECTED_CSP): the search page must not change it.
EXPECTED_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data: blob: https://tiles.example.invalid; "
                "connect-src 'self' https://tiles.example.invalid; font-src 'self'; worker-src blob:; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SEARCH_KEYS = sorted(key for key in JS_DEFAULTS if key.startswith("js.search_"))


class SiteSearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-search-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"

    def build(self):
        return {p.relative_to(self.public).as_posix() for p in build_site(self.root)}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def labels(self, labels):
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")

    def set_base(self, base):
        path = self.root / "content" / "site.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["base_url"] = base
        path.write_text(json.dumps(document), encoding="utf-8")

    # 1. the page
    def test_page_has_form_results_count_and_a_visible_fallback_linking_both_indexes(self):
        self.assertTrue({"search.html", "search.js"} <= self.build())
        html = self.read("search.html")
        form = re.search(r'<form class="site-search" id="search-form" role="search" data-agencies="([^"]*)" hidden>(.*?)</form>', html, re.S)
        self.assertIsNotNone(form)
        self.assertRegex(form.group(2), r'<label for="q">[^<]+</label> <input type="search" id="q"')
        self.assertIn('<ol class="search-results" id="search-results" aria-live="polite"></ol>', html)
        self.assertRegex(html, r'<p id="search-count" aria-live="polite" hidden></p>')
        fallback = re.search(r'<p id="search-fallback"([^>]*)>(.*?)</p>', html)
        self.assertEqual(fallback.group(1), "")  # visible until the script hides it
        self.assertIn('<a href="/findings/">Findings</a>', fallback.group(2))
        self.assertIn('<a href="/sources.html">Sources</a>', fallback.group(2))
        self.assertEqual(re.findall(r"<script\b[^>]*></script>", html), ['<script defer src="/search.js"></script>'])
        self.assertNotRegex(html, r'<script\b(?![^>]*\bsrc=)')
        self.assertNotIn("noindex", html)  # indexed like the other pages

    def test_agency_map_carries_only_published_names_and_source_anchors(self):
        self.build()
        data = json.loads(re.search(r'data-agencies="([^"]*)"', self.read("search.html")).group(1).replace("&quot;", '"').replace("&amp;", "&"))
        self.assertEqual(data["ca-cedar-police"][0], "Cedar Police Department (fictional)")
        sources = self.read("sources.html")
        for _, fragment in data.values():
            if fragment:
                self.assertIn(f'id="{fragment}"', sources)
        published = self.read("agencies.html")
        for name, _ in data.values():
            self.assertIn(name, published)

    # 2. the script
    def test_script_source_never_builds_html_and_loads_nothing_third_party(self):
        self.build()
        script = self.read("search.js")
        self.assertEqual(script, search_js())
        for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval", "document.write", "importScripts", "XMLHttpRequest"):
            self.assertNotIn(word, script)
        self.assertNotRegex(script, r"https?://[A-Za-z0-9]")  # no host named anywhere
        self.assertEqual(re.findall(r'load\("(/[^"]*)"', script), ["/data/findings.json", "/data/sources.json"])
        for modern in ("=>", "let ", "const ", "`", "async "):
            self.assertNotIn(modern, script)  # ES5, like findings/filter.js

    def test_script_text_is_the_label_defaults_not_hard_coded(self):
        script = search_js()
        self.assertNotIn("__LABELS__", script)
        self.assertIn("__LABELS__", SEARCH_JS)
        for key in SEARCH_KEYS:
            self.assertIn(json.dumps(key), script)
            self.assertIn(json.dumps(JS_DEFAULTS[key], ensure_ascii=True, separators=(",", ":"))[1:-1], script)

    # 3. reserved names, CSP and headers
    def test_slug_search_is_reserved_and_the_names_are_generated_top_level(self):
        self.assertIn("search", content_module.RESERVED_PAGE_SLUGS)
        self.assertTrue({"search.html", "search.js"} <= GENERATED_TOP_LEVEL)
        pages = self.root / "content" / "pages"
        pages.mkdir()
        (pages / "search.json").write_text(json.dumps({"title": "Mine", "body_md": "Text."}), encoding="utf-8")
        with self.assertRaisesRegex(ContentError, r"^pages/search\.json: slug search is reserved$"):
            build_site(self.root)
        self.assertFalse(self.public.exists())

    def test_csp_and_headers_are_unchanged_by_the_search_page(self):
        self.build()
        self.assertIn(f"Content-Security-Policy: {EXPECTED_CSP}\n", self.read("_headers"))
        self.assertTrue(self.read("_headers").endswith(f"Content-Security-Policy: {EXPECTED_CSP}\n"))
        self.assertNotIn("script-src 'self' 'unsafe", self.read("_headers"))

    # 4. footer and sitemap
    def test_footer_link_on_every_page_and_sitemap_only_with_base_url(self):
        self.build()
        pages = list(self.public.rglob("*.html"))
        self.assertGreater(len(pages), 10)
        for page in pages:
            self.assertIn('<a href="/feed.xml">Findings feed</a> · <a href="/search.html">Search</a>', page.read_text(encoding="utf-8"), page.name)
        self.assertIn(f"<loc>{BASE}/search.html</loc>", self.read("sitemap.xml"))
        self.set_base(None)
        shutil.rmtree(self.public)
        self.build()
        self.assertFalse((self.public / "sitemap.xml").exists())
        self.assertIn('<a href="/search.html">Search</a>', self.read("index.html"))

    # 5. labels through site-data.js
    def test_labels_are_defaults_with_documented_keys_and_reach_the_script_through_site_data(self):
        for key in ("footer.search", "search.title", "search.description", "search.label", "search.intro", "search.fallback", *SEARCH_KEYS):
            self.assertIn(key, DEFAULTS)
            self.assertIn(f"`{key}`", (REPO_ROOT / "docs" / "SITE-CONTENT.md").read_text(encoding="utf-8"))
        self.build()
        plain_script, plain_html = self.read("search.js"), self.read("search.html")
        self.assertNotIn("/site-data.js", plain_html)
        self.assertNotIn("LABELS=", self.read("site-data.js"))
        self.labels({"js.search_hint": "Escriba de 2 a 100 caracteres.", "footer.search": "Buscar"})
        shutil.rmtree(self.public)
        self.build()
        self.assertEqual(self.read("search.js"), plain_script)  # one script; LABELS overrides at run time
        html = self.read("search.html")
        self.assertIn('<script defer src="/site-data.js"></script>\n  <script defer src="/search.js"></script>', html)
        labels = json.loads(re.search(r"^var LABELS=(.*);$", self.read("site-data.js"), re.M).group(1))
        self.assertEqual(labels["js.search_hint"], "Escriba de 2 a 100 caracteres.")
        self.assertEqual(labels["js.search_none"], JS_DEFAULTS["js.search_none"])
        self.assertIn('<a href="/search.html">Buscar</a>', html)
        self.assertEqual(self.read("_headers").count("Content-Security-Policy"), 1)

    def test_a_label_placeholder_the_default_lacks_is_refused(self):
        self.labels({"js.search_count_many": "{total} resultados"})
        with self.assertRaises(ContentError):
            build_site(self.root)

    # 6. no published findings
    def test_zero_findings_still_builds_and_searches_sources(self):
        (self.root / "content" / "findings" / "cedar-policy-posted.json").unlink()
        self.build()
        self.assertEqual(json.loads(self.read("data/findings.json"))["findings"], [])
        self.assertTrue(json.loads(self.read("data/sources.json"))["sources"])
        self.assertIn('id="search-form"', self.read("search.html"))
        self.assertIn("/search.html", self.read("sitemap.xml"))


if __name__ == "__main__":
    unittest.main()
