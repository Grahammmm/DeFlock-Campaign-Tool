"""Timeline clock-basis shape at the request board's load/render boundary."""
import json
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import console_board as cb

from .test_console_board import RUN, TODAY, build, put


class BoardClockShapeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        build(self.project)
        outcomes = self.project / "run-outputs" / RUN / "outcomes"
        put(outcomes / "received-items.json",
            {"schema": "records-received-item-v1", "run_id": RUN, "items": []})
        self.path = outcomes / "timeline.json"
        self.doc = json.loads(self.path.read_text())
        self.request = self.doc["requests"][0]
        self.due = self.request["clocks"]["due"]

    def load_and_render(self):
        put(self.path, self.doc)
        model = cb.load(self.project, RUN)
        return model, cb.render_board(model, TODAY)

    def test_nonstring_basis_degrades_only_the_timeline(self):
        # With a due date, list and object values used to reach the badge lookup and crash.
        # Scalars must also be refused rather than silently rendered as an unknown code.
        for basis in (["recorded_receipt"], {"code": "recorded_receipt"}, 1, False):
            with self.subTest(basis=basis):
                self.request["clocks"]["basis"] = basis
                model, page = self.load_and_render()
                self.assertEqual(cb.sources(model), {
                    "timeline": {"state": "unavailable", "code": "shape_invalid"},
                    "links": {"state": "ok"}, "tiers": {"state": "ok"}, "received": {"state": "ok"},
                })
                self.assertIn("the request timeline: unavailable. The file is not in the expected format", page)
                self.assertNotIn("Requests, needs action first", page)

    def test_known_and_unrecognized_string_bases_keep_their_badges(self):
        # Keep known labels and the UNKNOWN fallback rather than imposing a closed vocabulary.
        for basis, badge in (("recorded_receipt", "DATED"), ("submission_proxy", "INFERRED"),
                             ("synthetic_future_basis", "UNKNOWN")):
            with self.subTest(basis=basis):
                self.request["clocks"]["basis"] = basis
                model, page = self.load_and_render()
                self.assertEqual(model["timeline"]["state"], "ok")
                self.assertIn(f"<td>{self.due} ({badge})</td>", page)

    def test_null_and_omitted_basis_keep_the_fallback(self):
        # Legacy rows with an optional basis still render a due date.
        for clocks in ({"due": self.due, "basis": None}, {"due": self.due}):
            with self.subTest(clocks=clocks):
                self.request["clocks"] = clocks
                model, page = self.load_and_render()
                self.assertEqual(model["timeline"]["state"], "ok")
                self.assertIn(f"<td>{self.due} (UNKNOWN)</td>", page)
