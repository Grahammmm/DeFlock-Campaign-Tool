"""DF0022-10: organizer-verified regular meeting schedules, agenda-watch prompts and the meetings.html rule line.

Synthetic bodies and URLs only; no network. Run from the repository root:
python3 -B -m unittest tests.test_body_schedules -v
"""
import calendar
import contextlib
import io
import json
import os
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool import body_schedule_cmd, body_schedules, cli, today
from campaign_tool.body_schedules import load_body_schedules, next_expected_dates, read_body_schedules, stale
from campaign_tool.content import MAX_JSON_BYTES, ContentError, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 4)
BODY = "Synthetic Council"
URL = "https://example.invalid/calendar"


def row(**changes):
    base = {"body": BODY, "weekday": "tuesday", "ordinals": [1, 3], "local_start_time": "18:30",
            "time_zone": "America/Los_Angeles", "source_url": URL, "checked_on": "2026-10-01"}
    base.update(changes)
    return base


def document(*rows):
    return {"schema_version": 1, "schedules": list(rows)}


def staged(starts_at, body=BODY, status="scheduled"):
    return {"body": body, "starts_at": starts_at, "status": status}


def snapshot(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


class Base(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-body-schedules-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.path = self.root / "content" / "body-schedules.json"

    def write(self, value):
        self.path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")

    def stage(self, name, starts_at, body=BODY, **more):
        folder = self.root / "content" / "meetings"
        (folder / f"{name}.json").write_text(json.dumps({"id": name, "body": body, "starts_at": starts_at, **more}),
                                             encoding="utf-8")


class FormatTests(Base):
    def refused(self, value, needle):
        with self.assertRaises(ContentError) as caught:
            load_body_schedules(value, today=TODAY)
        self.assertIn(needle, str(caught.exception))

    def test_valid_rows_are_sorted_by_literal_body_and_ordinals_canonical(self):
        rows = load_body_schedules(document(row(body="b council", ordinals=["last", 3, 1]), row(body="B Council"),
                                            row(body="Zed", checked_on="2026-11-04")), today=TODAY)
        self.assertEqual([r["body"] for r in rows], ["B Council", "Zed", "b council"])
        self.assertEqual(rows[2]["ordinals"], [1, 3, "last"])
        self.assertEqual(load_body_schedules(document(), today=TODAY), [])
        self.assertEqual(load_body_schedules(document(row(checked_on="2000-01-01")), today=TODAY)[0]["checked_on"],
                         "2000-01-01")  # an old date is valid (and stale)

    def test_format_refusals_name_field(self):
        label = "body-schedules.json.schedules[0]"
        self.refused(document(row(), row()), f"body-schedules.json: duplicate body {BODY}")
        for weekday in ("Tuesday", "funday", "", 2, None, ["tuesday"]):
            self.refused(document(row(weekday=weekday)), f"{label}.weekday: must be monday through sunday")
        for ordinals in ([], [5], [0], [-1], ["1"], [True], [1.0], [None], [1, 1], ["last", "last"], "1", 1, None,
                         [1, "Last"], [[1]], ["fifth"]):
            self.refused(document(row(ordinals=ordinals)), f"{label}.ordinals: use unique 1, 2, 3, 4 or last")
        for start in ("6:30", "18:30:00", "24:00", "18:60", "18:30Z", "18:30-07:00", "\uff11\uff18:30", 1830, None, "", "18:3"):
            self.refused(document(row(local_start_time=start)), f"{label}.local_start_time")
        self.refused(document(row(time_zone="Nowhere/Land")), f"{label}.time_zone: unknown time zone Nowhere/Land")
        for zone in ("", "../etc/passwd", "/etc/passwd", 5, None, "America/Los_Angeles ", "x" * 80):
            self.refused(document(row(time_zone=zone)), f"{label}.time_zone")
        for url in ("http://example.invalid/x", "https://user@example.invalid/", "javascript:alert(1)", None, 5,
                    "https://example.invalid/" + "a" * 500, "https://"):
            self.refused(document(row(source_url=url)), f"{label}.source_url")
        for checked in ("2026-13-01", "2026-02-30", "2026-1-1", "20261101", "2026-11-01T00:00:00", "", None, 20261101,
                        "２０２６-11-01", "0000-01-01", "2026-11-05"):
            self.refused(document(row(checked_on=checked)), f"{label}.checked_on")
        for body in ("", " Council", "Council ", "Council\nTwo", "Council\tTwo", "<b>Council</b>", 5, None, "x" * 201):
            self.refused(document(row(body=body)), f"{label}.body")
        self.refused(document(row(), row(body="Other", nickname="x")), "body-schedules.json.schedules[1]: unknown field(s) ['nickname']")
        bare = row()
        del bare["time_zone"]
        self.refused(document(bare), "missing field(s) ['time_zone']")
        self.refused(document(row(), "text"), "schedules[1]: expected an object")
        self.refused(document(*[row(body=f"Body {n}") for n in range(101)]), "at most 100")
        self.assertEqual(len(load_body_schedules(document(*[row(body=f"Body {n}") for n in range(100)]), today=TODAY)), 100)
        for version in (2, 0, True, "1", None, 1.0):
            self.refused({"schema_version": version, "schedules": []}, "body-schedules.json.schema_version: must be 1")
        self.refused({"schema_version": 1}, "missing field(s) ['schedules']")
        self.refused({"schema_version": 1, "schedules": [], "extra": 1}, "unknown field(s) ['extra']")
        self.refused({"schema_version": 1, "schedules": {}}, "schedules: must be a list")
        for root in (None, [], "text", 5, [row()]):
            self.refused(root, "body-schedules.json: expected an object")

    def test_future_checked_on_is_judged_against_the_injected_day(self):
        load_body_schedules(document(row(checked_on="2026-11-05")), today=date(2026, 11, 5))
        self.refused(document(row(checked_on="2026-11-05")), "checked_on: 2026-11-05 is in the future")

    def test_reader_absent_file_unreadable_and_oversize(self):
        before = snapshot(self.root)
        self.assertEqual(read_body_schedules(self.root, today=TODAY), [])
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(self.path.exists())  # reading never creates the file
        for text, needle in (("{not json", "not valid UTF-8 JSON"), ("null", "expected an object"), ("[]", "expected an object"),
                             ("5", "expected an object"), ('"x"', "expected an object")):
            self.write(text)
            with self.assertRaisesRegex(ContentError, needle):
                read_body_schedules(self.root, today=TODAY)
        self.write(json.dumps({"schema_version": 1, "schedules": [], "_": "x" * (2 * 1024 * 1024)}))
        with self.assertRaisesRegex(ContentError, "body-schedules.json: file too large"):
            read_body_schedules(self.root, today=TODAY)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaisesRegex(ContentError, "must be a regular file"):
            read_body_schedules(self.root, today=TODAY)

    def test_symlink_even_dangling_is_refused_not_absent(self):
        real = self.tmp / "real.json"
        real.write_text(json.dumps(document(row())), encoding="utf-8")
        os.symlink(real, self.path)
        with self.assertRaisesRegex(ContentError, "body-schedules.json: symlinks are not read"):
            read_body_schedules(self.root, today=TODAY)
        self.path.unlink()
        os.symlink(self.tmp / "missing.json", self.path)  # dangling: lexists is true, so never read as absence
        with self.assertRaisesRegex(ContentError, "body-schedules.json: symlinks are not read"):
            read_body_schedules(self.root, today=TODAY)
        with self.assertRaisesRegex(ContentError, "symlinks are not read"):
            load_content(self.root, today=TODAY)
        self.assertTrue(any("body-schedules.json: symlinks are not read" in line for line in content_problems(self.root)))


class CalendarTests(unittest.TestCase):
    def dates(self, as_of, ordinals, weekday="tuesday", meetings=(), limit=2, zone="America/Los_Angeles"):
        schedule = load_body_schedules(document(row(weekday=weekday, ordinals=ordinals, time_zone=zone)), today=TODAY)[0]
        return next_expected_dates(schedule, list(meetings), as_of, limit=limit)

    def test_calendar_first_fourth_last_leap_and_year_rollover(self):
        # February: leap 2028 (29 days; Feb 29 is a Tuesday) and 2026 (28 days; fourth Tuesday is also the last).
        self.assertEqual(self.dates(date(2028, 2, 1), [4, "last"]), [date(2028, 2, 22), date(2028, 2, 29)])
        self.assertEqual(self.dates(date(2026, 2, 1), [4, "last"], limit=3), [date(2026, 2, 24), date(2026, 3, 24), date(2026, 3, 31)])
        # four-versus-five-weekday months: March 2026 has five Tuesdays; last is the 31st, never a fifth by arithmetic.
        self.assertEqual(self.dates(date(2026, 3, 1), ["last"], limit=3), [date(2026, 3, 31), date(2026, 4, 28), date(2026, 5, 26)])
        self.assertEqual(self.dates(date(2026, 3, 1), [1, 2, 3, 4], limit=6),
                         [date(2026, 3, 3), date(2026, 3, 10), date(2026, 3, 17), date(2026, 3, 24), date(2026, 4, 7), date(2026, 4, 14)])
        # December to January; today (as_of) is included.
        self.assertEqual(self.dates(date(2026, 12, 20), [1]), [date(2027, 1, 5), date(2027, 2, 2)])
        self.assertEqual(self.dates(date(2026, 12, 29), ["last"]), [date(2026, 12, 29), date(2027, 1, 26)])
        self.assertEqual(self.dates(date(2026, 3, 3), [1, 3]), [date(2026, 3, 3), date(2026, 3, 17)])
        self.assertEqual(self.dates(date(2026, 3, 4), [1, 3]), [date(2026, 3, 17), date(2026, 4, 7)])
        # ordinals given in any order are sorted and de-duplicated by date
        self.assertEqual(self.dates(date(2026, 3, 1), ["last", 4, 1], limit=4),
                         [date(2026, 3, 3), date(2026, 3, 24), date(2026, 3, 31), date(2026, 4, 7)])

    def test_every_weekday_and_ordinal_matches_the_calendar(self):
        for index, weekday in enumerate(body_schedules.WEEKDAYS):
            for ordinal in (1, 2, 3, 4, "last"):
                for year, month in ((2026, 2), (2026, 3), (2028, 2), (2027, 12)):
                    days = [d for d in range(1, calendar.monthrange(year, month)[1] + 1)
                            if date(year, month, d).weekday() == index]
                    want = date(year, month, days[-1] if ordinal == "last" else days[ordinal - 1])
                    self.assertEqual(self.dates(date(year, month, 1), [ordinal], weekday=weekday)[0], want,
                                     (weekday, ordinal, year, month))

    def test_date_max_terminates_without_a_day_scan(self):
        last = date.max
        other = body_schedules.WEEKDAYS[(last.weekday() + 1) % 7]
        self.assertEqual(self.dates(last, [1, 2, 3, 4, "last"], weekday=other), [])
        self.assertEqual(self.dates(last, ["last"], weekday=body_schedules.WEEKDAYS[last.weekday()]), [last])
        found = self.dates(date(9999, 12, 1), [1, 2, 3, 4, "last"], weekday=body_schedules.WEEKDAYS[last.weekday()], limit=50)
        self.assertTrue(found and all(date(9999, 12, 1) <= d <= last for d in found))
        self.assertEqual(found, sorted(set(found)))

    def test_suppression_uses_exact_body_and_local_date(self):
        # 2026-03-03 is the first Tuesday, 2026-03-17 the third.
        free = [date(2026, 3, 3), date(2026, 3, 17)]
        self.assertEqual(self.dates(date(2026, 3, 1), [1, 3]), free)
        # a staged meeting of this body on a candidate date answers it, whatever its time or status; the walk continues
        for status in ("scheduled", "cancelled"):
            self.assertEqual(self.dates(date(2026, 3, 1), [1, 3], meetings=[staged("2026-03-03T18:00:00-08:00", status=status)]),
                             [date(2026, 3, 17), date(2026, 4, 7)])
        # several staged items on one date suppress it once; two unstaged dates are still returned
        twice = [staged("2026-03-03T09:00:00-08:00"), staged("2026-03-03T18:30:00-08:00"), staged("2026-03-17T18:30:00-07:00")]
        self.assertEqual(self.dates(date(2026, 3, 1), [1, 3], meetings=twice), [date(2026, 4, 7), date(2026, 4, 21)])
        # a different body or a different spelling never suppresses
        other = [staged("2026-03-03T18:30:00-08:00", body="Other Council"), staged("2026-03-03T18:30:00-08:00", body="synthetic council"),
                 staged("2026-03-03T18:30:00-08:00", body=BODY + " ")]
        self.assertEqual(self.dates(date(2026, 3, 1), [1, 3], meetings=other), free)
        # the schedule zone decides the local day: 02:00Z on the 4th is 18:00 on the 3rd in Los Angeles
        for starts in ("2026-03-04T02:00:00Z", "2026-03-04T02:00:00+00:00", "2026-03-03T21:00:00-05:00"):
            self.assertEqual(self.dates(date(2026, 3, 1), [1, 3], meetings=[staged(starts)]), [date(2026, 3, 17), date(2026, 4, 7)], starts)
        # ... and a meeting at 00:30 local on the 4th is not on the 3rd
        self.assertEqual(self.dates(date(2026, 3, 1), [1, 3], meetings=[staged("2026-03-04T00:30:00-08:00")]), free)
        self.assertEqual(self.dates(date(2026, 3, 1), [1, 3], meetings=[staged("2026-03-03T23:30:00-08:00")]),
                         [date(2026, 3, 17), date(2026, 4, 7)])
        # the same instant in another zone is another local date
        self.assertEqual(self.dates(date(2026, 3, 1), [1, 3], meetings=[staged("2026-03-04T02:00:00Z")], zone="Pacific/Auckland"), [
            date(2026, 3, 3), date(2026, 3, 17)])

    def test_dst_transitions_use_the_instant_and_iana_zone(self):
        # 2026-03-08 (second Sunday) is the US spring-forward day; 2026-11-01 (first Sunday) the fall-back day.
        sundays = dict(weekday="sunday")
        self.assertEqual(self.dates(date(2026, 3, 1), [2], meetings=[staged("2026-03-08T10:00:00-07:00")], **sundays),
                         [date(2026, 4, 12), date(2026, 5, 10)])
        # 07:30Z on Nov 2 is 23:30 PST on Nov 1 (a UTC-date comparison would miss it)
        self.assertEqual(self.dates(date(2026, 11, 1), [1], meetings=[staged("2026-11-02T07:30:00Z")], **sundays),
                         [date(2026, 12, 6), date(2027, 1, 3)])
        for starts in ("2026-11-01T01:30:00-07:00", "2026-11-01T01:30:00-08:00"):  # both readings of the repeated hour
            self.assertEqual(self.dates(date(2026, 11, 1), [1], meetings=[staged(starts)], **sundays),
                             [date(2026, 12, 6), date(2027, 1, 3)], starts)
        # a nonexistent local time (02:30 on Mar 8) is never materialized: only dates come back
        for day in self.dates(date(2026, 3, 1), [2], **sundays):
            self.assertIs(type(day), date)

    def test_stale_boundary(self):
        def fresh(checked, as_of):
            return not stale(load_body_schedules(document(row(checked_on=checked)), today=as_of)[0], as_of)
        self.assertTrue(fresh("2025-11-04", date(2026, 11, 4)))   # exactly 365 days
        self.assertFalse(fresh("2025-11-03", date(2026, 11, 4)))  # 366
        self.assertTrue(fresh("2026-11-04", date(2026, 11, 4)))
        self.assertTrue(fresh("2027-03-02", date(2028, 3, 1)))    # leap-year span: 365 days across Feb 29
        self.assertFalse(fresh("2027-03-01", date(2028, 3, 1)))   # 366 days
        self.assertTrue(fresh("2024-02-29", date(2025, 2, 28)))
        self.assertFalse(fresh("2024-02-29", date(2025, 3, 1)))


class TodayTests(Base):
    BODIES = ("expected_regular_meeting", "stale_body_schedule")

    def schedule_items(self, as_of=date(2026, 3, 1)):
        return [i for i in today.todo(self.root, as_of) if i["kind"] in self.BODIES]

    def test_expected_dates_listed_without_scheduled_or_deadline_wording(self):
        self.write(document(row(checked_on="2026-02-01")))
        items = self.schedule_items()
        self.assertEqual([(i["kind"], i["what"], i["due"], i["command"]) for i in items], [
            ("expected_regular_meeting", f"Expected {BODY} meeting 2026-03-03: watch for the agenda", "2026-03-03",
             f"Check {URL} for the agenda; stage only a verified meeting"),
            ("expected_regular_meeting", f"Expected {BODY} meeting 2026-03-17: watch for the agenda", "2026-03-17",
             f"Check {URL} for the agenda; stage only a verified meeting")])
        for item in items:
            self.assertEqual(set(item), {"rank", "kind", "what", "due", "command"})
            text = (item["what"] + item["command"]).lower()
            for word in ("deadline", "overdue", "late", "posted", "post ", "--online", "meetings-stage"):
                self.assertNotIn(word, text)
            self.assertNotIn("scheduled", item["what"].lower())

    def test_staged_meeting_of_the_body_hides_its_date_and_the_walk_continues(self):
        self.stage("m1", "2026-03-03T18:30:00-08:00", body=BODY, status="cancelled")
        self.stage("m2", "2026-03-03T19:00:00-08:00", body=BODY)
        self.write(document(row(checked_on="2026-02-01")))
        self.assertEqual([i["due"] for i in self.schedule_items()], ["2026-03-17", "2026-04-07"])
        self.stage("m3", "2026-03-17T18:30:00-07:00", body="Other Council")  # a different body hides nothing
        self.assertEqual([i["due"] for i in self.schedule_items()], ["2026-03-17", "2026-04-07"])

    def test_inclusive_as_of_and_example_body_with_no_feed(self):
        self.write(document(row(body="Cedar City Council (fictional)", weekday="wednesday", ordinals=[2], checked_on="2026-10-01")))
        # the example staged meeting is 2026-10-14 (second Wednesday): hidden even on that very day
        self.assertEqual([i["due"] for i in self.schedule_items(date(2026, 10, 14))], ["2026-11-11", "2026-12-09"])
        self.assertEqual([i["due"] for i in self.schedule_items(date(2026, 10, 15))], ["2026-11-11", "2026-12-09"])
        self.assertEqual([i["due"] for i in self.schedule_items(date(2026, 11, 11))], ["2026-11-11", "2026-12-09"])

    def test_stale_boundary_and_today_independence(self):
        as_of = date(2026, 11, 4)
        for checked, is_stale in (("2025-11-04", False), ("2025-11-03", True)):
            self.write(document(row(checked_on=checked)))
            kinds = [i["kind"] for i in self.schedule_items(as_of)]
            self.assertEqual("stale_body_schedule" in kinds, is_stale, checked)
            self.assertEqual(kinds.count("expected_regular_meeting"), 2)  # prompts continue even when stale
        items = self.schedule_items(as_of)
        self.write(document(row(checked_on="2025-01-01")))
        items = self.schedule_items(as_of)
        self.assertEqual(items[0]["kind"], "stale_body_schedule")  # immediately before expected_regular_meeting
        self.assertEqual(items[0]["what"], f"Regular meeting schedule for {BODY} is stale (checked 2025-01-01); re-check {URL}")
        self.assertEqual(items[0]["due"], "2025-01-01")
        self.assertEqual(items[0]["command"], f"body-schedule recheck --directory {shlex.quote(str(self.root))} "
                         f"--body 'Synthetic Council' --checked-on {TODAY.isoformat()}")
        for item in items:
            self.assertNotIn("overdue", item["what"].lower())
            self.assertNotIn("legally", item["what"].lower())

    def test_kinds_rank_between_review_waiting_and_unreadable(self):
        kinds = list(today.KINDS)
        self.assertEqual(kinds[-4:], ["stale_body_schedule", "expected_regular_meeting", "backup_due", "unreadable"])
        self.assertEqual(kinds[-5], "review_waiting")

    def test_broken_meeting_beside_a_valid_schedule_is_one_unreadable_row_and_suppresses_nothing(self):
        (self.root / "content" / "meetings" / "broken.json").write_text("{not json", encoding="utf-8")
        self.write(document(row(checked_on="2026-02-01")))
        items = today.todo(self.root, date(2026, 3, 1))
        broken = [i for i in items if i["kind"] == "unreadable" and "broken.json" in i["what"]]
        self.assertEqual(len(broken), 1)
        self.assertEqual([i["due"] for i in items if i["kind"] == "expected_regular_meeting"], ["2026-03-03", "2026-03-17"])
        self.assertEqual(items[-1]["kind"], "unreadable")

    def test_bad_schedule_is_one_unreadable_item_and_hides_nothing_else(self):
        self.stage("m-soon", "2026-03-20T18:00:00-07:00", body="Soon Council",
                   participation={"in_person": True, "remote": None, "source_url": "https://agenda.synthetic.example/rules",
                                  "verified_by": "Pat Organizer", "verified_at": "2026-03-10",
                                  "written_comment": {"how": "email the synthetic clerk", "deadline": "2026-03-18T12:00:00-07:00"}})
        self.write(document(row(weekday="funday")))
        items = today.todo(self.root, date(2026, 3, 15))
        bad = [i for i in items if "body-schedules.json" in i["what"]]
        self.assertEqual(len(bad), 1)
        self.assertEqual((bad[0]["kind"], bad[0]["command"]), ("unreadable", "open content/body-schedules.json and fix it"))
        self.assertIn("weekday: must be monday through sunday", bad[0]["what"])
        self.assertEqual([i["kind"] for i in items if i["kind"] not in ("unreadable",)], ["comment_deadline"])
        self.assertEqual({i["kind"] for i in items if i["kind"] in self.BODIES}, set())

    def test_no_file_means_no_lines_and_identical_text_and_json(self):
        plain = (today.run_cli(self.root, "2026-03-15"), today.run_cli(self.root, "2026-03-15", as_json=True))
        self.assertEqual(self.schedule_items(), [])
        self.write(document())  # an empty list adds nothing either
        self.assertEqual((today.run_cli(self.root, "2026-03-15"), today.run_cli(self.root, "2026-03-15", as_json=True)), plain)
        before = snapshot(self.root)
        self.write(document(row(checked_on="2026-02-01")))
        edited = snapshot(self.root)
        today.run_cli(self.root, "2026-03-15")
        self.assertEqual(snapshot(self.root), edited)  # reading writes nothing
        self.assertNotEqual(edited, before)


class SiteTests(Base):
    BUILD_DAY = date(2026, 10, 20)

    def build(self):
        build_site(self.root, today=self.BUILD_DAY)
        public = self.root / "public"
        return {p.relative_to(public).as_posix(): p.read_bytes() for p in sorted(public.rglob("*")) if p.is_file()}

    def meetings(self, **labels):
        if labels:
            (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")
        return self.build()["meetings.html"].decode("utf-8")

    def test_rule_render_labels_and_no_schedule_bytes(self):
        baseline = self.build()
        shutil.rmtree(self.root / "public")
        self.write(document())  # empty schedules: no section, no whitespace, every page byte-identical
        self.assertEqual(self.build(), baseline)
        for name, data in baseline.items():
            self.assertNotIn(b"Usually meets", data, name)
            self.assertNotIn(b"regular-meetings", data, name)
        shutil.rmtree(self.root / "public")
        self.write(document(row(body='Zed "Board" & Co', ordinals=["last", 3, 1], weekday="friday", checked_on="2025-10-01"),
                            row(body="Alpha Board", ordinals=[2], weekday="monday", local_start_time="09:05", time_zone="UTC",
                                source_url="https://example.invalid/a?x=1&y=2", checked_on="2026-10-20")))
        tree = self.build()
        self.assertEqual(sorted(name for name in tree if tree[name] != baseline.get(name)), ["meetings.html"])
        self.assertEqual(sorted(baseline), sorted(tree))  # no new public file, data export, feed or sitemap entry
        page = tree["meetings.html"].decode("utf-8")
        self.assertLess(page.index("Alpha Board"), page.index("Zed &quot;Board&quot; &amp; Co"))
        self.assertIn("<h3>Alpha Board</h3><p>Usually meets: second Monday at 09:05 (UTC) "
                      '(<a href="https://example.invalid/a?x=1&amp;y=2" rel="noopener noreferrer">'
                      "https://example.invalid/a?x=1&amp;y=2</a>, checked 2026-10-20)</p>", page)
        self.assertIn("Usually meets: first, third, and last Friday at 18:30 (America/Los_Angeles) (", page)
        self.assertEqual(page.count("Stale schedule: re-check the source"), 1)  # 2025-10-01 is stale, 2026-10-20 is not
        self.assertGreater(page.index("Stale schedule"), page.index("Zed"))
        self.assertLess(page.index('id="content"'), page.index('id="regular-meetings"'))  # after the introduction
        self.assertLess(page.index('id="regular-meetings"'), page.index('id="past-meetings"'))
        for name, data in tree.items():
            if name != "meetings.html":
                self.assertNotIn(b"Usually meets", data, name)
        for scheduled in ("2026-10-2", "2026-11"):
            self.assertNotIn("Expected", page)  # the page states the rule only, never a date
            self.assertNotIn("deadline", page.split('id="regular-meetings"')[1].split("</section>")[0].lower(), scheduled)

    def test_ordinal_joining_and_every_weekday_token(self):
        cases = {(1,): "first", (1, 3): "first and third", (4, "last"): "fourth and last", ("last",): "last",
                 (1, 2, 3, 4): "first, second, third, and fourth", (1, 2, "last"): "first, second, and last"}
        for ordinals, words in cases.items():
            self.write(document(row(ordinals=list(ordinals))))
            self.assertIn(f"Usually meets: {words} Tuesday at 18:30 (America/Los_Angeles) (", self.meetings(), ordinals)
        for weekday in body_schedules.WEEKDAYS:
            self.write(document(row(weekday=weekday)))
            self.assertIn(f"first and third {weekday.capitalize()} at 18:30", self.meetings(), weekday)

    def test_translated_labels_and_placeholders(self):
        self.write(document(row(checked_on="2024-01-01")))
        page = self.meetings(**{"schedule.usually": "Suele: {rule} / {source} / {date}", "schedule.rule": "{ordinals} {weekday} {time} {zone}",
                                "schedule.ordinal_1": "primer", "schedule.ordinal_3": "tercer", "schedule.weekday_tuesday": "martes",
                                "schedule.join_two": "{first} y {last}", "schedule.stale": "Calendario viejo"})
        self.assertIn("Suele: primer y tercer martes 18:30 America/Los_Angeles / <a href=", page)
        self.assertIn("</a> / 2024-01-01</p>", page)
        self.assertIn("<p>Calendario viejo</p>", page)
        self.assertNotIn("Usually meets", page)
        self.write(document(row(ordinals=[1, 2, 3])))
        shutil.rmtree(self.root / "public")
        page = self.meetings(**{"schedule.join_many": "{prefix} & {last}", "schedule.ordinal_2": "segundo"})
        self.assertIn("first, segundo &amp; third Tuesday", page)

    def test_every_schedule_label_is_replaceable_and_has_a_spanish_draft(self):
        keys = sorted(key for key in body_schedules_labels() if key.startswith("schedule."))
        self.assertEqual(len(keys), 17)
        self.write(document(row(checked_on="2024-01-01")))
        page = self.meetings(**{key: f"X{key}X" for key in keys})
        for key in ("schedule.usually", "schedule.stale"):
            self.assertIn(f"X{key}X", page)
        self.assertNotIn("Usually meets", page)
        draft = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))["labels"]
        self.assertEqual(sorted(key for key in draft if key.startswith("schedule.")), keys)

    def test_build_content_check_parity_and_read_only(self):
        good = document(row())
        cases = [(document(row(weekday="funday")), "schedules[0].weekday: must be monday through sunday"),
                 (document(row(ordinals=[5])), "schedules[0].ordinals: use unique 1, 2, 3, 4 or last"),
                 (document(row(), row()), f"body-schedules.json: duplicate body {BODY}"),
                 (document(row(time_zone="Mars/Base")), "time_zone: unknown time zone Mars/Base"),
                 (document(row(checked_on="2999-01-01")), "schedules[0].checked_on"),
                 (document(row(extra=1)), "unknown field(s) ['extra']"),
                 ("{not json", "body-schedules.json: not valid UTF-8 JSON"), ("null", "body-schedules.json: expected an object"),
                 ("[]", "body-schedules.json: expected an object")]
        self.write(good)
        self.assertEqual(content_problems(self.root), [])
        for value, needle in cases:
            self.write(value)
            problems = [line for line in content_problems(self.root) if "body-schedules.json" in line]
            self.assertEqual(len(problems), 1, (needle, problems))
            self.assertIn(needle, problems[0])
            with self.assertRaises(ContentError) as caught:
                load_content(self.root, today=self.BUILD_DAY)
            self.assertEqual(str(caught.exception), problems[0])  # one reader, one message, in both checks
            result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content-check", "--json", "--directory", str(self.root)],
                                    cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 3)
            records = [p for p in json.loads(result.stdout)["problems"] if "body-schedules.json" in (p["file"] or p["message"])]
            self.assertEqual(len(records), 1)
            self.assertIn(needle, (records[0]["file"] or "") + ": " + records[0]["message"])
        # an independent bad file is collected beside it, never hidden
        (self.root / "content" / "meetings" / "bad.json").write_text("{not json", encoding="utf-8")
        self.write(document(row(weekday="funday")))
        lines = content_problems(self.root)
        self.assertTrue(any("bad.json" in line for line in lines) and any("body-schedules.json" in line for line in lines))
        (self.root / "content" / "meetings" / "bad.json").unlink()
        # content-check and the model read nothing into the tree; a good file adds the model key only when nonempty
        self.write(good)
        before = snapshot(self.root)
        self.assertEqual(content_problems(self.root), [])
        self.assertEqual([r["body"] for r in load_content(self.root, today=self.BUILD_DAY)["body_schedules"]], [BODY])
        self.assertEqual(snapshot(self.root), before)
        self.write(document())
        self.assertNotIn("body_schedules", load_content(self.root, today=self.BUILD_DAY))
        self.path.unlink()
        self.assertNotIn("body_schedules", load_content(self.root, today=self.BUILD_DAY))

    def test_bad_schedule_stops_the_build_before_the_public_tree_is_replaced(self):
        self.build()
        public = self.root / "public"
        before = snapshot(public)
        time.sleep(0.01)
        self.write(document(row(weekday="funday")))
        with self.assertRaisesRegex(ContentError, "schedules\\[0\\].weekday: must be monday through sunday"):
            build_site(self.root, today=self.BUILD_DAY)
        self.assertEqual(snapshot(public), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith(".public")), [])

    def test_no_scheduled_meeting_rows_posting_deadline_or_holiday_inference(self):
        self.write(document(row()))
        tree = self.build()
        page = tree["meetings.html"].decode("utf-8")
        before = json.loads(tree["data/meetings.json"])
        self.assertEqual([m["body"] for m in before["meetings"]], ["Cedar City Council (fictional)"])
        for name in ("meetings.xml", "meetings.ics", "sitemap.xml", "data/meetings.json", "data/meetings.csv"):
            if name in tree:
                self.assertNotIn(BODY.encode(), tree[name], name)
        section = page.split('id="regular-meetings"')[1].split("</section>")[0].lower()
        for word in ("deadline", "holiday", "overdue", "late", "posted", "expected"):
            self.assertNotIn(word, section)


