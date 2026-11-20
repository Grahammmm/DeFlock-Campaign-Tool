"""Screen C: documents and findings, on synthetic run outputs (two invented agencies)."""
import hashlib
import http.client
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.records import admin_panel as ap, console_layout
from campaign_tool.records import console_documents as cd, console_export, console_ocr, finding_review
from campaign_tool.records import console_cited_pages, console_snapshot

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


class PagePathRedaction(unittest.TestCase):
    def test_paths_are_omitted_from_rendered_source(self):
        from campaign_tool.records import console_page_page

        paths = ("/secret.txt", "./private.txt", "../private.txt",
                 "/home/user/My Documents/secret.pdf", "C:\\My Documents\\secret.pdf")
        row = {"agency": "alpha-pd", "received_at": "2026-09-01T06:30:00Z", "route": "native",
               "confidence_band": "not_applicable", "review_state": "not_applicable", "citations": [],
               "text": "\n".join("See " + path + " for details." for path in paths)}
        rendered = console_page_page._render(H1[:12], 1, row, None)
        for path in paths:
            with self.subTest(path=path):
                self.assertNotIn(path, rendered)
        self.assertEqual(rendered.count("See [omitted] for details."), len(paths))


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
        self.assertIn("proposals: unavailable", page)
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

    def test_only_produced_asks_count_as_answered(self):
        build(self.project)
        out = self.project / "run-outputs" / RUN / "outcomes" / "response-items.json"
        doc = json.loads(out.read_text())
        ev = [{"item_id": "i-a", "page": 1, "quote": "x"}]
        doc["requests"][0]["asks"] += [
            {"ask_id": "a3", "text": "w", "rating": "withheld", "evidence": ev, "flags": []},
            {"ask_id": "a4", "text": "n", "rating": "no_records", "evidence": ev, "flags": []}]
        put(out, doc)
        item = cd.view(self.model(), "alpha-pd", NOW)["items"][0]
        self.assertEqual(item["answers_asks"], ["a2"])

    def test_malformed_source_shape_degrades_only_that_source(self):
        build(self.project)
        out = self.project / "run-outputs" / RUN / "outcomes"
        doc = json.loads((out / "release-links.json").read_text())
        doc["links"] = [None]
        put(out / "release-links.json", doc)
        prop = json.loads((out / "gap-proposals.json").read_text())
        prop["agencies"][0]["requests"] = [None]
        put(out / "gap-proposals.json", prop)
        m = self.model()
        src = cd.sources(m)
        self.assertEqual(src["links"]["code"], "shape_invalid")
        self.assertEqual(src["proposals"]["code"], "shape_invalid")
        self.assertEqual(src["received"]["state"], "ok")
        self.assertIn("alpha-pd", cd.agencies(m))
        self.assertIn("release links: unavailable", cd.render(m, NOW))

    def test_each_remaining_malformed_shape_isolates_one_source(self):
        cases = [("received-items.json", "items", None, "received"), ("release-links.json", "links", 1, "links"),
                 ("release-links.json", "links", None, "links"), ("gap-proposals.json", "agencies", None, "proposals"),
                 ("response-items.json", "requests", None, "response")]
        for name, field, bad, source in cases:
            with self.subTest(file=name, field=field, bad=bad):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                project = Path(tmp.name)
                build(project)
                path = project / "run-outputs" / RUN / "outcomes" / name
                doc = json.loads(path.read_text())
                doc[field] = bad
                put(path, doc)
                m = cd.load(project, RUN, pages=False)
                self.assertEqual(cd.sources(m)[source]["code"], "shape_invalid")
                page = cd.render(m, NOW)
                self.assertIn(console_layout.plain([source])[0] + ": unavailable", page)
                self.assertIn("alpha-pd", page)
        # nested null list and text-state record missing its state
        build(self.project)
        out = self.project / "run-outputs" / RUN
        prop = json.loads((out / "outcomes" / "gap-proposals.json").read_text())
        prop["agencies"][0]["requests"][0]["proposals"] = None
        put(out / "outcomes" / "gap-proposals.json", prop)
        put(out / "text-state.json", {"schema": "records-text-state-v1", "run_id": RUN, "records": [{"sha256": H1}]})
        m = self.model()
        src = cd.sources(m)
        self.assertEqual((src["proposals"]["code"], src["textstate"]["code"]), ("shape_invalid", "shape_invalid"))
        self.assertEqual(src["received"]["state"], "ok")
        cd.render(m, NOW)

    def test_malformed_scalar_fields_isolate_one_source(self):
        cases = [
            ("received-items.json", lambda d: d["items"][0].__setitem__("findings", 1), "received"),
            ("received-items.json", lambda d: d["items"][0].__setitem__("original_sha256", []), "received"),
            ("received-items.json", lambda d: d["items"][0].__setitem__("item_id", []), "received"),
            ("gap-proposals.json", lambda d: d["agencies"][0]["requests"][0]["proposals"][0].__setitem__("confidence", []),
             "proposals"),
            ("response-items.json", lambda d: d["requests"][0].__setitem__("agency_key", []), "tiers"),
            ("response-items.json", lambda d: d["requests"][0]["asks"][0].__setitem__("evidence", 1), "tiers"),
        ]
        for name, fn, source in cases:
            with self.subTest(file=name, source=source):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                project = Path(tmp.name)
                build(project)
                path = project / "run-outputs" / RUN / "outcomes" / name
                doc = json.loads(path.read_text())
                fn(doc)
                put(path, doc)
                m = cd.load(project, RUN, pages=False)
                # response-tier shapes are refused early by item_tiers (RF1450-8)
                expected = "response_items_invalid" if source == "tiers" else "shape_invalid"
                self.assertEqual(cd.sources(m)[source]["code"], expected)
                page = cd.render(m, NOW)
                self.assertIn(console_layout.plain([source])[0] + ": unavailable", page)
                self.assertIn("alpha-pd", page)

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

    def panel(self, *args, **kwargs):
        server = ap.build_server(*args, actor="op", system=False, **kwargs)
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

        return get

    def edit(self, relative, update):
        path = self.project / "run-outputs" / RUN / relative
        doc = json.loads(path.read_text())
        update(doc)
        put(path, doc)

    def test_panel_route(self):
        build(self.project)
        self.edit("outcomes/received-items.json", lambda doc: doc["items"].append(
            {"item_id": "i-unbound", "agency_id": "alpha-pd"}))
        get = self.panel(self.project, RUN)
        status, body = get("/documents?agency=alpha-pd")
        self.assertEqual(status, 200)
        self.assertIn("alpha-pd", body)
        self.assertNotIn("beta-so", body)
        self.assertIn('href="/holds"', body)
        self.assertIn("i-unbound", body)
        self.assertEqual(body.count('href="/document?id='), 1)
        self.assertIn(f'href="/document?id={H1[:12]}"', body)
        self.assertNotIn(H1, body)
        self.assertEqual(get("/documents?agency=bad%20id")[0], 400)
        self.assertEqual(get("/documents", host="evil.test")[0], 403)
        self.assertEqual(get("/nope")[0], 404)

    def test_document_detail_prefix_is_exact_and_unambiguous(self):
        build(self.project)
        received = "outcomes/received-items.json"
        self.edit(received, lambda doc: doc["items"].append(dict(doc["items"][1], item_id="i-repeat")))
        get = self.panel(self.project, RUN)
        path = "/document?id=" + H1[:12]
        self.assertEqual(get(path)[0], 200)
        for bad in ("/document", "/document?id=abcdefABCDEF", "/document?id=" + H1, path + "&id=" + H1[:12],
                    path + "&extra=", path + "&", "/document?id=../../elsewhere"):
            with self.subTest(bad=bad):
                status, body = get(bad)
                self.assertEqual(status, 400)
                self.assertNotRegex(body, r"[0-9a-f]{64}|\.\./elsewhere")
        self.assertEqual(get("/document?id=" + "f" * 12)[0], 404)
        self.edit(received, lambda doc: doc["items"].append(dict(doc["items"][1], original_sha256=H1[:12] + "a" * 52)))
        status, body = get(path)
        self.assertEqual(status, 409)
        self.assertIn("more than one document", body)
        self.assertNotRegex(body, r"[0-9a-f]{64}")
        self.edit(received, lambda doc: doc.update(run_id="stale"))
        self.assertEqual(get(path)[0], 503)

    def test_document_detail_pin_parity_and_privacy(self):
        build(self.project)
        image, failed = "3" * 64, "4" * 64
        self.edit("outcomes/received-items.json", lambda doc: doc["items"].extend([
            {"original_sha256": image, "kind": "image-fixture.pdf", "agency_id": "alpha-pd/private"},
            {"original_sha256": failed, "request_id": "attic/private/" + H1}]))
        self.edit("text-state.json", lambda doc: doc["records"].extend([
            {"sha256": H2, "state": "text_reviewed"}, {"sha256": image, "state": "awaiting_ocr"},
            {"sha256": failed, "state": "could_not_be_read", "words": "raw document canary"}]))
        self.edit("outcomes/response-items.json", lambda doc: doc["requests"][0]["asks"].extend([
            {"ask_id": "a3", "rating": "withheld", "evidence": [{"item_id": "i-a"}]},
            {"ask_id": "foreign-ask", "rating": "answered", "evidence": [{"item_id": "i-b"}]}]))
        self.edit("outcomes/response-items.json", lambda doc: doc["requests"].append(
            {"agency_key": "beta-so", "request_id": "R-2", "asks": [
                {"ask_id": "beta-ask", "rating": "answered", "evidence": [{"item_id": "i-b"}]}]}))
        # A shared link's merged date is specifically cited to beta, not every source it mentions.
        self.edit("outcomes/release-links.json", lambda doc: doc["links"][0].update(
            sources=[{"original_sha256": H1, "item_id": "i-a"}, {"original_sha256": H2, "item_id": "i-b"}],
            expiry={"date": "2025-11-01", "original_sha256": H2}))
        ledger = self.project / console_export.LEDGER
        ledger.parent.mkdir()
        with closing(sqlite3.connect(ledger)) as db, db:
            db.execute("CREATE TABLE page_state(original_sha256, page_no, method, status, reason, needs_visual_review, "
                       "confidence, derivative_sha256)")
            db.executemany("INSERT INTO page_state VALUES (?, ?, ?, ?, NULL, 1, ?, ?)", [
                (H2, 1, "legacy-intake:pdf", "ok", None, H2),
                (H1, 1, "local-ocr-3", "partial", 47, H1), (H1, 2, "local-ocr-3", "partial", 93, H1)])

        root = self.project / "hosting"
        name = "20261105T000000Z-" + RUN
        pinned = root / "exports" / name
        root.mkdir(mode=0o700)
        pinned.mkdir(parents=True, mode=0o700)
        shutil.copytree(self.project / "run-outputs", pinned / "run-outputs")
        (pinned / ledger.parent.name).mkdir()
        shutil.copyfile(ledger, pinned / console_export.LEDGER)
        # Real pinned snapshot reader and file verification; no export builder/trust access.
        pid = "1" * 16
        put(pinned / "console-model/ocr-pages.json", {
            "schema": console_export.MODEL_SCHEMA, "source_project_id": pid, "state": "ok", "truncated": True,
            "counts": {"pending": 2}, "rows": [
                {"original_sha256": H1, "page_no": n, "confidence": conf, "state": "pending",
                 "text": "raw document canary", "id": f"{H1}-{n}"} for n, conf in [(1, 47), (2, 93)]]})
        files = {str(p.relative_to(pinned)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in pinned.rglob("*") if p.is_file()}
        put(pinned / "EXPORT.json", {"schema": console_export.SCHEMA, "run_id": RUN, "source_project_id": pid,
                                     "exported_at": "2026-11-05T00:00:00Z", "files": files})
        (root / "current").symlink_to(Path("exports") / name)
        analysis = self.project / "analysis"
        analysis.mkdir()
        live = self.panel(self.project, RUN)
        # Analysis cannot overlap the live project; enable it only on the export console.
        exported = self.panel(export_root=str(root), run_id="auto", analysis_root=str(analysis.resolve()))

        def findings(_root, *, agency):
            return [{"agency": agency, "topic": topic, "review_state": "unreviewed", "claim": "raw finding canary",
                     "facts": [{"source_sha256": digest, "quote": "raw quote canary"}]}
                    for topic, digest in [("Search oversight", H1), ("Wrong original", H1[:12] + "e" * 52)]]

        with mock.patch.object(finding_review, "list_findings", side_effect=findings) as listed, \
                mock.patch.object(console_ocr, "_page_text", side_effect=AssertionError("opened evidence text")):
            for digest, expected in (
                (H1, ("alpha-pd", "R-1", "2026-08-31 17:00 PDT", "<dd>records</dd>", "<dd>2</dd>",
                      "<dd>OCR — awaiting visual review</dd>",
                      "<td>1</td><td>Below 80", "<td>2</td><td>80 or above", "Awaiting review", 'href="/ocr-review"',
                      "<td>a2</td><td>produced", "<td>a3</td><td>withheld")),
                (H2, ("beta-so", "<dd>1</dd>", "<dd>Native</dd>", "2025-11-01", "Expired", "<td>beta-ask</td>")),
                (image, ("<dd>Awaiting OCR</dd>", "<dd>Unknown</dd>")), (failed, ("<dd>Read error</dd>",))):
                with self.subTest(digest=digest[:12]):
                    path = "/document?id=" + digest[:12]
                    status, local = live(path)
                    self.assertEqual(status, 200)
                    # Change a live run fact after the export. Export cannot borrow it or read another run.
                    if digest == H1:
                        self.edit("text-state.json", lambda doc: doc["records"][0].update(state="waiting"))
                    status, pinned_page = exported(path)
                    self.assertEqual(status, 200)
                    for body in (local, pinned_page):
                        for marker in expected:
                            self.assertIn(marker, body)
                        for private in ("raw document canary", "raw finding canary", "raw quote canary", "image-fixture.pdf",
                                        "alpha-pd/private", "attic/private", "files.example.test", "foreign-ask", "Wrong original"):
                            self.assertNotIn(private, body)
                        self.assertNotRegex(body, r"[0-9a-fA-F]{64}")
                        self.assertNotIn("<form", body)
                    if digest == H1:
                        self.assertNotIn("2025-11-01", pinned_page)
                        self.assertIn("Only part of the OCR review queue is in this export.", pinned_page)
                        self.assertIn("Search oversight", pinned_page)
                        self.assertNotIn("Search oversight", local)
        self.assertEqual({call.kwargs["agency"] for call in listed.call_args_list}, {"alpha-pd", "beta-so"})

    def test_cited_page_viewer_export_and_live_parity(self):
        build(self.project)
        hostile = "<script>alert(1)</script> & \"q\" " + "d" * 64 + " /home/user/secret/file.txt"
        row = lambda sha, page, text, route, band, review, cites: {
            "original_sha256": sha, "page": page, "agency": "alpha-pd", "received_at": "2026-09-01T06:30:00Z", "route": route,
            "confidence_band": band, "review_state": review, "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "citations": cites}
        kinds = [{"kind": "fact", "id": "alpha-pd/x/1", "label": "retention"}, {"kind": "gap", "id": "p1", "label": "policy_gap"},
                 {"kind": "deadline", "id": "i-a/1", "label": "2026-11-30"}]
        cited = {"schema": "records-console-cited-pages-v1", "source_project_id": "1" * 16, "state": "ok", "run_id": RUN,
                 "pages": [row(H1, 2, "Native line one\nline two", "native", "not_applicable", "not_applicable", kinds),
                           row(H1, 3, hostile, "ocr", "below_80", "pending", kinds[:1])],
                 "not_exported": [{"original_sha256": H1, "page": 5, "reason": "page_bytes"}]}
        self.edit("outcomes/gap-proposals.json", lambda doc: doc["agencies"][0]["requests"][0]["proposals"][1].update(
            item_id="i-a", evidence_kind="document"))
        root, name = self.project / "hosting", "20261105T000000Z-" + RUN
        pinned = root / "exports" / name
        root.mkdir(mode=0o700)
        pinned.mkdir(parents=True, mode=0o700)
        shutil.copytree(self.project / "run-outputs", pinned / "run-outputs")
        (pinned / "console-model").mkdir()
        (pinned / "console-model" / "cited-pages.json").write_bytes(console_cited_pages.encode(cited))

        def seal():
            names = {str(p.relative_to(pinned)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in pinned.rglob("*") if p.is_file() and p.name != "EXPORT.json"}
            put(pinned / "EXPORT.json", {"schema": console_export.SCHEMA, "run_id": RUN, "source_project_id": "1" * 16,
                                         "exported_at": "2026-11-05T00:00:00Z", "files": names})
        seal()
        (root / "current").symlink_to(Path("exports") / name)
        exported = self.panel(export_root=str(root), run_id="auto")
        with mock.patch.object(console_snapshot.LiveSource, "cited_pages", return_value=cited):
            live = self.panel(self.project, RUN)
            documents = live("/documents?agency=alpha-pd")[1]
            self.assertEqual(documents.count('href="/page?doc='), 1)  # only the document-backed proposal row
            self.assertIn(f'href="/page?doc={H1[:12]}&amp;p=2"', documents)
            self.assertEqual(documents.count('href="/document?id='), 1)
            for get in (live, exported):
                status, body = get(f"/page?doc={H1[:12]}&p=2")
                self.assertEqual(status, 200)
                for marker in ("Private; source text, review required; not for publication", "alpha-pd", "2026-08-31 23:30 PDT",
                               "<dd>Native</dd>", "<pre>Native line one\nline two</pre>", "Fact statement: retention",
                               "Gap proposal: policy_gap", "Deadline: 2026-11-30"):
                    self.assertIn(marker, body)
                status, body = get(f"/page?doc={H1[:12]}&p=3")
                self.assertEqual(status, 200)
                for marker in ("<dd>OCR</dd>", "Below 80", "Awaiting review", "&lt;script&gt;alert(1)&lt;/script&gt; &amp;"):
                    self.assertIn(marker, body)
                self.assertNotIn("<script>", body)
                self.assertNotRegex(body, r"[0-9a-fA-F]{64}")
                self.assertNotIn("/home/user", body)
                self.assertNotIn("<form", body)
                status, body = get(f"/page?doc={H1[:12]}&p=5")
                self.assertEqual(status, 200)
                self.assertIn("larger than the export's per-page limit", body)
                self.assertEqual(get(f"/page?doc={H1[:12]}&p=4")[0], 404)  # not cited
                self.assertEqual(get("/page?doc=" + "f" * 12 + "&p=2")[0], 404)
                for bad in ("/page", f"/page?doc={H1[:12]}", f"/page?doc={H1}&p=2", f"/page?doc={H1[:12]}&p=0",
                            f"/page?doc={H1[:12]}&p=2&p=3", f"/page?doc={H1[:12]}&p=2&x=1", f"/page?doc={H1[:12]}&p=%d" % 10 ** 12):
                    with self.subTest(bad=bad):
                        self.assertEqual(get(bad)[0], 400)
            # A second received document sharing the prefix, even one that cites no page, makes it ambiguous.
            self.edit("outcomes/received-items.json", lambda doc: doc["items"].append(
                {"item_id": "i-c", "original_sha256": H1[:12] + "a" * 52, "agency_id": "alpha-pd"}))
            self.assertEqual(live(f"/page?doc={H1[:12]}&p=2")[0], 409)
        # An older export without the model is unavailable, never "not cited".
        (pinned / "console-model" / "cited-pages.json").unlink()
        seal()
        self.assertEqual(self.panel(export_root=str(root), run_id="auto")(f"/page?doc={H1[:12]}&p=2")[0], 503)


if __name__ == "__main__":
    unittest.main()
