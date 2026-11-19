"""GET /health over synthetic runtime data, not a live console or pipeline run."""
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import timedelta
from pathlib import Path
from unittest import mock

from campaign_tool.records import admin_panel, pipeline_health as ph
from tests.records.test_console_model import RUN
from tests.records.test_console_snapshot import Panel
from tests.records.test_pipeline_health import NOW, report_for


class HealthPage(Panel):
    def setUp(self):
        super().setUp()
        self.target = self.tmp / "runtime" / "target"
        self.alerts = self.target / "alerts"
        self.alerts.mkdir(parents=True)
        self.report_path = str(self.target / "run-report.json")

    def start_health(self, *, export=False, system=False, backups=None):
        server = admin_panel.build_server(project=None if export else str(self.live),
                                          export_root=str(self.root) if export else None,
                                          run_id="auto" if export else "run_1", actor="operator", system=system,
                                          as_of="2026-11-12", run_report_path=self.report_path, backup_alerts_dir=backups)
        self.server = server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))

    # Owner: the GET composition. Fails if the engine's optional-check roll-up is copied,
    # a future check is omitted, or one unreadable source hides the others.
    def test_health_verdict_and_failed_source_isolation(self):
        newest = report_for(NOW + timedelta(days=7))
        (self.target / "reports").mkdir()
        (self.target / "reports" / (newest["run_id"] + ".json")).write_text(json.dumps(newest))

        def date_export(dest):
            path = dest / "EXPORT.json"
            path.write_text(json.dumps(dict(json.loads(path.read_text()), exported_at="2026-11-11T00:00:00Z")))

        self.make_export(mutate=date_export)
        self.start_health(export=True, system=True)
        real_open = os.open

        def refuse_alerts(path, flags, *args, **kwargs):
            if path == self.alerts:
                raise PermissionError("alert-error-canary")
            return real_open(path, flags, *args, **kwargs)

        def usage(path):
            if path == self.target.parent:
                raise OSError("disk-error-canary")
            self.assertEqual(Path(path).parent.parent, self.root / "exports")
            self.assertEqual(Path(path).name, "processing-v1")
            return shutil._ntuple_diskusage(120 * ph.GIB, 42 * ph.GIB, 78 * ph.GIB)

        checks = [dict(check="arrival_backlog", state="ok", reason_code="none_waiting", since=None),
                  dict(check="analysis", state="unknown", reason_code="analysis_not_configured", since=None),
                  dict(check="notify", state="warn", reason_code="notify_failed", since=None),
                  dict(check="later_check", state="stop", reason_code="later_reason", since=NOW.isoformat(),
                       free_gib=19.5, path_role="runtime", body="check-body-canary")]
        with (mock.patch.object(os, "open", side_effect=refuse_alerts),
              mock.patch.object(shutil, "disk_usage", side_effect=usage),
              mock.patch.object(ph, "assess") as assess):
            for count, state in ((4, "stop"), (3, "warn"), (2, "unknown"), (1, "ok"), (0, "unknown")):
                with self.subTest(state=state, count=count):
                    assess.return_value = dict(state="ok", checks=checks[:count], waiting=0)
                    html = self.get("/health")
                    self.assertIn(f"Verdict: {state}", html)
                    self.assertIn("Open alerts: unavailable: the alerts directory cannot be read safely.", html)
                    self.assertIn("Runtime: unavailable:", html)
                    self.assertIn("Mounted export: 78.0 GiB free of 120.0 GiB", html)
                    self.assertRegex(html, rf"Console export: run {RUN}, .+? \(24 h ago\)")
                    self.assertIn("Stale: a newer run has completed.", html)
                    self.assertIn(newest["run_id"], html)
            assess.side_effect = ValueError("assessment-error-canary")
            failed = self.get("/health")
            self.assertIn("Health checks: unavailable:", failed)
            self.assertIn("Verdict: unknown", failed)
            self.assertIn("Mounted export: 78.0 GiB free", failed)
            self.assertNotIn("assessment-error-canary", failed)
            assess.side_effect = None
            assess.return_value = dict(state="ok", checks=checks, waiting=0)
            html = self.get("/health")
        ordered = ("Verdict:", "Pipeline checks", "Open alerts", "Backups", "Free disk space", "Console export freshness")
        self.assertEqual(re.findall(r"Verdict:|(?<=<h2>)[^<]+", html.split('<main', 1)[1]), list(ordered))
        self.assertIn('<a href="/health" aria-current="page">System health</a>', html)
        for fragment in ("Later check: stop", "No description is available for this reason (later_reason).",
                         "Since: 2026-11-04 14:30 PST", "Free: 19.5 GiB", "Filesystem: Runtime",
                         "Waiting arrivals: 0", '<a href="/jobs">Jobs</a>', '<a href="/runs">Run history</a>'):
            self.assertIn(fragment, html)
        for canary in ("alert-error-canary", "disk-error-canary", "check-body-canary", str(self.tmp)):
            self.assertNotIn(canary, html)

    # Owner: the page's alert metadata projection. A cleared, misnamed, nonregular,
    # invalid or symlinked entry must not become an open alert or leak its payload.
    def test_health_open_alert_timestamps_and_safe_projection(self):
        first, last = "2026-07-01T22:00:00Z", "2026-11-04T22:30:00+00:00"

        def put(check, reason, state, name=None, **extra):
            path = self.alerts / (name or f"health-{check}-{reason}.json")
            data = dict(schema=ph.ALERT_SCHEMA, check=check, reason_code=reason, state=state,
                        first_seen=first, last_seen=last, body="alert-body-canary")
            path.write_text(json.dumps(dict(data, **extra)))
            return path

        valid = put("mail_poll", "mail_poll_stale", "stop")
        put("notify", "notify_failed", "warn")
        put("notify", "notify_failed", "cleared", "health-notify-notify_failed.cleared.json")
        put("analysis", "analysis_stale", "stop", "health-schedule-missed_slot.json")
        put("notify", "notify_failed", "warn", "health-notify-notify_failed.json\n")
        put("run_failures", "latest_run_failed", "warn", last_seen="2026-11-04T22:00:00")
        (self.alerts / "health-mail_poll-mail_poll_unreadable.json").symlink_to(valid)
        os.mkfifo(self.alerts / "health-analysis-analysis_stale.json")
        before = {path: path.read_bytes() for path in self.alerts.iterdir() if path.is_file()}
        self.start_health()
        with (mock.patch.object(shutil, "disk_usage", side_effect=AssertionError("disk disabled")),
              mock.patch.object(ph, "_disk", side_effect=AssertionError("disk check disabled")),
              mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess"))):
            html = self.get("/health")
        alerts = html.split("Open alerts</h2>", 1)[1].split("<h2>", 1)[0]
        self.assertEqual(alerts.count("First seen:"), 2)
        for fragment in ("Mail poll: stop", "Notify: warn", "First seen: 2026-07-01 15:00 PDT",
                         "Last seen: 2026-11-04 14:30 PST", "6 entries not shown"):
            self.assertIn(fragment, alerts)
        for fragment in ("alert-body-canary", "analysis_stale", str(self.tmp), "<form"):
            self.assertNotIn(fragment, html)
        self.assertIn("Disk checks are disabled", html)
        self.assertIn("Console export: unavailable: this console is serving live project data.", html)
        self.assertEqual({path: path.read_bytes() for path in self.alerts.iterdir() if path.is_file()}, before)

    # Owner: the freshness comparison as served, including the real engine's check from the pin.
    # Fails if an absent history is called current or the engine still treats a pinned export as unconfigured.
    def test_health_compares_the_pin_only_to_usable_run_evidence(self):
        def date_export(dest):
            path = dest / "EXPORT.json"
            path.write_text(json.dumps(dict(json.loads(path.read_text()), exported_at="2026-11-11T23:00:00Z")))

        self.make_export(mutate=date_export)
        self.start_health(export=True)
        report = dict(report_for(NOW + timedelta(days=7)), run_id=RUN)
        Path(self.report_path).write_text(json.dumps(report))
        html = self.get("/health")
        self.assertIn("Console export: ok", html)
        self.assertIn("Run comparison: unavailable:", html)
        self.assertNotIn("Current:", html)
        archive = self.target / "reports"
        archive.mkdir()
        (archive / (RUN + ".json")).write_text(json.dumps(report))
        self.assertIn("Current: the export matches that run.", self.get("/health"))

    def backup_dir(self, name="backup-alerts"):
        path = self.tmp / name
        path.mkdir(mode=0o700)
        return path

    def backup_section(self, html):
        return html.split("Backups</h2>", 1)[1].split("<h2>", 1)[0]

    def snapshot(self, directory):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}, directory.stat().st_mtime_ns

    # Owner: the Backups section. Fails if "no alarm" is shown as a passed or existing backup, if the scan reads
    # the backup set instead of the alarms, if an unreadable directory looks empty, or if a set path or digest leaks.
    def test_backup_section_reports_open_alarms_without_certifying_backup(self):
        def alarm(directory, code, now):  # the producer's own writer, as `records backup-check` calls it
            ph.write_alerts({"now": now, "checks": [{"check": "backup", "state": "stop", "reason_code": code}]}, directory)

        self.start_health()
        self.assertIn("Backups: not configured on this console.", self.backup_section(self.get("/health")))
        empty = self.backup_dir("empty")
        os.utime(empty, (1793831400, 1793831400))  # 2026-11-04T22:30:00Z
        self.start_health(backups=str(empty))
        section = self.backup_section(self.get("/health"))
        for fragment in ("No open backup alert.", "last changed 2026-11-04 14:30 PST",
                         "This is not evidence that a backup exists or was verified."):
            self.assertIn(fragment, section)
        self.assertNotIn("entries not shown", section)
        self.assertEqual(os.stat(empty).st_mtime, 1793831400)

        stale = self.backup_dir("stale")
        alarm(stale, "backup_stale", "2026-07-01T22:00:00+00:00")
        alarm(stale, "backup_stale", "2026-11-04T22:30:00+00:00")
        failed = self.backup_dir("failed")
        (failed / "health-backup-backup_verify_failed.json").write_text(json.dumps(dict(
            schema=ph.ALERT_SCHEMA, check="backup", reason_code="backup_verify_failed", state="stop",
            first_seen="2026-11-04T22:00:00Z", last_seen="2026-11-04T22:30:00Z", set="/srv/backups/set-path-canary",
            manifest_sha256="d1gest" * 10)))
        (failed / "notes.txt").write_text("not-an-alert-canary")
        for directory, sentence, first in ((stale, "The newest recorded backup is older than the allowed age.", "2026-07-01 15:00 PDT"),
                                           (failed, "The newest recorded backup set failed verification.", "2026-11-04 14:00 PST")):
            with self.subTest(directory=directory.name):
                before = self.snapshot(directory)
                self.start_health(backups=str(directory))
                html = self.get("/health")
                section = self.backup_section(html)
                for fragment in ("Backup: stop", sentence, f"First seen: {first}", "Last seen: 2026-11-04 14:30 PST"):
                    self.assertIn(fragment, section)
                self.assertEqual(section.count("First seen:"), 1)
                self.assertEqual("1 entries not shown" in section, directory is failed)  # the non-alert file is counted
                self.assertNotRegex(section, r"(?i)no open backup alert|backup (passed|is ok)")
                for canary in ("set-path-canary", "d1gest", "not-an-alert-canary", str(self.tmp), "<form"):
                    self.assertNotIn(canary, html)
                self.assertEqual(self.snapshot(directory), before)

        real_open, target = os.open, str(failed)

        def refuse(path, *args, **kwargs):
            if str(path) == target:
                raise PermissionError("backup-dir-canary")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(os, "open", side_effect=refuse):
            html = self.get("/health")
        self.assertIn("Backups: unavailable: the backup alerts directory cannot be read safely.", html)
        self.assertNotIn("No open backup alert", html)
        self.assertNotIn("backup-dir-canary", html)

    # Owner: option validation. Fails if a relative, missing, symlinked or console-write-overlapping directory
    # starts a server, or if the refusal comes after bind.
    def test_backup_alert_option_refuses_invalid_roots(self):
        good = self.backup_dir()
        server = admin_panel.build_server(project=str(self.live), run_id="run_1", actor="operator", backup_alerts_dir=str(good))
        self.addCleanup(server.server_close)
        self.assertEqual(server.backup_alerts_dir, str(good))
        link = self.tmp / "link"
        link.symlink_to(good)
        inside = self.live / "backup-alerts"
        inside.mkdir()
        file = self.tmp / "file"
        file.write_text("x")
        for bad in ("backup-alerts", str(self.tmp / "missing"), str(link), str(file), str(inside), str(self.live),
                    str(self.live.parent)):
            with self.subTest(bad=bad), mock.patch("http.server.HTTPServer", side_effect=AssertionError("bound")):
                with self.assertRaises(admin_panel.PanelError) as raised:
                    admin_panel.build_server(project=str(self.live), run_id="run_1", actor="operator", backup_alerts_dir=bad)
                self.assertEqual(raised.exception.code, "backup_alerts_dir_invalid")
        self.make_export()
        (self.root / "inner").mkdir()
        with self.assertRaises(admin_panel.PanelError) as raised:
            admin_panel.build_server(export_root=str(self.root), run_id="auto", actor="operator",
                                     backup_alerts_dir=str(self.root / "inner"))
        self.assertEqual(raised.exception.code, "backup_alerts_dir_invalid")
        with mock.patch("sys.stderr") as err:
            self.assertEqual(admin_panel.main(["--project", str(self.live), "--run-id", "run_1", "--actor", "operator",
                                               "--backup-alerts-dir", "relative"]), 2)
        self.assertIn("backup_alerts_dir_invalid", "".join(call.args[0] for call in err.write.call_args_list))
