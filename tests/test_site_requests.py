"""DF1103-13: /requests.html lists every public asked item across agencies in one table (synthetic fixtures, no network)."""
import builtins
import html
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool.cli import read_config
from campaign_tool.content import ASKED_STATES, ContentError, load_content
from campaign_tool.site import REQUESTS_FILTER_CSS, Site, asked_sentence, build_site, preview_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)
SHERIFF, POLICE, COUNCIL = "ca-cedar-county-sheriff", "ca-cedar-police", "ca-cedar-city-council"
ROW = re.compile(r'<tr(?: data-agency="[^"]*" data-state="[^"]*")?><th scope="row" data-label="([^"]*)"><a href="([^"]*)">(.*?)</a></th>'
                 r'<td data-label="([^"]*)">(.*?)</td><td data-label="([^"]*)"><time datetime="([^"]*)">\7</time></td>'
                 r'<td data-label="([^"]*)">(.*?)</td><td data-label="([^"]*)">(.*?)</td></tr>', re.S)
LEAD = re.compile(r"<p>(\d+) requests listed\. By state: (.*?)</p>")
FEED_TO_CORRECTIONS = re.compile(r'<a href="/feed\.xml">Findings feed</a> · <a href="/meetings\.xml">Meetings feed</a> · '
                                 r'<a href="/search\.html">Search</a> · '
                                 r'<a href="/requests\.html">What we have asked for</a> · <a href="/corrections\.html">')


def item(topic, asked_on, state="awaiting_response", **more):
    return dict({"topic": topic, "asked_on": asked_on, "state": state}, **more)


# Input order is deliberately not display order; SHERIFF has no individual page (no findings, no sources).
MIXED = {
    SHERIFF: [item("Zeta log", "2026-09-01", "answered", updated_on="2026-10-05"), item("Dup", "2026-10-01")],
    POLICE: [item("Dup", "2026-10-01", "acknowledged", updated_on="2026-10-10"),
             item("Dup", "2026-10-01", "acknowledged", updated_on="2026-10-10"), item("Newest", "2026-10-20", "withdrawn")],
    COUNCIL: [item("Mid", "2026-09-15", "partly_answered")],
}


class SiteRequestsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-site-requests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.public = self.root / "public"

    def asked(self, by_agency):
        path = self.content / "agencies.json"
        agencies = json.loads(path.read_text(encoding="utf-8"))
        for agency in agencies:
            agency.pop("asked", None)
            if agency["agency_id"] in by_agency:
                agency["asked"] = by_agency[agency["agency_id"]]
        path.write_text(json.dumps(agencies), encoding="utf-8")

    def build(self, today=TODAY):
        return build_site(self.root, today=today)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def rows(self, name="requests.html"):
        return ROW.findall(self.read(name))

    def assertFound(self, match):
        self.assertIsNotNone(match)
        return match

    def lead(self):
        total, rest = self.assertFound(LEAD.search(self.read("requests.html"))).groups()
        return int(total), re.findall(r"(?:^|· )([^·(]+?) \((\d+)\)", rest)

    def test_all_agencies_and_duplicate_items_newest_first(self):
        self.asked(MIXED)
        self.build()
        rows = self.rows()
        self.assertEqual([(r[4], r[6]) for r in rows], [
            ("Newest", "2026-10-20"), ("Dup", "2026-10-01"), ("Dup", "2026-10-01"), ("Dup", "2026-10-01"),
            ("Mid", "2026-09-15"), ("Zeta log", "2026-09-01")])
        # equal days: agency_id ascending (sheriff before police), then list position; the two identical police rows stay
        self.assertEqual([html.unescape(r[2]) for r in rows[1:4]], ["Cedar County Sheriff's Office (fictional)"] + ["Cedar Police Department (fictional)"] * 2)
        self.assertEqual([r[10] for r in rows[1:4]], ["Not recorded"] + ['<time datetime="2026-10-10">2026-10-10</time>'] * 2)
        self.assertEqual(rows[5][10], '<time datetime="2026-10-05">2026-10-05</time>')  # an update never reorders
        total, counts = self.lead()
        self.assertEqual(total, 6)
        self.assertEqual(counts, [("Awaiting response", "1"), ("Acknowledged", "2"), ("Partly answered", "1"),
                                  ("Answered", "1"), ("Declined in writing", "0"), ("Withdrawn", "1")])
        self.assertEqual(sum(int(n) for _, n in counts), len(rows))
        agencies = self.read("agencies.html")
        for _, href, *_ in rows:  # every agency link resolves: an individual page, or a real card anchor
            if "#" in href:
                self.assertEqual(href.split("#")[0], "/agencies.html")
                self.assertIn(f'<article id="{href.split("#")[1]}">', agencies)
            else:
                self.assertTrue((self.public / href.lstrip("/")).is_file(), href)
        self.assertEqual(rows[0][1], "/agencies/cedar.html")
        self.assertTrue(rows[1][1].startswith("/agencies.html#"))  # the asked-only sheriff has no page of its own
        self.assertEqual(self.read("requests.html").count("<tbody>"), 1)

    def test_state_sentence_matches_agency_for_every_state(self):
        items = [item(f"Topic {state} {'upd' if extra else 'bare'}", f"2026-10-{n:02d}", state, **extra)
                 for n, state in enumerate(ASKED_STATES, 1) for extra in ({}, {"updated_on": "2026-10-28"})]
        items.append(item("Noted", "2026-10-30", "answered", updated_on="2026-10-31", note="A private-looking note."))
        self.asked({POLICE: items})
        for today in (TODAY, date(2026, 12, 9)):  # awaiting_response with no update follows the injected build day
            self.build(today)
            requests, agencies = self.read("requests.html"), html.unescape(self.read("agencies.html"))
            self.assertEqual(len(self.rows()), len(items))
            for row in self.rows():
                state = html.unescape(row[8])
                match = next(i for i in items if i["topic"] == html.unescape(row[4]) and i["asked_on"] == row[6])
                self.assertEqual(state, asked_sentence(match, today))
                self.assertIn(f'asked {row[6]}. {state}', agencies)  # the agency page uses the same sentence
            self.assertNotIn("A private-looking note.", requests)
            self.assertIn(f"No written response recorded as of {today.isoformat()}.", requests)
            self.assertEqual(self.lead()[0], len(items))

    def test_empty_requests_page_and_zero_lead(self):
        self.asked({})
        self.build()
        page = self.read("requests.html")
        self.assertIn("No public requests are listed yet.", page)
        self.assertEqual(self.rows(), [])
        self.assertNotIn("<tr>", page)
        self.assertNotIn("<table", page)
        total, counts = self.lead()
        self.assertEqual((total, [n for _, n in counts]), (0, ["0"] * 6))
        self.assertEqual([label for label, _ in counts], ["Awaiting response", "Acknowledged", "Partly answered", "Answered",
                                                          "Declined in writing", "Withdrawn"])
        self.assertIn("<h2>What we have asked for</h2>", page)
        # agencies that exist but list nothing, and no agencies at all
        self.asked({SHERIFF: [], POLICE: []})
        self.build()
        self.assertIn("No public requests are listed yet.", self.read("requests.html"))
        model = load_content(self.root, TODAY)
        model["agencies"] = []
        page = Site(self.root, read_config(self.root), model, today=TODAY).requests_html()["html"]
        self.assertIn("No public requests are listed yet.", page)
        self.assertIn("0 requests listed.", page)

    def test_escaped_text_and_labels(self):
        evil = '<script>alert(1)</script> & "q"'
        self.asked({POLICE: [item("Plain", "2026-10-01")]})
        model = load_content(self.root, TODAY)
        model["agencies"][1]["name"] = '<b>Bad</b> & "Co"'
        model["agencies"][1]["asked"] = [item(evil, "2026-10-01", note="<i>n</i>")]
        page = Site(self.root, read_config(self.root), model, today=TODAY).requests_html()["html"]
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<b>Bad", page)
        self.assertNotIn("<i>n</i>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;q&quot;", page)
        self.assertIn("&lt;b&gt;Bad&lt;/b&gt; &amp; &quot;Co&quot;", page)
        # the normal loader refuses angle brackets, an invalid date and an unknown state, and writes nothing
        for bad, message in ((item("<b>x</b>", "2026-10-01"), "plain text"), (item("T", "2026-13-01"), "asked_on"),
                             (item("T", "2026-10-01", "overdue"), "choose one of"), (item("T", "2026-12-01"), "build date")):
            self.asked({POLICE: [bad]})
            with self.assertRaisesRegex(ContentError, message):
                self.build()
            self.assertFalse(self.public.exists())
        labels = {"requests.title": 'Asked & "answered"', "requests.col_topic": "Subject & \"1\"", "requests.empty": "None & none",
                  "requests.not_recorded": "n/a", "requests.count": "{state}={count}", "requests.state_withdrawn": "Pulled & gone",
                  "requests.lead": "Total {total}.", "footer.requests": "Asked \"list\"",
                  **{f"requests.state_{state}": f"S-{state}" for state in ASKED_STATES[:5]}}
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")
        self.asked({POLICE: [item("Plain", "2026-10-01")]})
        self.build()
        page = self.read("requests.html")
        self.assertIn("<h2>Asked &amp; &quot;answered&quot;</h2>", page)
        self.assertIn('data-label="Subject &amp; &quot;1&quot;"', page)
        self.assertIn("n/a</td>", page)
        self.assertIn("Total 1. S-awaiting_response=1 · S-acknowledged=0 · S-partly_answered=0 · S-answered=0 · "
                      "S-declined_in_writing=0 · Pulled &amp; gone=0", page)
        self.assertEqual(page.count('<a href="/requests.html">Asked &quot;list&quot;</a>'), 1)
        self.asked({})
        self.build()
        self.assertIn("None &amp; none", self.read("requests.html"))

    def test_mobile_semantics_and_no_script(self):
        long_topic = "W" * 120
        self.asked({POLICE: [item(long_topic, "2026-10-01", updated_on="2026-10-02")]})
        self.build()
        page, css = self.read("requests.html"), self.read("style.css")
        main = page[page.index('id="content"'):page.index("<footer")]
        self.assertIn('<table class="glance-table"><caption>Every public request across agencies, newest first</caption>', main)
        heads = ["Agency", "Topic", "Asked on", "State", "Updated on"]
        self.assertEqual(re.findall(r'<th scope="col">([^<]*)</th>', main), heads)
        row = self.rows()[0]
        self.assertEqual([row[0], row[3], row[5], row[7], row[9]], heads)  # every cell carries its column's label
        self.assertEqual(row[4], long_topic)
        self.assertEqual(main.count('<th scope="row"'), 1)
        for rule in (".glance-table{width:100%", "overflow-wrap:anywhere", ".glance-table tbody th::before,.glance-table td::before{content:attr(data-label)",
                     ".glance-table,.glance-table tbody,.glance-table tr,.glance-table th,.glance-table td{display:block}"):
            self.assertIn(rule, css)
        self.assertNotIn("<script", main)
        self.assertNotIn("<style", main)
        self.assertNotIn(" style=", main)
        self.assertEqual(re.findall(r"<script\b[^>]*>(.*?)</script>", page, re.S), [""] * len(re.findall(r"<script\b", page)))
        headers, with_rows = self.read("_headers"), css
        self.asked({})
        self.build()
        self.assertEqual((self.read("_headers"), self.read("style.css")), (headers, with_rows))  # CSP and CSS ignore the rows
        self.assertEqual(self.read("requests.html").count(" style="), 0)

    def test_footer_sitemap_and_reserved_slots(self):
        self.asked(MIXED)
        self.build()
        pages = sorted(self.public.rglob("*.html"))
        self.assertIn(self.public / "requests.html", pages)
        for path in pages:
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count('<a href="/requests.html">What we have asked for</a>'), 1, path.name)
            self.assertEqual(len(FEED_TO_CORRECTIONS.findall(text)), 1, path.name)
        locs = re.findall(r"<loc>([^<]+)</loc>", self.read("sitemap.xml"))
        self.assertEqual(sum(loc.endswith("/requests.html") for loc in locs), 1)
        entry = self.assertFound(re.search(r"<url><loc>[^<]*/requests\.html</loc>(.*?)</url>", self.read("sitemap.xml"))).group(1)
        self.assertEqual(entry, "")  # no build-time lastmod
        self.assertIn('<a href="/requests.html">What we have asked for</a>', self.read("agencies.html"))
        self.assertIn('<a href="/agencies.html" aria-current="page">', self.read("requests.html"))
        self.assertEqual(self.read("requests.html").count("aria-current"), 1)  # no new main-nav item
        report = preview_site(self.root, TODAY)
        self.assertEqual((report["added"], report["changed"], report["removed"]), ([], [], []))  # a rebuild changes nothing
        shutil.rmtree(self.public)
        self.assertIn("requests.html", [p["path"] for p in preview_site(self.root, TODAY)["added"]])
        # no base_url: no sitemap, the footer link stays
        site_path = self.content / "site.json"
        site = json.loads(site_path.read_text(encoding="utf-8"))
        del site["base_url"]
        site_path.write_text(json.dumps(site), encoding="utf-8")
        self.build()
        self.assertFalse((self.public / "sitemap.xml").exists())
        self.assertIn('<a href="/requests.html">What we have asked for</a>', self.read("index.html"))
        # a custom page, or a public asset, may not take the reserved slot, and nothing is written
        shutil.rmtree(self.public)
        (self.content / "pages").mkdir(exist_ok=True)
        (self.content / "pages" / "requests.json").write_text(
            json.dumps({"title": "Mine", "description": "d", "body_md": "x", "nav": False}), encoding="utf-8")
        with self.assertRaisesRegex(ContentError, "requests"):
            self.build()
        self.assertFalse(self.public.exists())
        (self.content / "pages" / "requests.json").unlink()
        assets = self.content / "public-assets"
        (assets / "requests.html").mkdir(parents=True)
        (assets / "requests.html" / "logo.png").write_bytes(b"\x89PNG")
        with self.assertRaisesRegex(ContentError, "public-assets/requests.html/logo.png: collides with a generated path"):
            self.build()
        self.assertFalse(self.public.exists())
        (assets / "requests.html" / "logo.png").unlink()
        (assets / "requests.html").rmdir()
        (assets / "requests.html").write_text("<p>impostor</p>", encoding="utf-8")
        with self.assertRaisesRegex(ContentError, "public-assets/requests.html: "):
            self.build()
        self.assertFalse(self.public.exists())

    def test_private_files_never_opened(self):
        self.asked({POLICE: [item("Public topic", "2026-10-01")]})
        kit = self.root / "kit"
        kit.mkdir()
        poison = "POISON-PRIVATE-ASK"
        (kit / "agencies.json").write_text(json.dumps([{"agency_id": POLICE, "asked": [item(poison, "2026-10-05")]}]), encoding="utf-8")
        (kit / "sent.json").write_text(json.dumps({POLICE: [{"topic": poison, "sent_on": "2026-10-05"}]}), encoding="utf-8")
        (kit / "correction-requests.json").write_text(json.dumps([{"note": poison}]), encoding="utf-8")
        opened = []
        real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

        def watch(real):
            def guarded(file, *args, **kwargs):
                if isinstance(file, (str, bytes, os.PathLike)):
                    path = Path(os.fsdecode(file))
                    if path.is_absolute() and path.is_relative_to(self.root / "kit"):
                        opened.append(str(path))
                return real(file, *args, **kwargs)
            return guarded
        with mock.patch("builtins.open", watch(real_open)), mock.patch("io.open", watch(real_io_open)), \
                mock.patch("os.open", watch(real_os_open)):
            builtins.open(kit / "sent.json").close()  # control: the guard really sees a read of the private file
            self.assertEqual(len(opened), 1)
            opened.clear()
            self.build()
            preview_site(self.root, TODAY)
        self.assertEqual(opened, [])
        for path in self.public.rglob("*"):
            if path.is_file():
                self.assertNotIn(poison.encode(), path.read_bytes(), path.name)
        self.assertEqual([r[4] for r in self.rows()], ["Public topic"])
        self.assertEqual(self.lead()[0], 1)

    def test_repeat_build_and_unrelated_exports_unchanged(self):
        self.asked(MIXED)
        self.build()
        first = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.build()
        second = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.assertTrue(first == second, "a fixed-day rebuild changed bytes")
        self.asked({})
        self.build()
        bare = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.assertEqual(sorted(bare), sorted(set(first) - {"requests/filter.js"}))  # only the conditional filter script
        self.assertTrue(first["style.css"].endswith(REQUESTS_FILTER_CSS.encode()))
        self.assertNotIn(b"requests-table", bare["style.css"])
        first["style.css"] = first["style.css"][:-len(REQUESTS_FILTER_CSS)]  # compare without the enabled-only fragment
        for name in ("_headers", "style.css", "robots.txt", "feed.xml", "data/findings.json", "data/findings.csv",
                     "data/sources.json", "data/sources.csv", "data/meetings.json", "site-data.js", "app.js", "search.js"):
            self.assertTrue(bare[name] == first[name], name)
        self.assertNotIn(b"requests", bare["data/findings.json"] + bare["data/meetings.json"])


if __name__ == "__main__":
    unittest.main()
