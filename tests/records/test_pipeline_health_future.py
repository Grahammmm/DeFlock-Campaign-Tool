"""Future-dated run reports are not evidence of completed processing (RF1405-4); synthetic runtime dirs only."""
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys
import unittest

from campaign_tool.records import console_oversight as co, pipeline_health as ph
from tests.records import test_console_oversight as tco
from tests.records import test_pipeline_health as tph

NOW = tph.NOW
YEAR = timedelta(days=365)


def drop_reports(base):
    for path in (base.target_dir / "reports").iterdir():
        path.unlink()
    (base.target_dir / "run-report.json").unlink()


class HealthFuture(tph.Base):
    def run_cli(self, *extra):
        cmd = [sys.executable, "-B", "-m", "campaign_tool.records", "pipeline-health", "--runtime-dir",
               str(self.runtime), "--target", "t", "--root", str(self.root), "--mail-store", str(self.store),
               "--now", NOW.isoformat(), *extra]
        return subprocess.run(cmd, cwd=tph.REPO, capture_output=True, text=True, timeout=120)

    def test_future_current_report_stops_health_and_cli(self):
        for delta in (timedelta(seconds=1), YEAR):  # started before / after now
            with self.subTest(delta=str(delta)):
                self.write_report(NOW + delta, seq=9)
                report = self.assess()
                item = self.check(report, "last_run")
                self.assertEqual((item["state"], item["reason_code"]), ("stop", "run_report_future"))
                self.assertEqual((report["state"], report["exit_code"], report["last_run_finished_at"]), ("stop", 3, None))
                line = ph.status_line(report)
                self.assertIn("run_report_future", line)
                self.assertNotIn("last run", line)
                plain = self.run_cli()
                self.assertEqual(plain.returncode, 3, plain.stderr)
                self.assertIn("run_report_future", plain.stdout)
                self.assertNotIn("last run", plain.stdout)
                data = self.run_cli("--json")
                self.assertEqual(data.returncode, 3, data.stderr)
                self.assertEqual(json.loads(data.stdout)["state"], "stop")

    def test_finish_at_now_is_admitted(self):
        other = timezone(timedelta(hours=-8))
        for finished, clock in ((NOW, NOW), (NOW, NOW.astimezone(other)), (NOW - timedelta(seconds=1), NOW)):
            with self.subTest(finished=finished.isoformat(), clock=clock.isoformat()):
                self.write_report(finished, seq=8)
                item = self.check(self.assess(now=clock), "last_run")
                self.assertEqual((item["state"], item["reason_code"]), ("ok", "run_recent"))
        self.write_report(NOW + timedelta(seconds=1), seq=8)
        self.assertEqual(self.check(self.assess(now=NOW.astimezone(other)), "last_run")["reason_code"],
                         "run_report_future")

    def test_future_success_cannot_reset_failures(self):
        self.write_report(NOW - timedelta(minutes=10), "fail", seq=1)
        self.write_report(NOW + timedelta(hours=1), seq=2, latest=False)
        item = self.check(self.assess(), "run_failures")
        self.assertEqual((item["state"], item["reason_code"]), ("warn", "latest_run_failed"))
        for index in range(3):
            self.write_report(NOW - timedelta(minutes=30 - 10 * index), "fail", seq=3 + index)
        self.write_report(NOW + YEAR, seq=7, latest=False)
        item = self.check(self.assess(), "run_failures")
        self.assertEqual((item["state"], item["reason_code"]), ("stop", "repeated_failures"))
        drop_reports(self)  # future-only history proves nothing
        self.write_report(NOW + timedelta(hours=1), seq=2, latest=False)
        item = self.check(self.assess(), "run_failures")
        self.assertEqual((item["state"], item["reason_code"]), ("unknown", "run_report_missing"))

    def test_future_success_cannot_suppress_notify_alert(self):
        alert = self.target_dir / "alerts" / "notify-failed-run_x.json"
        alert.write_text("{}")
        os.utime(alert, ((NOW - timedelta(minutes=5)).timestamp(),) * 2)
        self.write_report(NOW + timedelta(hours=1), seq=2, latest=False)
        item = self.check(self.assess(), "notify")
        self.assertEqual((item["state"], item["reason_code"]), ("warn", "notify_failed"))
        self.write_report(NOW - timedelta(minutes=1), seq=3)  # a real, later, successful run
        self.assertEqual(self.check(self.assess(), "notify")["state"], "ok")

    def test_future_alert_repeats_then_clears(self):
        alerts = self.target_dir / "alerts"
        self.write_stamp(NOW - timedelta(minutes=45))
        self.write_report(NOW + timedelta(hours=1), seq=2)
        self.assertEqual(ph.write_alerts(self.assess(), alerts), 2)
        self.assertEqual(ph.write_alerts(self.assess(), alerts), 2)
        name = "health-last_run-run_report_future.json"
        data = json.loads((alerts / name).read_text())
        self.assertEqual((data["count"], data["state"], data["reason_code"]), (2, "stop", "run_report_future"))
        self.assertEqual(sorted(data), ["check", "count", "first_seen", "last_seen", "reason_code", "schema", "state"])
        text = (alerts / name).read_text()
        for private in (str(self.base), "run_2026", "finished_at"):
            self.assertNotIn(private, text)
        self.write_report(NOW - timedelta(minutes=1), seq=3)  # eligible replacement of the current report
        self.assertEqual(ph.write_alerts(self.assess(), alerts), 1)
        self.assertEqual(sorted(os.listdir(alerts)), ["health-arrival_backlog-arrival_not_consumed.json",
                                                      "health-last_run-run_report_future.cleared.json"])

    def test_assess_future_evidence_is_read_only(self):
        self.write_report(NOW + timedelta(hours=1), seq=2)
        self.write_report(NOW + YEAR, seq=3, latest=False)

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
        after = snapshot()
        self.assertEqual([k for k in set(before) | set(after) if before.get(k) != after.get(k)], [])


