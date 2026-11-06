"""Screens E (oversight) and F (public updates), on synthetic run outputs (two invented agencies)."""
import http.client
import json
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.records import admin_panel as ap
from campaign_tool.records import console_oversight as co
from campaign_tool.records import public_updates as pu
from tests.records import test_console_board as tcb
from tests.records import test_pipeline_health as tph

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
T0 = int(NOW.timestamp())


def candidate(agency="ex", text_date="2026-11-04"):
    timeline = {"schema": "records-timeline-v1", "requests": [{
        "agency_key": agency, "agency": "Example PD", "request_id": "r1", "issues": [],
        "events": [{"type": "sent", "date": text_date, "source": {"kind": "outbox_receipt", "message_id": "<m@x>"}}]}]}
    return pu.generate(timeline, today="2026-11-05")[0][0]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name) / "project"
        self.project.mkdir()
        tcb.build(self.project)
        self.target = Path(tmp.name) / "runtime" / "t"
        (self.target / "reports").mkdir(parents=True)
        for finished, name in ((NOW - timedelta(hours=2), "recent"), (NOW - timedelta(hours=40), "old")):
            data = tph.report_for(finished, seq=1 if name == "old" else 2)
            extract = next(s for s in data["stages"] if s["name"] == "extract")
            extract["counts"] = {"input": 5, "output": 4, "failed": 0, "skipped": 1}
            extract["outcomes"] = [{"code": "PRESERVE_PENDING", "count": 2}, {"code": "EXTRACT_OK", "count": 3}]
            (self.target / "reports" / (data["run_id"] + ".json")).write_text(json.dumps(data))
            if name == "recent":
                (self.target / "run-report.json").write_text(json.dumps(data))
        self.report = str(self.target / "run-report.json")

    def model(self, **kw):
        return co.load(self.project, tcb.RUN, self.report, now=NOW)

    def put_lines(self, *cands, hold=False, post=False):
        pu.init(self.project)
        pu.propose(self.project, list(cands), now=T0)
        return [c["id"] for c in cands]


class ScreenE(Base):
    def test_now_stuck_and_needs_g_sections_from_real_shapes(self):
        m = self.model()
        self.assertEqual(len(m["runs"]["runs"]), 1)  # the 40 h old run is outside the 24 h window
        run = m["runs"]["runs"][0]
        self.assertEqual((run["items_in"], run["items_processed"], run["preserve_pending"]), (5, 4, 2))
        self.assertEqual([r["request_id"] for r in co.overdue_requests(m)], ["R-1"])
        self.assertEqual([x["host"] for x in co.expiring_links(m)], ["files.example.test"])
        page = co.render_oversight(m)
        for text in ("Now", "Stuck", "Needs G", "Run health:", "PRESERVE_PENDING", "files.example.test",
                     "Open the approval queue", "Auto-post switch: <strong>OFF</strong>", "R-1"):
            self.assertIn(text, page)
        self.assertNotIn("<form", page)

    def test_stuck_means_ours_open_and_idle_over_72_hours(self):
        stuck = {s["request_id"]: s for s in co.stuck_requests(self.model())}
        self.assertIn("R-1", stuck)  # ball in our court, last dated event weeks old
        self.assertNotIn("R-3", stuck)  # closed
        later = co.load(self.project, tcb.RUN, self.report, now=datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc))
        self.assertNotIn("R-1", {s["request_id"] for s in co.stuck_requests(later)})  # not yet idle 72 h

    def test_pending_approvals_by_age(self):
        row = {"id": "a" * 64, "kind": "followup-missing-records", "agency_id": "alpha-pd", "request_id": "R-2",
               "status": "pending", "created_at": "2026-11-03T12:00:00Z"}
        rows = [row, dict(row, id="b" * 64, created_at="2026-11-05T09:00:00Z"), dict(row, id="c" * 64, status="approved")]
        with mock.patch.object(co.approval_queue, "load_queue", return_value=rows):
            m = self.model()
        got = co.pending_approvals(m)
        self.assertEqual([(p["id"][0], p["age_hours"]) for p in got], [("a", 48), ("b", 3)])

    def test_each_source_fails_alone_and_is_named(self):
        (self.target / "run-report.json").write_text("{not json")
        (self.project / "run-outputs" / tcb.RUN / "outcomes" / "release-links.json").unlink()
        m = self.model()
        self.assertEqual(m["links"]["state"], "unavailable")
        self.assertEqual(m["timeline"]["state"], "ok")
        self.assertEqual(m["health"]["state"], "ok")  # health reports the bad report itself
        page = co.render_oversight(m)
        self.assertIn("links: unavailable (missing)", page)
        self.assertIn("R-1", page)
        no_report = co.load(self.project, tcb.RUN, None, now=NOW)
        self.assertEqual(no_report["runs"]["code"], "run_report_not_configured")
        self.assertIn("runs: unavailable", co.render_oversight(no_report))

    def test_missing_timeline_degrades_without_error(self):
        (self.project / "run-outputs" / tcb.RUN / "outcomes" / "timeline.json").unlink()
        page = co.render_oversight(self.model())
        self.assertIn("timeline: unavailable", page)
        self.assertIn("unavailable without a timeline", page)

    def test_escaping(self):
        m = self.model()
        m["links"]["doc"]["links"][0]["host"] = "<script>x</script>"
        page = co.render_oversight(m)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)


