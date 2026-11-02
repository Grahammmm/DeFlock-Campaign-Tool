"""RF2030-2: sitemap.xml only with absolute URLs (no base_url: no sitemap, no footer link); finding lastmod.

Synthetic content only (a temp copy of the fictional example); no network.
"""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from campaign_tool import cli
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
S = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
FIXED = ["/agencies.html", "/corrections.html", "/sources.html", "/meetings.html", "/about.html"]
NOTE = "sitemap.xml skipped: content/site.json has no base_url"


class SitemapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sitemap-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.findings = self.root / "content" / "findings"
        self.public = self.root / "public"

    def edit_json(self, path, **changes):
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")

    def finding(self, slug="cedar-policy-posted", **changes):
        path = self.findings / f"{slug}.json"
        if not path.exists():  # a second synthetic finding copied from the example
            shutil.copyfile(self.findings / "cedar-policy-posted.json", path)
            changes = dict(id="fnd_" + slug.replace("-", "_"), slug=slug, title=f"Synthetic {slug}", **changes)
        self.edit_json(path, **changes)

    def base_url(self, value):
        path = self.root / "content" / "site.json"
        self.edit_json(path, base_url=value)
        if value == "absent":  # the key left out entirely reads as unset too
            data = json.loads(path.read_text(encoding="utf-8"))
            del data["base_url"]
            path.write_text(json.dumps(data), encoding="utf-8")

    def build(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            cli.build(self.root)
        return err.getvalue()

    def urls(self):
        root = ET.fromstring((self.public / "sitemap.xml").read_text(encoding="utf-8"))
        self.assertEqual(root.tag, S + "urlset")
        return {u.findtext(S + "loc"): u.findtext(S + "lastmod") for u in root.findall(S + "url")}

    def all_text(self):
        return {p.relative_to(self.public).as_posix(): p.read_text(encoding="utf-8", errors="replace")
                for p in self.public.rglob("*") if p.is_file()}

    # 1. two findings: absolute locs; each finding its own day; / and /findings/ the newer; others none
    def test_two_findings_absolute_locs_and_lastmod(self):
        self.finding(corrections=[], updated_at="2026-02-05T09:30:00+00:00")
        self.finding("second-finding", corrections=[], updated_at="2026-03-07T10:00:00+00:00")
        self.assertEqual(self.build(), "")
        urls = self.urls()
        self.assertTrue(all(loc.startswith(BASE + "/") for loc in urls), urls)
        self.assertEqual(urls[BASE + "/findings/cedar-policy-posted.html"], "2026-02-05")
        self.assertEqual(urls[BASE + "/findings/second-finding.html"], "2026-03-07")
        self.assertEqual((urls[BASE + "/"], urls[BASE + "/findings/"]), ("2026-03-07", "2026-03-07"))
        for path in FIXED:
            self.assertIsNone(urls[BASE + path], path)
        self.assertEqual(len(urls), len(FIXED) + 4)
        self.assertIn('<a href="/sitemap.xml">Sitemap</a>', self.all_text()["index.html"])
        self.assertIn(f"Sitemap: {BASE}/sitemap.xml", self.all_text()["robots.txt"])

    # 2. a correction newer than updated_at wins; out-of-order corrections; an updated_at newer than them wins
    def test_correction_date_drives_lastmod(self):
        cases = [(["2026-02-10"], "2026-02-05T09:30:00+00:00", "2026-02-10"),
                 (["2026-02-20", "2026-02-12"], None, "2026-02-20"),
                 (["2026-02-03"], "2026-02-09T09:30:00+00:00", "2026-02-09")]
        for dates, updated, expected in cases:
            with self.subTest(dates=dates, updated=updated):
                self.finding(corrections=[{"date": d, "note": "Synthetic correction."} for d in dates], updated_at=updated)
                self.build()
                urls = self.urls()
                self.assertEqual(urls[BASE + "/findings/cedar-policy-posted.html"], expected)
                self.assertEqual(urls[BASE + "/"], expected)

    # 3. no findings: no lastmod anywhere, still a valid urlset
    def test_no_findings_no_lastmod(self):
        (self.findings / "cedar-policy-posted.json").unlink()
        self.build()
        text = (self.public / "sitemap.xml").read_text(encoding="utf-8")
        self.assertNotIn("lastmod", text)
        self.assertEqual(sorted(self.urls()), sorted(BASE + p for p in ["/", "/findings/"] + FIXED))

    # 4. no base_url (null or key absent): no sitemap, no link anywhere, no robots line, one note; a rebuild with it writes it
    def test_no_base_url_no_sitemap_then_rebuild_with_one(self):
        for value in (None, "absent"):
            with self.subTest(base_url=value):
                self.base_url(value)
                err = self.build()
                self.assertEqual(err.count(NOTE), 1, err)
                self.assertFalse((self.public / "sitemap.xml").exists())
                files = self.all_text()
                self.assertTrue(files["index.html"])
                for name, text in files.items():
                    self.assertNotIn("sitemap.xml", text, name)
                self.assertNotIn("Sitemap:", files["robots.txt"])
                self.assertIn('<a href="/corrections.html">Corrections</a></p>', files["index.html"])
        self.base_url(BASE)
        self.assertEqual(self.build(), "")
        self.assertIn(BASE + "/", self.urls())
        self.assertIn('<a href="/sitemap.xml">Sitemap</a>', self.all_text()["about.html"])

    # 5. a public/ that had a sitemap, rebuilt without base_url: the old file is gone, nothing else lost
    def test_stale_sitemap_removed_by_rebuild(self):
        self.build()
        before = set(self.all_text())
        self.assertIn("sitemap.xml", before)
        self.base_url(None)
        self.build()
        self.assertEqual(set(self.all_text()), before - {"sitemap.xml"})

    # 6. a trailing slash on base_url: no double slash in any loc
    def test_trailing_slash_base_url(self):
        self.base_url(BASE + "/")
        self.build()
        locs = list(self.urls())
        self.assertIn(BASE + "/", locs)
        for loc in locs:
            self.assertNotIn("//", loc[len("https://"):], loc)

    # 7. a zone-offset publication late in the local evening: lastmod is the UTC day (2026-03-08, not 03-07)
    def test_zone_offset_uses_utc_day(self):
        self.finding("late-evening", corrections=[], updated_at=None, published_at="2026-03-07T22:30:00-08:00")
        self.build()
        urls = self.urls()
        self.assertEqual(urls[BASE + "/findings/late-evening.html"], "2026-03-08")
        self.assertEqual(urls[BASE + "/findings/"], "2026-03-08")

    # 8. starter mode (no content/): unchanged, no sitemap and no note
    def test_starter_mode_unchanged(self):
        shutil.rmtree(self.root / "content")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            written = build_site(self.root)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(sorted(p.name for p in written), ["_headers", "index.html", "style.css"])


if __name__ == "__main__":
    unittest.main()
