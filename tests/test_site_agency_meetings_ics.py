"""DF1622-10: agencies/<slug>.ics, one meetings calendar per agency with a page and a staged meeting.

Synthetic content only (temp copies of the fictional example); no network. The old-tree digests come from checked main, not this code.
"""
import hashlib
import json
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool import publish_paths
from campaign_tool.cli import check_public_tree
from campaign_tool.content import ContentError
from campaign_tool.site import build_site, ics_text
from tests.test_site_packet import strip_packet_counterevidence

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
DIGESTS = {  # digests are stored as spaced 8-hex groups (credential scan); join them
    _name: {_f: _h.replace(" ", "") for _f, _h in _files.items()}
    for _name, _files in json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "agency-meetings-main-digests.json").read_text(encoding="utf-8")
    )["scenarios"].items()
}
LD_JSON = re.compile(rb'  <script type="application/ld\+json">[^<]*</script>\n')  # DF1407-08: inert data, absent from the pre-feature pins
DAY = date(2026, 10, 20)
HOST = "cedar-records.example.invalid"
POLICE, COUNCIL, SHERIFF = "ca-cedar-police", "ca-cedar-city-council", "ca-cedar-county-sheriff"
LINK = '<p><a href="/agencies/cedar.ics" type="text/calendar">Subscribe to this agency\'s meetings (.ics)</a></p>'
SCENARIOS = {  # mirrored by the capture of tests/fixtures/agency-meetings-main-digests.json
    "default": None,
    "eligible": [("police-old", POLICE, "2026-10-10T18:00:00-07:00", None), ("police-next", POLICE, "2026-11-10T18:00:00-08:00", None),
                 ("police-cancelled", POLICE, "2026-11-12T18:00:00-08:00", "cancelled"),
                 ("council-next", COUNCIL, "2026-11-11T18:00:00-08:00", None)],
    "past_only": [("police-old", POLICE, "2026-10-10T18:00:00-07:00", None)],
    "no_meetings": [],
}


def unfold(text):
    return text.replace("\r\n ", "")


def events(text):
    """VEVENT blocks as raw strings (unfolded form kept as written, folded lines intact)."""
    return ["BEGIN:VEVENT\r\n" + b.split("END:VEVENT\r\n")[0] + "END:VEVENT\r\n" for b in text.split("BEGIN:VEVENT\r\n")[1:]]


