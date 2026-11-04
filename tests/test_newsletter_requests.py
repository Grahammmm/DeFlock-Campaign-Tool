"""DF1207-05: newsletter --from-content lists public records requests under "Records requests". Synthetic; nothing is sent."""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter.draft import MAX_MEETINGS, DraftError, build_draft  # noqa: E402
from campaign_tool.newsletter.from_content import manifest_from_content  # noqa: E402
from campaign_tool.site import asked_sentence, build_site  # noqa: E402
from tests.test_newsletter import MANIFEST, NOW as WORKER_NOW  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
SINCE = "2026-10-01T00:00:00+00:00"
CFG = {"name": "Synthetic Campaign", "county": "Synthetic"}
BASE = "https://cedar-records.example.invalid"
POLICE, COUNCIL = "Cedar Police Department (fictional)", "Cedar City Council (fictional)"
PRIVATE = "PRIVATE-KIT-MARKER-7731"
HEADINGS = ("## Records requests", "<h3>Records requests</h3>")  # the no-findings sentence also says "Records requests"


def request(agency, topic, asked_on, state="awaiting_response", updated_on=None, sentence=None, path="/agencies/cedar.html"):
    return {"agency": agency, "topic": topic, "asked_on": asked_on, "updated_on": updated_on, "state": state,
            "sentence": sentence or "No written response recorded as of 2026-10-30.", "path": path}


def manifest(requests, **changes):
    base = {"schema_version": 1, "campaign": {"name": "Synthetic Campaign", "base_url": BASE, "county_name": "Synthetic"},
            "since": SINCE, "findings": [], "meetings": [], "requests": requests}
    return {**base, **changes}


def lines(text):
    return text.split("## Records requests\n", 1)[1].split("\n\n", 1)[0].splitlines()


