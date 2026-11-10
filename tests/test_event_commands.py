"""DF1109-14: ``event add|remove|list`` edits content/events.json from local wall-clock times and an IANA zone.

Expected offsets and UTC instants below are written out by hand from the published zone rules, never computed by the
converter under test. Temporary copies of the fictional example only; sockets are blocked, nothing is fetched.
"""
import json
import os
import shutil
import stat
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from campaign_tool import events
from campaign_tool.content_events import build_cutoff, partition, read_events
from campaign_tool.events import EventError, add_event, list_events, local_moment, remove_event
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NO_SOCKETS = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))
LA = "America/Los_Angeles"


def snapshot(root):
    found = {}
    for path in sorted(Path(root).rglob("*")):
        info = os.lstat(path)
        found[path.relative_to(root).as_posix()] = (
            None if stat.S_ISDIR(info.st_mode) else path.read_bytes() if stat.S_ISREG(info.st_mode) else "special",
            stat.S_IMODE(info.st_mode))
    return found


class EventCommandCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-event-cmd-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.path = self.content / "events.json"
        self.description = self.tmp / "description.md"
        self.description.write_bytes("How to read a records response.\n\n**All welcome.** Caf\u00e9 \u2014 bring notes.\n".encode())
        NO_SOCKETS.start()
        self.addCleanup(NO_SOCKETS.stop)

    def add(self, event_id="teach-in", starts="2026-11-09T18:00", ends="2026-11-09T20:00", tz=LA, **extra):
        extra.setdefault("venue", None if extra.get("online") else "Cedar Library, room B")
        return add_event(self.root, event_id=event_id, title=extra.pop("title", "Records teach-in"), starts=starts,
                         ends=ends, tz=tz, description_file=extra.pop("description_file", self.description), **extra)

    def stored(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def refused(self, pattern, call, *args, **kwargs):
        """The call raises EventError matching ``pattern`` and the whole campaign is byte- and mode-identical."""
        before = snapshot(self.root)
        with self.assertRaisesRegex(EventError, pattern) as caught:
            call(*args, **kwargs)
        self.assertEqual(snapshot(self.root), before, "a refusal changed the campaign")
        self.assertEqual(caught.exception.exit_code, 2)
        return str(caught.exception)


class ConversionTests(EventCommandCase):
    def test_gap_and_overlap_refuse_each_endpoint(self):
        # 2026-03-08 02:30 does not exist in Los Angeles (clocks jump 02:00 -> 03:00); 2026-11-01 01:30 happens twice.
        cases = (("2026-03-08T02:30", "2026-03-08T04:00", "--starts: nonexistent"),
                 ("2026-03-08T01:00", "2026-03-08T02:30", "--ends: nonexistent"),
                 ("2026-11-01T01:30", "2026-11-01T03:00", "--starts: ambiguous"),
                 ("2026-10-31T23:00", "2026-11-01T01:30", "--ends: ambiguous"))
        for starts, ends, pattern in cases:
            with self.subTest(starts=starts, ends=ends):
                self.refused(pattern, self.add, starts=starts, ends=ends)  # absent target stays absent
                self.assertFalse(self.path.exists())
        self.add(event_id="kept")
        for starts, ends, pattern in cases:
            with self.subTest(existing=True, starts=starts):
                self.refused(pattern, self.add, event_id="other", starts=starts, ends=ends)  # old bytes unchanged

    def test_normal_fold_deduplication_and_non_hour_transitions(self):
        self.add(event_id="la-winter")
        self.add(event_id="la-summer", starts="2026-07-04T09:05:07", ends="2026-07-04T11:00")
        self.add(event_id="utc", starts="2026-11-03T12:00", ends="2026-11-03T13:00", tz="UTC", online=True)
        self.add(event_id="lord-howe", starts="2026-01-10T10:00", ends="2026-01-10T11:00", tz="Australia/Lord_Howe")
        self.add(event_id="lord-howe-std", starts="2026-07-10T10:00", ends="2026-07-10T11:00", tz="Australia/Lord_Howe")
        stored = {row["id"]: (row["starts_at"], row["ends_at"]) for row in self.stored()}
        self.assertEqual(stored, {
            "la-winter": ("2026-11-09T18:00:00-08:00", "2026-11-09T20:00:00-08:00"),
            "la-summer": ("2026-07-04T09:05:07-07:00", "2026-07-04T11:00:00-07:00"),
            "utc": ("2026-11-03T12:00:00+00:00", "2026-11-03T13:00:00+00:00"),
            "lord-howe": ("2026-01-10T10:00:00+11:00", "2026-01-10T11:00:00+11:00"),
            "lord-howe-std": ("2026-07-10T10:00:00+10:30", "2026-07-10T11:00:00+10:30")})
        self.assertEqual(local_moment("2026-11-09T18:00", LA, "--starts").astimezone(timezone.utc),
                         datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc))
        # Lord Howe moves 30 minutes: 2026-10-04 02:00 -> 02:30 (gap) and 2026-04-05 02:00 -> 01:30 (overlap).
        self.refused("--starts: nonexistent", self.add, event_id="gap", starts="2026-10-04T02:15", ends="2026-10-04T04:00",
                     tz="Australia/Lord_Howe")
        self.refused("--starts: ambiguous", self.add, event_id="overlap", starts="2026-04-05T01:45", ends="2026-04-05T04:00",
                     tz="Australia/Lord_Howe")
        self.add(event_id="after-gap", starts="2026-10-04T02:30", ends="2026-10-04T04:00", tz="Australia/Lord_Howe")
        self.assertEqual(self.stored()[-1]["starts_at"], "2026-10-04T02:30:00+11:00")

    def test_zone_and_local_grammar_refusals(self):
        for tz, pattern in (("Mars/Olympus_Mons", "--tz: unknown time zone"), ("../etc/passwd", "--tz: must be an IANA"),
                            ("/usr/share/zoneinfo/UTC", "--tz: must be an IANA"), ("", "--tz: must be an IANA")):
            with self.subTest(tz=tz):
                self.refused(pattern, self.add, tz=tz)
        with mock.patch("zoneinfo.ZoneInfo", side_effect=KeyError("no db")), \
                mock.patch("zoneinfo.available_timezones", return_value=set()):
            message = self.refused("--tz: no IANA time zone database is installed", self.add)
        self.assertIn("nothing is fetched", message)
        for value in ("2026-11-09T18:00-08:00", "2026-11-09T18:00Z", "2026-11-09T18:00:00.5", "2026-11-09 18:00",
                      "2026-11-09", "2026-11-09T18", "\uff12026-11-09T18:00", "2026-11-09T18:00:00 "):
            with self.subTest(value=value):
                message = self.refused("--starts: must be a local time", self.add, starts=value)
                self.assertNotIn(value, message)
        for value in ("2026-02-29T10:00", "2026-13-01T10:00", "2026-11-09T24:00", "2026-11-09T10:60", "0000-01-01T00:00"):
            with self.subTest(value=value):
                self.refused("--starts: not a real calendar date", self.add, starts=value)
        self.add(event_id="leap", starts="2028-02-29T10:00", ends="2028-02-29T11:00")
        self.refused("--ends: outside the range", self.add, event_id="late", starts="9999-12-31T10:00",
                     ends="9999-12-31T23:30")
        self.refused("--starts: outside the range", self.add, event_id="early", starts="0001-01-01T00:00",
                     ends="0001-01-01T12:00", tz="Asia/Tokyo")
        # Before standard time Los Angeles kept local mean time, -07:52:58: an offset with seconds is refused by the loader.
        self.refused(r"events\.json\[1\]\.starts_at: must be", self.add, event_id="lmt", starts="1880-06-01T10:00",
                     ends="1880-06-01T11:00")

    def test_utc_end_order_across_transition(self):
        self.path.write_text('[{"id": "old", "title": "Old", "starts_at": "2026-10-10T18:00:00Z", '
                             '"ends_at": "2026-10-11T00:00:00+05:30", "online": true, "description_md": "Kept."}]\n',
                             encoding="utf-8")
        # 00:30 PDT (07:30Z) to 03:00 PST (11:00Z) is 4.5 h of real time although the offsets differ.
        self.add(event_id="overnight", starts="2026-11-01T00:30", ends="2026-11-01T03:00")
        self.add(event_id="equal", starts="2026-11-09T18:00", ends="2026-11-09T18:00")
        rows = self.stored()
        self.assertEqual(rows[0]["starts_at"], "2026-10-10T18:00:00Z")  # old row untouched, offsets kept
        self.assertEqual(rows[0]["ends_at"], "2026-10-11T00:00:00+05:30")
        self.assertEqual((rows[1]["starts_at"], rows[1]["ends_at"]), ("2026-11-01T00:30:00-07:00", "2026-11-01T03:00:00-08:00"))
        self.assertEqual(rows[2]["starts_at"], rows[2]["ends_at"])
        self.refused("--ends: is before --starts", self.add, event_id="backwards", starts="2026-11-09T18:00",
                     ends="2026-11-09T17:59:59")
        # 01:45 PST (09:45Z) to 03:10 PDT (10:10Z): 25 minutes of real time across the spring change.
        self.add(event_id="spring", starts="2026-03-08T01:45", ends="2026-03-08T03:10")
        self.assertEqual(self.stored()[-1]["ends_at"], "2026-03-08T03:10:00-07:00")
        # 03:00 PST (11:00Z) to 02:00 PST (10:00Z) on the fall-back day is refused.
        self.refused("--ends: is before --starts", self.add, event_id="fall-back", starts="2026-11-01T03:00",
                     ends="2026-11-01T02:00")


