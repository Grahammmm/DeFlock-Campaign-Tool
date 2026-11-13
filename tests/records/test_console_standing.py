"""Home 'Where each agency stands' on synthetic run outputs (two invented agencies)."""
import json
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import console_home as home, console_standing as cs
from tests.records.test_console_board import RUN, TODAY, build, put
from datetime import datetime, timezone

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
BETA = "tracker:beta sheriff / coroner"


class Standing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        build(self.project)

    def edit(self, change, name="outcomes/timeline.json"):
        path = self.project / "run-outputs" / RUN / name
        doc = json.loads(path.read_text())
        change(doc)
        put(path, doc)

    def rows(self, roster=None):
        model = home.load(self.project, RUN, now=NOW)
        return {r["key"]: r for r in cs.agency_rows(model, TODAY, roster or {"state": "absent"})}, model

    def test_one_row_per_agency_with_basis(self):
        rows, _ = self.rows()
        self.assertEqual(sorted(rows), ["alpha-pd", BETA])
        self.assertEqual((rows["alpha-pd"]["rank"], rows["alpha-pd"]["basis"]), ("overdue", "DATED"))
        self.assertEqual(rows["alpha-pd"]["days"], 35)
        self.assertEqual(rows["alpha-pd"]["open"], 2)

        def proxy(doc):
            doc["requests"][0]["overdue_marks"][0]["basis"] = "submission_proxy"
        self.edit(proxy)
        self.assertEqual(self.rows()[0]["alpha-pd"]["basis"], "INFERRED")

        def unknown(doc):
            doc["requests"][0]["overdue_marks"] = [{"kind": "unknown", "date": None, "basis": None, "label": "x"}]
        self.edit(unknown)
        row, model = self.rows()
        self.assertIsNone(row["alpha-pd"]["overdue"])
        page = cs.render_standing(model, TODAY)
        self.assertIn("clock unknown (UNKNOWN)", page)
        self.assertNotIn("overdue 20", page)

    def test_closed_last_and_hand_note_not_contact(self):
        def close(doc):
            for r in doc["requests"]:
                if r["agency_key"] == BETA:
                    r["closed"] = True
                    r["events"].append({"type": "hand note", "date": "2026-11-04", "summary": "n",
                                        "source": {"kind": "manual", "event_id": "h", "actor": "G"}})
        self.edit(close)
        rows, model = self.rows()
        self.assertEqual(rows[BETA]["stage"], "closed")
        self.assertEqual(rows[BETA]["last"].isoformat(), "2026-11-01")
        self.assertEqual([r["key"] for r in cs.agency_rows(model, TODAY, {"state": "absent"})][-1], BETA)

    def test_tier1_and_missing_items(self):
        rows, model = self.rows()
        self.assertIn("R-1: ", rows["alpha-pd"]["tier1"])
        self.assertIn("R-2: not available", rows["alpha-pd"]["tier1"])
        (self.project / "run-outputs" / RUN / "outcomes/response-items.json").unlink()
        page = home.render_home(home.load(self.project, RUN, now=NOW), "x")
        self.assertIn("R-1: not available", page)
        self.assertIn("alpha-pd", page)

    def test_timeline_unavailable(self):
        (self.project / "run-outputs" / RUN / "outcomes/timeline.json").unlink()
        page = home.render_home(home.load(self.project, RUN, now=NOW), "x")
        self.assertIn('id="standing"', page)
        self.assertIn("The request timeline: unavailable. This file has not been created yet; run the records step that writes it. No agency rows can be shown.", page)
        self.assertIn("Needs your decision", page)

    def test_roster(self):
        model = home.load(self.project, RUN, now=NOW)
        self.assertIn("roster not staged; showing register agencies only", cs.render_standing(model, TODAY))
        roster = self.project / cs.ROSTER
        roster.write_text("agency_key\tagency\nalpha-pd\tAlpha PD\nnone-yet\tGamma Clerk\nzeta\tZeta Office\n")
        page = cs.render_standing(model, TODAY)
        self.assertIn("Gamma Clerk (no request on record in this run)", page)
        self.assertIn("(not in roster)", page)  # beta is missing from the roster
        self.assertEqual(page.count("<tr>"), 5)
        for bad in ("agency_key\tagency\nalpha-pd\tA\nalpha-pd\tB\n", "key\tname\nalpha-pd\tA\n",
                    "agency_key\tagency\nalpha-pd\tA\tx\n"):
            roster.write_text(bad)
            page = cs.render_standing(model, TODAY)
            self.assertIn("roster: unavailable (roster_invalid)", page)
            self.assertIn("alpha-pd", page)


if __name__ == "__main__":
    unittest.main()
