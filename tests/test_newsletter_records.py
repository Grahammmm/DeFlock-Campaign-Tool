"""DF1422-15: newsletter --from-content lists new library entries under "New in the records library". Synthetic; nothing is sent."""
import copy
import json
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter.draft import MAX_FINDINGS, DraftError, build_draft  # noqa: E402
from campaign_tool.newsletter.from_content import manifest_from_content  # noqa: E402
from tests.test_newsletter import MANIFEST, NOW as WORKER_NOW  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
SINCE = "2026-10-01T00:00:00+00:00"
CFG = {"name": "Synthetic Campaign", "county": "Synthetic"}
BASE = "https://cedar-records.example.invalid"
POLICE = "Cedar Police Department (fictional)"
HEADINGS = ("## New in the records library", "<h3>New in the records library</h3>")
FULL = "abcdefabcdef" + "0" * 52


def record(title, received_at, prefix="abcdefabcdef", agency=POLICE, public_url=None, path="/sources.html#cedar-police-records"):
    return {"title": title, "agency": agency, "received_at": received_at, "prefix": prefix, "public_url": public_url, "path": path}


def manifest(records, **changes):
    base = {"schema_version": 1, "campaign": {"name": "Synthetic Campaign", "base_url": BASE, "county_name": "Synthetic"},
            "since": SINCE, "findings": [], "meetings": [], "records": records}
    return {**base, **changes}


def lines(text):
    return text.split("## New in the records library\n", 1)[1].split("\n\n", 1)[0].splitlines()