class StoreTests(EventCommandCase):
    def test_loader_parity_and_whole_file_refusal(self):
        self.add()
        self.refused("--id: an event with this id already exists", self.add, title="Something else")
        bad = (("bad-url", {"url": "http://example.com/x"}, r"\[1\]\.url: must be an https"),
               ("Bad_Id", {}, r"\[1\]\.id: must be a lowercase id"),
               ("title", {"title": "<b>x</b>"}, r"\[1\]\.title: must be trimmed"),
               ("venue", {"venue": " padded"}, r"\[1\]\.venue: must be trimmed"),
               ("note-email", {"accessibility_note": "Ask jane.doe@example.invalid"}, r"\[1\]\.accessibility_note: privacy lint: email"),
               ("venue-phone", {"venue": "Call 805-555-0142"}, r"\[1\]\.venue: privacy lint: phone"))
        for event_id, extra, pattern in bad:
            with self.subTest(event_id=event_id):
                message = self.refused(pattern, self.add, event_id=event_id, **extra)
                self.assertNotIn("jane.doe", message)
                self.assertNotIn("805-555", message)
        self.description.write_text("Write to jane.doe@example.invalid", encoding="utf-8")
        self.refused(r"\[1\]\.description_md: privacy lint: email", self.add, event_id="desc-email")
        rows = [dict(self.stored()[0], id=f"e-{n}") for n in range(50)]
        self.path.write_text(json.dumps(rows), encoding="utf-8")
        self.refused(r"events\.json: must hold at most 50 events", self.add, event_id="fifty-one")
        self.path.write_text(json.dumps(rows[:49]), encoding="utf-8")
        self.description.write_text("Synthetic gathering.", encoding="utf-8")
        self.add(event_id="fiftieth")
        self.assertEqual(len(self.stored()), 50)
        for raw, pattern in ((b"\xff[]", "not valid UTF-8 JSON"), (b"[", "not valid UTF-8 JSON"), (b"null", "must be a JSON array"),
                             (json.dumps([dict(rows[0], extra=1)]).encode(), r"\[0\]: unknown field extra"),
                             (json.dumps([rows[0], rows[0]]).encode(), r"\[1\]\.id: duplicates"),
                             (json.dumps([dict(rows[0], venue="x", online=True)]).encode(), r"\[0\]\.venue: must be left out")):
            with self.subTest(raw=raw[:20]):
                self.path.write_bytes(raw)
                self.refused(pattern, self.add, event_id="new-one")
                self.refused(pattern, remove_event, self.root, event_id=rows[0]["id"])  # no silent repair
                self.refused(pattern, list_events, self.root, date(2026, 10, 1))

    def test_description_is_exact_and_location_is_exclusive(self):
        self.add(event_id="venue", url="https://example.com/teach-in", accessibility_note="Step-free entrance.")
        self.add(event_id="online", online=True)
        rows = self.stored()
        text = self.description.read_bytes().decode("utf-8")
        self.assertEqual(rows[0], {"id": "venue", "title": "Records teach-in", "starts_at": "2026-11-09T18:00:00-08:00",
                                   "ends_at": "2026-11-09T20:00:00-08:00", "venue": "Cedar Library, room B",
                                   "description_md": text, "url": "https://example.com/teach-in",
                                   "accessibility_note": "Step-free entrance."})
        self.assertIn("\n\n", rows[1]["description_md"])
        self.assertTrue(rows[1]["description_md"].endswith("notes.\n"), "the description was trimmed")
        self.assertEqual(rows[1]["online"], True)
        self.assertNotIn("venue", rows[1])
        self.assertEqual(list(rows[1]), ["id", "title", "starts_at", "ends_at", "online", "description_md"])
        self.refused("exactly one of --venue or --online", self.add, event_id="both", venue="Hall", online=True)
        self.refused("exactly one of --venue or --online", add_event, self.root, event_id="neither", title="T",
                     starts="2026-11-09T18:00", ends="2026-11-09T19:00", tz=LA, description_file=self.description)
        for name, setup, pattern in (
                ("blank", lambda p: p.write_text("  \n", encoding="utf-8"), r"description_md: must be non-blank"),
                ("long", lambda p: p.write_text("x" * 1201, encoding="utf-8"), r"description_md: must be non-blank text of at most 1200"),
                ("huge", lambda p: p.write_text("x" * 5000, encoding="utf-8"), "--description-file: longer than 1200"),
                ("crlf", lambda p: p.write_bytes(b"Line one\r\nLine two"), r"description_md: must be non-blank"),
                ("utf8", lambda p: p.write_bytes(b"caf\xe9"), "--description-file: not valid UTF-8"),
                ("image", lambda p: p.write_text("![x](https://example.com/a.png)", encoding="utf-8"), "description_md: must be"),
                ("link", lambda p: p.symlink_to(self.description), "--description-file: could not be read"),
                ("fifo", lambda p: os.mkfifo(p), "--description-file: must be a regular file"),
                ("folder", lambda p: p.mkdir(), "--description-file: must be a regular file"),
                ("missing", lambda p: None, "--description-file: could not be read")):
            with self.subTest(name=name):
                path = self.tmp / f"desc-{name}"
                setup(path)
                message = self.refused(pattern, self.add, event_id=f"d-{name}", description_file=path)
                self.assertNotIn("caf", message)

    def test_partition_matches_site_at_utc_midnight(self):
        raw = ('[\n {"id": "b-exact", "title": "Exact", "starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-10-01T01:00:00Z",'
               ' "online": true, "description_md": "x"},\n'
               ' {"id": "ongoing", "title": "Ongoing", "starts_at": "2026-09-30T16:00:00-07:00", "ends_at": "2026-10-02T00:00:00-07:00",'
               ' "online": true, "description_md": "x"},\n'
               ' {"id": "a-exact", "title": "Tie", "starts_at": "2026-09-30T17:00:00-07:00", "ends_at": "2026-09-30T18:00:00-07:00",'
               ' "online": true, "description_md": "x"},\n'
               ' {"id": "late", "title": "Late", "starts_at": "2026-10-01T09:00:00+14:00", "ends_at": "2026-10-01T10:00:00+14:00",'
               ' "online": true, "description_md": "x"},\n'
               ' {"id": "old", "title": "Old", "starts_at": "2026-09-01T10:00:00Z", "ends_at": "2026-09-01T11:00:00Z",'
               ' "online": true, "description_md": "x"}\n]')
        self.path.write_text(raw, encoding="utf-8")
        before = snapshot(self.root)
        listing = list_events(self.root, date(2026, 10, 1))
        self.assertEqual(snapshot(self.root), before, "list changed a file")
        self.assertEqual(listing["as_of"], "2026-10-01")
        # 00:00Z ties (a-exact is 17:00-07:00) sort by id; ongoing (23:00Z the day before) and late (19:00Z) are past.
        self.assertEqual([row["id"] for row in listing["upcoming"]], ["a-exact", "b-exact"])
        self.assertEqual([row["id"] for row in listing["past"]], ["ongoing", "late", "old"])
        site_upcoming, site_past = partition(read_events(self.root), build_cutoff(date(2026, 10, 1)))
        self.assertEqual((listing["upcoming"], listing["past"]), (site_upcoming, site_past))
        self.assertEqual(listing["upcoming"][0]["starts_at"], "2026-09-30T17:00:00-07:00")  # shown as stored

    def test_remove_one_id_last_empty_and_unknown_id(self):
        self.refused("--id: no event with this id", remove_event, self.root, event_id="ghost")
        self.assertFalse(self.path.exists())
        self.assertEqual(list_events(self.root, date(2026, 10, 1)), {"as_of": "2026-10-01", "upcoming": [], "past": []})
        self.assertFalse(self.path.exists(), "list created the file")
        self.add(event_id="first")
        self.add(event_id="second", online=True)
        self.refused("--id: no event with this id", remove_event, self.root, event_id="secon")
        self.assertEqual(remove_event(self.root, event_id="first"), {"action": "removed", "id": "first"})
        self.assertEqual([row["id"] for row in self.stored()], ["second"])
        remove_event(self.root, event_id="second")
        self.assertEqual(self.path.read_bytes(), b"[]\n")
        self.refused("--id: no event with this id", remove_event, self.root, event_id="second")
        self.assertEqual(list_events(self.root, date(2026, 10, 1))["upcoming"], [])
        self.assertEqual(self.path.read_bytes(), b"[]\n")


