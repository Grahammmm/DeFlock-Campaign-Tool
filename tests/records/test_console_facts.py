"""GET /facts over a synthetic agency-factsheets.json shaped like outcomes/factsheets.py output (DF1117L-02)."""
import json
import re
import threading
import unittest
from unittest import mock

from campaign_tool.records import admin_panel as ap, console_page_facts as page
from campaign_tool.records.outcomes import factsheets as fs
from tests.records.test_console_runs import fetch
from tests.records.test_console_snapshot import RUN, Panel

DOC = ["a1b2c3d4e5f6" + "0" * 52, "b2c3d4e5f6a1" + "1" * 52, "c3d4e5f6a1b2" + "2" * 52]  # pragma: allowlist secret (synthetic document ids, not credentials)
UNIT, PRIVATE = "f" * 64, "/home/user/private/processing/ledger.sqlite"
AGENCIES = ("synthetic-mesa-pd", "synthetic-harbor-pd", "synthetic-cedar-pd")


def statement(quote, doc=0, page_no=1):
    return {"quote": quote, "citation": {"document_sha256": DOC[doc], "page": page_no, "unit_id": UNIT, "text_sha256": DOC[1], "payload_sha256": DOC[2]}}


def sheet(agency, found=None, gaps=()):
    fields = {k: {"status": page.NOT_FOUND, "statements": []} for k in fs.FIELDS}
    for key, quotes in (found or {}).items():
        fields[key] = {"status": page.FOUND, "statements": quotes}
    return {"agency_id": agency, "agency": agency.replace("synthetic-", "").upper(), "document_sha256s": DOC[:1], "gaps": [dict(code=c, document_sha256=DOC[0], unit_id=UNIT) for c in gaps], "fields": fields}


def doc(run=RUN, agencies=None):
    mesa = sheet(AGENCIES[0], {"cost": [statement("Annual fee: $12,345.67.", 0, 3), statement("Cost: $900 per camera.", 1, 7)], "retention": [statement("Images are retained 30 days.", 0, 2)]},
                 ("page_locator_unavailable", "page_locator_unavailable", "statement_too_long"))
    harbor = sheet(AGENCIES[1], {"vendor": [statement("Vendor: Flock Safety", 1, 1)]})
    cedar = sheet(AGENCIES[2])
    return {"schema": fs.SCHEMA, "run_id": run, "review_required": True, "semantic_acceptance": False, "publication_ready": False,
            "basis": "Exact source excerpts; NOT FOUND means no supported page-cited statement, not absence of conduct.", "gaps": [],
            "agencies": [mesa, harbor, cedar] if agencies is None else agencies}


def main_html(html):
    return re.search(r"<h1>Agency fact sheets</h1>.*?(?=<footer|</main>)", html, re.S)[0]


