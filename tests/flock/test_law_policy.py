import csv
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_law_policy.py"
FIXTURES = ROOT / "tests" / "fixtures" / "flock"
AGENCY = "sample-city-police-department"


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


class FlockLawPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def edited(self, name, edits):
        with open(FIXTURES / name, newline="") as handle:
            table = list(csv.reader(handle))
        for row, column, value in edits:
            table[row - 1][table[0].index(column)] = value
        path = Path(self.tmp.name) / name
        with open(path, "w", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(table)
        return str(path)

    def test_fixtures_check_clean_and_versions_answer(self):
        self.assertEqual(run("--check", str(FIXTURES / "law-sample.csv"))[:2], (0, "law=3 policy=0 errors=0\n"))
        self.assertEqual(run("--check", str(FIXTURES / "policy-sample.csv"))[:2], (0, "law=0 policy=2 errors=0\n"))
        policy = str(FIXTURES / "policy-sample.csv")
        code, out, _ = run("versions", policy, "--agency", AGENCY)
        lines = out.splitlines()
        self.assertEqual((code, len(lines)), (0, 2))
        row_a, row_b = lines
        self.assertTrue(row_a.endswith("adoption unverified | governs not established"))
        self.assertTrue(row_b.endswith("adoption verified | governs 2026-10-13..open"))
        code, out, _ = run("versions", policy, "--agency", AGENCY, "--on", "2026-10-20")
        self.assertEqual((code, out), (0, row_b + "\n"))
        code, out, _ = run("versions", policy, "--agency", AGENCY, "--on", "2025-07-01")
        self.assertEqual((code, out), (3, "no adopted version established for 2025-07-01\n"))

    def test_fail_closed_on_package_host_label_and_unadopted_governing(self):
        law = self.edited("law-sample.csv", [
            (2, "rule_id", "ca-civ-0000-none"),
            (3, "official_url", "https://example.org/sample/2"),
            (4, "review", "verified"),
        ])
        code, out, _ = run("--check", law)
        self.assertEqual(code, 1)
        for text in ("row 2: rule_id", "row 3: official_url", "row 4: review"):
            self.assertIn(text, out)
        policy = self.edited("policy-sample.csv", [(2, "governs_from", "2025-06-03")])
        code, out, _ = run("--check", policy)
        self.assertEqual(code, 1)
        self.assertIn("row 2: governs_from", out)
        code, out, err = run("versions", policy, "--agency", AGENCY)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("row 2: governs_from", err)


if __name__ == "__main__":
    unittest.main()
