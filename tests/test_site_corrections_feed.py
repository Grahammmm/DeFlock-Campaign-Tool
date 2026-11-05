"""DF0237-09: corrections.xml, an Atom feed of corrections alone. Synthetic content only; no network."""
import json
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from campaign_tool.content import ContentError
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
DAY = date(2026, 10, 20)
NS = {"a": "http://www.w3.org/2005/Atom"}
BASE = "https://cedar-records.example.invalid"
FEED_LINK = '<link rel="alternate" type="application/atom+xml" title="Corrections feed" href="/corrections.xml">'
FOOTER = '<a href="/corrections.xml">Corrections feed</a>'


class CorrectionsFeedTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-corrections-feed-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.findings = self.root / "content" / "findings"
        self.base = json.loads((self.findings / "cedar-policy-posted.json").read_text(encoding="utf-8"))
        self.public = self.root / "public"

    def only(self, *findings):
        for path in self.findings.glob("*.json"):
            path.unlink()
        for slug, corrections, extra in findings:
            extra = dict({"published_at": "2020-01-01T00:00:00+00:00", "updated_at": None}, **extra)
            record = dict(self.base, id="fnd_" + slug.replace("-", "_"), slug=slug, title=f"Synthetic {slug} (fictional)",
                          corrections=[{"date": d, "note": n} for d, n in corrections], **extra)
            (self.findings / f"{slug}.json").write_text(json.dumps(record), encoding="utf-8")

    def build(self):
        return build_site(self.root, today=DAY)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def entries(self):
        feed = ET.fromstring(self.read("corrections.xml"))
        return feed, feed.findall("a:entry", NS)

    def tree(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}

    def test_entry_shape_order_and_stable_ids(self):
        self.only(("alpha", [("2026-03-01", "a0"), ("2026-05-01", "a1 & \"q\" é"), ("2026-03-01", "a0")], {}),
                  ("zeta", [("2026-05-01", "z0")], {}), ("beta", [], {}))
        self.build()
        feed, entries = self.entries()
        self.assertEqual([e.find("a:id", NS).text for e in entries], [
            "urn:deflock-correction:fnd_alpha:1", "urn:deflock-correction:fnd_zeta:0",
            "urn:deflock-correction:fnd_alpha:0", "urn:deflock-correction:fnd_alpha:2"])  # equal rows are not merged
        first = entries[0]
        self.assertEqual(first.find("a:title", NS).text, "Correction: Synthetic alpha (fictional)")
        self.assertEqual(first.find("a:summary", NS).text, "a1 & \"q\" é")  # escaped once, parsed back as text
        self.assertEqual(first.find("a:summary", NS).get("type"), "text")
        self.assertEqual(first.find("a:updated", NS).text, "2026-05-01T00:00:00+00:00")
        link = first.find("a:link", NS)
        self.assertEqual((link.get("rel"), link.get("href")), ("alternate", BASE + "/findings/alpha.html#corrections"))
        self.assertEqual(feed.find("a:updated", NS).text, "2026-05-01T00:00:00+00:00")
        self.assertEqual(feed.find("a:title", NS).text, "Cedar County Records Project" + " corrections")
        self.assertEqual(self.read("corrections.xml").count("<entry"), 4)

    def test_newest_fifty_across_findings_keeps_stored_index(self):
        for count, expected in ((49, 49), (50, 50), (51, 50), (75, 50)):
            with self.subTest(count):
                rows = [(f"2026-01-{1 + i % 28:02d}", f"n{i}") for i in range(count)]
                self.only(("alpha", rows, {}))
                self.build()
                ids = [e.find("a:id", NS).text for e in self.entries()[1]]
                self.assertEqual(len(ids), expected)
                self.assertEqual(len(set(ids)), expected)
                self.assertTrue(all(i.startswith("urn:deflock-correction:fnd_alpha:") for i in ids))
                if count > 50:  # an entry that dropped out of the window keeps its stored index for the ones that stay
                    self.assertIn(f"urn:deflock-correction:fnd_alpha:{max(range(count), key=lambda i: (rows[i][0], -i))}", ids)

    def test_dates_are_midnight_utc_and_never_a_clock(self):
        self.only(("alpha", [("2024-02-29", "leap")], {"published_at": "2024-02-01T18:00:00-08:00", "updated_at": "2026-09-09T10:00:00+00:00"}))
        self.build()
        feed, (entry,) = self.entries()
        self.assertEqual(entry.find("a:updated", NS).text, "2024-02-29T00:00:00+00:00")
        self.assertEqual(feed.find("a:updated", NS).text, "2024-02-29T00:00:00+00:00")
        for tag in ("published", "category", "content"):
            self.assertIsNone(entry.find(f"a:{tag}", NS))

    def test_no_base_url_uses_the_campaign_urn(self):
        site = json.loads((self.root / "content" / "site.json").read_text(encoding="utf-8"))
        site.pop("base_url", None)
        (self.root / "content" / "site.json").write_text(json.dumps(site), encoding="utf-8")
        self.only(("alpha", [("2026-03-01", "a")], {}))
        self.build()
        feed, (entry,) = self.entries()
        self.assertTrue(feed.find("a:id", NS).text.endswith(":corrections"))
        self.assertEqual(entry.find("a:link", NS).get("href"), "/findings/alpha.html#corrections")

    def test_discovery_links_only_where_a_correction_exists(self):
        self.only(("alpha", [("2026-03-01", "a")], {}), ("beta", [], {}))
        self.build()
        for page, expected in (("corrections.html", 1), ("findings/alpha.html", 1), ("findings/beta.html", 0), ("index.html", 0),
                               ("findings/index.html", 0), ("findings/packet-all.html", 0), ("404.html", 0), ("agencies.html", 0)):
            self.assertEqual(self.read(page).count(FEED_LINK), expected, page)
        for path in self.public.rglob("*.html"):
            self.assertEqual(path.read_text(encoding="utf-8").count(FOOTER), 1, path.name)
        self.assertNotIn("corrections.xml", self.read("sitemap.xml"))
        self.assertIn('<link rel="alternate" type="application/atom+xml" title="Findings" href="/feed.xml">', self.read("findings/alpha.html"))

    def test_corrected_finding_outside_the_window_still_advertises_the_feed(self):
        old = [("2025-01-01", "old")]
        self.only(("alpha", old, {}), ("zeta", [("2026-02-%02d" % (1 + i % 28), f"z{i}") for i in range(50)], {}))
        self.build()
        self.assertNotIn("fnd_alpha", self.read("corrections.xml"))
        self.assertEqual(self.read("findings/alpha.html").count(FEED_LINK), 1)

    def test_updates_never_enter_the_feed(self):
        later = [{"date": "2026-10-01", "note": "later update"}]
        self.only(("alpha", [("2026-03-01", "a")], {}))
        self.build()
        before = self.read("corrections.xml")
        self.only(("alpha", [("2026-03-01", "a")], {"updates": later}))
        self.build()
        self.assertEqual(self.read("corrections.xml"), before)  # a newer update changes nothing in the feed
        self.assertNotIn("later update", before)
        self.only(("alpha", [], {"updates": later}))
        self.build()
        self.assertFalse((self.public / "corrections.xml").exists())  # updates alone make no feed

    def test_no_correction_no_file_and_last_removal_restores_the_tree(self):
        self.only(("alpha", [], {}))
        baseline_files = self.build()
        self.assertNotIn("corrections.xml", {p.relative_to(self.public).as_posix() for p in baseline_files})
        none = self.tree()
        for name, data in none.items():
            self.assertNotIn(b"corrections.xml", data, name)
        self.only(("alpha", [("2026-03-01", "a")], {}))
        self.build()
        first = self.tree()
        self.build()
        self.assertEqual(self.tree(), first)  # two builds, identical bytes
        self.assertTrue(first["corrections.xml"].endswith(b"</feed>\n"))
        self.only(("alpha", [], {}))
        self.build()
        self.assertEqual(self.tree(), none)  # stale file and links are gone

    def test_asset_named_corrections_xml_is_refused_and_public_kept(self):
        self.only(("alpha", [("2026-03-01", "a")], {}))
        self.build()
        before = self.tree()
        assets = self.root / "content" / "public-assets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "corrections.xml").write_text("<x/>", encoding="utf-8")
        with self.assertRaises(ContentError):
            self.build()
        self.assertEqual(self.tree(), before)

    def test_invalid_correction_date_refuses_and_keeps_the_previous_tree(self):
        self.only(("alpha", [("2026-03-01", "a")], {}))
        self.build()
        before = self.tree()
        self.only(("alpha", [("2026-13-45", "a")], {}))
        with self.assertRaises(ContentError):
            self.build()
        self.assertEqual(self.tree(), before)


if __name__ == "__main__":
    unittest.main()
