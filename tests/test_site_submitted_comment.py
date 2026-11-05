"""DF1407-06: the public copy of a submitted meeting comment on the meeting page, the agency page and data/meetings.json.

Synthetic content only (temp copies of the fictional example); no network.
"""
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool import law
from campaign_tool.content import ContentError
from campaign_tool.labels import DEFAULTS
from campaign_tool.site import PUBLIC_MEETING_FIELDS, PUBLIC_MEETING_OPTIONAL, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "meetings-open-data.schema.json").read_text(encoding="utf-8"))
BUILD_DAY = date(2026, 10, 20)
TEXT = 'Line one <script>alert("x")</script> & **bold** [link](https://x.invalid)\r\n\r\n  indented "quote" \'q\' </pre><h1>x</h1>\n'


def comment(text=TEXT, channel="portal", on="2026-10-10"):
    return {"on": on, "channel": channel, "text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


class SubmittedCommentSiteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-submitted-comment-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.example = self.meetings / "2026-10-cedar-council.json"
        self.public = self.root / "public"

    def edit(self, path=None, **changes):
        path = path or self.example
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")

    def build(self):
        shutil.rmtree(self.public, ignore_errors=True)
        build_site(self.root, today=BUILD_DAY)
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def read(self, name):
        return (self.public / name).read_bytes().decode("utf-8")  # no newline translation

    def test_absent_field_changes_no_byte_and_adds_no_path(self):
        before = self.build()
        self.edit(submitted_comment=comment())
        shown = self.build()
        self.assertEqual(set(shown), set(before))
        changed = sorted(name for name in before if before[name] != shown[name])
        self.assertIn("data/meetings.json", changed)
        self.assertIn("meetings/cedar-council-2026-10.html", changed)
        self.assertTrue(all(n in ("data/meetings.json", "meetings/cedar-council-2026-10.html", "data.html", "data/datapackage.json")
                            for n in changed), changed)
        self.assertTrue(all(b"submitted-comment" not in data and b"Our submitted comment" not in data for data in before.values()))
        self.assertNotIn("submitted_comment", json.dumps(json.loads(before["data/meetings.json"])))

    def test_meeting_page_shows_escaped_literal_text_under_the_exact_heading(self):
        self.edit(submitted_comment=comment())
        self.build()
        page = self.read("meetings/cedar-council-2026-10.html")
        self.assertIn('id="submitted-comment"', page)
        self.assertIn("<h3>Our submitted comment (portal, 2026-10-10)</h3>", page)
        escaped = TEXT.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.assertIn(escaped.replace('"', "&quot;").replace("'", "&#x27;"), page.replace("&#39;", "&#x27;"))
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<h1>x</h1>", page)
        self.assertNotIn("<strong>bold</strong>", page)
        self.assertNotIn('href="https://x.invalid"', page)

    def test_every_channel_names_itself_in_the_heading(self):
        for channel in ("email", "portal", "mail", "in_person"):
            self.edit(submitted_comment=comment(channel=channel, on="2026-10-12"))
            self.build()
            self.assertIn(f"Our submitted comment ({channel}, 2026-10-12)", self.read("meetings/cedar-council-2026-10.html"))

    def test_agency_page_links_only_a_past_meeting_that_has_a_comment(self):
        self.edit(agency_id="ca-cedar-police")
        self.build()
        without = self.read("agencies/cedar.html")
        self.assertIn("Cedar City Council (fictional)", without)
        self.assertNotIn("#submitted-comment", without)
        self.edit(submitted_comment=comment())
        self.build()
        with_comment = self.read("agencies/cedar.html")
        link = '<a href="/meetings/cedar-council-2026-10.html#submitted-comment">Our submitted comment</a>'
        self.assertEqual(with_comment.count(link), 1)
        self.assertGreater(with_comment.index(link), with_comment.index("Past meetings"))
        self.assertNotIn("#submitted-comment", self.read("meetings.html"))  # the all-meetings list is unchanged

    def test_other_agency_future_and_agencyless_meetings_invent_no_link(self):
        other = self.meetings / "other.json"
        other.write_text(json.dumps({"id": "other", "body": "Elsewhere board (fictional)", "starts_at": "2026-10-01T18:00:00-07:00",
                                     "submitted_comment": comment()}), encoding="utf-8")
        self.edit(submitted_comment=comment())  # the body without an agency page of its own
        before = set(self.build())
        self.assertNotIn("#submitted-comment", "".join(self.read(n) for n in before if n.startswith("agencies/")))
        self.assertIn("Our submitted comment", self.read("meetings/other.html"))
        other.write_text(json.dumps({"id": "other", "body": "Future board (fictional)", "starts_at": "2026-11-01T18:00:00-07:00",
                                     "agency_id": "ca-cedar-police", "submitted_comment": comment()}), encoding="utf-8")
        shown = self.build()
        self.assertNotIn("#submitted-comment", "".join(data.decode("utf-8") for n, data in shown.items() if n.startswith("agencies/")))
        # the page-owning police agency now also owns a calendar for the moved meeting (DF1622-10), nothing else is new
        self.assertEqual(set(shown) - before, {"agencies/cedar.ics"})
        self.assertEqual(before - set(shown), set())
        self.assertFalse(any(n.endswith(".html") for n in set(shown) - before))  # no HTML agency page was invented
        calendar = shown["agencies/cedar.ics"].decode("utf-8")
        self.assertIn("Future board (fictional)", calendar)
        self.assertNotIn(TEXT, calendar)
        self.assertNotIn(comment()["sha256"], calendar)

    def test_json_carries_only_when_and_how_and_csv_is_unchanged(self):
        before = self.build()
        self.edit(submitted_comment=comment())
        shown = self.build()
        doc = json.loads(shown["data/meetings.json"])
        row = next(m for m in doc["meetings"] if m["id"] == "cedar-council-2026-10")
        self.assertEqual(row["submitted_comment"], {"on": "2026-10-10", "channel": "portal"})
        self.assertEqual([m for m in doc["meetings"] if "submitted_comment" in m], [row])
        raw = shown["data/meetings.json"].decode("utf-8")
        for secret in ("Line one", comment()["sha256"], "script"):
            self.assertNotIn(secret, raw)
        law.validate(doc, SCHEMA)
        self.assertEqual(shown["data/meetings.csv"], before["data/meetings.csv"])  # JSON-only: no CSV column
        self.assertNotIn(b"submitted", shown["data/meetings.csv"])
        self.assertEqual(PUBLIC_MEETING_OPTIONAL, ("submitted_comment",))
        self.assertNotIn("submitted_comment", PUBLIC_MEETING_FIELDS)

    def test_schema_rejects_injected_text_hash_or_unknown_fields(self):
        self.edit(submitted_comment=comment())
        self.build()
        doc = json.loads(self.read("data/meetings.json"))
        for extra in ({"text": "x"}, {"sha256": "0" * 64}, {"path": "/x"}, {"channel": "fax"}):
            bad = json.loads(json.dumps(doc))
            next(m for m in bad["meetings"] if "submitted_comment" in m)["submitted_comment"].update(extra)
            with self.subTest(extra), self.assertRaises(Exception):
                law.validate(bad, SCHEMA)

    def test_build_refuses_a_hand_edited_comment_that_is_not_consistent(self):
        before = self.build()
        for name, value in (("digest", {**comment(), "sha256": "0" * 64}), ("placeholder", comment("Dear [name]")),
                            ("pii", comment("write jdoe" + "@" + "example.org")), ("extra", {**comment(), "note": "x"}),
                            ("future", comment(on="2999-01-01")), ("channel", comment(channel="fax"))):
            self.edit(submitted_comment=value)
            with self.subTest(name), self.assertRaises(ContentError):
                build_site(self.root, today=BUILD_DAY)
        self.edit(submitted_comment=comment())
        self.assertEqual(set(self.build()), set(before))  # a valid one builds again

    def test_site_notes_scan_the_hand_authored_comment_text(self):
        from campaign_tool.legal_claims import site_notes
        clean = site_notes(self.root)
        self.edit(submitted_comment=comment("The city violated the Public Records Act and broke the law."))
        notes = [n for n in site_notes(self.root) if n not in clean]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("submitted_comment.text", notes[0])
        self.assertNotIn("violated", notes[0])

    def test_labels_have_both_variables_in_the_spanish_draft(self):
        self.assertIn("{channel}", DEFAULTS["meeting.submitted_comment"])
        self.assertIn("{date}", DEFAULTS["meeting.submitted_comment"])
        spanish = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))
        spanish = spanish.get("labels", spanish)
        self.assertIn("{channel}", spanish["meeting.submitted_comment"])
        self.assertIn("{date}", spanish["meeting.submitted_comment"])


if __name__ == "__main__":
    unittest.main()
