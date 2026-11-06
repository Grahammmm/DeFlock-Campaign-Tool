"""Precedent lookup: private evidence names the agency, the draft phrase never does."""
import os
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import item_tiers as it
from campaign_tool.records import playbook as pb
from campaign_tool.records import precedent as pr

HEADER = ",".join(pr.HEADER) + "\n"
GOOD = HEADER + 'city-alpha,logs,"monthly CSV exports with plate numbers redacted",2026-09-16,"ref 1"\n'


def write(text):
    d = tempfile.mkdtemp()
    p = Path(d) / "precedents.csv"
    p.write_text(text, encoding="utf-8")
    return p


def tiers(text, rating, key="county-sheriff"):
    rep = {"schema": it.SCHEMA_IN, "run_id": "r1", "requests": [
        {"agency_key": key, "request_id": "R-1", "flags": [],
         "asks": [{"ask_id": "a1", "text": text, "rating": rating, "evidence": [], "flags": []}]}]}
    return it.tier_report(rep, pb.load())


class PrecedentTests(unittest.TestCase):
    def rows(self, text=GOOD):
        rows, problems = pr.load(write(text))
        self.assertEqual(problems, [])
        return rows

    def test_denied_log_ask_gets_unnamed_phrase_and_named_private_evidence(self):
        out = pr.precedents(tiers("ALPR search logs for January", "withheld"), self.rows())
        hit = out["requests"][0]["precedents"][0]
        self.assertIn("Another county agency", hit["draft_phrase"])
        self.assertFalse(pr.names_agency(hit["draft_phrase"], ["city-alpha"]))
        self.assertEqual(hit["private_evidence"][0]["agency"], "city-alpha")
        self.assertTrue(out["private"] and not out["publishable"] and out["review_required"])

    def test_own_agency_is_never_its_own_precedent(self):
        out = pr.precedents(tiers("search logs", "withheld", key="city-alpha"), self.rows())
        self.assertEqual(out["requests"][0]["precedents"], [])

    def test_answered_or_non_tier1_asks_get_nothing(self):
        self.assertEqual(pr.precedents(tiers("search logs", "answered"), self.rows())["requests"][0]["precedents"], [])
        self.assertEqual(pr.precedents(tiers("the vendor contract", "withheld"), self.rows())["requests"][0]["precedents"], [])

    def test_form_text_naming_an_agency_is_refused_at_load(self):
        rows, problems = pr.load(write(HEADER + 'city-alpha,logs,"the City Alpha export",2026-09-16,"r"\n'))
        self.assertEqual(rows, [])
        self.assertEqual(problems[0]["field"], "form")

    def test_bad_rows_are_reported_not_used(self):
        text = GOOD + GOOD.splitlines()[1] + "\n" + 'x,money,"f",2026-13-40,"r"\n'
        rows, problems = pr.load(write(text))
        self.assertEqual(len(rows), 1)
        self.assertEqual({p["field"] for p in problems}, {"row", "kind", "released_on"})

    def test_bad_header_and_symlink_refused(self):
        self.assertEqual(pr.load(write("a,b\n1,2\n"))[0], [])
        p = write(GOOD)
        link = p.with_name("link.csv")
        os.symlink(p, link)
        self.assertEqual(pr.load(link)[1][0]["reason"], "not a regular file")

    def test_wrong_report_schema_refused(self):
        with self.assertRaises(pb.PlaybookError):
            pr.precedents({"schema": "nope"}, self.rows())


if __name__ == "__main__":
    unittest.main()
