import csv
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
FIXTURES = ROOT / "tests" / "fixtures" / "flock"
AGENCY, SECOND = "sample-city-police-department", "second-city-police"
WATCH, ROSTER = str(FIXTURES / "watchlist-sweep.csv"), str(FIXTURES / "roster-sample.csv")
HEAD = ("request_id,agency,sent,receipt_evidence,receipt_date,determination_date,extension_notice_date,"
        "status,source_sha256\n")
LINE = "funnel | %s | requested=%s posted=%s facts=%s cited=%s findings=%s verified=%s public_ok=%s | stalls_at %s"
SHA = [c * 64 for c in "abcd"]


def run(tool, *args):
    done = subprocess.run([sys.executable, "-B", str(TOOLS / tool), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


class FlockSweepFunnelTest(unittest.TestCase):
    def setUp(self):  # synthetic store: test_sweep's fixtures, run scaffolded only where a test needs one
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = self.tmp / "store"
        code, _ = run("flock_facts.py", "import", str(FIXTURES / "compare-facts.csv"), "--store", str(self.store),
                      "--added-by", "tester", "--date", "2026-10-30")
        self.assertEqual(code, 0)
        shutil.copy(FIXTURES / "law-sample.csv", self.store / "LAW.csv")
        shutil.copy(FIXTURES / "policy-sample.csv", self.store / "agencies" / AGENCY / "POLICY.csv")
        (self.store / "agencies" / SECOND).mkdir()
        self.run_dir = self.store / "agencies" / AGENCY / "runs" / "2026-10-30"
        self.out = self.tmp / "out"
        with open(self.store / "agencies" / AGENCY / "FACTS.csv", newline="") as handle:
            self.fact_ids = {r["fact_id"] for r in csv.DictReader(handle)}
        self.requests(AGENCY, "Sample City Police Department", 2)
        self.requests(SECOND, "Second City Police", 1)

    def requests(self, slug, name, n, text=None):
        rows = "".join("REQ-%d,%s,2026-10-01,,,,,open,\n" % (i, name) for i in range(n))
        (self.store / "agencies" / slug / "REQUESTS.csv").write_text(text if text is not None else HEAD + rows)

    def scaffold(self):
        code, _ = run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", AGENCY,
                      "--date", "2026-10-30", "--provider", "anthropic", "--model", "claude-opus-5-5",
                      "--applicability", "Sample city police; local ALPR policy applies")
        self.assertEqual(code, 0)

    def finish(self):
        self.scaffold()
        shutil.copy(FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")
        self.assertEqual(run("flock_verify.py", "start", str(self.run_dir), "--provider", "openai", "--model", "m")[0], 0)
        shutil.copy(FIXTURES / "verify-sample.csv", self.run_dir / "VERIFY.csv")
        self.assertEqual(run("flock_verify.py", "finish", str(self.run_dir))[0], 0)

    def inventory(self, rows, name="inv.csv"):
        path = self.tmp / name
        path.write_text("request,doc_id,title,sha256,analysis_only\n" + "".join(
            "REQ-0,D%d,Title %d,%s,%s\n" % (n, n, sha, only) for n, (sha, only) in enumerate(rows)))
        return path

    def listing(self):
        return sorted((str(p), p.stat().st_mtime_ns) for p in self.store.rglob("*"))

    def funnel(self, *args, out=None, roster=ROSTER):
        before = self.listing()
        code, text = run("flock_sweep.py", "funnel", "--watchlist", WATCH, "--roster", roster, "--store",
                         str(self.store), "--out", str(out or self.out), *args)
        self.assertEqual(self.listing(), before)  # read-only on the store
        return code, text

    def table(self):  # FUNNEL.csv rows by slug; the file must match stdout line for line
        with open(self.out / "FUNNEL.csv", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["slug", "stage", "requested", "posted", "facts", "facts_cited", "findings",
                                   "verified", "public_ok", "stalls_at"])
        self.assertEqual((self.out / "FUNNEL.csv").stat().st_mode & 0o777, 0o600)
        return rows[1:], {r[0]: r[2:] for r in rows[1:]}

    def check_stdout(self, text, rows):
        lines = text.splitlines()
        self.assertEqual(lines[:-1], [LINE % tuple([r[0]] + r[2:]) for r in rows])
        stalled, unknown = sum(r[-1] != "-" for r in rows), sum(v == "-" for r in rows for v in r[2:-1])
        self.assertEqual(lines[-1], "funnel | agencies=%d stalled=%d unknown_cells=%d" % (len(rows), stalled, unknown))
        return lines

    def test_every_stage_populated_no_stall_and_counts_exact(self):
        self.finish()
        inv = self.inventory([(SHA[0], "no"), (SHA[0], "no"), (SHA[1], "no"), (SHA[1], "yes"), (SHA[2], "no"),
                              (SHA[3], "")])
        code, text = self.funnel("--inventory", "%s=%s" % (AGENCY, inv))
        self.assertEqual(code, 0, text)
        rows, by = self.table()
        lines = self.check_stdout(text, rows)
        with open(FIXTURES / "findings-sample.csv", newline="") as handle:
            found = list(csv.DictReader(handle))
        cited = {i.strip() for f in found for i in f["fact_ids"].split(";") if i.strip()} & self.fact_ids
        self.assertTrue(cited)
        # posted: a and c only (b mixed no/yes, d blank analysis_only); 7 findings, O2 rejected; PUBLIC-OK has 4 rows
        self.assertEqual(by[AGENCY], ["2", "2", str(len(self.fact_ids)), str(len(cited)), "7", "6", "4", "-"])
        self.assertEqual([r[0] for r in rows], [AGENCY, SECOND, "campus-police", "parks"])
        self.assertEqual(rows[0][1], "finished")
        self.assertEqual(lines[-1], "funnel | agencies=4 stalled=3 unknown_cells=6")
        for claim in [f["claim"] for f in found] + list(self.fact_ids) + [str(inv), str(self.store)]:
            self.assertNotIn(claim, text)

    def test_requests_but_no_facts_stalls_at_facts(self):
        code, text = self.funnel()
        self.assertEqual(code, 0, text)
        rows, by = self.table()
        self.check_stdout(text, rows)
        self.assertEqual(by[SECOND], ["1", "-", "0", "-", "0", "0", "0", "facts"])
        self.assertEqual(by["campus-police"], ["0", "-", "0", "-", "0", "0", "0", "requested"])  # no REQUESTS.csv

    def test_facts_but_no_run_stalls_at_findings(self):
        code, text = self.funnel()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.table()[1][AGENCY], ["2", "-", str(len(self.fact_ids)), "-", "0", "0", "0", "findings"])
        self.scaffold()  # a run with no findings yet: nothing cited
        self.assertEqual(self.funnel()[0], 0)
        self.assertEqual(self.table()[1][AGENCY][3:], ["0", "0", "0", "0", "facts_cited"])

    def test_cited_fact_absent_from_facts_not_counted(self):
        self.finish()
        code, _ = self.funnel()
        base = self.table()[1][AGENCY]
        with open(self.run_dir / "FINDINGS.csv", "a", newline="") as handle:  # no VERIFY row, cites an unknown fact
            csv.writer(handle, lineterminator="\n").writerow(
                ["X1", "audits", "record-gap", "weak", "Synthetic.", "F-ffffffffffff; F-eeeeeeeeeeee", "", "", "", "", ""])
        code, text = self.funnel()
        self.assertEqual(code, 0, text)
        got = self.table()[1][AGENCY]
        self.assertEqual((got[3], got[4], got[5]), (base[3], "8", "6"))

    def test_posted_distinct_sha_and_mixed_analysis_only(self):
        cases = (([(SHA[0], "no"), (SHA[0], "no")], "1"), ([(SHA[0], "no"), (SHA[0], "yes")], "0"),
                 ([(SHA[0], "yes"), (SHA[0], "no"), (SHA[1], "no")], "1"), ([], "0"))
        for rows, posted in cases:
            with self.subTest(rows=rows):
                code, text = self.funnel("--inventory", "%s=%s" % (SECOND, self.inventory(rows)))
                self.assertEqual(code, 0, text)
                got = self.table()[1][SECOND]
                self.assertEqual((got[1], got[-1]), (posted, "posted" if posted == "0" else "facts"))
                self.assertEqual(self.table()[1][AGENCY][1], "-")  # no --inventory for this slug: unknown, skipped

    def test_inventory_usage_errors_exit_2_nothing_written(self):
        inv = str(self.inventory([(SHA[0], "no")]))
        cases = (("--inventory", "unknown-slug=" + inv), ("--inventory", AGENCY), ("--inventory", "=" + inv),
                 ("--inventory", AGENCY + "="), ("--inventory", "Bad_Slug=" + inv),
                 ("--inventory", AGENCY + "=" + inv, "--inventory", AGENCY + "=" + inv))
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(self.funnel(*args)[0], 2)
                self.assertFalse(self.out.exists())
        code, _ = run("flock_sweep.py", "status", "--watchlist", WATCH, "--roster", ROSTER, "--store", str(self.store),
                      "--inventory", AGENCY + "=" + inv)
        self.assertEqual(code, 2)
        code, _ = run("flock_sweep.py", "funnel", "--watchlist", WATCH, "--roster", ROSTER, "--store", str(self.store))
        self.assertEqual(code, 2)  # funnel needs --out

    def test_malformed_requests_unknown_cell_exit_0(self):
        bad = (HEAD.replace("status", "state") + "REQ-1,Second City Police,2026-10-01,,,,,open,\n",
               HEAD + "REQ-1,Second City Police,10/01/2026,,,,,open,\n", HEAD + "REQ-1,Elsewhere,2026-10-01,,,,,open,\n",
               HEAD + "REQ-1,Second City Police,2026-10-01,,,,,open\n", b"\xff\xfe\x00bad")
        for text in bad:
            with self.subTest(text=text):
                path = self.store / "agencies" / SECOND / "REQUESTS.csv"
                path.write_bytes(text) if isinstance(text, bytes) else path.write_text(text)
                code, out = self.funnel()
                self.assertEqual(code, 0, out)
                rows, by = self.table()
                self.assertEqual(by[SECOND], ["-", "-", "0", "-", "0", "0", "0", "facts"])
                self.assertEqual(self.check_stdout(out, rows)[-1], "funnel | agencies=4 stalled=4 unknown_cells=9")
                self.assertNotIn("REQ-1", out)

    def test_unreadable_findings_or_inventory_exit_1_nothing_written(self):
        self.finish()
        good = self.inventory([(SHA[0], "no")])
        for name, args in (("missing", ("--inventory", "%s=%s" % (AGENCY, self.tmp / "none.csv"))),
                           ("header", ("--inventory", "%s=%s" % (AGENCY, self.tmp / "bad.csv"))),
                           ("findings", ("--inventory", "%s=%s" % (AGENCY, good)))):
            with self.subTest(name=name):
                (self.tmp / "bad.csv").write_text("request,doc_id,title\nREQ-0,D1,T\n")
                if name == "findings":
                    (self.run_dir / "FINDINGS.csv").write_bytes(b"finding_id,topic\n\xff\xfe bad\n")
                code, text = self.funnel(*args)
                self.assertEqual(code, 1, text)
                self.assertFalse(self.out.exists())
                self.assertNotIn("funnel |", text)
                self.assertNotIn(str(self.tmp), text)

    def test_out_inside_store_or_repo_refused(self):
        for out, why in ((self.store / "funnel", "--out inside store\n"), (ROOT / "funnel-out", "--out inside repository\n")):
            with self.subTest(why=why):
                self.assertEqual(self.funnel(out=out), (1, why))
                self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
