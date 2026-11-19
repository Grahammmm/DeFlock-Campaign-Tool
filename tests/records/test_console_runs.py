"""GET /runs: run history over a synthetic <runtime>/<target>/reports directory (DFCON17-02)."""
import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.records import admin_panel as ap, console_page_runs as page, console_runs_model as model
from tests.records.test_console_snapshot import Panel
from tests.records.test_pipeline_health import NOW, report_for

BASE = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
STAGE_CODE = "OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE"


def make(i, kind="ok"):
    """One finished report; i is its finish hour after BASE (and its sequence, so every run id is distinct)."""
    data = report_for(BASE + timedelta(hours=i), "fail" if kind == "fail" else "ok", seq=i)
    if kind == "warn":
        data["stages"][4].update(status="warn", error_code="EXTRACT_NOT_ACCEPTED")
        data["overall_status"] = "warn"
    if kind == "fail":
        data["stages"][0].update(error_code=STAGE_CODE)
        data["mode"], data["trigger"] = "arrival", "arrival"
    return data


class RunHistory(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.target = Path(tmp.name) / "t"
        self.reports = self.target / "reports"
        self.reports.mkdir(parents=True)
        self.path = str(self.target / "run-report.json")

    def put(self, data, name=None):
        (self.reports / (name or data["run_id"] + ".json")).write_text(json.dumps(data))

    # Owner: the list boundary. Fails if the cap is applied before validation, a rejected file takes a slot or is
    # dropped uncounted, rows are not newest-first, a stage chip or code is lost, or the time is not Pacific.
    def test_runs_keeps_thirty_valid_and_names_rejected_reports(self):
        for i in range(31):
            self.put(make(i, {5: "warn", 30: "fail"}.get(i, "ok")))
        self.put(make(40), name="run_20261101T000000Z_deadbeef.json")  # filename and report id disagree
        (self.reports / (make(41)["run_id"] + ".json")).write_text("{not json")  # invalid
        self.put(report_for(datetime(2099, 1, 1, tzinfo=timezone.utc), seq=99))  # future
        (self.reports / "notes.json").write_text("{}")  # not an archive name: ignored, not counted
        history = model.load_history(self.path, NOW)
        self.assertEqual((history["state"], len(history["reports"]), history["skipped"]), ("ok", 31, 3))
        html = page.render(history)
        rows = re.findall(r"<tbody>(.*)</tbody>", html)[0].split("</tr>")[:-1]
        self.assertEqual(len(rows), 30)
        ids = re.findall(r'/run\?id=(run_\w+)', html)
        self.assertEqual(ids, [make(i)["run_id"] for i in range(30, 0, -1)])  # newest first; the oldest is cut
        self.assertIn("Older valid runs not listed: 1.", html)
        self.assertIn("Reports not shown: 3 (invalid", html)
        fail = rows[0]  # the arrival run that failed: 2026-11-02 18:00Z finish, started 18:00-60s = 10:59 PST
        for fragment in ("<td>2026-11-02 09:59 PST</td><td>arrival</td><td>fail</td><td>1m 0s</td>", "sync: fail</span>",
                         "adoption: skipped</span>", "<br>".join(["sync " + STAGE_CODE] + ["%s UPSTREAM_FAILED" % n
                                                                                       for n in model.run_report.STAGE_ORDER[1:]])):
            self.assertIn(fragment, fail)
        warn = rows[30 - 5]
        self.assertIn("extract: warn</span>", warn)
        self.assertIn("extract EXTRACT_NOT_ACCEPTED</td>", warn)
        self.assertIn("<td>unknown</td><td>warn</td>", warn)  # legacy mode "scheduled" without a trigger is not guessed
        self.assertNotIn(str(self.target), html)

    # Owner: location/status. Fails if "not configured", "unavailable" and "no runs" are ever confused.
    def test_runs_distinguishes_unconfigured_unavailable_and_empty(self):
        for path in (None, "", str(self.target / "other.json")):
            history = model.load_history(path, NOW)
            self.assertEqual(history["state"], "not_configured")
            self.assertIn("Run history: not configured on this console", page.render(history))
            self.assertNotIn("no completed runs", page.render(history))
        empty = page.render(model.load_history(self.path, NOW))
        self.assertIn("Run history: no completed runs yet.", empty)
        self.assertNotIn("<table", empty)
        self.put(make(1), name="run_20261101T000000Z_deadbeef.json")  # only a rejected file: not "empty"
        self.assertNotIn("no completed runs", page.render(model.load_history(self.path, NOW)))
        elsewhere = self.reports.parent / "elsewhere"
        self.reports.rename(elsewhere)
        for code, setup in (("reports_missing", lambda: None), ("reports_unreadable", lambda: self.reports.symlink_to(elsewhere))):
            setup()
            history = model.load_history(self.path, NOW)
            self.assertEqual((history["state"], history["code"]), ("unavailable", code))
            self.assertIn("Run history: unavailable (%s)" % code, page.render(history))
            self.assertNotIn("no completed runs", page.render(history))
        self.reports.unlink()
        self.reports.mkdir()
        with mock.patch.object(os, "scandir", side_effect=PermissionError):
            self.assertEqual(model.load_history(self.path, NOW)["code"], "reports_unreadable")
        for i in range(3):
            self.put(make(i))
        with mock.patch.object(model, "FILE_LIMIT", 2):
            history = model.load_history(self.path, NOW)
        self.assertEqual((history["state"], history["code"], history["reports"]), ("unavailable", "history_limit", []))
        self.assertNotIn("<tr>", page.render(history))


def fetch(server, path):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


class RunHistoryPages(Panel):
    # Owner: both console modes share one reader. Fails if the export (pinned) and live panels render different rows,
    # the page is not registered in More, or it answers a query string.
    def test_export_and_live_modes_render_the_same_rows(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reports = Path(tmp.name) / "t" / "reports"
        reports.mkdir(parents=True)
        for i, kind in enumerate(("ok", "warn", "fail")):
            data = make(i, kind)
            (reports / (data["run_id"] + ".json")).write_text(json.dumps(data))
        (reports / "run_20261101T000000Z_deadbeef.json").write_text("[]")
        report = str(reports.parent / "run-report.json")
        live = ap.build_server(str(self.live), "run_1", actor="operator", system=False, run_report_path=report)
        thread = threading.Thread(target=live.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (live.shutdown(), live.server_close(), thread.join()))
        self.make_export()
        self.up(run_report_path=report)
        pages = []
        for server in (live, self.server):
            status, html = fetch(server, "/runs")
            self.assertEqual(status, 200)
            self.assertEqual(fetch(server, "/runs?x=1")[0], 404)
            self.assertIn('<a href="/runs" aria-current="page">Run history</a>', html)
            pages.append(re.findall(r"<table.*?</table>|Reports not shown: \d+", html, re.S))
        self.assertEqual(pages[0][0].count("<tr>"), 4)  # header row + 3 valid rows
        self.assertEqual(pages[0], pages[1])
        self.assertIn("Reports not shown: 1", pages[0])
        unconfigured = ap.build_server(str(self.live), "run_1", actor="operator", system=False)
        thread = threading.Thread(target=unconfigured.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (unconfigured.shutdown(), unconfigured.server_close(), thread.join()))
        self.assertIn("Run history: not configured on this console", fetch(unconfigured, "/runs")[1])
