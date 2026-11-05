"""DF1952-10: the home "Where things stand" strip (synthetic temp copies of the fictional example; no network)."""
import builtins
import io
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool.content import ContentError, load_content
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)  # build instant 2026-11-01T00:00:00Z
POLICE, SHERIFF = "ca-cedar-police", "ca-cedar-county-sheriff"
STRIP = re.compile(r'<section class="section" id="standings">(.*?)</section>', re.S)
ITEM = re.compile(r'<li><a href="([^"]*)">([^<]*)</a>: <strong>([^<]*)</strong></li>')
DEADLINE = re.compile(r'<p class="comment-deadline">Next verified written-comment deadline: '
                      r'<a href="([^"]*)">([^<]*)</a>, <time datetime="([^"]*)">([^<]*)</time></p>')
CAMERA_LABEL = "Community-mapped camera points; ownership unconfirmed"


def ask(topic, state="awaiting_response", **more):
    return dict({"topic": topic, "asked_on": "2026-10-01", "state": state}, **more)


def meeting(mid, deadline, starts="2026-11-28T18:00:00-08:00", **more):
    written = {"how": "Email the clerk", "deadline": deadline}
    return dict({"id": mid, "body": f"Body {mid}", "starts_at": starts, "agenda_url": None, "agenda_item": "", "location": "",
                 "comment_kit_md": "", "agency_id": "ca-cedar-city-council",
                 "participation": {"in_person": True, "remote": None, "written_comment": written,
                                   "source_url": "https://example.invalid/agenda", "verified_by": "Synthetic Organizer",
                                   "verified_at": "2026-11-01"}}, **more)


class StandingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-standings-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content, self.public = self.root / "content", self.root / "public"

    def agencies(self, by_agency):
        path = self.content / "agencies.json"
        agencies = json.loads(path.read_text(encoding="utf-8"))
        for agency in agencies:
            agency.pop("asked", None)
            if agency["agency_id"] in by_agency:
                agency["asked"] = by_agency[agency["agency_id"]]
        path.write_text(json.dumps(agencies), encoding="utf-8")

    def meetings(self, *items):
        folder = self.content / "meetings"
        for path in folder.glob("*.json"):
            path.unlink()
        for item in items:
            (folder / f'{item["id"]}.json').write_text(json.dumps(item), encoding="utf-8")

    def build(self):
        build_site(self.root, today=TODAY)

    def read(self, name="index.html"):
        return (self.public / name).read_text(encoding="utf-8")

    def metrics(self):
        return ITEM.findall(self.strip(self.read()))

    def strip(self, page):
        match = STRIP.search(page)
        self.assertIsNotNone(match, "no standings strip")
        return match.group(0) if match else ""

    def deadline(self):
        return DEADLINE.findall(self.read())

    def no_map(self):
        shutil.rmtree(self.content / "map")

    # 1. denominators
    def test_public_denominators_and_duplicate_asked_items(self):
        self.agencies({POLICE: [ask("A", "answered"), ask("A", "answered"), ask("B", "partly_answered"),
                                ask("C", "acknowledged"), ask("D", "declined_in_writing"), ask("E", "withdrawn")],
                       SHERIFF: [ask("F")]})
        (self.root / "kit").mkdir()
        (self.root / "kit" / "sent.json").write_text(json.dumps({"ca-cedar-city-council": [{"topic": "private"}]}), encoding="utf-8")
        self.build()
        model = load_content(self.root, TODAY)
        rows = self.metrics()
        self.assertEqual([r[2] for r in rows], ["2", "3", str(len(model["sources"])), str(len(model["findings"])),
                                                 str(len(json.loads((self.content / "map" / "cameras.geojson").read_text())["features"]))])
        self.assertEqual([r[0] for r in rows], ["/requests.html", "/requests.html", "/sources.html", "/findings/", "/cameras.html"])
        self.assertGreater(len(model["sources"]), 0)
        self.assertIn(CAMERA_LABEL, rows[4][1])
        self.assertLess(self.read().index('id="standings"'), self.read().index('id="agencies"'))

    # 2. all zero
    def test_all_zero_none_yet_and_real_links(self):
        self.agencies({})
        for name in ("sources.json",):
            (self.content / name).write_text("[]", encoding="utf-8")
        for path in (self.content / "findings").glob("*"):
            path.unlink() if path.is_file() else shutil.rmtree(path)
        self.no_map()
        self.build()
        rows = self.metrics()
        self.assertEqual([r[2] for r in rows], ["none yet"] * 5)
        self.assertNotIn("<strong>0</strong>", self.read())
        for href, _, _ in rows:
            self.assertTrue((self.public / (href.strip("/") + "/index.html" if href.endswith("/") else href.strip("/"))).is_file(), href)
        self.assertIn("none yet", self.read("cameras.html").lower())
        self.assertIn("not confirmed agency ownership", self.read("cameras.html"))
        self.assertEqual(self.deadline(), [])

    # 3. mapped points: every feature, not per-city totals
    def test_mapped_points_all_features_not_city_totals(self):
        doc = json.loads((self.content / "map" / "cameras.geojson").read_text(encoding="utf-8"))
        doc["features"] += [{"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [170.0 + i / 100, 80.0]}}
                            for i in range(37)]  # far outside every boundary
        (self.content / "map" / "cameras.geojson").write_text(json.dumps(doc), encoding="utf-8")
        self.build()
        self.assertEqual(self.metrics()[4][2], str(len(doc["features"])))

    # 4. deadline
    def test_next_verified_deadline_all_candidates_ties_offsets(self):
        self.meetings(
            meeting("a1", "2026-11-25T09:00:00-08:00", starts="2026-11-28T18:00:00-08:00"),
            meeting("a2", "2026-11-26T09:00:00-08:00", starts="2026-11-29T18:00:00-08:00"),
            meeting("a3", "2026-11-27T09:00:00-08:00", starts="2026-11-29T20:00:00-08:00"),
            meeting("zlate", "2026-11-24T10:00:00+09:00", starts="2026-12-01T18:00:00-08:00"),  # 4th by start, earliest instant
            meeting("zdup", "2026-11-24T01:00:00Z", starts="2026-12-01T18:00:00-08:00"),  # same instant as zlate
            meeting("equal", "2026-11-01T00:00:00Z"), meeting("past", "2026-10-30T00:00:00Z"), meeting("none", None),
            meeting("cancelled", "2026-11-02T00:00:00Z", status="cancelled"))
        self.build()
        found = self.deadline()
        self.assertEqual(found, [("/meetings/zdup.html", "Body zdup", "2026-11-24T01:00:00Z",
                                  "Tuesday, November 24, 2026, 1:00 AM (UTC+00:00)")])
        self.meetings(meeting("only", "2026-11-24T10:00:00+09:00"))
        self.build()
        self.assertEqual(self.deadline(), [("/meetings/only.html", "Body only", "2026-11-24T10:00:00+09:00",
                                            "Tuesday, November 24, 2026, 10:00 AM (UTC+09:00)")])
        self.meetings(meeting("equal", "2026-11-01T00:00:00Z"), meeting("past", "2026-10-30T00:00:00Z"), meeting("none", None))
        self.build()
        self.assertEqual(self.deadline(), [])
        self.assertNotIn("Next verified", self.read())

    # 5. escaping, fixed day, host timezone
    def test_labels_escaping_accessibility_and_fixed_day(self):
        self.meetings(meeting("m1", "2026-11-24T10:00:00-08:00", body="Board & Co \"quoted\""))
        self.agencies({})
        self.build()
        page = self.read()
        self.assertIn("Board &amp; Co &quot;quoted&quot;", page)
        self.assertNotIn("Board & Co", page)
        self.assertIn("<h2>Where things stand</h2><", self.strip(page) + "<")
        self.assertIn("<ul><li>", self.strip(page))
        first = self.read()
        old = os.environ.get("TZ")
        self.addCleanup(lambda: (os.environ.pop("TZ", None) if old is None else os.environ.__setitem__("TZ", old), time.tzset()))
        os.environ["TZ"] = "Pacific/Kiritimati"
        time.tzset()
        self.build()
        self.assertEqual(self.read(), first)

    # 6. no private reads; legacy fallback adds no asks
    def test_private_reads_guarded_and_legacy_fallback(self):
        kit = self.root / "kit"
        kit.mkdir()
        (kit / "agencies.json").write_text(json.dumps([{"agency_id": POLICE, "asked": [ask("POISON")]}]), encoding="utf-8")
        (kit / "sent.json").write_text("{}", encoding="utf-8")
        self.agencies({POLICE: [ask("Public")]})
        opened = []
        real = {"b": builtins.open, "i": io.open, "o": os.open}

        def watch(fn):
            def guarded(file, *args, **kwargs):
                if isinstance(file, (str, bytes, os.PathLike)) and Path(os.fsdecode(file)).is_relative_to(kit):
                    opened.append(str(file))
                return fn(file, *args, **kwargs)
            return guarded
        with mock.patch("builtins.open", watch(real["b"])), mock.patch("io.open", watch(real["i"])), mock.patch("os.open", watch(real["o"])):
            self.build()
        self.assertEqual(opened, [])
        self.assertEqual(self.metrics()[0][2], "1")
        self.assertNotIn("POISON", self.read())

    # 7. cameras slug reserved even without a map; nothing written on refusal
    def test_camera_empty_reservation_atomic_and_unrelated_bytes(self):
        self.no_map()
        self.build()
        before = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.assertIn("cameras.html", before)
        pages = self.content / "pages"
        pages.mkdir(exist_ok=True)
        (pages / "cameras.json").write_text(json.dumps({"title": "Mine", "body_md": "x"}), encoding="utf-8")
        with self.assertRaisesRegex(ContentError, "slug cameras is reserved"):
            self.build()
        after = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.assertTrue(before == after, "a refused build changed the public tree")


if __name__ == "__main__":
    unittest.main()
