"""Synthetic Home outstanding items: grouping, partial responses and unavailable inputs."""
import json
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import console_home as home, console_outstanding as co
from tests.records.test_console_board import build, put, RUN


class Outstanding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        build(self.project)
        self.path = self.project / "run-outputs" / RUN / "outcomes/response-items.json"
        def req(agency, ident, ratings):
            texts = ("Search logs", "Retention policy", "Vendor contract", "Other requested item")
            return {"agency_key": agency, "request_id": ident, "asks": [
                {"ask_id": "a%d" % i, "text": text, "rating": rating,
                 "evidence": [{"quote": "QUOTE-SENTINEL-1"}]} for i, (text, rating) in enumerate(zip(texts, ratings))]}
        self.doc = {"schema": "records-response-items-v1", "run_id": RUN, "requests": [
            req("tracker:beta sheriff / coroner", "L-R-3", ("missing", "withheld", "unclear", "missing")),
            req("alpha-pd", "R-2", ("answered", "answered", "answered")),
            req("alpha-pd", "R-1", ("missing", "answered", "withheld"))]}
        put(self.path, self.doc)

    def model(self):
        return home.load(self.project, RUN)

    def test_group_order_counts_partial_closed_links_and_privacy(self):
        model = self.model()
        rows = co.outstanding(model)
        self.assertEqual([r["tier"] for r in rows], [1, 1, 2, 3, 3, None])
        self.assertEqual([r["request_id"] for r in rows], ["R-1", "L-R-3", "L-R-3", "R-1", "L-R-3", "L-R-3"])
        self.assertTrue(all(r["request_partly_answered"] == (r["request_id"] == "R-1") for r in rows))
        self.assertTrue(all(r["closed"] == (r["request_id"] == "L-R-3") for r in rows))
        self.assertEqual(rows[1]["link"], "/agency?agency=tracker%3Abeta%20sheriff%20%2F%20coroner#L-R-3")
        page = co.render_outstanding(model)
        for heading, count in (("Tier 1:", 2), ("Tier 2:", 1), ("Tier 3:", 2), ("Untiered", 1)):
            line = next(line for line in page.splitlines() if line.startswith("<h3>" + heading))
            self.assertIn("(%d)</h3>" % count, line)
        self.assertLess(page.index("Tier 1:"), page.index("Tier 2:"))
        for text in ("closed by agency", co.LABEL, 'id="outstanding"'):
            self.assertIn(text, page)
        self.assertNotIn("QUOTE-SENTINEL-1", page)
        self.assertNotIn("<form", page)
        self.assertIn(page, home.render_home(model, "unused"))

    def test_source_order_escaping_truncation_and_readonly(self):
        asks = self.doc["requests"][2]["asks"]
        asks += [dict(asks[0], ask_id="a-last", text="Search logs <script>" + "x" * 200)]
        put(self.path, self.doc)
        before = self.path.read_bytes()
        model = self.model()
        self.assertEqual([r["ask_id"] for r in co.outstanding(model) if r["request_id"] == "R-1"], ["a0", "a-last", "a2"])
        page = co.render_outstanding(model)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)
        self.assertNotIn("x" * 161, page)
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_stale_invalid_and_empty_sources_keep_home(self):
        for mode in ("missing", "stale", "invalid", "empty"):
            with self.subTest(mode=mode):
                doc = json.loads(json.dumps(self.doc))
                if mode == "stale":
                    doc["run_id"] = "old-run"
                if mode == "empty":
                    doc["requests"] = []
                put(self.path, doc)
                if mode == "missing":
                    self.path.unlink()
                if mode == "invalid":
                    self.path.write_text("not json")
                model = self.model()
                self.assertEqual(co.outstanding(model), [])
                page = home.render_home(model, "unused")
                self.assertIn("<h1>Home</h1>", page)
                self.assertIn("No outstanding items" if mode == "empty" else "unavailable (no_response_items)", page)


if __name__ == "__main__":
    unittest.main()