class Facts(Panel):
    def put(self, data, project=None):
        path = (project or self.live) / "run-outputs" / RUN / "agency-factsheets.json"
        path.unlink(missing_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))

    def live_get(self, path, status=200):
        if not hasattr(self, "live_server"):
            self.live_server = ap.build_server(str(self.live), RUN, actor="operator", system=False)
            thread = threading.Thread(target=self.live_server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(lambda: (self.live_server.shutdown(), self.live_server.server_close(), thread.join()))
        code, html = fetch(self.live_server, path)
        self.assertEqual(code, status, (path, html[:300]))
        return html

    # Owner: the page's data boundary. Fails if the heading/basis, a matrix count or 'not found' cell, its link, the ten columns,
    # the 12-hex citation prefix and page, the Library link or the gap counts are lost, or another agency's text leaks into a detail.
    def test_facts_matrix_and_detail_cover_three_agencies(self):
        self.put(doc())
        html = self.live_get("/facts")
        self.assertLess(html.index(page.LEAD), html.index(page.BASIS))
        self.assertLess(html.index(page.BASIS), html.index("<table"))
        self.assertEqual(len(re.findall(r'<th scope="col">', html)), 11)
        self.assertRegex(html, r'<th scope="row">MESA-PD \(synthetic-mesa-pd\)</th><td><a href="/facts\?agency=synthetic-mesa-pd">not found</a></td>'
                               r'<td><a [^>]*>not found</a></td><td><a [^>]*>2</a></td>')
        self.assertEqual(html.count("not found</a>"), 10 * 3 - 3)  # 3 of 30 cells have supported statements
        self.assertEqual(html.count('href="/facts?agency=synthetic-cedar-pd"'), 10)
        detail = self.live_get("/facts?agency=" + AGENCIES[0])
        self.assertIn(page.LEAD, detail)
        self.assertIn(page.BASIS, detail)
        self.assertIn('<li>Annual fee: $12,345.67. <a href="/documents?agency=synthetic-mesa-pd">%s page 3</a></li>' % DOC[0][:12], detail)
        self.assertIn("%s page 7" % DOC[1][:12], detail)
        self.assertIn("<li>page_locator_unavailable: 2</li>", detail)
        self.assertIn("<li>statement_too_long: 1</li>", detail)
        self.assertEqual(detail.count("not found"), 8)
        self.assertNotIn("Vendor: Flock", detail)
        self.assertIn("<p>none recorded</p>", self.live_get("/facts?agency=" + AGENCIES[1]))

    # Owner: the agency filter. Fails if an unknown id renders an empty detail (200) instead of 404, or a blank, duplicate or
    # extra query value is accepted or reflected; unavailable data is never 404.
    def test_facts_unknown_agency_is_404_and_bad_filters_are_400(self):
        self.put(doc())
        html = self.live_get("/facts?agency=nobody-pd", 404)
        self.assertIn(page.LEAD, html)
        self.assertNotIn("nobody-pd", html)
        self.assertNotIn("<table", html)
        self.live_get("/facts?agency=" + AGENCIES[2])
        for query in ("agency=", "agency=a&agency=b", "extra=1", "agency", "agency=%s&extra=1" % AGENCIES[0], "agency=%20" + AGENCIES[0], "agency=nobody&zz"):
            with self.subTest(query):
                body = self.live_get("/facts?" + query, 400)
                self.assertNotIn("nobody", body)
                self.assertNotIn("<table", body)
        self.put(doc(agencies=[]))
        self.assertIn("list no agencies", self.live_get("/facts"))
        self.live_get("/facts?agency=" + AGENCIES[0], 404)
        self.put("{not json")
        self.assertIn("unavailable: invalid_json", self.live_get("/facts?agency=nobody"))  # membership is unknowable, not fabricated

    # Owner: output escaping and provenance hiding. Fails if markup is live, a quote is not cut at 300 characters, a full 64-hex
    # string, unit id or private path reaches the page, or ordinary source text (a street) is removed.
    def test_facts_caps_and_escapes_quote_without_exposing_provenance(self):
        long = "X" * 300 + " TAILMARK " + "Y" * 50
        hostile = ['<script>alert(1)</script> "><img src=x> at 12 Main St/Oak Ave', "See %s and %s unit %s" % (PRIVATE, DOC[2], UNIT), long]
        data = doc()
        data["agencies"][0]["fields"]["cost"]["statements"] = [statement(q, 0, 4) for q in hostile]
        data["agencies"][0]["agency"] = "<b>Mesa</b>"
        data["gaps"] = ["agency_registry_unavailable"]
        self.put(data)
        for route in ("/facts", "/facts?agency=" + AGENCIES[0]):
            html = self.live_get(route)
            for raw in ("<script>", "<img", "<b>", PRIVATE, "/home/user", "ledger.sqlite", DOC[2], UNIT, "TAILMARK"):
                self.assertNotIn(raw, html, route)
            self.assertIsNone(re.search(r"[0-9a-fA-F]{64}", html), route)
            self.assertIn("&lt;b&gt;Mesa&lt;/b&gt;", html)
            self.assertIn("Run-level coverage gaps: agency_registry_unavailable.", html)
        html = self.live_get("/facts?agency=" + AGENCIES[0])
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("at 12 Main St/Oak Ave", html)
        self.assertIn("X" * 300 + " (cut)", html)
        self.assertIn("%s page 4" % DOC[0][:12], html)

    # Owner: the load boundary. Fails if a wrong run/schema, a malformed statement/citation/status, a missing basis, a symlink
    # or an oversize file renders a matrix instead of a fixed code; the same-run valid file is the control.
    def test_facts_run_mismatch_and_bad_sources_are_unavailable(self):
        def mutate(fn):
            d = doc()
            fn(d)
            return d
        statements = lambda d: d["agencies"][0]["fields"]["cost"]["statements"]
        cases = [("run_mismatch", doc(run=RUN + "-other")), ("schema_invalid", mutate(lambda d: d.update(schema="x"))),
                 ("shape_invalid", mutate(lambda d: statements(d)[0]["citation"].update(document_sha256=DOC[0][:63]))),
                 ("shape_invalid", mutate(lambda d: statements(d)[0]["citation"].update(page=True))),
                 ("shape_invalid", mutate(lambda d: statements(d)[0].update(quote=5))),
                 ("shape_invalid", mutate(lambda d: d["agencies"][0]["fields"]["cost"].update(status="NOT FOUND"))),
                 ("shape_invalid", mutate(lambda d: d["agencies"][0]["fields"].pop("audits"))),
                 ("shape_invalid", mutate(lambda d: d.update(basis="Exact source excerpts."))),
                 ("shape_invalid", mutate(lambda d: d.update(basis=d["basis"] + "\ud800"))),
                 ("shape_invalid", mutate(lambda d: d["agencies"].append(d["agencies"][0]))),
                 ("invalid_json", "{not json"), ("invalid_json", '{"a": 1, "a": 2}')]
        for code, data in cases:
            with self.subTest(code=code):
                self.put(data)
                for route in ("/facts", "/facts?agency=" + AGENCIES[0]):
                    html = self.live_get(route)
                    self.assertIn("unavailable: " + code, html)
                    self.assertIn(page.LEAD, html)
                    self.assertIn(page.BASIS, html)
                    self.assertNotIn("<table", html)
                    self.assertNotIn(AGENCIES[1], html)
        path = self.live / "run-outputs" / RUN / "agency-factsheets.json"
        path.unlink()
        self.assertIn("unavailable: missing", self.live_get("/facts"))
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_text(json.dumps(doc()))
        path.symlink_to(elsewhere)
        html = self.live_get("/facts")
        self.assertIn("unavailable: unsafe_or_unreadable", html)
        self.assertNotIn(str(elsewhere), html)
        self.put(doc())
        with mock.patch.object(page.fs, "MAX_OUTPUT_BYTES", 100):
            self.assertEqual(page.load_facts(self.live, RUN)["code"], "oversize")
        self.assertEqual(page.load_facts(self.live, RUN)["state"], "ok")

    # Owner: export parity. Fails if the pinned export and the live panel render different feature content, or the export
    # reads the live file after install instead of its pinned copy.
    def test_facts_live_and_export_render_same_pinned_content(self):
        self.put(doc())
        self.make_export()
        self.up()
        for route in ("/facts", "/facts?agency=" + AGENCIES[0], "/facts?agency=" + AGENCIES[2]):
            self.assertEqual(main_html(self.get(route)), main_html(self.live_get(route)), route)
        self.assertEqual(main_html(self.get("/facts?agency=nobody", 404)), main_html(self.live_get("/facts?agency=nobody", 404)))
        self.get("/facts?extra=1", 400)
        self.assertIn('<a href="/facts" aria-current="page">Agency fact sheets</a>', self.get("/facts"))
        pinned = main_html(self.get("/facts"))
        self.put(doc(agencies=[sheet("changed-later")]))
        self.assertEqual(main_html(self.get("/facts")), pinned)
        self.assertIn("changed-later", self.live_get("/facts"))


if __name__ == "__main__":
    unittest.main()