class WriteSafetyTests(EventCommandCase):
    def test_atomic_write_failure_target_change_and_absence(self):
        self.refused("write failed; the previous file was kept", self._with, "fsync", OSError("disk"))
        self.add(event_id="kept")
        os.chmod(self.path, 0o600)
        for name in ("fsync", "replace"):
            with self.subTest(fail=name):
                self.refused("write failed; the previous file was kept", self._with, name, OSError("disk"))
        self.add(event_id="mode")
        self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), 0o600, "existing mode was not kept")

        def mutate(fd, real=os.fsync):
            real(fd)
            self.path.write_text(self.path.read_text(encoding="utf-8").replace("Records", "Changed"), encoding="utf-8")
        before = self.path.read_bytes()
        with mock.patch("campaign_tool.events.os.fsync", side_effect=mutate), \
                self.assertRaisesRegex(EventError, "changed while it was being edited"):
            self.add(event_id="race")
        self.assertNotIn(b"race", self.path.read_bytes())
        self.assertNotEqual(self.path.read_bytes(), before)  # the competing write survives; ours is refused
        self.path.unlink()

        def arrive(fd, real=os.fsync):
            real(fd)
            self.path.write_text("[]", encoding="utf-8")
        with mock.patch("campaign_tool.events.os.fsync", side_effect=arrive), \
                self.assertRaisesRegex(EventError, "changed while it was being edited"):
            self.add(event_id="arrival")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[]")
        self.assertEqual(sorted(p.name for p in self.content.iterdir() if p.name.startswith(".events-")), [], "temp left")
        self.path.unlink()
        target = self.tmp / "elsewhere.json"
        target.write_text("[]", encoding="utf-8")
        self.path.symlink_to(target)
        self.refused("must be a regular file with a single link", self.add)
        self.refused("must be a regular file with a single link", remove_event, self.root, event_id="x")
        self.path.unlink()
        self.path.mkdir()
        self.refused("must be a regular file with a single link", self.add)
        self.path.rmdir()
        os.mkfifo(self.path)
        self.refused("must be a regular file with a single link", self.add)
        self.refused("must be a regular file", list_events, self.root, date(2026, 10, 1))
        self.path.unlink()
        shutil.move(self.content, self.tmp / "real-content")
        self.content.symlink_to(self.tmp / "real-content")
        self.refused("content/ does not exist", self.add)
        self.content.unlink()
        self.refused("content/ does not exist", self.add)
        self.refused("content/ does not exist", list_events, self.root)

    def _with(self, name, error):
        with mock.patch(f"campaign_tool.events.os.{name}", side_effect=error):
            self.add(event_id="failing")

    def test_no_events_build_bytes_are_unchanged(self):
        public = self.root / "public"

        def tree():
            build_site(self.root, today=date(2026, 10, 1))
            return {p.relative_to(public).as_posix(): p.read_bytes() for p in sorted(public.rglob("*")) if p.is_file()}
        absent = tree()
        list_events(self.root, date(2026, 10, 1))
        with self.assertRaises(EventError):
            self.add(starts="2026-03-08T02:30")
        self.assertFalse(self.path.exists())
        self.assertEqual(tree(), absent)
        self.add(event_id="temporary")
        self.assertIn("events.html", tree())
        remove_event(self.root, event_id="temporary")
        self.assertEqual(self.path.read_bytes(), b"[]\n")
        self.assertEqual(tree(), absent, "an emptied events file changed the built site")


if __name__ == "__main__":
    unittest.main()
