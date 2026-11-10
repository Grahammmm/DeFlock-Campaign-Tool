"""DF1109B-16: the upcoming/past meeting split is one shared predicate, ``site.meeting_is_upcoming(meeting, day)``,
and moving it there changed no built byte.

The digests in PINS below were captured from the whole built tree on main BEFORE
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
AGENCY, POLICE = "ca-cedar-city-council", "ca-cedar-police"
OUTCOME = {"result": "approved", "on": "2026-11-06", "source_url": "https://example.invalid/m",
           "verified_by": "Synthetic Organizer", "verified_at": "2026-11-06"}


# name -> {files, key (selected file digests), tree}; sha256 of the built public/ tree captured on main
PINS = {
    'both-offsets': {
        "files": 51,
        "key": {
            'agencies/cedar.html': "6ae9dba30b6b443dafab4420b8ebea6ca1253a785d466f633dffb5064a1f34d5",  # pragma: allowlist secret
            'index.html': "2c61e13fb4b3cb6809f96ffcb55dbd723c1156b5e0e3dfb9e60853d61272134d",  # pragma: allowlist secret
            'meetings.html': "bfbae57878ea9f004bce9b6a42a035efc8688ed4d775fb22ad6289a34940be34",  # pragma: allowlist secret
            'meetings.ics': "6cdce01d1052ed5f8f8aeee1f65758eb884360069569893dbf27dc74b7f24492",  # pragma: allowlist secret
            'sitemap.xml': "3ea9aee8db466883be77be732eea2da69d4b0fe54dfb1163c50c7413604e998e",  # pragma: allowlist secret
        },
        "tree": "29e4ae592b70eb09e74a8bab9d103240fc1cb533b9d7f8d1bfee944af12706b4",  # pragma: allowlist secret
    },
    'cancelled': {
        "files": 51,
        "key": {
            'agencies/cedar.html': "6ae9dba30b6b443dafab4420b8ebea6ca1253a785d466f633dffb5064a1f34d5",  # pragma: allowlist secret
            'index.html': "c660e4f6b7b3a2a77457a9dd9ff02945614df49b8534f904962bf5b81d0c59a6",  # pragma: allowlist secret
            'meetings.html': "f58c25fceff81a6e4a29eb8092d0e9c03954b4fa7e9278fc48df8d2a4bf482e5",  # pragma: allowlist secret
            'meetings.ics': "257be980e6dd7bd609506cf18ed9484ea74d29bcb2bdb709d81b5cf881335445",  # pragma: allowlist secret
            'sitemap.xml': "2b1d0e3768ff9e8b6a26c8edf1f7b5de057240cb60c4861c317e694ad39764f7",  # pragma: allowlist secret
        },
        "tree": "1103841926ddb6cd66e372dfc091be5517dd76ace6597d05148888c25cdf40a0",  # pragma: allowlist secret
    },
    'day-of': {
        "files": 50,
        "key": {
            'agencies/cedar.html': "6ae9dba30b6b443dafab4420b8ebea6ca1253a785d466f633dffb5064a1f34d5",  # pragma: allowlist secret
            'index.html': "e8286f856b21770c65fe7138729f6ddc1233704d9d52d0e3eddadbcf905fe243",  # pragma: allowlist secret
            'meetings.html': "154f4981e20336a33ea73892496a6aeb3156d8a80a3ee6890b12e10ed4b4bb73",  # pragma: allowlist secret
            'meetings.ics': "229d3667bde7c966b47e0bab00dfd4c088b793cbc091b12576594fde0d7ba2ed",  # pragma: allowlist secret
            'sitemap.xml': "38021adbd0e1c4c478178c05744285b96c494317142381baa15ea2cdcf1c2875",  # pragma: allowlist secret
        },
        "tree": "306cc0dfb620bfea649edbc12ef9390a252f9a9d7b5eb272ea72674887b4dd1e",  # pragma: allowlist secret
    },
    'events-enabled': {
        "files": 50,
        "key": {
            'agencies/cedar.html': "a6ba813c87ca60b4216648e76ddf2c27fd7d63e637327aa836ffaeb5502acd47",  # pragma: allowlist secret
            'events.html': "cdff1336e0b5482e6bf4fe7d75f10686ebb7d530237342d3139b432d846ef13c",  # pragma: allowlist secret
            'events.ics': "2d049115cb0ebec465e00db4459d140ae3e39fdfe319f91c93204054bc4a56f8",  # pragma: allowlist secret
            'index.html': "b91124c3b87d8e6f3ac297fa683684db22a1e7d2c37add8fe567ac70b8b877a2",  # pragma: allowlist secret
            'meetings.html': "e10339ac57db559057da9985d3e7faeffa9641af5d8eea8e09922afe9e1176c6",  # pragma: allowlist secret
            'meetings.ics': "c491d9072ca00dbc164dd9b001f51fdcfd439109aba282f3fe49c5606877df66",  # pragma: allowlist secret
            'sitemap.xml': "d05f44489064d7e6b1266c0c3b1c084e45fc9a063734d57eb099e1165a2b5b0d",  # pragma: allowlist secret
        },
        "tree": "fe88b1e75270916a7410a1fe59ab0715153c796f50b6b726ebbfd893f58e2f1f",  # pragma: allowlist secret
    },
    'no-meetings': {
        "files": 46,
        "key": {
            'agencies/cedar.html': "8c8bd3cc51870082639cd83b15891dbe481aac4ff21d63aa277d2fb249718a3c",  # pragma: allowlist secret
            'index.html': "4dbc65433a2b27b47654a19c155a91f28c59b81d444a358b3c173e33b774faba",  # pragma: allowlist secret
            'meetings.html': "d03229d420988305135ad3dca45ab80426404800bb29037c7ea532b767135afd",  # pragma: allowlist secret
            'meetings.ics': "39cabc1d64bf3e8af0e59f34e668a5ec23c6fa766e6f4b37def1eed7604a5170",  # pragma: allowlist secret
            'sitemap.xml': "ff362e218fb7a56e11009c0f8f289a07ca6a662a1ab76970238dc1e6651d2d71",  # pragma: allowlist secret
        },
        "tree": "5986abe6175377e65e175b504902f74852f5c011a03b30a71cb1fe4a62f7358e",  # pragma: allowlist secret
    },
    'only-past': {
        "files": 50,
        "key": {
            'agencies/cedar.html': "6ae9dba30b6b443dafab4420b8ebea6ca1253a785d466f633dffb5064a1f34d5",  # pragma: allowlist secret
            'index.html': "7445513c1545f6a0561648ed0523a531e5420e6523e6d94526305ec06f25a844",  # pragma: allowlist secret
            'meetings.html': "0a99e95e5b5656485a34e55b64559265fe04f6f1ecea3a750ce0fbc40c49ec4c",  # pragma: allowlist secret
            'meetings.ics': "39cabc1d64bf3e8af0e59f34e668a5ec23c6fa766e6f4b37def1eed7604a5170",  # pragma: allowlist secret
            'sitemap.xml': "60d5d7c76e1573fe0a951fbd9019b681cee839c541acb8a3f5f6b570ac35c5fc",  # pragma: allowlist secret
        },
        "tree": "00458e89c4f81474bda9a0c235a8dea3e04132049fde1fe5d1653d111a5ebb31",  # pragma: allowlist secret
    },
    'outcome': {
        "files": 50,
        "key": {
            'agencies/cedar.html': "6ae9dba30b6b443dafab4420b8ebea6ca1253a785d466f633dffb5064a1f34d5",  # pragma: allowlist secret
            'index.html': "aa6ae5f3441ed4f04d8aa4c43439252562dce09ddb153d7e9ad23edb8e893504",  # pragma: allowlist secret
            'meetings.html': "d941acfd8526751ebde8ab0feb0e61beb715ef53916069981df882a8e1fea8cf",  # pragma: allowlist secret
            'meetings.ics': "de4da2072e04bc527cc897a65bf0ff8e86ca7dad9e0a2faa4cf7ef15a9aac45a",  # pragma: allowlist secret
            'sitemap.xml': "4066230aad1ae491562bad68c9d945eb5d48c18d5b15476d48f2e29be8e12989",  # pragma: allowlist secret
        },
        "tree": "c356ee0923f6ea73e557e1f890083aceee57f0dbcfa80cc245c6bef4df435fac",  # pragma: allowlist secret
    },
    'per-agency-feeds': {
        "files": 54,
        "key": {
            'agencies/cedar.html': "b4391126fd342e3e38a166493e7f8094532b89144efc6467906a9d82902bc91d",  # pragma: allowlist secret
            'agencies/cedar.ics': "3e4f0f9ed89e756c3a15ef8dcb4c47076d854e3270e03030f334b76aaef67a1c",  # pragma: allowlist secret
            'index.html': "888ca5b59f314a86f50fd0fb4edcdd673ffd9823bf6be5d9d946fbad3428dbd2",  # pragma: allowlist secret
            'meetings.html': "e031f9ccb3ea300128aea5527ce87dfe23e8040917d53ded4682dc1cec7f44c2",  # pragma: allowlist secret
            'meetings.ics': "cca8d16d84b5a5c1de28e5881f331086868bed6bbdfe398ac795b0baaed58bda",  # pragma: allowlist secret
            'sitemap.xml': "acfc27c694ff16bbacf0181dddde9f2ab29b313618c26fbd6490ab58600a6c2c",  # pragma: allowlist secret
        },
        "tree": "16dd9c541875b90816338d82c2b028c16e68f02247a7aebdd21acac783ee03b7",  # pragma: allowlist secret
    },
}


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
        pins = PINS
        self.assertEqual(sorted(pins), sorted(SCENARIOS))
        for name in SCENARIOS:
            with self.subTest(scenario=name):
                got = scenario_digest(name)
                self.assertEqual(got["files"], pins[name]["files"])
                for path, digest in pins[name]["key"].items():
                    self.assertEqual(got["key"].get(path), digest, path)
                self.assertEqual(got["tree"], pins[name]["tree"])

    def test_built_scenarios_exercise_per_agency_feeds_and_events(self):
        pins = PINS
        self.assertTrue(any(path.startswith("agencies/") and path.endswith(".ics")
                            for path in pins["per-agency-feeds"]["key"]))
        self.assertIn("events.ics", pins["events-enabled"]["key"])
        self.assertNotIn("events.ics", pins["no-meetings"]["key"])


if __name__ == "__main__":
    unittest.main()
