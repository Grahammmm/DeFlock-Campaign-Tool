"""DFGL-04: an untied received message becomes one 'tie to a request' decision; the approved link is used next run."""
import json
import stat
import threading
import unittest
from unittest.mock import patch

from campaign_tool.records import admin_panel as ap, approval_queue as aq, run_stages
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import received_items as ri, unmatched
from tests.approval_signing import Signer
from tests.records import test_outcome_received_items as _ri
from tests.records.test_admin_panel import Http

CALTRANS = ["Caltrans District 5\nWe have received your public records request.\n"]
SUBJECT, SENDER = "SECRET-SUBJECT-LINE", "clerk-local@caltrans.example"


class UnmatchedTests(unittest.TestCase):
    setUp0 = _ri.ReceivedItemsTests.setUp
    private, document, enroll, tree = (_ri.ReceivedItemsTests.private, _ri.ReceivedItemsTests.document,
                                      _ri.ReceivedItemsTests.enroll, _ri.ReceivedItemsTests.tree)
    stage = _ri.ReceivedItemsTests.stage

    def setUp(self):
        self.setUp0()
        self.sha, _ = self.document(CALTRANS, "caltrans")
        headers = json.dumps({"schema": "verified-eml-headers-v1", "source_original_sha256": self.sha, "parser_defects": [],
                              "fields": [["From", "Clerk <%s>" % SENDER], ["Subject", SUBJECT]]})
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO mail_messages VALUES(?,?,?,?,?,?,?,?,?,?)",
                        ("m1", "acct", "INBOX", 1, 1, "<x@example.test>", self.sha, headers, "2041-02-03T10:00:00Z", "verified"))
            for rid, agency, when in (("req-a", "caltrans", "2041-01-01"), ("req-b", "caltrans", "2041-01-05"),
                                      ("req-x", "other", "2041-01-02")):
                con.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?)", (rid, agency, rid, when, None, None, None, "open"))
            con.commit()
        (self.processing / "runs").mkdir(mode=0o700)
        (self.processing / "runs" / "extract-test-run.json").write_text(json.dumps(
            {"schema": "records-extract-outcomes-v1", "run_id": "test-run", "documents": []}))
        self.signer = Signer()
        self.addCleanup(self.signer.close)
        trust = self.signer.trust()
        trust.__enter__()
        self.addCleanup(trust.__exit__, None, None, None)

    def outcomes(self, run="test-run"):
        return self.project / "run-outputs" / run / "outcomes"

    def run_outputs(self, run="test-run"):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_stages.main(["outputs", "--project", str(self.project), "--run-id", run])
        return code, json.loads(out.getvalue())

    def server(self):
        server = ap.build_server(self.project, "test-run", actor="operator-1", as_of="2041-02-04", system=False,
                                 run_report_path=self.project / "report.json")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(5)
            server.server_close()
        self.addCleanup(stop)
        return server, Http(server.server_address[1])

    def row(self):
        rows = [r for r in aq.load_queue(self.project) if r["kind"] == "message_link"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def post(self, http, server, action, ident, **fields):
        return http.post("/approvals/" + action, {"csrf_token": server.csrf_token, "id": ident, **fields})[0]

    def test_untied_message_listed_metadata_only_and_one_decision_queued(self):
        self.assertEqual(self.run_outputs()[0], 0)
        raw = (self.outcomes() / "unmatched-messages.json").read_text()
        report = json.loads(raw)
        self.assertEqual(report["schema"], "records-unmatched-v1")
        (entry,) = report["messages"]
        self.assertEqual((entry["message_sha256"], entry["received_date"], entry["sender_domain"], entry["attachment_count"]),
                         (self.sha, "2041-02-03", "caltrans.example", 0))
        self.assertEqual(entry["candidates"], [{"request_id": "req-b", "agency_id": "caltrans"},
                                               {"request_id": "req-a", "agency_id": "caltrans"}])  # newest first
        for text in (SUBJECT, SENDER, "clerk-local", "public records request"):
            self.assertNotIn(text, raw)
        self.assertEqual(stat.S_IMODE((self.outcomes() / "unmatched-messages.json").stat().st_mode), 0o600)
        row = self.row()
        self.assertEqual((row["status"], row["item_id"]), ("pending", self.sha))
        for text in (SUBJECT, SENDER):
            self.assertNotIn(text, json.dumps(row))
        self.run_outputs()  # a replay never queues a second decision
        self.row()  # still exactly one

    def test_console_shows_the_decision_and_approval_ties_the_next_run(self):
        self.run_outputs()
        server, http = self.server()
        ident = self.row()["id"]
        status, _, page = http.send("GET", "/review?id=" + ident, headers={"Content-Length": None, "Content-Type": None})
        self.assertEqual(status, 200)
        self.assertIn("Tie this message to a request", page)
        self.assertEqual(page.count('name="choice"'), 2)
        sig = self.signer.sign(aq.approval_payload(self.project, ident))
        self.assertEqual(self.post(http, server, "approve", ident, signature=sig, choice="req-b"), 303)
        links = self.project / "message-links.jsonl"
        self.assertEqual(stat.S_IMODE(links.stat().st_mode), 0o600)
        (line,) = links.read_text().splitlines()
        link = json.loads(line)
        self.assertEqual((link["message_sha256"], link["request_id"], link["agency_id"], link["actor"]),
                         (self.sha, "req-b", "caltrans", self.signer.principal))
        self.assertEqual(self.post(http, server, "approve", ident, signature=sig, choice="req-b"), 303)  # replay
        self.assertEqual(len(links.read_text().splitlines()), 1)
        before = self.tree()
        self.assertEqual(self.post(http, server, "approve", ident, signature=sig, choice="req-a"), 409)
        self.assertEqual(self.tree(), before)
        self.assertEqual(links.read_text().splitlines(), [line])
        self.assertEqual(self.row()["status"], "approved")
        self.assertEqual(unmatched.read_links(self.project), ({self.sha: ("caltrans", "req-b")}, 0))
        self.assertEqual(self.run_outputs("next-run")[0], 0)
        item = json.loads((self.outcomes("next-run") / "received-items.json").read_text())["items"][0]
        self.assertEqual((item["original_sha256"], item["agency_id"], item["request_id"]), (self.sha, "caltrans", "req-b"))
        self.assertEqual(json.loads((self.outcomes("next-run") / "unmatched-messages.json").read_text())["messages"], [])

    def test_approval_retry_writes_a_link_only_when_missing(self):
        self.run_outputs()
        server, http = self.server()
        ident = self.row()["id"]
        sig = self.signer.sign(aq.approval_payload(self.project, ident))
        with patch.object(unmatched, "append_link", side_effect=OSError("synthetic write failure")):
            self.assertEqual(self.post(http, server, "approve", ident, signature=sig, choice="req-b"), 503)
        self.assertEqual(self.row()["status"], "approved")
        links = self.project / "message-links.jsonl"
        self.assertFalse(links.exists())
        self.assertEqual(self.post(http, server, "approve", ident, signature=sig, choice="req-b"), 303)
        (line,) = links.read_text().splitlines()
        self.assertEqual(json.loads(line)["request_id"], "req-b")
        self.assertEqual(self.post(http, server, "approve", ident, signature=sig, choice="req-b"), 303)
        self.assertEqual(links.read_text().splitlines(), [line])

    def test_choice_outside_the_candidates_is_400_and_changes_nothing(self):
        self.run_outputs()
        server, http = self.server()
        ident = self.row()["id"]
        sig = self.signer.sign(aq.approval_payload(self.project, ident))
        for fields in ({"choice": "req-x"}, {"choice": "nope"}, {}):
            self.assertEqual(self.post(http, server, "approve", ident, signature=sig, **fields), 400)
        self.assertEqual(self.post(http, server, "reject", ident, reason="no", choice="req-a"), 400)
        self.assertFalse((self.project / "message-links.jsonl").exists())
        self.assertEqual(self.row()["status"], "pending")

    def test_reject_records_not_a_response_and_links_nothing(self):
        self.run_outputs()
        server, http = self.server()
        ident = self.row()["id"]
        self.assertEqual(self.post(http, server, "reject", ident, reason="not a response"), 303)
        self.assertEqual(self.row()["status"], "rejected")
        self.assertFalse((self.project / "message-links.jsonl").exists())

    def test_link_to_a_vanished_request_is_ignored_and_counted(self):
        unmatched.append_link(self.project, self.sha, "caltrans", "req-gone", "tester")
        links, bad = unmatched.read_links(self.project)
        self.assertEqual((links, bad), ({self.sha: ("caltrans", "req-gone")}, 0))
        with run_stages.snapshot_ro(self.database) as con:
            report = ri.collect(con, self.processing, None, run_id="test-run", links=links)
        item = self.item(report, self.sha)
        self.assertIsNone(item["request_id"])
        self.assertIn("message_link_request_missing", item["diagnostics"])
        self.assertIn("message_link_request_missing", report["diagnostics"])

    def test_domain_candidates_when_no_agency_is_known(self):
        with store.ledger(self.database) as con:
            con.execute("UPDATE agencies SET portal_host='other.example' WHERE id='other'")
            con.commit()
        with run_stages.snapshot_ro(self.database) as con:
            self.assertEqual(unmatched._candidates(con, None, "mail.other.example"),
                             [{"request_id": "req-x", "agency_id": "other"}])
            self.assertEqual(unmatched._candidates(con, None, "unknown.example"), [])

    def item(self, report, sha):
        return next(i for i in report["items"] if i["original_sha256"] == sha)

    def test_writer_failure_warns_and_keeps_every_earlier_output(self):
        with patch("campaign_tool.records.outcomes.unmatched.write_run_unmatched", side_effect=OSError("boom")):
            code, line = self.run_outputs()
        self.assertEqual((code, line["status"], line["error_code"]), (0, "warn", "OUTPUTS_UNMATCHED_UNAVAILABLE"))
        self.assertTrue((self.outcomes() / "received-items.json").exists())
        self.assertTrue((self.project / "run-outputs" / "test-run.json").exists())
        self.assertFalse((self.outcomes() / "unmatched-messages.json").exists())


if __name__ == "__main__":
    unittest.main()
