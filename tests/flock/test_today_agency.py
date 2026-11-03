"""flock_today --agency SLUG (RF0510-12): items and totals for the chosen agencies only; an unknown slug refused first."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest

from tests.flock import test_today as base
from tests.flock.test_today import (DATE, LEADS_HEAD, SWEEP_AGENCY, TOOL, WATCH, cols, fact, facts, known, load, requests,
                                    totals, unknown)

LEADS_FAIL = "today | 0 | source-error | leads | Fail"
SOURCES = ("reminders", "leads", "stale_cited", "unreviewed", "reviews")


def scoped(line, agencies):  # the totals line with the agencies= note before " | "
    return line.replace(" | provisional", " agencies=%s | provisional" % agencies)


class FlockTodayAgencyTest(unittest.TestCase):
    setUp, write, findings, sweep_store, today = (base.FlockTodayTest.setUp, base.FlockTodayTest.write,
                                                  base.FlockTodayTest.findings, base.FlockTodayTest.sweep_store,
                                                  base.FlockTodayTest.today)

    def two(self):  # agencies alpha and beta, each with a reminder, a new lead and unreviewed facts
        self.write("agencies/alpha/REQUESTS.csv", requests.HEADER, [known("A-0", "Alpha", 0), unknown("A-UNK", "Alpha")])
        self.write("agencies/beta/REQUESTS.csv", requests.HEADER, [known("B-3", "Beta", 3)])
        expired = fact("Alpha", "x", end="2026-01-01")
        self.write("agencies/alpha/FACTS.csv", facts.V2, [expired, fact("Alpha", "a", status="unreviewed"),
                                                          fact("Alpha", "b", status="unreviewed")])
        self.write("agencies/beta/FACTS.csv", facts.V2, [fact("Beta", "a", status="unreviewed")])
        self.write("LEADS.csv", LEADS_HEAD, [["L-1", "Alpha", "2026-10-02", "new", "", ""],
                                             ["L-2", "Beta", "2026-10-01", "new", "", ""]])
        return self.findings([["FND-1", "t", expired["fact_id"], "c"]])

    def usage(self, *args):  # a usage error: exit 2, nothing on stdout
        done = subprocess.run([sys.executable, "-B", str(TOOL), "--store", str(self.store), "--watchlist", WATCH, "--roster",
                               str(self.roster), "--date", DATE, *args], capture_output=True, text=True, check=False)
        self.assertEqual((done.returncode, done.stdout), (2, ""), args)
        return done.stderr

    def in_process(self, *args, store=None):  # every source patched to record a call; returns (exit, stdout, calls)
        mod, called = load("flock_today"), []
        for name in SOURCES:
            setattr(mod, name, lambda *_, n=name: called.append(n) or [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mod.main(["--store", str(store or self.store), "--watchlist", WATCH, "--roster", str(self.roster),
                             "--date", DATE, *args])
        return code, out.getvalue(), called

    def test_one_agency_lists_only_its_items_and_totals(self):
        self.sweep_store()
        path = self.two()
        full = self.today("--findings", "alpha=" + path)
        lines = self.today("--agency", "alpha", "--findings", "alpha=" + path)
        self.assertEqual(cols(lines), [["1", "reminder-today", "alpha", "A-0"], ["3", "stale-cited", "alpha", "FND-1"],
                                       ["4", "lead-new", "alpha", "L-1"], ["6", "facts-unreviewed", "alpha", "2"],
                                       ["7", "receipt-unknown", "alpha", "A-UNK"]])
        self.assertEqual(lines[:-1], [line for line in full[:-1] if line.split(" | ")[3] == "alpha"])  # same lines, unchanged
        self.assertEqual(lines[-1], scoped(totals(5, reminders=1, leads=1, stale=1, unreviewed=1, receipt_unknown=1), "alpha"))
        self.assertEqual(self.today("--agency", "alpha")[-1],  # no --findings: stale stays "-"
                         scoped(totals(4, reminders=1, leads=1, unreviewed=1, receipt_unknown=1), "alpha"))
        lines = self.today("--agency", "beta")
        self.assertEqual(cols(lines), [["2", "reminder-week", "beta", "B-3"], ["4", "lead-new", "beta", "L-2"],
                                       ["6", "facts-unreviewed", "beta", "1"]])
        self.assertEqual(lines[-1], scoped(totals(3, reminders=1, leads=1, unreviewed=1), "beta"))
        lines = self.today("--agency", SWEEP_AGENCY)
        self.assertEqual(cols(lines), [["5", "review-wait", SWEEP_AGENCY, "ready-verify 2026-10-30"]])
        self.assertEqual(lines[-1], scoped(totals(1, reviews=1), SWEEP_AGENCY))
        doc = json.loads("\n".join(self.today("--json", "--agency", "beta")))  # --json carries the same scope
        self.assertEqual(list(doc)[:4], ["schema", "review_required", "date", "agencies"])
        self.assertEqual((doc["agencies"], [i["slug"] for i in doc["items"]]), (["beta"], ["beta"] * 3))
        self.assertEqual((doc["totals"]["items"], doc["totals"]["reminders"], doc["totals"]["stale"]), (3, 1, None))
        self.assertNotIn("agencies", json.loads("\n".join(self.today("--json"))))

    def test_all_agencies_chosen_is_the_unfiltered_list_and_no_agency_is_unchanged(self):
        self.two()
        full = self.today()
        self.assertEqual(cols(full), [["1", "reminder-today", "alpha", "A-0"], ["2", "reminder-week", "beta", "B-3"],
                                      ["4", "lead-new", "beta", "L-2"], ["4", "lead-new", "alpha", "L-1"],
                                      ["6", "facts-unreviewed", "alpha", "2"], ["6", "facts-unreviewed", "beta", "1"],
                                      ["7", "receipt-unknown", "alpha", "A-UNK"]])
        self.assertEqual(full[-1], totals(7, reminders=2, leads=2, unreviewed=2, receipt_unknown=1))  # main's line, no note
        both = self.today("--agency", "alpha", "--agency", "beta")
        self.assertEqual(both[:-1], full[:-1])
        self.assertEqual(both[-1], scoped(full[-1], "alpha,beta"))
        self.assertEqual(self.today("--agency", "beta", "--agency", "alpha")[-1], scoped(full[-1], "beta,alpha"))
        self.assertEqual(self.today(), full)  # without --agency the output is the same run after run

    def test_bad_agency_forms_and_findings_outside_the_chosen_set_exit_2(self):
        path = self.two()
        for bad in ("Bad_Slug", "Alpha", "alpha-", "a--b", "", "alpha/..", "../alpha"):
            self.assertIn("--agency must be a slug", self.usage("--agency=" + bad))
        self.assertIn("no repeats", self.usage("--agency", "alpha", "--agency", "beta", "--agency", "alpha"))
        many = [arg for n in range(21) for arg in ("--agency", "s%d" % n)]
        self.assertIn("at most 20", self.usage(*many))
        self.assertIn("--findings slug not in --agency", self.usage("--agency", "alpha", "--findings", "beta=" + path))
        self.assertIn("--findings slug not in --agency", self.usage("--findings", "gamma=x.csv", "--agency", "alpha",
                                                                    "--agency", "beta"))
        for n in range(20):  # exactly 20 real folders is accepted
            (self.store / "agencies" / ("s%d" % n)).mkdir()
        self.assertEqual(self.today(*many[:40]), [scoped(totals(0), ",".join("s%d" % n for n in range(20)))])

    def test_unknown_or_linked_agency_refused_before_any_source(self):
        self.two()
        (self.tmp / "elsewhere" / "zz").mkdir(parents=True)
        refused = "today | refused | unknown agency zz\n"
        self.assertEqual(self.in_process("--agency", "zz"), (1, refused, []))
        self.assertEqual(self.in_process("--agency", "alpha", "--agency", "zz"), (1, refused, []))
        self.assertEqual(self.in_process("--agency", "zz", "--json"), (1, refused, []))
        os.symlink(self.tmp / "elsewhere" / "zz", self.store / "agencies" / "zz")  # a linked folder is not an agency
        self.assertEqual(self.in_process("--agency", "zz"), (1, refused, []))
        (self.store / "agencies" / "yy").write_text("")  # a file is not an agency
        self.assertEqual(self.in_process("--agency", "yy"), (1, "today | refused | unknown agency yy\n", []))
        linked = self.tmp / "linked-store"  # an agencies folder that is itself a link
        linked.mkdir()
        os.symlink(self.store / "agencies", linked / "agencies")
        self.assertEqual(self.in_process("--agency", "alpha", store=linked), (1, "today | refused | unknown agency alpha\n", []))
        self.assertEqual(self.in_process("--agency", "alpha"), (0, scoped(totals(0), "alpha") + "\n",
                                                                ["reminders", "leads", "unreviewed", "reviews"]))
        self.assertEqual(self.today("--agency", "zz", code=1), ["today | refused | unknown agency zz"])

    def test_source_error_still_reported_with_the_agency_items(self):
        self.two()
        self.write("LEADS.csv", LEADS_HEAD[:-1], [["L-1", "Alpha", "2026-10-02", "new", ""]])
        lines = self.today("--agency", "alpha", code=1)
        self.assertEqual(lines[0], LEADS_FAIL)
        self.assertEqual(cols(lines[1:]), [["1", "reminder-today", "alpha", "A-0"], ["6", "facts-unreviewed", "alpha", "2"],
                                           ["7", "receipt-unknown", "alpha", "A-UNK"]])
        self.assertEqual(lines[-1], scoped(totals(3, reminders=1, unreviewed=1, receipt_unknown=1), "alpha"))
        self.write("agencies/beta/REQUESTS.csv", requests.HEADER, [known("B-3", "Beta", 3), known("B-3", "Beta", 4)])
        lines = self.today("--agency", "alpha", code=1)  # another agency's broken requests still fail the store-wide source
        self.assertEqual(lines[:2], ["today | 0 | source-error | requests | Fail", LEADS_FAIL])
        self.assertEqual(cols(lines[2:]), [["6", "facts-unreviewed", "alpha", "2"]])

    def test_chosen_agency_with_nothing_due_is_zero_items(self):
        self.two()
        (self.store / "agencies" / "gamma").mkdir()
        self.assertEqual(self.today("--agency", "gamma"), [scoped(totals(0), "gamma")])
        self.assertEqual(self.today("--agency", "gamma", "--json")[0].count('"items": []'), 1)


if __name__ == "__main__":
    unittest.main()
