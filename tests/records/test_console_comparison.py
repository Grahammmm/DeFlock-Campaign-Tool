"""GET /comparison over a synthetic comparison.json (DF1117L-01). The rows come from the real comparison.compare()."""
import ast
import copy
import json
import re
import threading
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import admin_panel as ap, console_page_comparison as page
from campaign_tool.records.outcomes import comparison as cmp
from tests.records.test_console_runs import fetch
from tests.records.test_console_snapshot import RUN, Panel
from tests.records.test_outcome_comparison import fixture

ALLOW = {  # internal CPRA ValueError literals; public output collapses them to receipt_calendar_extension_or_determination_unresolved
    "receipt_or_calendar_unresolved", "calendar_coverage_or_request_date_unresolved", "holiday_calendar_unresolved",
    "calendar_coverage_insufficient", "calendar_roll_bound", "extension_status_unresolved", "extension_unqualified",
    "determination_date_inconsistent", "determination_status_unresolved"}
MESA, HARBOR = "synthetic-mesa-pd", "synthetic-harbor-pd"


def doc(agencies=None, status="fact_sheets_supplied", run=RUN):
    report = cmp.compare(fixture() if agencies is None else agencies, run_id=run, as_of="2026-04-30")
    return {**report, "input_status": status, "inventory_gaps": [], "inventory_complete": True, "input_sha256": None}


def main_html(html):
    return re.search(r"<h1>Policy comparison</h1>.*?(?=<footer|</main>)", html, re.S)[0]


