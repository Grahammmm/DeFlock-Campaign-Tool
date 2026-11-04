"""Synthetic offline acceptance of the public-only campaign timeline."""
import copy
import json
import random
import re
import shutil
import tempfile
import unittest
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from campaign_tool.cli import read_config
from campaign_tool.content import ContentError, load_content
from campaign_tool.site import Site, build_site
from campaign_tool.timeline import LEAD, render_timeline, timeline_events
from tests.test_site_build import EXPECTED_CSP

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
TODAY = date(2026, 2, 15)


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


class TimelineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="synthetic-timeline-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.finding_path = self.root / "content/findings/cedar-policy-posted.json"
        finding = json.loads(self.finding_path.read_text())
        finding["title"] = "First finding"
        finding["corrections"] = [{"date": "2026-02-03", "note": "First correction"},
                                  {"date": "2026-02-04", "note": "Second correction"}]
        self.write("content/findings/cedar-policy-posted.json", finding)
        second = dict(finding, id="second", slug="second", title="Second finding",
                      published_at="2026-02-02T12:00:00Z", corrections=[])
        self.write("content/findings/second.json", second)
        shutil.rmtree(self.root / "content/meetings")
        for slug, starts in (("past", "2026-01-10"), ("future", "2026-03-01")):
            self.write(f"content/meetings/{slug}.json", {"id": slug, "body": slug.title() + " body",
                                                       "starts_at": starts + "T12:00:00-08:00"})

    def write(self, path, doc):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc), encoding="utf-8")

    def model(self):
        model = load_content(self.root)
        assert model is not None
        return model

    def site(self, model=None):
        return Site(self.root, read_config(self.root), model or self.model(), today=TODAY)

    def build(self):
        return build_site(self.root, today=TODAY)

    def page(self):
        return (self.root / "public/timeline.html").read_text()

    def test_exact_order_text_links_and_month_groups(self):
        events = timeline_events(self.model(), TODAY)
        self.assertEqual(events, [
            {"date": "2026-03-01", "kind": "meeting", "text": "Scheduled meeting: Future body",
             "href": "/meetings.html#meeting-future"},
            {"date": "2026-02-04", "kind": "correction", "text": "Correction to First finding",
             "href": "/findings/cedar-policy-posted.html#corrections"},
            {"date": "2026-02-03", "kind": "correction", "text": "Correction to First finding",
             "href": "/findings/cedar-policy-posted.html#corrections"},
            {"date": "2026-02-02", "kind": "finding_published", "text": "Finding published: Second finding",
             "href": "/findings/second.html"},
            {"date": "2026-02-01", "kind": "finding_published", "text": "Finding published: First finding",
             "href": "/findings/cedar-policy-posted.html"},
            {"date": "2026-01-22", "kind": "records_received",
             "text": "Records received from Cedar Police Department (fictional): Synthetic Cedar Police vendor agreement excerpt",
             "href": "/sources.html#cedar-police-records"},
            {"date": "2026-01-20", "kind": "records_received",
             "text": "Records received from Cedar Police Department (fictional): Synthetic Cedar Police ALPR policy",
             "href": "/sources.html#cedar-police-records"},
            {"date": "2026-01-10", "kind": "meeting", "text": "Meeting: Past body",
             "href": "/meetings.html#meeting-past"},
        ])
        self.build()
        parsed = Tags(self.page())
        self.assertEqual(sum(tag == "li" for tag, _ in parsed.tags), 8)
        self.assertEqual(sum(tag == "h3" for tag, _ in parsed.tags), 3)
        self.assertIn(LEAD, self.page())
        for month in ("March 2026", "February 2026", "January 2026"):
            self.assertIn(month, parsed.text)
        for event in events:
            path, _, fragment = event["href"].partition("#")
            target = (self.root / "public" / path.lstrip("/")).read_text()
            if fragment:
                self.assertIn(fragment, [a.get("id") for _, a in Tags(target).tags])
        self.assertIn(("a", {"href": "/timeline.html", "aria-current": "page"}), parsed.tags)

    def test_shuffle_is_byte_identical_including_same_href_ties(self):
        model = self.model()
        model["sources"][1]["received_at"] = model["sources"][0]["received_at"]
        expected = self.site(model).timeline_html()["html"]
        for seed in range(4):
            shuffled = copy.deepcopy(model)
            rng = random.Random(seed)
            for key in ("agencies", "sources", "findings", "meetings"):
                rng.shuffle(shuffled[key])
            for finding in shuffled["findings"]:
                rng.shuffle(finding["corrections"])
            self.assertEqual(self.site(shuffled).timeline_html()["html"], expected)
        self.build()
        before = self.page()
        with patch("campaign_tool.site.load_content", return_value=copy.deepcopy(model)):
            self.build()
        tied_page = self.page()
        model["sources"].reverse()
        with patch("campaign_tool.site.load_content", return_value=model):
            self.build()
        self.assertEqual(self.page(), tied_page)
        self.assertNotEqual(before, tied_page)

    def test_same_day_kind_then_href_and_correction_after_publication(self):
        model = self.model()
        for source in model["sources"]:
            source["received_at"] = "2026-02-01"
        model["sources"][1]["fragment"] = "aaa"
        for finding in model["findings"]:
            finding["published_at"] = "2026-02-01T12:00:00Z"
            for correction in finding["corrections"]:
                correction["date"] = "2026-02-01"
        for meeting in model["meetings"]:
            meeting["starts_at"] = "2026-02-01T01:00:00-08:00"
        events = timeline_events(model, TODAY)
        self.assertEqual([e["kind"] for e in events],
                         ["records_received"] * 2 + ["finding_published"] * 2 + ["correction"] * 2 + ["meeting"] * 2)
        self.assertEqual(events[0]["href"], "/sources.html#aaa")
        self.assertEqual(events[2]["href"], "/findings/cedar-policy-posted.html")

    def test_unknown_agency_and_missing_date_never_default_to_today(self):
        model = self.model()
        model["sources"][0]["agency_id"] = "ca-unknown-secret-id"
        del model["sources"][1]["received_at"]
        events = timeline_events(model, TODAY)
        sources = [e for e in events if e["kind"] == "records_received"]
        self.assertEqual(len(sources), 1)
        self.assertTrue(sources[0]["text"].startswith("Records received from an agency: "))
        self.assertNotIn("ca-unknown", render_timeline(self.site(), events))
        raw = json.loads((self.root / "content/sources.json").read_text())
        del raw[0]["received_at"]
        self.write("content/sources.json", raw)
        with self.assertRaises(ContentError):
            self.build()

    def test_cancelled_omitted_today_scheduled_past_never_claimed_held(self):
        model = self.model()
        model["meetings"][1]["status"] = "cancelled"
        self.assertFalse(any(e["href"].endswith("future") for e in timeline_events(model, TODAY)))
        model["meetings"][0]["starts_at"] = "2026-02-15T23:00:00-08:00"
        event = next(e for e in timeline_events(model, TODAY) if e["kind"] == "meeting")
        self.assertEqual(event["text"], "Scheduled meeting: Past body")

    def test_unpublished_attorney_and_injected_tags_refuse_without_overwrite(self):
        self.build()
        before = {p.relative_to(self.root / "public"): p.read_bytes()
                  for p in (self.root / "public").rglob("*") if p.is_file()}
        original = json.loads(self.finding_path.read_text())
        for key, value in (("state", "draft"), ("confidence", "needs_attorney_review"),
                           ("title", '<img src=x onerror="G APPROVED">')):
            with self.subTest(key=key):
                self.write("content/findings/cedar-policy-posted.json", dict(original, **{key: value}))
                with self.assertRaises(ContentError):
                    self.build()
                self.assertEqual(before, {p.relative_to(self.root / "public"): p.read_bytes()
                                         for p in (self.root / "public").rglob("*") if p.is_file()})

    def test_injection_is_text_not_tags_or_attributes(self):
        payload = 'G APPROVED & " onclick="javascript:alert(1)'
        model = self.model()
        for source in model["sources"]:
            source["title"] = payload
        for finding in model["findings"]:
            finding["title"] = payload
        for meeting in model["meetings"]:
            meeting["body"] = payload
        model["agencies"][0]["name"] = payload
        page = self.site(model).timeline_html()["html"]
        parsed = Tags(page)
        self.assertIn('&amp; &quot; onclick=&quot;javascript:', page)
        self.assertTrue(any(payload in text for text in parsed.text))
        for tag, attrs in parsed.tags:
            self.assertNotIn(tag, ("script", "img", "iframe"))
            self.assertFalse(any(key.startswith("on") for key in attrs))
            self.assertFalse(any(str(value).startswith("javascript:") for value in attrs.values()))

    def test_private_sentinels_ignored_headers_unchanged_and_sitemap(self):
        self.build()
        headers = (self.root / "public/_headers").read_bytes()
        csp = self.site().csp()
        self.assertEqual(csp, EXPECTED_CSP)
        sentinel = "SYNTHETIC-PRIVATE-SENTINEL-DO-NOT-PUBLISH"
        for path in ("kit/sent.json", "private/notes.json", "private/ledger.sqlite3"):
            self.write(path, {"secret": sentinel})
        self.build()
        for path in (self.root / "public").rglob("*"):
            if path.is_file():
                self.assertNotIn(sentinel.encode(), path.read_bytes())
        self.assertEqual(headers, (self.root / "public/_headers").read_bytes())
        self.assertIn("/timeline.html</loc>", (self.root / "public/sitemap.xml").read_text())
        site = json.loads((self.root / "content/site.json").read_text())
        site["base_url"] = None
        self.write("content/site.json", site)
        self.build()
        self.assertFalse((self.root / "public/sitemap.xml").exists())
        self.assertNotIn('href="/sitemap.xml"', self.page())

    def test_empty_content_and_no_private_event_fields(self):
        model = self.model()
        for key in ("sources", "findings", "meetings"):
            model[key] = []
        self.assertEqual(timeline_events(model, TODAY), [])
        self.assertIn("Nothing dated has been published yet.", self.site(model).timeline_html()["html"])
        events = timeline_events(self.model(), TODAY)
        for event in events:
            self.assertEqual(set(event), {"date", "kind", "text", "href"})
        page = self.site().timeline_html()["html"]
        for forbidden in ("cafecafe", "beefbeef", "page:1-2", "fnd_000", "private/", "@"):
            self.assertNotIn(forbidden, page)

    # --- DF1207-04: request and decision dates ---------------------------------------------------------

    def ask(self, index, items):
        path = self.root / "content/agencies.json"
        agencies = json.loads(path.read_text())
        agencies[index]["asked"] = items
        self.write("content/agencies.json", agencies)

    def outcome(self, slug, **changes):
        path = self.root / f"content/meetings/{slug}.json"
        doc = json.loads(path.read_text())
        doc["outcome"] = {"result": "approved", "on": "2026-01-10", "source_url": "https://records.example.org/min?a=1&b=2",
                          "verified_by": "Synthetic Organizer", "verified_at": "2026-02-15", **changes}
        self.write(f"content/meetings/{slug}.json", doc)

    def of_kind(self, events, *kinds):
        return [e for e in events if e["kind"] in kinds]

    def test_asked_item_adds_requested_and_update_events_with_agency_page_sentence(self):
        self.ask(1, [{"topic": "ALPR policy", "asked_on": "2026-01-05", "state": "declined_in_writing",
                      "updated_on": "2026-01-25"}])
        self.build()
        site = self.site()
        events = self.of_kind(site.timeline, "records_requested", "request_update")
        self.assertEqual(events, [
            {"date": "2026-01-25", "kind": "request_update", "href": "/agencies/cedar.html",
             "text": "Cedar Police Department (fictional): ALPR policy — Declined in writing on 2026-01-25."},
            {"date": "2026-01-05", "kind": "records_requested", "href": "/agencies/cedar.html",
             "text": "Records requested from Cedar Police Department (fictional): ALPR policy"}])
        self.assertIn("ALPR policy — asked 2026-01-05. Declined in writing on 2026-01-25.",
                      (self.root / "public/agencies.html").read_text())
        self.assertIn(LEAD, self.page())
        self.assertNotIn("not shown here yet", self.page())

    def test_awaiting_without_update_adds_only_the_requested_event(self):
        self.ask(1, [{"topic": "Sharing list", "asked_on": "2026-01-05", "state": "awaiting_response"}])
        events = self.of_kind(self.site().timeline, "records_requested", "request_update")
        self.assertEqual([e["kind"] for e in events], ["records_requested"])
        self.assertNotIn(TODAY.isoformat(), [e["date"] for e in self.site().timeline])
        self.ask(1, [{"topic": "Sharing list", "asked_on": "2026-01-05", "state": "awaiting_response",
                      "updated_on": "2026-01-09"}])
        events = self.of_kind(self.site().timeline, "request_update")
        self.assertEqual((events[0]["date"], events[0]["text"][-38:]),
                         ("2026-01-09", "No written response recorded as of 2026-01-09."[-38:]))

    def test_agency_without_page_links_to_existing_agencies_list_anchor(self):
        self.ask(2, [{"topic": "Camera count", "asked_on": "2026-01-05", "state": "answered", "updated_on": "2026-01-06"}])
        self.build()
        events = self.of_kind(self.site().timeline, "records_requested", "request_update")
        self.assertEqual({e["href"] for e in events}, {"/agencies.html#" + events[0]["href"].partition("#")[2]})
        path, _, fragment = events[0]["href"].partition("#")
        self.assertEqual(path, "/agencies.html")
        target = (self.root / "public/agencies.html").read_text()
        self.assertIn(fragment, [a.get("id") for _, a in Tags(target).tags])
        self.assertIn(f'href="{events[0]["href"]}"', self.page())

    def test_decision_event_only_for_non_cancelled_meeting_with_outcome(self):
        self.outcome("past")
        events = self.of_kind(self.site().timeline, "decision")
        self.assertEqual(events, [{"date": "2026-01-10", "kind": "decision", "text": "Decision at Past body: Approved",
                                   "href": "/meetings/past.html",
                                   "source_url": "https://records.example.org/min?a=1&b=2"}])
        model = self.model()
        next(m for m in model["meetings"] if m["id"] == "past")["status"] = "cancelled"
        self.assertEqual(self.of_kind(timeline_events(model, TODAY), "decision"), [])
        self.assertFalse(any(e["kind"] in ("meeting", "decision") and "Past" in e["text"]
                             for e in timeline_events(model, TODAY)))
        model = self.model()
        next(m for m in model["meetings"] if m["id"] == "past").pop("outcome")
        self.assertEqual(self.of_kind(timeline_events(model, TODAY), "decision"), [])

    def test_decision_item_has_two_links_and_escapes_text(self):
        self.outcome("past", result="denied")
        model = self.model()
        next(m for m in model["meetings"] if m["id"] == "past")["body"] = "Board <i>& Co"
        page = self.site(model).timeline_html()["html"]
        match = re.search(r"<li><time[^>]*>2026-01-10</time> · <a href=\"/meetings/past.html\">.*?</li>", page)
        self.assertIsNotNone(match)
        item = match.group(0)
        self.assertEqual(item.count("<a "), 2)
        self.assertIn("Decision at Board &lt;i&gt;&amp; Co: Denied</a>", item)
        self.assertIn(' · <a href="https://records.example.org/min?a=1&amp;b=2" rel="noopener noreferrer">official record</a></li>',
                      item)
        self.assertEqual(page.count("official record"), 1)

    def test_requested_topic_is_escaped(self):
        self.ask(1, [{"topic": "Fees & rates", "asked_on": "2026-01-05", "state": "answered"}])
        page = self.site().timeline_html()["html"]
        self.assertIn("Records requested from Cedar Police Department (fictional): Fees &amp; rates</a>", page)

    def test_same_day_request_finding_decision_order_and_builds_identical(self):
        self.ask(1, [{"topic": "T", "asked_on": "2026-02-02", "state": "answered", "updated_on": "2026-02-02"}])
        self.outcome("past", on="2026-02-02")
        events = timeline_events(self.model(), TODAY)
        same_day = [e["kind"] for e in events if e["date"] == "2026-02-02"]
        self.assertEqual(same_day, ["finding_published", "records_requested", "request_update", "decision"])
        self.build()
        first = self.page()
        self.build()
        self.assertEqual(self.page(), first)
        model = self.model()
        expected = self.site(model).timeline_html()["html"]
        rng = random.Random(7)
        for _ in range(3):
            shuffled = copy.deepcopy(model)
            for key in ("agencies", "sources", "findings", "meetings"):
                rng.shuffle(shuffled[key])
            self.assertEqual(self.site(shuffled).timeline_html()["html"], expected)


if __name__ == "__main__":
    unittest.main()
