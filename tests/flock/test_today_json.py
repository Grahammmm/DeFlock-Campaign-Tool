"""flock_today --json (RF0140-8): one flock-today-v1 object built from the same items, errors and counts as the lines."""
import json
import shlex
import subprocess
import sys
import unittest

from tests.flock import test_today as base
from tests.flock.test_today import (DATE, LEADS_HEAD, PLANTED, ROOT, SWEEP_AGENCY, TOOL, TOOLS, WATCH, fact, facts,
                                    known, requests, snapshot, totals, unknown)

KEYS = ["schema", "review_required", "date", "note", "items", "source_errors", "totals"]
ITEM = ["rank", "kind", "date", "slug", "item", "next"]


def zeros(**counts):
    out = dict(items=0, reminders=0, leads=0, stale=None, reviews=0, unreviewed=0, receipt_unknown=0)
    out.update(counts)
    return out


class FlockTodayJsonTest(unittest.TestCase):
    setUp, write, findings, sweep_store = (base.FlockTodayTest.setUp, base.FlockTodayTest.write,
                                           base.FlockTodayTest.findings, base.FlockTodayTest.sweep_store)

    def run_tool(self, *args, store=None, code=0):
        before = snapshot(self.tmp)
        done = subprocess.run([sys.executable, "-B", str(TOOL), "--store", str(store or self.store), "--watchlist", WATCH,
                               "--roster", str(self.roster), "--date", DATE, *args], capture_output=True, text=True, check=False)
        self.assertEqual(snapshot(self.tmp), before, "flock_today changed a file")
        out = done.stdout + done.stderr
        self.assertNotIn("Traceback", out)
        for word in ("quillfeather", "://", "@", "main st", "quote", "overdue"):  # no title, URL, address or evidence
            self.assertNotIn(word, out.casefold())
        self.assertEqual(done.returncode, code, out)
        return done.stdout

    def json(self, *args, store=None, code=0):
        out = self.run_tool("--json", *args, store=store, code=code)
        self.assertEqual(out.count("\n"), 1, out)  # exactly one line
        self.assertTrue(out.isascii())
        doc = json.loads(out)
        self.assertEqual(list(doc), KEYS)
        self.assertEqual((doc["schema"], doc["review_required"], doc["date"], doc["note"]),
                         ("flock-today-v1", True, DATE, "provisional; not a legal status"))
        for item in doc["items"]:
            self.assertEqual(list(item), ITEM)
            self.assertIs(type(item["rank"]), int)
            self.assertTrue(item["date"] is None or item["date"])
        return doc

    def same_as_text(self, doc, *args, code=0):  # field for field equal to the parsed text lines
        lines = self.run_tool(*args, code=code).splitlines()
        errors = [line for line in lines if " | source-error | " in line]
        self.assertEqual(errors, ["today | 0 | source-error | %(source)s | %(error)s" % e for e in doc["source_errors"]])
        rows = [line.split(" | ", 5)[1:] for line in lines[len(errors):-1]]
        self.assertEqual(rows, [[str(i["rank"]), i["kind"], i["slug"], i["item"], i["next"]] for i in doc["items"]])
        t = doc["totals"]
        self.assertEqual(lines[-1], totals(t["items"], t["reminders"], t["leads"], "-" if t["stale"] is None else t["stale"],
                                           t["reviews"], t["unreviewed"], t["receipt_unknown"]))

    def test_every_kind_same_order_fields_and_totals_as_text(self):
        self.sweep_store()
        self.write("agencies/alpha/REQUESTS.csv", requests.HEADER, [
            known("A-W2", "Alpha", 7), known("A-TODAY", "Alpha", 0), known("A-PASS", "Alpha", -2), known("A-W1", "Alpha", 7),
            known("A-FAR", "Alpha", 8), known("A-DONE", "Alpha", -5, status="closed"), unknown("A-UNK", "Alpha")])
        self.write("agencies/beta/REQUESTS.csv", requests.HEADER, [known("B-TODAY", "Beta", 0), unknown("B-UNK", "Beta")])
        expired, u1, u2 = fact("Alpha", "a", end="2026-01-01"), fact("Alpha", "b", status="unreviewed"), \
            fact("Alpha", "c", status="unreviewed")
        self.write("agencies/alpha/FACTS.csv", facts.V2, [expired, u1, u2])
        self.write("agencies/beta/FACTS.csv", facts.V2, [fact("Beta", "a", status="unreviewed")])
        self.write("LEADS.csv", LEADS_HEAD, [["L-3", "Alpha", "2026-10-02", "new", "", ""], ["L-1", "Alpha", "2026-10-02", "new", "", ""],
                                             ["L-2", "Beta", "2026-10-01", "new", "", ""]])
        path = self.findings([["FND-3", PLANTED, expired["fact_id"], PLANTED], ["FND-2", "t", u1["fact_id"], "c"],
                              ["FND-1", "t", "%s;%s" % (u2["fact_id"], expired["fact_id"]), PLANTED]])
        doc = self.json("--findings", "alpha=" + path)
        self.assertEqual([[str(i["rank"]), i["kind"], i["slug"], i["item"]] for i in doc["items"]], [
            ["1", "reminder-passed", "alpha", "A-PASS"], ["1", "reminder-today", "alpha", "A-TODAY"],
            ["1", "reminder-today", "beta", "B-TODAY"], ["2", "reminder-week", "alpha", "A-W1"],
            ["2", "reminder-week", "alpha", "A-W2"], ["3", "stale-cited", "alpha", "FND-1"], ["3", "stale-cited", "alpha", "FND-3"],
            ["4", "lead-new", "beta", "L-2"], ["4", "lead-new", "alpha", "L-1"], ["4", "lead-new", "alpha", "L-3"],
            ["5", "review-wait", SWEEP_AGENCY, "ready-verify 2026-10-30"], ["6", "facts-unreviewed", "alpha", "2"],
            ["6", "facts-unreviewed", "beta", "1"], ["7", "receipt-unknown", "alpha", "A-UNK"], ["7", "receipt-unknown", "beta", "B-UNK"]])
        self.assertEqual([i["date"] for i in doc["items"]][:2], ["2026-10-29", DATE])
        self.assertEqual([i["date"] for i in doc["items"]][5:12], [None, None, "2026-10-01", "2026-10-02", "2026-10-02",
                                                                  "2026-10-30", None])
        self.assertEqual(doc["items"][-1]["date"], "2026-10-20")
        self.assertEqual(doc["source_errors"], [])
        self.assertEqual(doc["totals"], zeros(items=15, reminders=5, leads=3, stale=2, reviews=1, unreviewed=2, receipt_unknown=2))
        self.same_as_text(doc, "--findings", "alpha=" + path)

    def test_next_with_bar_and_quotes_is_the_text_column_exactly(self):
        self.store = self.tmp / "st 'o' | re"
        (self.store / "agencies").mkdir(parents=True)
        self.write("agencies/alpha/REQUESTS.csv", requests.HEADER, [unknown("R 'x' | y", "Alpha")])
        self.write("LEADS.csv", LEADS_HEAD, [["L-1", "Alpha", "2026-10-02", "new", "", ""]])
        doc = self.json(store=self.store)
        q = shlex.quote
        self.assertEqual([i["next"] for i in doc["items"]], [
            "python3 %s lead --store %s --item L-1 --state requested|ignored --by <name>" % (q(str(TOOLS / "flock_monitor.py")),
                                                                                             q(str(self.store))),
            "receipt unknown, sent 11 days ago; python3 %s record %s --agency alpha --request %s --step receipt"
            % (q(str(TOOLS / "flock_requests.py")), q(str(self.store)), q("R 'x' | y"))])
        self.assertEqual(doc["items"][1]["item"], "R 'x' | y")
        lines = self.run_tool(store=self.store).splitlines()
        self.assertEqual(lines[0].split(" | ", 5)[5], doc["items"][0]["next"])
        self.assertTrue(lines[1].endswith(" | " + doc["items"][1]["next"]))

    def test_source_errors_are_objects_the_rest_still_listed_exit_1(self):
        self.write("agencies/alpha/REQUESTS.csv", requests.HEADER, [known("R-0", "Alpha", 0)])
        self.write("LEADS.csv", LEADS_HEAD[:-1], [["L-1", "Alpha", "2026-10-02", "new", ""]])
        doc = self.json(code=1)
        self.assertEqual(doc["source_errors"], [{"source": "leads", "error": "Fail"}])
        self.assertEqual([(i["kind"], i["item"]) for i in doc["items"]], [("reminder-today", "R-0")])
        self.assertEqual(doc["totals"], zeros(items=1, reminders=1))
        self.same_as_text(doc, code=1)
        self.write("agencies/alpha/REQUESTS.csv", requests.HEADER, [known("R-0", "Alpha", 0), ["R-0"] + known("x", "Alpha", 1)[1:]])
        self.write("agencies/alpha/FACTS.csv", facts.V2, [dict(fact("Alpha", "a"), status="pending")])
        doc = self.json("--findings", "alpha=" + str(self.tmp / "absent.csv"), code=1)
        self.assertEqual(doc["source_errors"], [{"source": s, "error": e} for s, e in (
            ("requests", "Fail"), ("leads", "Fail"), ("stale:alpha", "Fail"), ("facts", "Fail"))])
        self.assertEqual(doc["items"], [])
        self.same_as_text(doc, "--findings", "alpha=" + str(self.tmp / "absent.csv"), code=1)

    def test_stale_total_is_null_without_findings_and_int_with(self):
        self.write("agencies/alpha/FACTS.csv", facts.V2, [fact("Alpha", "a", end="2026-01-01")])
        cited = self.findings([["FND-1", "t", fact("Alpha", "a", end="2026-01-01")["fact_id"], "c"]])
        self.assertIsNone(self.json()["totals"]["stale"])
        self.assertEqual(self.json("--findings", "beta=" + cited, code=1)["totals"]["stale"], 0)
        doc = self.json("--findings", "alpha=" + str(self.tmp / "absent.csv"), code=1)
        self.assertEqual((doc["source_errors"], doc["totals"]["stale"]), ([{"source": "stale:alpha", "error": "FileNotFoundError"}], 0))
        self.same_as_text(doc, "--findings", "alpha=" + str(self.tmp / "absent.csv"), code=1)
        doc = self.json("--findings", "alpha=" + cited)
        self.assertEqual(doc["totals"], zeros(items=1, stale=1))
        self.assertEqual([(i["kind"], i["date"], i["item"]) for i in doc["items"]], [("stale-cited", None, "FND-1")])

    def test_empty_store_is_one_line_with_no_items_exit_0(self):
        doc = self.json()
        self.assertEqual((doc["items"], doc["source_errors"], doc["totals"]), ([], [], zeros()))
        self.same_as_text(doc)

    def test_store_refusals_stay_one_plain_line_not_json_exit_1(self):
        for store, line in ((self.tmp / "absent", "store is not a directory"), (ROOT / "tests", "store inside repository")):
            out = self.run_tool("--json", store=store, code=1)
            self.assertEqual(out, line + "\n")
            self.assertEqual(out, self.run_tool(store=store, code=1))
            with self.assertRaises(ValueError):
                json.loads(out)

    def test_usage_errors_exit_2_with_no_json(self):
        for argv in (["--date", "2026-02-30"], ["--date", "2026-6-1"], ["--findings", "alpha"], ["--findings", "Alpha=x.csv"],
                     ["--findings", "a=x.csv", "--findings", "a=y.csv"], ["--json=yes"]):
            done = subprocess.run([sys.executable, "-B", str(TOOL), "--store", str(self.store), "--watchlist", WATCH, "--roster",
                                   str(self.roster), "--date", DATE, "--json", *argv], capture_output=True, text=True, check=False)
            self.assertEqual((done.returncode, done.stdout), (2, ""), argv)
        done = subprocess.run([sys.executable, "-B", str(TOOL), "--json", "--store", str(self.store)], capture_output=True, text=True,
                              check=False)
        self.assertEqual((done.returncode, done.stdout), (2, ""))


if __name__ == "__main__":
    unittest.main()
