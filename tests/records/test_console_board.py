"""Screens A (Board) and B (Agency timeline), on synthetic run outputs (two invented agencies)."""
import http.client
import json
import tempfile
import threading
import urllib.parse
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from campaign_tool.records import admin_panel as ap
from campaign_tool.records import console_board as cb
from campaign_tool.records.outcomes import timeline as tl

RUN = "run_1"
TODAY = date(2026, 11, 5)


def clock(**kw):
    return {"state": "running", "basis": "recorded_receipt", "base_due": "2026-11-09", "extended_due": "2026-11-23",
            "due": "2026-11-09", "recorded_due": None, "determination_at": None, "extension_recorded": False, **kw}


def row(agency, request, deadline=None, action="wait", **kw):
    return {"agency": agency.upper(), "agency_key": agency, "request_id": request, "ledger_request_id": "L-" + request,
            "deadline": deadline or clock(), "next_action": action, "status": "open", "issues": [],
            "production_at": None, **kw}


def put(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def build(project, drop=()):
    out = project / "run-outputs" / RUN
    register = {"as_of": "2026-11-05", "requests": [
        row("alpha-pd", "R-1", clock(state="past_due_reminder", due="2026-10-20"), action="follow up"),
        row("alpha-pd", "R-2"),
        row("tracker:beta sheriff / coroner", "R-3", clock(state="determined", determination_at="2026-10-30"), status="closed"),
        row("tracker:beta sheriff / coroner", "R-4", clock(extension_recorded=True))]}
    register["requests"][2]["status"] = "closed"
    received = {"items": [
        {"item_id": "i-1", "agency_id": "alpha-pd", "request_id": "R-1", "received_at": "2026-10-01T00:00:00Z",
         "kind": "extension", "deadlines": [{"date": "2026-12-20"}], "comparison_status": None}]}
    drafts = [{"id": "d1", "agency_id": "alpha-pd", "request_id": "R-2", "kind": "followup-missing-records",
               "status": "pending", "created_at": "2026-11-02T00:00:00Z"}]
    outbox = [{"idempotency_key": "k1", "kind": "send_request", "agency_id": "alpha-pd", "request_id": "R-1",
               "sent_at": "2026-09-20T00:00:00Z", "state": "sent", "message_id": "<m1@example.test>"}]
    manual = [{"event_id": "m1", "agency_id": "tracker:beta sheriff / coroner", "request_id": "R-4", "type": "call", "date": "2026-11-01",
               "actor": "G", "summary": "<b>called</b>"}]
    docs = {
        "outcomes/timeline.json": tl.build_timeline(register, received, drafts, outbox, manual, run_id=RUN),
        "outcomes/release-links.json": {"schema": "records-release-links-v1", "run_id": RUN, "links": [
            {"link_id": "L" * 64, "host": "files.example.test", "platform": "x", "url_redacted": "u",
             "agency_ids": ["alpha-pd"], "sources": [], "expiry": {"date": "2026-11-12"}, "expiry_state": "stated"},
            {"link_id": "M" * 64, "host": "late.example.test", "platform": "x", "url_redacted": "u",
             "agency_ids": ["alpha-pd"], "sources": [], "expiry": {"date": "2026-12-30"}, "expiry_state": "stated"}]},
        "outcomes/response-items.json": {"schema": "records-response-items-v1", "run_id": RUN, "requests": [
            {"agency_key": "alpha-pd", "request_id": "R-1", "flags": [], "asks": [
                {"ask_id": "a1", "text": "The vendor contract", "rating": "missing", "evidence": [], "flags": []},
                {"ask_id": "a2", "text": "Search logs for January", "rating": "answered",
                 "evidence": [{"item_id": "i-1", "page": 1, "quote": "log"}], "flags": []}]}]},
    }
    for name, doc in docs.items():
        if name not in drop:
            put(out / name, doc)


class Boards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        build(self.project)

    def model(self):
        return cb.load(self.project, RUN)

    def test_agency_computed_citation_once_per_request_and_unverified_without_register(self):
        from campaign_tool import law
        from campaign_tool.records import followup_clock
        import hashlib
        path = self.project / "run-outputs" / RUN / followup_clock.REGISTER_NAME
        rows = [row("alpha-pd", "R-1", request_date="2026-10-30"),
                row("alpha-pd", "R-2", request_date="2026-10-30")]
        put(path, {"schema": "records-request-register-v1", "as_of": "2026-11-05", "requests": rows})
        digest = hashlib.sha256(law.package_path("us-ca").read_bytes()).hexdigest()[:12]
        page = cb.render_agency(self.model(), "alpha-pd")
        citation = "Dates computed from Gov. Code § 7922.535 (law package %s); provisional, not a legal finding." % digest
        self.assertEqual(page.count(citation), 2)
        self.assertEqual(page.count("(COMPUTED; from recorded receipt)"), 4)
        path.unlink()
        page = cb.render_agency(self.model(), "alpha-pd")
        self.assertEqual(page.count("(UNVERIFIED;"), 4)
        self.assertNotIn("(COMPUTED;", page)

    def test_board_orders_needs_action_first_and_counts(self):
        m = self.model()
        rows = cb.board_rows(m, TODAY)
        self.assertEqual([r["request_id"] for r in rows], ["R-1", "R-2", "R-4", "R-3"])
        self.assertEqual(rows[0]["overdue"], [tl.OVERDUE_LABEL])
        self.assertEqual(rows[0]["last_contact_days"], 35)  # latest of sent 09-20, received 10-01
        c = cb.counters(m, rows, TODAY)
        self.assertEqual((c["requests"], c["overdue"], c["drafts_pending"], c["links_expiring_14d"]), (4, 1, 1, 1))

    def test_filters(self):
        rows = cb.board_rows(self.model(), TODAY)
        ids = lambda f: [r["request_id"] for r in rows if cb.keep(r, f)]
        self.assertEqual(ids("overdue"), ["R-1"])
        self.assertEqual(ids("closed"), ["R-3"])
        self.assertIn("R-4", ids("waiting"))
        self.assertNotIn("R-3", ids("ours"))
        self.assertEqual(len(ids("all")), 4)

    def test_tier1_status_and_agency_page_clocks_and_marks(self):
        m = self.model()
        self.assertNotEqual(cb.board_rows(m, TODAY)[0]["tier1"], "not available")
        page = cb.render_agency(m, "alpha-pd")
        for text in ("recorded_receipt", "OVERDUE", tl.OVERDUE_LABEL, "extension total exceeds 14 days on our records",
                     "2026-12-20", "Stage", "Next action", 'href="mid:m1@example.test"', "tier"):
            self.assertIn(text, page)
        self.assertNotIn("tracker:beta sheriff / coroner", page.replace('href="/documents?agency=alpha-pd"', ""))
        self.assertLess(page.index("2026-09-20"), page.index("2026-10-01"))  # oldest first

    def test_escaping_and_no_action_forms(self):
        page = cb.render_agency(self.model(), "tracker:beta sheriff / coroner")
        self.assertIn("&lt;b&gt;called&lt;/b&gt;", page)
        self.assertNotIn("<b>called</b>", page)
        self.assertNotIn("<form", page + cb.render_board(self.model(), TODAY))

    def test_missing_stale_or_malformed_timeline_is_named(self):
        for mutate, code in (("drop", "missing"), ("stale", "run_mismatch"), ("shape", "shape_invalid"),
                             ("schema", "schema_invalid")):
            with self.subTest(code=code):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                project = Path(tmp.name)
                build(project, drop=("outcomes/timeline.json",) if mutate == "drop" else ())
                path = project / "run-outputs" / RUN / "outcomes" / "timeline.json"
                if mutate != "drop":
                    doc = json.loads(path.read_text())
                    if mutate == "stale":
                        doc["run_id"] = "other"
                    elif mutate == "shape":
                        doc["requests"][0]["events"] = None
                    else:
                        doc["schema"] = "nope"
                    put(path, doc)
                m = cb.load(project, RUN)
                self.assertEqual(m["timeline"]["code"], code)
                page = cb.render_board(m, TODAY)
                self.assertIn("timeline: unavailable (%s)" % code, page)
                self.assertIn("No timeline", page)
                self.assertIn("timeline: unavailable", cb.render_agency(m, "alpha-pd"))

    def test_other_source_missing_degrades_alone(self):
        build(self.project, drop=("outcomes/release-links.json", "outcomes/response-items.json"))
        (self.project / "run-outputs" / RUN / "outcomes" / "release-links.json").unlink(missing_ok=True)
        (self.project / "run-outputs" / RUN / "outcomes" / "response-items.json").unlink(missing_ok=True)
        m = self.model()
        rows = cb.board_rows(m, TODAY)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["tier1"], "not available")
        self.assertIsNone(cb.counters(m, rows, TODAY)["links_expiring_14d"])
        self.assertIn("links: unavailable (missing)", cb.render_board(m, TODAY))

    def test_unknown_clock_is_not_called_overdue(self):
        path = self.project / "run-outputs" / RUN / "outcomes" / "timeline.json"
        doc = json.loads(path.read_text())
        doc["requests"][1]["overdue_marks"] = [{"kind": "unknown", "date": None, "basis": "unknown", "label": "x"}]
        put(path, doc)
        r2 = [r for r in cb.board_rows(self.model(), TODAY) if r["request_id"] == "R-2"][0]
        self.assertEqual((r2["overdue"], r2["clock_unknown"]), ([], True))

    def test_agency_page_renders_unknown_clock_without_overdue_assertion(self):
        path = self.project / "run-outputs" / RUN / "outcomes" / "timeline.json"
        doc = json.loads(path.read_text())
        doc["requests"][1]["overdue_marks"] = [{"kind": "unknown", "date": None, "basis": "unknown", "label": "no date"}]
        put(path, doc)
        page = cb.render_agency(self.model(), "alpha-pd")
        self.assertIn("Clock unknown: no date", page)
        self.assertNotIn("<strong>OVERDUE</strong> unknown", page)

    def test_documents_links_work_for_tracker_keys(self):
        m = self.model()
        plain = cb.render_agency(m, "alpha-pd")
        self.assertIn('href="/documents?agency=alpha-pd"', plain)
        page = cb.render_agency(m, "tracker:beta sheriff / coroner")
        self.assertIn('href="/documents"', page)
        self.assertNotIn("/documents?agency=tracker", page)

    def test_last_contact_ignores_promised_dates(self):
        row_ = {"events": [
            {"type": "received", "date": "2026-10-01", "source": {"kind": "received_item"}},
            {"type": "promised date", "date": "2026-11-03", "new_dates": ["2026-11-03"], "source": {"kind": "received_item"}}]}
        self.assertEqual(cb._last_contact(row_, TODAY), 35)

    def test_malformed_nested_timeline_fields_degrade_as_unavailable(self):
        path = self.project / "run-outputs" / RUN / "outcomes" / "timeline.json"
        base = json.loads(path.read_text())
        for name, mutate in (
                ("null event", lambda d: d["requests"][0].__setitem__("events", [None])),
                ("event type", lambda d: d["requests"][0]["events"][0].__setitem__("type", 5)),
                ("new_dates", lambda d: d["requests"][0]["events"][0].__setitem__("new_dates", [1])),
                ("mark", lambda d: d["requests"][0].__setitem__("overdue_marks", [None])),
                ("mark missing label", lambda d: d["requests"][0].__setitem__("overdue_marks", [{"kind": "statutory"}])),
                ("mark null label", lambda d: d["requests"][0].__setitem__("overdue_marks", [{"kind": "statutory", "label": None}])),
                ("chain new_dates", lambda d: d["requests"][0].__setitem__("extension_chain", {"new_dates": [1]})),
                ("clocks", lambda d: d["requests"][0].__setitem__("clocks", [])),
                ("source", lambda d: d["requests"][0]["events"][0].__setitem__("source", "x"))):
            with self.subTest(name):
                doc = json.loads(json.dumps(base))
                mutate(doc)
                put(path, doc)
                m = self.model()
                self.assertEqual(m["timeline"], {"state": "unavailable", "code": "shape_invalid"})
                self.assertIn("No timeline", cb.render_board(m, TODAY))
                self.assertIn("timeline: unavailable", cb.render_agency(m, "alpha-pd"))

    def test_panel_routes(self):
        server = ap.build_server(self.project, RUN, actor="op", system=False)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        def get(path, host=None):
            with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as conn:
                conn.putrequest("GET", path, skip_host=True)
                conn.putheader("Host", host or "127.0.0.1:%d" % port)
                conn.endheaders()
                r = conn.getresponse()
                return r.status, r.read().decode()

        status, body = get("/board?filter=overdue")
        self.assertEqual(status, 200)
        self.assertIn("R-1", body)
        self.assertNotIn("R-4", body)
        status, body = get("/agency?agency=" + urllib.parse.quote("tracker:beta sheriff / coroner", safe=""))
        self.assertEqual((status, "R-4" in body), (200, True))
        self.assertEqual(get("/board?filter=bogus")[0], 400)
        self.assertEqual(get("/agency")[0], 400)
        self.assertEqual(get("/agency?agency=nobody")[0], 404)
        self.assertEqual(get("/agency?agency=%s" % ("x" * 300))[0], 400)
        self.assertEqual(get("/agency?agency=a%0Ab")[0], 400)
        status, body = get("/board")  # board links resolve for keys with spaces and slashes
        link = [l for l in body.split('href="') if l.startswith("/agency?")][0].split('"')[0]
        self.assertEqual(get(link.replace("&amp;", "&"))[0], 200)
        self.assertEqual(get("/board", host="evil.test")[0], 403)
        self.assertEqual(get("/documents?agency=alpha-pd")[0], 200)  # Screen C still served


