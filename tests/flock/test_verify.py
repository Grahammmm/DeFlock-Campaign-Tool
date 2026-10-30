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
GOLDEN = str(FIXTURES / "morrobay-golden.csv")


def run(tool, *args):
    done = subprocess.run([sys.executable, "-B", str(TOOLS / tool), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


class FlockVerifyTest(unittest.TestCase):
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
        code, _ = run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", AGENCY,
                      "--date", "2026-10-30", "--provider", "anthropic", "--model", "claude-opus-5-5",
                      "--applicability", "Sample city police; local ALPR policy applies")
        self.assertEqual(code, 0)
        shutil.copy(FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")
        self.rd = str(self.run_dir)

    def start(self, provider):
        return run("flock_verify.py", "start", self.rd, "--provider", provider, "--model", "gpt-5-codex")

    def mutate(self, row, drop=False, **fields):
        with open(FIXTURES / "verify-sample.csv", newline="") as handle:
            table = list(csv.reader(handle))
        for column, value in fields.items():
            table[row - 1][table[0].index(column)] = value
        with open(self.run_dir / "VERIFY.csv", "w", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(table[:row - 1] + table[row:] if drop else table)

    def test_happy_path(self):
        self.assertEqual(self.start("openai"), (0, "started verify run\n"))
        shutil.copy(FIXTURES / "verify-sample.csv", self.run_dir / "VERIFY.csv")
        self.assertEqual(run("flock_verify.py", "--check", self.rd), (0, "verified=7 errors=0\n"))
        self.assertEqual(run("flock_verify.py", "finish", self.rd, "--golden", GOLDEN),
                         (0, "public_ok=4 questions=2 rejected=1\n"))
        with open(self.run_dir / "PUBLIC-OK.csv", newline="") as handle:
            public = [r[0] for r in csv.reader(handle)][1:]
        self.assertEqual(public, ["N1", "O1", "A1", "T1"])
        lines = (self.run_dir / "QUESTIONS.md").read_text().splitlines()
        self.assertEqual(lines[0], "Research draft - not legal advice. Records to seek; not allegations.")
        self.assertEqual([l.split(" | ")[:3] for l in lines[1:]], [["- Q N2", "na-search-purposes", "prior REQ-101"],
                                                                     ["- Q R1", "retention-changes", "prior REQ-101"]])
        self.assertNotIn("O2", (self.run_dir / "PUBLIC-OK.csv").read_text() + "\n".join(lines))
        self.assertEqual(run("flock_verify.py", "finish", self.rd)[0], 1)

    def test_fail_closed_family(self):
        self.assertEqual(self.start("anthropic"), (3, "refused: verifier family equals author family\n"))
        self.assertFalse((self.run_dir / "VERIFY.json").exists())
        self.assertEqual(self.start("quietfinch")[0], 3)
        self.assertEqual(self.start("OpenAI")[0], 0)
        shutil.copy(FIXTURES / "verify-sample.csv", self.run_dir / "VERIFY.csv")
        (self.run_dir / "VERIFY.json").write_text(json.dumps({"schema": 1, "verifier_provider": "Anthropic"}))
        for args in (("--check", self.rd), ("finish", self.rd)):
            self.assertEqual(run("flock_verify.py", *args), (3, "refused: verifier family equals author family\n"))
        self.assertFalse((self.run_dir / "PUBLIC-OK.csv").exists())

    def test_fail_closed_verdicts(self):
        self.assertEqual(self.start("openai")[0], 0)
        cases = [
            (2, {"reread_page": "3"}, "row 2: reread_page"),
            (4, {"new_rating": "medium", "records_sought": "Existing outside-agency access log"}, "row 4: new_rating"),
            (7, {"new_rating": "strong"}, "row 7: new_rating"),
            (3, {"records_sought": "Existing records showing why searches lack a purpose"}, "row 3: records_sought"),
            (8, {"drop": True}, "finding_id T1: has no VERIFY.csv row"),
        ]
        for row, fields, expected in cases:
            with self.subTest(row=row):
                self.mutate(row, **fields)
                code, out = run("flock_verify.py", "--check", self.rd)
                self.assertEqual(code, 1)
                self.assertIn(expected, out)
                self.assertTrue(out.endswith("errors=1\n"), out)
                self.assertEqual(run("flock_verify.py", "finish", self.rd)[0], 1)
        self.assertFalse((self.run_dir / "QUESTIONS.md").exists())


if __name__ == "__main__":
    unittest.main()