CAP = MAX_JSON_BYTES


def padded(rows=(), size=None):
    """Valid schedule JSON followed by legal whitespace to exactly ``size`` encoded bytes."""
    data = json.dumps(document(*rows), ensure_ascii=False).encode("utf-8")
    return data if size is None else data + b" " * (size - len(data))


class SpyBytes(bytes):
    """Payload bytes that record whether the reader decoded them."""
    decoded = []

    def decode(self, *args, **kwargs):
        SpyBytes.decoded.append(len(self))
        return super().decode(*args, **kwargs)


class SpyHandle:
    """Delegates to the real binary stream; records every read size and returned length."""

    def __init__(self, handle, spy):
        self.handle, self.spy = handle, spy

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *exc):
        return self.handle.__exit__(*exc)

    def fileno(self):
        return self.handle.fileno()

    def read(self, size=-1):
        self.spy.reads.append(size)
        if self.spy.read_error:
            raise OSError("synthetic read failure")
        data = self.handle.read(size)
        self.spy.lengths.append(len(data))
        return SpyBytes(data)


class Spy:
    """Thin wrappers over the real os.open/fdopen/fstat that observe only the target schedule file."""

    def __init__(self, test, after_fstat=None, open_error=None, fdopen_error=False, fstat_error=False,
                 read_error=False):
        self.test, self.after_fstat, self.open_error = test, after_fstat, open_error
        self.fdopen_error, self.fstat_error, self.read_error = fdopen_error, fstat_error, read_error
        self.opened, self.fds, self.handles, self.reads, self.lengths, self.stats = [], [], [], [], [], []
        self.unrestricted = []
        self.real_open, self.real_fdopen, self.real_fstat = os.open, os.fdopen, os.fstat
        self.real_read_bytes, self.real_read_text = Path.read_bytes, Path.read_text
        SpyBytes.decoded = []

    def open(self, path, *args, **kwargs):
        if os.fspath(path) != os.fspath(self.test.path):
            return self.real_open(path, *args, **kwargs)
        self.opened.append(path)
        if self.open_error:
            raise self.open_error
        fd = self.real_open(path, *args, **kwargs)
        self.fds.append(fd)
        return fd

    def fdopen(self, fd, *args, **kwargs):
        if fd not in self.fds:
            return self.real_fdopen(fd, *args, **kwargs)
        if self.fdopen_error:
            raise OSError("synthetic fdopen failure")
        handle = self.real_fdopen(fd, *args, **kwargs)
        self.handles.append(handle)
        return SpyHandle(handle, self)

    def fstat(self, fd):
        result = self.real_fstat(fd)
        if fd in self.fds:
            self.stats.append(result)
            if self.fstat_error:
                raise OSError("synthetic fstat failure")
            if self.after_fstat:
                self.after_fstat()
        return result

    def whole_file(self, path, *args, **kwargs):  # Path.read_bytes / read_text
        if path.name == body_schedules.FILE:
            self.unrestricted.append(path)
            raise AssertionError("unrestricted whole-file read of the schedule")
        return self.real_read_bytes(path, *args, **kwargs)

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        for target, name, value in ((os, "open", self.open), (os, "fdopen", self.fdopen), (os, "fstat", self.fstat),
                                    (Path, "read_bytes", lambda p, *a, **k: self.whole_file(p, *a, **k)),
                                    (Path, "read_text", lambda p, *a, **k: self.whole_file(p, *a, **k))):
            self.stack.enter_context(mock.patch.object(target, name, value))
        return self

    def __exit__(self, *exc):
        self.stack.close()
        return False

    def closed(self):
        """Every descriptor this reader acquired is closed (checked with the real fstat, outside the spy)."""
        for fd in self.fds:
            with self.test.assertRaises(OSError):
                self.real_fstat(fd)
        for handle in self.handles:
            self.test.assertTrue(handle.closed)


