"""DF0137-09: data/meetings.json open data for staged meetings, outcomes and votes.

Synthetic content only (temp copies of the fictional example); no network.
"""
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import law
from campaign_tool.content import ContentError
from campaign_tool.site import PUBLIC_MEETING_FIELDS, PUBLIC_MEETING_TOP_LEVEL, PUBLIC_OUTCOME_FIELDS, Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "meetings-open-data.schema.json").read_text(encoding="utf-8"))
BUILD_DAY = date(2026, 10, 20)
EXAMPLE_ID = "cedar-council-2026-10"
AGENDA = "https://example.invalid/cedar-council/agenda"
RECORD = "https://example.invalid/cedar-council/minutes"
CONTACT = "synthetic-clerk@example.invalid"
LINK = '<a href="/data/meetings.json" type="application/json">'
VOTES = [{"name": "Zed Example", "vote": "no"}, {"name": "Ash Example", "vote": "yes"},
         {"name": "Bo Example", "vote": "abstain"}]
OUTCOME = {"result": "approved", "on": "2026-10-14", "source_url": RECORD, "verified_by": "Synthetic Organizer",
           "verified_at": "2026-10-15", "vote": "2-1", "votes": VOTES}
PARTICIPATION = {"in_person": True, "remote": {"available": True, "how": f"Zoom, write {CONTACT}"},
                 "written_comment": {"how": f"Email {CONTACT}", "deadline": "2026-10-13T12:00:00-07:00"},
                 "source_url": RECORD, "verified_by": "Synthetic Organizer", "verified_at": "2026-10-10"}


class MeetingsOpenDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meetings-json-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.example = self.meetings / "2026-10-cedar-council.json"
        self.public = self.root / "public"
        officials = [{"name": v["name"], "role": "Council member", "source_url": "https://cedar.example/council",
                      "checked_on": "2026-09-01"} for v in VOTES]
        (self.root / "content" / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": [
            {"agency_id": "ca-cedar-city-council", "officials": officials}]}), encoding="utf-8")

    def edit(self, path=None, drop=(), **changes):
        path = path or self.example
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        for key in drop:
            data.pop(key, None)
        path.write_text(json.dumps(data), encoding="utf-8")

    def add(self, meeting_id, starts_at, **extra):
        record = {"id": meeting_id, "body": f"Synthetic board {meeting_id} (fictional)", "starts_at": starts_at, **extra}
        (self.meetings / f"{meeting_id}.json").write_text(json.dumps(record), encoding="utf-8")

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def data(self):
        build_site(self.root, today=BUILD_DAY)
        return json.loads(self.read("data/meetings.json"))

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    # 1 + 2. kit, participation contacts and verifier fields never appear; the file validates
    def test_kit_participation_and_verifier_never_appear(self):
        self.edit(participation=PARTICIPATION, outcome=OUTCOME)
        doc = self.data()
        self.assertEqual(doc["meetings"], [{
            "id": EXAMPLE_ID, "body": "Cedar City Council (fictional)", "agency_id": "ca-cedar-city-council",
            "starts_at": "2026-10-14T18:00:00-07:00", "status": "scheduled", "agenda_url": AGENDA,
            "url": f"/meetings/{EXAMPLE_ID}.html",
            "outcome": {"result": "approved", "on": "2026-10-14", "source_url": RECORD, "vote": "2-1", "votes": VOTES}}])
        self.assertEqual(set(doc), PUBLIC_MEETING_TOP_LEVEL)
        law.validate(doc, SCHEMA)
        text = self.read("data/meetings.json")
        for leaked in (CONTACT, "participation", "comment_kit", "Two minutes, one ask", "verified_by", "verified_at",
                       "Synthetic Organizer", "Cedar Civic Center", "agenda_item", "Item 7"):
            self.assertNotIn(leaked, text)
        self.assertIn("Synthetic Organizer", self.read(f"meetings/{EXAMPLE_ID}.html"))  # the page already shows it
        self.assertEqual(text, json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    # 3. a key injected into the builder is refused naming the file, nothing is written
    def test_injected_key_is_refused_naming_meetings_json(self):
        self.edit(outcome=OUTCOME)
        build_site(self.root, today=BUILD_DAY)
        before = self.snapshot()
        real = Site._public_meeting
        leaks = {"comment_kit_md": lambda v: v.update(comment_kit_md="kit"),
                 "participation": lambda v: v.update(participation={"in_person": True}),
                 "location": lambda v: v.update(location="Hall"),
                 "verified_by": lambda v: v["outcome"].update(verified_by="Synthetic Organizer"),
                 "verified_at": lambda v: v["outcome"].update(verified_at="2026-10-15"),
                 "note": lambda v: v["outcome"]["votes"][0].update(note="x"),
                 "Body": lambda v: v.update(Body="case variant")}
        for key, leak in leaks.items():
            with self.subTest(key=key):
                def builder(self_, meeting, leak=leak):
                    value = real(self_, meeting)
                    leak(value)
                    return value
                with mock.patch.object(Site, "_public_meeting", builder):
                    with self.assertRaisesRegex(ContentError, f"^data/meetings\\.json: non-public key {key}$"):
                        build_site(self.root, today=BUILD_DAY)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith(".public")), [])
        self.assertEqual(set(PUBLIC_MEETING_FIELDS) & {"participation", "comment_kit_md", "location"}, set())
        self.assertEqual(set(PUBLIC_OUTCOME_FIELDS) & {"verified_by", "verified_at"}, set())

    # 4. a cancelled meeting carries its status and no outcome
    def test_cancelled_meeting_has_status_and_no_outcome(self):
        self.edit(status="cancelled", starts_at="2026-11-04T18:00:00-07:00")
        self.add("past-one", "2026-10-01T18:00:00-07:00", outcome={**{k: v for k, v in OUTCOME.items() if k != "votes"},
                 "on": "2026-10-01", "verified_at": "2026-10-02"})
        doc = self.data()
        law.validate(doc, SCHEMA)
        cancelled = [m for m in doc["meetings"] if m["id"] == EXAMPLE_ID][0]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertNotIn("outcome", cancelled)
        self.assertIn("outcome", [m for m in doc["meetings"] if m["id"] == "past-one"][0])

    # 5. no meetings: an empty list, schema-valid, links still present and the about text says so
    def test_no_meetings_writes_empty_list_and_keeps_links(self):
        self.example.unlink()
        doc = self.data()
        self.assertEqual(doc["meetings"], [])
        self.assertEqual(doc["schema_version"], 1)
        law.validate(doc, SCHEMA)
        self.assertIn(LINK + "Meetings and outcomes as open data (JSON)</a>", self.read("meetings.html"))
        about = self.read("about.html")
        self.assertIn(LINK + "data/meetings.json</a>", about)
        self.assertIn("No meetings are listed yet.", about)

    # 6. votes keep stored order; an outcome without votes (or without a vote line) has no such key
    def test_votes_in_stored_order_and_absent_without_votes(self):
        self.edit(outcome=OUTCOME)
        self.add("plain-one", "2026-10-02T18:00:00-07:00",
                 outcome={"result": "continued", "on": "2026-10-02", "source_url": RECORD, "verified_by": "Synthetic Organizer",
                          "verified_at": "2026-10-03"})
        self.add("future-one", "2026-11-05T18:00:00-07:00")
        meetings = {m["id"]: m for m in self.data()["meetings"]}
        self.assertEqual(meetings[EXAMPLE_ID]["outcome"]["votes"], VOTES)
        self.assertEqual([v["name"] for v in meetings[EXAMPLE_ID]["outcome"]["votes"]], ["Zed Example", "Ash Example", "Bo Example"])
        self.assertEqual(meetings["plain-one"]["outcome"], {"result": "continued", "on": "2026-10-02", "source_url": RECORD})
        self.assertNotIn("outcome", meetings["future-one"])
        self.assertEqual(meetings["future-one"]["agency_id"], None)
        self.assertEqual(meetings["future-one"]["agenda_url"], None)

    # 7. every url is a generated page; agenda_url and source_url are copied verbatim; newest first
    def test_urls_resolve_and_links_are_verbatim_newest_first(self):
        self.edit(outcome=OUTCOME)
        self.add("older-one", "2026-09-02T18:00:00-07:00")
        self.add("newer-one", "2026-11-05T18:00:00-07:00", agenda_url=AGENDA + "?x=1&y=2")
        doc = self.data()
        self.assertEqual([m["id"] for m in doc["meetings"]], ["newer-one", EXAMPLE_ID, "older-one"])
        for meeting in doc["meetings"]:
            self.assertTrue((self.public / meeting["url"].lstrip("/")).is_file(), meeting["url"])
        self.assertEqual(doc["meetings"][0]["agenda_url"], AGENDA + "?x=1&y=2")
        self.assertEqual(doc["meetings"][1]["outcome"]["source_url"], RECORD)
        about = self.read("about.html")
        self.assertIn(LINK + "data/meetings.json</a>", about)
        self.assertIn('<a href="/data/findings.json" type="application/json">data/findings.json</a> and '
                      '<a href="/data/sources.json" type="application/json">data/sources.json</a>', about)
        self.assertIn(LINK + "Meetings and outcomes as open data (JSON)</a>", self.read("meetings.html"))

    # 8. building twice is byte-identical, and the clock is the build's own
    def test_two_builds_are_byte_identical_and_use_the_build_clock(self):
        self.edit(outcome=OUTCOME)
        build_site(self.root, today=BUILD_DAY)
        first = self.read("data/meetings.json")
        build_site(self.root, today=BUILD_DAY)
        self.assertEqual(self.read("data/meetings.json"), first)
        self.assertEqual(json.loads(first)["generated_at"], "2026-10-20T00:00:00Z")
        build_site(self.root, today=date(2026, 10, 21))
        self.assertEqual(json.loads(self.read("data/meetings.json"))["generated_at"], "2026-10-21T00:00:00Z")

    def test_build_check_sees_the_file_and_its_links(self):
        self.edit(outcome=OUTCOME)
        written = {p.relative_to(self.public).as_posix() for p in build_site(self.root, today=BUILD_DAY)}
        self.assertIn("data/meetings.json", written)


if __name__ == "__main__":
    unittest.main()
