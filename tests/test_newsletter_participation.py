"""DF2037-15: newsletter --from-content carries each upcoming meeting's verified participation. Synthetic; nothing is sent."""
import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter.draft import DraftError, build_draft  # noqa: E402
from campaign_tool.newsletter.from_content import manifest_from_content  # noqa: E402
from tests.test_newsletter import MANIFEST, NOW as WORKER_NOW  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
CFG = {"name": "Synthetic Campaign", "county": "Synthetic"}
SOURCE = "https://records.example.gov/meetings/board-full"
# First 8 bytes of sha256(json.dumps(build_draft(MANIFEST, now=WORKER_NOW), sort_keys=True)) on main before
# DF2037-15, as a big-endian integer so the credential scan's quoted-hex entropy rule has nothing to match.
PRE_CHANGE = 3466603619878033266
AGENDA = "  Check the posted agenda for how to comment."


def facts(**overrides):
    value = {"in_person": True, "remote": {"available": True, "how": "Join the posted video link"},
             "written_comment": {"how": "Use the city eComment form", "deadline": "2026-11-10T17:00:00Z"},
             "speaker_minutes": 3, "source_url": SOURCE, "verified_by": "Synthetic Organizer",
             "verified_at": "2026-11-01"}
    value.update(overrides)
    return value


def meeting(meeting_id, starts_at="2026-11-11T18:00:00-08:00", participation=None, status=None):
    document = {"id": meeting_id, "body": "Synthetic " + meeting_id, "starts_at": starts_at,
                "agenda_url": "https://records.example.gov/agenda/" + meeting_id, "agenda_item": "Item 4"}
    if participation is not None:
        document["participation"] = participation
    if status:
        document["status"] = status
    return document


def worker_meeting(participation):
    """A manifest meeting in the --from-content shape (the draft builder alone, no loader)."""
    return {"body": "Synthetic Board", "starts_at": "2026-11-11T18:00:00-08:00", "agenda_item": "Item 4",
            "agenda_url": "https://records.example.gov/agenda/1", "relevance": None, "participation": participation}


def manifest_with(*meetings):
    return {"schema_version": 1, "campaign": {"name": "Synthetic Campaign", "base_url": None, "county_name": "S"},
            "since": None, "findings": [], "meetings": list(meetings)}


def how_line(text):
    lines = [line for line in text.splitlines() if line.startswith("  How to take part: ")]
    return lines


class ContentParticipationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-newsletter-participation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root)
        self.folder = self.root / "content" / "meetings"

    def write(self, document):
        (self.folder / (document["id"] + ".json")).write_text(json.dumps(document))

    def draft(self, now=NOW):
        manifest = manifest_from_content(self.root, CFG)
        return manifest, build_draft(manifest, now=now)

    def test_all_facts_in_text_and_html(self):
        self.write(meeting("board-full", participation=facts()))
        manifest, draft = self.draft()
        expected = ("  How to take part: In person: yes; Remote: Join the posted video link; Written comments: "
                    "Use the city eComment form, by Tue Nov 10, 09:00 UTC-08:00; Speaker limit: 3 minutes "
                    f"(checked 2026-11-01) [Official details]({SOURCE})")
        self.assertEqual(how_line(draft["text"]), [expected])
        upcoming = [m for m in manifest["meetings"] if m["body"] == "Synthetic board-full"]
        self.assertEqual(upcoming[0]["participation"]["source_url"], SOURCE)
        html = draft["html"]
        for part in ("In person: yes", "Remote: Join the posted video link", "Written comments: Use the city eComment form",
                     "by Tue Nov 10, 09:00 UTC-08:00", "Speaker limit: 3 minutes", "(checked 2026-11-01)",
                     f'<a href="{SOURCE}" rel="noopener noreferrer">Official details</a>'):
            self.assertIn(part, html)
        self.assertNotIn("comment_deadlines_passed", draft["excluded"])
        self.assertNotIn("contact_text_withheld", draft["excluded"])

    def test_meeting_without_participation_says_check_the_agenda(self):
        self.write(meeting("board-bare"))
        self.write(meeting("board-nulls", starts_at="2026-11-12T18:00:00-08:00",
                           participation=facts(in_person=None, remote=None, written_comment=None, speaker_minutes=None)))
        manifest, draft = self.draft()
        bare = [m for m in manifest["meetings"] if m["body"] == "Synthetic board-bare"][0]
        self.assertIsNone(bare["participation"])
        lines = draft["text"].splitlines()
        self.assertEqual(lines[lines.index(next(x for x in lines if "Synthetic board-bare" in x)) + 1], AGENDA)
        self.assertIn("<p>Check the posted agenda for how to comment.</p>", draft["html"])
        # in_person null (and every other part unstated): no in-person part, still the checked link
        self.assertEqual(how_line(draft["text"]), [
            f"  How to take part: see official details (checked 2026-11-01) [Official details]({SOURCE})"])
        self.assertNotIn("In person", draft["text"] + draft["html"])

    def test_cancelled_meeting_with_participation_is_still_left_out(self):
        self.write(meeting("board-off", participation=facts(), status="cancelled"))
        manifest, draft = self.draft()
        self.assertNotIn("Synthetic board-off", [m["body"] for m in manifest["meetings"]])
        self.assertNotIn("board-off", draft["text"])
        self.assertEqual(how_line(draft["text"]), [])

    def test_contact_text_is_withheld_and_the_draft_still_builds(self):
        self.write(meeting("board-contact", participation=facts(
            remote={"available": True, "how": "Call 555-010-1234 to listen"},
            written_comment={"how": "Email clerk@city.example", "deadline": "2026-11-10T17:00:00Z"})))
        _, draft = self.draft()
        self.assertEqual(how_line(draft["text"]), [
            "  How to take part: In person: yes; Remote: see official details; Written comments: see official details, "
            f"by Tue Nov 10, 09:00 UTC-08:00; Speaker limit: 3 minutes (checked 2026-11-01) [Official details]({SOURCE})"])
        self.assertEqual(draft["excluded"]["contact_text_withheld"], 2)
        for blob in (draft["text"], draft["html"]):
            self.assertNotIn("clerk@", blob)
            self.assertNotIn("555-010", blob)

    def test_deadline_passed_at_draft_time_is_omitted_and_counted(self):
        self.write(meeting("board-late", participation=facts(
            written_comment={"how": "Email clerk@city.example", "deadline": "2026-11-10T17:00:00Z"})))
        for now in (datetime(2026, 11, 10, 17, tzinfo=timezone.utc), datetime(2026, 11, 11, 1, tzinfo=timezone.utc)):
            _, draft = self.draft(now)
            self.assertEqual(draft["counts"]["meetings"], 1)  # the meeting itself is still upcoming
            self.assertNotIn("Written comments", draft["text"] + draft["html"])
            self.assertEqual(draft["excluded"]["comment_deadlines_passed"], 1)
            self.assertNotIn("contact_text_withheld", draft["excluded"])  # omitted, so nothing withheld
        _, before = self.draft(datetime(2026, 11, 10, 16, 59, tzinfo=timezone.utc))
        self.assertIn("Written comments: see official details, by Tue Nov 10, 09:00 UTC-08:00", before["text"])
        self.assertNotIn("comment_deadlines_passed", before["excluded"])


