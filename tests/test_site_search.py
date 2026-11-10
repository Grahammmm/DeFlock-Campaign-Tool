"""DF1207-15: /search.html and the first-party search.js (built from a temp copy of the fictional example; no network)."""
import hashlib
import json
import re
import shutil
import tempfile
import unittest
from html import escape as html_escape
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
# DF1109-09: the agencies-and-meetings search keys; the two-kind search never carries or shows them.
NEW_KEYS = {"js.search_agency", "js.search_meeting", "js.search_none_all"}
HTML_KEYS = ("search.label_all", "search.intro_all", "search.description_all", "search.fallback_more")
# search.js and search.html of main before DF1109-09 for the example without findings, sources and meetings (so no
# agency page): default, without base_url, with labels (js.search_hint, footer.search) and with an empty meetings/ folder.
LEGACY_JS = "0f6185d1995caa68bb3292cf15417996ee917bf5b3c48c81d05cbd057d0da1f6"  # pragma: allowlist secret -- public rendered-file SHA-256
LEGACY_HTML = {
    "base": "81c55ea7bafa7ad6719e1492587dd27267ce51a2d5a1630ab925cb7e14a35e25",  # pragma: allowlist secret -- public rendered-file SHA-256
    "nobase": "b289c1e275a060e6de9b47a618d5cc940b1dc84d1786d910c4340dbf312a7999",  # pragma: allowlist secret -- public rendered-file SHA-256
    "labels": "543c64b9f93779380a0a2c222d2bcea635ec13143633b36622f41cd0b729dd74",  # pragma: allowlist secret -- public rendered-file SHA-256
    "emptydir": "81c55ea7bafa7ad6719e1492587dd27267ce51a2d5a1630ab925cb7e14a35e25",  # pragma: allowlist secret -- public rendered-file SHA-256
}


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
        form = re.search(r'<form class="site-search" id="search-form" role="search" data-agencies="([^"]*)" data-search-extra="([^"]*)" hidden>(.*?)</form>', html, re.S)
        self.assertIsNotNone(form)
        self.assertRegex(form.group(3), r'<label for="q">Search findings, sources, agencies and meetings</label> <input type="search" id="q"')
        self.assertIn('<ol class="search-results" id="search-results" aria-live="polite"></ol>', html)
        self.assertRegex(html, r'<p id="search-count" aria-live="polite" hidden></p>')
        fallback = re.search(r'<p id="search-fallback"([^>]*)>(.*?)</p>', html)
        self.assertEqual(fallback.group(1), "")  # visible until the script hides it
        self.assertIn('<a href="/findings/">Findings</a>', fallback.group(2))
        self.assertIn('<a href="/sources.html">Sources</a>', fallback.group(2))
        # DF1109-09: the example has an agency page and a staged meeting, so both indexes are linked without JavaScript
        self.assertTrue(fallback.group(2).endswith(' Also browse: <a href="/agencies.html">Agencies</a> · <a href="/meetings.html">Meetings</a>.'))
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
        self.assertEqual(script, search_js(("agency", "meeting")))
        for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval", "document.write", "importScripts", "XMLHttpRequest"):
            self.assertNotIn(word, script)
        self.assertNotRegex(script, r"https?://[A-Za-z0-9]")  # no host named anywhere
        self.assertEqual(re.findall(r'load\("(/[^"]*)"', script), ["/data/findings.json", "/data/sources.json"])
        self.assertEqual(re.findall(r'opt\("(/[^"]*)"', script), ["/data/agencies.json", "/data/meetings.json"])
        for modern in ("=>", "let ", "const ", "`", "async "):
            self.assertNotIn(modern, script)  # ES5, like findings/filter.js

    def test_script_text_is_the_label_defaults_not_hard_coded(self):
        script, legacy = search_js(("agency", "meeting")), search_js()
        self.assertNotIn("__LABELS__", script)
        self.assertIn("__LABELS__", SEARCH_JS)
        for key in SEARCH_KEYS:
            self.assertIn(json.dumps(key), script)
            self.assertIn(json.dumps(JS_DEFAULTS[key], ensure_ascii=True, separators=(",", ":"))[1:-1], script)
            self.assertEqual(json.dumps(key) in legacy, key not in NEW_KEYS, key)  # the two-kind script keeps its labels

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
            self.assertIn('<a href="/feed.xml">Findings feed</a> · <a href="/meetings.xml">Meetings feed</a> · <a href="/search.html">Search</a>', page.read_text(encoding="utf-8"), page.name)
        self.assertIn(f"<loc>{BASE}/search.html</loc>", self.read("sitemap.xml"))
        self.set_base(None)
        shutil.rmtree(self.public)
        self.build()
        self.assertFalse((self.public / "sitemap.xml").exists())
        self.assertIn('<a href="/search.html">Search</a>', self.read("index.html"))

    # 5. labels through site-data.js
    def test_labels_are_defaults_with_documented_keys_and_reach_the_script_through_site_data(self):
        for key in ("footer.search", "search.title", "search.description", "search.label", "search.intro", "search.fallback",
                    *HTML_KEYS, *SEARCH_KEYS):
            self.assertIn(key, DEFAULTS)
            self.assertIn(f"`{key}`", (REPO_ROOT / "docs" / "SITE-CONTENT.md").read_text(encoding="utf-8"))
        self.build()
        plain_script, plain_html = self.read("search.js"), self.read("search.html")
        self.assertNotIn("/site-data.js", plain_html)
        labels = json.loads(re.search(r"^var LABELS=(.*);$", self.read("site-data.js"), re.M).group(1))
        self.assertEqual(labels, {"js.filter_count": JS_DEFAULTS["js.filter_count"]})  # source filter only; no search overrides
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

    # 7. DF1109-09: agencies and meetings
    def strip(self, findings=True, sources=True, meetings=True, empty_dir=False):
        content = self.root / "content"
        if findings:
            (content / "findings" / "cedar-policy-posted.json").unlink()
        if sources:
            (content / "sources.json").unlink()  # sources too: any cited or catalogued agency gets a page
        if meetings:
            for path in (content / "meetings").glob("*.json"):
                path.unlink()
            if not empty_dir:
                (content / "meetings").rmdir()

    def extra(self):
        found = re.search(r'data-search-extra="([^"]*)"', self.read("search.html"))
        return json.loads(found.group(1).replace("&quot;", '"').replace("&amp;", "&")) if found else None

    def test_search_without_agency_pages_or_meetings_is_byte_identical_to_main(self):
        for name, setup in (("base", {}), ("nobase", {"base": None}), ("emptydir", {"empty_dir": True}),
                            ("labels", {"labels": {"js.search_hint": "Escriba.", "footer.search": "Buscar"}})):
            with self.subTest(name):
                self.setUp()
                self.strip(empty_dir=setup.get("empty_dir", False))
                if "base" in setup:
                    self.set_base(setup["base"])
                if "labels" in setup:
                    self.labels(setup["labels"])
                self.build()
                self.assertEqual(hashlib.sha256((self.public / "search.js").read_bytes()).hexdigest(), LEGACY_JS)
                self.assertEqual(hashlib.sha256((self.public / "search.html").read_bytes()).hexdigest(), LEGACY_HTML[name])
                self.assertEqual(json.loads(self.read("data/agencies.json"))["agencies"], [])  # still built, empty
                self.assertEqual(json.loads(self.read("data/meetings.json"))["meetings"], [])
                self.assertNotIn("opt(", self.read("search.js"))

    def test_each_kind_alone_fetches_only_its_file_and_links_only_its_index(self):
        self.strip(findings=False, sources=False)  # agency page, no meeting
        self.build()
        script, html = self.read("search.js"), self.read("search.html")
        self.assertEqual(script, search_js(("agency",)))
        self.assertIn('load("/data/sources.json","sources"),opt("/data/agencies.json","agencies"),[]]', script)
        self.assertNotIn("/data/meetings.json", script)
        self.assertEqual(self.extra(), {"agencies": {"ca-cedar-police": ["/agencies.html#cedar", "City police"]}})
        self.assertIn('Also browse: <a href="/agencies.html">Agencies</a>.</p>', html)
        self.assertNotIn('<a href="/meetings.html">', html.split('id="search-fallback"')[1])
        self.setUp()
        self.strip(meetings=False)  # a staged meeting, no agency page
        self.build()
        script, html = self.read("search.js"), self.read("search.html")
        self.assertEqual(script, search_js(("meeting",)))
        self.assertIn('load("/data/sources.json","sources"),[],opt("/data/meetings.json","meetings")]', script)
        self.assertEqual(self.extra(), {"meetings": {"cedar-council-2026-10": "Item 7: consent calendar, technology renewals"}})
        self.assertIn('Also browse: <a href="/meetings.html">Meetings</a>.</p>', html)

    def test_agenda_map_holds_only_staged_meetings_agenda_items(self):
        meetings = self.root / "content" / "meetings"
        (meetings / "zq-cancelled.json").write_text(json.dumps({  # cancelled and without an agenda item: no map entry
            "id": "zq-cancelled", "body": "Zq board", "starts_at": "2026-09-01T18:00:00-07:00", "status": "cancelled"}),
            encoding="utf-8")
        self.build()
        extra = self.extra()
        self.assertEqual(extra["meetings"], {"cedar-council-2026-10": "Item 7: consent calendar, technology renewals"})
        exported = {m["id"] for m in json.loads(self.read("data/meetings.json"))["meetings"]}
        self.assertEqual(exported, {"cedar-council-2026-10", "zq-cancelled"})  # both searchable by body and date
        staged = json.loads((meetings / "2026-10-cedar-council.json").read_text(encoding="utf-8"))
        attribute = re.search(r'data-search-extra="([^"]*)"', self.read("search.html")).group(1)
        for key in ("location", "comment_kit_md"):
            if staged.get(key):
                self.assertNotIn(html_escape(staged[key])[:40], attribute, key)  # no other meeting field spills

    def test_extended_html_labels_override_one_by_one_and_keep_placeholders(self):
        self.labels({"search.label_all": "Buscar todo", "search.intro_all": "Busca todo.",
                     "search.description_all": "Busca hallazgos", "search.fallback_more": "Vea también: {links}.",
                     "nav.agencies": "Agencias", "js.search_agency": "Agencia"})
        self.build()
        html = self.read("search.html")
        for text in ('<label for="q">Buscar todo</label>', "<p>Busca todo.</p>", 'content="Busca hallazgos"',
                     'Vea también: <a href="/agencies.html">Agencias</a> · <a href="/meetings.html">Meetings</a>.</p>'):
            self.assertIn(text, html)
        self.assertIn('<script defer src="/site-data.js"></script>\n  <script defer src="/search.js"></script>', html)
        labels = json.loads(re.search(r"^var LABELS=(.*);$", self.read("site-data.js"), re.M).group(1))
        self.assertEqual((labels["js.search_agency"], labels["js.search_meeting"]), ("Agencia", "Meeting"))
        self.assertEqual(self.read("search.js"), search_js(("agency", "meeting")))  # the script reads LABELS at run time
        self.labels({"search.fallback_more": "Vea también: {enlaces}."})
        with self.assertRaises(ContentError):
            build_site(self.root)


if __name__ == "__main__":
    unittest.main()
