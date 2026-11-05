"""DF2237-07: content/pages/<slug>.json explainer pages (nav, sitemap, refusals) and the 404 label override.

Builds a temporary copy of the fictional example with synthetic pages; no network.
"""
import json
import os
import re
import shutil
import tempfile
import unittest
import zlib
from pathlib import Path

from campaign_tool.content import ContentError, load_pages
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FINDING_SLUG = "cedar-policy-posted"  # the example's one published finding
BASE = "https://cedar-records.example.invalid"


def _chunk(kind, body):
    return len(body).to_bytes(4, "big") + kind + body + zlib.crc32(kind + body).to_bytes(4, "big")


# A well-formed PNG with no metadata chunks, so the public-assets metadata gate accepts it (DF1822-12).
CLEAN_PNG = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", bytes(13)) + _chunk(b"IDAT", b"\x78\x9c\x01\x02")
             + _chunk(b"IEND", b""))


class ContentPagesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-pages-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.pages = self.root / "content" / "pages"

    def page(self, name, document=None, **fields):
        self.pages.mkdir(parents=True, exist_ok=True)
        if document is None:
            document = {"title": "Explainer", "body_md": "Some text.", **fields}
        path = self.pages / name
        path.write_text(json.dumps(document) if not isinstance(document, str) else document, encoding="utf-8")
        return path

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def nav(self, html):
        match = re.search(r'<nav class="site-nav"[^>]*>(.*?)</nav>', html)
        self.assertIsNotNone(match, "no site nav")
        return match.group(1) if match else ""

    def assert_refused(self, pattern):
        with self.assertRaisesRegex(ContentError, pattern):
            build_site(self.root)

    # 1. two pages, one in the nav, both in the sitemap; 404 never listed
    def test_two_pages_written_one_in_nav_on_every_page_both_in_sitemap(self):
        self.page("how-to-request.json", title="How to request records", description="Steps.", nav=True,
                  body_md="Ask the **clerk**.")
        self.page("terminology.json", title="Terminology", body_md="- ALPR: plate reader")
        written = {p.relative_to(self.public).as_posix() for p in build_site(self.root)}
        self.assertTrue({"how-to-request.html", "terminology.html", "404.html"} <= written)
        link = '<a href="/how-to-request.html">How to request records</a>'
        html_files = sorted(p for p in written if p.endswith(".html"))
        self.assertGreater(len(html_files), 8)
        for name in html_files:
            nav = self.nav(self.read(name))
            self.assertEqual(nav.count('href="/how-to-request.html"'), 1, name)
            self.assertNotIn("terminology.html", nav, name)  # nav: false (the default) stays out of the nav
            self.assertIn('About</a><a href="/how-to-request.html"', nav, name)  # appended after About
            if name != "how-to-request.html":
                self.assertIn(link, nav, name)
        own = self.read("how-to-request.html")
        self.assertIn('<a href="/how-to-request.html" aria-current="page">How to request records</a>', self.nav(own))
        self.assertEqual(self.nav(own).count("aria-current"), 1)
        self.assertIn("<h2>How to request records</h2><p>Ask the <strong>clerk</strong>.</p>", own)
        self.assertIn('<meta name="description" content="Steps.">', own)
        self.assertIn(f'<link rel="canonical" href="{BASE}/how-to-request.html">', own)
        terminology = self.read("terminology.html")
        self.assertIn('<meta name="description" content="Terminology">', terminology)  # description defaults to the title
        self.assertIn("<li>ALPR: plate reader</li>", terminology)
        self.assertNotIn("aria-current", self.nav(terminology))
        locs = re.findall(r"<loc>([^<]+)</loc>", self.read("sitemap.xml"))
        self.assertIn(f"{BASE}/how-to-request.html", locs)
        self.assertIn(f"{BASE}/terminology.html", locs)
        self.assertEqual([loc for loc in locs if "404" in loc], [])
        self.assertEqual(len(locs), len(set(locs)))

    def test_load_pages_sorted_by_slug_and_empty_without_folder(self):
        self.assertEqual(load_pages(self.root / "content", set()), [])
        self.page("zeta.json")
        self.page("alpha.json", description="d", nav=True)
        pages = load_pages(self.root / "content", set())
        self.assertEqual([p["slug"] for p in pages], ["alpha", "zeta"])
        self.assertEqual(pages[1], {"slug": "zeta", "title": "Explainer", "description": "", "body_md": "Some text.",
                                    "nav": False})

    # 2. reserved and finding slugs refused; the previous public/ stays byte-identical
    def test_reserved_and_finding_slugs_refused_and_public_unchanged(self):
        build_site(self.root)
        before = self.snapshot()
        for slug in ("about", "404", "feed", "meetings", "corrections", "sitemap", "style", "index", "supporters", "sharing", "press", FINDING_SLUG):
            with self.subTest(slug=slug):
                path = self.page(f"{slug}.json")
                self.assert_refused(rf"^pages/{slug}\.json: slug {slug} is reserved$")
                self.assertTrue(self.snapshot() == before, "public/ changed after a refused build")
                self.assertEqual(list(self.root.glob(".public.*")), [])
                path.unlink()
        build_site(self.root)  # the same tree without the refused file builds again
        self.assertTrue(self.snapshot() == before)

    # 3. nav cap, strict bool, unknown key, empty body, title length; every refusal names the file
    def test_fourth_nav_page_refused_three_allowed(self):
        for name in ("a.json", "b.json", "c.json"):
            self.page(name, nav=True)
        build_site(self.root)
        self.assertEqual(self.nav(self.read("index.html")).count('href="/a.html"'), 1)
        before = self.snapshot()
        self.page("d.json", nav=True)
        self.assert_refused(r"^pages: at most 3 pages may set nav: true$")
        self.assertTrue(self.snapshot() == before)
        self.page("d.json", nav=False)  # a fourth page outside the nav is fine
        build_site(self.root)
        self.assertTrue((self.public / "d.html").is_file())

    def test_invalid_page_documents_refused_with_file_named(self):
        cases = [
            ({"nav": "yes"}, r"^pages/x\.json\.nav: must be true or false$"),
            ({"nav": 1}, r"^pages/x\.json\.nav: must be true or false$"),
            ({"nav": None}, r"^pages/x\.json\.nav: must be true or false$"),
            ({"extra": 1}, r"^pages/x\.json: unknown field\(s\) \['extra'\]$"),
            ({"body_md": ""}, r"^pages/x\.json\.body_md: must be a non-empty string"),
            ({"body_md": "  \n"}, r"^pages/x\.json\.body_md: must be a non-empty string"),
            ({"title": " "}, r"^pages/x\.json\.title: must be a non-empty string"),
            ({"title": "T" * 121}, r"^pages/x\.json\.title: must be a non-empty string of at most 120 characters$"),
            ({"title": ["T"]}, r"^pages/x\.json\.title: "),
            ({"description": "D" * 301}, r"^pages/x\.json\.description: must be a string of at most 300 characters$"),
        ]
        for fields, pattern in cases:
            with self.subTest(fields=list(fields)):
                self.page("x.json", **fields)
                self.assert_refused(pattern)
                self.assertFalse(self.public.exists())
        self.page("x.json", {"body_md": "No title."})
        self.assert_refused(r"^pages/x\.json: missing field\(s\) \['title'\]$")
        self.page("x.json", "[1]")
        self.assert_refused(r"^pages/x\.json: expected an object$")
        self.page("x.json", "{not json")
        self.assert_refused(r"^x\.json: not valid UTF-8 JSON")
        self.page("x.json", title="T" * 120, description="D" * 300)  # at the limits: accepted
        build_site(self.root)
        self.assertIn("<h2>" + "T" * 120 + "</h2>", self.read("x.html"))

    # 4. file names: not a slug refused, other extensions ignored, symlinks and folders refused
    def test_file_names_symlinks_and_folders(self):
        for name in ("Bad Name.json", "Upper.json", "under_score.json", ".hidden.json"):
            with self.subTest(name=name):
                path = self.page(name)
                self.assert_refused(rf"^pages/{re.escape(name)} filename: must be a lowercase slug")
                path.unlink()
        self.page("x.JSON")
        self.page("notes.md", "# not a page")
        self.page("draft.json.bak")
        written = {p.relative_to(self.public).as_posix() for p in build_site(self.root)}
        self.assertEqual([n for n in written if n.lower().startswith(("x.", "notes", "draft"))], [])
        target = Path(self.root.parent / "outside.json")
        target.write_text(json.dumps({"title": "Linked", "body_md": "x"}), encoding="utf-8")
        os.symlink(target, self.pages / "linked.json")
        self.assert_refused(r"^pages/linked\.json: must be a regular file, not a symlink or folder$")
        (self.pages / "linked.json").unlink()
        (self.pages / "folder.json").mkdir()
        self.assert_refused(r"^pages/folder\.json: must be a regular file")
        (self.pages / "folder.json").rmdir()
        shutil.move(str(self.pages), str(self.root / "real-pages"))
        os.symlink(self.root / "real-pages", self.pages)
        self.assert_refused(r"^pages: the folder must not be a symlink$")

    def test_more_than_fifty_page_files_refused(self):
        for n in range(51):
            self.page(f"p{n:02d}.json")
        self.assert_refused(r"^pages: at most 50 page files$")
        (self.pages / "p50.json").unlink()
        build_site(self.root)
        self.assertTrue((self.public / "p49.html").is_file())

    # 5. HTML in body_md and title is escaped everywhere it appears
    def test_script_in_body_and_angle_bracket_in_title_escaped(self):
        self.page("rules.json", title='Rules <b>& "quotes"', description="<i>d</i>", nav=True,
                  body_md="<script>alert(1)</script>\n\n[x](javascript:alert(1))")
        build_site(self.root)
        page = self.read("rules.html")
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn('href="javascript', page)
        self.assertIn("<title>Rules &lt;b&gt;&amp; &quot;quotes&quot; | Cedar County Records Project</title>", page)
        self.assertIn('<meta name="description" content="&lt;i&gt;d&lt;/i&gt;">', page)
        for name in ("index.html", "rules.html", "404.html"):
            nav = self.nav(self.read(name))
            self.assertIn("Rules &lt;b&gt;&amp; &quot;quotes&quot;</a>", nav, name)
            self.assertNotIn("<b>", nav, name)

    # 6. no pages/ (or only ignored files) changes nothing that was generated before
    def test_no_pages_folder_is_byte_identical(self):
        build_site(self.root)
        without = self.snapshot()
        top = {n for n in without if n.endswith(".html") and "/" not in n}
        self.assertEqual(top, {"index.html", "agencies.html", "sources.html", "meetings.html", "about.html",
                               "corrections.html", "404.html", "cameras.html", "methods.html", "privacy.html", "search.html", "timeline.html", "requests.html"})
        self.page("README.md", "Pages go here.")
        build_site(self.root)
        self.assertTrue(self.snapshot() == without, "an ignored file in pages/ changed the output")
        self.page("hidden-from-nav.json")
        build_site(self.root)
        changed = sorted(n for n, data in self.snapshot().items() if without.get(n) != data)
        self.assertEqual(changed, ["hidden-from-nav.html", "sitemap.xml"])  # a nav: false page touches no other page

    # 7. public-assets entries named like a content page or 404.html collide
    def test_public_asset_colliding_with_a_page_is_refused(self):
        folder = self.root / "content" / "public-assets" / "terminology.html"
        folder.mkdir(parents=True)
        (folder / "logo.png").write_bytes(CLEAN_PNG)
        build_site(self.root)  # no terminology page yet: an ordinary asset folder
        self.assertTrue((self.public / "terminology.html" / "logo.png").is_file())
        before = self.snapshot()
        self.page("terminology.json")
        self.assert_refused(r"^public-assets/terminology\.html/logo\.png: collides with a generated path$")
        self.assertTrue(self.snapshot() == before)
        shutil.rmtree(folder)
        (self.root / "content" / "public-assets" / "404.html").mkdir()
        (self.root / "content" / "public-assets" / "404.html" / "logo.png").write_bytes(CLEAN_PNG)
        self.assert_refused(r"^public-assets/404\.html/logo\.png: collides with a generated path$")

    # 8. labels.json overriding title.not_found is used by 404.html
    def test_not_found_title_label_override(self):
        (self.root / "content" / "labels.json").write_text(json.dumps(
            {"schema_version": 1, "labels": {"title.not_found": "Seite nicht gefunden"}}), encoding="utf-8")
        self.page("guide.json", title="Guide", nav=True)
        build_site(self.root)
        page = self.read("404.html")
        self.assertIn("<title>Seite nicht gefunden | Cedar County Records Project</title>", page)
        self.assertIn('<a href="/guide.html">Guide</a>', self.nav(page))
        self.assertNotIn("aria-current", page)


if __name__ == "__main__":
    unittest.main()
