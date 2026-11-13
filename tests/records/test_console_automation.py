"""Automation page acceptance on synthetic metadata; no real records, network or units."""
import json
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from campaign_tool.records import admin_panel as ap, console_automation as ca
from tests.records.test_console_jobs import fake, timer, service
from tests.records import test_console_manual_events as http_fixture
from tests.records.test_admin_panel import Base as HttpBase
from tests.records.test_pipeline_health import NOW, report_for
from tests.records.test_console_snapshot import Panel, tree_state, OpenSpy


class Automation(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "run-report.json"
        self.data = report_for(NOW - timedelta(minutes=5))
        self.put()
        self.runner = fake({"records-run.timer": timer(), "records-run.service": service()})

    def put(self):
        self.path.write_text(json.dumps(self.data))

    def model(self):
        return ca.load(self.path, units=["records-run.timer", "records-health.timer"], runner=self.runner, now=NOW)

    def test_report_and_units_match_sources_without_implying_substeps(self):
        self.data["stages"][4].update(status="warn", error_code="EXTRACT_NOT_ACCEPTED",
                                      counts={"input": 3, "output": 1, "failed": 2, "skipped": 0})
        self.data["overall_status"] = "warn"
        self.data["needs_g"] = [{"stage": "extract", "code": "EXTRACT_NOT_ACCEPTED", "count": 2, "severity": "action"}]
        self.put()
        model = self.model()
        self.assertEqual(model["report"]["data"]["stages"], self.data["stages"])
        self.assertEqual([(r["unit"], r["state"]) for r in model["timers"]],
                         [("records-health.timer", "not_installed"), ("records-run.timer", "ok")])
        page = ca.render(model)
        for value in (self.data["run_id"], self.data["finished_at"], "EXTRACT_NOT_ACCEPTED", "(needs G)",
                      "NOT INSTALLED", "2026-11-08", "UNKNOWN (not separately reported)", "semantic acceptance"):
            self.assertIn(value, page)
        for name in ca.UNREPORTED:
            self.assertIn(name, page)
        self.assertTrue(all(call[0] == "show" for call in self.runner.calls))
        self.assertNotIn("<form", page)

    def test_old_success_is_historical_and_exact_age_boundary(self):
        for hours, stale in ((ca.STALE_HOURS, False), (ca.STALE_HOURS + 1, True)):
            self.data = report_for(NOW - timedelta(hours=hours))
            self.put()
            model = self.model()
            self.assertEqual(model["report"]["state"] == "stale", stale)
            self.assertEqual("ok (historical)" in ca.render(model), stale)

    def test_missing_bad_shape_canary_and_future_report_are_unknown(self):
        for raw in ("bad json", "[]", json.dumps(dict(self.data, private_text="CANARY-secret")),
                    json.dumps(report_for(NOW + timedelta(seconds=1))),
                    json.dumps(dict(self.data, finished_at="2026-99-99T00:00:00Z"))):
            self.path.write_text(raw)
            model = self.model()
            self.assertIsNone(model["report"]["data"])
            page = ca.render(model)
            self.assertIn("Run report unavailable", page)
            self.assertNotIn("CANARY-secret", page)
            self.assertIn("records-run.timer", page)  # sources fail independently
        self.path.unlink()
        self.assertIsNone(self.model()["report"]["data"])
        self.assertEqual(ca.report(None, NOW)["code"], "run_report_not_configured")

    def test_no_follow_nonregular_or_oversized_metadata(self):
        self.path.unlink()
        real = self.root / "real.json"
        real.write_text(json.dumps(self.data))
        self.path.symlink_to(real)
        self.assertIsNone(self.model()["report"]["data"])
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertIsNone(self.model()["report"]["data"])
        self.path.unlink()
        self.path.write_bytes(b" " * (ca.pipeline_health.MAX_JSON_BYTES + 1))
        self.assertIsNone(self.model()["report"]["data"])

    def test_open_cleared_and_notify_markers_are_distinguished_without_writes(self):
        alerts = self.root / "alerts"
        alerts.mkdir()
        open_alert = {"schema": ca.pipeline_health.ALERT_SCHEMA, "check": "last_run", "reason_code": "no_recent_run",
                      "state": "stop"}
        (alerts / "health-last_run-no_recent_run.json").write_text(json.dumps(open_alert))
        (alerts / "health-last_run-run_recent.cleared.json").write_text("never read me")
        (alerts / "notify-failed-run_1.json").write_text("CANARY-private-body")
        before = {p.name: p.read_bytes() for p in alerts.iterdir()}
        model = self.model()
        self.assertEqual(model["alarms"], {"state": "read", "rows": [("last_run", "no_recent_run", "stop")],
                                           "notify_markers": 1})
        page = ca.render(model)
        self.assertIn("no_recent_run", page)
        self.assertIn("resolution unknown", page)
        self.assertNotIn("CANARY-private-body", page)
        self.assertEqual(before, {p.name: p.read_bytes() for p in alerts.iterdir()})

    def test_alarm_missing_invalid_symlink_and_cap_never_zero(self):
        self.assertEqual(self.model()["alarms"]["state"], "unknown")
        alerts = self.root / "alerts"
        alerts.mkdir()
        self.assertEqual(self.model()["alarms"]["state"], "read")
        bad = alerts / "health-last_run-no_recent_run.json"
        bad.write_text('{"schema":"wrong","private":"CANARY-secret"}')
        self.assertEqual(self.model()["alarms"]["state"], "unknown")
        bad.unlink()
        bad.symlink_to(self.path)
        self.assertEqual(self.model()["alarms"]["state"], "unknown")
        bad.unlink()
        for index in range(1001):
            (alerts / str(index)).touch()
        self.assertEqual(self.model()["alarms"]["state"], "unknown")
        self.assertNotIn("Saved health alarms: 0", ca.render(self.model()))
        self.assertEqual(ca.alarms(self.root / "historical.json")["state"], "unknown")

    def test_skipped_and_held_exit_are_not_passes(self):
        self.data = report_for(NOW - timedelta(minutes=2), status="fail")
        self.put()
        self.runner = fake({"records-run.timer": timer(),
                            "records-run.service": service(Result="exit-code", ExecMainStatus="73")})
        page = ca.render(self.model())
        self.assertIn("SKIPPED (not a pass)", page)
        self.assertIn("UPSTREAM_FAILED", page)
        self.assertIn("unknown / not run", page)

    def test_ocr_counters_and_no_monitor_are_not_acceptance(self):
        self.data["ocr"] = {"pages_ocr": 2, "pages_conf_ge80": 1, "pages_conf_lt80": 1, "pages_conf_unknown": 0,
                            "pages_reviewed": 0, "awaiting_ocr_images": 3}
        self.put()
        page = ca.render(self.model())
        self.assertIn("pages_ocr=2", page)
        self.assertIn("not proof of acceptance", page)
        self.assertIn("Saved health alarms: UNKNOWN", page)

    def test_service_failure_unknown_and_escape(self):
        runner = fake({"records-run.service": service(ActiveState="failed", Result="exit-code", ExecMainStatus="14"),
                       "records-console.service": service(ActiveState="active", UnitFileState="<script>x</script>")})
        model = ca.load(self.path, units=[], runner=runner, now=NOW)
        self.assertEqual(model["services"][0]["state"], "FAILED")
        page = ca.render(model)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)
        self.assertIn("NOT INSTALLED", page)

    def test_enabled_but_stopped_trigger_does_not_supply_next_opportunity(self):
        runner = fake({"records-run.timer": timer(ActiveState="inactive"), "records-run.service": service()})
        model = ca.load(self.path, units=["records-run.timer"], runner=runner, now=NOW)
        self.assertEqual(model["timers"][0]["state"], "unknown")
        self.assertIn("trigger is not active", ca.render(model))


