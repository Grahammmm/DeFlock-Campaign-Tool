"""Schedule-derived health and the rendered activation/rollback scripts.

Health is computed from the approved 07:00 / 13:00 / 20:30 America/Los_Angeles
promise against ledger run rows. The activation and rollback scripts are
exercised against a fake ``systemctl`` on PATH so the exact command sequence,
state file and unit placement are verified without touching any real host.
"""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

from campaign_tool.records import schedule
from campaign_tool.records.ledger import store
from campaign_tool.records.run import Pipeline
from tests.records.test_run_slice import synthetic_email

LA = ZoneInfo("America/Los_Angeles")
REPO = Path(__file__).resolve().parents[2]


def la(*parts):
    return datetime(*parts, tzinfo=LA)


class SlotMathTests(unittest.TestCase):
    def test_due_and_next_slots_follow_the_zone_across_dst_end(self):
        # 2026-11-01 02:00 PDT -> 01:00 PST. The 20:30 slot on Oct 31 and the 07:00 slot on
        # Nov 1 are 11.5 hours apart in UTC, not 10.5.
        now = la(2026, 11, 1, 7, 5)
        due = schedule.due_slots(now)
        self.assertEqual([s.isoformat() for s in due],
                         ["2026-11-01T07:00:00-08:00", "2026-10-31T20:30:00-07:00", "2026-10-31T13:00:00-07:00"])
        # Same-tzinfo subtraction is wall-clock in Python; compare instants instead.
        self.assertEqual(due[0].timestamp() - due[1].timestamp(), 11.5 * 3600)
        self.assertEqual(schedule.next_slot(now).isoformat(), "2026-11-01T13:00:00-08:00")

    def test_due_slots_before_first_slot_of_day_reach_back_to_previous_day(self):
        now = la(2026, 10, 2, 6, 59)
        self.assertEqual(schedule.due_slots(now, 1)[0], la(2026, 10, 1, 20, 30))
        self.assertEqual(schedule.next_slot(now), la(2026, 10, 2, 7, 0))

    def test_naive_datetimes_are_rejected(self):
        with self.assertRaises(ValueError):
            schedule.due_slots(datetime(2026, 10, 1, 8, 0))


