"""flock_today: one ranked, read-only organizer to-do list from requests, leads, stale facts, unreviewed facts and sweep (NB-DF-11)."""
import csv
import hashlib
import importlib.util
import os
from datetime import date, timedelta
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOLS, FIXTURES = ROOT / "tools", ROOT / "tests" / "fixtures" / "flock"
TOOL = TOOLS / "flock_today.py"
WATCH = str(FIXTURES / "watchlist-sweep.csv")
DATE = "2026-10-31"
AS_OF = date.fromisoformat(DATE)
PLANTED = "Quillfeather late overdue violated https://quillfeather.example/x 12 Main St"  # inputs only; never output
LEGAL = re.compile(r"\b(late|overdue)\b|violat", re.I)
SHA = hashlib.sha256(b"synthetic today doc").hexdigest()
SWEEP_AGENCY = "sample-city-police-department"
LEADS_HEAD = ["item_id", "agency", "first_seen", "state", "state_at", "state_by"]


def load(name):
    spec = importlib.util.spec_from_file_location("_today_test_" + name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


requests, facts = load("flock_requests"), load("flock_facts")
PKG = requests.law.load_package("us-ca")


def receipt_for(offset):  # a receipt date whose provisional determination date is AS_OF + offset
    target = AS_OF + timedelta(days=offset)
    return next(r for r in (target - timedelta(days=b) for b in range(60))
                if requests.law.deadline(PKG, r.isoformat()) == target)


def known(rid, agency, offset, status="open"):
    receipt = receipt_for(offset)
    return [rid, agency, (receipt - timedelta(days=1)).isoformat(), PLANTED[:100], receipt.isoformat(), "", "", status, SHA]


def unknown(rid, agency, sent="2026-10-20"):
    return [rid, agency, sent, "", "", "", "", "open", ""]


def fact(agency, fact_type, end="", status="checked"):
    row = dict(zip(facts.V1, [agency, "REQ-T-1", fact_type, PLANTED + fact_type, SHA, "1", PLANTED + " quote"]))
    row.update(kind="policy", status=status, effective_from="", effective_to=end, added_by="today-tester", added_at="2026-10-01")
    row["fact_id"] = facts.fact_id(row)
    return row


def snapshot(root):  # bytes and mtime of every path under root
    return {str(p): (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))}


def cols(lines):
    return [line.split(" | ")[1:5] for line in lines[:-1]]


def totals(items, reminders=0, leads=0, stale="-", reviews=0, unreviewed=0, receipt_unknown=0):
    return ("today | date=%s items=%d reminders=%d leads=%d stale=%s reviews=%d unreviewed=%d receipt_unknown=%d | "
            "provisional; not a legal status" % (DATE, items, reminders, leads, stale, reviews, unreviewed, receipt_unknown))


class FlockTodayTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = self.tmp / "store"
        (self.store / "agencies").mkdir(parents=True)
        roster = (FIXTURES / "roster-sample.csv").read_text().replace("local ALPR policy applies", "Quillfeather overdue")
        self.roster = self.tmp / "roster.csv"
        self.roster.write_text(roster)

    def write(self, rel, head, rows):
        path = self.store / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows([head] + [[r[k] for k in head] if isinstance(r, dict) else r
                                                                       for r in rows])
        return path

    def findings(self, rows):
        path = self.tmp / "FINDINGS.csv"
        with open(path, "w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows([["finding_id", "topic", "fact_ids", "claim"]] + rows)
        return str(path)

    def sweep_store(self):  # the test_sweep store build: one agency in ready-verify
        def tool(name, *args):
            done = subprocess.run([sys.executable, "-B", str(TOOLS / name), *args], capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        tool("flock_facts.py", "import", str(FIXTURES / "compare-facts.csv"), "--store", str(self.store), "--added-by", "t",
             "--date", "2026-10-30")
        shutil.copy(FIXTURES / "law-sample.csv", self.store / "LAW.csv")
        shutil.copy(FIXTURES / "policy-sample.csv", self.store / "agencies" / SWEEP_AGENCY / "POLICY.csv")
        tool("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", SWEEP_AGENCY, "--date", "2026-10-30",
             "--provider", "anthropic", "--model", "m", "--applicability", "Sample city police; Quillfeather overdue")
        run = self.store / "agencies" / SWEEP_AGENCY / "runs" / "2026-10-30"
        shutil.copy(FIXTURES / "findings-sample.csv", run / "FINDINGS.csv")
        return run

    def today(self, *args, store=None, roster=None, code=0):
        before = snapshot(self.tmp)
        cmd = [sys.executable, "-B", str(TOOL), "--store", str(store or self.store), "--watchlist", WATCH,
               "--roster", str(roster or self.roster), "--date", DATE, *args]
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
        self.assertEqual(snapshot(self.tmp), before, "flock_today changed a file")
        out = done.stdout + done.stderr
        self.assertNotIn("Traceback", out)
        self.assertIsNone(LEGAL.search(out), out)
        for word in ("quillfeather", "https://", "main st", "quote", "acknowledg"):
            self.assertNotIn(word, out.casefold())
        self.assertEqual(done.returncode, code, out)
        return done.stdout.splitlines()

    def test_one_item_of_every_kind_in_rank_and_tie_order_with_totals(self):
        run = self.sweep_store()
        self.write("agencies/alpha/REQUESTS.csv", requests.HEADER, [
            known("A-W2", "Alpha", 7), known("A-TODAY", "Alpha", 0), known("A-PASS", "Alpha", -2), known("A-W1", "Alpha", 7),
            known("A-FAR", "Alpha", 8), known("A-DONE", "Alpha", -5, status="closed"), unknown("A-UNK", "Alpha")])
        self.write("agencies/beta/REQUESTS.csv", requests.HEADER, [known("B-TODAY", "Beta", 0), unknown("B-UNK", "Beta")])
        expired, u1, u2 = fact("Alpha", "a", end="2026-01-01"), fact("Alpha", "b", status="unreviewed"), \
            fact("Alpha", "c", status="unreviewed")
        self.write("agencies/alpha/FACTS.csv", facts.V2, [expired, u1, u2])
        self.write("agencies/beta/FACTS.csv", facts.V2, [fact("Beta", "a", status="unreviewed")])
        self.write("LEADS.csv", LEADS_HEAD, [["L-3", "Alpha", "2026-10-02", "new", "", ""], ["L-1", "Alpha", "2026-10-02", "new", "", ""],
                                             ["L-2", "Beta", "2026-10-01", "new", "", ""], ["L-4", "Alpha", "2026-09-01", "requested",
                                                                                            "2026-09-02", "x"]])
        path = self.findings([["FND-3", PLANTED, expired["fact_id"], PLANTED], ["FND-2", "t", u1["fact_id"], "c"],
                              ["FND-1", "t", "%s;%s" % (u2["fact_id"], expired["fact_id"]), PLANTED]])
        lines = self.today("--findings", "alpha=" + path)
        self.assertEqual(cols(lines), [
            ["1", "reminder-passed", "alpha", "A-PASS"], ["1", "reminder-today", "alpha", "A-TODAY"],
            ["1", "reminder-today", "beta", "B-TODAY"], ["2", "reminder-week", "alpha", "A-W1"],
            ["2", "reminder-week", "alpha", "A-W2"], ["3", "stale-cited", "alpha", "FND-1"], ["3", "stale-cited", "alpha", "FND-3"],
            ["4", "lead-new", "beta", "L-2"], ["4", "lead-new", "alpha", "L-1"], ["4", "lead-new", "alpha", "L-3"],
            ["5", "review-wait", SWEEP_AGENCY, "ready-verify 2026-10-30"], ["6", "facts-unreviewed", "alpha", "2"],
            ["6", "facts-unreviewed", "beta", "1"], ["7", "receipt-unknown", "alpha", "A-UNK"], ["7", "receipt-unknown", "beta", "B-UNK"]])
        self.assertEqual(lines[-1], totals(15, reminders=5, leads=3, stale=2, reviews=1, unreviewed=2, receipt_unknown=2))
        q = lambda p: shlex.quote(str(p))  # noqa: E731
        nxt = [line.split(" | ", 5)[5] for line in lines[:-1]]
        self.assertEqual(nxt[0], "reminder date passed 2 days ago (provisional); python3 %s due %s --as-of %s"
                         % (q(TOOLS / "flock_requests.py"), q(self.store), DATE))
        self.assertTrue(nxt[1].startswith("reminder date today (provisional); ") and nxt[3].startswith("in 7 days (provisional); "))
        self.assertEqual(nxt[5], "cites %s (expired 2026-01-01); python3 %s stale --store %s --date %s --agency alpha --findings %s"
                         % (expired["fact_id"], q(TOOLS / "flock_facts.py"), q(self.store), DATE, q(path)))
        self.assertEqual(nxt[7], "python3 %s lead --store %s --item L-2 --state requested|ignored --by <name>"
                         % (q(TOOLS / "flock_monitor.py"), q(self.store)))
        self.assertEqual(nxt[10], "python3 %s start %s --provider openai --model <model>; verify card" % (q(TOOLS / "flock_verify.py"), q(run)))
        self.assertEqual(nxt[11], "python3 %s status --store %s --agency alpha --list unreviewed" % (q(TOOLS / "flock_facts.py"), q(self.store)))
        self.assertEqual(nxt[13], "receipt unknown, sent 11 days ago; python3 %s record %s --agency alpha --request A-UNK --step receipt"
                         % (q(TOOLS / "flock_requests.py"), q(self.store)))
        (run / "FINDINGS.csv").unlink()  # back to an open compare run: still a review wait, with the sweep's own next step
        wait = [line for line in self.today("--findings", "alpha=" + path) if "review-wait" in line]
        self.assertEqual(wait, ["today | 5 | review-wait | %s | compare-open 2026-10-30 | resume compare card (REMAINING.md)" % SWEEP_AGENCY])

    def test_reminder_window_seven_days_listed_eight_not_today_and_passed(self):
        self.write("agencies/alpha/REQUESTS.csv", requests.HEADER, [
            known("R-7", "Alpha", 7), known("R-8", "Alpha", 8), known("R-0", "Alpha", 0), known("R-1", "Alpha", 1),
            known("R-M1", "Alpha", -1), known("R-OLD", "Alpha", -40, status="produced")])
        lines = self.today()
        self.assertEqual(cols(lines), [["1", "reminder-passed", "alpha", "R-M1"], ["1", "reminder-today", "alpha", "R-0"],
                                       ["2", "reminder-week", "alpha", "R-1"], ["2", "reminder-week", "alpha", "R-7"]])
        self.assertEqual(lines[-1], totals(4, reminders=4))
        self.assertEqual(lines[0].split(" | ")[5].split(";")[0], "reminder date passed 1 day ago (provisional)")

    def test_missing_leads_is_none_and_malformed_or_linked_leads_is_a_source_error(self):
        self.write("agencies/alpha/REQUESTS.csv", requests.HEADER, [known("R-0", "Alpha", 0)])
        self.assertEqual(self.today(), ["today | 1 | reminder-today | alpha | R-0 | " + self.today()[0].split(" | ", 5)[5],
                                        totals(1, reminders=1)])
        bad = self.write("LEADS.csv", LEADS_HEAD[:-1], [["L-1", "Alpha", "2026-10-02", "new", ""]])
        lines = self.today(code=1)
        self.assertEqual(lines[0], "today | 0 | source-error | leads | Fail")
        self.assertEqual(cols(lines[1:]), [["1", "reminder-today", "alpha", "R-0"]])
        self.assertEqual(lines[-1], totals(1, reminders=1))
        self.write("LEADS.csv", LEADS_HEAD, [["L-1", "Alpha", "2026-10-02", "new", "", ""], ["L-2", "Alpha"]])
        self.assertEqual(self.today(code=1)[0], "today | 0 | source-error | leads | Fail")
        bad.unlink()
        real = self.write("../real-leads.csv", LEADS_HEAD, [["L-1", "Alpha", "2026-10-02", "new", "", ""]])
        os.symlink(real, self.store / "LEADS.csv")  # a linked LEADS.csv is refused, never followed
        self.assertEqual(self.today(code=1)[0], "today | 0 | source-error | leads | Fail")

    def test_other_bad_sources_are_one_line_each_and_the_rest_still_listed(self):
        self.write("agencies/alpha/REQUESTS.csv", requests.HEADER, [known("R-0", "Alpha", 0), ["R-0"] + known("x", "Alpha", 1)[1:]])
        self.write("agencies/alpha/FACTS.csv", facts.V2, [dict(fact("Alpha", "a"), status="pending")])
        self.write("LEADS.csv", LEADS_HEAD, [["L-1", "Alpha", "2026-10-02", "new", "", ""]])
        roster = self.tmp / "bad-roster.csv"
        roster.write_text("watch_agency,slug\nX,x\n")
        lines = self.today(roster=roster, code=1)
        self.assertEqual(lines[:3], ["today | 0 | source-error | requests | Fail", "today | 0 | source-error | facts | Fail",
                                     "today | 0 | source-error | sweep | Fail"])
        self.assertEqual(cols(lines[3:]), [["4", "lead-new", "alpha", "L-1"]])
        self.assertEqual(lines[-1], totals(1, leads=1))

    def test_findings_absent_skips_stale_and_bad_findings_forms_exit_2(self):
        self.write("agencies/alpha/FACTS.csv", facts.V2, [fact("Alpha", "a", end="2026-01-01")])
        self.write("agencies/beta/FACTS.csv", facts.V2, [fact("Beta", "a", end="2026-01-01")])
        cited = self.findings([["FND-1", "t", fact("Alpha", "a", end="2026-01-01")["fact_id"], "c"]])
        self.assertEqual(self.today(), [totals(0)])
        self.assertEqual(self.today("--findings", "beta=" + cited), [totals(0, stale=0)])  # beta's stale facts are not cited
        self.assertEqual(cols(self.today("--findings", "alpha=" + cited)), [["3", "stale-cited", "alpha", "FND-1"]])
        lines = self.today("--findings", "alpha=" + str(self.tmp / "absent.csv"), code=1)
        self.assertEqual(lines, ["today | 0 | source-error | stale:alpha | FileNotFoundError", totals(0, stale=0)])
        self.assertEqual(self.today("--findings", "gamma=" + cited, code=1)[0], "today | 0 | source-error | stale:gamma | Fail")
        for bad in ("alpha", "Alpha=" + cited, "=" + cited, "alpha=", "al_pha=" + cited, "-alpha=" + cited):
            done = subprocess.run([sys.executable, "-B", str(TOOL), "--store", str(self.store), "--watchlist", WATCH, "--roster",
                                   str(self.roster), "--date", DATE, "--findings", bad], capture_output=True, text=True, check=False)
            self.assertEqual((done.returncode, done.stdout), (2, ""), bad)
        twice = ["--findings", "alpha=" + cited, "--findings", "alpha=" + cited]
        for argv in (twice, ["--date", "2026-02-30"], ["--date", "2026-6-1"]):
            done = subprocess.run([sys.executable, "-B", str(TOOL), "--store", str(self.store), "--watchlist", WATCH, "--roster",
                                   str(self.roster), "--date", DATE, *argv], capture_output=True, text=True, check=False)
            self.assertEqual((done.returncode, done.stdout), (2, ""), argv)
        done = subprocess.run([sys.executable, "-B", str(TOOL), "--store", str(self.store)], capture_output=True, text=True, check=False)
        self.assertEqual((done.returncode, done.stdout), (2, ""))

    def test_store_with_no_agencies_is_zero_items_and_repo_or_missing_store_refused(self):
        self.assertEqual(self.today(), [totals(0)])
        self.assertEqual(self.today(store=self.tmp / "absent", code=1), ["store is not a directory"])
        self.assertEqual(self.today(store=ROOT / "tests", code=1), ["store inside repository"])


if __name__ == "__main__":
    unittest.main()
