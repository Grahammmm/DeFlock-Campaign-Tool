"""DF1737-05: /corrections.html lists every correction newest first; lists mark corrected findings.

Synthetic content only (temp copies of the fictional example); no network.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.cli import check_public_tree, read_config
from campaign_tool.content import ContentError, load_content
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BUILD_DAY = date(2026, 10, 20)
ROWS = re.compile(r'<li><time datetime="([^"]+)">\1</time> · <a href="/findings/([^"]+)\.html#corrections">([^<]*)</a>: ([^<]*)</li>')
PILL = re.compile(r'<span class="status-pill">Corrected <time datetime="([^"]+)">\1</time></span>')
FOOTER_LINKS = ('<p><a href="/feed.xml">Findings feed</a> · <a href="/corrections.html">Corrections</a> · '
                '<a href="/sitemap.xml">Sitemap</a></p>')


class SiteCorrectionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-site-corrections-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.findings = self.root / "content" / "findings"
        self.base = json.loads((self.findings / "cedar-policy-posted.json").read_text(encoding="utf-8"))
        self.public = self.root / "public"

    def finding(self, slug, corrections, title=None, published="2026-02-01T18:00:00+00:00"):
        record = dict(self.base, id="fnd_" + slug.replace("-", "_"), slug=slug, title=title or f"Synthetic {slug} (fictional)",
                      corrections=[{"date": d, "note": n} for d, n in corrections], published_at=published)
        (self.findings / f"{slug}.json").write_text(json.dumps(record), encoding="utf-8")

    def only(self, *findings):
        for path in self.findings.glob("*.json"):
            path.unlink()
        for args in findings:
            self.finding(*args)

    def build(self):
        return build_site(self.root, today=BUILD_DAY)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def rows(self):
        return [(d, slug, note) for d, slug, _, note in ROWS.findall(self.read("corrections.html"))]

    @staticmethod
    def pills(html, slug):
        start = html.index(f'<a href="/findings/{slug}.html">')
        item = html[html.rindex("<li>", 0, start):html.index("</li>", start)]
        return PILL.findall(item)

    def test_all_corrections_newest_first_with_links(self):
        self.only(("alpha", [("2026-03-01", "a1"), ("2026-05-01", "a2")]), ("beta", [("2026-04-01", "b1")]))
        self.build()
        self.assertEqual(self.rows(), [("2026-05-01", "alpha", "a2"), ("2026-04-01", "beta", "b1"),
                                       ("2026-03-01", "alpha", "a1")])
        page = self.read("corrections.html")
        self.assertIn("<h2>Corrections</h2>", page)
        self.assertIn("Every correction issued on a published finding, newest first.", page)
        self.assertNotIn("No corrections have been issued.", page)
        for slug in ("alpha", "beta"):  # every link target exists
            self.assertIn('<h3 id="corrections">Corrections</h3>', self.read(f"findings/{slug}.html"))

    def test_same_date_orders_by_slug_then_original_order(self):
        # zeta is published later, so it comes first in the model; the page still orders by slug.
        self.only(("zeta", [("2026-04-01", "z1")], None, "2026-03-01T00:00:00+00:00"),
                  ("alpha", [("2026-04-01", "a-second-listed-first"), ("2026-04-01", "a-later"),
                             ("2026-04-01", "a-later")]))
        self.build()
        self.assertEqual(self.rows(), [("2026-04-01", "alpha", "a-second-listed-first"), ("2026-04-01", "alpha", "a-later"),
                                       ("2026-04-01", "alpha", "a-later"), ("2026-04-01", "zeta", "z1")])

    def test_order_ignores_file_and_publication_order(self):
        self.only(("beta", [("2026-01-02", "b-old"), ("2026-06-30", "b-new")], None, "2026-01-01T00:00:00+00:00"),
                  ("alpha", [("2026-06-30", "a-new")], None, "2026-07-01T00:00:00+00:00"))
        self.build()
        self.assertEqual([r[2] for r in self.rows()], ["a-new", "b-new", "b-old"])

    def test_no_corrections_empty_state_and_no_pill(self):
        self.only(("alpha", []), ("beta", []))
        self.build()
        page = self.read("corrections.html")
        self.assertIn("<p>No corrections have been issued.</p>", page)
        self.assertEqual(ROWS.findall(page), [])
        for path in self.public.rglob("*.html"):
            self.assertNotIn('status-pill">Corrected', path.read_text(encoding="utf-8"), path.name)

    def test_markup_characters_are_escaped(self):
        self.only(("alpha", [("2026-04-01", 'Fixed "page 2" & page 3')], 'Policy "A" & B (fictional)'))
        self.build()
        page = self.read("corrections.html")
        self.assertIn("Policy &quot;A&quot; &amp; B (fictional)</a>: Fixed &quot;page 2&quot; &amp; page 3</li>", page)
        self.assertNotIn('"A" & B', page)

    def test_angle_brackets_refused_by_loader_and_escaped_by_renderer(self):
        self.only(("alpha", [("2026-04-01", "<script>x</script>")]))
        with self.assertRaises(ContentError):
            self.build()
        self.assertFalse(self.public.exists())  # nothing written
        self.only(("alpha", [("2026-04-01", "plain")]))
        model = load_content(self.root)
        assert model is not None
        model["findings"][0]["title"] = "<b>bold</b> & co"
        model["findings"][0]["corrections"][0]["note"] = '<img src=x onerror="y">'
        html = Site(self.root, read_config(self.root), model, today=BUILD_DAY).corrections_html()["html"]
        self.assertIn("&lt;b&gt;bold&lt;/b&gt; &amp; co</a>: &lt;img src=x onerror=&quot;y&quot;&gt;</li>", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("<b>", html)

    def test_lists_mark_corrected_findings_with_latest_date(self):
        self.only(("alpha", [("2026-03-01", "a1"), ("2026-05-01", "a2"), ("2026-04-01", "a3")]), ("beta", []))
        self.build()
        for page in ("index.html", "findings/index.html"):
            html = self.read(page)
            self.assertEqual(self.pills(html, "alpha"), ["2026-05-01"], page)
            self.assertEqual(self.pills(html, "beta"), [], page)
            self.assertEqual(len(PILL.findall(html)), 1, page)

    def test_footer_finding_pages_and_sitemap_link_the_page(self):
        self.only(("alpha", [("2026-03-01", "a1")]), ("beta", []))
        self.build()
        pages = sorted(self.public.rglob("*.html"))
        self.assertIn(self.public / "corrections.html", pages)
        for path in pages:
            html = path.read_text(encoding="utf-8")
            self.assertEqual(html.count(FOOTER_LINKS), 1, path.name)
            self.assertIn(FOOTER_LINKS + "</footer>", html, path.name)
        for slug in ("alpha", "beta"):
            self.assertIn('<p><a href="/corrections.html">All corrections</a></p>', self.read(f"findings/{slug}.html"))
        locs = re.findall(r"<loc>([^<]+)</loc>", self.read("sitemap.xml"))
        base = "https://cedar-records.example.invalid"
        self.assertIn(base + "/corrections.html", locs)
        self.assertEqual(locs.index(base + "/corrections.html"), locs.index(base + "/findings/") + 1)
        self.assertEqual(locs.count(base + "/corrections.html"), 1)

    def test_asset_named_corrections_html_is_refused(self):
        assets = self.root / "content" / "public-assets"
        (assets / "corrections.html").mkdir(parents=True)  # a top-level name an allowed asset can take
        (assets / "corrections.html" / "logo.png").write_bytes(b"\x89PNG")
        with self.assertRaisesRegex(ContentError, "public-assets/corrections.html/logo.png: collides with a generated path"):
            self.build()
        self.assertFalse(self.public.exists())  # nothing written
        (assets / "corrections.html" / "logo.png").unlink()
        (assets / "corrections.html").rmdir()
        (assets / "corrections.html").write_text("<p>impostor</p>", encoding="utf-8")  # a file of that name is refused too
        with self.assertRaisesRegex(ContentError, "public-assets/corrections.html: "):
            self.build()
        self.assertFalse(self.public.exists())
        (assets / "corrections.html").unlink()
        self.build()
        before = self.read("corrections.html")
        (assets / "corrections.html").mkdir()
        (assets / "corrections.html" / "logo.png").write_bytes(b"\x89PNG")
        with self.assertRaises(ContentError):
            self.build()
        self.assertEqual(self.read("corrections.html"), before)  # previous site untouched
        self.assertNotIn("impostor", before)

    def test_rebuild_is_byte_identical_and_check_passes(self):
        self.build()
        first = (self.public / "corrections.html").read_bytes()
        self.build()
        self.assertEqual((self.public / "corrections.html").read_bytes(), first)
        self.assertEqual(self.rows(), [("2026-02-03", "cedar-policy-posted",
                                        "Corrected the policy date from 2026-01-14 to 2026-01-15 after re-reading page 1.")])
        self.assertEqual(check_public_tree(self.public), [])
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Public tree check passed", result.stdout)
        self.assertIn("corrections.html", result.stdout)

    def test_starter_mode_writes_no_corrections_page(self):
        shutil.rmtree(self.root / "content")
        written = {p.name for p in self.build()}
        self.assertNotIn("corrections.html", written)
        self.assertFalse((self.public / "corrections.html").exists())
        self.assertNotIn("corrections.html", self.read("index.html"))


if __name__ == "__main__":
    unittest.main()
