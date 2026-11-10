"""DF1109B-16: the upcoming/past meeting split is one shared predicate, ``site.meeting_is_upcoming(meeting, day)``,
and moving it there changed no built byte.

The digests in tests/fixtures/site-meeting-partition-main.json were captured from the whole built tree on main BEFORE
the predicate was moved (the capture runs ``scenario_digest`` over ``SCENARIOS``); this module only compares.
Synthetic content (temporary copies of the fictional example); nothing is fetched.

Run from the repository root: python3 -B -m unittest tests.test_site_meeting_partition -v
"""
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import site
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
PINS = REPO_ROOT / "tests" / "fixtures" / "site-meeting-partition-main.json"
AGENCY, POLICE = "ca-cedar-city-council", "ca-cedar-police"
OUTCOME = {"result": "approved", "on": "2026-11-06", "source_url": "https://example.invalid/m",
           "verified_by": "Synthetic Organizer", "verified_at": "2026-11-06"}


def event(number, starts_at):
    return {"id": f"forum-{number}", "title": f"Teach-in {number}", "starts_at": starts_at,
            "ends_at": starts_at, "venue": f"Cedar Library room {number}", "description_md": "Synthetic."}


def meeting(ident, starts_at, agency=AGENCY, **extra):
    return {"id": ident, "body": f"Synthetic council {ident} (fictional)", "starts_at": starts_at,
            "agency_id": agency, **extra}


# name -> (build day, meetings, events or None); every build starts from the example without its own meetings
SCENARIOS = {
    "no-meetings": ("2026-11-10", [], None),
    "only-past": ("2026-11-10", [meeting("old-a", "2026-11-01T18:00:00-08:00"),
                                 meeting("old-b", "2026-11-09T23:59:00+00:00")], None),
    "day-of": ("2026-11-10", [meeting("same-day", "2026-11-10T00:00:00+14:00"),
                              meeting("same-late", "2026-11-10T23:59:00-12:00")], None),
    "both-offsets": ("2026-11-10", [meeting("west", "2026-11-09T19:30:00-08:00"),
                                    meeting("east", "2026-11-10T01:00:00+09:00"),
                                    meeting("zulu", "2026-11-10T00:00:00Z")], None),
    "cancelled": ("2026-11-10", [meeting("kept", "2026-11-12T18:00:00-08:00"),
                                 meeting("dropped", "2026-11-13T18:00:00-08:00", status="cancelled"),
                                 meeting("dropped-past", "2026-11-01T18:00:00-08:00", status="cancelled")], None),
    "outcome": ("2026-11-10", [meeting("heard", "2026-11-05T18:00:00-08:00", outcome=OUTCOME),
                               meeting("ahead", "2026-12-01T18:00:00-08:00")], None),
    "per-agency-feeds": ("2026-11-10", [meeting("a-1", "2026-11-11T18:00:00-08:00", POLICE),
                                        meeting("a-2", "2026-11-20T18:00:00-08:00", POLICE),
                                        meeting("a-3", "2026-10-20T18:00:00-08:00", POLICE),
                                        meeting("a-4", "2026-11-14T18:00:00-08:00", POLICE, status="cancelled"),
                                        meeting("a-5", "2026-11-15T18:00:00-08:00")], None),
    "events-enabled": ("2026-11-10", [meeting("with-events", "2026-11-11T18:00:00-08:00")],
                       [event(1, "2026-11-09T19:30:00-08:00"), event(2, "2026-11-10T00:00:00Z"),
                        event(3, "2026-11-12T18:00:00-08:00")]),
}


def scenario_digest(name):
    """Build one scenario in a fresh copy and return {"files": n, "tree": sha256 of every path and byte, "key": ...}."""
    day, meetings, events = SCENARIOS[name]
    with tempfile.TemporaryDirectory(prefix="synthetic-partition-") as temporary:
        root = Path(temporary) / "campaign"
        shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
        folder = root / "content" / "meetings"
        for path in folder.glob("*.json"):
            path.unlink()
        for row in meetings:
            (folder / f"{row['id']}.json").write_text(json.dumps(row), encoding="utf-8")
        if events is not None:
            (root / "content" / "events.json").write_text(json.dumps(events), encoding="utf-8")
        build_site(root, today=date.fromisoformat(day))
        public = root / "public"
        files = {p.relative_to(public).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(public.rglob("*")) if p.is_file()}
    tree = hashlib.sha256("".join(f"{path}\0{digest}\n" for path, digest in files.items()).encode()).hexdigest()
    key = {path: digest for path, digest in files.items()
           if path in ("meetings.html", "meetings.ics", "index.html", "events.ics", "events.html", "sitemap.xml")
           or path.startswith("agencies/")}
    return {"files": len(files), "tree": tree, "key": key}


