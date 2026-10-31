import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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
        self.mutate(1)
        (self.store / "agencies" / AGENCY / "POLICY.csv").unlink()
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn("store: POLICY.csv", out)

    def test_golden(self):
        golden = str(FIXTURES / "morrobay-golden.csv")
        self.assertEqual(self.check("--golden", golden), (0, "findings=7 errors=0\n"))
        self.mutate(5, rating="medium")
        code, out = self.check("--golden", golden)
        self.assertEqual(code, 1)
        self.assertIn("row 5: rating above golden", out)

    def test_scaffold_writes_through_fresh_temp(self):  # BL-DF-1: no planted/stale .tmp or final-name symlink is followed or reused
        spec = importlib.util.spec_from_file_location("_bl_df_1_compare", TOOLS / "flock_compare.py")
        spec.loader.exec_module(compare := importlib.util.module_from_spec(spec))
        sentinel, run_dir = Path(self.store).parent / "sentinel", self.store / "agencies" / AGENCY / "runs" / "2026-10-31"
        sentinel.write_bytes(b"keep\n")
        run_dir.mkdir(mode=0o700)
        (run_dir / "RUN.json").symlink_to(sentinel)
        (run_dir / "FINDINGS.csv.tmp").symlink_to(sentinel)
        (run_dir / "FINDINGS.md.tmp").write_bytes(b"stale\n")
        (run_dir / "FINDINGS.md.tmp").chmod(0o644)
        before = os.lstat(run_dir / "FINDINGS.md.tmp")
        argv = ["scaffold", "--store", str(self.store), "--agency", AGENCY, "--date", "2026-10-31", "--provider", "anthropic",
                "--model", "claude-opus-5-5", "--applicability", "Sample city police; local ALPR policy applies"]
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(compare.main(argv), 0)
        self.assertEqual(printed.getvalue(), "scaffolded run for %s on 2026-10-31\n" % AGENCY)
        self.assertEqual((sentinel.read_bytes(), os.readlink(run_dir / "FINDINGS.csv.tmp")), (b"keep\n", str(sentinel)))
        for name in ("RUN.json", "FINDINGS.csv", "FINDINGS.md"):
            st = os.lstat(run_dir / name)
            self.assertEqual((name, stat.S_ISREG(st.st_mode), stat.S_IMODE(st.st_mode)), (name, True, 0o600))
        self.assertEqual(json.loads((run_dir / "RUN.json").read_text())["date"], "2026-10-31")
        self.assertEqual((run_dir / "FINDINGS.csv").read_text(), ",".join(compare.HEADER) + "\n")
        after = os.lstat(run_dir / "FINDINGS.md.tmp")
        self.assertEqual(((run_dir / "FINDINGS.md.tmp").read_bytes(), stat.S_IMODE(after.st_mode), after.st_ino), (b"stale\n", 0o644, before.st_ino))
        self.assertNotEqual(os.lstat(run_dir / "FINDINGS.md").st_ino, before.st_ino)
        listing, kept = sorted(os.listdir(run_dir)), {n: ((run_dir / n).read_bytes(), os.lstat(run_dir / n).st_ino) for n in ("RUN.json", "FINDINGS.csv")}
        with patch.object(compare.os, "replace", side_effect=OSError("replace failed")), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual((compare.main(argv), printed.getvalue()), (1, "io: OSError\n"))
        self.assertEqual({n: ((run_dir / n).read_bytes(), os.lstat(run_dir / n).st_ino) for n in kept}, kept)
        self.assertEqual(sorted(os.listdir(run_dir)), listing)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(compare.main(argv), 0)  # header-only run: re-scaffold still allowed


if __name__ == "__main__":
    unittest.main()