class RecordsSectionTests(unittest.TestCase):
    def test_entry_in_the_window_is_one_exact_line_in_html_and_text_with_prefix_only(self):
        item = record("ALPR policy", "2026-10-10T16:30:00+00:00", public_url="https://records.example.invalid/policy.pdf")
        draft = build_draft(manifest([item]), now=NOW)
        self.assertEqual(lines(draft["text"]), [
            f"- **ALPR policy** ({POLICE}), received 2026-10-10, record abcdefabcdef "
            f"[Public copy](https://records.example.invalid/policy.pdf) [Sources page]({BASE}/sources.html#cedar-police-records)"])
        self.assertIn(f"<li><strong>ALPR policy</strong> ({POLICE}), received 2026-10-10, record abcdefabcdef "
                      '<a href="https://records.example.invalid/policy.pdf" rel="noopener noreferrer">Public copy</a> '
                      f'<a href="{BASE}/sources.html#cedar-police-records" rel="noopener noreferrer">Sources page</a></li>', draft["html"])
        self.assertEqual(draft["counts"]["records"], 1)
        self.assertIn(HEADINGS[1], draft["html"])
        for blob in (draft["text"], draft["html"], json.dumps(draft)):
            self.assertIsNone(re.search(r"[0-9a-f]{64}", blob))

    def test_window_edges_day_before_since_is_out_since_day_and_now_day_are_in_day_after_now_is_out(self):
        edge = [record("Day before since", "2026-09-30T23:59:59+00:00"), record("On since day", "2026-10-01T00:00:00+00:00"),
                record("On now day", "2026-10-30T23:00:00+00:00"), record("Day after now", "2026-10-31T00:00:00+00:00")]
        text = build_draft(manifest(edge), now=NOW)["text"]
        self.assertEqual([entry.split("**")[1] for entry in lines(text)], ["On since day", "On now day"])

    def test_without_since_every_entry_up_to_now_is_listed(self):
        draft = build_draft(manifest([record("Very old", "2020-01-01T00:00:00Z")], since=None), now=NOW)
        self.assertEqual(len(lines(draft["text"])), 1)

    def test_public_copy_link_only_when_set_and_sources_link_only_with_base_url_never_relative(self):
        item = record("Private only", "2026-10-10T00:00:00+00:00")
        bare = build_draft(manifest([item], campaign={"name": "Synthetic Campaign", "base_url": None, "county_name": "S"}), now=NOW)
        for blob in (bare["text"], bare["html"]):
            self.assertIn("Private only", blob)
            self.assertNotIn("Public copy", blob)
            self.assertNotIn("Sources page", blob)
            self.assertNotIn("/sources.html", blob)
        linked = build_draft(manifest([item]), now=NOW)
        self.assertNotIn("Public copy", linked["text"] + linked["html"])
        self.assertIn(f"[Sources page]({BASE}/sources.html#cedar-police-records)", linked["text"])

    def test_no_kept_entries_means_no_section_and_a_byte_identical_draft(self):
        base = manifest([], findings=copy.deepcopy(MANIFEST["findings"]), meetings=copy.deepcopy(MANIFEST["meetings"]))
        without = {key: value for key, value in base.items() if key != "records"}
        for records in ([], [record("Old", "2020-01-01T00:00:00Z"), record("Future", "2027-01-01T00:00:00Z")]):
            draft = build_draft({**base, "records": records}, now=NOW)
            self.assertEqual(draft, build_draft(without, now=NOW))
            self.assertNotIn(HEADINGS[0], draft["text"])
            self.assertNotIn(HEADINGS[1], draft["html"])
            self.assertNotIn("records", draft["counts"])

    def test_worker_manifest_without_records_is_byte_identical_and_bad_records_are_refused(self):
        before = build_draft(MANIFEST, now=WORKER_NOW)
        self.assertEqual(build_draft({**MANIFEST, "records": None}, now=WORKER_NOW), before)
        self.assertEqual(build_draft({**MANIFEST, "records": []}, now=WORKER_NOW), before)
        for bad in ({}, "text", 3):
            with self.assertRaisesRegex(DraftError, "records must be a list"):
                build_draft({**MANIFEST, "records": bad}, now=WORKER_NOW)
        good = record("Ask", "2026-10-10T00:00:00Z")
        for prefix in ("ABC", "ABCDEFABCDEF", "abcdefabcde", "abcdefabcdef7", FULL, None):
            with self.subTest(prefix=prefix):
                with self.assertRaisesRegex(DraftError, r"records\[0\]\.prefix"):
                    build_draft(manifest([{**good, "prefix": prefix}]), now=NOW)
        for change, expected in (({"received_at": "10/10/2026"}, r"records\[0\]\.received_at"),
                                 ({"title": ""}, r"records\[0\]: title and agency"),
                                 ({"title": "t" * 161}, r"records\[0\]\.title: longer than 160"),
                                 ({"agency": "a" * 121}, r"records\[0\]\.agency: longer than 120"),
                                 ({"title": "Mail me at a@b.example.invalid"}, "email"),
                                 ({"public_url": "http://insecure.example.invalid/x"}, r"records\[0\]\.public_url"),
                                 ({"path": "https://elsewhere.example.invalid/"}, r"records\[0\]\.path")):
            with self.subTest(change=change):
                with self.assertRaisesRegex(DraftError, expected):
                    build_draft(manifest([{**good, **change}]), now=NOW)
        # An entry outside the window is validated too, so a bad entry is never silently skipped.
        with self.assertRaisesRegex(DraftError, r"records\[0\]\.prefix"):
            build_draft(manifest([record("Old", "2020-01-01T00:00:00Z", prefix="ABC")]), now=NOW)

    def test_section_sits_after_records_requests_and_before_decisions(self):
        requests = [{"agency": POLICE, "topic": "Ask", "asked_on": "2026-10-10", "updated_on": None, "state": "awaiting_response",
                     "sentence": "No written response recorded as of 2026-10-30.", "path": "/agencies/cedar.html"}]
        decisions = [{"body": "Synthetic Board", "on": "2026-10-06", "result": "denied", "vote": None,
                      "source_url": "https://records.example.invalid/minutes/1", "meeting_path": "/meetings/x.html"}]
        draft = build_draft(manifest([record("Doc", "2026-10-10T00:00:00Z")], requests=requests, decisions=decisions), now=NOW)
        text = draft["text"]
        self.assertLess(text.index("## Records requests"), text.index("## New in the records library"))
        self.assertLess(text.index("## New in the records library"), text.index("## Decisions"))
        self.assertEqual(draft["counts"], {"findings": 0, "meetings": 0, "requests": 1, "records": 1, "decisions": 1})

    def test_more_than_the_limit_lists_the_limit_then_and_n_more_on_the_sources_page(self):
        items = [record(f"Doc {n:02d}", "2026-10-10T00:00:00Z", prefix=f"{n:012x}") for n in range(MAX_FINDINGS + 1)]
        draft = build_draft(manifest(items), now=NOW)
        shown = lines(draft["text"])
        self.assertEqual(len(shown), MAX_FINDINGS + 1)
        self.assertEqual(MAX_FINDINGS, 12)
        self.assertEqual(shown[-1], "- and 1 more on the sources page.")
        self.assertEqual(draft["counts"]["records"], 13)
        self.assertIn("Doc 11", draft["text"])
        self.assertNotIn("Doc 12", draft["text"])


class RecordsFromContentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-newsletter-records-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root)
        self.path = self.root / "content" / "sources.json"
        self.sources = [
            {"sha256": "aaaaaaaaaaaa" + "1" * 52, "title": "Older record", "agency_id": "ca-cedar-police",
             "received_at": "2026-10-05T09:00:00+00:00", "public_url": None, "fragment": "cedar-police-records", "pages": 3,
             "note": "Organizer note with someone@example.invalid"},
            {"sha256": "bbbbbbbbbbbb" + "2" * 52, "title": "Newer record", "agency_id": "ca-cedar-police",
             "received_at": "2026-10-20T09:00:00+00:00", "public_url": "https://records.example.invalid/newer",
             "fragment": "cedar-police-records", "pages": 1, "public_copy": {"matches": True, "checked_on": "2026-10-21"}},
            {"sha256": "cccccccccccc" + "3" * 52, "title": "Unknown agency record", "agency_id": "ca-nowhere-pd",
             "received_at": "2026-10-20T09:00:00+00:00", "public_url": None, "fragment": "nowhere-records"},
            {"sha256": "dddddddddddd" + "4" * 52, "title": "Before the window", "agency_id": "ca-cedar-police",
             "received_at": "2026-09-30T09:00:00+00:00", "public_url": None, "fragment": "cedar-police-records"}]
        self.path.write_text(json.dumps(self.sources))
        self.agency = next(a["name"] for a in json.loads((self.root / "content" / "agencies.json").read_text())
                           if a["agency_id"] == "ca-cedar-police")

    def draft(self, since=SINCE):
        value = manifest_from_content(self.root, CFG, since, now=NOW)
        return value, build_draft(value, now=NOW)

    def test_manifest_records_are_public_fields_only_newest_first_and_unknown_agency_shows_its_id(self):
        value, draft = self.draft()
        self.assertEqual([(r["title"], r["agency"], r["prefix"], r["path"]) for r in value["records"]], [
            ("Newer record", self.agency, "bbbbbbbbbbbb", "/sources.html#cedar-police-records"),
            ("Unknown agency record", "ca-nowhere-pd", "cccccccccccc", "/sources.html#nowhere-records"),
            ("Older record", self.agency, "aaaaaaaaaaaa", "/sources.html#cedar-police-records"),
            ("Before the window", self.agency, "dddddddddddd", "/sources.html#cedar-police-records")])
        self.assertEqual(set(value["records"][0]), {"title", "agency", "received_at", "prefix", "public_url", "path"})
        self.assertEqual([entry.split("**")[1] for entry in lines(draft["text"])], ["Newer record", "Unknown agency record", "Older record"])
        self.assertIn("(ca-nowhere-pd), received 2026-10-20, record cccccccccccc", draft["text"])
        self.assertIn("[Public copy](https://records.example.invalid/newer)", draft["text"])

    def test_note_and_full_hash_never_reach_the_manifest_or_the_draft(self):
        value, draft = self.draft(None)
        # DF1622-13: findings and meetings now carry their content ids (identity only, never rendered); the hash scan skips those id keys
        public = {**value, "findings": [{k: v for k, v in f.items() if k != "id"} for f in value["findings"]],
                  "meetings": [{k: v for k, v in m.items() if k != "id"} for m in value["meetings"]]}
        for blob in (json.dumps(public), draft["text"], draft["html"], json.dumps(draft)):
            self.assertNotIn("Organizer note", blob)
            self.assertNotIn("someone@example.invalid", blob)
            self.assertIsNone(re.search(r"[0-9a-f]{13,}", blob))

    def test_no_entry_in_the_window_leaves_the_draft_as_it_was_before_this_section(self):
        value, draft = self.draft("2026-10-25T00:00:00+00:00")
        self.assertEqual(len(value["records"]), 4)
        self.assertNotIn(HEADINGS[0], draft["text"])
        self.assertNotIn(HEADINGS[1], draft["html"])
        self.assertEqual(draft, build_draft({key: val for key, val in value.items() if key != "records"}, now=NOW))
        self.path.write_text("[]")
        empty, again = self.draft("2026-10-25T00:00:00+00:00")
        self.assertEqual(empty["records"], [])
        self.assertEqual(again, draft)


if __name__ == "__main__":
    unittest.main()