def legacy_split(meetings, day):
    """The partition as Site.__init__ wrote it on main, before the shared predicate."""
    cutoff = day.isoformat()
    upcoming = [m for m in meetings if m["starts_at"][:10] >= cutoff and m["status"] != "cancelled"]
    past = [m for m in meetings if m["starts_at"][:10] < cutoff or m["status"] == "cancelled"][::-1]
    return upcoming, past


class MeetingPartition(unittest.TestCase):
    def test_predicate_is_the_typed_calendar_day_and_never_cancelled(self):
        day = date(2026, 11, 10)
        rows = (("2026-11-09T19:30:00-08:00", "scheduled", False),  # UTC day is 11-10, typed day is 11-09
                ("2026-11-10T01:00:00+09:00", "scheduled", True),   # UTC day is 11-09, typed day is 11-10
                ("2026-11-09T23:59:59+00:00", "scheduled", False),
                ("2026-11-10T00:00:00+14:00", "scheduled", True),
                ("2026-11-10T23:59:59-12:00", "scheduled", True),
                ("2026-11-11T00:00:00-12:00", "scheduled", True),
                ("2026-11-10T09:00:00-08:00", "cancelled", False),
                ("2026-11-01T09:00:00-08:00", "cancelled", False))
        for starts_at, status, expected in rows:
            with self.subTest(starts_at=starts_at, status=status):
                self.assertIs(site.meeting_is_upcoming({"starts_at": starts_at, "status": status}, day), expected)

    def test_split_of_a_model_matches_the_rule_main_wrote_inline(self):
        for day in (date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)):
            rows = [{"id": f"m{n}", "starts_at": starts, "status": "cancelled" if n % 4 == 3 else "scheduled"}
                    for n, starts in enumerate(("2026-11-09T19:30:00-08:00", "2026-11-10T01:00:00+09:00",
                                                "2026-11-10T00:00:00Z", "2026-11-30T09:00:00-08:00",
                                                "2026-10-30T09:00:00-08:00", "2026-11-09T23:59:59+00:00",
                                                "2026-11-11T00:30:00-12:00", "2026-11-10T12:00:00-08:00"))]
            upcoming, past = legacy_split(rows, day)
            self.assertEqual(upcoming, [m for m in rows if site.meeting_is_upcoming(m, day)])
            self.assertEqual(past, [m for m in rows if not site.meeting_is_upcoming(m, day)][::-1])
            self.assertEqual(len(upcoming) + len(past), len(rows))

    def test_build_calls_the_shared_predicate_once_per_meeting_per_list(self):
        day = date(2026, 11, 10)
        calls = []
        real = site.meeting_is_upcoming

        def spy(meeting, when):
            calls.append((meeting["id"], when))
            return real(meeting, when)
        with mock.patch.object(site, "meeting_is_upcoming", spy), tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "campaign"
            shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
            folder = root / "content" / "meetings"
            for path in folder.glob("*.json"):
                path.unlink()
            for row in SCENARIOS["both-offsets"][1]:
                (folder / f"{row['id']}.json").write_text(json.dumps(row), encoding="utf-8")
            build_site(root, today=day)
            ics = (root / "public" / "meetings.ics").read_text(encoding="utf-8")
        self.assertEqual(sorted(calls), sorted((ident, day) for ident in ("west", "east", "zulu") for _ in (0, 1)))
        self.assertNotIn("UID:meeting-west@", ics)  # typed day 11-09 is past although its UTC day is the 10th
        self.assertIn("UID:meeting-east@", ics)
        self.assertIn("UID:meeting-zulu@", ics)

    def test_full_build_tree_byte_identical_for_partition_refactor(self):
        pins = json.loads(PINS.read_text(encoding="utf-8"))
        self.assertEqual(sorted(pins), sorted(SCENARIOS))
        for name in SCENARIOS:
            with self.subTest(scenario=name):
                got = scenario_digest(name)
                self.assertEqual(got["files"], pins[name]["files"])
                for path, digest in pins[name]["key"].items():
                    self.assertEqual(got["key"].get(path), digest, path)
                self.assertEqual(got["tree"], pins[name]["tree"])

    def test_built_scenarios_exercise_per_agency_feeds_and_events(self):
        pins = json.loads(PINS.read_text(encoding="utf-8"))
        self.assertTrue(any(path.startswith("agencies/") and path.endswith(".ics")
                            for path in pins["per-agency-feeds"]["key"]))
        self.assertIn("events.ics", pins["events-enabled"]["key"])
        self.assertNotIn("events.ics", pins["no-meetings"]["key"])


if __name__ == "__main__":
    unittest.main()
