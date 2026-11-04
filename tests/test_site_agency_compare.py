"""DF0137-12: the ``agencies.html`` "At a glance" table: one row per agency in model order (never sorted by a number),
newest transparency numbers, posted ALPR policy, contract end and asked count, each linked to its source or
"not checked"; no ranking wording; stacked on phones, bordered table in print.

Offline, synthetic agencies, URLs and numbers only.
"""
import json
import re
import shutil
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from campaign_tool.labels import DEFAULTS
from campaign_tool.site import build_site
from campaign_tool.site_check import check_accessibility, check_links

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
PORTAL = "https://transparency.flocksafety.com/zq-1"  # the police agency's slug zq-1 below
NEW = {"checked_on": "2025-09-01", "source_url": PORTAL, "cameras": None, "retention_days": None,
       "searches_last_30_days": 9100, "hotlist_alerts": 77}
OLD = {"checked_on": "2025-06-01", "source_url": PORTAL + "/", "cameras": 4236, "retention_days": 30,
       "searches_last_30_days": 9300}
CONTRACT = {"vendor": "Zq vendor", "start": "2024-01-01", "end": "2025-12-31", "renewal_note": "",
            "source_url": "https://example.invalid/zq-contract", "checked_on": "2025-08-01"}
ASKED = [{"topic": "Zq retention", "asked_on": "2025-05-01", "state": "awaiting_response"},
         {"topic": "Zq audits", "asked_on": "2025-04-01", "state": "answered", "updated_on": "2025-05-02"}]
NOT_CHECKED = "not checked"
# agencies.html #content for an empty agency list, captured from main ccd09bfe before this change.
EMPTY_MAIN = ('<section class="section" id="content"><div class="section-heading"><div><p class="eyebrow">Agencies</p>'
              '<h2>Public bodies in Cedar County (fictional).</h2></div><p>Listing an agency here says only that we asked or'
              ' intend to ask it for records. No agency is asserted to operate license plate readers unless a published '
              'finding says so with sources. <a href="/data/agencies.json" type="application/json">Agencies as open data (JSON)</a> · '
              '<a href="/data/agencies.csv" type="text/csv">Spreadsheet (CSV)</a>.'
              '</p></div><p>No agencies are configured yet.</p></section>')


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


class Table(HTMLParser):
    """Rows of the glance table: [(cell text, [hrefs], tag, attrs)], plus the header cells."""

    def __init__(self):
        super().__init__()
        self.head, self.rows, self.cell, self.caption, self.in_caption = [], [], None, "", False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self.rows.append([])
        elif tag in ("th", "td"):
            self.cell = ["", [], tag, attrs]
        elif tag == "a" and self.cell is not None:
            self.cell[1].append(attrs["href"])
        elif tag == "caption":
            self.in_caption = True

    def handle_endtag(self, tag):
        if tag in ("th", "td"):
            self.rows[-1].append((" ".join(self.cell[0].split()), *self.cell[1:]))
            self.cell = None
        elif tag == "caption":
            self.in_caption = False

    def handle_data(self, data):
        if self.cell is not None:
            self.cell[0] += data
        elif self.in_caption:
            self.caption += data


class AgencyCompareTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-agency-compare-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "agencies.json"
        self.base = json.loads(self.path.read_text())

    @staticmethod
    def portal(agency, n, transparency):
        agency.update(verified=True, flock_transparency_slug=f"zq-{n}", verification={
            "source_url": f"https://example.invalid/zq-check-{n}", "checked_on": "2025-01-15"})
        agency.setdefault("profile", {"status": "Listed"})["transparency"] = transparency

    def write(self, agencies):
        self.path.write_text(json.dumps(agencies))

    def full(self):
        """Sheriff: posted policy, contract, two asked items. Police: two snapshots. Council: nothing."""
        agencies = json.loads(json.dumps(self.base))
        agencies[0]["profile"]["contract"], agencies[0]["asked"] = CONTRACT, ASKED
        self.portal(agencies[1], 1, [NEW, OLD])
        self.write(agencies)
        return agencies

    def build(self):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root)
        self.assertEqual(urlopen.call_count, 0)
        return (self.public / "agencies.html").read_text(encoding="utf-8")

    def glance(self, page):
        return page.split('<section class="at-a-glance" id="at-a-glance">', 1)[1].split("</section>", 1)[0]

    def table(self, page):
        parser = Table()
        parser.feed(self.glance(page))
        return parser

    def article_ids(self, page):
        return re.findall(r'<article id="([^"]+)">', page)

    def test_rows_follow_model_order_never_a_number(self):
        orders = []
        for counts in ((9000, 50, 10), (10, 50, 9000)):
            agencies = json.loads(json.dumps(self.base))
            for n, count in enumerate(counts):
                self.portal(agencies[n], n, [dict(OLD, cameras=count, source_url=PORTAL[:-1] + str(n))])
            self.write(agencies)
            page = self.build()
            rows = self.table(page).rows[1:]
            self.assertEqual([row[1][0] for row in rows], [f"{count} checked 2025-06-01" for count in counts])
            orders.append([row[0][0] for row in rows])
        self.assertEqual(orders[0], orders[1])
        self.assertEqual(orders[0], [agency["name"] for agency in self.base])

    def test_mixed_cells_link_their_sources(self):
        self.full()
        page = self.build()
        sheriff, police, council = self.article_ids(page)
        rows = self.table(page).rows[1:]
        self.assertEqual([cell[:2] for cell in rows[0][1:]], [
            (NOT_CHECKED, []), (NOT_CHECKED, []), (NOT_CHECKED, []),
            ("posted checked 2026-10-30", [self.base[0]["profile"]["alpr_policy"]["url"]]),
            ("2025-12-31 checked 2025-08-01", [CONTRACT["source_url"]]), (NOT_CHECKED, []), ("2", [f"#{sheriff}-asked"])])
        # Newest snapshot only: cameras/retention null there stay "not checked", never the older 4236 / 30.
        self.assertEqual([cell[:2] for cell in rows[1][1:]], [
            (NOT_CHECKED, []), ("9100 checked 2025-09-01", [PORTAL]), (NOT_CHECKED, []),
            (NOT_CHECKED, []), (NOT_CHECKED, []), (NOT_CHECKED, []), ("0", [])])
        self.assertIn('<a href="' + PORTAL + '" rel="noopener noreferrer">9100</a> <small>checked '
                      '<time datetime="2025-09-01">2025-09-01</time></small>', self.glance(page))
        self.assertIn(f'<section class="agency-block"><h4 id="{sheriff}-asked">What we asked</h4>', page)
        self.assertEqual(rows[1][0][1], ["/agencies/cedar.html"])  # its own page when it has one
        self.assertEqual(rows[2][0][1], [f"#{council}"])  # otherwise its card on this page
        self.assertEqual(police, "cedar")

    def test_agency_with_nothing_is_not_checked_six_times_and_zero(self):
        self.full()
        rows = self.table(self.build()).rows[1:]
        self.assertEqual([cell[0] for cell in rows[2][1:]], [NOT_CHECKED] * 6 + ["0"])
        self.assertEqual([href for cell in rows[2][1:] for href in cell[1]], [])

    def test_no_agencies_no_section_and_one_agency_still_a_table(self):
        for name in ("findings", "meetings", "map"):
            shutil.rmtree(self.root / "content" / name)
        (self.root / "content" / "sources.json").unlink()
        self.write([])
        page = self.build()
        self.assertNotIn("at-a-glance", page)
        self.assertNotIn("<table", page)
        self.assertIn(EMPTY_MAIN, page)
        self.write([self.base[2]])
        parser = self.table(self.build())
        self.assertEqual(len(parser.rows), 2)
        self.assertEqual(parser.rows[1][0][0], self.base[2]["name"])

    def test_every_link_is_a_shown_source_or_in_page_anchor(self):
        self.full()
        page = self.build()
        self.assertEqual(check_links(self.public), [])
        glance = self.glance(page)
        rest = page.replace(glance, "")
        hrefs = [href for row in self.table(page).rows for cell in row for href in cell[1]]
        self.assertEqual(len(hrefs), 7)
        for href in hrefs:
            if href.startswith("https://"):
                self.assertIn(f'href="{href}"', rest, href)
            else:
                self.assertTrue(href.startswith(("#", "/agencies/")), href)

    def test_caption_scoped_headers_and_a11y_lint(self):
        self.full()
        parser = self.table(self.build())
        self.assertEqual(check_accessibility(self.public), [])
        self.assertEqual(" ".join(parser.caption.split()), DEFAULTS["agencies.glance_caption"])
        head = parser.rows[0]
        self.assertEqual([cell[0] for cell in head], ["Agency", "Cameras (portal)", "Searches last 30 days (portal)",
                                                      "Retention days (portal)", "ALPR policy", "Contract ends",
                                                      "Contract cost as stated", "Records items asked"])
        self.assertTrue(all(cell[2] == "th" and cell[3] == {"scope": "col"} for cell in head))
        for row in parser.rows[1:]:
            self.assertEqual((row[0][2], row[0][3]["scope"]), ("th", "row"))
            self.assertEqual([cell[3]["data-label"] for cell in row], [cell[0] for cell in head])

    def test_no_ranking_or_score_wording(self):
        banned = re.compile(r"best|worst|rank|score", re.I)
        keys = [key for key in DEFAULTS if key.startswith("agencies.glance_")]
        self.assertEqual(len(keys), 14)
        self.assertEqual([key for key in keys if key != "agencies.glance_note" and banned.search(DEFAULTS[key])], [])
        # The note's only such word is the disclaimer itself.
        self.assertEqual(DEFAULTS["agencies.glance_note"], "Numbers are as each source reported them on the date "
                                                            "shown. This table does not rank agencies.")
        self.full()
        glance = self.glance(self.build())
        self.assertEqual(banned.findall(glance.split("<table", 1)[1]), [])
        self.assertEqual(banned.findall(glance), ["rank"])

    def test_phone_stacks_and_print_keeps_bordered_table(self):
        self.full()
        self.build()
        css = (self.public / "style.css").read_text(encoding="utf-8")
        phone = css.split("@media screen and (max-width:640px){", 1)[1].split("\n}", 1)[0]
        self.assertIn(".glance-table td::before{content:attr(data-label)", phone)
        self.assertIn(".glance-table th,.glance-table td{display:block}", phone)
        before = css.split("@media screen and (max-width:640px){", 1)[0]
        self.assertIn(".glance-table th,.glance-table td{border:1px solid #7d8a86;", before)
        self.assertIn('a[href^="http"]::after{content:" (" attr(href) ")"', css.split("@media print{", 1)[1])
        # Only source cells hold http links, so print shows only their URLs.
        rows = self.table((self.public / "agencies.html").read_text(encoding="utf-8")).rows[1:]
        self.assertEqual([href.startswith("http") for row in rows for href in row[0][1] + row[7][1]], [False] * 4)


if __name__ == "__main__":
    unittest.main()
