"""RF1510-5: generated 404.html (site shell, corrections note, 4 links); noindex, no canonical/share tags.

Builds the fictional example into a temporary copy; synthetic content only, no network.
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

from campaign_tool.content import ContentError
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
LINKS = ['<li><a href="/findings/">Findings</a></li>', '<li><a href="/agencies.html">Agencies</a></li>',
         '<li><a href="/meetings.html">Meetings</a></li>', '<li><a href="/">Home page</a></li>']
TARGETS = {"/findings/": "findings/index.html", "/agencies.html": "agencies.html",
           "/meetings.html": "meetings.html", "/": "index.html", "/corrections.html": "corrections.html"}


class NotFoundPageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-not-found-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.site_json = self.root / "content" / "site.json"

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def drop_base_url(self):
        data = json.loads(self.site_json.read_text(encoding="utf-8"))
        del data["base_url"]
        self.site_json.write_text(json.dumps(data), encoding="utf-8")

    def assert_not_found_page(self):
        page = self.read("404.html")
        head = page.split("</head>")[0]
        self.assertEqual(head.count('<meta name="robots" content="noindex">'), 1)
        self.assertNotIn('rel="canonical"', page)
        self.assertIsNone(re.search(r'<meta (?:property|name)="(?:og|twitter|article):', page), "share tag on 404")
        self.assertIn("<title>Page not found | Cedar County Records Project</title>", head)
        self.assertIn('<nav class="site-nav" aria-label="Site">', page)
        self.assertIn('<footer class="site-footer">', page)
        self.assertIn("<h2>Page not found</h2>", page)
        self.assertIn('Findings are never silently removed: corrections and withdrawals are listed on '
                      '<a href="/corrections.html">Corrections</a>.', page)
        self.assertIn("<ul>" + "".join(LINKS) + "</ul>", page)
        self.assertNotIn("<script", page)
        return page

    def test_shell_noindex_and_no_share_tags_with_base_url(self):
        build_site(self.root)
        page = self.assert_not_found_page()
        self.assertNotIn("cedar-records.example.invalid", page)  # no absolute self URL anywhere

    def test_shell_noindex_and_no_share_tags_without_base_url(self):
        self.drop_base_url()
        build_site(self.root)
        self.assert_not_found_page()

    def test_no_nav_item_is_current_and_every_link_resolves(self):
        build_site(self.root)
        page = self.read("404.html")
        self.assertNotIn("aria-current", page)
        main = page.split("<main", 1)[1].split("</main>")[0]
        for href in re.findall(r'href="([^"]+)"', main):
            self.assertIn(href, TARGETS, href)  # root-relative only, no stray link
            self.assertTrue((self.public / TARGETS[href]).is_file(), href)
        self.assertIn('aria-current="page"', self.read("meetings.html"))  # the marker still works elsewhere

    def test_other_pages_byte_identical_to_a_build_without_the_404_page(self):
        for drop in (False, True):
            with self.subTest(base_url=not drop):
                if drop:
                    self.drop_base_url()
                build_site(self.root)
                with_404 = self.snapshot()
                stand_in = lambda site: site.about_html()  # noqa: E731 - skip not_found_html's own rendering
                with mock.patch.object(Site, "not_found_html", stand_in):
                    build_site(self.root)
                without = self.snapshot()
                self.assertEqual(set(with_404), set(without))
                for name in with_404:
                    if name != "404.html":
                        self.assertTrue(with_404[name] == without[name], name)
                self.assertNotEqual(with_404["404.html"], without["404.html"])

    def test_absent_from_sitemap_and_every_feed(self):
        build_site(self.root)
        feeds = ["feed.xml"] + [p.relative_to(self.public).as_posix()
                                for p in (self.public / "findings" / "agency").glob("*.xml")]
        self.assertGreater(len(feeds), 1)
        for name in ["sitemap.xml", "robots.txt", *feeds]:
            self.assertNotIn("404.html", self.read(name), name)
        self.assertNotIn("404.html", self.read("data/findings.json"))

    def test_public_asset_named_404_is_refused_and_nothing_written(self):
        assets = self.root / "content" / "public-assets"
        (assets / "404.html").mkdir(parents=True)
        (assets / "404.html" / "logo.png").write_bytes(b"\x89PNG")
        with self.assertRaisesRegex(ContentError, "public-assets/404.html/logo.png: collides with a generated path"):
            build_site(self.root)
        self.assertFalse(self.public.exists())
        self.assertEqual(list(self.root.glob(".public.*")), [])
        (assets / "404.html" / "logo.png").unlink()
        (assets / "404.html").rmdir()
        (assets / "404.html").write_text("<p>impostor</p>", encoding="utf-8")  # a plain file of that name too
        with self.assertRaisesRegex(ContentError, "public-assets/404.html: "):
            build_site(self.root)
        self.assertFalse(self.public.exists())

    def test_refused_asset_leaves_previous_404_untouched(self):
        build_site(self.root)
        before = self.snapshot()
        assets = self.root / "content" / "public-assets" / "404.html"
        assets.mkdir(parents=True)
        (assets / "logo.png").write_bytes(b"\x89PNG")
        with self.assertRaises(ContentError):
            build_site(self.root)
        self.assertTrue(self.snapshot() == before, "previous public/ changed")

    def test_build_check_passes_and_headers_unchanged(self):
        build_site(self.root)
        headers = self.read("_headers")
        with mock.patch.object(Site, "not_found_html", lambda site: site.about_html()):
            build_site(self.root)
        self.assertEqual(self.read("_headers"), headers)
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root),
                                 "--check"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Public tree check passed", result.stdout)
        self.assertRegex(result.stdout, r"\n  404\.html +\d+ bytes\n")
        self.assertEqual(self.read("_headers"), headers)


if __name__ == "__main__":
    unittest.main()
