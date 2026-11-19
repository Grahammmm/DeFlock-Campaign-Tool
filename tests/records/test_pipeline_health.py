"""Silent-stop watch: every check, the status line, alerts and the CLI, on synthetic runtime dirs."""
from datetime import datetime, timedelta, timezone
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from zoneinfo import ZoneInfo

from campaign_tool.records import pipeline_health as ph, schedule
from campaign_tool.records.ledger import store

REPO = Path(__file__).resolve().parents[2]
GIB = 1024 ** 3
NOW = datetime(2026, 11, 4, 22, 30, tzinfo=timezone.utc)  # 14:30 PST, after the 13:00 slot
STAGES = ("sync", "delta", "adoption", "portal", "extract", "digest_gates", "outputs", "notify")
ZERO = {"input": 0, "output": 0, "failed": 0, "skipped": 0}
REF = "ab" * 32
CANARY = "CANARY-private-agency@example.org /home/canary/secret"


def stamp_text(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def report_for(finished, status="ok", seq=0, notify_failed=False):
    started = finished - timedelta(seconds=60)
    stages = []
    for name in STAGES:
        if status == "fail" and name == "sync":
            stages.append({"name": name, "status": "fail", "counts": dict(ZERO), "duration_seconds": 1.0,
                           "error_code": "SYNC_FAILED"})
        elif status == "fail":
            stages.append({"name": name, "status": "skipped", "counts": dict(ZERO), "duration_seconds": None,
                           "error_code": "UPSTREAM_FAILED"})
        elif name == "notify" and notify_failed:
            stages.append({"name": name, "status": "fail", "counts": dict(ZERO), "duration_seconds": 1.0,
                           "error_code": "NOTIFY_FAILED"})
        else:
            stages.append({"name": name, "status": "ok", "counts": dict(ZERO), "duration_seconds": 1.0,
                           "error_code": None})
    return {"schema_version": 1, "report_kind": "flock-records-run-report",
            "run_id": "run_%sZ_%08x" % (started.strftime("%Y%m%dT%H%M%S"), seq), "mode": "scheduled",
            "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_seconds": 60.0, "overall_status": "fail" if (status == "fail" or notify_failed) else "ok",
            "stages": stages, "new_data": {"documents": 0, "pages": 0, "extracted_units": 0, "portal_releases": 0,
                                           "request_status_changes": 0},
            "review_required_count": 0, "notify_failed": notify_failed,
            "needs_g": []}


def vfs(available_gib, frsize=1024):
    """statvfs stand-in: f_bavail is what counts; f_bfree (reserved blocks included) and f_bsize deliberately mislead."""
    blocks = int(available_gib * GIB) // frsize
    return types.SimpleNamespace(f_bavail=blocks, f_bfree=blocks + 500 * GIB // frsize, f_frsize=frsize, f_bsize=4096)


class Base(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        tmp = tempfile.TemporaryDirectory(prefix="records-ph-")
        self.addCleanup(tmp.cleanup)
        # Health assertions must not depend on how full the host disk is: every test sees a roomy volume unless it says otherwise.
        self.free_gib = 500
        patcher = mock.patch.object(os, "statvfs", side_effect=lambda path: vfs(self.free_gib))
        self.statvfs = patcher.start()
        self.addCleanup(patcher.stop)
        self.base = Path(tmp.name)
        self.runtime = self.base / "runtime"
        self.target_dir = self.runtime / "t"
        for directory in (self.runtime, self.target_dir, self.target_dir / "reports", self.target_dir / "alerts"):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = self.base / "root"
        self.root.mkdir(mode=0o700)
        store.initialize(self.root / "ledger.sqlite")
        self.store = self.base / "store"
        (self.store / "mail-poll").mkdir(mode=0o700, parents=True)
        self.add_ledger_run(NOW - timedelta(minutes=90), NOW - timedelta(minutes=89))
        self.write_report(NOW - timedelta(hours=1))
        self.write_poll(NOW - timedelta(minutes=10))

    def add_ledger_run(self, started, ended):
        with store.ledger(self.root / "ledger.sqlite") as con:
            con.execute("INSERT INTO runs(run_id,kind,started_at,ended_at,engine_version,image_digest,config_sha256,"
                        "coverage_cutoff,status,summary) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        ("r-" + started.isoformat(), "stage-runner", started.isoformat(), ended.isoformat(), "t",
                         None, None, None, "completed", "{}"))
            con.commit()

    def write_report(self, finished, status="ok", seq=0, latest=True, **kw):
        data = report_for(finished, status, seq, **kw)
        text = json.dumps(data)
        (self.target_dir / "reports" / (data["run_id"] + ".json")).write_text(text)
        if latest:
            (self.target_dir / "run-report.json").write_text(text)

    def write_poll(self, updated):
        (self.store / "mail-poll" / "state.json").write_text(json.dumps({"updated_at": updated.isoformat()}))

    def write_stamp(self, seen):
        arrival_dir = self.runtime / "arrival"
        arrival_dir.mkdir(mode=0o700, exist_ok=True)
        name = seen.strftime("%Y%m%dT%H%M%S.%f") + "Z-inbox-watch.json"
        (arrival_dir / name).write_text(json.dumps({"source": "inbox-watch", "ref": REF, "seen_at": stamp_text(seen)}))

    def assess(self, now=NOW, **kw):
        args = dict(runtime_dir=self.runtime, target="t", records_root=self.root, mail_store=self.store, now=now)
        args.update(kw)
        return ph.assess(**args)

    def check(self, report, name):
        return next(item for item in report["checks"] if item["check"] == name)

    def main(self, *extra, target="t"):
        """The CLI in-process (so the stubbed filesystem applies); the result mimics a CompletedProcess."""
        argv = ["--runtime-dir", str(self.runtime), "--target", target, "--root", str(self.root),
                "--mail-store", str(self.store), "--now", NOW.isoformat(), *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = ph.main(argv)
            except SystemExit as raised:  # argparse refusals
                code = raised.code
        return types.SimpleNamespace(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())


class CheckTests(Base):
    def test_all_ok(self):
        report = self.assess()
        self.assertEqual((report["state"], report["exit_code"]), ("ok", 0))
        self.assertEqual(ph.status_line(report), "Run health: ok - last run 60 min ago, 0 waiting")

    def test_arrival_not_consumed(self):
        self.write_stamp(NOW - timedelta(minutes=31))
        report = self.assess()
        item = self.check(report, "arrival_backlog")
        self.assertEqual((item["state"], item["reason_code"]), ("stop", "arrival_not_consumed"))
        self.assertEqual(report["state"], "stop")
        self.assertEqual(ph.status_line(report),
                         "Run health: STOPPED - arrival_not_consumed (1 waiting, oldest 31 min)")

    def test_arrival_within_limit_and_malformed(self):
        self.write_stamp(NOW - timedelta(minutes=29))
        report = self.assess()
        self.assertEqual((report["state"], report["waiting"]), ("ok", 1))
        (self.runtime / "arrival" / "20261104T000000.000000Z-inbox-watch-01.json").write_text("{not json")
        report = self.assess()
        item = self.check(report, "arrival_backlog")
        self.assertEqual((item["state"], item["reason_code"]), ("unknown", "arrival_unreadable"))
        self.assertEqual(report["state"], "unknown")

    def test_mail_poll_stale_bounds(self):
        def state(delta):
            self.write_poll(NOW - delta)
            return self.check(self.assess(), "mail_poll")["state"]
        self.assertEqual(state(timedelta(hours=5, minutes=59)), "ok")
        self.assertEqual(state(timedelta(hours=6, minutes=1)), "warn")
        self.assertEqual(state(timedelta(hours=24, minutes=1)), "stop")
        (self.store / "mail-poll" / "state.json").unlink()
        item = self.check(self.assess(), "mail_poll")
        self.assertEqual((item["state"], item["reason_code"]), ("unknown", "mail_poll_never"))
        item = self.check(self.assess(mail_store=None), "mail_poll")
        self.assertEqual((item["state"], item["reason_code"]), ("unknown", "mail_poll_not_configured"))
        (self.store / "mail-poll" / "state.json").write_text("[]")
        self.assertEqual(self.check(self.assess(), "mail_poll")["reason_code"], "mail_poll_unreadable")

    def test_no_recent_run_and_invalid_report(self):
        item = self.check(self.assess(now=NOW + timedelta(hours=25, minutes=1)), "last_run")
        self.assertEqual((item["state"], item["reason_code"]), ("stop", "no_recent_run"))
        self.assertEqual(self.check(self.assess(now=NOW + timedelta(hours=24)), "last_run")["state"], "ok")
        data = report_for(NOW - timedelta(hours=1))
        data["stages"].pop()
        (self.target_dir / "run-report.json").write_text(json.dumps(data))
        item = self.check(self.assess(), "last_run")
        self.assertEqual((item["state"], item["reason_code"]), ("stop", "run_report_invalid"))

    def test_repeated_failures(self):
        for index in range(3):
            self.write_report(NOW - timedelta(minutes=50 - 10 * index), "fail", seq=index + 1)
        item = self.check(self.assess(), "run_failures")
        self.assertEqual((item["state"], item["reason_code"]), ("stop", "repeated_failures"))

    def test_fail_ok_fail_is_only_a_warning(self):
        self.write_report(NOW - timedelta(minutes=50), "fail", seq=1)
        self.write_report(NOW - timedelta(minutes=40), "ok", seq=2)
        self.write_report(NOW - timedelta(minutes=30), "fail", seq=3)
        item = self.check(self.assess(), "run_failures")
        self.assertEqual((item["state"], item["reason_code"]), ("warn", "latest_run_failed"))

    def test_notify_failed_after_last_success(self):
        alert = self.target_dir / "alerts" / "notify-failed-run_x.json"
        alert.write_text("{}")
        os.utime(alert, ((NOW - timedelta(minutes=5)).timestamp(),) * 2)
        item = self.check(self.assess(), "notify")
        self.assertEqual((item["state"], item["reason_code"]), ("warn", "notify_failed"))
        os.utime(alert, ((NOW - timedelta(hours=5)).timestamp(),) * 2)
        self.assertEqual(self.check(self.assess(), "notify")["state"], "ok")

    def test_weekend_gap_is_not_a_missed_or_stale_run(self):
        pst = timezone(timedelta(hours=-8))
        friday = datetime(2026, 11, 6, 15, 0, tzinfo=pst)
        for earlier in (friday - timedelta(days=1), friday - timedelta(hours=8)):  # Thu 15:00 and Fri 07:00 slots
            self.add_ledger_run(earlier + timedelta(minutes=1), earlier + timedelta(minutes=9))
        self.add_ledger_run(friday + timedelta(minutes=1), friday + timedelta(minutes=9))
        self.write_report(friday + timedelta(minutes=9), seq=1)
        for now in (datetime(2026, 11, 7, 12, 0, tzinfo=pst), datetime(2026, 11, 8, 23, 0, tzinfo=pst),
                    datetime(2026, 11, 9, 6, 59, tzinfo=pst)):
            with self.subTest(now=now.isoformat()):
                report = self.assess(now=now)
                self.assertEqual((self.check(report, "last_run")["state"], self.check(report, "schedule")["state"]),
                                 ("ok", "ok"), report)
        # Tuesday afternoon with no run since Friday is a real miss, and says so.
        tuesday = datetime(2026, 11, 10, 16, 0, tzinfo=pst)
        report = self.assess(now=tuesday)
        self.assertEqual(self.check(report, "last_run")["reason_code"], "no_recent_run")
        self.assertEqual(self.check(report, "schedule")["state"], "stop")

    def test_custom_calendar_is_honoured(self):
        spec = schedule.Schedule("Mon,Wed", "09:00", "America/New_York")
        ny = ZoneInfo("America/New_York")
        monday = datetime(2026, 11, 2, 9, 0, tzinfo=ny)
        self.add_ledger_run(monday + timedelta(minutes=1), monday + timedelta(minutes=5))
        self.write_report(monday + timedelta(minutes=5), seq=2)
        tuesday = datetime(2026, 11, 3, 18, 0, tzinfo=ny)  # 33 h later, past the 26 h limit: no slot has been skipped
        report = self.assess(now=tuesday, spec=spec)
        self.assertEqual((self.check(report, "last_run")["state"], self.check(report, "schedule")["state"]), ("ok", "ok"))

    def test_sparse_calendar_month_old_run_is_not_recent(self):
        spec = schedule.Schedule("Mon", "07:00")
        ny = ZoneInfo("America/Los_Angeles")
        thursday = datetime(2026, 11, 12, 12, 0, tzinfo=ny)
        old = thursday - timedelta(days=30)
        self.write_report(old, seq=3)
        report = self.assess(now=thursday, spec=spec)
        self.assertEqual(self.check(report, "last_run")["reason_code"], "no_recent_run")

    def test_schedule_missed_is_stop(self):
        item = self.check(self.assess(now=NOW + timedelta(hours=10)), "schedule")
        self.assertEqual((item["state"], item["reason_code"]), ("stop", "schedule_missed"))

    def test_unknown_never_ok(self):
        empty = self.base / "empty"
        empty.mkdir(mode=0o700)
        report = ph.assess(runtime_dir=empty, target="t", records_root=empty / "none", now=NOW)
        self.assertEqual((report["state"], report["exit_code"]), ("unknown", 2))
        self.assertTrue(ph.status_line(report).startswith("Run health: UNKNOWN - "))

    def test_read_only_assess(self):
        self.write_stamp(NOW - timedelta(minutes=5))

        def snapshot():
            files = {}
            for path in sorted(self.base.rglob("*")):
                if path.name.startswith("ledger.sqlite-") or path == self.root:
                    continue  # SQLite WAL sidecars appear on any read-only open of a WAL ledger
                info = path.lstat()
                files[str(path)] = (info.st_mtime_ns, info.st_size, path.read_bytes() if path.is_file() else None)
            return files
        before = snapshot()
        self.assess()
        self.assess(now=NOW + timedelta(days=2))
        after = snapshot()
        self.assertEqual([k for k in set(before) | set(after) if before.get(k) != after.get(k)], [])

    def test_disk_assessment_uses_available_bytes_and_distinct_devices(self):
        """Owner of the disk check. Catches: f_bfree or f_bsize instead of f_bavail * f_frsize, a path in the report,
        rounding the classification, counting one volume twice, a failed read hiding a known low volume or reading as ok."""
        run, rec = "/fake/runtime", "/fake/records"
        fail = OSError(5, "I/O error")

        def observe(runtime, root, same_device=False, warn=100, stop=30):
            """(runtime, root) available GiB or an OSError; returns the check and the paths statvfs was asked for."""
            volumes = {run: (1, runtime), rec: (1 if same_device else 2, root)}
            asked = []

            def fake_stat(path, *args, **kw):
                return types.SimpleNamespace(st_dev=volumes[str(path)][0])

            def fake_statvfs(path):
                asked.append(str(path))
                value = volumes[str(path)][1]
                if isinstance(value, OSError):
                    raise value
                return vfs(value)
            with mock.patch.object(os, "stat", fake_stat), mock.patch.object(os, "statvfs", fake_statvfs):
                item = ph._disk(run, rec, dict(ph.LIMITS, disk_warn_gib=warn, disk_stop_gib=stop))
            return item, asked

        for label, runtime, root, same, expected in (
                ("above warn", 150, 200, False, ("ok", "disk_ok", 150.0, "runtime")),
                ("exactly warn is ok", 100, 100, False, ("ok", "disk_ok", 100.0, "runtime")),
                ("just under warn", 99.99, 150, False, ("warn", "disk_low", 100.0, "runtime")),
                ("between, records root lower", 150, 60, False, ("warn", "disk_low", 60.0, "records_root")),
                ("exactly stop is only a warning", 30, 80, False, ("warn", "disk_low", 30.0, "runtime")),
                ("just under stop, classified from bytes", 29.999, 80, False, ("stop", "disk_critical", 30.0, "runtime")),
                ("below stop on the records root", 500, 10, False, ("stop", "disk_critical", 10.0, "records_root")),
                ("stop beats a lower-free warning", 40, 20, False, ("stop", "disk_critical", 20.0, "records_root")),
                ("tied roles go to runtime", 10, 10, False, ("stop", "disk_critical", 10.0, "runtime")),
                ("unreadable runtime, other critical", fail, 10, False, ("stop", "disk_critical", 10.0, "records_root")),
                ("unreadable records root, other warning", 60, fail, False, ("warn", "disk_low", 60.0, "runtime")),
                ("unreadable runtime, other ok", fail, 150, False, ("unknown", "disk_unreadable", None, "runtime")),
                ("unreadable alone", 150, fail, False, ("unknown", "disk_unreadable", None, "records_root")),
                ("same device is read once", 150, 10, True, ("ok", "disk_ok", 150.0, "runtime"))):
            with self.subTest(label):
                item, asked = observe(runtime, root, same)
                self.assertEqual((item["state"], item["reason_code"], item["free_gib"], item["path_role"]), expected)
                self.assertEqual(asked, [run] if same else [run, rec])
                self.assertNotIn("/fake", json.dumps(item))
        self.assertEqual(observe(70, 70, warn=70, stop=40)[0]["state"], "ok")  # the limits come from the options

    def test_disk_check_in_report_line_and_alerts(self):
        """Contract: the disk check is part of assess, the line and the shared alert writer (dispatch only; the generic
        state machine is owned by test_alert_dedup_and_clear)."""
        alerts = self.target_dir / "alerts"
        report = self.assess()
        self.assertEqual((self.check(report, "disk")["reason_code"], report["state"]), ("disk_ok", "ok"))
        self.free_gib = 50
        report = self.assess()
        self.assertEqual((report["state"], report["exit_code"]), ("warn", 1))
        self.assertEqual(ph.status_line(report), "Run health: WARNING - disk_low (runtime, 50.0 GiB free)")
        self.assertEqual(ph.write_alerts(report, alerts), 1)
        self.assertEqual(os.listdir(alerts), ["health-disk-disk_low.json"])
        self.free_gib = 10
        report = self.assess()
        self.assertEqual(ph.status_line(report), "Run health: STOPPED - disk_critical (runtime, 10.0 GiB free)")
        self.assertEqual(ph.write_alerts(report, alerts), 1)
        self.assertEqual(sorted(os.listdir(alerts)), ["health-disk-disk_critical.json", "health-disk-disk_low.cleared.json"])
        with mock.patch.object(os, "statvfs", side_effect=OSError(5, "I/O error")):  # unreadable never clears an open alert
            report = self.assess()
            self.assertEqual(self.check(report, "disk")["reason_code"], "disk_unreadable")
            self.assertEqual(ph.write_alerts(report, alerts), 0)
        self.assertIn("health-disk-disk_critical.json", os.listdir(alerts))
        self.free_gib = 500
        self.assertEqual(ph.write_alerts(self.assess(), alerts), 0)
        self.assertNotIn("health-disk-disk_critical.json", os.listdir(alerts))

    def test_status_line_content_free(self):
        report = self.assess(now=NOW + timedelta(days=3))
        report["checks"] = [dict(item, state="stop", reason_code=item["reason_code"] + "_x" * 20) for item in
                            report["checks"]] + [dict(report["checks"][0], reason_code=CANARY)]
        report["state"] = "stop"
        line = ph.status_line(report)
        self.assertLess(len(line), 200)
        self.assertNotIn("\n", line)
        # Report content reaches the line only through fixed codes; canary values in a report body never do.
        data = report_for(NOW - timedelta(hours=1))
        data["needs_g"] = []
        data["run_id"] = "run_20261104T210000Z_0123abcd"
        (self.target_dir / "run-report.json").write_text(json.dumps(dict(data, extra=CANARY)))
        clean = ph.status_line(self.assess())
        self.assertNotIn("CANARY", clean)
        self.assertNotIn(str(self.base), clean)
        self.assertNotIn("0123abcd", clean)


class AlertTests(Base):
    def stop_report(self, now=NOW):
        self.write_stamp(NOW - timedelta(minutes=45))
        return self.assess(now=now)

    def test_alert_dedup_and_clear(self):
        wrapper = self.target_dir / "alerts" / "notify-failed-run_x.json"
        wrapper.write_text('{"wrapper": 1}')
        os.utime(wrapper, ((NOW - timedelta(hours=9)).timestamp(),) * 2)
        alerts = self.target_dir / "alerts"
        self.assertEqual(ph.write_alerts(self.stop_report(), alerts), 1)
        self.assertEqual(ph.write_alerts(self.assess(), alerts), 1)
        files = sorted(os.listdir(alerts))
        self.assertEqual(files, ["health-arrival_backlog-arrival_not_consumed.json", "notify-failed-run_x.json"])
        data = json.loads((alerts / files[0]).read_text())
        self.assertEqual((data["count"], data["state"], data["schema"]), (2, "stop", "records-health-alert-v1"))
        self.assertEqual(oct(os.lstat(alerts / files[0]).st_mode & 0o777), "0o600")
        for path in (self.runtime / "arrival").iterdir():
            path.unlink()
        self.assertEqual(ph.write_alerts(self.assess(), alerts), 0)
        self.assertEqual(sorted(os.listdir(alerts)),
                         ["health-arrival_backlog-arrival_not_consumed.cleared.json", "notify-failed-run_x.json"])
        self.assertEqual(wrapper.read_text(), '{"wrapper": 1}')
        ph.write_alerts(self.assess(), alerts)  # still healthy: no new file
        self.assertEqual(len(os.listdir(alerts)), 2)

    def test_unknown_check_keeps_an_open_alert(self):
        alerts = self.target_dir / "alerts"
        ph.write_alerts(self.stop_report(), alerts)
        for path in (self.runtime / "arrival").iterdir():
            path.write_text("{bad")
        ph.write_alerts(self.assess(), alerts)
        self.assertIn("health-arrival_backlog-arrival_not_consumed.json", os.listdir(alerts))

    def test_alerts_dir_unsafe(self):
        report = self.stop_report()
        real = self.base / "elsewhere"
        real.mkdir(mode=0o700)
        link = self.base / "linked"
        link.symlink_to(real)
        with self.assertRaises(ph.HealthError) as raised:
            ph.write_alerts(report, link)
        self.assertEqual(raised.exception.code, "alerts_unsafe")
        self.assertEqual(os.listdir(real), [])
        loose = self.target_dir / "alerts" / "health-arrival_backlog-arrival_not_consumed.json"
        loose.write_text("{}")
        os.chmod(loose, 0o644)
        with self.assertRaises(ph.HealthError) as raised:
            ph.write_alerts(report, self.target_dir / "alerts")
        self.assertEqual(raised.exception.code, "alerts_unsafe")
        self.assertEqual(loose.read_text(), "{}")
        os.chmod(self.target_dir / "alerts", 0o755)
        loose.unlink()
        with self.assertRaises(ph.HealthError):
            ph.write_alerts(report, self.target_dir / "alerts")


class CliTests(Base):
    def run_cli(self, *extra, target="t"):
        return self.main(*extra, target=target)

    def run_subprocess(self, *extra, target="t"):
        cmd = [sys.executable, "-B", "-m", "campaign_tool.records", "pipeline-health", "--runtime-dir", str(self.runtime),
               "--target", target, "--root", str(self.root), "--mail-store", str(self.store),
               "--now", NOW.isoformat(), *extra]
        return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=120)

    def test_cli(self):
        done = self.run_cli()
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "Run health: ok - last run 60 min ago, 0 waiting"))
        self.write_stamp(NOW - timedelta(minutes=40))
        done = self.run_cli("--json", "--record-alerts")
        self.assertEqual(done.returncode, 3)
        data = json.loads(done.stdout)
        self.assertEqual((data["state"], data["alerts_written"]), ("stop", 1))
        self.assertEqual(os.listdir(self.target_dir / "alerts"), ["health-arrival_backlog-arrival_not_consumed.json"])

    def test_cli_disk_thresholds_default_and_override(self):
        self.free_gib = 60
        done = self.run_cli("--json")
        self.assertEqual(done.returncode, 1, done.stderr)
        item = self.check(json.loads(done.stdout), "disk")
        self.assertEqual((item["state"], item["reason_code"], item["free_gib"], item["path_role"]),
                         ("warn", "disk_low", 60.0, "runtime"))
        self.assertNotIn(str(self.base), done.stdout)
        done = self.run_cli()
        self.assertEqual(done.stdout.strip(), "Run health: WARNING - disk_low (runtime, 60.0 GiB free)")
        self.assertEqual(self.run_cli("--disk-warn-gib", "50").returncode, 0)  # lowered warn limit: 60 GiB is fine
        self.assertEqual(self.run_cli("--disk-stop-gib", "59").returncode, 1)
        stopped = self.run_cli("--disk-stop-gib", "61", "--disk-warn-gib", "70")
        self.assertEqual((stopped.returncode, stopped.stdout.strip()),
                         (3, "Run health: STOPPED - disk_critical (runtime, 60.0 GiB free)"))

    def test_cli_refuses_invalid_disk_limits_before_any_read_or_write(self):
        self.write_stamp(NOW - timedelta(minutes=40))
        self.statvfs.reset_mock()
        for args in (("--disk-warn-gib", "0"), ("--disk-warn-gib", "10001"), ("--disk-stop-gib", "0"),
                     ("--disk-stop-gib", "-5"), ("--disk-stop-gib", "100"), ("--disk-warn-gib", "20"),  # warn <= default stop 30
                     ("--disk-warn-gib", "50", "--disk-stop-gib", "50"), ("--disk-warn-gib", "50", "--disk-stop-gib", "60"),
                     ("--disk-warn-gib", "20000", "--disk-stop-gib", "10"), ("--disk-warn-gib", "1.5"), ("--disk-stop-gib", "x")):
            with self.subTest(args=args):
                done = self.run_cli(*args, "--record-alerts")
                self.assertEqual(done.returncode, 2)
                self.assertIn("--disk-", done.stderr)
                self.assertEqual(done.stdout, "")
                self.assertEqual(os.listdir(self.target_dir / "alerts"), [])
        self.statvfs.assert_not_called()
        done = self.run_cli("--disk-warn-gib", "10000", "--disk-stop-gib", "1", "--json")  # inclusive bounds are accepted
        self.assertEqual((done.returncode, self.check(json.loads(done.stdout), "disk")["reason_code"]), (3, "disk_low"))

    def test_cli_mail_poll_limits_preserve_defaults_and_accept_daily_cadence(self):
        self.write_poll(NOW - timedelta(hours=7))
        default = self.run_cli("--json")
        self.assertEqual(default.returncode, 1, default.stderr)
        self.assertEqual(self.check(json.loads(default.stdout), "mail_poll")["state"], "warn")
        daily = self.run_cli("--json", "--mail-poll-warn-hours", "26", "--mail-poll-stop-hours", "50")
        self.assertEqual(daily.returncode, 0, daily.stderr)
        self.assertEqual(self.check(json.loads(daily.stdout), "mail_poll")["state"], "ok")

    def test_cli_refuses_invalid_mail_poll_limits_before_alert_writes(self):
        self.write_stamp(NOW - timedelta(minutes=40))
        for warn, stop in (("0", "50"), ("-1", "50"), ("26", "0"), ("26", "-1"),
                           ("26", "26"), ("50", "26"), ("1.5", "50")):
            with self.subTest(warn=warn, stop=stop):
                done = self.run_cli("--mail-poll-warn-hours", warn, "--mail-poll-stop-hours", stop,
                                    "--record-alerts")
                self.assertEqual(done.returncode, 2)
                self.assertIn("--mail-poll-", done.stderr)
                self.assertEqual(done.stdout, "")
                self.assertEqual(os.listdir(self.target_dir / "alerts"), [])

    def test_cli_bad_target(self):
        for bad in ("..", ".", "a/b", "x" * 65, ""):
            done = self.run_subprocess(target=bad)  # the one real-process dispatch control: independent of disk size
            self.assertEqual(done.returncode, 2, bad)
            self.assertIn("target_invalid", done.stderr)
            self.assertEqual(done.stdout, "")


