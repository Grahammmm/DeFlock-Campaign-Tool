"""RF0445-3: meetings-status classifies meetings by the parsed meeting-local calendar day, not a timestamp prefix.

Synthetic temporary campaigns only; nothing is fetched, sent or written outside the temp folder.
Run from the repository root: python3 -B -m unittest tests.test_meetings_status_calendar -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import meetings_status
from campaign_tool.meetings_status import inventory, render_text, run_cli
from tests.test_meetings_status import cli, snapshot, write

TODAY = date(2026, 11, 10)
AS_OF = "2026-11-09"
SRC = "https://example.invalid/agenda"
# The same January meeting (Thursday, ISO week 1 of 2026) in three admitted spellings; local day 2026-01-01.
JAN = {"ext": "2026-01-01T18:00:00-08:00", "basic": "20260101T180000-0800", "week": "2026-W01-4T18:00:00-08:00"}


def outcome(result="approved", minutes=True, votes=True):
    value = {"result": result, "on": "2026-01-05", "source_url": SRC, "verified_by": "Synthetic Verifier",
             "verified_at": "2026-01-05"}
    if minutes:
        value.update(minutes_url="https://example.invalid/minutes", minutes_approved_on="2026-01-05")
    if votes:
        value["vote"] = "4-1"
    return value


class CalendarBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-meetings-calendar-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "campaign"
        (self.root / "kit").mkdir(parents=True)
        (self.root / "content" / "meetings").mkdir(parents=True)

    def stage(self, mid, starts, **extra):
        write(self.root / "content" / "meetings" / f"{mid}.json",
              {"id": mid, "body": f"Body {mid}", "starts_at": starts, **extra})

    def scan(self, leads):
        write(self.root / "kit" / "meetings.json", {"schema_version": 1, "meetings": leads})

    def lead(self, mid, starts, **extra):
        return {"id": mid, "body": f"Scan {mid}", "starts_at": starts, **extra}

    def report(self, as_of=AS_OF):
        return inventory(self.root, as_of, directory="D", today=TODAY)

    def rows(self, as_of=AS_OF):
        return {row.get("id", row["path"]): row for row in self.report(as_of)["rows"]}

    def order(self, as_of=AS_OF):
        return [row.get("id", row["path"]) for row in self.report(as_of)["rows"]]


class StagedCalendarTests(CalendarBase):
    maxDiff = None

    def test_equivalent_staged_iso_spellings_share_period_and_outcome_hint(self):
        for key, stamp in JAN.items():
            self.stage(key, stamp)  # no participation, no kit: an upcoming reading would ask for participation
        rows = self.rows()
        for key, stamp in JAN.items():
            self.assertEqual(rows[key]["starts_at"], stamp)  # stored spelling kept verbatim
            self.assertEqual(rows[key]["period"], "past", key)
            self.assertEqual(rows[key]["next_step"], "record_outcome", key)
            self.assertEqual(rows[key]["command"], "python3 -m campaign_tool meetings-outcome --directory D --id "
                             f"{key} --result RESULT --on YYYY-MM-DD --source OFFICIAL_URL --verified-by NAME")
        # The only differences between the spellings are the identity and the stored text.
        strip = {k: v for k, v in rows["ext"].items() if k not in ("id", "path", "body", "starts_at", "command")}
        for key in ("basic", "week"):
            self.assertEqual({k: v for k, v in rows[key].items() if k not in ("id", "path", "body", "starts_at", "command")},
                             strip)

    def test_corrected_period_uses_existing_workflow_ladders(self):
        # Outcome dates are validated against the start's first ten characters by another module, so fixtures that
        # carry an outcome keep the canonical extended spelling; the spelling-sensitive rows have none.
        self.stage("p-none", JAN["basic"])
        self.stage("p-nominutes", JAN["ext"], outcome=outcome(minutes=False, votes=False))
        self.stage("p-novotes", JAN["ext"], outcome=outcome(votes=False))
        self.stage("p-done", JAN["ext"], outcome=outcome())
        self.stage("p-cancelled", JAN["basic"], status="cancelled")
        self.stage("p-continued", JAN["ext"], outcome=outcome("continued", minutes=False, votes=False))
        self.stage("up-ext", "2026-11-20T18:00:00-08:00")
        self.stage("up-basic", "20261121T180000-0800")
        rows = self.rows()
        self.assertEqual({mid: rows[mid]["next_step"] for mid in rows}, {
            "p-none": "record_outcome", "p-nominutes": "record_minutes", "p-novotes": "review_optional_votes",
            "p-done": None, "p-cancelled": None, "p-continued": "record_minutes",
            "up-ext": "verify_participation", "up-basic": "verify_participation"})
        self.assertIsNone(rows["p-cancelled"]["command"])
        self.assertEqual(rows["p-novotes"]["command"], "python3 -m campaign_tool meetings-outcome --help")
        self.assertEqual({mid: rows[mid]["period"] for mid in rows}, {
            "p-none": "past", "p-nominutes": "past", "p-novotes": "past", "p-done": "past", "p-cancelled": "past",
            "p-continued": "past", "up-ext": "upcoming", "up-basic": "upcoming"})
        # A continued meeting stays on the outcome ladder even when as-of makes it upcoming.
        later = self.rows("2025-12-01")
        self.assertEqual(later["p-continued"]["period"], "upcoming")
        self.assertEqual(later["p-continued"]["next_step"], "record_minutes")
        self.assertEqual(later["p-none"]["next_step"], "verify_participation")  # genuinely upcoming now

    def test_before_today_after_and_iso_week_year_boundary(self):
        self.stage("jan-ext", JAN["ext"])
        self.stage("jan-basic", JAN["basic"])
        self.stage("jan-week", JAN["week"])
        for as_of, period in (("2025-12-31", "upcoming"), ("2026-01-01", "upcoming"), ("2026-01-02", "past")):
            for mid, row in self.rows(as_of).items():
                self.assertEqual(row["period"], period, (as_of, mid))
        # ISO week 1 Monday of 2026 is 2025-12-29: the week-year in the text is not the calendar year.
        self.stage("wk-monday", "2026-W01-1T18:00:00-08:00")
        self.stage("wk-monday-basic", "20251229T180000-0800")
        for as_of, period in (("2025-12-28", "upcoming"), ("2025-12-29", "upcoming"), ("2025-12-30", "past")):
            rows = self.rows(as_of)
            for mid in ("wk-monday", "wk-monday-basic"):
                self.assertEqual(rows[mid]["period"], period, (as_of, mid))
        self.assertEqual(self.rows("2025-12-30")["wk-monday"]["starts_at"], "2026-W01-1T18:00:00-08:00")

    def test_leap_day_and_fractional_second_controls(self):
        self.stage("leap-ext", "2028-02-29T10:00:00-08:00")
        self.stage("leap-basic", "20280229T100000-0800")
        self.stage("frac-ext", "2026-01-01T18:00:00.500-08:00")
        self.stage("frac-basic", "20260101T180000.500-0800")
        for as_of, period in (("2028-02-28", "upcoming"), ("2028-02-29", "upcoming"), ("2028-03-01", "past")):
            rows = self.rows(as_of)
            for mid in ("leap-ext", "leap-basic"):
                self.assertEqual(rows[mid]["period"], period, (as_of, mid))
        rows = self.rows("2026-01-02")
        for mid in ("frac-ext", "frac-basic"):
            self.assertEqual(rows[mid]["period"], "past", mid)
        self.assertEqual(rows["frac-basic"]["starts_at"], "20260101T180000.500-0800")

    def test_local_day_not_utc_day_controls_period(self):
        # 20:00 at -08:00 is 04:00 UTC the next day; 02:00 at +09:00 is 17:00 UTC the previous day.
        cases = {"late-ext": "2026-01-01T20:00:00-08:00", "late-basic": "20260101T200000-0800",
                 "early-ext": "2026-01-01T02:00:00+09:00", "early-basic": "20260101T020000+0900",
                 "early-week": "2026-W01-4T02:00:00+09:00"}
        for mid, stamp in cases.items():
            self.stage(mid, stamp)
        for as_of, period in (("2025-12-31", "upcoming"), ("2026-01-01", "upcoming"), ("2026-01-02", "past")):
            rows = self.rows(as_of)
            for mid, stamp in cases.items():
                self.assertEqual((rows[mid]["period"], rows[mid]["starts_at"]), (period, stamp), (as_of, mid))

    def test_mixed_formats_preserve_two_bucket_order_and_ties(self):
        up = {"u-4-ext": "2026-11-20T18:00:00-08:00",      # wall 18:00
              "u-3-basic": "20261120T090000+0900",         # wall 09:00 (UTC instant is the earliest of the three)
              "u-2-week": "2026-W47-5T08:00:00.250-08:00",  # Friday 2026-11-20, wall 08:00:00.25
              "u-1-today": "20261109T235959-0800",         # last second of as-of day (already 2026-11-10 UTC)
              "u-6-tie-b": "20261125T100000-0800", "u-5-tie-a": "2026-11-25T10:00:00-08:00"}
        past = {"p-z-tie-basic": "20260101T180000-0800", "p-a-tie-ext": "2026-01-01T18:00:00-08:00",
                "p-m-oct": "2026-10-15T18:00:00-07:00", "p-n-nov8": "20261108T010000+0900"}
        for mid, stamp in {**up, **past}.items():
            self.stage(mid, stamp)
        self.stage("zz-broken", "2026-13-45T18:00:00-08:00")
        self.scan([self.lead("lead-past-basic", "20260102T090000-0800"), self.lead("lead-up", "2026-12-01T09:00:00-08:00"),
                   self.lead("lead-past-ext", "2026-01-03T09:00:00-08:00"), self.lead("lead-up-basic", "20261130T090000-0800")])
        self.assertEqual(self.order(), [
            "u-1-today", "u-2-week", "u-3-basic", "u-4-ext", "u-5-tie-a", "u-6-tie-b",
            "p-n-nov8", "p-m-oct", "p-a-tie-ext", "p-z-tie-basic",
            "content/meetings/zz-broken.json",
            "lead-up-basic", "lead-up", "lead-past-ext", "lead-past-basic"])
        rows = self.rows()
        self.assertEqual(rows["p-z-tie-basic"]["period"], "past")
        self.assertEqual(rows["u-1-today"]["period"], "upcoming")


class ScanLeadCalendarTests(CalendarBase):
    def test_scan_leads_use_parsed_local_day_without_changing_hint_policy(self):
        leads = [self.lead(f"scan-{key}", stamp) for key, stamp in JAN.items()]
        leads.append(self.lead("scan-cancelled-basic", JAN["basic"], status="cancelled"))
        leads.append(self.lead("scan-up-basic", "20261121T180000-0800"))
        write(self.root / "kit" / "meetings-manual.json", {"schema_version": 1, "meetings": [{
            "id": "manual-ext", "body": "Manual ext", "agency_id": "ca-cedar-police", "starts_at": JAN["ext"],
            "agenda_url": SRC, "agenda_item": "Item", "source_url": SRC, "checked_on": "2026-01-01", "source": "manual",
            "time_known": True, "status": "scheduled", "history": []}]})
        self.stage("staged-basic", JAN["basic"])
        self.scan(leads + [self.lead("staged-basic", JAN["basic"])])  # a staged id suppresses its lead
        rows = self.rows()
        for key, stamp in JAN.items():
            row = rows[f"scan-{key}"]
            self.assertEqual((row["kind"], row["period"], row["next_step"], row["starts_at"]),
                             ("unstaged", "past", "stage_meeting", stamp), key)
        self.assertEqual((rows["manual-ext"]["period"], rows["manual-ext"]["next_step"]), ("past", "stage_meeting"))
        self.assertEqual((rows["scan-cancelled-basic"]["period"], rows["scan-cancelled-basic"]["next_step"]),
                         ("past", None))
        self.assertEqual(rows["scan-up-basic"]["period"], "upcoming")
        self.assertEqual(rows["staged-basic"]["kind"], "staged")
        self.assertEqual([r["kind"] for r in self.report()["rows"] if r["id"] == "staged-basic"], ["staged"])
        self.assertEqual(self.order()[:1], ["staged-basic"])  # staged rows precede unstaged leads
        # upcoming leads first, then past leads newest first with deterministic id ties
        self.assertEqual([r["id"] for r in self.report()["rows"] if r["kind"] == "unstaged"],
                         ["scan-up-basic", "manual-ext", "scan-basic", "scan-cancelled-basic", "scan-ext", "scan-week"])


class CliAndCompatibilityTests(CalendarBase):
    def build_mixed(self):
        for key, stamp in JAN.items():
            self.stage(key, stamp)
        self.stage("future", "20261120T180000-0800", participation=None)
        self.scan([self.lead(f"scan-{key}", stamp) for key, stamp in JAN.items()])

    def test_real_cli_text_json_and_read_only_compatibility(self):
        self.build_mixed()
        before = snapshot(self.root)
        mtimes = {p: p.lstat().st_mtime_ns for p in self.root.rglob("*")}
        expected = ["future", "basic", "ext", "week", "scan-basic", "scan-ext", "scan-week"]
        for _ in range(2):
            text = cli("meetings-status", "--directory", str(self.root), "--as-of", AS_OF)
            as_json = cli("meetings-status", "--directory", str(self.root), "--as-of", AS_OF, "--json")
            for result in (text, as_json):
                self.assertEqual((result.returncode, result.stderr), (0, ""))
            lines = text.stdout.splitlines()
            self.assertEqual([line.split(" | ")[1] for line in lines[:len(expected)]], expected)
            for line in lines[:len(expected)]:
                self.assertEqual(line.count("\n"), 0)
            report = json.loads(as_json.stdout)
            rows = {r["id"]: r for r in report["rows"]}
            self.assertEqual([r["id"] for r in report["rows"]], expected)
            self.assertEqual({k: rows[k]["period"] for k in expected},
                             {"future": "upcoming", **{k: "past" for k in expected[1:]}})
            self.assertEqual(rows["basic"]["starts_at"], JAN["basic"])
            self.assertIn("meetings-outcome --directory", rows["basic"]["command"])
            self.assertIn(f"| {JAN['week']} |", text.stdout)
            self.assertTrue(lines[1].endswith("| private; review-required"))
            self.assertIn("next=python3 -m campaign_tool meetings-outcome --directory", lines[2])
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual({p: p.lstat().st_mtime_ns for p in self.root.rglob("*")}, mtimes)

    def test_default_date_path_uses_injected_clock(self):
        self.build_mixed()

        class Clock(date):
            @classmethod
            def today(cls):
                return cls(2026, 11, 10)

        stdout = io.StringIO()
        with mock.patch.object(meetings_status, "date", Clock), contextlib.redirect_stdout(stdout):
            code = run_cli(Namespace(as_of=None, as_json=True), str(self.root))
        report = json.loads(stdout.getvalue())
        self.assertEqual((code, report["as_of"]), (0, "2026-11-10"))
        self.assertEqual({r["id"]: r["period"] for r in report["rows"]}["basic"], "past")
        self.assertEqual({r["id"]: r["period"] for r in report["rows"]}["future"], "upcoming")

    def test_invalid_neighbor_and_legacy_extended_bytes_unchanged(self):
        self.stage("good-basic", JAN["basic"])
        self.stage("good-ext", JAN["ext"])
        sentinel = "SENTINEL-PARSER-PAYLOAD"
        self.stage("bad-text", sentinel)
        self.stage("bad-zone", "2026-01-01T18:00:00")  # no explicit offset: still refused
        self.stage("bad-date", "2026-13-45T18:00:00-08:00")
        self.scan([self.lead("scan-bad", sentinel), self.lead("scan-good", JAN["week"])])
        report = self.report()
        text = render_text(report)
        self.assertNotIn(sentinel, text + json.dumps(report))
        rows = report["rows"]
        self.assertEqual([r.get("id", r["path"]) for r in rows], [
            "good-basic", "good-ext",
            "content/meetings/bad-date.json", "content/meetings/bad-text.json", "content/meetings/bad-zone.json",
            "scan-good", "kit/meetings.json[0]"])
        for row in rows:
            if row["kind"] == "unreadable":
                self.assertEqual(set(row), {"kind", "path", "error", "next_step", "command"})
                self.assertEqual((row["error"], row["next_step"], row["command"]), ("unreadable_input", "repair_input", None))
        result = cli("meetings-status", "--directory", str(self.root), "--as-of", AS_OF)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(sentinel, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("good-basic", result.stdout)

    def test_ordinary_extended_report_is_unchanged(self):
        self.stage("a-future", "2026-11-20T18:00:00-08:00")
        self.stage("b-today", "2026-11-09T19:00:00-08:00")
        self.stage("c-past", "2026-10-01T18:00:00-07:00")
        report = self.report()
        self.assertEqual([(r["id"], r["period"], r["next_step"]) for r in report["rows"]], [
            ("b-today", "upcoming", "verify_participation"), ("a-future", "upcoming", "verify_participation"),
            ("c-past", "past", "record_outcome")])
        self.assertEqual(render_text(report).splitlines()[0],
                         "staged | b-today | Body b-today | 2026-11-09T19:00:00-08:00 | scheduled | participation=not_verified"
                         " | kit=missing | sent=not_recorded | speakers=absent | outcome=not_recorded | minutes=not_recorded"
                         " | votes=not_recorded | next=python3 -m campaign_tool meetings-stage --directory D --id b-today"
                         " --replace --participation FILE | private; review-required")


if __name__ == "__main__":
    unittest.main()
