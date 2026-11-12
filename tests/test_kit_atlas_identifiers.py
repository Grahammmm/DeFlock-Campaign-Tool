"""Atlas text refuses control-bearing agency identifiers; JSON unchanged. Synthetic data only."""
import copy
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_atlas  # noqa: E402
from campaign_tool.kit_atlas import AtlasError  # noqa: E402
from tests import test_kit_agency_atlas as base  # noqa: E402  (module alias: legacy TestCases are not re-collected)

ERROR = "Atlas report: an agency identifier contains a control or format character"
NAME_ERROR = "Atlas report: an agency name contains a control or format character"
CONTROLS = ("\x00", "\t", "\n", "\r", "\x1b", "\x7f", "\x85", "\u200b", "\u202e", "\u2028", "\u2029")


class IdentifierTests(base.AtlasTestCase):
    def healthy(self):
        # twin rows are ambiguous (two candidates); east is listed; added-board is selected-not-listed
        return self.compare(self.atlas([base.row("East Fictional Police", "Flock"), base.row("Twin Police"),
                                        base.row("Nowhere PD")]))

    def refused(self, report, message=ERROR):
        original = copy.deepcopy(report)
        with base.forbidden(), self.assertRaises(AtlasError) as caught:
            kit_atlas.render_atlas(report)
        self.assertEqual(str(caught.exception), message)
        self.assertEqual(report, original)

    def locations(self, report):
        """Yield (label, setter) for every printed identifier, first and last."""
        for i, item in enumerate(report["listed_seed"]["rows"]):
            yield f"listed{i}", lambda r, v, i=i: r["listed_seed"]["rows"][i].__setitem__("agency_id", v)
        for i, item in enumerate(report["unmatched_atlas"]["rows"]):
            for j, _ in enumerate(item["candidate_agency_ids"]):
                yield f"cand{i}.{j}", lambda r, v, i=i, j=j: r["unmatched_atlas"]["rows"][i]["candidate_agency_ids"].__setitem__(j, v)
        for i, item in enumerate(report["selected_not_listed"]["rows"]):
            yield f"sel{i}", lambda r, v, i=i: r["selected_not_listed"]["rows"][i].__setitem__("agency_id", v)

    def test_each_identifier_location_obeys_atlas_controls(self):
        baseline = self.healthy()
        labels = [label for label, _ in self.locations(baseline)]
        self.assertTrue(any(x.startswith("listed") for x in labels))
        self.assertTrue(any(x.startswith("cand") for x in labels))
        self.assertTrue(any(x.startswith("sel") for x in labels))
        self.assertGreaterEqual(len([x for x in labels if x.startswith("cand")]), 2)
        for control in CONTROLS:
            for label, setter in self.locations(baseline):  # includes late rows and later candidates
                with self.subTest(control=repr(control), where=label):
                    report = copy.deepcopy(baseline)
                    setter(report, "ca-fict-x" + control)
                    self.refused(report)
        clean = copy.deepcopy(baseline)
        for _, setter in self.locations(clean):
            setter(clean, "ca-fict-unicode-é")
        self.assertIn("ca-fict-unicode-é", kit_atlas.render_atlas(clean))

    def test_name_error_keeps_precedence(self):
        report = self.healthy()
        report["listed_seed"]["rows"][0]["name"] = "Bad\nName"
        report["listed_seed"]["rows"][0]["agency_id"] = "ca-fict-bad\n"
        self.refused(report, NAME_ERROR)

    def test_real_seed_trailing_lf_unique_and_ambiguous_text_only(self):
        seed = copy.deepcopy(base.SEED)
        agencies = seed["counties"][0]["agencies"]
        for target in ("ca-fict-east-police", "ca-fict-twin-police-a", "ca-fict-twin-police-b"):
            with self.subTest(target=target):
                fresh = copy.deepcopy(seed)
                for a in fresh["counties"][0]["agencies"]:
                    if a["agency_id"] == target:
                        a["agency_id"] += "\n"
                report = self.compare(self.atlas([base.row("East Fictional Police"), base.row("Twin Police")]),
                                      seed=fresh)
                self.refused(report)
                text = json.dumps(report, ensure_ascii=False)  # JSON path stays successful
                self.assertIn(target + "\\n", text)
        self.assertEqual(len(agencies), 7)

    def test_real_kit_trailing_lf_selected_only_text_only(self):
        self.entries[-2]["agency_id"] = "ca-fict-added-board\n"
        self.entries[-1]["agency_id"] = "ca-fict-added-off\n"  # unselected, unprinted
        self.write_campaign()
        report = self.healthy()
        self.assertEqual([r["agency_id"] for r in report["selected_not_listed"]["rows"]
                          if r["agency_id"].endswith("\n")], ["ca-fict-added-board\n"])
        self.refused(report)
        # identity is not repaired: clean and LF ids stay distinct
        self.assertNotEqual("ca-fict-added-board\n", "ca-fict-added-board")

    def test_unprinted_bad_ids_are_irrelevant_and_healthy_text_is_stable(self):
        self.entries[-1]["agency_id"] = "ca-fict-added-off\n"
        self.write_campaign()
        report = self.healthy()
        text = kit_atlas.render_atlas(report)
        self.assertNotIn("ca-fict-added-off", text)
        self.assertIn("- East Fictional Police [ca-fict-east-police]: selected", text)
        self.assertIn("    candidates: ca-fict-twin-police-a, ca-fict-twin-police-b", text)
        self.assertTrue(text.endswith(kit_atlas.TRAILER))
        self.assertEqual(text, kit_atlas.render_atlas(report))

    def test_cli_text_refuses_json_succeeds_read_only(self):
        self.entries = [{"agency_id": "ca-slo-added\n", "name": "Added", "kind": "other", "selected": True}]
        self.write_campaign(county="San Luis Obispo")
        csv_path = self.atlas([base.row("Nowhere PD", county="San Luis Obispo")])
        before = base.snapshot(self.tmp)
        result = base.cli("kit-agency", "atlas", "--csv", csv_path, "--directory", self.root)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", "Stopped: " + ERROR + "\n"))
        result = base.cli("kit-agency", "atlas", "--csv", csv_path, "--directory", self.root, "--json")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertIn("ca-slo-added\\n", result.stdout)
        self.assertEqual(base.snapshot(self.tmp), before)


if __name__ == "__main__":
    unittest.main()