class AnalysisExportTests(Base):
    """ANALYSIS-HEALTH-1: the analysis lane and the console export age."""

    def setUp(self):
        super().setUp()
        self.lane = self.base / "analysis"
        (self.lane / ".analysis").mkdir(mode=0o700, parents=True)
        self.export = self.base / "export"
        self.run_id = json.loads((self.target_dir / "run-report.json").read_text())["run_id"]

    def touch(self, name, minutes, text=""):
        path = self.lane / ".analysis" / name
        path.write_text(text)
        os.utime(path, ((NOW - timedelta(minutes=minutes)).timestamp(),) * 2)

    def ledger(self, *entries):
        self.touch("analysis-ledger.jsonl", 1, "".join(json.dumps(entry) + "\n" for entry in entries))

    def exported(self, run_id):
        folder = self.export / "exports" / ("20261104T220000Z-" + run_id)
        folder.mkdir(parents=True)
        (folder / "EXPORT.json").write_text(json.dumps({"schema": "records-console-export-v1", "run_id": run_id}))
        (self.export / "current").unlink(missing_ok=True)
        (self.export / "current").symlink_to(Path("exports") / folder.name)

    def item(self, name, **kw):
        report = self.assess(**dict(dict(analysis_root=self.lane, console_export_root=self.export), **kw))
        found = self.check(report, name)
        return found["state"], found["reason_code"]

    def test_unconfigured_is_unknown_and_leaves_the_old_checks_alone(self):
        report = self.assess()
        self.assertEqual((self.check(report, "analysis")["state"], self.check(report, "analysis")["reason_code"]),
                         ("unknown", "analysis_not_configured"))
        self.assertEqual(self.check(report, "console_export")["reason_code"], "console_export_not_configured")
        self.assertEqual((report["state"], ph.status_line(report)), ("ok", "Run health: ok - last run 60 min ago, 0 waiting"))
        self.write_stamp(NOW - timedelta(minutes=31))
        self.assertNotIn("not_configured", ph.status_line(self.assess()))

    def test_lane_off_fresh_and_stale_tick(self):
        self.touch("cycle.log", 600)
        self.assertEqual(self.item("analysis"), ("ok", "lane_off"))
        self.touch("LANE-ON", 600)
        self.assertEqual(self.item("analysis"), ("stop", "analysis_stale"))  # switched on, never cycled since
        self.touch("cycle.log", 5)
        self.assertEqual(self.item("analysis"), ("ok", "analysis_ticking"))
        self.touch("cycle.log", 61)
        self.assertEqual(self.item("analysis"), ("stop", "analysis_stale"))
        self.assertEqual(self.item("analysis", limits=dict(ph.LIMITS, analysis_max_minutes=90)), ("ok", "analysis_ticking"))
        report = self.assess(analysis_root=self.lane)
        self.assertEqual(ph.status_line(report), "Run health: STOPPED - analysis_stale")
        self.assertEqual(self.item("analysis", analysis_root=self.base / "missing"), ("unknown", "analysis_unreadable"))

    def test_error_line_warns(self):
        self.touch("LANE-ON", 600)
        self.touch("cycle.log", 5)
        self.ledger({"kind": "tick", "agency": "a", "outcome": "ERROR FAILED f3"}, {"kind": "tick", "agency": "b", "outcome": "UNCHANGED"})
        self.assertEqual(self.item("analysis"), ("warn", "analysis_error"))
        self.ledger({"kind": "tick", "agency": "a", "outcome": "ERROR FAILED f3"}, {"kind": "tick", "agency": "a", "outcome": "ADVANCED x→y"},
                    {"kind": "tick", "agency": "b", "outcome": "ERROR LOCKED"})
        self.assertEqual(self.item("analysis"), ("ok", "analysis_ticking"))
        self.ledger({"kind": "receive", "run_id": "run_x", "outcome": "EXPORT_INVALID"})
        self.assertEqual(self.item("analysis"), ("warn", "analysis_error"))
        self.touch("analysis-ledger.jsonl", 1, "{cut\n")
        self.assertEqual(self.item("analysis"), ("unknown", "analysis_unreadable"))

    def test_pending_sync_warns_until_the_pair_syncs(self):
        self.touch("LANE-ON", 600)
        self.touch("cycle.log", 5)  # run-analysis.sh refreshes cycle.log even when analysis-sync exits 1
        alerts, pair = self.target_dir / "alerts", {"kind": "sync", "run_id": "run_x", "agency_id": "AG-1"}
        missing = [dict(pair, outcome="PROFILE_MISSING")] + [{"kind": "tick", "agency": "a", "outcome": "UNCHANGED"}] * 6000
        self.ledger(*missing)  # logged once, then followed by far more than 256 KiB of healthy ticks
        self.assertEqual(self.item("analysis"), ("warn", "analysis_error"))
        self.assertEqual(ph.write_alerts(self.assess(analysis_root=self.lane), alerts), 1)
        self.assertEqual(os.listdir(alerts), ["health-analysis-analysis_error.json"])
        self.ledger(*missing, dict(pair, outcome="SYNCED"))
        self.assertEqual(self.item("analysis"), ("ok", "analysis_ticking"))
        self.assertEqual(ph.write_alerts(self.assess(analysis_root=self.lane), alerts), 0)
        self.assertEqual(os.listdir(alerts), ["health-analysis-analysis_error.cleared.json"])

    def test_export_matching_pending_and_behind(self):
        self.assertEqual(self.item("console_export"), ("stop", "console_export_stale"))  # no export at all
        self.exported(self.run_id)
        self.assertEqual(self.item("console_export"), ("ok", "console_export_current"))
        self.exported("run_20261103T000000Z_00000000")
        self.assertEqual(self.item("console_export"), ("stop", "console_export_stale"))
        self.assertEqual(self.item("console_export", now=NOW - timedelta(minutes=46)), ("ok", "console_export_pending"))
        (self.export / "current").unlink()
        (self.export / "current").mkdir()
        (self.export / "current" / "EXPORT.json").write_text("[]")
        self.assertEqual(self.item("console_export"), ("unknown", "console_export_unreadable"))

    def test_alerts_and_cli(self):
        self.touch("LANE-ON", 600)
        self.touch("cycle.log", 120)
        alerts = self.target_dir / "alerts"
        report = self.assess(analysis_root=self.lane, console_export_root=self.export)
        self.assertEqual(ph.write_alerts(report, alerts), 2)
        self.assertEqual(sorted(os.listdir(alerts)), ["health-analysis-analysis_stale.json",
                                                      "health-console_export-console_export_stale.json"])
        (self.lane / ".analysis" / "LANE-ON").unlink()
        self.exported(self.run_id)
        self.assertEqual(ph.write_alerts(self.assess(analysis_root=self.lane, console_export_root=self.export), alerts), 0)
        self.assertEqual(sorted(os.listdir(alerts)), ["health-analysis-analysis_stale.cleared.json",
                                                      "health-console_export-console_export_stale.cleared.json"])
        done = self.cli("--json", "--analysis-root", str(self.lane), "--console-export-root", str(self.export))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([self.check(json.loads(done.stdout), name)["reason_code"] for name in ("analysis", "console_export")],
                         ["lane_off", "console_export_current"])
        self.assertEqual(self.cli("--analysis-max-minutes", "0").returncode, 2)

    def cli(self, *extra):
        return self.main(*extra)


if __name__ == "__main__":
    unittest.main()