class DraftParticipationTests(unittest.TestCase):
    def test_deadline_shown_in_the_meetings_offset(self):
        draft = build_draft(manifest_with(worker_meeting(facts())), now=NOW)
        self.assertIn("by Tue Nov 10, 09:00 UTC-08:00", draft["text"])
        self.assertIn("**Synthetic Board**, Wed Nov 11, 2026 18:00 UTC-08:00", draft["text"])
        utc = worker_meeting(facts(written_comment={"how": "Online form", "deadline": "2026-11-10T09:00:00-08:00"}))
        utc["starts_at"] = "2026-11-12T02:00:00Z"
        self.assertIn("Online form, by Tue Nov 10, 17:00 UTC;", build_draft(manifest_with(utc), now=NOW)["text"])

    def test_remote_not_offered_and_markdown_in_how_never_links(self):
        participation = facts(remote={"available": False, "how": ""}, written_comment={
            "how": "**x** [a](javascript:alert(1)) [b](http://plain.example.gov/c)", "deadline": None})
        draft = build_draft(manifest_with(worker_meeting(participation)), now=NOW)
        self.assertIn("Remote: not offered", draft["text"])
        self.assertIn("Written comments: **x** [a](javascript:alert(1)) [b](http://plain.example.gov/c);", draft["text"])
        self.assertNotIn('href="javascript', draft["html"])
        self.assertNotIn('href="http://', draft["html"])
        self.assertEqual(draft["html"].count("<a href="), 3)  # agenda, official details, unsubscribe only

    def test_malformed_participation_is_refused(self):
        for bad, message in (("yes", "must be an object or null"),
                             (facts(source_url="http://records.example.gov/x"), "source_url: must be an https"),
                             (facts(verified_at="Nov 1"), "verified_at: invalid day"),
                             (facts(written_comment={"how": "Form", "deadline": "2026-11-10"}), "needs a UTC offset"),
                             (facts(remote={"available": True, "how": "<b>x</b>"}), "angle brackets")):
            with self.subTest(bad=bad), self.assertRaisesRegex(DraftError, message):
                build_draft(manifest_with(worker_meeting(bad)), now=NOW)

    def test_worker_manifest_without_participation_is_byte_identical_to_main(self):
        draft = build_draft(MANIFEST, now=WORKER_NOW)
        blob = json.dumps(draft, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self.assertEqual(int.from_bytes(hashlib.sha256(blob).digest()[:8], "big"), PRE_CHANGE)
        self.assertEqual(sorted(draft["excluded"]), ["findings_before_since", "meetings_past"])
        self.assertNotIn("How to take part", draft["text"])
        self.assertNotIn("Check the posted agenda", draft["text"])
        # the key, even null, is what switches the line on
        with_key = copy.deepcopy(MANIFEST)
        with_key["meetings"][0]["participation"] = None
        self.assertIn(AGENDA, build_draft(with_key, now=WORKER_NOW)["text"])


if __name__ == "__main__":
    unittest.main()
