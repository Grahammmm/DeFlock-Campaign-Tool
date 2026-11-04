"""DF1103-11: one linkable record page per library entry (``sources/<12-hex prefix>.html``).

Synthetic content only: temporary copies of the fictional example; no network, no private reads.
"""
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

from campaign_tool.cli import read_config
from campaign_tool.content import ContentError, load_content
from campaign_tool.provenance_text import EXPLAINER
from campaign_tool.site import Site, build_site
from campaign_tool.site_check import check_links

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
CAFE, BEEF = "cafe" * 16, "beef" * 16
GONE = "d00d" * 16  # cited by a finding, never catalogued
OTHER = "abc1" * 16  # catalogued, uncited, an agency without a page
SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
NOT_CITED = "Not cited by a published finding"
TODAY = date(2026, 3, 1)


def entry(sha, fragment, title="Synthetic record", agency="ca-cedar-police", **extra):
    return {"sha256": sha, "title": title, "agency_id": agency, "received_at": "2026-01-25T10:00:00+00:00",
            "public_url": None, "fragment": fragment, **extra}


class RecordPageCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-record-pages-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content, self.public = self.root / "content", self.root / "public"
        self.library = json.loads((self.content / "sources.json").read_text(encoding="utf-8"))
        self.base = json.loads((self.content / "findings" / "cedar-policy-posted.json").read_text(encoding="utf-8"))

    def write_library(self, document=None):
        (self.content / "sources.json").write_text(json.dumps(self.library if document is None else document),
                                                   encoding="utf-8")

    def add_finding(self, slug, title, published_at, shas):
        finding = dict(self.base, id="fnd_" + slug.replace("-", "_"), slug=slug, title=title, published_at=published_at,
                       corrections=[], sources=[{"sha256": sha, "locator": f"page:{i + 1}", "title": "Synthetic record"}
                                                for i, sha in enumerate(shas)])
        (self.content / "findings" / f"{slug}.json").write_text(json.dumps(finding), encoding="utf-8")

    def build(self, **kwargs):
        return build_site(self.root, today=TODAY, **kwargs)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def tree(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def site(self, mutate=None):
        model = copy.deepcopy(load_content(self.root, TODAY))
        if mutate:
            mutate(model)
        return Site(self.root, read_config(self.root), model, today=TODAY), model


class RecordPageTests(RecordPageCase):
    def test_one_page_per_entry_and_precise_links(self):
        self.library.append(entry(OTHER, "other-records", "Synthetic uncited record", agency="ca-nowhere-pd"))
        self.write_library()
        self.add_finding("cites-unlisted", "Cites an unlisted record", "2026-03-01T18:00:00+00:00", [GONE])
        self.build()
        pages = {p.name for p in (self.public / "sources").glob("*.html")}
        self.assertEqual(pages, {"cafecafecafe.html", "beefbeefbeef.html", "abc1abc1abc1.html"})  # no page for d00dd00dd00d
        self.assertEqual(check_links(self.public), [])  # every /sources/<prefix>.html href resolves
        finding = self.read("findings/cedar-policy-posted.html")
        self.assertIn('<a href="/sources/cafecafecafe.html">Record no. <code>cafecafecafe</code></a>', finding)
        unlisted = self.read("findings/cites-unlisted.html")  # uncatalogued: no link, no invented receipt date
        self.assertIn('<p class="record-no">Record no. <code>d00dd00dd00d</code></p>', unlisted)
        self.assertNotIn("/sources/d00dd00dd00d", unlisted)
        self.assertNotIn(", received ", unlisted)
        self.assertFalse((self.public / "sources" / "d00dd00dd00d.html").exists())
        library = self.read("sources.html")
        for prefix in pages:
            self.assertIn(f'<a href="/sources/{prefix}">Record details</a>', library)
        self.assertIn('<a href="/agencies/cedar.html">Cedar Police Department (fictional)</a>', self.read("sources/cafecafecafe.html"))
        unknown = self.read("sources/abc1abc1abc1.html")
        self.assertIn("ca-nowhere-pd", unknown)  # unknown agency: escaped id, no invented link
        self.assertNotIn('href="/agencies/ca-nowhere', unknown)
        for name in ("findings/packet-all.html", "agencies/cedar.html"):  # group anchors still serve the old links
            self.assertIn('href="/sources.html#cedar-police-records"', self.read(name))
        self.assertIn('id="cedar-police-records"', library)

    def test_fields_and_copy_verdicts(self):
        url = "https://example.invalid/fixtures/a?x=1&y=\"2\""
        match = {"matches": True, "checked_on": "2026-02-01"}
        cases = [("private", None, None, None, "Original held in the private archive"),
                 ("unchecked", url, None, 3, "Public copy</a>"),
                 ("matching", url, match, 0, "same file as this record number"),
                 ("redacted", url, {"matches": False, "checked_on": "2026-02-01", "reason": "redacted"}, 2, "differs (redacted copy)"),
                 ("converted", url, {"matches": False, "checked_on": "2026-02-01", "reason": "converted"}, None, "differs (converted copy)"),
                 ("unknown", url, {"matches": False, "checked_on": "2026-02-01", "reason": "unknown"}, 1, "differs (reason unknown)")]
        site, model = self.site()
        for name, public_url, check, pages, wording in cases:
            with self.subTest(name):
                source = dict(model["sources"][0], public_url=public_url, pages=pages, note="")
                if check:
                    source["public_copy"] = check
                else:
                    source.pop("public_copy", None)
                html = site.record_html(source)["html"]
                self.assertIn(wording, html)
                self.assertEqual(html.count("How to check a copy"), 1)  # on every record page, checked or not
                for command in ("<code>sha256sum FILE</code>", "<code>shasum -a 256 FILE</code>", "<code>Get-FileHash FILE</code>"):
                    self.assertEqual(html.count(command), 1, command)
                self.assertEqual(html.count(EXPLAINER), 1)
                self.assertIn('<time datetime="2026-01-20T16:30:00+00:00">', html)  # received date as a time tag
                self.assertNotRegex(html, r"[0-9a-fA-F]{40,}")
                self.assertNotIn(str(self.root), html)
                self.assertEqual("page(s)" in html, pages is not None)
                if pages is not None:
                    self.assertIn(f"{pages} page(s)", html)
                self.assertEqual("Public copy checked" in html, check is not None)
                if name == "private":
                    self.assertNotIn("Public copy</a>", html)
                else:
                    self.assertIn('href="https://example.invalid/fixtures/a?x=1&amp;y=&quot;2&quot;"', html)
        hostile = dict(model["sources"][0], title="<img src=x onerror=1>", note='"><script>alert(1)</script>',
                       agency_id="a<b>&")
        html = site.record_html(hostile)["html"]
        for raw in ("<img", "<script", "a<b>"):
            self.assertNotIn(raw, html)
        self.assertNotIn("&amp;lt;", html)  # escaped once, not twice
        self.assertIn("&lt;img src=x onerror=1&gt;", html)
        self.assertIn("a&lt;b&gt;&amp;", html)

    def test_published_citations_only_once(self):
        self.add_finding("two-locators", "Two locator finding", "2026-03-01T18:00:00+00:00", [CAFE, CAFE])
        self.add_finding("older-finding", "Older finding", "2026-02-10T18:00:00+00:00", [CAFE, BEEF])
        (self.root / "private" / "findings").mkdir(parents=True)
        (self.root / "private" / "findings" / "draft.json").write_text(
            json.dumps({"title": "PRIVATE-DRAFT-SENTINEL", "sources": [{"sha256": GONE}, {"sha256": CAFE}]}), encoding="utf-8")
        self.build()
        site, model = self.site()
        order = [f["slug"] for f in model["findings"] if CAFE in [s["sha256"] for s in f["sources"]]]
        self.assertEqual(sorted(order), ["cedar-policy-posted", "older-finding", "two-locators"])
        cited = re.findall(r'<a href="/findings/([^"]+)\.html">', self.read("sources/cafecafecafe.html").split("Cited by:")[1])
        self.assertEqual(cited, order)  # each finding once, current model order
        once = self.read("sources/beefbeefbeef.html")  # two locators or not, one finding is listed once
        self.assertEqual(re.findall(r'<a href="/findings/([^"/]+)\.html">', once), ["older-finding"])
        self.library.append(entry(GONE, "other-records", "Synthetic uncited record"))
        self.write_library()
        self.build()
        page = self.read(f"sources/{GONE[:12]}.html")
        self.assertIn(NOT_CITED, page)
        self.assertNotIn("Cited by:", page)
        for path in self.public.rglob("*.html"):
            self.assertNotIn("PRIVATE-DRAFT-SENTINEL", path.read_text(encoding="utf-8"))

    def test_prefix_collisions_refuse_without_writes(self):
        self.build()
        before = self.tree()
        variants = {"distinct hashes": [entry(CAFE[:12] + "0" * 52, "other-records")],
                    "identical hash in another fragment": [entry(CAFE, "other-records")]}
        for name, extra in variants.items():
            with self.subTest(name):
                self.write_library(self.library + extra)
                with self.assertRaises(ContentError) as raised:
                    self.build()
                message = str(raised.exception)
                self.assertIn("entries 0, 2 share record number cafecafecafe", message)
                self.assertNotIn(CAFE, message)
                self.assertNotIn("0" * 20, message)
                self.assertEqual(self.tree(), before)  # the existing public tree is byte-identical
                self.assertEqual([p.name for p in self.root.glob(".public.*")], [])
        shutil.rmtree(self.public)  # nothing built yet: a refused build leaves nothing, not even a partial sources/
        with self.assertRaises(ContentError):
            self.build()
        self.assertFalse(self.public.exists())
        self.write_library(self.library + [entry(CAFE, "cedar-police-records")])  # loader rule kept: duplicate in a fragment
        with self.assertRaisesRegex(ContentError, r"duplicate sha256 within fragment 'cedar-police-records'"):
            self.build()
        self.write_library(self.library + [entry(BEEF[:12] + "9" * 52, "other-records")])  # same prefix as entry 1
        with self.assertRaisesRegex(ContentError, "entries 1, 2 share record number beefbeefbeef"):
            self.build()

    def test_reserved_paths_and_dom_handoff(self):
        (self.content / "pages").mkdir()
        (self.content / "pages" / "sources.json").write_text(json.dumps({"title": "Sources", "body_md": "text"}), encoding="utf-8")
        with self.assertRaisesRegex(ContentError, "sources"):
            self.build()
        (self.content / "pages" / "sources.json").unlink()
        assets = self.content / "public-assets"
        (assets / "sources").mkdir(parents=True)
        (assets / "sources" / "logo.svg").write_text(SVG, encoding="utf-8")
        for library in (self.library, []):  # with entries and with an empty library
            with self.subTest(entries=len(library)):
                self.write_library(library)
                with self.assertRaisesRegex(ContentError, r"^public-assets/sources/logo\.svg: collides with a generated path$"):
                    self.build()
        shutil.rmtree(assets / "sources")
        (assets / "seal.svg").write_text(SVG, encoding="utf-8")
        self.write_library()
        self.build()
        self.assertTrue((self.public / "seal.svg").is_file())  # an unrelated clean asset is still allowed
        # DOM hooks: escaped agency, the stored year without time-zone conversion, cited flag, direct li only
        def mutate(model):
            model["sources"][1]["agency_id"] = 'a"b<'
            model["sources"][0]["received_at"] = "2025-12-31T23:30:00-08:00"
        site, model = self.site(mutate)
        html = site.sources_html()["html"]
        self.assertIn('<li class="source-entry" data-agency="ca-cedar-police" data-year="2025" data-cited="true" data-record="cafecafecafe">', html)
        self.assertIn('<li class="source-entry" data-agency="a&quot;b&lt;" data-year="2026" data-cited="false" data-record="beefbeefbeef">', html)
        self.assertEqual(html.count("data-record="), len(model["sources"]))
        tables = "".join(re.findall(r'<ul class="source-table">(.*?)</ul>', html))
        self.assertEqual(tables.count("<li"), tables.count('<li class="source-entry"'))  # hooks on the direct li only
        self.assertEqual(re.findall(r'<section class="agency-document-group" id="([^"]+)">', html), ["cedar-police-records"])

    def test_sitemap_metadata_and_rebuild(self):
        self.build()
        locs = [loc.text for loc in ET.fromstring(self.read("sitemap.xml")).iter(SM + "loc")]
        self.assertEqual(locs[-2:], [f"{BASE}/sources/cafecafecafe.html", f"{BASE}/sources/beefbeefbeef.html"])  # library order, last
        self.assertEqual(len([loc for loc in locs if "/sources/" in loc]), 2)
        for url in ET.fromstring(self.read("sitemap.xml")).iter(SM + "url"):
            if "/sources/" in url.find(SM + "loc").text:
                self.assertIsNone(url.find(SM + "lastmod"))  # no invented lastmod
        self.assertNotIn("/sources/", self.read("feed.xml"))
        self.assertIn(f'<link rel="canonical" href="{BASE}/sources/cafecafecafe.html">', self.read("sources/cafecafecafe.html"))
        self.assertNotIn("article:published_time", self.read("sources/cafecafecafe.html"))  # website metadata, not an article
        self.assertIn("<title>Synthetic Cedar Police ALPR policy | ", self.read("sources/cafecafecafe.html"))
        self.assertIn('content="Source record cafecafecafe"', self.read("sources/cafecafecafe.html"))
        self.assertIn('<a href="/sources.html" aria-current="page">Sources</a>', self.read("sources/cafecafecafe.html"))
        self.assertEqual(self.site()[0].page_paths()[-2:], ["/sources/cafecafecafe.html", "/sources/beefbeefbeef.html"])
        self.library.pop()  # removing an entry removes its page on the next staged rebuild
        self.write_library()
        self.build()
        self.assertFalse((self.public / "sources" / "beefbeefbeef.html").exists())
        self.assertTrue((self.public / "sources" / "cafecafecafe.html").is_file())
        site_json = json.loads((self.content / "site.json").read_text(encoding="utf-8"))
        site_json["base_url"] = None  # no base_url: pages still built, root-relative, no sitemap
        (self.content / "site.json").write_text(json.dumps(site_json), encoding="utf-8")
        self.build()
        self.assertFalse((self.public / "sitemap.xml").exists())
        self.assertNotIn("canonical", self.read("sources/cafecafecafe.html"))
        self.write_library([])  # an empty library writes no record pages
        self.build()
        self.assertFalse((self.public / "sources").exists())
        shutil.rmtree(self.content)  # the starter is unchanged
        self.build()
        self.assertFalse((self.public / "sources").exists())

    def test_privacy_and_legacy_links(self):
        for folder, sentinel in (("private", "PRIVATE-LEDGER-SENTINEL"), ("kit", "KIT-SENTINEL")):
            (self.root / folder).mkdir()
            (self.root / folder / "ledger.json").write_text(json.dumps({"path": f"/srv/{sentinel}/original.pdf"}), encoding="utf-8")
        self.library[0]["note"] = "Public note only."
        self.write_library()
        with mock.patch("socket.socket", side_effect=OSError("network is not allowed in this test")):
            self.build()
        first = self.tree()
        text = "".join(data.decode("utf-8") for name, data in first.items() if name.endswith((".html", ".xml", ".json", ".csv")))
        for private in ("SENTINEL", "original.pdf", str(self.root), "/srv/"):
            self.assertNotIn(private, text)
        self.assertIn("Public note only.", self.read("sources/cafecafecafe.html"))
        self.assertEqual(check_links(self.public), [])
        self.assertIn('href="/sources.html#cedar-police-records"', self.read("findings/packet-all.html"))
        self.assertIn('href="/sources.html#cedar-police-records">In the source library</a>', self.read("agencies/cedar.html"))
        shutil.rmtree(self.public)
        self.build()
        self.assertEqual(self.tree(), first)  # a fixed build date repeats byte for byte
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("broken link", result.stderr)


if __name__ == "__main__":
    unittest.main()