class AutomationHttp(HttpBase):
    request = http_fixture.ManualEvents.request
    close_server = http_fixture.ManualEvents.close_server

    def test_get_nav_no_system_no_network_post_denied_and_refresh(self):
        path = Path(self.tmp.name) / "run-report.json"
        data = report_for(NOW)
        path.write_text(json.dumps(data))
        server = ap.build_server(self.project, "run_1", actor="Operator", system=False, run_report_path=str(path))
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.origin = "http://127.0.0.1:%d" % server.server_port
        with mock.patch.object(ca.jobs, "system_runner", side_effect=AssertionError("no system")), \
                mock.patch.object(ca.jobs, "github_runs", side_effect=AssertionError("no credentials or network")), \
                mock.patch.object(ca.pipeline_health, "assess", side_effect=AssertionError("no ledger")):
            status, page, headers = self.request("GET", "/automation")
            self.assertEqual(status, 200)
            self.assertIn("UNKNOWN (not a pass)", page)
            self.assertIn(data["run_id"], page)
            self.assertIn('aria-current="page">Automation', page)

            self.assertIn("no-store", str(headers))
            path.write_text("invalid")
            self.assertIn("Run report unavailable", self.request("GET", "/automation")[1])
            self.assertNotEqual(self.request("POST", "/automation", {"csrf_token": server.csrf_token})[0], 200)
            self.assertEqual(self.request("GET", "/automation?ignored=1")[0], 404)
        server.jobs_hooks = {"environ": {}}
        self.assertIn('href="/automation"', self.request("GET", "/jobs")[1])


class AutomationExport(Panel):
    def test_export_mode_without_report_is_unknown_and_nonmutating(self):
        self.make_export()
        self.up()
        before = tree_state(self.root)
        spy = OpenSpy(self.root)
        with mock.patch.object(os, "open", spy), \
                mock.patch.object(ca.jobs, "system_runner", side_effect=AssertionError("no system")):
            page = self.get("/automation")
        self.assertIn("Run report unavailable", page)
        self.assertIn("Saved health alarms: UNKNOWN", page)
        self.assertNotIn("<form", page)
        self.assertEqual(spy.writes, [])
        self.assertEqual(tree_state(self.root), before)
        self.assertEqual(self.request("POST", "/automation", {"csrf_token": self.server.csrf_token})[0], 503)