class RequestsSectionTests(unittest.TestCase):
    def test_item_asked_inside_the_window_is_one_exact_line_in_html_and_text(self):
        draft = build_draft(manifest([request(POLICE, "Data-sharing list", "2026-10-10")]), now=NOW)
        self.assertEqual(lines(draft["text"]), [
            f"- **{POLICE}**: Data-sharing list — No written response recorded as of 2026-10-30. "
            f"[Agency page]({BASE}/agencies/cedar.html)"])
        self.assertIn("<h3>Records requests</h3>", draft["html"])
        self.assertIn(f"<li><strong>{POLICE}</strong>: Data-sharing list — No written response recorded as of 2026-10-30. "
                      f'<a href="{BASE}/agencies/cedar.html" rel="noopener noreferrer">Agency page</a></li>', draft["html"])
        self.assertEqual(draft["counts"]["requests"], 1)

    def test_asked_before_but_updated_inside_is_listed_and_both_before_is_not(self):
        updated = request(POLICE, "Camera count", "2026-08-01", "answered", "2026-10-15", "Answered 2026-10-15.")
        stale = request(COUNCIL, "Old ask", "2026-08-01", "answered", "2026-09-01", "Answered 2026-09-01.")
        draft = build_draft(manifest([updated, stale]), now=NOW)
        self.assertEqual(lines(draft["text"]), [f"- **{POLICE}**: Camera count — Answered 2026-10-15. [Agency page]({BASE}/agencies/cedar.html)"])
        self.assertNotIn("Old ask", draft["text"] + draft["html"])
        none = build_draft(manifest([stale]), now=NOW)
        self.assertNotIn(HEADINGS[0], none["text"])
        self.assertNotIn(HEADINGS[1], none["html"])

    def test_window_edges_since_day_and_now_day_count_the_day_after_now_does_not(self):
        edge = [request(POLICE, "On since day", "2026-10-01"), request(POLICE, "On now day", "2026-10-30"),
                request(POLICE, "Day before since", "2026-09-30"), request(POLICE, "Day after now", "2026-10-31")]
        text = build_draft(manifest(edge), now=NOW)["text"]
        self.assertEqual([entry.split(": ")[1].split(" — ")[0] for entry in lines(text)], ["On since day", "On now day"])

    def test_item_updated_after_now_is_not_listed_even_when_asked_inside(self):
        future = request(POLICE, "Future update", "2026-10-10", "answered", "2026-11-05", "Answered 2026-11-05.")
        draft = build_draft(manifest([future]), now=NOW)
        self.assertNotIn("Future update", draft["text"] + draft["html"])
        self.assertNotIn("requests", draft["counts"])

    def test_without_since_every_item_up_to_now_is_listed(self):
        draft = build_draft(manifest([request(POLICE, "Very old", "2020-01-01")], since=None), now=NOW)
        self.assertEqual(len(lines(draft["text"])), 1)

    def test_link_only_with_base_url_and_never_relative(self):
        items = [request(POLICE, "Data-sharing list", "2026-10-10"),
                 request(COUNCIL, "Contracts", "2026-10-11", path="/agencies.html#cedar-council")]
        bare = build_draft(manifest(items, campaign={"name": "Synthetic Campaign", "base_url": None, "county_name": "S"}), now=NOW)
        for blob in (bare["text"], bare["html"]):
            self.assertIn("Contracts", blob)
            self.assertNotIn("Agency page", blob)
            self.assertNotIn("/agencies", blob)
        linked = build_draft(manifest(items), now=NOW)
        self.assertIn(f"[Agency page]({BASE}/agencies.html#cedar-council)", linked["text"])
        self.assertIn(f'<a href="{BASE}/agencies.html#cedar-council" rel="noopener noreferrer">Agency page</a>', linked["html"])

    def test_no_kept_items_means_no_section_and_a_byte_identical_draft(self):
        base = manifest([], findings=copy.deepcopy(MANIFEST["findings"]), meetings=copy.deepcopy(MANIFEST["meetings"]))
        without = {key: value for key, value in base.items() if key != "requests"}
        for requests in ([], [request(POLICE, "Old", "2020-01-01")]):
            draft = build_draft({**base, "requests": requests}, now=NOW)
            self.assertEqual(draft, build_draft(without, now=NOW))
            self.assertNotIn(HEADINGS[0], draft["text"])
            self.assertNotIn(HEADINGS[1], draft["html"])
            self.assertNotIn("requests", draft["counts"])

    def test_worker_manifest_without_requests_is_byte_identical_and_bad_requests_name_the_field(self):
        before = build_draft(MANIFEST, now=WORKER_NOW)
        self.assertEqual(build_draft({**MANIFEST, "requests": None}, now=WORKER_NOW), before)
        self.assertEqual(build_draft({**MANIFEST, "requests": []}, now=WORKER_NOW), before)
        for bad in ({}, "text", 3):
            with self.assertRaisesRegex(DraftError, "requests must be a list"):
                build_draft({**MANIFEST, "requests": bad}, now=WORKER_NOW)

    def test_sections_sit_after_corrections_and_before_decisions(self):
        findings = [{"title": "Old finding", "summary": "S", "classification": "documented_fact", "confidence": "verified",
                     "path": "/findings/old.html", "published_at": "2026-08-01T00:00:00Z", "corrected": True,
                     "corrections": [{"date": "2026-10-10", "note": "Fixed."}]}]
        decisions = [{"body": "Synthetic Board", "on": "2026-10-06", "result": "denied", "vote": None,
                      "source_url": "https://records.example.invalid/minutes/1", "meeting_path": "/meetings/x.html"}]
        text = build_draft(manifest([request(POLICE, "Ask", "2026-10-10")], findings=findings, decisions=decisions), now=NOW)["text"]
        self.assertLess(text.index("## Corrections"), text.index("## Records requests"))
        self.assertLess(text.index("## Records requests"), text.index("## Decisions"))
        self.assertEqual(build_draft(manifest([request(POLICE, "Ask", "2026-10-10")], decisions=decisions), now=NOW)["counts"],
                         {"findings": 0, "meetings": 0, "requests": 1, "decisions": 1})

    def test_more_than_the_limit_lists_the_limit_then_and_n_more(self):
        items = [request(POLICE, f"Topic {n:02d}", "2026-10-10") for n in range(12)]
        draft = build_draft(manifest(items), now=NOW)
        shown = lines(draft["text"])
        self.assertEqual(len(shown), MAX_MEETINGS + 1)
        self.assertEqual(shown[-1], f"- and {12 - MAX_MEETINGS} more on the site.")
        self.assertEqual(draft["counts"]["requests"], 12)
        self.assertIn("Topic 07", draft["text"])
        self.assertNotIn("Topic 08", draft["text"])

    def test_malformed_items_are_refused_naming_the_entry(self):
        good = request(POLICE, "Ask", "2026-10-10")
        for change, expected in (({"asked_on": "10/10/2026"}, r"requests\[0\]\.asked_on"),
                                 ({"updated_on": "2026-13-40"}, r"requests\[0\]\.updated_on"),
                                 ({"sentence": ""}, r"requests\[0\]: agency, topic and sentence"),
                                 ({"topic": None}, r"requests\[0\]: agency, topic and sentence"),
                                 ({"topic": "<b>bold</b>"}, r"requests\[0\]\.topic"),
                                 ({"agency": "x" * 121}, r"requests\[0\]\.agency: longer than 120"),
                                 ({"sentence": "y" * 201}, r"requests\[0\]\.sentence: longer than 200"),
                                 ({"path": "https://elsewhere.example.invalid/"}, r"requests\[0\]\.path"),
                                 ({"path": "/agencies/../x.html"}, r"requests\[0\]\.path"),
                                 ({"path": "agencies/cedar.html"}, r"requests\[0\]\.path"),
                                 ({"topic": "Call 805-555-0100 about it"}, "contact details")):
            with self.subTest(change=change):
                with self.assertRaisesRegex(DraftError, expected):
                    build_draft(manifest([{**good, **change}]), now=NOW)
        with self.assertRaisesRegex(DraftError, r"requests\[1\]: must be an object"):
            build_draft(manifest([good, "text"]), now=NOW)
        # An item outside the window is validated too, so a bad entry is never silently skipped.
        with self.assertRaisesRegex(DraftError, r"requests\[0\]\.path"):
            build_draft(manifest([{**request(POLICE, "Old", "2020-01-01"), "path": "x"}]), now=NOW)


class RequestsFromContentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-newsletter-requests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root)
        self.path = self.root / "content" / "agencies.json"
        self.agencies = json.loads(self.path.read_text())
        self.agencies[1]["asked"] = [
            {"topic": "ALPR retention policy", "asked_on": "2026-08-01", "state": "answered", "updated_on": "2026-10-15",
             "note": "Private-ish organizer note."},
            {"topic": "Data-sharing list", "asked_on": "2026-10-10", "state": "awaiting_response"},
            {"topic": "Camera count", "asked_on": "2026-09-15", "state": "declined_in_writing", "updated_on": "2026-09-20"}]
        self.agencies[2]["asked"] = [{"topic": "Agenda records", "asked_on": "2026-10-10", "state": "acknowledged"}]
        self.path.write_text(json.dumps(self.agencies))

    def draft(self, since=SINCE):  # since=None: every item up to now
        value = manifest_from_content(self.root, CFG, since, now=NOW)
        return value, build_draft(value, now=NOW)

    def test_manifest_requests_from_public_asked_items_sorted_by_latest_activity(self):
        value, draft = self.draft()
        self.assertEqual([(r["topic"], r["state"], r["sentence"], r["path"]) for r in value["requests"]], [
            ("ALPR retention policy", "answered", "Answered 2026-10-15.", "/agencies/cedar.html"),
            ("Agenda records", "acknowledged", "Acknowledged; no records yet.", "/agencies.html#cedar-city-council"),
            ("Data-sharing list", "awaiting_response", "No written response recorded as of 2026-10-30.", "/agencies/cedar.html"),
            ("Camera count", "declined_in_writing", "Declined in writing on 2026-09-20.", "/agencies/cedar.html")])
        self.assertEqual(set(value["requests"][0]), {"agency", "topic", "asked_on", "updated_on", "state", "sentence", "path"})
        # Camera count is before the window; the other three are listed.
        self.assertEqual(len(lines(draft["text"])), 3)
        self.assertNotIn("Camera count", draft["text"])
        self.assertIn(f"[Agency page]({BASE}/agencies.html#cedar-city-council)", draft["text"])
        self.assertNotIn("Private-ish organizer note", draft["text"] + draft["html"] + json.dumps(value))

    def test_sentence_is_the_one_the_agency_page_shows(self):
        build_site(self.root, today=date(2026, 10, 30))
        page = (self.root / "public" / "agencies.html").read_text()
        for item in self.agencies[1]["asked"] + self.agencies[2]["asked"]:
            sentence = asked_sentence(item, date(2026, 10, 30))
            self.assertIn(sentence, page)
        value, _ = self.draft(None)
        self.assertIn("No written response recorded as of 2026-10-30.", [r["sentence"] for r in value["requests"]])

    def test_nothing_from_the_private_kit_appears(self):
        kit = self.root / "kit"
        kit.mkdir(exist_ok=True)
        (kit / "sent.json").write_text(json.dumps({"issues": [{"notes": PRIVATE, "bases": [PRIVATE + "-base"]}]}))
        (kit / "newsletter-sent.json").write_text(json.dumps([{"sent_on": "2026-09-01", "note": PRIVATE}]))
        (kit / "requests.json").write_text(json.dumps([{"agency": PRIVATE, "topic": PRIVATE, "asked_on": "2026-10-10"}]))
        value, draft = self.draft()
        for blob in (json.dumps(value), draft["text"], draft["html"], json.dumps(draft)):
            self.assertNotIn(PRIVATE, blob)
        self.assertEqual(len(value["requests"]), 4)

    def test_no_asked_items_means_no_section(self):
        for agency in self.agencies:
            agency.pop("asked", None)
        self.path.write_text(json.dumps(self.agencies))
        value, draft = self.draft()
        self.assertEqual(value["requests"], [])
        self.assertNotIn(HEADINGS[0], draft["text"])
        self.assertNotIn(HEADINGS[1], draft["html"])


if __name__ == "__main__":
    unittest.main()
