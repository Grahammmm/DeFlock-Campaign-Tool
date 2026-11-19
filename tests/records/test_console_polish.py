"""DFGL-14: plain wording, refused review forms that keep the typed text, landmarks, 404s and small console fixes."""
import io
import json
import re
from contextlib import redirect_stderr
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from campaign_tool.records import approval_queue as aq, console_calendar as cc, console_layout as layout
from campaign_tool.records import console_pages
from tests.records import test_console_snapshot as snapshots
from tests.records.test_admin_panel import Http, ServerCase, journal, tree_state
from tests.records.test_pipeline_health import report_for

TYPED = '<b>typed</b> & "quoted"'


class RefusedReviewFormTests(ServerCase):
    def post(self, action, **fields):
        base = {"csrf_token": self.tok, "id": self.draft_row["id"], "return": "review"}
        base.update(fields)
        if base["csrf_token"] is None:
            del base["csrf_token"]
        return self.http.post("/approvals/" + action, base)

    def refused(self, response, message, kept=None):
        status, headers, page = response
        self.assertEqual(status, 400)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertIn(message, page)
        self.assertIn('role="alert"', page)
        self.assertEqual(page.count("<main"), 1)
        self.assertIn("<form", page)  # the review page again, not a bare error
        if kept:
            self.assertIn(kept, page)
        self.assertNotIn("<b>typed</b>", page)  # echoed escaped only

    def tree(self):
        return {k: v for k, v in tree_state(self.project).items() if not k.endswith(("-shm", "-wal"))}

    def assert_untouched(self, before_tree, before_journal):
        self.assertEqual(self.tree(), before_tree)
        self.assertEqual(journal(self.project), before_journal)
        self.assertEqual(self.row()["status"], "pending")

    def test_empty_and_whitespace_reason_keep_typed_text_and_change_nothing(self):
        tree, log = self.tree(), journal(self.project)
        self.refused(self.post("reject", reason=""), "Say why you are rejecting")
        self.refused(self.post("reject", reason="   \r\n "), "Say why you are rejecting")
        self.assert_untouched(tree, log)

    def test_empty_edit_body_is_refused_and_current_draft_kept(self):
        tree, log = self.tree(), journal(self.project)
        self.refused(self.post("edit", body="  \n", expected_revision="0"), "The edited text is empty")
        self.assert_untouched(tree, log)

    def test_missing_csrf_gets_a_plain_message_and_no_echo(self):
        tree, log = self.tree(), journal(self.project)
        status, _, page = self.post("edit", csrf_token=None, body=TYPED, expected_revision="0")
        self.assertEqual(status, 400)
        self.assertIn("The form expired; reload the page and try again.", page)
        self.assertNotIn("&lt;b&gt;typed", page)
        self.assertNotIn("<b>typed", page)
        self.assert_untouched(tree, log)

    def test_dashboard_forms_still_get_a_shared_layout_error(self):
        status, _, page = self.http.post("/approvals/reject", {"csrf_token": self.tok, "id": self.draft_row["id"],
                                                               "reason": " "})
        self.assertEqual(status, 400)
        self.assertEqual(page.count("<main"), 1)
        self.assertNotIn("role=\"alert\"", page)

    def test_typed_text_is_echoed_escaped_in_the_reason_field(self):
        # whitespace-only reasons are refused; the refused field carries the typed whitespace back, escaped
        status, _, page = self.post("reject", reason="  ")
        self.assertEqual(status, 400)
        self.assertRegex(page, r'<input id="reason" name="reason" required maxlength="2000" value="  ">')
        self.assertEqual(aq.load_queue(self.project)[0]["status"], "pending")


class LandmarkAndNavTests(ServerCase):
    def get(self, path):
        return self.http.send("GET", path, headers={"Content-Length": None, "Content-Type": None})

    def test_banner_is_inside_header_and_skip_link_is_first(self):
        for path in ("/", "/settings", "/dashboard", "/nope"):
            _, _, page = self.get(path)
            body = page.split("<body>", 1)[1]
            self.assertTrue(body.startswith('<a class="skip" href="#content">Skip to content</a><header>'), path)
            self.assertRegex(body, r"<header><p class=\"notice\">PRIVATE[^<]*</p></header>")
            self.assertRegex(body, r'</header><div class="data-age"[^>]*>Live project data</div>')
            self.assertEqual(body.count('class="data-age"'), 1, path)
            self.assertEqual(page.count("<main"), 1, path)

    def test_error_page_uses_the_shared_layout_with_main(self):
        status, _, page = self.get("/nope")
        self.assertEqual(status, 404)
        self.assertIn("<main", page)
        self.assertIn('aria-label="Sections"', page)

    def test_phone_nav_targets_are_44px_and_dashboard_tables_scroll(self):
        _, _, page = self.get("/")
        self.assertRegex(page, r"max-width:699px\)\">[^<]*\.side a[^<]*min-height:44px")
        _, _, dash = self.get("/dashboard")
        self.assertIn(".scroll{overflow-x:auto", dash)
        self.assertRegex(dash, r'<div class="scroll"[^>]*><table>')
        self.assertEqual(dash.count("<table"), dash.count('<div class="scroll"'))

    def test_unknown_agency_is_404(self):
        status, _, page = self.get("/documents?agency=nope")
        self.assertEqual(status, 404)
        self.assertIn("Unknown agency", page)
        self.assertEqual(self.get("/documents")[0], 200)