class HealthTests(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.tmp = tempfile.TemporaryDirectory(prefix="records-health-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root"
        self.root.mkdir(mode=0o700)
        store.initialize(self.root / "ledger.sqlite")

    def add_run(self, run_id, started, ended, status="completed", kind="stage-runner"):
        with store.ledger(self.root / "ledger.sqlite") as con:
            con.execute("INSERT INTO runs(run_id,kind,started_at,ended_at,engine_version,image_digest,config_sha256,"
                        "coverage_cutoff,status,summary) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (run_id, kind, started.isoformat(), ended.isoformat() if ended else None, "t", None, None, None,
                         status, "{}"))
            con.commit()

    def statuses(self, now):
        report = schedule.health(self.root, now=now)
        return report["status"], [s["status"] for s in report["slots"]], report

    def test_never_and_missing_ledger(self):
        status, slots, report = self.statuses(la(2026, 10, 1, 9, 0))
        self.assertEqual((status, slots, report["exit_code"]), ("never", [], 1))
        missing = schedule.health(self.root / "nope", now=la(2026, 10, 1, 9, 0))
        self.assertEqual((missing["status"], missing["exit_code"]), ("never", 2))

    def test_ok_late_missed_failed_and_stalled(self):
        self.add_run("r1", la(2026, 10, 1, 7, 1), la(2026, 10, 1, 7, 9))
        self.assertEqual(self.statuses(la(2026, 10, 1, 8, 0))[0], "ok")
        # 13:00 slot due, inside grace, no run yet -> late (exit 0), the 07:00 slot still ok.
        status, slots, report = self.statuses(la(2026, 10, 1, 13, 20))
        self.assertEqual((status, slots[:2], report["exit_code"]), ("late", ["late", "ok"], 0))
        # Grace passed -> missed (exit 1).
        status, slots, report = self.statuses(la(2026, 10, 1, 14, 0))
        self.assertEqual((status, slots[:2], report["exit_code"]), ("missed", ["missed", "ok"], 1))
        # A failed run for the 13:00 slot.
        self.add_run("r2", la(2026, 10, 1, 13, 2), la(2026, 10, 1, 13, 3), status="failed")
        self.assertEqual(self.statuses(la(2026, 10, 1, 14, 0))[0], "failed")
        # A run that started for the 20:30 slot and never finalised: running inside grace, stalled after.
        self.add_run("r3", la(2026, 10, 1, 20, 31), None, status="running")
        status, slots, report = self.statuses(la(2026, 10, 1, 20, 50))
        self.assertEqual((status, slots[0]), ("degraded", "running"))  # degraded: 13:00 failed in lookback
        status, slots, report = self.statuses(la(2026, 10, 1, 22, 0))
        self.assertEqual((status, slots[0], report["exit_code"]), ("stalled", "stalled", 1))
        self.assertEqual(report["missed_in_lookback"], 2)

    def test_run_finishing_after_slot_counts_for_that_slot(self):
        # Started 06:50 (before the 07:00 slot, e.g. Persistent catch-up after a reboot) and ended 07:10.
        self.add_run("r1", la(2026, 10, 1, 6, 50), la(2026, 10, 1, 7, 10))
        self.assertEqual(self.statuses(la(2026, 10, 1, 7, 30))[1][0], "ok")

    def test_record_writes_keyed_alerts_once_per_slot(self):
        self.add_run("r1", la(2026, 10, 1, 7, 1), la(2026, 10, 1, 7, 9))
        report = schedule.health(self.root, now=la(2026, 10, 1, 14, 0))
        self.assertEqual(schedule.record_health(self.root, report), 1)
        self.assertEqual(schedule.record_health(self.root, report), 1)
        con = sqlite3.connect(self.root / "ledger.sqlite")
        rows = con.execute("SELECT key,count,state FROM alerts").fetchall()
        con.close()
        self.assertEqual(rows, [("schedule:missed:2026-10-01T13:00:00-07:00", 2, "open")])

    def test_health_after_a_real_pipeline_run_is_ok_and_cli_exit_codes_agree(self):
        inbox = Path(self.tmp.name) / "inbox"
        inbox.mkdir(mode=0o700)
        (inbox / "m.eml").write_bytes(synthetic_email())
        Pipeline(self.root).run(inbox)
        now = datetime.now(ZoneInfo("UTC"))
        report = schedule.health(self.root, now=now)
        self.assertEqual(report["slots"][0]["status"], "ok", report)
        self.assertEqual(report["status"], "ok")
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "health", "--root", str(self.root),
                                 "--now", now.isoformat(), "--json"], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ok")
        # The same ledger a day later with no further runs is missed.
        later = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "health", "--root", str(self.root),
                                "--now", (now + timedelta(days=1)).isoformat()], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(later.returncode, 1)
        self.assertIn("missed", later.stdout)