class AgencySections(unittest.TestCase):
    """Synthetic read-only sources, including real refusal classification and CSV validation."""
    def model(self):
        return cb.load(self.project, RUN)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        build(self.project)
        self.retention_path = self.project / "agency-retention.csv"
        self.write_retention([])
        path = self.project / "run-outputs" / RUN / "outcomes" / "response-items.json"
        doc = json.loads(path.read_text())
        for n, ask in enumerate(doc["requests"][0]["asks"], 1):
            ask.update(ask_id="ask-%d" % n, rating="withheld", evidence=[{"item_id": "i-%d" % n, "page": n,
                       "quote": "<script>Withheld</script> under Government Code section 9999."}])
        put(path, doc)

    def write_retention(self, rows):
        import csv
        with self.retention_path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, cb.agency_retention.HEADER)
            writer.writeheader()
            writer.writerows(rows)

    def statement(self, **kw):
        return dict(agency="alpha-pd", system="ALPR", vendor="flock", data_layer="detections",
                    stated_period="30 days", period_days="30", bound="exact", source_kind="order_form",
                    quote="Synthetic retention statement.", doc_sha256="a" * 64, page="2",
                    confidence="high", checked="VERIFIED-ORIGINAL", **kw)

    def test_refusal_rows_review_and_escaped_quote(self):
        from unittest.mock import patch
        queue = [{"id": "draft-1", "agency_id": "alpha-pd", "request_id": "R-1", "kind": "appeal-denial",
                  "status": "pending", "revision": 0, "evidence": {"item_id": "i-1", "ask_numbers": [1]}}]
        with patch.object(cb.denial_log.approval_queue, "load_queue", return_value=queue), \
                patch.object(cb.denial_log.mail_send, "results", return_value=[]):
            model = self.model()
        self.assertEqual(len(model["refusals"]["entries"]), 2)
        page = cb.render_agency(model, "alpha-pd")
        section = page.split("<h2>Refusals</h2>")[1].split("</section>")[0]
        for text in ("R-1", "p.1", "p.2", "UNVERIFIED by us", "Draft ready",
                     'href="/review?id=draft-1"', "Not drafted", "&lt;script&gt;Withheld&lt;/script&gt;"):
            self.assertIn(text, section)
        self.assertNotIn("<script>", section)
        self.assertEqual(page.count(cb.READING_LABEL), 2)
        self.assertNotIn("<form", page)

    def test_reply_states_cancelled_expired_duplicate_and_stale_receipts(self):
        from unittest.mock import patch
        base = dict(id="draft-1", agency_id="alpha-pd", request_id="R-1", kind="appeal-denial",
                    revision=1, evidence={"item_id": "i-1", "ask_numbers": [1]})
        for status, expected in (("approved", "Approved"), ("sent", "Sent"), ("rejected", "Rejected"),
                                 ("cancelled", "Not drafted"), ("expired", "Not drafted")):
            with self.subTest(status=status), \
                    patch.object(cb.denial_log.approval_queue, "load_queue", return_value=[dict(base, status=status)]), \
                    patch.object(cb.denial_log.mail_send, "results", return_value=[
                        {"queue_id": "draft-1", "revision": 0, "state": "sent"}]):
                section = cb.render_agency(self.model(), "alpha-pd").split("<h2>Refusals</h2>")[1]
                self.assertIn("<td>%s</td>" % expected, section)
                self.assertNotIn('/review?id=', section)
                if status == "approved":
                    self.assertNotIn("<td>Sent</td>", section)
        with patch.object(cb.denial_log.approval_queue, "load_queue", return_value=[dict(base, status="pending")] * 2), \
                patch.object(cb.denial_log.mail_send, "results", return_value=[]):
            self.assertIn("Refusals unavailable (denial_log_invalid_source)", cb.render_agency(self.model(), "alpha-pd"))

    def test_retention_sources_scopes_other_vendor_and_exact_agency_match(self):
        flock = self.statement()
        policy = dict(flock, data_layer="downloaded_data", source_kind="policy", stated_period="minimum 365 days",
                      period_days="365", bound="min", page="3", confidence="medium", checked="UNVERIFIED")
        other = dict(flock, system="Other ALPR", vendor="motorola-vigilant", stated_period="90 days", period_days="90")
        self.write_retention([flock, policy, other])
        page = cb.render_agency(self.model(), "alpha-pd")
        for text in ("30 days", "minimum 365 days", "differ in scope", "motorola-vigilant", "90 days",
                     "order_form, p.2, doc aaaaaaaaaaaa", "policy, p.3", "high, VERIFIED-ORIGINAL", "medium, UNVERIFIED"):
            self.assertIn(text, page)
        self.assertIn("No retention statement on file.", cb.render_agency(self.model(), "Alpha-PD"))
        self.write_retention([flock, dict(policy, data_layer="detections")])
        self.assertIn("longer than the Flock 30-day default", cb.render_agency(self.model(), "alpha-pd"))

    def test_empty_sources_stale_denials_and_invalid_retention_degrade_alone(self):
        page = cb.render_agency(self.model(), "tracker:beta sheriff / coroner")
        self.assertIn("No refusals on our records.", page)
        self.assertIn("No retention statement on file.", page)
        path = self.project / "run-outputs" / RUN / "outcomes" / "response-items.json"
        doc = json.loads(path.read_text())
        for change in ("stale", "missing"):
            if change == "stale":
                put(path, dict(doc, run_id="old-run"))
                code = "denial_log_invalid_source"
            else:
                path.unlink()
                code = "denial_log_source_missing"
            self.write_retention([self.statement()])
            page = cb.render_agency(self.model(), "alpha-pd")
            self.assertIn("Refusals unavailable (%s)" % code, page)
            self.assertIn("30 days", page)
        self.write_retention([self.statement(), self.statement()])
        model = self.model()
        self.assertEqual(len(model["retention"]["rows"]), 1)
        self.assertIn("1 validation problem(s)", cb.render_agency(model, "alpha-pd"))
        self.retention_path.unlink()
        page = cb.render_agency(self.model(), "alpha-pd")
        self.assertIn(cb.RETENTION_ABSENT, page)
        self.assertNotIn("validation problem(s)", page.split("<h2>Retention</h2>")[1])

    def retention_section(self, agency="alpha-pd"):
        return cb.render_agency(self.model(), agency).split("<h2>Retention</h2>")[1].split("</section>")[0]

    def test_retention_absent_is_plain_not_a_problem(self):
        self.retention_path.unlink()
        self.assertEqual(self.model()["retention"], {"state": "absent"})
        section = self.retention_section()
        self.assertIn("No retention statements recorded for this campaign (agency-retention.csv in the project folder).",
                      section)
        self.assertNotIn("validation problem", section)
        self.assertNotIn("<table", section)
        self.assertNotIn("No retention statement on file.", section)

    def test_retention_from_project_file(self):
        self.write_retention([self.statement()])
        section = self.retention_section()
        self.assertNotIn(cb.RETENTION_ABSENT, section)
        for text in ("<td>ALPR</td>", "<td>flock</td>", "<td>30 days</td>", "order_form, p.2, doc aaaaaaaaaaaa"):
            self.assertIn(text, section)

    def test_retention_ignores_repo_pilot_file(self):
        pilot = Path(cb.__file__).resolve().parents[2] / "data" / "slo-pilot" / "agency-retention.csv"
        self.assertTrue(pilot.is_file())  # the repo copy exists and is valid but must not be read
        self.retention_path.unlink()
        self.assertEqual(self.model()["retention"], {"state": "absent"})
        section = self.retention_section()
        self.assertIn(cb.RETENTION_ABSENT, section)
        self.assertNotIn("<table", section)

    def test_retention_symlink_is_refused(self):
        real = self.project / "real-retention.csv"
        real.write_text(self.retention_path.read_text())
        self.write_retention([self.statement()])
        real.write_text(self.retention_path.read_text())
        self.retention_path.unlink()
        self.retention_path.symlink_to(real)
        model = self.model()
        self.assertEqual(model["retention"]["state"], "ok")
        self.assertEqual(model["retention"]["rows"], [])
        section = self.retention_section()
        self.assertIn("1 validation problem(s)", section)
        self.assertNotIn("<table", section)
        self.assertNotIn(cb.RETENTION_ABSENT, section)

    def test_retention_bad_header_counts_one_problem(self):
        self.retention_path.write_text("agency,system\nalpha-pd,ALPR\n")
        section = self.retention_section()
        self.assertIn("1 validation problem(s)", section)
        self.assertNotIn("<table", section)

    def test_retention_text_is_escaped(self):
        self.write_retention([self.statement(), dict(self.statement(), system="<script>x</script>", data_layer="audit_trail",
                                                     quote="<script>q</script>")])
        section = self.retention_section()
        self.assertNotIn("<script>", section)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", section)

    def test_home_and_oversight_still_render(self):
        from campaign_tool.records import console_oversight as oversight
        from tests.records import test_console_home as th
        home_project = Path(tempfile.mkdtemp(dir=self.tmp.name))
        th.fixture(home_project)

        def render_all():
            page = oversight.render_oversight(oversight.load(self.project, RUN))
            self.assertIn("<h1", page)
            self.assertIn("Requests", th.home.render_home(th.home.load(home_project, RUN, now=th.NOW), "t"))

        (home_project / "agency-retention.csv").write_text(self.retention_path.read_text())
        render_all()  # with the file
        self.retention_path.unlink()
        (home_project / "agency-retention.csv").unlink()
        render_all()  # without the file


if __name__ == "__main__":
    unittest.main()