class Comparison(Panel):
    def put(self, data, project=None):
        path = (project or self.live) / "run-outputs" / RUN / "comparison.json"
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

    # Owner: the page's data boundary. Fails if section order, per-agency counts (taken before the status filter), the CPRA
    # request label, a row's review words, reason text or the leginfo-only external link is lost.
    def test_comparison_projects_status_counts_fields_and_cpra_request(self):
        self.put(doc())
        html = self.live_get("/comparison")
        positions = [html.index(t) for t in (cmp.DISCLAIMER[:40], "Fact sheets were supplied", "Missing inventory tables: none", "As of 2026-04-30", "Rows per status")]
        self.assertEqual(positions, sorted(positions))
        self.assertRegex(html, r"<td><a [^>]*>%s</a></td><td>0</td><td>1</td><td>15</td>" % MESA)
        self.assertRegex(html, r"<td><a [^>]*>%s</a></td><td>15</td><td>0</td><td>0</td>" % HARBOR)
        self.assertIn("CPRA response deadline (request synthetic-request-77)", html)
        self.assertIn("CPRA response deadline (request none recorded)", html)
        self.assertIn("Observed practice matches the supplied policy&#x27;s required (shall) bounds.", html)
        self.assertEqual(html.count(page.REVIEW), 31)
        self.assertEqual(len(re.findall(r'href="https?://', html)), 31)
        self.assertEqual(len(re.findall(r'<a href="https://leginfo[^"]*" rel="noreferrer">', html)), 31)
        filtered = self.live_get("/comparison?status=gap&agency=" + MESA)
        self.assertIn("<td>15</td>", filtered)  # full unknown count survives the status filter
        self.assertNotIn(HARBOR, filtered)
        self.assertEqual(filtered.count(page.REVIEW), 1)
        self.assertIn("No rows with status gap for this agency.", self.live_get("/comparison?agency=" + HARBOR + "&status=gap"))

    # Owner: the missing-input case. Fails if a writer-shaped fact_sheets_missing artifact is shown as empty/unavailable,
    # or a missing artifact is shown as a table.
    def test_comparison_missing_reviewed_input_is_not_a_missing_file(self):
        self.put(doc([{"agency_id": "a-one"}, {"agency_id": "a-two", "requests": [{"request_id": "r1"}]}], "fact_sheets_missing"))
        html = self.live_get("/comparison")
        self.assertIn(page.NO_INPUT, html)
        self.assertRegex(html, r"<td><a [^>]*>a-two</a></td><td>0</td><td>0</td><td>15</td>")
        self.assertNotIn("Fact sheets were supplied", html)
        (self.live / "run-outputs" / RUN / "comparison.json").unlink()
        missing = self.live_get("/comparison")
        self.assertIn("unavailable: missing", missing)
        self.assertNotIn("<table", missing)

    # Owner: the load boundary. Fails if a wrong run/schema, malformed row, symlink, bad JSON or oversize file renders
    # a table or zero counts instead of a fixed code.
    def test_comparison_refuses_run_mismatch_and_unreadable_artifacts(self):
        good = doc()
        bad_row = copy.deepcopy(good)
        bad_row["rows"][0]["status"] = "bogus"
        bad_date = {**good, "as_of": "yesterday"}
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_text(json.dumps(good))
        path = self.live / "run-outputs" / RUN / "comparison.json"
        cases = [("run_mismatch", {**good, "run_id": RUN + "-other"}), ("schema_invalid", {**good, "schema": "x"}), ("shape_invalid", bad_row),
                 ("shape_invalid", bad_date), ("shape_invalid", {**good, "rows": {}}), ("invalid_json", "{not json"),
                 ("invalid_json", '{"schema": NaN}'), ("invalid_json", '{"a": 1, "a": 2}')]
        for code, data in cases:
            with self.subTest(code=code, data=str(data)[:30]):
                self.put(data)
                html = self.live_get("/comparison")
                self.assertIn("unavailable: " + code, html)
                self.assertNotIn("<table", html)
                self.assertNotIn(HARBOR, html)
        path.unlink()
        path.symlink_to(elsewhere)
        html = self.live_get("/comparison")
        self.assertIn("unavailable: unsafe_or_unreadable", html)
        self.assertNotIn(str(elsewhere), html)
        self.put(good)
        with mock.patch.object(page, "MAX_OUTPUT_BYTES", 100):
            self.assertEqual(page.load_comparison(self.live, RUN)["code"], "oversize")
        self.assertEqual(page.load_comparison(self.live, RUN)["state"], "ok")  # control: the same file reads when within the ceiling

    # Owner: the filter boundary. Fails if an unknown, blank, duplicate or extra query value is accepted or echoed.
    def test_comparison_rejects_bad_filters(self):
        self.put(doc())
        for query in ("agency=nobody", "status=BOGUS", "status=", "agency=", "status=gap&status=meets", "agency=" + MESA + "&agency=" + HARBOR,
                      "extra=1", "agency", "status=gap&extra=1", "status=%20gap"):
            with self.subTest(query):
                html = self.live_get("/comparison?" + query, 400)
                self.assertNotIn("<table", html)
                self.assertNotIn("BOGUS", html)
                self.assertNotIn("nobody", html)
        self.put("{not json")
        self.live_get("/comparison?status=BOGUS", 400)  # a bad filter is refused even when the file is unavailable
        self.assertIn("unavailable: invalid_json", self.live_get("/comparison?agency=nobody"))  # membership is unknowable, not fabricated
        self.put(doc())
        self.live_get("/comparison?status=gap&agency=" + MESA)

    # Owner: output escaping. Fails if any file text reaches the page as markup, a nested observed value is rendered, or a
    # non-leginfo citation URL becomes a link.
    def test_comparison_escapes_hostile_text_and_only_links_leginfo(self):
        data = doc()
        data["disclaimer"], data["inventory_gaps"] = "<i>disclaimer</i>", ["<s>gap</s>"]
        mesa, harbor = (next(r for r in data["rows"] if r["agency_id"] == a and r["evidence"]) for a in (MESA, HARBOR))
        harbor["evidence"] = [{"source_id": "<script>alert(1)</script>", "locator": "\"><img src=x>"}]
        harbor.update(expected="<b>expected</b>", observed={"k<b>": "<u>v</u>", "nested": {"deep": "NESTEDSECRET"}, "list": ["NESTEDSECRET"]})
        harbor["citation"] = {"label": "<u>label</u>", "url": "javascript:alert(1)"}
        mesa["reason"] = "<b>evil</b>"
        mesa["citation"]["url"] += "&x=1"  # a leginfo look-alike that is not the producer's exact URL
        self.put(data)
        html = self.live_get("/comparison")
        for raw in ("<script>alert", "<img", "<b>", "<u>", "<i>", "<s>", "NESTEDSECRET", "javascript:"):
            self.assertNotIn(raw, html)
        for escaped in ("&lt;script&gt;alert(1)&lt;/script&gt;", "&lt;b&gt;evil&lt;/b&gt; (no description yet)", "k&lt;b&gt;: &lt;u&gt;v&lt;/u&gt;",
                        "&lt;u&gt;label&lt;/u&gt;", "&lt;i&gt;disclaimer&lt;/i&gt;", "&lt;s&gt;gap&lt;/s&gt;"):
            self.assertIn(escaped, html)
        self.assertEqual(len(re.findall(r'href="https?://', html)), 29)  # 31 rows less the two hostile citations
        self.assertNotIn("&amp;x=1\" rel", html)

    # Owner: reason-code drift. Fails when comparison.py emits a reason literal (default, keyword, row["reason"] or a
    # constructed supplied_policy_* value) that the page neither describes nor an explicit allow-list entry justifies.
    def test_reason_literals_have_text_or_documented_allowlist(self):
        def literals(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return [node.value]
            if isinstance(node, ast.IfExp):
                return literals(node.body) + literals(node.orelse)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                return [a + b for a in literals(node.left) for b in literals(node.right)]
            return ["{modality}"]  # the only computed piece is value["policy_modality"]
        tree = ast.parse(Path(cmp.__file__).read_text())
        found, internal = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "_row":  # the default of the positional parameter named reason
                    names = [a.arg for a in node.args.args][-len(node.args.defaults):]
                    found.update(literals(node.args.defaults[names.index("reason")]))
                if node.name == "_cpra_row":
                    internal.update(n.args[0].value for n in ast.walk(node) if isinstance(n, ast.Call)
                                    and getattr(n.func, "id", "") == "ValueError" and isinstance(n.args[0], ast.Constant))
            elif isinstance(node, ast.keyword) and node.arg == "reason":
                found.update(literals(node.value))
            elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Subscript) and getattr(t.slice, "value", None) == "reason" for t in node.targets):
                found.update(literals(node.value))
        found = {c.replace("{modality}", m) for c in found for m in ("shall", "should")}
        self.assertTrue({"missing_or_unqualified_evidence", "supplied_policy_should_discrepancy", "determination_still_pending"} <= found, found)
        self.assertEqual(found - set(page.REASONS), set())
        self.assertEqual(internal, ALLOW)  # a new or removed internal literal must be reviewed here
        self.assertEqual(set(page.REASONS) - found, set())  # no stale description
        self.assertIn("zz_new (no description yet)", page.reason_text("zz_new"))

    # Owner: export parity. Fails if the pinned export and the live panel render different feature content, or the export
    # reads the live file after install instead of its pinned copy.
    def test_export_and_live_comparison_use_same_pinned_artifact(self):
        self.put(doc())
        self.make_export()
        self.up()
        routes = ("/comparison", "/comparison?agency=" + MESA, "/comparison?status=gap", "/comparison?agency=" + HARBOR + "&status=meets")
        for route in routes:
            self.assertEqual(main_html(self.get(route)), main_html(self.live_get(route)), route)
        self.assertIn('<a href="/comparison" aria-current="page">Policy comparison</a>', self.get("/comparison"))
        self.get("/comparison?status=BOGUS", 400)
        pinned = main_html(self.get("/comparison"))
        self.put(doc([{"agency_id": "changed-later"}], "fact_sheets_missing"))
        self.assertEqual(main_html(self.get("/comparison")), pinned)
        self.assertIn("changed-later", self.live_get("/comparison"))


if __name__ == "__main__":
    unittest.main()