class DataAgeTests(snapshots.Panel):
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)

    def stamp_export(self, value):
        path = self.root / "current" / "EXPORT.json"
        manifest = json.loads(path.read_text())
        manifest["exported_at"] = value
        path.chmod(0o600)
        path.write_text(json.dumps(manifest))
        path.chmod(0o400)

    def status_strip(self, page):
        self.assertEqual(page.count('class="data-age"'), 1)
        match = re.search(r'</header>(<div class="data-age"[^>]*>.*?</div>)', page)
        assert match is not None, "status strip must immediately follow the header"
        return match.group(1)

    def test_age_strip_uses_pinned_export_and_keeps_other_bytes(self):
        self.make_export()
        exported = "2026-11-10T20:00:00Z"
        self.stamp_export(exported)
        current = self.root / "current"
        target = current.readlink()
        base = layout.shell("Fixture", "<p>Registered page</p>")

        def serve(handler):
            current.unlink()  # simulate a broken selector after the request has pinned its data
            handler._send(200, base)
            return True

        test_page = SimpleNamespace(__name__="fixture", PATHS=("/age-fixture",), NAV=None, serve=serve)
        with mock.patch.object(console_pages, "_discover", return_value=[test_page]):
            routes, _ = console_pages._load()
        self.up(as_of=self.now.date().isoformat())
        assert self.server is not None
        http = Http(self.server.server_port)
        with mock.patch.object(console_pages, "_ROUTES", routes):
            status, headers, page = http.send("GET", "/age-fixture")
        self.assertEqual(status, 200)
        strip = self.status_strip(page)
        self.assertIn(f"Data from run {snapshots.RUN}, exported 12:00 PT, 2026-11-10 (4 h ago)", strip)
        self.assertNotIn("STALE", strip)
        footer = f'<footer class="muted"><p>Run {snapshots.RUN}, export {target.name}, exported {exported}.</p></footer>'
        self.assertEqual(page.replace(strip, ""), base.replace("</main>", footer + "</main>"))
        self.assertEqual(int(headers["content-length"]), len(page.encode()))
        self.assertEqual(headers["content-security-policy"],
                         "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                         "base-uri 'none'; frame-ancestors 'none'")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.status_strip(self.get("/")), strip)  # the last-good export
        current.symlink_to(target)
        for path in ("/", "/board", "/documents", "/calendar", "/settings", "/next-steps"):
            self.assertEqual(self.status_strip(self.get(path)), strip, path)
        status, headers, calendar = http.send("GET", "/calendar.ics")
        self.assertEqual((status, headers["content-type"]), (200, "text/calendar; charset=utf-8"))
        self.assertTrue(calendar.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertNotIn("data-age", calendar)

    def test_stale_when_a_newer_valid_completed_run_exists(self):
        self.make_export()
        self.stamp_export("2026-11-10T20:00:00Z")
        path = self.tmp / "run-report.json"
        recent = self.tmp / "reports"
        recent.mkdir()
        newer = report_for(self.now - timedelta(hours=5))  # newer run, even before the export was made
        same = report_for(self.now - timedelta(hours=1))
        same.update(run_id=snapshots.RUN, started_at="2026-11-10T15:00:00Z", duration_seconds=8 * 3600)
        future = report_for(self.now + timedelta(seconds=1))
        self.up(as_of=self.now.date().isoformat(), run_report_path=path, stale_hours=5)
        for doc, stale in ((same, False), (future, False), ({**newer, "stages": []}, False), (newer, True)):
            with self.subTest(report=doc["run_id"], stale=stale):
                path.write_text(json.dumps(doc))
                self.assertEqual('<strong style="color:#b00020">STALE:' in self.status_strip(self.get("/nope", 404)), stale)
        path.write_text("{invalid")
        (recent / (future["run_id"] + ".json")).write_text(json.dumps(future))
        self.assertNotIn("STALE", self.status_strip(self.get("/nope", 404)))
        history = recent / (newer["run_id"] + ".json")
        history.write_text(json.dumps(newer))
        self.assertIn("STALE", self.status_strip(self.get("/nope", 404)))
        history.unlink()
        self.stamp_export("2026-11-10T18:59:59Z")  # over 5 h, but under the default 13 h
        self.assertIn("STALE", self.status_strip(self.get("/nope", 404)))

    def test_default_age_cutoff_is_strict_and_independent_of_reports(self):
        self.make_export()
        self.up(as_of=self.now.date().isoformat())
        for exported, stale in (("2026-11-10T11:00:00Z", False), ("2026-11-10T10:59:59Z", True)):
            self.stamp_export(exported)
            self.assertEqual("STALE" in self.status_strip(self.get("/nope", 404)), stale)

    def test_unavailable_exports_and_unadmitted_host_do_not_claim_live_data(self):
        self.up(as_of=self.now.date().isoformat())
        with redirect_stderr(io.StringIO()):
            self.assertIn("Data age: unknown", self.status_strip(self.get("/", 503)))
            self.make_export()
            manifest = self.root / "current" / "EXPORT.json"
            original = manifest.read_text()
            manifest.chmod(0o600)
            manifest.write_text("{invalid")
            self.assertIn("Data age: unknown", self.status_strip(self.get("/", 503)))
            manifest.write_text(original)
        for stamp in (None, "nonsense", "2026-11-11T00:00:01Z", "2026-11-10T20:00:00"):
            self.stamp_export(stamp)
            self.assertIn("Data age: unknown", self.status_strip(self.get("/nope", 404)))
        self.stamp_export("2026-11-10T23:00:00Z")
        assert self.server is not None
        with mock.patch.object(snapshots.console_snapshot.Pinner, "pin", side_effect=AssertionError("Host refused before pin")):
            status, _, page = Http(self.server.server_port).send("GET", "/", host="other.invalid")
        self.assertEqual(status, 403)
        self.assertIn("Data age: unknown", self.status_strip(page))


class SourceWordingTests(ServerCase):
    def test_codes_have_plain_sentences_and_unknown_codes_fall_back(self):
        for code in ("missing", "run_mismatch", "shape_invalid", "register_missing"):
            self.assertNotIn("_", layout.reason(code))
        self.assertIn("could not be read", layout.reason("never_heard_of_it"))
        note = layout.unavailable_note("received documents", "missing")
        self.assertNotIn("(missing)", note)
        self.assertIn("not been created", note)


class CalendarSummaryTests(ServerCase):
    def test_drafting_off_agencies_are_one_summary_line_plus_details(self):
        notes = ["follow-up drafting is off for this agency: %s" % n for n in ("ALPHA", "BETA", "GAMMA")]
        with mock.patch.object(cc, "build", return_value=([], notes + ["something else"])):
            page = cc.render({"run_id": "r"}, date(2026, 11, 5), date(2026, 11, 1))
        self.assertNotIn("follow-up drafting is off for this agency", page)
        self.assertEqual(page.count("Follow-up drafting is off for 3 agencies"), 1)
        details = re.search(r"<details[^>]*><summary>[^<]*</summary><ul>(.*?)</ul></details>", page, re.S).group(1)
        self.assertEqual(re.findall(r"<li>(.*?)</li>", details), ["ALPHA", "BETA", "GAMMA"])
        self.assertIn("something else", page)


class StatusLineRowTests(ServerCase):
    def test_two_status_lines_show_template_and_date(self):
        from campaign_tool.records import console_home as ch
        model = {"now": datetime(2026, 11, 5, tzinfo=timezone.utc),
                 "queue": {"rows": []}, "sends": {"rows": []}, "packets": {"rows": []},
                 "public": {"switch": "OFF", "lines": [
                     {"id": "a", "state": "proposed", "agency_key": "x", "request_id": "R1", "template": "t-ack",
                      "event": {"type": "ack", "date": "2026-11-01"}, "proposed_at": None},
                     {"id": "b", "state": "proposed", "agency_key": "x", "request_id": "R1", "template": "t-closed",
                      "event": {"type": "closed", "date": "2026-11-03"}, "proposed_at": None}]}}
        with mock.patch.object(ch, "_overdue", return_value=(None, None, None)):
            rows = ch.decision_rows(model, date(2026, 11, 5))
        what = sorted(r["what"] for r in rows)
        self.assertEqual(what, ["Public status line (t-ack, 2026-11-01)", "Public status line (t-closed, 2026-11-03)"])
