"""DF1103-10: public/meetings.xml, a deterministic Atom feed of staged meetings.

Synthetic content only (temp copies of the fictional example); no network.
"""
import builtins
import json
import os
import re
import shutil
import socket
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

from campaign_tool.cli import read_config
from campaign_tool.content import ContentError, load_content
from campaign_tool.site import Site, build_site
from runner.client import safe_site_path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
EXAMPLE_ID = "cedar-council-2026-10"  # starts_at 2026-10-14T18:00:00-07:00, in the past on BUILD_DAY, no outcome
BUILD_DAY = date(2026, 10, 20)
BASE = "https://cedar-records.example.invalid"
A = "{http://www.w3.org/2005/Atom}"
RECORD = "https://example.invalid/cedar-council/minutes"
AGENDA = "https://example.invalid/cedar-council/agenda"
SECRET = "SENTINEL-PRIVATE-7731"
EPOCH = "2000-01-01T00:00:00+00:00"
OUTCOME = {"result": "approved", "on": "2026-10-14", "source_url": RECORD, "verified_by": "Synthetic Organizer",
           "verified_at": "2026-10-15", "vote": "2-1", "recording_url": "https://example.invalid/video",
           "votes": [{"name": "Zed Example", "vote": "no"}, {"name": "Ash Example", "vote": "yes"}]}
PLAIN = {key: value for key, value in OUTCOME.items() if key not in ("votes", "recording_url")}  # no roll call: needs no officials
PARTICIPATION = {"in_person": True, "remote": {"available": True, "how": f"Write {SECRET}@example.invalid"},
                 "written_comment": {"how": "Email the clerk", "deadline": "2026-10-13T12:00:00-07:00"},
                 "source_url": RECORD, "verified_by": "Synthetic Organizer", "verified_at": "2026-10-10"}


class MeetingsFeedTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meetings-feed-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.example = self.meetings / "2026-10-cedar-council.json"
        self.public = self.root / "public"

    def add(self, meeting_id, starts_at, **extra):
        record = {"id": meeting_id, "body": f"Synthetic board {meeting_id} (fictional)", "starts_at": starts_at, **extra}
        (self.meetings / f"{meeting_id}.json").write_text(json.dumps(record), encoding="utf-8")

    def edit(self, path=None, **changes):
        path = path or self.example
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")

    def only(self, *records):
        """Replace the staged meetings with ``(id, starts_at, extra)`` records."""
        for path in self.meetings.glob("*.json"):
            path.unlink()
        for meeting_id, starts_at, extra in records:
            self.add(meeting_id, starts_at, **extra)

    def build(self, today=BUILD_DAY):
        return {p.relative_to(self.public).as_posix() for p in build_site(self.root, today=today)}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def feed(self, today=BUILD_DAY):
        self.build(today)
        return ET.fromstring(self.read("meetings.xml").encode("utf-8"))

    def ids(self, today=BUILD_DAY):
        return [e.find(A + "id").text for e in self.feed(today).findall(A + "entry")]

    def site(self, today=BUILD_DAY):
        return Site(self.root, read_config(self.root), load_content(self.root, today), today=today)

    def test_eligibility_and_one_entry_per_id(self):
        self.only(("future", "2026-10-25T18:00:00-07:00", {}),
                  ("today", "2026-10-20T09:00:00-07:00", {}),
                  ("cancel-future", "2026-11-01T18:00:00-07:00", {"status": "cancelled"}),
                  ("cancel-past", "2026-10-01T18:00:00-07:00", {"status": "cancelled"}),
                  ("past-outcome", "2026-10-10T18:00:00-07:00", {"outcome": {**PLAIN, "on": "2026-10-11"}}),
                  ("past-plain", "2026-10-05T18:00:00-07:00", {}))
        ids = self.ids()
        self.assertEqual(sorted(ids), sorted("urn:deflock-meeting:" + i for i in
                                             ("future", "today", "cancel-future", "cancel-past", "past-outcome")))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn("urn:deflock-meeting:past-plain", ids)
        # the same meeting listed twice in the model, in any order, still gives one entry and the same bytes
        site = self.site()
        first = site.meetings_feed_xml()
        site.model["meetings"] = site.model["meetings"][::-1] + site.model["meetings"][:1]
        self.assertEqual(site.meetings_feed_xml(), first)
        # staged list empty: no file; nonempty but nothing eligible: an empty feed at the epoch
        self.only()
        self.build()
        self.assertFalse((self.public / "meetings.xml").exists())
        self.add("past-plain", "2026-10-05T18:00:00-07:00")
        root = self.feed()
        self.assertEqual(root.findall(A + "entry"), [])
        self.assertEqual(root.find(A + "updated").text, EPOCH)
        self.assertIn("/meetings.xml", self.read("meetings.html"))

    def test_content_timestamps_and_offset_order(self):
        self.only(("held", "2026-10-10T18:00:00-07:00", {"outcome": {**PLAIN, "on": "2026-10-11", "verified_at": "2026-10-19"}}),
                  ("late-offset", "2026-10-25T23:30:00-07:00", {}),   # 2026-10-26T06:30Z
                  ("late-z", "2026-10-26T06:30:00Z", {}),             # the same instant
                  ("early-z", "2026-10-26T01:00:00Z", {}))
        root = self.feed()
        entries = {e.find(A + "id").text.split(":")[-1]: e for e in root.findall(A + "entry")}
        self.assertEqual(entries["held"].find(A + "updated").text, "2026-10-11T00:00:00+00:00")  # not verified_at
        self.assertEqual(entries["late-offset"].find(A + "updated").text, "2026-10-25T23:30:00-07:00")
        self.assertEqual(entries["late-z"].find(A + "updated").text, "2026-10-26T06:30:00Z")
        # newest instant first, an equal instant by ascending id, the outcome date last
        self.assertEqual([e.find(A + "id").text.split(":")[-1] for e in root.findall(A + "entry")],
                         ["late-offset", "late-z", "early-z", "held"])
        self.assertEqual(root.find(A + "updated").text, "2026-10-26T06:30:00+00:00")
        # a clock or file time never becomes `updated`
        before = self.read("meetings.xml")
        for path in self.meetings.glob("*.json"):
            os.utime(path, (4_000_000_000, 4_000_000_000))
        self.build(today=date(2026, 10, 21))
        self.assertEqual(self.read("meetings.xml"), before)
        # a cancellation has no recorded date, so the status change keeps the start-derived `updated`
        self.edit(self.meetings / "early-z.json", status="cancelled")
        root = self.feed()
        early = [e for e in root.findall(A + "entry") if e.find(A + "id").text.endswith("early-z")][0]
        self.assertEqual(early.find(A + "updated").text, "2026-10-26T01:00:00Z")
        self.assertTrue(early.find(A + "title").text.startswith("Cancelled: "))
        self.assertEqual(root.find(A + "updated").text, "2026-10-26T06:30:00+00:00")

    def test_rebuild_bytes_and_no_private_reads(self):
        self.edit(participation=PARTICIPATION, outcome=OUTCOME, comment_kit_md=f"Kit text {SECRET}")
        officials = [{"name": v["name"], "role": "Council member", "source_url": "https://cedar.example/council",
                      "checked_on": "2026-09-01"} for v in OUTCOME["votes"]]
        (self.root / "content" / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": [
            {"agency_id": "ca-cedar-city-council", "officials": officials}]}), encoding="utf-8")
        kit = self.root / "kit"
        kit.mkdir()
        (kit / "meetings.json").write_text(json.dumps({"private": SECRET}), encoding="utf-8")
        (kit / "comment-sent.json").write_text(json.dumps({"private": SECRET}), encoding="utf-8")
        self.build()
        first = self.read("meetings.xml")
        self.build()
        self.assertEqual(self.read("meetings.xml"), first)
        (kit / "meetings.json").write_text(json.dumps({"private": "changed", "other": 1}), encoding="utf-8")
        (kit / "subscribers.csv").write_text("a@example.invalid\n", encoding="utf-8")
        self.build()
        self.assertEqual(self.read("meetings.xml"), first)
        for leaked in (SECRET, "Synthetic Organizer", "Zed Example", "Ash Example", "Kit text", "example/video", "video",
                       "participation", "Cedar Civic Center"):
            self.assertNotIn(leaked, first)
        # the feed methods read the loaded model only: no file open, no socket
        site = self.site()

        def refuse(*args, **kwargs):
            raise AssertionError("private read or network use")
        with mock.patch.object(builtins, "open", refuse), mock.patch.object(Path, "open", refuse), \
                mock.patch.object(Path, "read_text", refuse), mock.patch.object(Path, "read_bytes", refuse), \
                mock.patch.object(socket.socket, "connect", refuse), mock.patch.object(socket, "create_connection", refuse):
            self.assertEqual(site.meetings_feed_xml(), first)

    def test_entry_fields_escape_and_labels(self):
        self.edit(body='Council & "Board" Caf\u00e9', agenda_item="Item 9 & \u201ctech\u201d", outcome=OUTCOME,
                  starts_at="2026-10-14T18:00:00-07:00")
        officials = [{"name": v["name"], "role": "Council member", "source_url": "https://cedar.example/council",
                      "checked_on": "2026-09-01"} for v in OUTCOME["votes"]]
        (self.root / "content" / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": [
            {"agency_id": "ca-cedar-city-council", "officials": officials}]}), encoding="utf-8")
        self.add("plain", "2026-10-30T18:00:00-07:00")
        self.add("off", "2026-11-02T18:00:00-07:00", status="cancelled", agenda_url=AGENDA, agenda_item="Item 1")
        root = self.feed()
        self.assertTrue(self.read("meetings.xml").startswith('<?xml version="1.0" encoding="UTF-8"?>\n<feed xmlns="'))
        self.assertTrue(self.read("meetings.xml").endswith("</feed>\n"))
        entries = {e.find(A + "id").text: e for e in root.findall(A + "entry")}
        held = entries["urn:deflock-meeting:" + EXAMPLE_ID]
        self.assertEqual(held.find(A + "title").text, 'Council & "Board" Caf\u00e9: Item 9 & \u201ctech\u201d')
        self.assertEqual(held.find(A + "summary").text,
                         'Starts: 2026-10-14T18:00:00-07:00. Body: Council & "Board" Caf\u00e9. Agenda item: Item 9 & \u201ctech\u201d. '
                         f"Agenda: {AGENDA}. Outcome: Approved on 2026-10-14. Vote: 2-1.")
        self.assertEqual(held.find(A + "summary").get("type"), "text")
        self.assertEqual([(l.get("rel"), l.get("href")) for l in held.findall(A + "link")],
                         [("alternate", f"{BASE}/meetings/{EXAMPLE_ID}.html"), ("related", AGENDA), ("related", RECORD)])
        self.assertEqual(held.find(A + "updated").text, "2026-10-14T00:00:00+00:00")
        plain = entries["urn:deflock-meeting:plain"]
        self.assertEqual(plain.find(A + "title").text, "Synthetic board plain (fictional)")
        self.assertEqual(plain.find(A + "summary").text,
                         "Starts: 2026-10-30T18:00:00-07:00. Body: Synthetic board plain (fictional).")
        self.assertEqual([l.get("rel") for l in plain.findall(A + "link")], ["alternate"])
        off = entries["urn:deflock-meeting:off"]
        self.assertEqual(off.find(A + "title").text, "Cancelled: Synthetic board off (fictional): Item 1")
        self.assertEqual(off.find(A + "summary").text, "Starts: 2026-11-02T18:00:00-07:00. Body: Synthetic board off (fictional)."
                                                       f" Agenda item: Item 1. Cancelled. Agenda: {AGENDA}.")
        for entry in entries.values():
            for forbidden in ("Zed Example", "Synthetic Organizer", "video", "kit", "Cedar Civic Center", "RSVP"):
                self.assertNotIn(forbidden, ET.tostring(entry, encoding="unicode"))
        self.assertEqual(root.find(A + "title").text, read_config(self.root)["name"] + " meetings")
        # Atom stays English even when every label is replaced; the HTML labels follow the site's labels
        before = self.read("meetings.xml")
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "meeting.feed": "Calendario de reuniones", "title.meetings_feed": "Atom de reuniones",
            "meeting.cancelled": "Cancelada:"}}), encoding="utf-8")
        self.build()
        self.assertEqual(self.read("meetings.xml"), before)
        self.assertIn('<a href="/meetings.xml" type="application/atom+xml">Calendario de reuniones</a>', self.read("meetings.html"))
        self.assertIn('title="Atom de reuniones" href="/meetings.xml"', self.read("meetings.html"))
        self.assertNotIn("Meetings feed", self.read("meetings.html"))
        self.assertNotIn("Calendario", self.read("meetings.xml"))
        self.assertIn("<title>Cancelled: Synthetic board off", self.read("meetings.xml"))  # not the label override
        self.assertNotIn("Cancelada", self.read("meetings.xml"))
        self.assertIn(">Calendario de reuniones</a>", self.read("index.html"))

    def test_absolute_relative_links_and_discovery(self):
        self.build()
        link = '<link rel="alternate" type="application/atom+xml" title="Meetings Atom feed" href="/meetings.xml">'
        finding = '<link rel="alternate" type="application/atom+xml" title="Findings" href="/feed.xml">'
        footer = '<a href="/meetings.xml">Meetings feed</a>'
        intro = '<a href="/meetings.xml" type="application/atom+xml">Meetings feed</a>'
        for page in sorted(self.public.rglob("*.html")):
            name, html = page.relative_to(self.public).as_posix(), page.read_text(encoding="utf-8")
            self.assertIn(footer, html, name)
            self.assertEqual(html.count(link), 1 if name == "meetings.html" or name.startswith("meetings/") else 0, name)
            self.assertEqual(html.count(finding), 1, name)  # the findings alternate stays everywhere
        self.assertIn(intro, self.read("meetings.html"))
        self.assertNotIn(intro, self.read("meetings/" + EXAMPLE_ID + ".html"))
        root = self.feed()
        self.assertEqual(root.find(A + "id").text, BASE + "/meetings.xml")
        self.assertEqual([(l.get("rel"), l.get("href")) for l in root.findall(A + "link")],
                         [("self", BASE + "/meetings.xml"), ("alternate", BASE + "/meetings.html")])
        # a trailing slash on base_url does not double up; without base_url links are root-relative
        site_path = self.root / "content" / "site.json"
        site = json.loads(site_path.read_text(encoding="utf-8"))
        site["base_url"] = BASE + "/"
        site_path.write_text(json.dumps(site), encoding="utf-8")
        self.edit(outcome=PLAIN)
        self.assertEqual(self.read_after_build("meetings.xml"), self.read("meetings.xml"))
        self.assertIn(f'href="{BASE}/meetings/{EXAMPLE_ID}.html"', self.read("meetings.xml"))
        self.assertNotIn(BASE + "//", self.read("meetings.xml"))
        site["base_url"] = None
        site_path.write_text(json.dumps(site), encoding="utf-8")
        root = self.feed()
        self.assertEqual(root.find(A + "id").text, "urn:deflock-campaign:" + re.sub(
            r"[^a-z0-9]+", "-", read_config(self.root)["name"].lower()) + ":meetings")
        self.assertEqual([(l.get("rel"), l.get("href")) for l in root.findall(A + "link")],
                         [("self", "/meetings.xml"), ("alternate", "/meetings.html")])
        self.assertEqual(root.find(A + "entry").find(A + "link").get("href"), f"/meetings/{EXAMPLE_ID}.html")
        # every local href in every page resolves to a written file, XML never reaches the sitemap or the page list
        written = self.snapshot()
        for page in self.public.rglob("*.html"):
            for href in re.findall(r'href="(/[^"#]*)"', page.read_text(encoding="utf-8")):
                target = href.lstrip("/") or "index.html"
                if target.endswith("/"):
                    target += "index.html"
                self.assertIn(target, written, f"{page.name}: {href}")
        site["base_url"] = BASE
        site_path.write_text(json.dumps(site), encoding="utf-8")
        self.build()
        self.assertNotIn("meetings.xml", self.read("sitemap.xml"))

    def read_after_build(self, name):
        self.build()
        return self.read(name)

    def test_no_staged_cleanup_and_collision(self):
        self.build()
        self.assertTrue((self.public / "meetings.xml").is_file())
        self.example.unlink()
        written = self.build()
        self.assertNotIn("meetings.xml", written)
        self.assertFalse((self.public / "meetings.xml").exists())
        for name in ("meetings.html", "index.html", "404.html"):
            self.assertNotIn("meetings.xml", self.read(name), name)
        # the starter (no content/) keeps its output and never gets a feed
        shutil.rmtree(self.root / "content")
        starter = {p.relative_to(self.public).as_posix() for p in build_site(self.root, today=BUILD_DAY)}
        self.assertNotIn("meetings.xml", starter)
        # a public asset named meetings.xml is refused with or without staged meetings, and public/ is left alone
        for staged in (False, True):
            with self.subTest(staged=staged):
                shutil.rmtree(self.root / "content", ignore_errors=True)
                shutil.copytree(EXAMPLE / "content", self.root / "content")
                if not staged:
                    (self.meetings / "2026-10-cedar-council.json").unlink()
                self.build()
                before = self.snapshot()
                asset = self.root / "content" / "public-assets" / "meetings.xml"
                asset.parent.mkdir(parents=True, exist_ok=True)
                asset.write_text("<x/>", encoding="utf-8")
                with self.assertRaises(ContentError) as caught:
                    self.build()
                self.assertIn("public-assets/meetings.xml", str(caught.exception))
                self.assertEqual(self.snapshot(), before)
                self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])

    def test_bad_content_atomic_and_findings_unchanged(self):
        self.edit(outcome=PLAIN)
        self.build()
        before = self.snapshot()
        bad = {
            "naive start": dict(starts_at="2026-10-30T18:00:00"),
            "impossible start": dict(starts_at="2026-13-45T18:00:00-07:00"),
            "unsafe agenda": dict(starts_at="2026-10-30T18:00:00-07:00", agenda_url="http://example.invalid/a"),
            "script agenda": dict(starts_at="2026-10-30T18:00:00-07:00", agenda_url="javascript:alert(1)"),
            "unsafe outcome source": dict(outcome={**PLAIN, "source_url": "http://example.invalid/r"}),
            "malformed outcome": dict(outcome={"result": "approved"}),
            "unknown result": dict(outcome={**PLAIN, "result": "adopted"}),
            "cancelled with outcome": dict(status="cancelled", outcome=PLAIN),
        }
        for label, changes in bad.items():
            with self.subTest(label):
                self.add("zz-bad", "2026-10-30T18:00:00-07:00")
                path = self.meetings / "zz-bad.json"
                if "outcome" in changes and "starts_at" not in changes:
                    changes = {**changes, "starts_at": "2026-10-10T18:00:00-07:00"}
                self.edit(path, **changes)
                with self.assertRaises(ContentError):
                    self.build()
                self.assertEqual(self.snapshot(), before)
                path.unlink()
        # a duplicate effective id is refused before the tree swap
        (self.meetings / f"{EXAMPLE_ID}.json").write_text(json.dumps({"body": "Other board (fictional)",
                                                                       "starts_at": "2026-11-05T18:00:00-07:00"}), encoding="utf-8")
        with self.assertRaises(ContentError) as caught:
            self.build()
        self.assertIn("duplicate meeting id", str(caught.exception))
        self.assertEqual(self.snapshot(), before)
        (self.meetings / f"{EXAMPLE_ID}.json").unlink()
        # a render failure in the feed leaves the prior tree and no staging directory behind
        self.edit(body="Changed body (fictional)")
        with mock.patch.object(Site, "meetings_feed_xml", side_effect=RuntimeError("render failed")):
            with self.assertRaises(RuntimeError):
                self.build()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])
        # the findings feeds are byte-identical with and without the meetings feed
        with_feed = {k: v for k, v in self.snapshot().items() if k in ("feed.xml", "findings/agency/cedar.xml", "data/findings.json")}
        self.example.unlink()
        self.build()
        without = {k: v for k, v in self.snapshot().items() if k in with_feed}
        self.assertEqual(sorted(without), sorted(with_feed))
        self.assertTrue(all(without[k] == with_feed[k] for k in with_feed))

    def test_existing_upload_extension_contract(self):
        self.assertEqual(safe_site_path("meetings.xml"), "meetings.xml")
        self.assertIsNone(safe_site_path("meetings.ics"))
        self.assertIn("meetings.xml", self.build())


if __name__ == "__main__":
    unittest.main()
