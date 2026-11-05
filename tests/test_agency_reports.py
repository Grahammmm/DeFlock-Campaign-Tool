"""DF0237-02: published ALPR usage reports on record (``profile.reports``), as an organizer transcribed them.

The agency card and page list each report with its own source link and checked date, newest reporting period first;
the "At a glance" table gains a "Latest report" column; data/agencies.json carries the same list. Nothing here says a
report is required, late or missing. Offline, synthetic agencies, titles and URLs only.
"""
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from html import escape
from pathlib import Path
from unittest import mock

from campaign_tool import law
from campaign_tool.content import REPORT_KINDS, ContentError, _reports, load_agencies
from campaign_tool.content_check import content_problems
from campaign_tool.labels import DEFAULTS
from campaign_tool.site import PUBLIC_AGENCY_FIELDS, PUBLIC_ARCHIVE_FIELDS, PUBLIC_REPORT_FIELDS, Site, build_site
from campaign_tool.site_check import check_accessibility, check_links

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "agencies-open-data.schema.json").read_text(encoding="utf-8"))
ES = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))["labels"]
BUILD_DAY = date(2026, 11, 1)
SHERIFF, CEDAR = 0, 1  # indexes in the example's agencies.json: Cedar PD has an agency page, the sheriff has none
LABEL = f"content/agencies.json[{CEDAR}].profile.reports"
ITEM = LABEL + "[0]"
REPORT = {"title": "Synthetic ALPR annual report 2025", "kind": "annual_report", "period_start": "2025-01-01",
          "period_end": "2025-12-31", "source_url": "https://example.invalid/cedar/report-2025", "checked_on": "2026-10-21"}
SENTINEL = "Zq-private-sentinel-91"
LIST_ERROR = ": must be a list of 1 to 20 report entries"
SHAPE_ERROR = ": needs title, kind, period_start, period_end, source_url and checked_on, and may add only published_on"
KIND_ERROR = ".kind: choose one of ['annual_report', 'audit_summary', 'usage_statistics', 'surveillance_report', 'other']"
TITLE_ERROR = ".title: plain title as published, 1 to 160 characters without markup or control characters"
DAY_ERROR = ".{}: must be a real YYYY-MM-DD calendar date"
ORDER_ERROR = ".period_end: must be after period_start"
FUTURE_ERROR = ".checked_on: must not be after the build date"
PUBLISHED_ERROR = ".published_on: must not be after checked_on"
SOURCE_ERRORS = (".source_url: must be an https:// URL", ".source_url: invalid URL host")
REPORT_LABELS = [key for key in DEFAULTS if key.startswith("agency.report")]
NONE_ON_RECORD = "none on record"
ABSENT = object()


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


def entry(**changes):
    return {**REPORT, **changes}


def is_refused(value, tail, today=BUILD_DAY, label=LABEL):
    try:
        _reports(value, label, today)
    except ContentError as caught:
        return str(caught) == label + tail
    return False


class ReportsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-reports-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "agencies.json"
        self.agencies = json.loads(self.path.read_text(encoding="utf-8"))

    # -- helpers -------------------------------------------------------------
    def record(self, reports=ABSENT, index=CEDAR):
        if reports is not ABSENT:
            self.agencies[index].setdefault("profile", {})["reports"] = reports
        self.path.write_text(json.dumps(self.agencies), encoding="utf-8")

    def build(self, today=BUILD_DAY):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root, today=today)
        self.assertEqual(urlopen.call_count, 0)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def snapshot(self, base=None):
        base = base or self.public
        return {p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*") if p.is_file()}

    def articles(self):
        return dict(re.findall(r'<article id="([^"]+)">(.*?)</article>', self.read("agencies.html"), re.S))

    def card(self):
        return self.articles()["cedar"]

    def exported(self):
        doc = json.loads(self.read("data/agencies.json"))
        law.validate(doc, SCHEMA)
        return [a for a in doc["agencies"] if a["agency_id"] == "ca-cedar-police"][0]

    def glance_rows(self):
        table = self.read("agencies.html").split('<table class="glance-table">', 1)[1].split("</table>", 1)[0]
        return [re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S) for row in re.findall(r"<tr>(.*?)</tr>", table, re.S)]

    def accepted(self, reports):
        self.record(reports)
        self.build()
        return self.exported()["reports"]

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args, "--directory", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def assert_refused(self, reports, tail, today=BUILD_DAY, alternatives=None):
        """Refused with exactly ``ITEM``-style text; the message never repeats a submitted value."""
        with self.assertRaises(ContentError) as caught:
            _reports(reports, LABEL, today)
        self.assertIn(str(caught.exception), [LABEL + t for t in (alternatives or (tail,))])
        self.assertNotIn(SENTINEL, str(caught.exception))

    # -- validation ----------------------------------------------------------
    def test_list_shapes_and_exact_keys(self):
        self.assertEqual(len(_reports([REPORT], LABEL, BUILD_DAY)), 1)
        twenty = [entry(title=f"Synthetic report {n}") for n in range(20)]
        self.assertEqual(len(_reports(twenty, LABEL, BUILD_DAY)), 20)
        for bad in ([], None, {}, REPORT, [entry(title=f"Synthetic report {n}") for n in range(21)], "x", 5, True, (REPORT,)):
            with self.subTest(shape=repr(bad)[:40]):
                self.assert_refused(bad, LIST_ERROR)
        for bad in (None, "x", 5, [REPORT], True):
            with self.subTest(entry=bad):
                self.assert_refused([bad], "[0]: expected an object")
        for key in REPORT:
            with self.subTest(missing=key):
                self.assert_refused([{k: v for k, v in REPORT.items() if k != key}], "[0]" + SHAPE_ERROR)
        self.assert_refused([{**REPORT, SENTINEL: "x"}], "[0]" + SHAPE_ERROR)  # an unknown key is never echoed
        self.assert_refused([{**REPORT, "summary": SENTINEL}], "[0]" + SHAPE_ERROR)
        for kind in ("Annual_Report", "annual report", "", None, 5, True, ["annual_report"], {"kind": "other"}, SENTINEL):
            with self.subTest(kind=kind):
                self.assert_refused([entry(kind=kind)], "[0]" + KIND_ERROR)
        for kind in ("annual_report", "audit_summary", "usage_statistics", "surveillance_report", "other"):
            self.assertEqual(_reports([entry(kind=kind)], LABEL, BUILD_DAY)[0]["kind"], kind)
        self.assert_refused([REPORT, entry(kind="bogus")], "[1]" + KIND_ERROR)  # the index names the entry

    def test_title_boundaries_and_escaping(self):
        for title in ("x", "y" * 160, "Cañón d’O'Brien & Sons — Évaluation"):
            with self.subTest(title=title[:12]):
                self.assertEqual(_reports([entry(title=title)], LABEL, BUILD_DAY)[0]["title"], title)
        for title in ("", "y" * 161, " x", "x ", "\tx", "x\n", "a\nb", "a\tb", "a\x00b", "a\x7fb", "a\x85b", "a\u202eb", "a\u200bb",
                      "a\ufeffb", "a\u2028b", "a\u2029b", "<b>x</b>", "a>b", "a<b", None, 5, ["x"], True):
            with self.subTest(title=repr(title)):
                self.assert_refused([entry(title=title)], "[0]" + TITLE_ERROR)
        title = 'Rock & "Roll" *not bold* [a](b) \'q\' #1 &amp; &lt;'
        shown = escape(title, quote=True)
        self.record([entry(title=title, source_url="https://example.invalid/a?x=1&y=2")])
        self.build()
        for html in (self.card(), self.read("agencies/cedar.html")):
            self.assertEqual(html.count(f">{shown}</a>"), 1)  # escaped once, Markdown and entities literal
            self.assertNotIn("<em>", html)
            self.assertIn('href="https://example.invalid/a?x=1&amp;y=2" rel="noopener noreferrer">' + shown, html)
        self.assertEqual(self.exported()["reports"][0]["title"], title)  # JSON carries the text verbatim

    def test_dates_and_injected_build_day(self):
        for day in ("2024-02-29", "2000-01-01"):  # a real leap day
            self.assertEqual(_reports([entry(period_start=day)], LABEL, BUILD_DAY)[0]["period_start"], day)
        junk = ("2025-02-29", "2026-02-30", "2026-13-01", "2026-00-10", "0000-01-01", "2026-1-5", "20260101", "2026-01-01T00:00:00",
                "2026-01-01 ", " 2026-01-01", "2026-01-01\n", "２０２６-01-01", "", 20260101, None, True, ["2026-01-01"])
        for key in ("period_start", "period_end", "checked_on"):
            for day in junk:
                with self.subTest(key=key, day=repr(day)):
                    self.assert_refused([entry(**{key: day})], "[0]" + DAY_ERROR.format(key))
        for day in junk:
            with self.subTest(key="published_on", day=repr(day)):
                self.assert_refused([entry(published_on=day)], "[0]" + DAY_ERROR.format("published_on"))
        self.assert_refused([entry(period_start="2025-06-01", period_end="2025-06-01")], "[0]" + ORDER_ERROR)  # same day
        self.assert_refused([entry(period_start="2025-06-02", period_end="2025-06-01")], "[0]" + ORDER_ERROR)  # reversed
        self.assertEqual(len(_reports([entry(period_start="2025-06-01", period_end="2025-06-02")], LABEL, BUILD_DAY)), 1)
        # a period may end after the build date or after publication: the document's stated scope is kept
        self.assertEqual(len(_reports([entry(period_end="2031-12-31", published_on="2026-01-02")], LABEL, BUILD_DAY)), 1)
        self.assertEqual(_reports([entry(checked_on="2026-11-01")], LABEL, BUILD_DAY)[0]["checked_on"], "2026-11-01")  # build day itself
        self.assert_refused([entry(checked_on="2026-11-02")], "[0]" + FUTURE_ERROR)  # the next day, though long past today
        self.assertEqual(_reports([entry(checked_on="2026-11-02")], LABEL, date(2026, 11, 2))[0]["checked_on"], "2026-11-02")
        self.assertEqual(_reports([entry(checked_on="2026-10-21", published_on="2026-10-21")], LABEL, BUILD_DAY)[0]["published_on"],
                         "2026-10-21")
        self.assert_refused([entry(checked_on="2026-10-21", published_on="2026-10-22")], "[0]" + PUBLISHED_ERROR)
        self.assert_refused([entry(published_on="2099-01-01")], "[0]" + PUBLISHED_ERROR)
        self.assertNotIn("published_on", _reports([REPORT], LABEL, BUILD_DAY)[0])  # never inferred
        for url in ("http://example.invalid/x", "https://user@example.invalid/x", "https:///x", "https://", "ftp://example.invalid/x",
                    "example.invalid/x", "https://exa mple.invalid/x", "", None, 5, ["https://a.invalid"]):
            with self.subTest(source=url):
                self.assert_refused([entry(source_url=url)], "[0]", alternatives=["[0]" + t for t in SOURCE_ERRORS])

    def test_build_and_content_check_agree_under_a_frozen_clock(self):
        class Frozen(date):
            @classmethod
            def today(cls):
                return BUILD_DAY

        for day, ok in (("2026-11-01", True), ("2026-11-02", False)):
            with self.subTest(day=day):
                self.record([entry(checked_on=day)])
                with mock.patch("campaign_tool.content.date", Frozen):
                    problems = content_problems(self.root) or []
                self.assertEqual(any(line == ITEM + FUTURE_ERROR for line in problems), not ok)
                if not ok:
                    self.assertEqual(len([p for p in problems if "reports" in p]), 1)
                    with self.assertRaises(ContentError) as caught:
                        self.build(today=BUILD_DAY)
                    self.assertEqual(str(caught.exception), ITEM + FUTURE_ERROR)
                    self.assertFalse(self.public.exists())
        self.record([entry(checked_on="2099-01-01")])  # in the future for every clock: the commands exit alike
        built, checked = self.cli("build"), self.cli("content-check")
        self.assertEqual(built.returncode, 1)
        self.assertIn("Stopped: " + ITEM + FUTURE_ERROR, built.stderr)
        self.assertEqual(checked.returncode, 3)
        self.assertIn(ITEM + FUTURE_ERROR, checked.stdout.splitlines())
        self.assertFalse(self.public.exists())
        self.record([entry(kind=SENTINEL)])
        built, checked = self.cli("build"), self.cli("content-check")
        self.assertEqual((built.returncode, checked.returncode), (1, 3))
        for text in (built.stderr, built.stdout, checked.stdout, checked.stderr):
            self.assertNotIn(SENTINEL, text)

    # -- order and display ---------------------------------------------------
    def test_order_is_period_not_publication(self):
        url = "https://example.invalid/r/"
        reports = [
            entry(title="B-year-ago", period_start="2023-01-01", period_end="2023-12-31", published_on="2026-10-01", source_url=url + "1"),
            entry(title="A-newest-period", period_start="2025-07-01", period_end="2025-12-31", published_on="2025-12-31", source_url=url + "2"),
            entry(title="Overlap-long", period_start="2025-01-01", period_end="2025-12-31", published_on="2026-02-01", source_url=url + "3"),
            entry(title="Overlap-short", period_start="2025-03-01", period_end="2025-12-31", checked_on="2026-10-30", source_url=url + "4"),
            entry(title="Same-everything", period_start="2025-01-01", period_end="2025-12-31", source_url=url + "6"),
            entry(title="Same-everything", period_start="2025-01-01", period_end="2025-12-31", source_url=url + "5"),
            entry(title="Same-everything", period_start="2025-01-01", period_end="2025-12-31", source_url=url + "5", checked_on="2026-10-22"),
            entry(title="Same-everything", period_start="2025-01-01", period_end="2025-12-31", source_url=url + "5", checked_on="2026-10-23"),
        ]
        ordered = _reports(reports, LABEL, BUILD_DAY)
        # period_end descending, then period_start descending (A 07-01, Overlap-short 03-01, then the 01-01 group by title and
        # url ascending, exact ties in input order), then the older year: never publication or checking dates
        self.assertEqual([(r["title"], r["source_url"][-1], r["checked_on"][-2:]) for r in ordered], [
            ("A-newest-period", "2", "21"), ("Overlap-short", "4", "30"), ("Overlap-long", "3", "21"),
            ("Same-everything", "5", "21"), ("Same-everything", "5", "22"), ("Same-everything", "5", "23"),
            ("Same-everything", "6", "21"), ("B-year-ago", "1", "21")])
        # nothing is merged or deduplicated, even with a repeated URL
        twin = [entry(title="Twin one"), entry(title="Twin two")]
        self.assertEqual(len(_reports(twin, LABEL, BUILD_DAY)), 2)
        self.assertEqual(len({r["source_url"] for r in _reports(twin, LABEL, BUILD_DAY)}), 1)
        # the page, the JSON and the glance cell use that same validated order, and the year is the period's end year
        self.record(reports)
        self.build()
        first = self.snapshot()
        titles = [(r["title"], r["source_url"]) for r in ordered]
        exported = self.exported()["reports"]
        self.assertEqual([(r["title"], r["source_url"]) for r in exported], titles)
        self.assertEqual(exported, ordered)
        for html in (self.card(), self.read("agencies/cedar.html")):
            links = re.findall(r'<li><a href="([^"]+)" rel="noopener noreferrer">([^<]+)</a>', html.split("agency-reports", 1)[1])
            self.assertEqual([(title, href) for href, title in links], titles)
        cell = self.glance_rows()[CEDAR + 1][-1]
        self.assertIn(">2025</a>", cell)
        self.assertIn('href="https://example.invalid/r/2"', cell)  # the newest period's own source
        self.assertIn('checked <time datetime="2026-10-21">2026-10-21</time>', cell)
        self.build()
        self.assertEqual(self.snapshot(), first)  # two builds are byte-identical
        # period_end year, not the publication or checking year
        self.record([entry(period_end="2023-12-31", period_start="2023-01-01", published_on="2026-10-01", checked_on="2026-10-30")])
        self.build()
        self.assertIn(">2023</a>", self.glance_rows()[CEDAR + 1][-1])
        self.assertNotIn(">2026</a>", self.glance_rows()[CEDAR + 1][-1])

    def test_each_source_and_latest_none(self):
        reports = [entry(title="Report one", period_end="2025-12-31", published_on="2026-01-15", source_url="https://example.invalid/one",
                         checked_on="2026-10-20"),
                   entry(title="Report two", kind="audit_summary", period_start="2023-01-01", period_end="2023-12-31",
                         source_url="https://example.invalid/two", checked_on="2026-09-02"),
                   entry(title="Report three", kind="other", period_start="2021-01-01", period_end="2021-12-31",
                         source_url="https://example.invalid/three", checked_on="2026-08-03")]
        self.record(reports)
        self.build()
        time = lambda day: f'<time datetime="{day}">{day}</time>'
        blocks = []
        for html in (self.card(), self.read("agencies/cedar.html")):
            self.assertEqual(html.count("Reports this agency has published about its ALPR use"), 1)
            block = html.split('<section class="agency-reports">', 1)[1].split("</section>", 1)[0]
            blocks.append(block)
            items = re.findall(r"<li>(.*?)</li>", block)
            self.assertEqual(len(items), 3)
            self.assertIn('<a href="https://example.invalid/one" rel="noopener noreferrer">Report one</a>', items[0])
            self.assertIn("Annual report", items[0])
            self.assertIn(f"Reporting period {time('2025-01-01')} – {time('2025-12-31')}", items[0])
            self.assertIn(f"Published {time('2026-01-15')}", items[0])
            self.assertIn(f"checked {time('2026-10-20')}", items[0])
            self.assertIn("Audit summary", items[1])
            self.assertNotIn("Published", items[1])  # an absent published_on is omitted, never filled in
            self.assertIn(f"checked {time('2026-09-02')}", items[1])
            self.assertNotIn("2026-10-20", items[1])  # no provenance shared across the list
            self.assertIn("Other", items[2])
            self.assertIn(f"checked {time('2026-08-03')}", items[2])
        for kind, label in (("annual_report", "Annual report"), ("audit_summary", "Audit summary"),
                            ("usage_statistics", "Usage statistics"), ("surveillance_report", "Surveillance report"), ("other", "Other")):
            self.assertEqual(DEFAULTS["agency.report_kind_" + kind], label)
        self.assertEqual(DEFAULTS["agency.reports_heading"], "Reports this agency has published about its ALPR use")
        self.assertEqual(DEFAULTS["agencies.glance_report"], "Latest report")
        # heading levels: h3 on the detail page, h4 on the agencies.html article
        page = self.read("agencies/cedar.html")
        self.assertIn("<h3>Reports this agency has published about its ALPR use</h3>", page)
        self.assertIn("<h4>Reports this agency has published about its ALPR use</h4>", self.card())
        rows = self.glance_rows()
        self.assertEqual(rows[0][-1], "Latest report")
        self.assertEqual(len(rows[0]), 9)
        self.assertIn(">2025</a>", rows[CEDAR + 1][-1])
        self.assertIn('href="https://example.invalid/one"', rows[CEDAR + 1][-1])
        for other in (rows[SHERIFF + 1], rows[CEDAR + 2]):
            self.assertEqual(other[-1], NONE_ON_RECORD)  # no link, no invented checked date
        # neutral wording: no duty, lateness or accomplishment is implied by anything the feature writes
        implied = re.compile(r"overdue|required|late\b|deadline|complian|comply|failed|missing|accomplish|won\b|victory", re.I)
        written = blocks + [rows[SHERIFF + 1][-1], rows[CEDAR + 2][-1], rows[CEDAR + 1][-1]]
        written += [DEFAULTS[key] for key in REPORT_LABELS + ["agencies.glance_report", "agencies.glance_no_report"]]
        for text in written:
            self.assertIsNone(implied.search(re.sub(r"<[^>]*>", " ", text)), text)
        today = self.read("timeline.html") + self.read("index.html")
        self.assertNotIn("Report one", today)  # no today row, deadline or timeline entry comes from a report

    def test_page_eligibility_mobile_print_and_labels(self):
        self.record([entry(title="Sheriff report")], SHERIFF)  # no finding or source: the sheriff has no page and no export row
        self.build()
        self.assertIn("Sheriff report", "".join(v for k, v in self.articles().items() if k != "cedar"))
        self.assertNotIn("Sheriff report", self.card())
        names = {p.relative_to(self.public).as_posix() for p in self.public.rglob("*") if p.is_file()}
        self.assertEqual([n for n in names if n.startswith("agencies/")], ["agencies/cedar.html"])
        self.assertEqual([a["agency_id"] for a in json.loads(self.read("data/agencies.json"))["agencies"]], ["ca-cedar-police"])
        self.assertNotIn("Sheriff report", self.read("agencies/cedar.html"))
        rows = self.glance_rows()
        self.assertTrue(all(len(row) == 9 for row in rows))
        self.assertIn(">2025</a>", rows[SHERIFF + 1][-1])
        self.assertEqual([row[-1] for row in rows[2:]], [NONE_ON_RECORD, NONE_ON_RECORD])
        # scoped headers and data-labels for the new column, a11y and links
        page = self.read("agencies.html")
        self.assertEqual(page.count('<th scope="col">Latest report</th>'), 1)
        self.assertEqual(page.count('<td data-label="Latest report">'), 3)
        self.assertEqual(check_accessibility(self.public), [])
        self.assertEqual(check_links(self.public), [])
        self.assertIn('href="https://example.invalid/cedar/report-2025"', "".join(rows[SHERIFF + 1]))
        # every label can be replaced; no default phrase survives, and the Spanish draft keeps each placeholder
        self.agencies[CEDAR]["profile"]["reports"] = [entry(published_on="2026-01-02")]
        self.record()
        markers = {key: f"X{key}X" for key in DEFAULTS}
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": markers}), encoding="utf-8")
        self.build()
        for html in (self.read("agencies.html"), self.read("agencies/cedar.html")):
            for key in REPORT_LABELS:
                if key.startswith("agency.report_kind_") and key != "agency.report_kind_annual_report":
                    continue  # this fixture has one annual report; the other kinds are checked below
                self.assertIn(f"X{key}X", html, key)
            self.assertNotIn("Reports this agency has published", html)
        self.assertIn("Xagencies.glance_reportX", self.read("agencies.html"))
        self.assertIn("Xagencies.glance_no_reportX", self.read("agencies.html"))
        self.assertNotIn(NONE_ON_RECORD, self.read("agencies.html"))
        every = [entry(title=f"Kind {kind}", kind=kind, period_start=f"20{n}0-01-01", period_end=f"20{n}0-12-31")
                 for n, kind in enumerate(("annual_report", "audit_summary", "usage_statistics", "surveillance_report", "other"), 1)]
        self.agencies[CEDAR]["profile"]["reports"] = every
        self.record()
        self.build()
        for kind in ("annual_report", "audit_summary", "usage_statistics", "surveillance_report", "other"):
            self.assertIn(f"Xagency.report_kind_{kind}X", self.read("agencies/cedar.html"))
        for key in REPORT_LABELS + ["agencies.glance_report", "agencies.glance_no_report"]:
            self.assertIn(key, ES, key)
            self.assertEqual(sorted(re.findall(r"\{[a-z_]+\}", ES[key])), sorted(re.findall(r"\{[a-z_]+\}", DEFAULTS[key])), key)

    def test_no_agencies_no_table_and_one_agency_still_a_column(self):
        for name in ("findings", "meetings", "map"):
            shutil.rmtree(self.root / "content" / name)
        (self.root / "content" / "sources.json").unlink()
        self.agencies[2].setdefault("profile", {})["reports"] = [REPORT]
        self.path.write_text(json.dumps([]), encoding="utf-8")
        self.build()
        self.assertNotIn("at-a-glance", self.read("agencies.html"))
        self.assertNotIn("Latest report", self.read("agencies.html"))
        self.path.write_text(json.dumps([self.agencies[2]]), encoding="utf-8")
        self.build()
        rows = self.glance_rows()
        self.assertEqual([len(row) for row in rows], [9, 9])
        self.assertIn(">2025</a>", rows[1][-1])
        self.path.write_text(json.dumps([{k: v for k, v in self.agencies[2].items() if k != "profile"}]), encoding="utf-8")
        self.build()
        self.assertEqual(self.glance_rows()[1][-1], NONE_ON_RECORD)

    # -- open data, legacy bytes, refusals -----------------------------------
    def test_public_allowlist_and_legacy_bytes(self):
        self.build()
        plain = self.snapshot()
        agency = self.exported()
        self.assertEqual(set(agency), set(PUBLIC_AGENCY_FIELDS))  # absent reports: the key is omitted
        for name, data in plain.items():
            self.assertNotIn(b"agency-reports", data, name)
            self.assertNotIn(b'"reports"', data, name)
            self.assertNotIn(b"Reports this agency has published", data, name)
        # normalized model: no reports key at all for old profiles
        loaded = load_agencies(self.agencies, "content/agencies.json", BUILD_DAY)
        self.assertTrue(all("reports" not in a for a in loaded))
        entries = [entry(published_on="2026-01-02"), entry(title="Older report", kind="other", period_start="2022-01-01",
                                                           period_end="2022-12-31", source_url="https://example.invalid/older")]
        self.record(entries)
        self.record(entries, SHERIFF)
        self.build()
        reported = self.snapshot()
        self.assertEqual(self.exported()["reports"], entries)
        self.assertEqual(set(self.exported()), set(PUBLIC_AGENCY_FIELDS) | {"reports"})
        self.assertTrue(all(set(r) <= set(PUBLIC_REPORT_FIELDS) for r in self.exported()["reports"]))
        self.assertEqual({n for n in reported if reported[n] != plain.get(n)},
                         {"agencies.html", "agencies/cedar.html", "data/agencies.json"})  # every other file is byte for byte as before
        self.assertEqual(reported["data/agencies.csv"], plain["data/agencies.csv"])  # no CSV column is added
        header = next(csv.reader(io.StringIO(self.read("data/agencies.csv"))))
        self.assertNotIn("reports", header)
        self.assertEqual(set(reported), set(plain))  # no report-only file or page
        text = self.read("data/agencies.json")
        for leaked in ("records_email", "records_url", "portal", "notes", "verification", "mailto:"):
            self.assertNotIn(leaked, text)
        # schema: positive and negative
        doc = json.loads(text)
        law.validate(doc, SCHEMA)
        sample = doc["agencies"][0]["reports"]
        for bad in ([], [{**sample[0], "extra": 1}], [{k: v for k, v in sample[0].items() if k != "checked_on"}],
                    [{**sample[0], "kind": "annual"}], [{**sample[0], "title": ""}],
                    [{**sample[0], "source_url": "http://example.invalid/x"}], [{**sample[0], "period_start": "2025-1-1"}],
                    [{**sample[0], "published_on": None}], [{**sample[0], "published_on": "01/02/2026"}], None, {}):
            with self.subTest(bad=json.dumps(bad)[-90:]):
                broken = json.loads(text)
                broken["agencies"][0]["reports"] = bad
                with self.assertRaises(ValueError):
                    law.validate(broken, SCHEMA)
        # the repo's schema checker has no maxItems / maxLength; the bounds are declared (and the build enforces them)
        declared = SCHEMA["$defs"]
        self.assertEqual((declared["reports"]["minItems"], declared["reports"]["maxItems"]), (1, 20))
        self.assertEqual(declared["report_entry"]["properties"]["title"], {"type": "string", "minLength": 1, "maxLength": 160})
        self.assertEqual(sorted(declared["report_entry"]["required"]), sorted(REPORT))
        self.assertEqual(sorted(declared["report_entry"]["properties"]), sorted(PUBLIC_REPORT_FIELDS + PUBLIC_ARCHIVE_FIELDS))
        self.assertEqual(declared["report_entry"]["properties"]["kind"]["enum"], list(REPORT_KINDS))
        self.assertFalse(declared["report_entry"]["additionalProperties"])
        # a private nested key fails closed before anything is written
        real = Site._public_agency

        def leaky(self_, agency, cities):
            value = real(self_, agency, cities)
            if "reports" in value:
                value["reports"] = [{**value["reports"][0], "contact": SENTINEL}] + value["reports"][1:]
            return value
        before = self.snapshot()
        with mock.patch.object(Site, "_public_agency", leaky):
            with self.assertRaisesRegex(ContentError, "data/agencies.json: non-public key contact"):
                self.build()
        self.assertEqual(self.snapshot(), before)

    def test_bad_reports_preserve_inputs_and_public(self):
        self.record([REPORT])
        self.build()
        before_public = self.snapshot()
        content = self.root / "content"
        self.record([entry(kind="nope")])
        before_inputs = self.snapshot(content)
        with self.assertRaises(ContentError):
            self.build()
        self.assertEqual((self.snapshot(), self.snapshot(content)), (before_public, before_inputs))
        self.path.write_text("{ not json", encoding="utf-8")
        before_inputs = self.snapshot(content)
        with self.assertRaisesRegex(ContentError, "agencies.json: not valid UTF-8 JSON"):
            self.build()
        self.assertEqual((self.snapshot(), self.snapshot(content)), (before_public, before_inputs))
        real = self.tmp / "real-agencies.json"
        real.write_text(json.dumps(self.agencies), encoding="utf-8")
        real_bytes = real.read_bytes()
        for target in (real, self.tmp / "missing.json"):  # a symlink, and a dangling one, are never followed
            self.path.unlink()
            os.symlink(target, self.path)
            with self.subTest(target=target.name):
                before_inputs = self.snapshot(content)
                with self.assertRaisesRegex(ContentError, "agencies.json: symlinks are not read" if target == real else ""):
                    self.build()
                self.assertEqual((self.snapshot(), real.read_bytes()), (before_public, real_bytes))
                self.assertEqual(self.snapshot(content), before_inputs)
        self.path.unlink()
        self.path.write_text(json.dumps(self.agencies), encoding="utf-8")
        original = Path.read_bytes

        def denied(path):
            if path.name == "agencies.json":
                raise PermissionError(13, "Permission denied")
            return original(path)
        before_inputs = self.snapshot(content)
        with mock.patch.object(Path, "read_bytes", denied):
            with self.assertRaises(PermissionError):
                self.build()
        self.assertEqual((self.snapshot(), self.snapshot(content)), (before_public, before_inputs))


if __name__ == "__main__":
    unittest.main()
