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


if __name__ == "__main__":
    unittest.main()
