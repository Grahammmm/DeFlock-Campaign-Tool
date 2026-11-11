"""kit-search-log refuses control-bearing selected-agency metadata before emitting a report. Synthetic fixtures only."""
import json
import unittest
from unittest import mock

from campaign_tool import search_log
from tests import test_search_log as base

NAME_FIELD = "search-log agency_name contains a control character; repair kit/agencies.json"
ID_FIELD = "search-log agency_id contains a control character; repair kit/agencies.json"
BAD_NAMES = ("Synthetic\nrows counted: 999", "Synthetic\x1b[2J", "Synth\u2028etic", "Synth\u2029etic",
             "\x00Synthetic", "Synthetic\x7f", "Synthetic\x85", "Synthetic\x9f", "Synthetic\t")


def summary_for(agency_id, name):
    return {"agency_id": agency_id, "agency_name": name, "rows": {}, "input": {"columns": {}}, "period": {}}


class AgencyControlTests(unittest.TestCase):
    setUp, csv, run_cli, report, refused = (base.SearchLogTests.setUp, base.SearchLogTests.csv,
                                            base.SearchLogTests.run_cli, base.SearchLogTests.report,
                                            base.SearchLogTests.refused)

    def set_roster(self, name=None, agency_id=base.AID):
        row = {"agency_id": agency_id, "name": name or "Synthetic Police Department", "kind": "police", "selected": False}
        (self.root / "kit" / "agencies.json").write_text(json.dumps([
            row, {"agency_id": "ca-other-sheriff", "name": "Other\nSheriff", "kind": "sheriff"}]), encoding="utf-8")

    def args(self, agency=base.AID, *extra):
        return ("--csv", self.csv(), "--agency", agency, *extra)

    def test_formatter_refuses_control_matrix_without_payload(self):
        for name in BAD_NAMES:
            with self.assertRaises(ValueError) as caught:
                search_log.format_summary(summary_for("ca-x", name))
            self.assertEqual(str(caught.exception), NAME_FIELD)
        for agency_id in ("ca-x\n", "ca\x1b-x", "ca\u2028x"):
            with self.assertRaises(ValueError) as caught:
                search_log.format_summary(summary_for(agency_id, "Bad\nName"))  # ID wins
            self.assertEqual(str(caught.exception), ID_FIELD)
        for code in list(range(32)) + list(range(127, 160)):
            with self.assertRaises(ValueError):
                search_log.format_summary(summary_for("ca-x", "a" + chr(code) + "b"))

    def test_real_roster_cli_text_json_refuse_before_emission(self):
        for name in BAD_NAMES[:4]:
            self.set_roster(name)
            for extra in ((), ("--json",)):
                err = self.refused(*self.args(base.AID, *extra), words=(NAME_FIELD,))
                self.assertEqual(err.count("\n"), 1)
                self.assertNotIn("999", err)
        trailing = base.AID + "\n"
        self.set_roster(agency_id=trailing)
        for extra in ((), ("--json",)):
            self.refused(*self.args(trailing, *extra), words=(ID_FIELD,))

    def test_preflight_precedes_sharing_categories_and_csv(self):
        self.set_roster("Bad\nName")
        missing = self.files / "missing.csv"
        for extra in ((), ("--against-sharing",)):
            with mock.patch.object(search_log, "_recorded_sharing") as shared, \
                    mock.patch.object(search_log, "summarize_csv") as summarized:
                self.refused("--csv", missing, "--agency", base.AID, "--categories", self.files / "none.json", *extra,
                             words=(NAME_FIELD,))
            shared.assert_not_called()
            summarized.assert_not_called()
        self.set_roster()  # safe metadata still reaches the downstream refusal
        err = self.refused("--csv", missing, "--agency", base.AID, "--against-sharing")
        self.assertNotIn("control character", err)

    def test_safe_selected_agency_ignores_other_name_controls(self):
        self.set_roster()
        data = self.report(self.csv())
        self.assertEqual(data["agency_id"], base.AID)
        self.assertEqual(data["agency_name"], "Synthetic Police Department")

    def test_safe_metadata_is_unchanged(self):
        self.set_roster("Caf\u00e9 \"Q\" <b>PD</b>")
        code, out, err = self.run_cli(*self.args())
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("Search log summary for Caf\u00e9 \"Q\" <b>PD</b> (ca-synthetic-police)\n"), out)
        self.assertEqual(json.loads(self.run_cli(*self.args(base.AID, "--json"))[1])["agency_name"], "Caf\u00e9 \"Q\" <b>PD</b>")
        self.assertIn("rows counted", out)

    def test_raw_summary_computation_is_unchanged_and_refusal_is_read_only(self):
        agency = {"agency_id": base.AID, "name": "Bad\nrows counted: 999"}
        summary = search_log.summarize_csv(self.csv(), agency)
        self.assertEqual(summary["agency_name"], agency["name"])
        with self.assertRaises(ValueError):
            search_log.format_summary(summary)
        self.set_roster("Bad\nName")
        before = base.tree(self.base)
        self.refused(*self.args())
        self.refused(*self.args(base.AID, "--json"))
        self.assertEqual(base.tree(self.base), before)
        self.set_roster()  # a refusal does not stick
        code, out, err = self.run_cli(*self.args())
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("Search log summary for Synthetic Police Department"))


if __name__ == "__main__":
    unittest.main()