class BoundedReadTests(Base):
    """RF0325-2: one open schedule file is read at most MAX_JSON_BYTES + 1 bytes, refused before decode/parse."""

    def read(self, **options):
        with Spy(self, **options) as spy:
            try:
                return spy, read_body_schedules(self.root, today=TODAY), None
            except ContentError as exc:
                return spy, None, str(exc)

    def refused(self, needle, **options):
        spy, rows, error = self.read(**options)
        self.assertIsNone(rows)
        self.assertIsNotNone(error, "expected a refusal")
        self.assertIn(needle, error)
        return spy

    @staticmethod
    def fd_open(fd):
        try:
            os.fstat(fd)
        except OSError:
            return False
        return True

    def test_schedule_reader_exact_byte_cap_and_non_ascii(self):
        unicode_row = row(body="Conseil municipal d\u00e9mocratique \u2014 \u7c73")
        self.assertLess(len(unicode_row["body"]), len(unicode_row["body"].encode("utf-8")))
        for size in (CAP - 1, CAP):
            self.path.write_bytes(padded([unicode_row], size))
            self.assertEqual(self.path.stat().st_size, size)
            spy, rows, error = self.read()
            self.assertIsNone(error)
            self.assertEqual([r["body"] for r in rows], [unicode_row["body"]])
            self.assertEqual((spy.reads, spy.lengths, SpyBytes.decoded), ([CAP + 1], [size], [size]))
            spy.closed()
        for size in (CAP + 1, 4 * CAP):
            self.path.write_bytes(padded([], size))
            spy = self.refused("body-schedules.json: file too large")
            self.assertEqual((spy.reads, spy.lengths, SpyBytes.decoded), ([], [], []))
            spy.closed()

    def test_known_oversize_schedule_has_no_payload_read(self):
        self.path.write_bytes(padded([], 8 * 1024 * 1024))
        with mock.patch.object(body_schedules.json, "loads", side_effect=AssertionError("parsed")):
            spy = self.refused("body-schedules.json: file too large")
        self.assertEqual((len(spy.fds), len(spy.stats)), (1, 1))
        self.assertTrue(stat.S_ISREG(spy.stats[0].st_mode))
        self.assertGreater(spy.stats[0].st_size, CAP)
        self.assertEqual((spy.reads, spy.unrestricted, SpyBytes.decoded), ([], [], []))
        spy.closed()

    def test_schedule_growth_after_real_fstat_is_bounded_and_refused(self):
        def grow(extra):
            def append():
                with open(self.path, "ab") as handle:
                    handle.write(b" " * extra)
            return append
        self.path.write_bytes(padded([row()], CAP - 10))
        spy = self.refused("body-schedules.json: file too large", after_fstat=grow(100))
        self.assertEqual(spy.stats[0].st_size, CAP - 10)  # the below-cap observation is what the reader saw
        self.assertEqual(self.path.stat().st_size, CAP + 90)
        self.assertEqual((spy.reads, spy.lengths, SpyBytes.decoded, spy.unrestricted), ([CAP + 1], [CAP + 1], [], []))
        spy.closed()
        self.path.write_bytes(padded([row()], CAP - 10))  # control: growth to exactly the cap is accepted
        spy, rows, error = self.read(after_fstat=grow(10))
        self.assertIsNone(error)
        self.assertEqual([r["body"] for r in rows], [BODY])
        self.assertEqual((spy.reads, spy.lengths), ([CAP + 1], [CAP]))
        spy.closed()

    def test_schedule_shrink_and_malformed_inputs_keep_controlled_errors(self):
        for after in (b"", b"\xff\xfe", b'{"schema_version": 1,'):
            self.path.write_bytes(padded([row()], 4096))
            spy = self.refused("not valid UTF-8 JSON", after_fstat=lambda after=after: self.path.write_bytes(after))
            self.assertEqual(spy.stats[0].st_size, 4096)
            spy.closed()
        for value, needle in ((b"\xff", "not valid UTF-8 JSON"), (b"{not json", "not valid UTF-8 JSON"),
                              (b"[]", "expected an object"), (b"null", "expected an object"),
                              (json.dumps(document(row(), row())).encode(), "duplicate body"),
                              (json.dumps(document(row(checked_on="2026-11-05"))).encode(), "is in the future"),
                              (json.dumps({"schema_version": 1, "schedules": {}}).encode(), "must be a list")):
            self.path.write_bytes(value)
            self.refused(needle).closed()
        self.path.write_bytes(padded([row(checked_on="2000-01-01")]))  # stale rows stay valid
        spy, rows, error = self.read()
        self.assertEqual((error, [r["checked_on"] for r in rows]), (None, ["2000-01-01"]))
        spy.closed()

    def test_schedule_descriptor_admission_and_cleanup(self):
        sentinel = os.open(__file__, os.O_RDONLY)
        self.addCleanup(lambda: os.close(sentinel) if self.fd_open(sentinel) else None)
        spy, rows, error = self.read()  # absent optional store: [] and never opened
        self.assertEqual((rows, error, spy.opened), ([], None, []))
        self.path.mkdir()
        self.assertEqual(self.refused("must be a regular file").opened, [])
        self.path.rmdir()
        real = self.tmp / "real.json"
        real.write_text(json.dumps(document(row())), encoding="utf-8")
        for target in (real, self.tmp / "missing.json"):  # live, then dangling
            os.symlink(target, self.path)
            self.assertEqual(self.refused("body-schedules.json: symlinks are not read").opened, [])
            self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual(self.refused("must be a regular file").opened, [])
        with Spy(self) as spy:  # a FIFO that did reach the descriptor check is refused before any payload read
            with self.assertRaisesRegex(ContentError, "must be a regular file"):
                body_schedules._read_document(self.path)
        self.assertEqual((len(spy.fds), spy.reads), (1, []))
        spy.closed()
        self.path.unlink()
        for text, expected in ((document(), []), (document(row()), [BODY])):  # successful empty and nonempty reads
            self.write(text)
            spy, rows, error = self.read()
            self.assertEqual((error, [r["body"] for r in rows]), (None, expected))
            spy.closed()
        for options in ({"open_error": PermissionError(13, "denied")}, {"fdopen_error": True},
                        {"fstat_error": True}, {"read_error": True}):
            self.refused("body-schedules.json: could not be read", **options).closed()
        self.assertTrue(self.fd_open(sentinel))  # unrelated descriptors are never closed
        self.assertTrue(self.path.exists())  # and no failure is mistaken for absence

    def test_oversize_schedule_refusal_propagates_without_partial_results(self):
        self.path.write_bytes(padded([], CAP + 1))
        before = snapshot(self.root)
        message = "body-schedules.json: file too large"
        calls = ((body_schedule_cmd.list_schedules, {}), (body_schedule_cmd.add_schedule, row()),
                 (body_schedule_cmd.recheck_schedule, dict(body=BODY, checked_on="2026-11-04")),
                 (body_schedule_cmd.remove_schedule, dict(body=BODY)))
        with Spy(self) as spy, mock.patch.object(body_schedule_cmd, "_write", side_effect=AssertionError("writer")):
            for function, kwargs in calls:
                with self.assertRaisesRegex(body_schedule_cmd.BodyScheduleError, message):
                    function(self.root, today=TODAY, **kwargs)
        self.assertEqual((len(spy.fds), spy.reads, spy.unrestricted), (4, [], []))
        spy.closed()
        self.assertEqual(snapshot(self.root), before)
        for args in (["list"], ["remove", "--body", BODY]):
            out, err = io.StringIO(), io.StringIO()
            argv = ["campaign_tool", "body-schedule", *args, "--directory", str(self.root)]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as caught:
                    cli.main()
            self.assertEqual((caught.exception.code, out.getvalue()), (2, ""))
            self.assertIn(message, err.getvalue())
        items = today.todo(self.root, TODAY)
        self.assertEqual([item["kind"] for item in items if "body-schedules.json" in item["what"]], ["unreadable"])
        self.assertIn(message, [item["what"] for item in items if "body-schedules.json" in item["what"]][0])
        self.assertFalse([item for item in items if item["kind"] in ("expected_regular_meeting", "stale_body_schedule")])
        with self.assertRaisesRegex(ContentError, message):
            load_content(self.root, today=TODAY)
        self.assertEqual([line for line in content_problems(self.root) if "body-schedules.json" in line], [message])
        self.assertEqual(snapshot(self.root), before)

    def test_valid_schedule_outputs_and_old_pins_are_unchanged(self):
        self.write(document(row(body="Zed"), row(body="Alpha", ordinals=["last", 1], checked_on="2025-11-04")))
        expected = load_body_schedules(json.loads(self.path.read_text(encoding="utf-8")), today=TODAY)
        self.assertEqual(read_body_schedules(self.root, today=TODAY), expected)
        self.assertEqual([r["body"] for r in expected], ["Alpha", "Zed"])
        self.assertEqual(expected[0]["ordinals"], [1, "last"])
        before = snapshot(self.root)
        listing = body_schedule_cmd.list_schedules(self.root, today=TODAY)
        self.assertLess(listing.index("Alpha"), listing.index("Zed"))
        self.assertEqual(snapshot(self.root), before)


def body_schedules_labels():
    from campaign_tool.labels import DEFAULTS
    return DEFAULTS


if __name__ == "__main__":
    unittest.main()