class RenderAndActivationTests(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.tmp = tempfile.TemporaryDirectory(prefix="records-sched-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "root"
        self.root.mkdir(mode=0o700)
        self.mail = self.base / "mail.json"
        self.mail.write_text("{}")
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.bin = self.base / "bin"
        self.bin.mkdir(mode=0o700)
        self.calls = self.base / "systemctl.log"
        # Fake systemctl: logs every call; reports a legacy timer as enabled+active.
        shim = self.bin / "systemctl"
        shim.write_text("#!/bin/sh\n"
                        f"echo \"$@\" >> {self.calls}\n"
                        "case \"$*\" in\n"
                        "  *is-enabled*legacy-mail.timer*) echo enabled ;;\n"
                        "  *is-active*legacy-mail.timer*) echo active ;;\n"
                        "  *is-enabled*) echo disabled; exit 1 ;;\n"
                        "  *is-active*) echo inactive; exit 3 ;;\n"
                        "esac\n")
        os.chmod(shim, 0o700)
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(self.home)}
        self.env.pop("XDG_CONFIG_HOME", None)

    def run_script(self, name, *args):
        return subprocess.run(["sh", str(self.root / "ops" / name), *args], env=self.env, capture_output=True, text=True)

    def test_render_writes_resolved_units_with_the_approved_calendar(self):
        written = schedule.render(self.root, engine_root=REPO, python="/opt/py/bin/python3", mail_config=self.mail, ocr=True)
        self.assertEqual(sorted(written), ["activate.sh", "records-run.service", "records-run.timer", "rollback.sh"])
        timer = (self.root / "ops/records-run.timer").read_text()
        for line in ("OnCalendar=*-*-* 07:00:00 America/Los_Angeles", "OnCalendar=*-*-* 13:00:00 America/Los_Angeles",
                     "OnCalendar=*-*-* 20:30:00 America/Los_Angeles", "Persistent=true", "Unit=records-run.service"):
            self.assertIn(line, timer)
        service = (self.root / "ops/records-run.service").read_text()
        self.assertIn("ExecStart=/opt/py/bin/python3 -m campaign_tool.records run --root", service)
        self.assertIn("--mail-config", service)
        self.assertIn(" --ocr --json", service)
        self.assertIn("ExecStartPost=/opt/py/bin/python3 -m campaign_tool.records health --root", service)
        self.assertIn("UMask=0077", service)
        self.assertNotIn("@", service.split("[Service]")[1])  # no unresolved placeholders
        for name in ("activate.sh", "rollback.sh"):
            self.assertEqual(oct(os.stat(self.root / "ops" / name).st_mode & 0o777), "0o700")
        for name in ("records-run.service", "records-run.timer"):
            self.assertEqual(oct(os.stat(self.root / "ops" / name).st_mode & 0o777), "0o600")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not installed")
    def test_calendar_lines_are_accepted_by_systemd_and_elapse_in_the_zone(self):
        schedule.render(self.root, engine_root=REPO)
        timer = (self.root / "ops/records-run.timer").read_text()
        specs = [line.split("=", 1)[1] for line in timer.splitlines() if line.startswith("OnCalendar=")]
        self.assertEqual(len(specs), 3)
        for spec in specs:
            result = subprocess.run(["systemd-analyze", "calendar", "--base-time=2026-11-01 00:00:00 UTC", spec],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Next elapse", result.stdout)
            # The elapse is reported in the zone's local time (PDT/PST), not the host zone.
            self.assertRegex(result.stdout, r"Next elapse: .* (PDT|PST)\n")

    def test_activate_dry_run_changes_nothing(self):
        schedule.render(self.root, engine_root=REPO)
        result = self.run_script("activate.sh", "--dry-run", "--retire", "legacy-mail.timer")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("+ systemctl --user disable --now legacy-mail.timer", result.stdout)
        self.assertIn("+ systemctl --user enable --now records-run.timer", result.stdout)
        self.assertFalse((self.home / ".config/systemd/user/records-run.timer").exists())
        self.assertFalse((self.root / "ops/activation-state.txt").exists())
        # Only the is-enabled/is-active probes ran against systemctl.
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        self.assertTrue(all(c.split()[1] in ("is-enabled", "is-active") for c in calls), calls)

    def test_activate_then_rollback_restores_the_legacy_timer(self):
        schedule.render(self.root, engine_root=REPO)
        result = self.run_script("activate.sh", "--retire", "legacy-mail.timer")
        self.assertEqual(result.returncode, 0, result.stderr)
        units = self.home / ".config/systemd/user"
        self.assertTrue((units / "records-run.timer").exists())
        self.assertTrue((units / "records-run.service").exists())
        calls = self.calls.read_text().splitlines()
        self.assertIn("--user disable --now legacy-mail.timer", calls)
        self.assertIn("--user daemon-reload", calls)
        self.assertIn("--user enable --now records-run.timer", calls)
        self.assertLess(calls.index("--user disable --now legacy-mail.timer"), calls.index("--user enable --now records-run.timer"))
        state = (self.root / "ops/activation-state.txt").read_text()
        self.assertIn("legacy-mail.timer enabled=enabled active=active", state)
        self.calls.unlink()
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(calls[0], "--user disable --now records-run.timer")
        self.assertIn("--user enable --now legacy-mail.timer", calls)
        self.assertFalse((units / "records-run.timer").exists())
        self.assertFalse((units / "records-run.service").exists())
        self.assertIn("untouched", result.stdout)

    def test_rollback_without_retired_timers_does_not_enable_anything(self):
        schedule.render(self.root, engine_root=REPO)
        self.run_script("activate.sh")
        self.calls.unlink()
        self.run_script("rollback.sh")
        calls = self.calls.read_text().splitlines()
        self.assertFalse(any("enable" in c for c in calls), calls)

    def test_schedule_cli_render_and_show(self):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "schedule", "render", "--root",
                                 str(self.root), "--engine-root", str(REPO)], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("activate.sh --dry-run first", result.stdout)
        shown = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "schedule", "show", "--now",
                                "2026-10-01T12:00:00-07:00"], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(json.loads(shown.stdout)["next"], "2026-10-01T13:00:00-07:00")


if __name__ == "__main__":
    unittest.main()
