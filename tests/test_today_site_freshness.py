"""DF1109B-16: `today` warns before the public site goes wrong on its own.

(a) official_recheck_due: a content/officials.json row whose checked_on is 335 or more days old (within 30 days of the
365-day limit at which build refuses it, or past it). (b) one site_rebuild_due: what the built public/meetings.ics and
public/events.ics still list as upcoming that the site's own rules now call past, cancelled or no longer staged.

Synthetic campaigns only (temporary copies of the fictional example); read-only, nothing fetched.
Run from the repository root: python3 -B -m unittest tests.test_today_site_freshness -v
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool import cli, site, today
from campaign_tool.content import ContentError, load_content
from campaign_tool.site import build_site
from tests.test_site_events import event as _event

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
AS_OF = date(2026, 11, 10)
BUILD = date(2026, 11, 1)
COUNCIL, COUNCIL_NAME = "ca-cedar-city-council", "Cedar City Council (fictional)"
NEW = ("official_recheck_due", "site_rebuild_due")
WORDS = "the built site still lists %d meetings and %d events as upcoming that are now past, cancelled or no longer staged; rebuild and publish"


def event(number, starts_at, ends_at=None):
    return _event(number, starts_at=starts_at, ends_at=ends_at or starts_at)


def legacy_outputs(root):
    """Text and JSON of `today` for a campaign that has neither a roster nor a built site (names the ROOT placeholder)."""
    folder = Path(root) / "content" / "meetings"
    for name, starts in (("past-open", "2026-10-20T18:00:00-07:00"), ("soon", "2026-11-12T18:00:00-08:00")):
        (folder / f"{name}.json").write_text(json.dumps({"id": name, "body": "Cedar Board", "starts_at": starts,
                                                         "agency_id": COUNCIL}), encoding="utf-8")
    text = today.run_cli(root, "2026-11-10")
    data = today.run_cli(root, "2026-11-10", as_json=True)
    return text.replace(str(root), "<ROOT>"), data.replace(str(root), "<ROOT>")


class Base(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket.connect", "urllib.request.urlopen", "subprocess.Popen"):
            patcher = mock.patch(target, side_effect=AssertionError(f"{target} used"))
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-site-freshness-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.folder, self.public = self.root / "content" / "meetings", self.root / "public"
        for path in self.folder.glob("*.json"):
            path.unlink()

    def stage(self, ident, starts, **extra):
        document = {"id": ident, "body": f"Synthetic council {ident}", "starts_at": starts, "agency_id": COUNCIL, **extra}
        (self.folder / f"{ident}.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    def stage_events(self, rows):
        (self.root / "content" / "events.json").write_text(json.dumps(rows), encoding="utf-8")

    def build(self, day=BUILD):
        build_site(self.root, today=day)

    def todo(self, as_of=AS_OF, kinds=None, **kw):
        items = today.todo(self.root, as_of, **kw)
        return [i for i in items if kinds is None or i["kind"] in kinds]

    def rebuild(self, as_of=AS_OF):
        return self.todo(as_of, ("site_rebuild_due",))

    def counts(self, as_of=AS_OF):
        found = self.rebuild(as_of)
        if not found:
            return (0, 0)
        self.assertEqual(len(found), 1)
        n, m = map(int, re.fullmatch(r"the built site still lists (\d+) meetings and (\d+) events as upcoming that are now "
                                     r"past, cancelled or no longer staged; rebuild and publish", found[0]["what"]).groups())
        return (n, m)

    def bad(self, label, as_of=AS_OF):
        return [i for i in self.todo(as_of, ("unreadable",)) if i["what"].startswith(f"fix {label}:")]

    def ics(self, name="meetings.ics"):
        return self.public / name

    def rewrite(self, name, change):
        path = self.ics(name)
        path.write_bytes(change(path.read_bytes().decode("utf-8")).encode("utf-8"))


class Roster(Base):
    def roster(self, people, agency=COUNCIL, raw=None):
        document = {"schema_version": 1, "bodies": [{"agency_id": agency, "officials": [
            {"name": name, "role": "Council member", "source_url": "https://example.invalid/roster", "checked_on": checked}
            for name, checked in people]}]}
        path = self.root / "content" / "officials.json"
        path.write_bytes(raw if raw is not None else json.dumps(document).encode("utf-8"))

    def aged(self, age):
        return (AS_OF - timedelta(age)).isoformat()

    def test_official_warning_age_334_335_365_366_and_far_stale(self):
        self.roster([("Age334", self.aged(334)), ("Age335", self.aged(335)), ("Age365", self.aged(365)),
                     ("Age366", self.aged(366)), ("Far Stale", "2023-01-05"), ("Leap Day", "2024-02-29")])
        items = self.todo(kinds=("official_recheck_due",))
        self.assertEqual([i["what"] for i in items], [
            "Far Stale, %s: checked 2023-01-05; build refuses this row from 2024-01-06" % COUNCIL_NAME,
            "Leap Day, %s: checked 2024-02-29; build refuses this row from 2025-03-01" % COUNCIL_NAME,
            "Age366, %s: checked 2025-11-09; build refuses this row from 2026-11-10" % COUNCIL_NAME,
            "Age365, %s: checked 2025-11-10; build refuses this row from 2026-11-11" % COUNCIL_NAME,
            "Age335, %s: checked 2025-12-10; build refuses this row from 2026-12-11" % COUNCIL_NAME])
        self.assertEqual([i["due"] for i in items], ["2024-01-06", "2025-03-01", "2026-11-10", "2026-11-11", "2026-12-11"])
        self.assertEqual(items[4]["command"], "officials recheck --directory %s --agency ca-cedar-city-council "
                         "--name Age335 --checked-on YYYY-MM-DD" % self.shell(self.root))
        self.assertEqual(set(items[0]), {"rank", "kind", "what", "due", "command"})
        # the build's own limit: age 365 is accepted, 366 refused (the first refusal day is checked_on + 366, not + 365)
        self.roster([("Age365", self.aged(365))])
        load_content(self.root, today=AS_OF)
        self.roster([("Age366", self.aged(366))])
        with self.assertRaisesRegex(ContentError, "older than 365 days"):
            load_content(self.root, today=AS_OF)

    @staticmethod
    def shell(path):
        import shlex
        return shlex.quote(str(path))

    def test_one_row_per_stored_person_with_stable_duplicate_and_name_ties(self):
        old = self.aged(400)
        self.roster([("Twin", old), ("Twin", old), ("Quote 'O Name", old), ("Ada", old)])
        items = self.todo(kinds=("official_recheck_due",))
        self.assertEqual([i["what"].split(",")[0] for i in items], ["Ada", "Quote 'O Name", "Twin", "Twin"])
        self.assertEqual(items[1]["command"].split(" --name ")[1].split(" --checked-on")[0], "'Quote '\"'\"'O Name'")
        self.assertEqual(self.todo(kinds=("official_recheck_due",)), items)  # stable on a second read

    def test_roster_maintenance_loader_is_not_publication_bypass(self):
        self.roster([("Stale One", self.aged(500))])
        self.assertEqual(len(self.todo(kinds=("official_recheck_due",))), 1)
        with self.assertRaisesRegex(ContentError, "older than 365 days"):  # build and content-check still refuse it
            load_content(self.root, today=AS_OF)
        self.assertEqual(self.todo(kinds=("unreadable",)), [])
        for path in (self.root / "content" / "officials.json",):
            path.unlink()
        self.assertEqual(self.todo(kinds=NEW), [])
        self.assertFalse((self.root / "content" / "officials.json").exists())  # nothing was created
        self.roster([("Fresh", self.aged(10))])
        self.assertEqual(self.todo(kinds=NEW), [])

    def test_unreadable_roster_is_one_item_and_hides_nothing(self):
        self.stage("late", "2026-10-20T18:00:00-07:00")  # outcome_missing, from another source
        path = self.root / "content" / "officials.json"
        other = self.root / "elsewhere.json"
        other.write_text("{}", encoding="utf-8")
        cases = {
            "future": lambda: self.roster([("Soon", (AS_OF + timedelta(1)).isoformat())]),
            "bad-json": lambda: self.roster([], raw=b"{not json"),
            "bad-utf8": lambda: self.roster([], raw=b'{"schema_version": 1, "bodies": "\xff"}'),
            "unknown-agency": lambda: self.roster([("Nobody", self.aged(400))], agency="no-such-agency"),
            "wrong-shape": lambda: self.roster([], raw=b'{"schema_version": 1}'),
            "oversize": lambda: self.roster([], raw=b" " * (2 * 1024 * 1024 + 1)),
            "link": lambda: path.symlink_to(other),
            "folder": lambda: path.mkdir(),
            "fifo": lambda: os.mkfifo(path),
        }
        for name, make in cases.items():
            with self.subTest(case=name):
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
                elif path.exists() or path.is_symlink():
                    path.unlink()
                make()
                items = self.todo()
                self.assertEqual(len(self.bad("content/officials.json")), 1)
                self.assertEqual([i["kind"] for i in items if i["kind"] in NEW], [])
                self.assertEqual(len([i for i in items if i["kind"] == "outcome_missing"]), 1)  # other work retained

    def test_named_campaign_selector_in_the_recheck_hint(self):
        self.roster([("Stale One", self.aged(500))])
        item = self.todo(kinds=("official_recheck_due",), campaign_name="demo")[0]
        self.assertEqual(item["command"], "officials recheck --campaign demo --agency ca-cedar-city-council "
                         "--name 'Stale One' --checked-on YYYY-MM-DD")


class Meetings(Base):
    def test_typed_meeting_day_not_dtstart_utc_day(self):
        self.stage("west", "2026-11-09T19:30:00-08:00")  # UTC 2026-11-10, typed day 11-09: past on 11-10
        self.stage("east", "2026-11-10T01:00:00+09:00")  # UTC 2026-11-09, typed day 11-10: still upcoming on 11-10
        self.build()
        calls = []
        real = site.meeting_is_upcoming
        with mock.patch.object(site, "meeting_is_upcoming", lambda m, day: calls.append((m["id"], day)) or real(m, day)):
            self.assertEqual(self.counts(), (1, 0))
        self.assertEqual(sorted(calls), [("east", AS_OF), ("west", AS_OF)])  # today asks the build's own rule
        item = self.rebuild()[0]
        self.assertEqual(item["what"], WORDS % (1, 0))
        self.assertEqual(item["command"], "build --directory %s" % Roster.shell(self.root))
        self.assertEqual((item["due"], item["kind"]), (None, "site_rebuild_due"))
        self.assertEqual(self.counts(date(2026, 11, 9)), (0, 0))
        self.assertEqual(self.counts(date(2026, 11, 11)), (2, 0))

    def test_cancelled_removed_and_unreadable_staged_members(self):
        for ident in ("a-cancel", "b-gone", "c-keep", "d-moved", "e-broken"):
            self.stage(ident, "2026-11-20T18:00:00-08:00")
        self.build()
        self.assertEqual(self.counts(), (0, 0))
        self.stage("a-cancel", "2026-11-20T18:00:00-08:00", status="cancelled")  # cancelled after the build
        (self.folder / "b-gone.json").unlink()  # no longer staged
        (self.folder / "d-moved.json").rename(self.folder / "other-name.json")  # explicit id differs from the filename
        self.assertEqual(self.counts(), (2, 0))
        (self.folder / "e-broken.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(self.counts(), (1, 0))  # the unreadable file adds no count; a missing id is withheld, not guessed
        broken = [i for i in self.todo(kinds=("unreadable",)) if "e-broken" in i["what"]]
        self.assertEqual(len(broken), 1)
        self.assertEqual(len([i for i in self.todo(kinds=("unreadable",)) if "content/meetings/" in i["what"]]), 1)

    def test_duplicate_staged_ids_are_unreadable_never_a_winner(self):
        self.stage("dup", "2026-11-20T18:00:00-08:00")
        self.stage("solo", "2026-11-20T18:00:00-08:00")
        self.build()
        document = json.loads((self.folder / "dup.json").read_text(encoding="utf-8"))
        document["starts_at"] = "2026-10-01T18:00:00-07:00"
        (self.folder / "copy.json").write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(self.counts(), (0, 0))
        labels = sorted(i["what"].split(":")[0] for i in self.todo(kinds=("unreadable",)))
        self.assertEqual(labels, ["fix content/meetings/copy.json", "fix content/meetings/dup.json"])

    def test_duplicate_ids_already_reported_by_a_continuation_are_not_reported_twice(self):
        self.stage("dup", "2026-11-20T18:00:00-08:00")
        self.build()
        outcome = {"result": "continued", "on": "2026-11-05", "source_url": "https://example.invalid/m",
                   "verified_by": "Synthetic Organizer", "verified_at": "2026-11-05", "continued_to_date": "2026-11-30"}
        self.stage("dup", "2026-11-20T18:00:00-08:00", outcome=outcome)
        self.stage("dup-two", "2026-11-21T18:00:00-08:00", id="dup")
        before = [i["what"].split(":")[0] for i in self.todo(kinds=("unreadable",))]
        self.assertEqual(len(before), len(set(before)))
        self.assertEqual(self.counts(), (0, 0))


class Events(Base):
    def test_event_utc_midnight_boundaries(self):
        self.stage_events([event(1, "2026-11-09T19:30:00-08:00", "2026-11-09T21:00:00-08:00"),
                           event(2, "2026-11-10T00:00:00Z", "2026-11-12T00:00:00Z"),
                           event(3, "2026-11-09T23:59:59Z", "2026-11-12T00:00:00Z"),
                           event(4, "2026-11-30T00:00:00Z", "2026-11-30T01:00:00Z")])
        self.build()
        self.assertEqual(self.counts(), (0, 1))  # only 23:59:59Z the day before; the end time is irrelevant
        self.assertEqual(self.counts(date(2026, 11, 11)), (0, 3))  # -08 evening is UTC 11-10 03:30: counts only now
        self.assertEqual(self.counts(date(2026, 11, 9)), (0, 0))
        self.assertEqual(self.rebuild()[0]["what"], WORDS % (0, 1))


class Calendars(Base):
    def test_one_rebuild_item_combines_independent_counts(self):
        self.stage("m-1", "2026-11-12T18:00:00-08:00")
        self.stage("m-2", "2026-11-13T18:00:00-08:00")
        self.stage_events([event(1, "2026-11-12T18:00:00-08:00"), event(2, "2026-11-13T18:00:00-08:00")])
        self.build()
        self.assertEqual(self.rebuild(), [])
        self.assertEqual(self.counts(date(2026, 11, 13)), (1, 0))  # N only: the 11-12 meeting
        self.assertEqual(self.counts(date(2026, 11, 14)), (2, 1))  # both: event 2 is UTC 11-14 02:00, still upcoming
        self.stage("m-1", "2026-11-12T18:00:00-08:00", status="cancelled")
        self.assertEqual(self.counts(date(2026, 11, 11)), (1, 0))
        (self.folder / "m-1.json").unlink()
        self.stage("m-1", "2026-11-12T18:00:00-08:00")
        self.assertEqual(self.counts(date(2026, 11, 13)), (1, 0))
        items = self.todo(date(2026, 11, 14))
        shown = [i for i in items if i["kind"] == "site_rebuild_due"]
        self.assertEqual(len(shown), 1)
        self.assertEqual(set(shown[0]), {"rank", "kind", "what", "due", "command"})
        self.assertEqual(shown[0]["what"], WORDS % (2, 1))
        self.assertEqual(self.todo(date(2026, 11, 14), ("unreadable",)), [])
        events_only = self.todo(date(2026, 11, 14), ("site_rebuild_due",))
        self.assertEqual(json.loads(today.run_cli(self.root, "2026-11-14", as_json=True))[shown[0]["rank"] - 1]["what"],
                         events_only[0]["what"])

    def test_same_uid_collapsed_and_conflicting_duplicate_refused(self):
        self.stage("m-1", "2026-11-12T18:00:00-08:00")
        self.build()
        block = re.search(r"BEGIN:VEVENT\r\n.*?END:VEVENT\r\n", self.ics().read_bytes().decode("utf-8"), re.S)[0]
        self.rewrite("meetings.ics", lambda t: t.replace("END:VCALENDAR", block + "END:VCALENDAR"))
        self.assertEqual(self.counts(date(2026, 11, 14)), (1, 0))  # the same event twice counts once
        self.rewrite("meetings.ics", lambda t: t.replace("END:VCALENDAR", block.replace("DTSTAMP:2026", "DTSTAMP:2025")
                                                         + "END:VCALENDAR"))
        self.assertEqual(self.counts(date(2026, 11, 14)), (0, 0))
        self.assertEqual(len(self.bad("public/meetings.ics")), 1)

    def test_shuffled_uids_count_the_same(self):
        for n in range(1, 5):
            self.stage(f"m-{n}", f"2026-11-1{n}T18:00:00-08:00")
        self.build()
        blocks = re.findall(r"BEGIN:VEVENT\r\n.*?END:VEVENT\r\n", self.ics().read_bytes().decode("utf-8"), re.S)
        head = self.ics().read_bytes().decode("utf-8").split("BEGIN:VEVENT")[0]
        self.ics().write_bytes((head + "".join(reversed(blocks)) + "END:VCALENDAR\r\n").encode("utf-8"))
        self.assertEqual(self.counts(date(2026, 11, 13)), (2, 0))

    def test_bad_sibling_is_one_unreadable_and_the_good_one_still_counts(self):
        self.stage("m-1", "2026-11-12T18:00:00-08:00")
        self.stage_events([event(1, "2026-11-12T18:00:00-08:00")])
        self.build()
        self.rewrite("events.ics", lambda t: t[:t.index("END:VCALENDAR")])  # truncated
        self.assertEqual(self.counts(date(2026, 11, 14)), (1, 0))
        self.assertEqual(len(self.bad("public/events.ics", date(2026, 11, 14))), 1)
        self.assertEqual(self.bad("public/meetings.ics", date(2026, 11, 14)), [])
        self.ics("events.ics").write_text("complete garbage\r\n", encoding="utf-8")
        self.assertEqual(len(self.bad("public/events.ics", date(2026, 11, 14))), 1)
        self.assertEqual(self.counts(date(2026, 11, 14)), (1, 0))

    def test_no_public_or_no_such_file_adds_nothing(self):
        self.stage("m-1", "2026-11-12T18:00:00-08:00")
        self.assertEqual(self.todo(date(2026, 12, 1), NEW), [])
        self.assertFalse(self.public.exists())  # not created
        self.build()
        self.ics("meetings.ics").unlink()
        self.assertEqual(self.todo(date(2026, 12, 1), ("site_rebuild_due", "unreadable")), [])
        self.ics("events.ics").unlink(missing_ok=True)
        self.assertEqual(self.todo(date(2026, 12, 1), ("site_rebuild_due", "unreadable")), [])

    def test_valid_empty_calendar_is_clean(self):
        self.build()  # no meetings, no events
        self.assertTrue(self.ics().is_file())
        self.assertEqual(self.todo(date(2026, 12, 1), ("site_rebuild_due", "unreadable")), [])

    def test_local_ics_reader_folded_and_refused_inputs(self):
        self.stage("m-1", "2026-11-12T18:00:00-08:00", location="Café «Ünïcode» " + "long " * 40,
                   agenda_item="Item 4: " + "caméra " * 30)
        self.build()
        self.assertIn(b"\r\n ", self.ics().read_bytes())  # the real writer folded it
        late = date(2026, 12, 1)
        self.assertEqual(self.counts(late), (1, 0))
        good = self.ics().read_bytes()
        block = re.search(r"BEGIN:VEVENT\r\n.*?END:VEVENT\r\n", good.decode("utf-8"), re.S)[0]

        def many(t):
            return t.replace("END:VCALENDAR", "".join(block.replace("meeting-m-1@", f"meeting-m-{n}@") for n in range(2, 2003))
                             + "END:VCALENDAR")
        bad = {
            "truncated": lambda t: t[:t.index("END:VCALENDAR")],
            "garbage": lambda t: "not a calendar\r\n",
            "empty": lambda t: "",
            "version": lambda t: t.replace("VERSION:2.0", "VERSION:1.0"),
            "nested": lambda t: t.replace("BEGIN:VEVENT\r\n", "BEGIN:VEVENT\r\nBEGIN:VALARM\r\n", 1),
            "unclosed": lambda t: t.replace("END:VEVENT\r\n", "", 1),
            "no-uid": lambda t: re.sub(r"UID:[^\r\n]*\r\n", "", t, count=1),
            "no-dtstart": lambda t: re.sub(r"DTSTART:[^\r\n]*\r\n", "", t, count=1),
            "recurrence": lambda t: t.replace("DTSTAMP:", "RRULE:FREQ=DAILY\r\nDTSTAMP:", 1),
            "local-start": lambda t: re.sub(r"(DTSTART:\d{8}T\d{6})Z", r"\1", t, count=1),
            "zoned-start": lambda t: t.replace("DTSTART:", "DTSTART;TZID=America/Los_Angeles:", 1),
            "wrong-prefix": lambda t: t.replace("UID:meeting-", "UID:event-", 1),
            "bad-slug": lambda t: t.replace("UID:meeting-m-1@", "UID:meeting-M_1@", 1),
            "no-host": lambda t: t.replace("UID:meeting-m-1@", "UID:meeting-m-1", 1),
            "impossible-date": lambda t: re.sub(r"DTSTART:\d{8}", "DTSTART:20261399", t, count=1),
            "too-many-events": many,
        }
        for name, change in bad.items():
            with self.subTest(case=name):
                self.ics().write_bytes(change(good.decode("utf-8")).encode("utf-8"))
                self.assertEqual(self.counts(late), (0, 0))
                self.assertEqual(len(self.bad("public/meetings.ics", late)), 1)
        for name, data in (("bad-utf8", good + b"\xff\xfe"), ("too-big", good + b"X-PAD:" + b"a" * (2 * 1024 * 1024) + b"\r\n")):
            with self.subTest(case=name):
                self.ics().write_bytes(data)
                self.assertEqual(self.counts(late), (0, 0))
                self.assertEqual(len(self.bad("public/meetings.ics", late)), 1)
        elsewhere = self.root / "elsewhere.ics"
        elsewhere.write_bytes(good)
        for name in ("link", "dangling-link", "folder", "fifo"):
            with self.subTest(case=name):
                path = self.ics()
                path.unlink() if path.is_file() or path.is_symlink() else path.rmdir()
                if name == "link":
                    path.symlink_to(elsewhere)
                elif name == "dangling-link":
                    path.symlink_to(self.root / "missing.ics")
                elif name == "folder":
                    path.mkdir()
                else:
                    os.mkfifo(path)
                self.assertEqual(self.counts(late), (0, 0))
                self.assertEqual(len(self.bad("public/meetings.ics", late)), 1)
        for name in ("public-link", "public-file"):
            with self.subTest(case=name):
                if self.public.is_dir() and not self.public.is_symlink():
                    shutil.rmtree(self.public)
                else:
                    self.public.unlink(missing_ok=True)
                if name == "public-link":
                    real = self.root / "real-public"
                    real.mkdir(exist_ok=True)
                    (real / "meetings.ics").write_bytes(good)
                    self.public.symlink_to(real)
                else:
                    self.public.write_text("x", encoding="utf-8")
                self.assertEqual(self.counts(late), (0, 0))
                self.assertEqual(len(self.bad("public/meetings.ics", late)), 1)

    def test_only_the_two_root_calendars_are_read(self):
        self.stage("m-1", "2026-11-12T18:00:00-08:00")
        self.stage_events([event(1, "2026-11-12T18:00:00-08:00")])
        self.build()
        (self.public / "agencies").mkdir(exist_ok=True)
        (self.public / "agencies" / "cedar.ics").write_bytes(b"\xff garbage")
        (self.public / "meetings.html").write_bytes(b"\xff\xff <li>meeting-gone-1</li>")
        self.assertEqual(self.counts(date(2026, 11, 9)), (0, 0))
        self.assertEqual(self.todo(date(2026, 11, 9), ("unreadable",)), [])
        opened, real = [], os.open

        def spy(path, *args, **kwargs):
            opened.append(str(path))
            return real(path, *args, **kwargs)
        with mock.patch("os.open", spy):
            self.counts(date(2026, 11, 14))
        self.assertEqual(sorted({p for p in opened if not p.startswith("/")}), ["events.ics", "kit", "meetings.ics", "public"])


class DefaultBytes(Base):
    # captured from main (before any DF1109B-16 code) with legacy_outputs(); the "<ROOT>" placeholder stands for the folder
    LEGACY_TEXT = (
        "1. [outcome_missing] Cedar Board was scheduled for 2026-10-20 (past-open) with no outcome recorded: verify what "
        "happened, then record it — meetings-outcome --directory <ROOT> --id past-open --result RESULT --on YYYY-MM-DD "
        "--source OFFICIAL_URL --verified-by NAME\n2. [participation_unverified] Verify how the public may take part in "
        "Cedar Board on 2026-11-12 (soon) — verify how the public may take part, then meetings-stage --directory <ROOT> "
        "--id soon --participation FILE --replace")
    LEGACY_JSON_SHA = "97386ff706c8895ef05398cd9e7776e014fdfc7a1bcd0834cda023fa23388378"  # pragma: allowlist secret

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes() if p.is_file() else None, p.lstat().st_mode)
                for p in sorted(self.root.rglob("*"))}

    def test_today_default_bytes_where_nothing_is_new(self):
        text, data = legacy_outputs(self.root)
        self.assertEqual(text, self.LEGACY_TEXT)
        self.assertEqual(hashlib.sha256(data.encode("utf-8")).hexdigest(), self.LEGACY_JSON_SHA)

    def test_a_current_built_site_and_a_fresh_roster_change_no_byte(self):
        before = legacy_outputs(self.root)
        self.build(date(2026, 11, 10))  # built on the as-of day: everything it lists is current
        roster = {"schema_version": 1, "bodies": [{"agency_id": COUNCIL, "officials": [
            {"name": "Fresh Member", "role": "Council member", "source_url": "https://example.invalid/r",
             "checked_on": "2026-10-01"}]}]}
        (self.root / "content" / "officials.json").write_text(json.dumps(roster), encoding="utf-8")
        text = today.run_cli(self.root, "2026-11-10").replace(str(self.root), "<ROOT>")
        data = today.run_cli(self.root, "2026-11-10", as_json=True).replace(str(self.root), "<ROOT>")
        # the staged meetings are the same files; only the build's own page/feed bytes are new and are not read
        self.assertEqual((text, data), before)

    def test_read_only_with_no_fetch_build_or_new_file(self):
        self.stage("m-1", "2026-11-12T18:00:00-08:00")
        self.stage_events([event(1, "2026-11-12T18:00:00-08:00")])
        self.build()
        (self.root / "content" / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": [
            {"agency_id": COUNCIL, "officials": [{"name": "Old", "role": "Member", "source_url": "https://example.invalid/r",
                                                  "checked_on": "2025-12-01"}]}]}), encoding="utf-8")
        before = self.snapshot()
        with mock.patch.object(site, "build_site", side_effect=AssertionError("build used")), \
                mock.patch("os.mkdir", side_effect=AssertionError("mkdir used")):
            items = self.todo(date(2026, 12, 1), NEW)
        self.assertEqual([i["kind"] for i in items], ["official_recheck_due", "site_rebuild_due"])
        self.ics("meetings.ics").write_text("garbage", encoding="utf-8")
        before = self.snapshot()
        self.assertEqual(len(self.bad("public/meetings.ics", date(2026, 12, 1))), 1)
        self.assertEqual(self.snapshot(), before)

    def test_cli_exit_zero_with_new_items_and_unreadable(self):
        self.stage("m-1", "2026-11-12T18:00:00-08:00")
        self.build()
        self.ics("events.ics").write_text("garbage", encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["campaign_tool", "today", "--directory", str(self.root), "--as-of",
                                             "2026-12-01", "--json"]), contextlib.redirect_stdout(out):
            cli.main()
        kinds = [row["kind"] for row in json.loads(out.getvalue())]
        self.assertIn("site_rebuild_due", kinds)
        self.assertIn("unreadable", kinds)
        self.assertLess(kinds.index("site_rebuild_due"), kinds.index("unreadable"))


class KindOrder(unittest.TestCase):
    def test_kinds_sit_after_meeting_not_staged_before_minutes_pending(self):
        order = list(today.KINDS)
        at = order.index("meeting_not_staged")
        self.assertEqual(order[at:at + 4], ["meeting_not_staged", "official_recheck_due", "site_rebuild_due", "minutes_pending"])


if __name__ == "__main__":
    unittest.main()
