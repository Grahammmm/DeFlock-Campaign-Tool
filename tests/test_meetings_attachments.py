"""DF2022-13: attachment links on matched meetings and matters (recorded synthetic JSON; never fetched)."""
import json
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.meeting_stage import stage  # noqa: E402
from campaign_tool.meetings import (LegistarClient, MeetingsError, compare_meetings, comment_kit_md,  # noqa: E402
                                    fixture_opener, relevant_events, relevant_matters, write_meetings)
from campaign_tool.site import build_site  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "meetings" / "legistar-attachments.json"
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SINCE = date(2026, 1, 1)
BASE = "https://examplecity.legistar.com/View.ashx?M=F&ID="


def recorded():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def client(mapping, urls):
    inner = fixture_opener(mapping)
    return LegistarClient("examplecity", opener=lambda u: (urls.append(u), inner(u))[1])


def meetings(mapping=None, urls=None):
    urls = [] if urls is None else urls
    return relevant_events(client(mapping or recorded(), urls), since=SINCE)


class Attachments(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")))

    def test_matched_items_only_deduped_and_unsafe_links_dropped(self):
        urls = []
        (meeting,) = meetings(urls=urls)
        self.assertEqual(meeting["attachments"], [
            {"name": "Staff report", "url": BASE + "2&GUID=A"},
            {"name": "Vendor bagreement/b", "url": BASE + "3&GUID=B"},
            {"name": "Attachment", "url": "https://legistar2.granicus.com/examplecity/attachments/policy.pdf"}])
        self.assertEqual(meeting["attachments_dropped"], 3)
        self.assertNotIn("Unmatched", json.dumps(meeting))
        self.assertTrue(all(urllib.parse.urlsplit(u).hostname == "webapi.legistar.com" for u in urls), urls)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(urls[1]).query), {"Attachments": ["1"]})

    def test_host_rule(self):
        def kept(url):
            mapping = recorded()
            mapping["/v1/examplecity/events/3001/eventitems"] = [{"EventItemTitle": "ALPR", "EventItemMatterAttachments": [
                {"MatterAttachmentName": "x", "MatterAttachmentHyperlink": url}]}]
            return meetings(mapping)[0]["attachments"]
        self.assertTrue(kept("https://a.b.legistar.com/v.ashx"))
        self.assertTrue(kept("https://legistar.granicus.com/v.pdf"))
        for bad in ("https://legistar.com.evil.example/x", "https://evillegistar.com/x", "https://x.legistar.com@evil.example/x",
                    "https://user@x.legistar.com/x", "https://x.legistar.com:8443/x", "https://legistarx.granicus.com/x",
                    "https://x.legistar.com/a b", "https://x.legistar.com/\"onclick", "ftp://x.legistar.com/x", ""):
            self.assertEqual(kept(bad), [], bad)

    def test_cap_name_cleaning(self):
        mapping = recorded()
        mapping["/v1/examplecity/events/3001/eventitems"] = [{"EventItemTitle": "ALPR", "EventItemMatterAttachments": [
            {"MatterAttachmentName": ("<b>n</b> [x] " + "y" * 500), "MatterAttachmentHyperlink": f"{BASE}0"}] + [
            {"MatterAttachmentName": f"a{i}", "MatterAttachmentHyperlink": f"{BASE}{i + 1}"} for i in range(24)]}]
        found = meetings(mapping)[0]["attachments"]
        self.assertEqual(len(found), 20)
        self.assertLessEqual(len(found[0]["name"]), 200)
        self.assertTrue(all(c not in found[0]["name"] for c in "<>[]"))

    def test_matters_and_non_int_id(self):
        urls = []
        (matter,) = relevant_matters(client(recorded(), urls), since=SINCE)
        self.assertEqual(matter["attachments"], [{"name": "Contract", "url": BASE + "9&GUID=C"}])
        self.assertEqual(matter["attachments_dropped"], 1)
        urls.clear()
        with self.assertRaises(MeetingsError):
            client(recorded(), urls).matter_attachments("601")
        self.assertEqual(urls, [])

    def test_no_attachments_and_unrecorded_matter_attachments(self):
        mapping = recorded()
        del mapping["/v1/examplecity/matters/601/attachments"]
        self.assertEqual(relevant_matters(client(mapping, []), since=SINCE)[0]["attachments"], [])

    def test_attachments_alone_are_not_agenda_changed(self):
        (new,) = meetings()
        old = {**new, "attachments": []}
        document = {"schema_version": 1, "client": "examplecity", "meetings": [old]}
        self.assertEqual(compare_meetings(document, [new], "examplecity", SINCE)["agenda_changed"], [])
        document["meetings"] = [{**old, "agenda_item": "changed"}]
        self.assertEqual(compare_meetings(document, [new], "examplecity", SINCE)["agenda_changed"], [new["id"]])

    def test_comment_kit_section_and_unchanged_without_attachments(self):
        (meeting,) = meetings()
        text = comment_kit_md(meeting, [], "Campaign")
        self.assertIn("## Read first\n- [Staff report](" + BASE + "2&GUID=A)\n", text)
        self.assertLess(text.index("## Read first"), text.index("## Sources"))
        self.assertIn("read them before you speak.", text)
        bare = {k: v for k, v in meeting.items() if k not in ("attachments", "attachments_dropped")}
        self.assertEqual(comment_kit_md({**bare, "attachments": []}, [], "Campaign"), comment_kit_md(bare, [], "Campaign"))
        self.assertNotIn("Read first", comment_kit_md(bare, [], "Campaign"))

    def test_staged_and_built_site_links(self):
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-attachments-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "campaign"
        shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
        (meeting,) = meetings()
        write_meetings(root, [meeting], "examplecity")
        with mock.patch("sys.stdout"):
            stage(root, meeting["id"], today=date(2026, 10, 1))
        staged = json.loads((root / "content" / "meetings" / f"{meeting['id']}.json").read_text(encoding="utf-8"))
        self.assertIn("## Read first", staged["comment_kit_md"])
        build_site(root, today=date(2026, 10, 1))
        page = (root / "public" / "meetings" / f"{meeting['id']}.html").read_text(encoding="utf-8")
        self.assertEqual(page.count('href="' + BASE.replace("&", "&amp;")), 2)


if __name__ == "__main__":
    unittest.main()
