"""CDATA-BOARD-RECV-1: Board 'Received' column (alias-aware) and the clock-basis word, via the real load path."""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import console_board as cb
from campaign_tool.records import console_documents as cd
from campaign_tool.records.outcomes import timeline as tl

from .test_console_board import RUN, TODAY, build, clock, put, row


def item(item_id, agency, request):
    return {"item_id": item_id, "agency_id": agency, "request_id": request, "received_at": "2026-10-01T00:00:00Z",
            "kind": "extension", "deadlines": [], "comparison_status": None}


ITEMS = [item("i-1", "alpha-pd", "R-1"), item("i-2", "alpha-pd", "R-1"), item("i-3", "alpha-pd", "R-2"),
         item("i-4", "alphaca", "R-1")]


class BoardReceived(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        build(self.project)
        put(self.project / "run-outputs" / RUN / "outcomes" / "received-items.json",
            {"schema": "records-received-item-v1", "run_id": RUN, "items": ITEMS})

    def aliases(self, text):
        (self.project / "agency-aliases.tsv").write_text(text)

    def run_board(self):
        model = cb.load(self.project, RUN)
        rows = {r["request_id"]: r for r in cb.board_rows(model, TODAY)}
        return model, rows, cb.render_board(model, TODAY)

    def test_alias_present(self):
        self.aliases("agency_key\talias\nalpha-pd\talphaca\n")
        model, rows, html = self.run_board()
        self.assertEqual(model["aliases"]["state"], "ok")
        self.assertEqual((rows["R-1"]["received"], rows["R-2"]["received"]), (3, 1))
        self.assertIn('<a href="/documents?agency=alpha-pd">3 files</a>', html)
        self.assertIn('<a href="/documents?agency=alpha-pd">1 files</a>', html)

    def test_alias_absent(self):
        model, rows, html = self.run_board()
        self.assertEqual((rows["R-1"]["received"], rows["R-2"]["received"]), (2, 1))
        self.assertEqual(sum(r["received"] for r in rows.values()), 3)
        self.assertNotIn("aliases", cb.sources(model))
        self.assertNotIn("aliases:", html)

    def test_alias_invalid_fails_closed(self):
        self.aliases("agency_key\talias\nalpha-pd\talphaca\nbeta-pd\talphaca\n")
        model, rows, html = self.run_board()
        self.assertEqual(rows["R-1"]["received"], 2)
        self.assertIn("aliases: unavailable (aliases_invalid)", html)
        self.assertIn("Statutory due", html)
        self.assertIn("R-4", html)

    def test_received_missing(self):
        (self.project / "run-outputs" / RUN / "outcomes" / "received-items.json").unlink()
        model, rows, html = self.run_board()
        self.assertTrue(all(r["received"] is None for r in rows.values()))
        self.assertEqual(html.count("<td>unavailable</td>"), 4)
        self.assertIn("received: unavailable", html)
        self.assertIn("R-4", html)

    def test_single_cd_load(self):
        with mock.patch.object(cd, "load", wraps=cd.load) as spy:
            cb.load(self.project, RUN)
        self.assertEqual(spy.call_count, 1)

    def test_basis_words(self):
        for basis, word in (("recorded_receipt", "DATED"), ("submission_proxy", "INFERRED"), ("unknown", "UNKNOWN")):
            with self.subTest(basis=basis):
                register = {"as_of": "2026-11-05", "requests": [row("alpha-pd", "R-1", clock(basis=basis))]}
                doc = tl.build_timeline(register, {"items": []}, [], [], [], run_id=RUN)
                put(self.project / "run-outputs" / RUN / "outcomes" / "timeline.json", doc)
                html = cb.render_board(cb.load(self.project, RUN), TODAY)
                self.assertIn("2026-11-09 (%s)" % word, html)
                self.assertNotIn("recorded_receipt", html)
                self.assertNotIn("submission_proxy", html)


if __name__ == "__main__":
    unittest.main()
