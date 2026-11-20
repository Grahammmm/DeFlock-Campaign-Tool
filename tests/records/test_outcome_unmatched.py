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
        code, first = self.run_outputs()
        self.assertEqual(code, 0)
        self.assertEqual(first["draft_queue_counts"]["message_links"], 1)
        raw = (self.outcomes() / "unmatched-messages.json").read_text()
        report = json.loads(raw)
        self.assertEqual(report["schema"], "records-unmatched-v1")
        (entry,) = report["messages"]
        self.assertEqual((entry["message_sha256"], entry["received_date"], entry["sender_domain"], entry["attachment_count"]),
                         (self.sha, "2041-02-03", "caltrans.example", 0))
        self.assertEqual(entry["candidates"], [{"request_id": "req-b", "agency_id": "caltrans", "why": "same agency"},
                                               {"request_id": "req-a", "agency_id": "caltrans", "why": "same agency"}])  # newest first
        for text in (SUBJECT, SENDER, "clerk-local", "public records request"):
            self.assertNotIn(text, raw)
        self.assertEqual(stat.S_IMODE((self.outcomes() / "unmatched-messages.json").stat().st_mode), 0o600)
        row = self.row()
        self.assertEqual((row["status"], row["item_id"]), ("pending", self.sha))
        for text in (SUBJECT, SENDER):
            self.assertNotIn(text, json.dumps(row))
        self.assertEqual(self.run_outputs()[1]["draft_queue_counts"]["message_links"], 0)  # a replay queues none
        self.row()  # still exactly one

    def test_console_shows_the_decision_and_approval_ties_the_next_run(self):
        self.run_outputs()
        server, http = self.server()
        ident = self.row()["id"]
        status, _, page = http.send("GET", "/review?id=" + ident, headers={"Content-Length": None, "Content-Type": None})
        self.assertEqual(status, 200)
        self.assertIn("Tie this message to a request", page)
        self.assertEqual(page.count("&amp;choice=req-"), 2)  # one link per candidate; no signable text before a selection
        self.assertNotIn('id="approval-payload"', page)
        sig = self.signer.sign(aq.approval_payload(self.project, ident, choice="req-b"))
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
        self.assertEqual(self.post(http, server, "approve", ident, signature=sig, choice="req-a"), 403)  # the signature names req-b
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
        sig = self.signer.sign(aq.approval_payload(self.project, ident, choice="req-b"))
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
        sig = self.signer.sign(aq.approval_payload(self.project, ident, choice="req-a"))
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
                             [{"request_id": "req-x", "agency_id": "other", "why": "portal domain"}])
            self.assertEqual(unmatched._candidates(con, None, "unknown.example"), [])

    def mail(self, mid, sha, subject, **over):
        data = {"schema": "verified-eml-headers-v1", "source_original_sha256": sha, "parser_defects": [],
                "fields": [["From", "Clerk <a@mail.unknown.example.invalid>"], ["Subject", subject]]}
        data.update(over)
        with store.ledger(self.database) as con:
            con.execute("INSERT OR IGNORE INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (sha, 1, None, None, "agency_original", "in_scope", "/x", "pending", None, "{}"))
            con.execute("INSERT INTO mail_messages VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (mid, "acct", "INBOX", 1, 1000 + len(mid) * 100 + sum(map(ord, mid)), "<%s@example.test>" % mid, sha, json.dumps(data), "2041-02-03T10:00:00Z", "verified"))
            con.commit()

    def refs(self, **refs):
        with store.ledger(self.database) as con:
            for rid, ref in refs.items():
                con.execute("UPDATE requests SET external_ref=? WHERE id=?", (ref, rid.replace("_", "-")))
            con.commit()

    def cands(self, sha, agency=None, domain=None):
        with run_stages.snapshot_ro(self.database) as con:
            return unmatched._candidates(con, agency, domain, sha)

    def test_subject_reference_from_unknown_mail_domain_is_first(self):
        self.refs(req_a="REF-2041-001")
        with store.ledger(self.database) as con:
            con.execute("UPDATE mail_messages SET headers=? WHERE id='m1'", (json.dumps(
                {"schema": "verified-eml-headers-v1", "source_original_sha256": self.sha, "parser_defects": [],
                 "fields": [["From", "c@unknown.example.invalid"], ["Subject", "Re: [ref-2041-001] PRIVATE-SUBJECT-SENTINEL"]]}),))
            con.commit()
        self.assertEqual(self.cands(self.sha, None, "unknown.example.invalid"),
                         [{"request_id": "req-a", "agency_id": "caltrans", "why": "reference REF-2041-001 in the subject"}])
        self.assertEqual(self.run_outputs()[0], 0)
        raw = (self.outcomes() / "unmatched-messages.json").read_text()
        self.assertNotIn("PRIVATE-SUBJECT-SENTINEL", raw)
        self.assertNotIn("private-subject", raw.lower())
        self.assertIn("reference REF-2041-001 in the subject", raw)
        row = self.row()
        self.assertIn("1. request req-a (agency caltrans) \u2014 reference REF-2041-001 in the subject", row["body"])
        self.assertNotIn("PRIVATE-SUBJECT-SENTINEL", json.dumps(row))
        self.assertFalse((self.project / unmatched.LINKS_NAME).exists())

    def test_reference_token_boundaries_and_dedup_order(self):
        self.refs(req_a="REF-1234", req_b="R.9", req_x="AB")
        cases = {"Re: [ref-1234]": ["req-a"], "ref-1234": ["req-a"], "XREF-1234Y": [], "ref-1234_": [], "REF-12345": [],
                 "a ref-1234, r.9": ["req-a", "req-b"], "RX9 and AB": []}
        for n, (subject, expected) in enumerate(cases.items()):
            sha = "%064x" % (n + 1)
            self.mail("mx%d" % n, sha, subject)
            got = [c["request_id"] for c in self.cands(sha, None, "none.example.invalid") if c["why"].startswith("reference")]
            self.assertEqual(sorted(got), sorted(expected), subject)
        # same request by reference and agency appears once with the stronger reason; cap unchanged; reference tier first
        sha = "%064x" % 99
        self.mail("mdup", sha, "ref-1234")
        got = self.cands(sha, "caltrans")
        self.assertEqual([(c["request_id"], c["why"]) for c in got],
                         [("req-a", "reference REF-1234 in the subject"), ("req-b", "same agency")])

    def test_no_reference_unknown_domain_is_unchanged(self):
        self.refs(req_a="REF-1234")
        sha = "%064x" % 7
        self.mail("mnone", sha, "Your request was received")
        self.assertEqual(self.cands(sha, None, "unknown.example.invalid"), [])
        self.assertEqual(self.cands(sha, None, None), [])
        item = {"message_sha256": sha, "received_date": None, "sender_domain": None, "attachment_count": 0, "candidates": [],
                "agency_id": None}
        self.assertIn("No candidate request was found; you can only mark it as not a response.", unmatched._draft(item)["body"])

    def test_bad_subject_headers_give_no_reference_tier(self):
        self.refs(req_a="REF-1234")
        bad = {"dup": {"fields": [["Subject", "REF-1234"], ["subject", "x"]]}, "nonstr": {"fields": [["Subject", ["REF-1234"]]]},
               "defects": {"parser_defects": ["x"]}, "schema": {"schema": "other"}, "mismatch": {"source_original_sha256": "0" * 64}}
        for n, (name, over) in enumerate(bad.items()):
            sha = "%064x" % (200 + n)
            self.mail("mb" + name, sha, "REF-1234", **over)
            self.assertEqual(self.cands(sha, "caltrans")[0]["why"], "same agency", name)
        with store.ledger(self.database) as con:
            con.execute("UPDATE mail_messages SET headers='{not json' WHERE id='m1'")
            con.commit()
        self.assertEqual({c["why"] for c in self.cands(self.sha, "caltrans")}, {"same agency"})
        self.assertEqual({c["why"] for c in self.cands("%064x" % 5, "caltrans")}, {"same agency"})  # no mail row

    def test_legacy_draft_candidate_without_why_still_renders(self):
        entry = {"message_sha256": self.sha, "received_date": None, "sender_domain": None, "attachment_count": 0, "agency_id": None,
                 "candidates": [{"request_id": "req-a", "agency_id": "caltrans"}]}
        self.assertIn("\n1. request req-a (agency caltrans)", unmatched._draft(entry)["body"] + "\n")
        self.assertNotIn("\u2014", unmatched._draft(entry)["body"])

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
