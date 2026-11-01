"""flock_facts stale: facts past effective_to on --date, superseded or disputed, and the findings citing them (NB-DF-08)."""
import csv
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.flock.test_facts import TOOL, snapshot

_spec = importlib.util.spec_from_file_location("flock_facts_stale_under_test", TOOL)
facts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(facts)

DOC = hashlib.sha256(b"synthetic stale doc").hexdigest()
PLANTED = "Quillfeather"  # distinctive word in every value and quote; it must never reach the output
DATE = "2026-06-15"
FINDINGS = ["finding_id", "topic", "fact_ids", "claim"]


def fact(fact_type="retention", start="", end="", kind="policy", status="checked", agency="Alpha", page="1"):
    row = dict(zip(facts.V1, [agency, "REQ-S-1", fact_type, PLANTED + " " + fact_type + start + end, DOC, page,
                              PLANTED + " quote"]))
    row.update(kind=kind, status=status, effective_from=start, effective_to=end, added_by="stale-tester", added_at="2026-10-31")
    row["fact_id"] = facts.fact_id(row)
    return row


def summary(n, expired=0, superseded=0, disputed=0, not_yet=0, affected=0):
    return "facts=%d expired=%d superseded=%d disputed=%d not_yet=%d findings_affected=%d" % (
        n, expired, superseded, disputed, not_yet, affected)


class FactsStaleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = self.dir / "store"

    def agency(self, slug, rows, head=facts.V2):
        path = self.store / "agencies" / slug / "FACTS.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\r\n").writerows([head] + [[r[k] for k in head] if isinstance(r, dict) else r
                                                                          for r in rows])
        return path

    def findings(self, rows, head=FINDINGS):
        path = self.dir / "FINDINGS.csv"
        with open(path, "w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows([head] + rows)
        return str(path)

    def run_tool(self, *args, date=DATE):
        before = snapshot(self.dir)
        cmd = [sys.executable, "-B", str(TOOL), "stale", "--store", str(self.store)] + (["--date", date] if date else []) + list(args)
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
        self.assertEqual(snapshot(self.dir), before, "stale changed the store or the findings")
        self.assertNotIn(PLANTED.casefold(), (done.stdout + done.stderr).casefold())
        self.assertNotIn("Traceback", done.stderr)
        return done.returncode, done.stdout.splitlines(), done.stderr.strip()

    def test_expiry_boundary_day_before_is_expired_same_day_in_force(self):
        before, same, open_ended = fact("a", end="2026-06-14"), fact("b", end=DATE), fact("c")
        self.agency("alpha", [before, same, open_ended])
        self.assertEqual(self.run_tool(), (3, ["stale | alpha | %s | expired 2026-06-14" % before["fact_id"],
                                               summary(3, expired=1)], ""))
        self.assertEqual(self.run_tool(date="2026-06-14"), (0, [summary(3)], ""))

    def test_older_open_ended_fact_superseded_by_newer_in_force(self):
        old, new = fact(start="2025-01-01"), fact(start="2026-06-15")
        self.agency("alpha", [old, new])
        self.assertEqual(self.run_tool(), (3, ["stale | alpha | %s | superseded-by %s" % (old["fact_id"], new["fact_id"]),
                                               summary(2, superseded=1)], ""))
        later = fact(start="2026-07-01")  # newer not yet in force: nothing superseded, the newer one is not-yet
        self.agency("alpha", [old, later])
        self.assertEqual(self.run_tool(), (0, ["stale | alpha | %s | not-yet 2026-07-01" % later["fact_id"],
                                               summary(2, not_yet=1)], ""))
        mid = fact(start="2026-03-01")  # several later facts: the latest one in force supersedes; others superseded too
        self.agency("alpha", [new, old, mid])
        code, out, _ = self.run_tool()
        self.assertEqual((code, out), (3, ["stale | alpha | %s | superseded-by %s" % (old["fact_id"], new["fact_id"]),
                                           "stale | alpha | %s | superseded-by %s" % (mid["fact_id"], new["fact_id"]),
                                           summary(3, superseded=2)]))

    def test_no_supersession_across_kinds_dates_or_closed_windows(self):
        rows = [fact(start="2025-01-01", kind="record"), fact(start="2026-01-01"),  # same fact_type, different kind
                fact("hours", start="2025-01-01", end="2026-12-31"), fact("hours", start="2026-01-01"),  # older has its own end
                fact("audit"), fact("audit", start="2026-01-01"),  # older has no effective_from
                fact("logs", start="2026-01-01"), fact("logs", start="2026-01-01", page="2")]  # same start: neither older
        self.agency("alpha", rows)
        self.assertEqual(self.run_tool(), (0, [summary(8)], ""))
        closed = fact("hours", start="2025-01-01", end="2026-02-01")  # own effective_to: only the expiry rule applies
        self.agency("alpha", [closed, fact("hours", start="2026-01-01")])
        self.assertEqual(self.run_tool()[:2], (3, ["stale | alpha | %s | expired 2026-02-01" % closed["fact_id"],
                                                   summary(2, expired=1)]))

    def test_findings_citing_stale_facts_listed_once_per_stale_fact(self):
        expired, fresh, old, new = fact("a", end="2026-01-01"), fact("b"), fact(start="2025-01-01"), fact(start="2026-01-01")
        self.agency("alpha", [expired, fresh, old, new])
        path = self.findings([["FND-1", PLANTED, " %s ; %s;%s " % (expired["fact_id"], fresh["fact_id"], expired["fact_id"]),
                               PLANTED], ["FND-2", "t", fresh["fact_id"], "c"], [],
                              ["FND-3", "t", "%s;%s" % (old["fact_id"], expired["fact_id"]), "c"]])
        self.assertEqual(self.run_tool("--findings", path), (3, [
            "stale | alpha | %s | expired 2026-01-01" % expired["fact_id"],
            "stale | alpha | %s | superseded-by %s" % (old["fact_id"], new["fact_id"]),
            "stale | finding FND-1 | cites %s (expired 2026-01-01)" % expired["fact_id"],
            "stale | finding FND-3 | cites %s (superseded-by %s)" % (old["fact_id"], new["fact_id"]),
            "stale | finding FND-3 | cites %s (expired 2026-01-01)" % expired["fact_id"],
            summary(4, expired=1, superseded=1, affected=2)], ""))
        clean = self.findings([["FND-2", "t", "%s;%s" % (fresh["fact_id"], new["fact_id"]), "c"]])
        self.assertEqual(self.run_tool("--findings", clean)[:2], (0, [
            "stale | alpha | %s | expired 2026-01-01" % expired["fact_id"],
            "stale | alpha | %s | superseded-by %s" % (old["fact_id"], new["fact_id"]), summary(4, expired=1, superseded=1)]))

    def test_disputed_cited_is_listed_and_not_yet_cited_is_not(self):
        disputed, future = fact("a", status="disputed"), fact("b", start="2027-01-01")
        both = fact("c", end="2026-01-01", status="disputed")
        self.agency("alpha", [disputed, future, both])
        self.assertEqual(self.run_tool()[:2], (3, [
            "stale | alpha | %s | disputed" % disputed["fact_id"], "stale | alpha | %s | not-yet 2027-01-01" % future["fact_id"],
            "stale | alpha | %s | expired 2026-01-01" % both["fact_id"], "stale | alpha | %s | disputed" % both["fact_id"],
            summary(3, expired=1, disputed=2, not_yet=1)]))
        self.agency("alpha", [disputed, future])
        self.assertEqual(self.run_tool()[0], 0)  # disputed alone, no findings: not an exit-3 store
        path = self.findings([["FND-1", "t", disputed["fact_id"], "c"], ["FND-2", "t", future["fact_id"], "c"]])
        self.assertEqual(self.run_tool("--findings", path)[:2], (3, [
            "stale | alpha | %s | disputed" % disputed["fact_id"], "stale | alpha | %s | not-yet 2027-01-01" % future["fact_id"],
            "stale | finding FND-1 | cites %s (disputed)" % disputed["fact_id"], summary(2, disputed=1, not_yet=1, affected=1)]))

    def test_store_or_findings_errors_exit_1_with_no_stale_lines(self):
        expired = fact("a", end="2026-01-01")
        bad = dict(fact("b"), status="pending")
        self.agency("alpha", [expired, bad])
        self.assertEqual(self.run_tool(), (1, [], "file 1: row 3: status is invalid"))
        self.agency("alpha", [expired])
        self.agency("beta", [], head=facts.V1)
        self.assertEqual(self.run_tool(), (1, [], "file 2: row 1: header is not an accepted header"))
        self.assertEqual(self.run_tool("--agency", "Alpha")[0], 3)
        cite = [["FND-1", "t", expired["fact_id"], "c"]]
        for rows, head, err in ((cite, ["finding_id", "claim"], "findings: header lacks fact_ids"),
                                (cite + [["FND-2", "t"]], FINDINGS, "findings: row 3: columns have the wrong count")):
            self.assertEqual(self.run_tool("--agency", "alpha", "--findings", self.findings(rows, head)), (1, [], err))
        self.assertEqual(self.run_tool("--agency", "alpha", "--findings", str(self.dir / "absent.csv")),
                         (1, ["io: FileNotFoundError"], ""))
        self.assertEqual(self.run_tool("--agency", "gamma"), (1, [], "agency gamma: no FACTS.csv"))
        os.symlink(self.store / "agencies" / "alpha", self.store / "agencies" / "zeta")
        self.assertEqual(self.run_tool("--agency", "alpha")[0], 3)
        self.assertEqual(self.run_tool(), (1, [], "agencies/zeta: is a symlink; refusing"))
        self.assertEqual(self.run_tool("--agency", "zeta"), (1, [], "agencies/zeta: is a symlink; refusing"))

    def test_agency_filter_keeps_findings_to_that_agency(self):
        alpha, beta = fact("a", end="2026-01-01"), fact("a", end="2026-01-01", agency="Beta")
        self.agency("alpha", [alpha])
        self.agency("beta", [beta])
        path = self.findings([["FND-1", "t", beta["fact_id"], "c"]])
        self.assertEqual(self.run_tool("--findings", path)[0], 3)
        self.assertEqual(self.run_tool("--agency", "Alpha", "--findings", path), (0, [
            "stale | alpha | %s | expired 2026-01-01" % alpha["fact_id"], summary(1, expired=1)], ""))

    def test_usage_errors_exit_2(self):
        self.agency("alpha", [fact()])
        store = str(self.store)
        for argv in (["stale", "--date", DATE], ["stale", "--store", store], ["stale", "--store", store, "--date", "2026-02-30"],
                     ["stale", "--store", store, "--date", "2026-6-15"], ["stale", "x", "--store", store, "--date", DATE],
                     ["stale", "--store", store, "--date", DATE, "--agency", "!!"],
                     ["stale", "--store", store, "--date", DATE, "--list", "disputed"],
                     ["stale", "--store", store, "--date", DATE, "--text", store],
                     ["stale", "--store", store, "--date", DATE, "--check", store],
                     ["status", "--store", store, "--findings", store], ["--check", store, "--findings", store]):
            done = subprocess.run([sys.executable, "-B", str(TOOL), *argv], capture_output=True, text=True, check=False)
            self.assertEqual((done.returncode, done.stdout), (2, ""), argv)
        done = subprocess.run([sys.executable, "-B", str(TOOL), "--help"], capture_output=True, text=True, check=False)
        self.assertIn("stale --store DIR --date YYYY-MM-DD [--agency SLUG] [--findings FINDINGS.csv]", " ".join(done.stdout.split()))


if __name__ == "__main__":
    unittest.main()
