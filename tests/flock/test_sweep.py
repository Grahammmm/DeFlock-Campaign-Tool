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
AGENCY = "sample-city-police-department"
WATCH, ROSTER = str(FIXTURES / "watchlist-sweep.csv"), str(FIXTURES / "roster-sample.csv")
GATE = "golden gate: Morro Bay F3/F4 golden must pass first"


def run(tool, *args):
    done = subprocess.run([sys.executable, "-B", str(TOOLS / tool), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


class FlockSweepTest(unittest.TestCase):
    def setUp(self):  # the store build of tests/flock/test_verify.py; F1-F4 fixtures are read only
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = self.tmp / "store"
        code, _ = run("flock_facts.py", "import", str(FIXTURES / "compare-facts.csv"), "--store", str(self.store),
                      "--added-by", "tester", "--date", "2026-10-30")
        self.assertEqual(code, 0)
        shutil.copy(FIXTURES / "law-sample.csv", self.store / "LAW.csv")
        shutil.copy(FIXTURES / "policy-sample.csv", self.store / "agencies" / AGENCY / "POLICY.csv")
        self.run_dir = self.store / "agencies" / AGENCY / "runs" / "2026-10-30"
        code, _ = run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", AGENCY,
                      "--date", "2026-10-30", "--provider", "anthropic", "--model", "claude-opus-5-5",
                      "--applicability", "Sample city police; local ALPR policy applies")
        self.assertEqual(code, 0)
        (self.store / "agencies" / "second-city-police").mkdir()

    def sweep(self, command, *args, watch=WATCH, roster=ROSTER, store=None):
        return run("flock_sweep.py", command, "--watchlist", watch, "--roster", roster, "--store", str(store or self.store), *args)

    def finish(self):
        shutil.copy(FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")
        self.assertEqual(run("flock_verify.py", "start", str(self.run_dir), "--provider", "openai", "--model", "m")[0], 0)
        shutil.copy(FIXTURES / "verify-sample.csv", self.run_dir / "VERIFY.csv")
        self.assertEqual(run("flock_verify.py", "finish", str(self.run_dir))[0], 0)

    def status(self, stage, finished, ready):
        before = sorted((str(p), p.stat().st_mtime_ns) for p in self.store.rglob("*"))
        code, out = self.sweep("status")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in self.store.rglob("*")), before)
        lines = out.splitlines()
        rows = {r["slug"] or r["watch_agency"]: r for r in csv.DictReader(lines[:-1])}
        self.assertEqual(lines[-1], "agencies=5 finished=%d ready=%d blocked=%d unmapped=1" % (finished, ready, 4 - finished - ready))
        expected = {AGENCY: stage, "second-city-police": "no-facts", "campus-police": "scope-hold",
                    "parks": "out-of-scope", "Unlisted Agency": "unmapped"}
        self.assertEqual({k: r["stage"] for k, r in rows.items()}, expected)
        return rows[AGENCY]

    def test_stages(self):
        self.status("compare-open", 0, 0)
        shutil.copy(FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")
        row = self.status("ready-verify", 0, 1)
        self.assertIn("start %s --provider openai --model <model>; verify card" % self.run_dir, row["next"])
        self.finish()
        row = self.status("finished", 1, 0)
        self.assertEqual((row["public_ok"], row["questions"], row["author_family"], row["verifier_family"]),
                         ("4", "2", "anthropic", "openai"))

    def test_fail_closed(self):
        l3 = self.tmp / "l3.csv"
        l3.write_text("agency,kind,url,host,source_file\nX,news,https://x.example/,x.example,a.html\n")
        self.assertEqual(self.sweep("status", watch=str(l3)), (1, "watchlist row 1: header is not the accepted header "
                                                                  "(convert L3 to the F7 header first)\n"))
        text = (FIXTURES / "roster-sample.csv").read_text()
        cases = ((text.replace("yes,Second city police; local ALPR policy applies", "yes,"),
                  "roster row 3: applicability is required when in_scope=yes"),
                 (text + "Parks,parks,no,,\n", "roster row 6: watch_agency is empty or duplicated"))
        for roster, expected in cases:
            with self.subTest(expected=expected):
                (self.tmp / "roster.csv").write_text(roster)
                code, out = self.sweep("status", roster=str(self.tmp / "roster.csv"))
                self.assertEqual(code, 1)
                self.assertIn(expected, out)
        shutil.copy(FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")
        code, out = self.sweep("plan")
        self.assertEqual(code, 0)
        self.assertIn("## [RUN] %s verify 2026-10-30" % AGENCY, out)
        (self.tmp / "w.csv").write_text(Path(WATCH).read_text() + "Golden Town,agenda,https://www.golden.example/a,,golden.example,\n")
        (self.tmp / "r.csv").write_text(text + "Golden Town,morro-bay,yes,Golden town police; local ALPR policy applies,\n")
        code, out = self.sweep("plan", watch=str(self.tmp / "w.csv"), roster=str(self.tmp / "r.csv"))
        self.assertEqual((code, out.splitlines()[-1]), (0, GATE))
        self.assertNotIn(AGENCY, out)
        self.assertEqual(self.sweep("status", store=ROOT / "tests"), (1, "store inside repository\n"))

    def test_matrix(self):
        self.finish()
        out = self.tmp / "matrix"
        self.assertEqual(self.sweep("matrix", "--out", str(out)), (0, "matrix rows=7 agencies=1\n"))
        matrix, md = (out / "COMPARE-MATRIX.csv").read_text(), (out / "COMPARE.md").read_text()
        rows = list(csv.reader(matrix.splitlines()))
        self.assertEqual((rows[0], len(rows)), (["topic", "claim_class", AGENCY], 8))
        cells = {(r[0], r[1]): r[2] for r in rows[1:]}
        self.assertEqual((cells[("outside-agency-access", "duty-concern")], cells[("retention-changes", "record-gap")],
                          cells[("audits", "record-gap")]), ("rejected", "medium", "strong"))
        self.assertEqual(md.splitlines()[0], "Research draft - not legal advice. Ratings are research ratings, not legal conclusions.")
        self.assertEqual(md.splitlines()[-1], "- not finished: second-city-police;campus-police;parks")
        self.assertEqual((out / "COMPARE.md").stat().st_mode & 0o777, 0o600)
        with open(FIXTURES / "findings-sample.csv", newline="") as handle:
            for claim in [r[4] for r in csv.reader(handle)][1:]:
                self.assertNotIn(claim, matrix + md)
        self.assertEqual(self.sweep("matrix", "--out", str(ROOT / "matrix-out"))[0], 1)


if __name__ == "__main__":
    unittest.main()
