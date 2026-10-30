import csv
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_facts.py"
FIXTURE = ROOT / "tests" / "fixtures" / "flock" / "facts-sample.csv"


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


def snapshot(store):
    return {str(p.relative_to(store)): p.read_bytes() for p in sorted(store.rglob("*")) if p.is_file()}


class FlockFactsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = self.dir / "store"

    def rows(self):
        with open(FIXTURE, newline="") as handle:
            return list(csv.reader(handle))

    def write(self, rows):
        path = self.dir / "in.csv"
        with open(path, "w", newline="") as handle:
            csv.writer(handle, lineterminator="\r\n").writerows(rows)
        return str(path)

    def do_import(self, src, store=None):
        return run("import", src, "--store", str(store or self.store), "--added-by", "tester", "--date", "2026-10-30")

    def test_import_is_idempotent_and_store_checks_clean(self):
        count = len(self.rows()) - 1
        code, out = self.do_import(str(FIXTURE))
        self.assertEqual((code, out.strip()), (0, "imported=%d duplicates=0 errors=0 agencies=2" % count))
        self.assertEqual(sorted(p.name for p in (self.store / "agencies").iterdir()), ["sampleton-sheriff", "testville-pd"])
        self.assertEqual(run("--check", str(self.store))[0], 0)
        before = snapshot(self.store)
        code, out = self.do_import(str(FIXTURE))
        self.assertEqual((code, out.strip()), (0, "imported=0 duplicates=%d errors=0 agencies=2" % count))
        self.assertEqual(snapshot(self.store), before)

    def test_bad_rows_and_repo_store_fail_closed(self):
        rows = self.rows()
        rows[2][5] = ""
        rows[4][4] = rows[4][4][:10]
        code, out = self.do_import(self.write(rows))
        self.assertEqual(code, 1)
        self.assertIn("row 3: page ", out)
        self.assertIn("row 5: source_sha256 ", out)
        self.assertIn("imported=0 duplicates=0 errors=2", out)
        self.assertFalse(self.store.exists())
        code, out = self.do_import(str(FIXTURE), ROOT / "tests" / "not-a-store")
        self.assertEqual((code, out.strip()), (1, "store inside repository"))
        self.assertFalse((ROOT / "tests" / "not-a-store").exists())

    def test_check_reports_duplicate_rows(self):
        self.assertEqual(run("--check", str(FIXTURE))[0], 0)
        rows = self.rows()
        code, out = run("--check", self.write(rows + [rows[1]]))
        self.assertEqual(code, 1)
        self.assertIn("row 8: duplicate of row 2", out)


if __name__ == "__main__":
    unittest.main()
