"""Per-agency Atom feeds (DF2022-05), built from a temp copy of the fictional example.

Synthetic content only; no network.
"""
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
A = "{http://www.w3.org/2005/Atom}"
BASE = "https://cedar-records.example.invalid"
SUMMARY = ("[Documented fact; Verified against the record; event date 2026-01-15] In response to a synthetic records "
           "request, the fictional Cedar Police Department produced a two-page usage and privacy policy dated 2026-01-15.")
# feed.xml of the fictional build recorded from the base commit (before the _atom refactor).
BASE_FEED = [
    ("feed", {}, ""),
    ("title", {}, "Cedar County Records Project findings"),
    ("id", {}, BASE + "/feed.xml"),
    ("link", {"href": BASE + "/feed.xml", "rel": "self"}, ""),
    ("link", {"href": BASE + "/"}, ""),
    ("updated", {}, "2026-02-01T18:00:00+00:00"),
    ("author", {}, ""),
    ("name", {}, "Cedar County Records Project"),
    ("entry", {}, ""),
    ("title", {}, "Cedar Police produced a written ALPR usage policy (fictional)"),
    ("id", {}, "urn:deflock-finding:fnd_0000000000000001"),
    ("link", {"href": BASE + "/findings/cedar-policy-posted.html"}, ""),
    ("published", {}, "2026-02-01T18:00:00+00:00"),
    ("updated", {}, "2026-02-01T18:00:00+00:00"),
    ("summary", {}, SUMMARY),
    ("category", {"term": "documented_fact"}, ""),
]


def tuples(text):
    return [(el.tag.split("}")[1], dict(el.attrib), (el.text or "").strip()) for el in ET.fromstring(text).iter()]


class AgencyFeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-agency-feeds-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.content = self.root / "content"

    def edit(self, name, change):
        path = self.content / name
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def add_finding(self, slug, fid, agency_id, published="2026-03-01T18:00:00+00:00"):
        document = json.loads((self.content / "findings" / "cedar-policy-posted.json").read_text(encoding="utf-8"))
        document.update(slug=slug, id=fid, agency_id=agency_id, title=f"Synthetic finding {slug}",
                        published_at=published, corrections=[])
        (self.content / "findings" / f"{slug}.json").write_text(json.dumps(document), encoding="utf-8")

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def feeds(self):
        folder = self.public / "findings" / "agency"
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def entry_ids(self, name):
        return [el.text for el in ET.fromstring(self.read(name)).findall(f"{A}entry/{A}id")]

    def article(self, slug):
        match = re.search(rf'<article id="{slug}">(.*?)</article>', self.read("agencies.html"), flags=re.S)
        self.assertIsNotNone(match, slug)
        return match.group(1)

    def test_county_feed_unchanged_by_refactor(self):
        build_site(self.root)
        self.assertEqual(tuples(self.read("feed.xml")), BASE_FEED)

    def test_fictional_build_writes_cedar_feed_with_only_its_findings(self):
        build_site(self.root)
        self.assertEqual(self.feeds(), ["cedar.xml"])
        root = ET.fromstring(self.read("findings/agency/cedar.xml"))
        self.assertEqual(root.findtext(f"{A}title"), "Cedar County Records Project findings: Cedar Police Department (fictional)")
        self.assertEqual(root.findtext(f"{A}id"), BASE + "/findings/agency/cedar.xml")
        links = [(l.get("rel"), l.get("href")) for l in root.findall(f"{A}link")]
        self.assertEqual(links, [("self", BASE + "/findings/agency/cedar.xml"), (None, BASE + "/agencies.html#cedar")])
        self.assertEqual(root.findtext(f"{A}updated"), "2026-02-01T18:00:00+00:00")
        # Same entry builder as feed.xml: the entry subtree is identical.
        self.assertEqual(tuples(self.read("findings/agency/cedar.xml"))[8:], BASE_FEED[8:])

    def test_agency_without_findings_gets_no_feed_and_no_link(self):
        build_site(self.root)
        self.assertIn('<a href="/findings/agency/cedar.xml" type="application/atom+xml">Findings feed</a>', self.article("cedar"))
        for slug in ("cedar-county-sheriff", "cedar-city-council"):
            self.assertNotIn("Findings feed", self.article(slug))
            self.assertNotIn("findings/agency", self.article(slug))
            self.assertFalse((self.public / "findings" / "agency" / f"{slug}.xml").exists())

    def test_two_agencies_each_feed_holds_only_its_own_findings(self):
        self.add_finding("sheriff-contract", "fnd_0000000000000002", "ca-cedar-county-sheriff")
        self.add_finding("cedar-audit", "fnd_0000000000000003", "ca-cedar-police", "2026-02-15T18:00:00+00:00")
        build_site(self.root)
        self.assertEqual(self.feeds(), ["cedar-county-sheriff.xml", "cedar.xml"])
        self.assertEqual(self.entry_ids("findings/agency/cedar-county-sheriff.xml"), ["urn:deflock-finding:fnd_0000000000000002"])
        self.assertEqual(self.entry_ids("findings/agency/cedar.xml"),
                         ["urn:deflock-finding:fnd_0000000000000003", "urn:deflock-finding:fnd_0000000000000001"])
        self.assertEqual(sorted(self.entry_ids("feed.xml")), [f"urn:deflock-finding:fnd_000000000000000{n}" for n in (1, 2, 3)])
        sheriff = ET.fromstring(self.read("findings/agency/cedar-county-sheriff.xml"))
        self.assertEqual(sheriff.findtext(f"{A}updated"), "2026-03-01T18:00:00+00:00")
        cedar = ET.fromstring(self.read("findings/agency/cedar.xml"))
        self.assertEqual(cedar.findtext(f"{A}updated"), "2026-02-15T18:00:00+00:00")
        self.assertNotIn("Findings feed", self.article("cedar-city-council"))

    def test_finding_with_unknown_or_no_agency_is_in_county_feed_only(self):
        self.add_finding("orphan", "fnd_0000000000000004", "ca-nowhere-police")
        self.add_finding("no-agency", "fnd_0000000000000005", None)
        build_site(self.root)
        self.assertEqual(self.feeds(), ["cedar.xml"])
        self.assertIn("urn:deflock-finding:fnd_0000000000000004", self.entry_ids("feed.xml"))
        self.assertIn("urn:deflock-finding:fnd_0000000000000005", self.entry_ids("feed.xml"))
        self.assertEqual(self.entry_ids("findings/agency/cedar.xml"), ["urn:deflock-finding:fnd_0000000000000001"])

    def test_base_url_unset_uses_urn_and_root_relative_links(self):
        self.edit("site.json", lambda d: d.update(base_url=None))
        build_site(self.root)
        root = ET.fromstring(self.read("findings/agency/cedar.xml"))
        self.assertEqual(root.findtext(f"{A}id"), "urn:deflock-campaign:cedar-county-records-project:agency:cedar")
        self.assertEqual([l.get("href") for l in root.findall(f"{A}link")], ["/findings/agency/cedar.xml", "/agencies.html#cedar"])
        self.assertEqual(root.find(f"{A}entry/{A}link").get("href"), "/findings/cedar-policy-posted.html")
        county = ET.fromstring(self.read("feed.xml"))
        self.assertEqual(county.findtext(f"{A}id"), "urn:deflock-campaign:cedar-county-records-project")

    def test_base_url_with_trailing_slash_is_absolute_without_double_slash(self):
        self.edit("site.json", lambda d: d.update(base_url=BASE + "/"))
        build_site(self.root)
        text = self.read("findings/agency/cedar.xml")
        root = ET.fromstring(text)
        self.assertEqual(root.findtext(f"{A}id"), BASE + "/findings/agency/cedar.xml")
        hrefs = [l.get("href") for l in root.iter(f"{A}link")]
        self.assertEqual(hrefs, [BASE + "/findings/agency/cedar.xml", BASE + "/agencies.html#cedar",
                                 BASE + "/findings/cedar-policy-posted.html"])
        self.assertNotIn(".invalid//", text)

    def test_agency_name_with_markup_characters_is_escaped_and_round_trips(self):
        # The content loader already refuses angle brackets in names; "&" is the markup character that reaches the feed.
        name = "Cedar Police & Fire 'Unit' (fictional)"
        self.edit("agencies.json", lambda d: [a.update(name=name) for a in d if a["agency_id"] == "ca-cedar-police"])
        build_site(self.root)
        text = self.read("findings/agency/cedar.xml")
        self.assertIn("Cedar Police &amp; Fire", text)
        self.assertNotIn("Police & Fire", text)
        self.assertEqual(ET.fromstring(text).findtext(f"{A}title"), "Cedar County Records Project findings: " + name)

    def test_every_findings_feed_link_resolves_to_a_written_file(self):
        self.add_finding("sheriff-contract", "fnd_0000000000000002", "ca-cedar-county-sheriff")
        written = {p.relative_to(self.public).as_posix() for p in build_site(self.root)}
        hrefs = re.findall(r'<a href="/([^"]+)" type="application/atom\+xml">Findings feed</a>', self.read("agencies.html"))
        self.assertEqual(sorted(hrefs), ["findings/agency/cedar-county-sheriff.xml", "findings/agency/cedar.xml"])
        for href in hrefs:
            self.assertIn(href, written)
            self.assertTrue((self.public / href).is_file(), href)
        self.assertEqual({w for w in written if w.startswith("findings/agency/")}, set(hrefs))

    def test_feeds_not_listed_in_sitemap_and_head_unchanged(self):
        build_site(self.root)
        self.assertNotIn("findings/agency", self.read("sitemap.xml"))
        head = self.read("agencies.html").split("</head>")[0]
        self.assertNotIn("findings/agency", head)


if __name__ == "__main__":
    unittest.main()
