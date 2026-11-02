"""DF2022-03: one linkable, printable page per staged meeting with its comment kit open.

Synthetic content only (temp copies of the fictional example); no network.
"""
import json
import re
import shutil
import tempfile
import unittest
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

from campaign_tool.cli import read_config
from campaign_tool.content import ContentError, load_content
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
EXAMPLE_ID = "cedar-council-2026-10"  # starts_at 2026-10-14T18:00:00-07:00, agenda_url and a kit
PAGE = f"meetings/{EXAMPLE_ID}.html"
BUILD_DAY = date(2026, 10, 20)
BASE = "https://cedar-records.example.invalid"
S = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
LINK = re.compile(r'<a href="(/meetings/[^"]+\.html)">Meeting page</a>')


class Parsed(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags, self.text, self.anchors, self.h2, self._h2 = [], [], [], "", False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "a":
            self.anchors.append(dict(attrs))
        self._h2 = self._h2 or tag == "h2"

    def handle_endtag(self, tag):
        if tag == "h2":
            self._h2 = False

    def handle_data(self, data):
        self.text.append(data)
        if self._h2:
            self.h2 += data


class MeetingPageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-pages-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.example = self.meetings / "2026-10-cedar-council.json"
        self.public = self.root / "public"

    def edit(self, path, drop=(), **changes):
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        for key in drop:
            data.pop(key, None)
        path.write_text(json.dumps(data), encoding="utf-8")

    def add(self, meeting_id, starts_at, **extra):
        record = {"id": meeting_id, "body": f"Synthetic board {meeting_id} (fictional)", "starts_at": starts_at, **extra}
        (self.meetings / f"{meeting_id}.json").write_text(json.dumps(record), encoding="utf-8")

    def build(self, today=BUILD_DAY):
        return {p.relative_to(self.public).as_posix() for p in build_site(self.root, today=today)}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    # 1. the fictional build writes the page; the kit is open (no <details>) and its bullets are there
    def test_example_page_has_the_kit_open(self):
        self.assertIn(PAGE, self.build())
        page = Parsed(self.read(PAGE))
        self.assertNotIn("details", page.tags)
        self.assertNotIn("summary", page.tags)
        text = " ".join(page.text)
        self.assertIn("Ask whether any data-sharing agreement exists for the fictional camera program.", text)
        self.assertIn("Public comment kit", text)
        self.assertIn("Item 7: consent calendar, technology renewals", text)
        self.assertIn("Cedar Civic Center (fictional)", text)
        self.assertEqual(page.h2, "Cedar City Council (fictional)")
        self.assertNotIn("cancelled", text.lower())
        html = self.read(PAGE)
        self.assertIn('<time datetime="2026-10-14T18:00:00-07:00">2026-10-14 18:00</time>', html)
        self.assertIn("<title>Cedar City Council (fictional), 2026-10-14 | ", html)
        self.assertIn('<meta name="description" content="Item 7: consent calendar, technology renewals">', html)
        self.assertIn('<a href="/meetings.html">All meetings</a>', html)
        self.assertIn('aria-current="page"', html)
        # the listing keeps its folded kit
        self.assertIn("<details><summary>Public comment kit</summary>", self.read("meetings.html"))

    # 2. no comment_kit_md: still built, with the "not staged" line and no stray kit text
    def test_meeting_without_a_kit(self):
        self.edit(self.example, drop=("comment_kit_md", "agenda_item"))
        self.build()
        html = self.read(PAGE)
        self.assertIn("<p>No comment kit has been staged for this meeting.</p>", html)
        self.assertNotIn("Two minutes, one ask.", html)
        self.assertIn('<meta name="description" content="Public meeting and comment kit">', html)

    # 3. cancelled: page exists, marked, kit kept; listed only under past meetings even when in the future
    def test_cancelled_meeting(self):
        self.edit(self.example, status="cancelled", starts_at="2026-11-04T18:00:00-07:00")
        self.assertIn(PAGE, self.build())
        page = Parsed(self.read(PAGE))
        self.assertTrue(page.h2.startswith("Cancelled: "), page.h2)
        html = self.read(PAGE)
        self.assertIn('<p class="notice">This meeting was cancelled. The comment kit is kept for the record.</p>', html)
        self.assertIn("Two minutes, one ask.", html)
        listing = self.read("meetings.html")
        past = listing[listing.index('id="past-meetings"'):]
        upcoming = listing[:listing.index('id="past-meetings"')]
        self.assertIn(f'href="/meetings/{EXAMPLE_ID}.html"', past)
        self.assertNotIn(f'href="/meetings/{EXAMPLE_ID}.html"', upcoming)
        self.assertNotIn(f'href="/meetings/{EXAMPLE_ID}.html"', self.read("index.html"))
        # a scheduled meeting carries no cancelled wording
        self.add("open-one", "2026-11-05T18:00:00-07:00", status="scheduled")
        self.build()
        self.assertNotIn("cancelled", self.read("meetings/open-one.html").lower())

    # 4. agenda link only when agenda_url is set, then exactly one with noopener noreferrer
    def test_agenda_link(self):
        self.add("no-agenda", "2026-11-05T18:00:00-07:00")
        self.build()

        def article(name):
            html = self.read(name)
            return Parsed(html[html.index('<article class="section finding-body"'):html.index("</article>")])
        page = article(PAGE)
        self.assertEqual([a for a in page.anchors if a["href"].startswith("https://")],
                         [{"href": "https://example.invalid/cedar-council/agenda", "rel": "noopener noreferrer"}])
        page = article("meetings/no-agenda.html")
        self.assertNotIn("Agenda", " ".join(page.text))
        self.assertEqual([a["href"] for a in page.anchors], ["/meetings.html"])

    # 5. home and meetings.html link the page of every meeting they list; each link resolves
    def test_listings_link_every_meeting_page(self):
        for meeting_id, day in (("m1020", "2026-10-20"), ("m1028", "2026-10-28"), ("m1104", "2026-11-04"),
                                ("m1118", "2026-11-18")):
            self.add(meeting_id, day + "T18:00:00-07:00")
        self.build()
        index, listing = self.read("index.html"), self.read("meetings.html")
        ids = re.compile(r'<article id="meeting-([^"]+)"')
        for html in (index, listing):
            listed = ids.findall(html)
            self.assertTrue(listed)
            self.assertEqual(LINK.findall(html), [f"/meetings/{m}.html" for m in listed])
            for href in LINK.findall(html):
                self.assertTrue((self.public / href.lstrip("/")).is_file(), href)
        self.assertEqual(len(LINK.findall(index)), 3)  # home: next three only
        self.assertEqual(len(LINK.findall(listing)), 5)

    # 6. sitemap lists every meeting page (absolute locs); without base_url: no sitemap, relative page path
    def test_sitemap_lists_meeting_pages(self):
        self.add("m1104", "2026-11-04T18:00:00-07:00", status="cancelled")
        self.build()
        root = ET.fromstring(self.read("sitemap.xml"))
        locs = [u.findtext(S + "loc") for u in root.findall(S + "url")]
        for meeting_id in (EXAMPLE_ID, "m1104"):
            self.assertEqual(locs.count(f"{BASE}/meetings/{meeting_id}.html"), 1, meeting_id)
        self.edit(self.root / "content" / "site.json", base_url=None)
        self.build()
        self.assertFalse((self.public / "sitemap.xml").exists())
        self.assertTrue((self.public / PAGE).is_file())
        site = Site(self.root, read_config(self.root), load_content(self.root), today=BUILD_DAY)
        self.assertIn(f"/meetings/{EXAMPLE_ID}.html", site.page_paths())

    # 7. two meetings: two pages; zero meetings: no meetings/ folder, build passes
    def test_two_and_zero_meetings(self):
        self.add("second", "2026-11-05T18:00:00-07:00")
        written = self.build()
        self.assertEqual(sorted(n for n in written if n.startswith("meetings/")),
                         [PAGE, "meetings/second.html"])
        for path in self.meetings.glob("*.json"):
            path.unlink()
        written = self.build()
        self.assertFalse(any(n.startswith("meetings/") for n in written))
        self.assertFalse((self.public / "meetings").exists())
        self.assertIn("meetings.html", written)

    # 8. markup characters in content are escaped in title, <h2> and description; "<" never reaches a page
    def test_body_is_escaped(self):
        self.edit(self.example, body='Council &amp; "board" & co', agenda_item='Item 9 & "tech"')
        self.build()
        html = self.read(PAGE)
        self.assertIn("<title>Council &amp;amp; &quot;board&quot; &amp; co, 2026-10-14 | ", html)
        self.assertIn("<h2>Council &amp;amp; &quot;board&quot; &amp; co</h2>", html)
        self.assertIn('<meta name="description" content="Item 9 &amp; &quot;tech&quot;">', html)
        self.assertEqual(Parsed(html).h2, 'Council &amp; "board" & co')
        self.edit(self.example, body="Council <b>board</b>")
        with self.assertRaises(ContentError):  # the loader refuses angle brackets, so the page is never rendered
            self.build()
        self.assertNotIn("<b>", self.read(PAGE))

    # 9. a public-assets/meetings/ file collides with the generated folder; public/ untouched
    def test_public_assets_meetings_collision(self):
        self.build()
        before = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        asset = self.root / "content" / "public-assets" / "meetings" / "x.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"\x89PNG synthetic")
        self.edit(self.example, body="Changed body (fictional)")
        with self.assertRaises(ContentError) as caught:
            self.build()
        self.assertIn("public-assets/meetings/x.png", str(caught.exception))
        self.assertIn("collides with a generated path", str(caught.exception))
        after = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.assertEqual(sorted(after), sorted(before))
        self.assertTrue(all(after[name] == before[name] for name in before))


if __name__ == "__main__":
    unittest.main()