class ScreenF(Base):
    def test_lines_by_state_and_switch_state(self):
        ids = self.put_lines(candidate(), candidate("ex2", "2026-11-03"))
        pu.retract(self.project, ids[1], "g", now=T0)
        m = self.model()
        self.assertEqual(m["public"]["switch"], "OFF")
        self.assertEqual([r["id"] for r in co.lines_in(m, "proposed")], [ids[0]])
        self.assertEqual([r["id"] for r in co.lines_in(m, "retracted")], [ids[1]])
        page = co.render_public(m, "tok")
        self.assertIn("We sent a public records request to Example PD.", page)
        self.assertIn("Veto", page)

    def test_killed_and_posted_undo_form(self):
        ids = self.put_lines(candidate())
        with mock.patch.object(pu, "_switch_state", return_value="ON"):
            pu.advance(self.project, now=T0)
            pu.advance(self.project, now=T0 + pu.HOLD_SECONDS + 1)
        m = self.model()
        self.assertEqual([r["id"] for r in co.lines_in(m, "posted")], [ids[0]])
        self.assertIn('value="%s"' % ids[0], co.render_public(m, "tok"))
        self.assertIn("Undo", co.render_public(m, "tok"))
        pu.kill(self.project, "g", now=T0 + 10)
        self.assertEqual(self.model()["public"]["switch"], "KILLED")

    def test_missing_journal_is_empty_not_an_error_and_corrupt_journal_is_named(self):
        m = self.model()
        self.assertEqual((m["public"]["state"], m["public"]["lines"]), ("ok", []))
        self.assertFalse((self.project / pu.LOCK).exists())  # reading creates nothing
        (self.project / pu.JOURNAL).write_text("garbage\n")
        m = self.model()
        self.assertEqual(m["public"]["state"], "unavailable")
        self.assertIn("public: unavailable", co.render_public(m, "tok"))

    def test_unfinished_last_line_is_ignored(self):
        self.put_lines(candidate())
        with open(self.project / pu.JOURNAL, "ab") as f:
            f.write(b'{"schema": "records-public-')
        self.assertEqual(len(self.model()["public"]["lines"]), 1)


class Server(Base):
    def setUp(self):
        super().setUp()
        self.server = ap.build_server(self.project, tcb.RUN, actor="tester", run_report_path=self.report, system=False)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, origin=True, host=None):
        headers = {"Host": host or "127.0.0.1:%d" % self.port}
        if body is not None:
            headers.update({"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))})
        if origin:
            headers["Origin"] = "http://127.0.0.1:%d" % self.port
        with closing(http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)) as c:
            c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in headers.items():
                c.putheader(k, v)
            c.endheaders(body.encode() if body else None)
            r = c.getresponse()
            return r.status, r.read().decode()

    def post(self, path, **fields):
        fields.setdefault("csrf_token", self.server.csrf_token)
        return self.request("POST", path, urllib.parse.urlencode(fields))

    def test_pages_served(self):
        for path in ("/oversight", "/public-updates"):
            status, page = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertIn("noindex", page)
        self.assertEqual(self.request("GET", "/oversight", host="evil.test")[0], 403)

    def test_undo_off_and_kill_work_and_refuse_bad_input(self):
        ids = self.put_lines(candidate())
        self.assertEqual(self.post("/public-updates/retract", id=ids[0], csrf_token="wrong")[0], 403)
        self.assertEqual(self.post("/public-updates/retract", id="zz")[0], 400)
        self.assertEqual(self.post("/public-updates/retract", id="0" * 16)[0], 409)  # unknown line
        self.assertEqual(self.request("POST", "/public-updates/off", "csrf_token=%s" % self.server.csrf_token, origin=False)[0], 403)
        self.assertEqual(self.post("/public-updates/off", extra="1")[0], 400)
        self.assertEqual(self.post("/public-updates/retract", id=ids[0])[0], 303)
        self.assertEqual(self.post("/public-updates/retract", id=ids[0])[0], 409)  # duplicate undo
        self.assertEqual(co.lines_in(self.model(), "retracted")[0]["retract_reason"], "undo")
        self.assertEqual(self.post("/public-updates/off")[0], 303)
        self.assertEqual(self.post("/public-updates/kill")[0], 303)
        self.assertEqual(self.model()["public"]["switch"], "KILLED")

    def test_no_route_can_turn_the_switch_on_or_approve(self):
        self.assertEqual(self.post("/public-updates/on")[0], 404)
        self.assertEqual(self.post("/public-updates/enable", signature="x")[0], 404)
        self.assertEqual(self.model()["public"]["switch"], "OFF")


if __name__ == "__main__":
    unittest.main()

    def test_controls_work_with_panel_actor_labels_the_journal_would_reject(self):
        self.server.actor = "Graham (operator)"
        ids = self.put_lines(candidate())
        self.assertEqual(self.post("/public-updates/retract", id=ids[0])[0], 303)
        self.assertEqual(co.lines_in(self.model(), "retracted")[0]["retract_reason"], "undo")
        self.assertEqual(self.post("/public-updates/off")[0], 303)
        self.assertEqual(self.post("/public-updates/kill")[0], 303)
        self.assertEqual(self.model()["public"]["switch"], "KILLED")
