"""Tier and state per asked item, and the logs-and-audits summary, on synthetic requests."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import item_tiers as it
from campaign_tool.records import playbook as pb


def report(*asks, key="example-pd"):
    return {"schema": it.SCHEMA_IN, "run_id": "r1", "requests": [
        {"agency_key": key, "request_id": "R-1", "flags": [],
         "asks": [{"ask_id": "ask-%d" % n, "text": t, "rating": r, "evidence": [], "flags": []}
                  for n, (t, r) in enumerate(asks, 1)]}]}


class TierTests(unittest.TestCase):
    def setUp(self):
        self.book = pb.load()

    def test_tier_one_first_then_policy_then_contracts(self):
        rep = it.tier_report(report(("The vendor contract and order forms", "missing"),
                                    ("Your retention policy", "answered"),
                                    ("Completed audits of ALPR use", "no_records_stated"),
                                    ("Search logs for January", "answered"),
                                    ("Photos of the lobby", "missing")), self.book)
        asks = rep["requests"][0]["asks"]
        self.assertEqual([a["tier"] for a in asks], [1, 1, 2, 3, None])
        self.assertEqual(asks[0]["text"], "Completed audits of ALPR use")  # document order inside a tier
        self.assertEqual(asks[4]["tier_name"], "untiered")
        self.assertTrue(rep["review_required"])

    def test_state_words_and_unrated(self):
        rep = it.tier_report(report(("search logs", "withheld"), ("audit", "exemption_cited"),
                                    ("policy", "unclear"), ("contract", None)), self.book)
        self.assertEqual([a["state"] for a in rep["requests"][0]["asks"]],
                         ["withheld", "exemption cited", "unclear", "not rated"])

    def test_logs_and_audits_summary(self):
        rep = it.tier_report(report(("native access logs", "answered"), ("completed audits", "no_records_stated")), self.book)
        s = rep["requests"][0]["logs_and_audits"]
        self.assertEqual(s["logs"]["status"], "produced")
        self.assertEqual(s["audits"]["status"], "open")
        self.assertEqual(s["audits"]["states"], {"no records stated": 1})
        self.assertEqual(s["line"], "logs: produced; audits: open")

    def test_tier_one_not_asked(self):
        s = it.tier_report(report(("the retention policy", "answered")), self.book)["requests"][0]["logs_and_audits"]
        self.assertEqual((s["tier1_asked"], s["logs"]["status"], s["audits"]["status"]), (0, "not asked", "not asked"))

    def test_unrated_is_not_called_open_or_produced(self):
        s = it.tier_report(report(("search logs", None)), self.book)["requests"][0]["logs_and_audits"]
        self.assertEqual(s["logs"]["status"], "not rated")

    def test_other_playbook_tiers_are_data(self):
        book = json.loads(json.dumps(self.book))
        book["tiers"][2]["keywords"].append("lobby")
        rep = it.tier_report(report(("Photos of the lobby", "missing")), book)
        self.assertEqual(rep["requests"][0]["asks"][0]["tier"], 3)

    def test_refuses_wrong_schema(self):
        with self.assertRaises(pb.PlaybookError) as ctx:
            it.tier_report({"schema": "x", "requests": []}, self.book)
        self.assertEqual(ctx.exception.code, "response_items_invalid")


class CliTests(unittest.TestCase):
    def run_main(self, *argv):
        import contextlib
        import io
        import sys
        out, err = io.StringIO(), io.StringIO()
        old = sys.argv
        sys.argv = ["records items", *argv]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = it.main()
        finally:
            sys.argv = old
        return code, out.getvalue(), err.getvalue()

    def test_cli_prints_and_refuses(self):
        d = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        good = d / "ri.json"
        good.write_text(json.dumps(report(("contract", "missing"), ("search logs", "answered"))), encoding="utf-8")
        code, out, _ = self.run_main(str(good))
        self.assertEqual(code, 0)
        self.assertLess(out.index("search logs"), out.index("contract"))
        self.assertIn("logs: produced; audits: not asked", out)
        bad = d / "bad.json"
        bad.write_text("{", encoding="utf-8")
        code, _, err = self.run_main(str(bad))
        self.assertEqual(code, 2)
        self.assertIn("not_json", err)
        code, _, err = self.run_main(str(d / "missing.json"))
        self.assertEqual((code, "unreadable" in err), (2, True))


if __name__ == "__main__":
    unittest.main()
