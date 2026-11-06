"""Screen C: documents and findings, on synthetic run outputs (two invented agencies)."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from campaign_tool.records import admin_panel as ap
from campaign_tool.records import console_documents as cd

RUN = "run_1"
NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
H1, H2 = "1" * 64, "2" * 64


def put(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def build(project, drop=()):
    out = project / "run-outputs" / RUN
    docs = {
        "outcomes/received-items.json": {"schema": "records-received-item-v1", "run_id": RUN, "items": [
            {"item_id": "i-b", "original_sha256": H2, "agency_id": "beta-so", "request_id": "R-2",
             "received_at": "2026-10-01T00:00:00Z", "kind": "denial", "findings": [], "ocr_review_status": None},
            {"item_id": "i-a", "original_sha256": H1, "agency_id": "alpha-pd", "request_id": "R-1",
             "received_at": "2026-09-01T00:00:00Z", "kind": "records", "findings": [{}],
             "ocr_review_status": "pending"}]},
        "outcomes/release-links.json": {"schema": "records-release-links-v1", "run_id": RUN, "links": [
            {"link_id": "L" * 64, "host": "files.example.test", "platform": "sharepoint",
             "url_redacted": "https://files.example.test/x", "agency_ids": ["alpha-pd"],
             "sources": [{"received_at": "2026-09-01T00:00:00Z"}], "expiry": {"date": "2026-11-01"},
             "expiry_state": "stated"},
            {"link_id": "M" * 64, "host": "other.example.test", "platform": "govqa", "url_redacted": "https://other.example.test/",
             "agency_ids": [], "sources": [], "expiry": None, "expiry_state": "none_stated"}]},
        "outcomes/gap-proposals.json": {"schema": "records-gap-proposals-v1", "run_id": RUN, "agencies": [
            {"agency_id": "alpha-pd", "requests": [{"request_id": "R-1", "proposals": [
                {"proposal_id": "p2", "type": "misuse_lead", "request_id": "R-1", "quote": "low <b>q</b>", "page": 3,
                 "policy_section": None, "rule_ref": None, "confidence": "low", "question": "Q2?",
                 "review_bucket": "review_list", "review_reasons": [], "review_required": True},
                {"proposal_id": "p1", "type": "policy_gap", "request_id": "R-1", "quote": "high q", "page": 2,
                 "policy_section": {"section": "4.1"}, "rule_ref": None, "confidence": "high", "question": "Q1?",
                 "review_bucket": "proposal", "review_reasons": [], "review_required": True}]}]}]},
        "outcomes/response-items.json": {"schema": "records-response-items-v1", "run_id": RUN, "requests": [
            {"agency_key": "alpha-pd", "request_id": "R-1", "flags": [], "asks": [
                {"ask_id": "a1", "text": "The vendor contract", "rating": "missing", "evidence": [], "flags": []},
                {"ask_id": "a2", "text": "Search logs for January", "rating": "answered",
                 "evidence": [{"item_id": "i-a", "page": 1, "quote": "log"}], "flags": []}]}]},
        "text-state.json": {"schema": "records-text-state-v1", "run_id": RUN, "records": [
            {"sha256": H1, "state": "ocr_unreviewed", "words": "x"}]},
    }
    for name, doc in docs.items():
        if name not in drop:
            put(out / name, doc)


class ScreenC(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)

    def model(self, **kw):
        return cd.load(self.project, RUN, pages=False, **kw)

    def test_per_agency_slice_and_tier_order(self):
        build(self.project)
        m = self.model()
        self.assertEqual(cd.agencies(m), ["alpha-pd", "beta-so"])
        a = cd.view(m, "alpha-pd", NOW)
        self.assertEqual([i["item_id"] for i in a["items"]], ["i-a"])
        self.assertEqual(a["items"][0]["text_state"], "ocr_unreviewed")
        self.assertEqual(a["items"][0]["answers_asks"], ["a2"])
        self.assertEqual([x["ask_id"] for x in a["dispositions"][0]["asks"]], ["a2", "a1"])  # tier 1 logs first
        self.assertEqual([p["proposal_id"] for p in a["findings"]], ["p1", "p2"])  # high confidence first
        self.assertTrue(all(p["publishable"] is False for p in a["findings"]))
        self.assertEqual(cd.view(m, "beta-so", NOW)["findings"], [])

    def test_release_links_expiry_and_unattributed(self):
        build(self.project)
        m = self.model()
        link = cd.view(m, "alpha-pd", NOW)["release_links"][0]
        self.assertEqual((link["host"], link["status"], link["fetched"]), ("files.example.test", "expired", False))
        self.assertEqual([l["host"] for l in cd.view(m, None, NOW)["release_links"]], ["other.example.test"])

    def test_html_label_escaping_and_no_fetch_wording(self):
        build(self.project)
        page = cd.render(self.model(), NOW)
        self.assertIn(cd.LABEL, page)
        self.assertIn("&lt;b&gt;q&lt;/b&gt;", page)
        self.assertNotIn("<b>q</b>", page)
        self.assertIn("not fetched", page)

    def test_each_missing_source_is_named_not_empty_success(self):
        build(self.project, drop=("outcomes/gap-proposals.json", "outcomes/release-links.json"))
        m = self.model()
        src = cd.sources(m)
        self.assertEqual(src["proposals"]["code"], "missing")
        self.assertEqual(src["links"]["state"], "unavailable")
        self.assertEqual(src["received"]["state"], "ok")
        page = cd.render(m, NOW)
        self.assertIn("proposals: unavailable (missing)", page)
        self.assertEqual(len(cd.view(m, "alpha-pd", NOW)["items"]), 1)

    def test_stale_or_wrong_schema_source_is_refused(self):
        build(self.project)
        out = self.project / "run-outputs" / RUN / "outcomes"
        doc = json.loads((out / "gap-proposals.json").read_text())
        doc["run_id"] = "other"
        put(out / "gap-proposals.json", doc)
        put(out / "release-links.json", {"schema": "nope", "run_id": RUN})
        (out / "response-items.json").write_text("{bad")
        src = cd.sources(self.model())
        self.assertEqual(src["proposals"]["code"], "run_mismatch")
        self.assertEqual(src["links"]["code"], "schema_invalid")
        self.assertEqual(src["response"]["code"], "unreadable")
        self.assertEqual(src["tiers"]["state"], "unavailable")

    def test_symlinked_source_is_refused(self):
        build(self.project)
        out = self.project / "run-outputs" / RUN / "outcomes"
        real = out / "gap-proposals.json"
        moved = self.project / "elsewhere.json"
        real.rename(moved)
        real.symlink_to(moved)
        self.assertEqual(self.model()["proposals"]["code"], "not_regular_file")

    def test_bad_run_id_and_cli(self):
        with self.assertRaises(ValueError):
            cd.load(self.project, "../x")
        build(self.project)
        self.assertEqual(cd.main(["--project", str(self.project), "--run-id", "a/b"]), 2)
        self.assertEqual(cd.main(["--project", str(self.project), "--run-id", RUN, "--agency", "x y"]), 2)
        target = self.project / "page.html"
        self.assertEqual(cd.main(["--project", str(self.project), "--run-id", RUN, "--html", str(target),
                                  "--now", "2026-11-05T00:00:00Z"]), 0)
        self.assertIn("alpha-pd", target.read_text())
        self.assertEqual(cd.main(["--project", str(self.project), "--run-id", RUN, "--html", str(target)]), 2)  # no overwrite

    def test_panel_route(self):
        build(self.project)
        server = ap.build_server(self.project, RUN, actor="op", system=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
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

        status, body = get("/documents?agency=alpha-pd")
        self.assertEqual(status, 200)
        self.assertIn("alpha-pd", body)
        self.assertNotIn("beta-so", body)
        self.assertEqual(get("/documents?agency=bad%20id")[0], 400)
        self.assertEqual(get("/documents", host="evil.test")[0], 403)
        self.assertEqual(get("/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
