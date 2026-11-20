"""GET /stages through the actual console registry, using only synthetic archives."""
import json
import re
import threading
from pathlib import Path
from unittest import mock

from campaign_tool.records import admin_panel, console_pages, console_runs_model, golive_stages
from tests.records.test_admin_panel import AS_OF, RUN, Base, Http
from tests.records.test_golive_stages import APPROVAL, CLOCK, FIXTURE


class StagePage(Base):
    def setUp(self):
        super().setUp()
        self.archive = Path(self.tmp.name) / "target" / "reports"
        self.archive.mkdir(parents=True)
        server = admin_panel.build_server(self.project, RUN, actor="operator", as_of=AS_OF, system=False,
                                          run_report_path=str(self.archive.parent / "run-report.json"))
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join()))
        self.http = Http(server.server_port)

    def archive_report(self, hour, warning=None):
        report = json.loads(FIXTURE.read_text(encoding="utf-8"))
        run_id = f"run_20260101T{hour:02}0000Z_0a1b2c3d"
        report.update(run_id=run_id, mode="scheduled", started_at=f"2026-01-01T{hour:02}:00:00Z",
                      finished_at=f"2026-01-01T{hour:02}:07:30Z")
        if warning:
            next(stage for stage in report["stages"] if stage["name"] == "outputs").update(
                status="warn", error_code=warning)
        self.write(self.archive / f"{run_id}.json", report)
        return run_id

    def get(self, path="/stages"):
        status, _, html = self.http.send("GET", path)
        self.assertEqual(status, 200)
        return html

    def row(self, html, stage):
        rows = re.findall(rf"<tr><td>{stage}</td>.*?</tr>", html)
        self.assertEqual(len(rows), 1, stage)
        return rows[0]

    def test_registered_get_renders_ordered_evidence_only(self):
        # Protect navigation, deterministic table projection and the write-method boundary, not the proof rules.
        proof = self.archive_report(6)
        html = self.get()
        self.assertIn(("Go-live stages", "/stages"), console_pages.nav_links())
        self.assertIn('<a href="/stages" aria-current="page">Go-live stages</a>', html)
        self.assertEqual(re.findall(r"<tr><td>([^<]+)</td>", html), list(golive_stages.STAGES))
        self.assertIn(f'<a href="/run?id={proof}">{proof}</a>', self.row(html, "2"))
        self.assertIn("<td>external: not provable from run reports</td>", self.row(html, "A1"))
        self.assertNotIn("/run?id=", self.row(html, "A1"))
        self.assertIn("Read-only. Proven means the run report metadata qualifies; "
                      "it is machine-valid evidence, not semantic acceptance. "
                      "External stages are not provable from run reports.", html)
        self.assertEqual(self.get(), html)
        self.assertEqual(self.http.post("/stages", {})[0], 404)
        self.assertEqual(self.http.send("PUT", "/stages")[0], 405)

    def test_since_narrows_warning_cells_without_losing_proof(self):
        # Catch an ignored cutoff, a cutoff applied to all evidence, or a 400 for an unknown run.
        proof = self.archive_report(6)
        cutoff = self.archive_report(7, APPROVAL)
        self.archive_report(8, CLOCK)
        self.assertIn(f"<td>{APPROVAL}</td><td>{CLOCK}</td>", self.row(self.get(), "14"))
        row = self.row(self.get(f"/stages?since={cutoff}"), "14")
        self.assertIn(f"/run?id={proof}", row)
        self.assertIn(f"<td>2</td><td>{CLOCK}</td><td>{CLOCK}</td>", row)
        html = self.get("/stages?since=%3Cscript%3E")
        self.assertIn('<p role="status">unknown run</p>', html)
        self.assertNotIn("<table", html)
        self.assertNotIn("<script>", html)

    def test_unavailable_archive_matches_runs(self):
        # Missing/non-directory/over-limit must be diagnoses, never a plausible empty proof table.
        for code, setup in (("reports_missing", self.archive.rmdir),
                            ("reports_unreadable", self.archive.touch)):
            setup()
            self.assert_unavailable(code)
        self.archive.unlink()
        self.archive.mkdir()
        self.archive_report(6)
        self.archive_report(7)
        with mock.patch.object(console_runs_model, "FILE_LIMIT", 1):
            self.assert_unavailable("history_limit")

    def assert_unavailable(self, code):
        expected = re.findall(rf"Run history: unavailable \({code}\)[^<]+", self.get("/runs"))
        self.assertEqual(len(expected), 1, code)
        html = self.get()
        self.assertIn(expected[0], html)
        self.assertNotIn("<table", html)
        self.assertNotIn(str(self.archive), html)

    def test_invalid_report_is_named_without_raw_details(self):
        # A bad sibling must not be skipped or leak JSON payload, traceback or absolute path.
        self.archive_report(6)
        self.write(self.archive / "bad<script>.json", '{"body": "SYNTHETIC-HIDDEN"}')
        html = self.get()
        self.assertIn("golive_stages: &#x27;bad&lt;script&gt;.json&#x27;: invalid report", html)
        for text in ("<script>", "<table", "Traceback", "SYNTHETIC-HIDDEN", str(self.archive)):
            self.assertNotIn(text, html)

    def test_strict_loader_reads_the_archive_that_was_checked(self):
        # Reopening the named directory after the limit check could read a replacement archive instead.
        proof = self.archive_report(6)
        load = golive_stages.load_reports

        def replace_then_load(directory):
            self.archive.rename(self.archive.with_name("previous"))
            self.archive.mkdir()
            self.archive_report(7)
            return load(directory)

        with mock.patch.object(golive_stages, "load_reports", side_effect=replace_then_load):
            self.assertIn(f"/run?id={proof}", self.row(self.get(), "2"))

    def test_dynamic_cells_escape_visible_text_and_quote_link(self):
        # The real v1 schema rejects this ID; exercise the renderer's defense if a later reader allows it.
        hostile = "fixture/<script>&id"
        rows = [{"stage": "2", "name": "Name <script>", "state": "proven", "last_proven_run": hostile,
                 "runs_since_last_proof": 0, "basis": "Basis <script>"}]
        with mock.patch.object(golive_stages, "summarize", return_value={"stages": rows}):
            html = self.get()
        self.assertIn('href="/run?id=fixture%2F%3Cscript%3E%26id">fixture/&lt;script&gt;&amp;id</a>', html)
        self.assertIn("Name &lt;script&gt;", html)
        self.assertIn("Basis &lt;script&gt;", html)
        self.assertIn("<td>0</td>", html)
        self.assertNotIn("<script>", html)
