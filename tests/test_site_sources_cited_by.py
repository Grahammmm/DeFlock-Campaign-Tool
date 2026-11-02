"""sources.html names the published findings that cite each source record (DF2022-04).

Synthetic content only: a temporary copy of the fictional example; no network.
"""
import json
import shutil
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from campaign_tool.cli import read_config
from campaign_tool.content import load_content
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
CAFE = "cafe" * 16
BEEF = "beef" * 16
UNCITED = "d00d" * 16
UNKNOWN = "abcd" * 16
NOT_CITED = "Not cited by a published finding"


class SourceItems(HTMLParser):
    """Each <li> of a source-table: strong title, full text and (href, text) links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.current, self.link, self.in_strong, self.depth = [], None, None, False, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "ul" and attrs.get("class") == "source-table":
            self.depth += 1
        elif tag == "li" and self.depth:
            self.current = {"title": "", "text": "", "links": []}
        elif self.current is not None and tag == "a":
            self.link = [attrs.get("href"), ""]
        elif self.current is not None and tag == "strong":
            self.in_strong = True
        elif self.current is not None and tag != "code":
            self.current.setdefault("tags", []).append(tag)

    def handle_endtag(self, tag):
        if tag == "ul" and self.depth:
            self.depth -= 1
        elif tag == "li" and self.current is not None:
            self.items.append(self.current)
            self.current = None
        elif tag == "a" and self.link is not None:
            self.current["links"].append(tuple(self.link))
            self.link = None
        elif tag == "strong":
            self.in_strong = False

    def handle_data(self, data):
        if self.current is None:
            return
        self.current["text"] += data
        if self.link is not None:
            self.link[1] += data
        if self.in_strong:
            self.current["title"] += data


class SourcesCitedByTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-cited-by-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.base = json.loads((self.content / "findings" / "cedar-policy-posted.json").read_text())

    def add_finding(self, slug, title, published_at, shas):
        finding = dict(self.base, id="fnd_" + slug.replace("-", "_"), slug=slug, title=title,
                       published_at=published_at, corrections=[],
                       sources=[{"sha256": sha, "locator": f"page:{i + 1}", "title": "Synthetic record"}
                                for i, sha in enumerate(shas)])
        (self.content / "findings" / f"{slug}.json").write_text(json.dumps(finding))

    def add_source(self, sha, title, fragment="cedar-police-records"):
        path = self.content / "sources.json"
        sources = json.loads(path.read_text())
        sources.append({"sha256": sha, "title": title, "agency_id": "ca-cedar-police",
                        "received_at": "2026-01-25T10:00:00+00:00", "public_url": None, "fragment": fragment})
        path.write_text(json.dumps(sources))

    def build(self):
        build_site(self.root)
        html = (self.root / "public" / "sources.html").read_text(encoding="utf-8")
        parser = SourceItems()
        parser.feed(html)
        return html, parser.items

    @staticmethod
    def cited(item):
        return [(href, text) for href, text in item["links"] if href.startswith("/findings/")]

    def item(self, items, title):
        matches = [i for i in items if f'From a document we hold: {title}, received ' in i["text"]]
        self.assertEqual(len(matches), 1, title)
        return matches[0]

    def test_fictional_cited_entry_links_its_finding(self):
        html, items = self.build()
        entry = self.item(items, "Synthetic Cedar Police ALPR policy")
        self.assertEqual(self.cited(entry), [("/findings/cedar-policy-posted.html",
                                              "Cedar Police produced a written ALPR usage policy (fictional)")])
        self.assertIn(" · Cited by: Cedar Police produced", entry["text"])
        self.assertNotIn(NOT_CITED, entry["text"])
        self.assertIn("Each entry names the published findings that cite it.", html)

    def test_uncited_entry_says_so(self):
        self.add_source(UNCITED, "Synthetic uncited memo")
        _, items = self.build()
        for title in ("Synthetic uncited memo", "Synthetic Cedar Police vendor agreement excerpt"):
            entry = self.item(items, title)
            self.assertTrue(entry["text"].endswith(" · " + NOT_CITED), entry["text"])
            self.assertEqual(self.cited(entry), [])
            self.assertNotIn("Cited by", entry["text"])

    def test_two_findings_linked_in_model_order_once_each(self):
        # Later published_at sorts first in the model, ahead of the alphabetically earlier cedar slug.
        self.add_finding("zz-later-finding", "Later synthetic finding", "2026-03-01T18:00:00+00:00", [CAFE])
        _, items = self.build()
        model = load_content(self.root)
        order = [f["slug"] for f in model["findings"] if any(s["sha256"] == CAFE for s in f["sources"])]
        self.assertEqual(order, ["zz-later-finding", "cedar-policy-posted"])
        entry = self.item(items, "Synthetic Cedar Police ALPR policy")
        self.assertEqual([href for href, _ in self.cited(entry)], [f"/findings/{slug}.html" for slug in order])
        self.assertIn("Cited by: Later synthetic finding, Cedar Police produced", entry["text"])

    def test_same_sha_with_two_locators_listed_once(self):
        self.add_finding("two-locators", "Two locator finding", "2026-03-01T18:00:00+00:00", [BEEF, BEEF])
        _, items = self.build()
        entry = self.item(items, "Synthetic Cedar Police vendor agreement excerpt")
        self.assertEqual(self.cited(entry), [("/findings/two-locators.html", "Two locator finding")])
        site = Site(self.root, read_config(self.root), load_content(self.root))
        self.assertEqual([f["slug"] for f in site.cited_by[BEEF]], ["two-locators"])

    def test_same_sha_in_two_groups_lists_the_finding_in_both(self):
        self.add_source(CAFE, "Synthetic policy copy filed elsewhere", fragment="other-records")
        _, items = self.build()
        for title in ("Synthetic Cedar Police ALPR policy", "Synthetic policy copy filed elsewhere"):
            self.assertEqual([h for h, _ in self.cited(self.item(items, title))], ["/findings/cedar-policy-posted.html"])

    def test_finding_title_is_escaped(self):
        title = 'Tricky & "quoted" finding'  # the loader refuses angle brackets; see the renderer check below
        self.add_finding("tricky-title", title, "2026-03-01T18:00:00+00:00", [BEEF])
        html, items = self.build()
        entry = self.item(items, "Synthetic Cedar Police vendor agreement excerpt")
        self.assertEqual(self.cited(entry), [("/findings/tricky-title.html", title)])
        self.assertIn("Tricky &amp; &quot;quoted&quot; finding", html)
        model = load_content(self.root)
        model["findings"][0]["title"] = 'Raw <b>bold</b> & "q"'  # renderer escapes even what the loader would refuse
        page = Site(self.root, read_config(self.root), model).sources_html()["html"]
        parser = SourceItems()
        parser.feed(page)
        entry = self.item(parser.items, "Synthetic Cedar Police vendor agreement excerpt")
        self.assertEqual(entry.get("tags"), ["p", "p"])  # only provenance layout, not injected content
        self.assertEqual(self.cited(entry), [("/findings/tricky-title.html", 'Raw <b>bold</b> & "q"')])
        self.assertNotIn("<b>", page)
        self.assertIn("Raw &lt;b&gt;bold&lt;/b&gt; &amp; &quot;q&quot;", page)

    def test_citation_of_unknown_sha_changes_nothing_on_sources_page(self):
        before, _ = self.build()
        self.add_finding("unknown-record", "Cites an unlisted record", "2026-03-01T18:00:00+00:00", [UNKNOWN])
        after, items = self.build()
        self.assertEqual(after, before)
        self.assertNotIn("unknown-record", after)
        self.assertTrue((self.root / "public" / "findings" / "unknown-record.html").is_file())

    def test_every_cited_by_href_is_a_written_finding_page(self):
        self.add_finding("zz-later-finding", "Later synthetic finding", "2026-03-01T18:00:00+00:00", [CAFE, BEEF])
        _, items = self.build()
        hrefs = [href for item in items for href, _ in self.cited(item)]
        self.assertEqual(len(hrefs), 3)
        for href in hrefs:
            self.assertTrue(href.startswith("/findings/") and href.endswith(".html"), href)
            self.assertTrue((self.root / "public" / href.lstrip("/")).is_file(), href)


if __name__ == "__main__":
    unittest.main()