class OversightFuture(tco.Base):
    def put_future(self, delta, seq, current):
        data = tph.report_for(tco.NOW + delta, seq=seq)
        extract = next(s for s in data["stages"] if s["name"] == "extract")
        extract["counts"] = {"input": 9, "output": 8, "failed": 0, "skipped": 1}
        extract["outcomes"] = [{"code": "PRESERVE_PENDING", "count": 7}, {"code": "EXTRACT_OK", "count": 2}]
        text = json.dumps(data)
        (self.target / "reports" / (data["run_id"] + ".json")).write_text(text)
        if current:
            (self.target / "run-report.json").write_text(text)

    def test_future_current_oversight_is_unavailable_not_stale_success(self):
        self.put_future(timedelta(hours=1), 5, current=True)
        m = self.model()
        self.assertEqual(m["runs"], {"state": "unavailable", "code": "run_report_future"})
        self.assertEqual((m["timeline"]["state"], m["links"]["state"], m["queue"]["state"], m["health"]["state"]),
                         ("ok", "ok", "ok", "ok"))
        page = co.render_oversight(m)
        self.assertIn("run_report_future", page)  # the health check's fixed reason
        self.assertIn("in the latest run: unknown.", page)
        self.assertNotIn("in the latest run: 7.", page)
        self.assertNotIn("in the latest run: 2.", page)  # no fallback to the older eligible report
        self.assertIn("R-1", page)

    def test_future_history_ignored_with_valid_current(self):
        self.put_future(timedelta(hours=3), 6, current=False)
        m = self.model()
        self.assertEqual(len(m["runs"]["runs"]), 1)
        run = m["runs"]["runs"][0]
        self.assertEqual((run["items_in"], run["items_processed"], run["preserve_pending"]), (5, 4, 2))
        self.assertEqual(m["runs"]["latest"]["preserve_pending"], 2)
        self.assertIn("in the latest run: 2.", co.render_oversight(m))


if __name__ == "__main__":
    unittest.main()
