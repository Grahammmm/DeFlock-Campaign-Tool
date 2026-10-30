import csv
import json
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


def run(tool, *args):
    done = subprocess.run([sys.executable, "-B", str(TOOLS / tool), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


class FlockCompareTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        code, _ = run("flock_facts.py", "import", str(FIXTURES / "compare-facts.csv"), "--store", str(self.store),
                      "--added-by", "tester", "--date", "2026-10-30")
        self.assertEqual(code, 0)
        shutil.copy(FIXTURES / "law-sample.csv", self.store / "LAW.csv")
        shutil.copy(FIXTURES / "policy-sample.csv", self.store / "agencies" / AGENCY / "POLICY.csv")
        self.run_dir = self.store / "agencies" / AGENCY / "runs" / "2026-10-30"
        self.assertEqual(self.scaffold()[0], 0)
        shutil.copy(FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")

    def scaffold(self):
        return run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", AGENCY,
                   "--date", "2026-10-30", "--provider", "anthropic", "--model", "claude-opus-5-5",
                   "--applicability", "Sample city police; local ALPR policy applies")

    def mutate(self, row, **fields):
        with open(FIXTURES / "findings-sample.csv", newline="") as handle:
            table = list(csv.reader(handle))
        for column, value in fields.items():
            table[row - 1][table[0].index(column)] = value
        with open(self.run_dir / "FINDINGS.csv", "w", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(table)

    def check(self, *extra):
        return run("flock_compare.py", "--check", str(self.run_dir), *extra)

    def test_scaffold_and_clean_check(self):
        meta = json.loads((self.run_dir / "RUN.json").read_text())
        self.assertEqual((meta["author_provider"], meta["author_model"], meta["applicability"]),
                         ("anthropic", "claude-opus-5-5", "Sample city police; local ALPR policy applies"))
        self.assertEqual(self.check(), (0, "findings=7 errors=0\n"))

    def test_fail_closed_mutations_and_rescaffold(self):
        cases = [
            (2, {"fact_ids": "F-000000000000"}, "row 2: fact_ids"),
            (3, {"rule_ids": ""}, "row 3: rule_ids and policy_refs are both empty"),
            (4, {"claim": "The sample department admitted outside access."}, "row 4: claim quote-gated"),
            (6, {"claim": "There are no audit records."}, "row 6: claim always-banned"),
            (7, {"claim": "Flock cameras keep reads for a year."}, "row 7: claim vendor-gated"),
        ]
        for row, fields, expected in cases:
            with self.subTest(row=row):
                self.mutate(row, **fields)
                code, out = self.check()
                self.assertEqual(code, 1)
                self.assertIn(expected, out)
                self.assertTrue(out.endswith("errors=1\n"), out)
                self.assertNotIn(fields.get("claim", "\0"), out)
        self.assertEqual(self.scaffold()[0], 1)

    def test_golden(self):
        golden = str(FIXTURES / "morrobay-golden.csv")
        self.assertEqual(self.check("--golden", golden), (0, "findings=7 errors=0\n"))
        self.mutate(5, rating="medium")
        code, out = self.check("--golden", golden)
        self.assertEqual(code, 1)
        self.assertIn("row 5: rating above golden", out)


if __name__ == "__main__":
    unittest.main()