def uids(text):
    return re.findall(r"^UID:([^\r\n]*)", unfold(text), flags=re.M)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class AgencyIcsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-agency-ics-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"

    def stage(self, rows, **extra):
        folder = self.root / "content" / "meetings"
        for path in folder.glob("*.json"):
            path.unlink()
        for mid, aid, at, status in rows:
            record = {"id": mid, "body": f"Synthetic {mid} (fictional)", "starts_at": at, "agency_id": aid, **extra}
            if status:
                record["status"] = status
            (folder / f"{mid}.json").write_text(json.dumps(record), encoding="utf-8")

    def build(self, scenario=None):
        if scenario is not None and SCENARIOS[scenario] is not None:
            self.stage(SCENARIOS[scenario])
        build_site(self.root, today=DAY)
        # DF1109-08: the legacy pins predate the packet's counterevidence block; assert and strip exactly that block.
        return {name: strip_packet_counterevidence(self, name, LD_JSON.sub(b"", p.read_bytes()))
                for p in sorted(self.public.rglob("*")) if p.is_file() for name in [p.relative_to(self.public).as_posix()]}

    def digests(self, files):
        return {name: sha(data) for name, data in files.items()}

    def test_writer_preserves_global_bytes_and_disabled_whole_tree(self):
        for name in ("default", "no_meetings"):  # no eligible agency: the whole old tree, byte for byte
            self.setUp()
            self.assertEqual(self.digests(self.build(name)), DIGESTS[name], name)
        for name in ("eligible", "past_only"):
            self.setUp()
            files = self.build(name)
            new, old = self.digests(files), DIGESTS[name]
            # DF1622-09: two or more staged bodies also add the meetings body filter (meetings/filter.js, a changed meetings.html)
            filtered = name == "eligible"
            self.assertEqual(sorted(set(new) - set(old)), ["agencies/cedar.ics"] + (["meetings/filter.js"] if filtered else []), name)
            self.assertEqual(sorted(set(old) - set(new)), [])
            self.assertEqual(sorted(f for f in old if new[f] != old[f]),
                             ["agencies/cedar.html"] + (["meetings.html"] if filtered else []), name)
            page = files["agencies/cedar.html"].decode("utf-8")
            self.assertEqual(page.count(LINK), 1)
            self.assertEqual(sha(page.replace(LINK, "").encode("utf-8")), old["agencies/cedar.html"])  # exactly the new paragraph
            self.assertEqual(new["meetings.ics"], old["meetings.ics"])
            self.assertEqual(self.digests(self.build()), new)  # two builds, identical bytes

    def test_two_agencies_are_isolated_with_global_event_bytes_and_uids(self):
        agencies = json.loads((self.root / "content" / "agencies.json").read_text(encoding="utf-8"))
        for agency in agencies:  # the sheriff gets a page-owning source and a staged meeting too
            if agency["agency_id"] == SHERIFF:
                agency["slug"] = "sheriff"
        (self.root / "content" / "agencies.json").write_text(json.dumps(agencies), encoding="utf-8")
        sources = json.loads((self.root / "content" / "sources.json").read_text(encoding="utf-8"))
        sources.append({**sources[0], "sha256": "ab" * 32, "agency_id": SHERIFF, "title": "Sheriff record (fictional)"})
        (self.root / "content" / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
        self.stage([("p1", POLICE, "2026-11-20T18:00:00-08:00", None), ("c1", COUNCIL, "2026-11-11T18:00:00-08:00", None),
                    ("s1", SHERIFF, "2026-11-15T18:00:00-08:00", None), ("p0", POLICE, "2026-11-12T18:00:00-08:00", None)])
        files = self.build()
        g = dict(zip(uids(files["meetings.ics"].decode()), events(files["meetings.ics"].decode())))
        police = files["agencies/cedar.ics"].decode()
        self.assertEqual(sorted(uids(police)), sorted(f"meeting-{i}@{HOST}" for i in ("p1", "p0")))
        self.assertEqual(uids(files["agencies/sheriff.ics"].decode()), [f"meeting-s1@{HOST}"])
        self.assertNotIn("agencies/cedar-city-council.ics", files)  # council has no page
        for path in ("agencies/cedar.ics", "agencies/sheriff.ics"):
            for block, uid in zip(events(files[path].decode()), uids(files[path].decode())):
                self.assertEqual(block, g[uid])  # byte-identical to the global event
        self.assertIn("X-WR-CALNAME:Cedar County Records Project - Cedar Police Department (fictio\r\n nal) meetings\r\n", police)

    def test_eligibility_is_page_intersection_staged_not_upcoming(self):
        self.assertNotIn("agencies/cedar.ics", self.build("default"))  # staged, but the council has no page
        self.setUp()
        files = self.build("no_meetings")
        self.assertNotIn("agencies/cedar.ics", files)
        self.assertNotIn("Subscribe to this agency", files["agencies/cedar.html"].decode())
        for scenario in ("past_only",):  # past-only: empty calendar and link remain
            self.setUp()
            files = self.build(scenario)
            self.assertEqual(uids(files["agencies/cedar.ics"].decode()), [])
            self.assertTrue(files["agencies/cedar.ics"].decode().startswith("BEGIN:VCALENDAR\r\n"))
            self.assertIn(LINK, files["agencies/cedar.html"].decode())
        self.setUp()  # cancelled-only: same
        self.stage([("c", POLICE, "2026-11-12T18:00:00-08:00", "cancelled")])
        files = self.build()
        self.assertEqual(uids(files["agencies/cedar.ics"].decode()), [])
        self.assertIn(LINK, files["agencies/cedar.html"].decode())
        self.setUp()  # mixed current/cancelled plus a meeting on the build day regardless of clock time
        self.stage([("c", POLICE, "2026-11-12T18:00:00-08:00", "cancelled"), ("today", POLICE, "2026-10-20T00:30:00-07:00", None)])
        self.assertEqual(uids(self.build()["agencies/cedar.ics"].decode()), [f"meeting-today@{HOST}"])

    def test_ics_unicode_escaping_folding_optional_properties_and_offsets(self):
        path = self.root / "content" / "agencies.json"
        agencies = json.loads(path.read_text(encoding="utf-8"))
        name = "Cédar, Police; Dept \\ " + "Ünïcode " * 8
        for agency in agencies:
            if agency["agency_id"] == POLICE:
                agency["name"] = name
        path.write_text(json.dumps(agencies), encoding="utf-8")
        self.stage([("late", POLICE, "2026-11-03T18:00:00-08:00", None)], agenda_item="Item 1\nline two", location="Hall, A; B")
        files = self.build()
        text = files["agencies/cedar.ics"].decode("utf-8")
        for line in text.split("\r\n")[:-1]:
            self.assertLessEqual(len(line.encode("utf-8")), 75)
        self.assertEqual(text.count("\n"), text.count("\r\n"))
        self.assertIn("X-WR-CALNAME:" + ics_text("Cedar County Records Project - " + name + " meetings"), unfold(text))
        self.assertIn("DTSTART:20261104T020000Z", text)  # offset crosses the UTC date
        self.assertIn("LOCATION:Hall\\, A\\; B", text)
        self.assertNotIn("URL:", text)  # no agenda_url, no empty property
        self.assertEqual(events(text), events(files["meetings.ics"].decode()))

    def test_link_labels_escape_and_remain_agency_local(self):
        base = self.build("eligible")
        self.setUp()
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {"agency.meetings_calendar": "Calendar & more, d'été"}}), encoding="utf-8")
        files = self.build("eligible")
        page = files["agencies/cedar.html"].decode("utf-8")
        self.assertIn('<a href="/agencies/cedar.ics" type="text/calendar">Calendar &amp; more, d\'été</a>', page)
        self.assertEqual(files["agencies/cedar.ics"], base["agencies/cedar.ics"])
        self.assertEqual(files["meetings.ics"], base["meetings.ics"])
        self.assertEqual([n for n in base if base[n] != files.get(n)], ["agencies/cedar.html"])  # only the link text moved
        self.setUp()  # disabled: the override changes nothing
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {"agency.meetings_calendar": "Other"}}), encoding="utf-8")
        self.assertEqual(self.digests(self.build("no_meetings")), DIGESTS["no_meetings"])

    def test_rebuild_removes_only_orphan_calendars(self):
        self.build("eligible")
        self.assertTrue((self.public / "agencies" / "cedar.ics").is_file())
        self.stage([])
        files = self.build()
        self.assertNotIn("agencies/cedar.ics", files)
        self.assertFalse((self.public / "agencies" / "cedar.ics").exists())
        self.assertNotIn("Subscribe to this agency", files["agencies/cedar.html"].decode())

    def test_build_check_privacy_and_reserved_collisions(self):
        self.build("eligible")
        self.assertEqual(check_public_tree(self.public), [])
        html = (self.public / "agencies" / "cedar.html").read_text(encoding="utf-8")
        self.assertIn('href="/agencies/cedar.ics"', html)
        before = self.digests({p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()})
        path = self.root / "content" / "meetings" / "police-next.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["location"] = "Call 415-555-0134 or mail secret.person@example.org"
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ContentError) as caught:
            build_site(self.root, today=DAY)
        self.assertNotIn("415-555-0134", str(caught.exception))
        self.assertNotIn("secret.person", str(caught.exception))
        after = self.digests({p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()})
        self.assertEqual(before, after)  # a refused build leaves the old tree
        self.assertIn("agency-meetings-calendars", [e["id"] for e in publish_paths.PUBLISH_PATHS])

    def test_redirect_cannot_overwrite_a_generated_calendar(self):
        self.stage(SCENARIOS["eligible"])
        (self.root / "content" / "redirects.json").write_text(json.dumps([{"from": "/agencies/cedar.ics", "to": "/meetings.ics"}]), encoding="utf-8")
        with self.assertRaises(ContentError):
            build_site(self.root, today=DAY)

    def test_starter_has_no_agency_calendar(self):
        self.assertFalse([n for n in self.build("no_meetings") if n.endswith(".ics") and n.startswith("agencies/")])


if __name__ == "__main__":
    unittest.main()
