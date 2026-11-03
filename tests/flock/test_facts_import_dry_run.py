"""flock_facts import --dry-run (RF0740-9): every import check, per-agency would-import/duplicate counts, nothing written."""
import csv
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.flock.test_facts import FIXTURE, ROOT, TOOL, run

LEAKS = ("Testville PD", "Sampleton Sheriff", "REQ-00", "aaaa", "bbbb", "cameras", "Acme")


def tree(root):
    """Every path under root (and root itself) with its lstat mode and bytes or link target; links are never followed."""
    if not os.path.lexists(root):
        return None
    got = {}
    for top, dirs, files in os.walk(root):
        for name in [""] * (top == str(root)) + dirs + files:
            path = Path(top, name)
            mode = path.lstat().st_mode
            data = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
            got[str(path.relative_to(root))] = (mode, data)
    return got


class ImportDryRunTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = self.dir / "store"
        with open(FIXTURE, newline="") as handle:
            self.rows = list(csv.reader(handle))

    def write(self, rows, name="in.csv"):
        path = self.dir / name
        with open(path, "w", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(rows)
        return str(path)

    def imp(self, src, *extra, store=None):
        return run("import", src, "--store", str(store or self.store), "--added-by", "tester", "--date", "2026-10-30", *extra)

    def dry(self, src, store=None):
        """A dry run that must leave the whole store tree (paths, modes, bytes) exactly as it was."""
        before = tree(store or self.store)
        got = self.imp(src, "--dry-run", store=store)
        self.assertEqual(tree(store or self.store), before)
        self.assertFalse(any(s in got[1] for s in LEAKS), got[1])
        return got

    def test_fresh_store_counts_new_per_agency_in_slug_order_and_creates_nothing(self):
        code, out = self.dry(str(FIXTURE))
        self.assertEqual((code, out.splitlines()), (0, [
            "would-import | sampleton-sheriff | new=3 duplicates=0", "would-import | testville-pd | new=3 duplicates=0",
            "imported=0 would_import=6 duplicates=0 errors=0 agencies=2 dry_run=1"]))
        self.assertFalse(self.store.exists())
        self.store.mkdir()  # an empty store folder: still nothing made inside it
        self.assertEqual(self.dry(str(FIXTURE))[0], 0)
        self.assertEqual(list(self.store.iterdir()), [])

    def test_half_present_store_counts_duplicates_and_leaves_facts_bytes_and_mtime(self):
        self.assertEqual(self.imp(self.write(self.rows[:3] + self.rows[4:5], "half.csv"))[0], 0)
        facts = self.store / "agencies" / "testville-pd" / "FACTS.csv"
        stat, data = facts.stat(), facts.read_bytes()
        code, out = self.dry(str(FIXTURE))
        self.assertEqual((code, out.splitlines()), (0, [
            "would-import | sampleton-sheriff | new=2 duplicates=1", "would-import | testville-pd | new=1 duplicates=2",
            "imported=0 would_import=3 duplicates=3 errors=0 agencies=2 dry_run=1"]))
        self.assertEqual((facts.read_bytes(), facts.stat().st_mtime_ns, facts.stat().st_ino), (data, stat.st_mtime_ns, stat.st_ino))
        self.assertEqual(self.imp(str(FIXTURE)), (0, "imported=3 duplicates=3 errors=0 agencies=2\n"))
        self.assertEqual(self.dry(str(FIXTURE))[1].splitlines()[-1],
                         "imported=0 would_import=0 duplicates=6 errors=0 agencies=2 dry_run=1")

    def test_duplicates_inside_the_source_agree_with_the_real_import(self):
        self.assertEqual(self.imp(self.write(self.rows[:2], "one.csv"))[0], 0)
        src = self.write(self.rows + [self.rows[1], self.rows[5], self.rows[5]])
        code, out = self.dry(src)
        self.assertEqual((code, out.splitlines()), (0, [
            "would-import | sampleton-sheriff | new=3 duplicates=2", "would-import | testville-pd | new=2 duplicates=2",
            "imported=0 would_import=5 duplicates=4 errors=0 agencies=2 dry_run=1"]))
        self.assertEqual(self.imp(src), (0, "imported=5 duplicates=4 errors=0 agencies=2\n"))
        self.assertEqual(run("--check", str(self.store))[0], 0)

    def test_invalid_source_and_store_rows_give_the_import_error_lines(self):
        rows = [list(r) for r in self.rows]
        rows[2][5], rows[4][4] = "", rows[4][4][:10]
        src = self.write(rows)
        real = self.imp(src)
        self.assertEqual(real[0], 1)
        self.assertEqual(real[1].splitlines()[-1], "imported=0 duplicates=0 errors=2")
        self.assertFalse(self.store.exists())
        self.assertEqual(self.dry(src), real)
        self.assertFalse(self.store.exists())
        self.assertEqual(self.imp(str(FIXTURE))[0], 0)
        facts = self.store / "agencies" / "testville-pd" / "FACTS.csv"
        facts.write_text(facts.read_text().replace(",12,", ",,", 1))
        real = self.imp(str(FIXTURE))
        self.assertEqual((real[0], real[1].splitlines()[0][:13]), (1, "store file 1:"))
        self.assertEqual(self.dry(str(FIXTURE)), real)

    def test_linked_or_non_folder_agency_paths_are_refused_and_not_followed(self):
        target = self.dir / "elsewhere"
        target.mkdir()
        (target / "FACTS.csv").write_text("not,a,store\n")
        (self.store / "agencies").mkdir(parents=True)
        (self.store / "agencies" / "testville-pd").symlink_to(target, target_is_directory=True)
        outside = tree(target)
        self.assertEqual(self.dry(str(FIXTURE)),
                         (1, "agencies/testville-pd: is a symlink; refusing\nimported=0 duplicates=0 errors=1\n"))
        self.assertEqual(tree(target), outside)
        (self.store / "agencies" / "testville-pd").unlink()
        (self.store / "agencies" / "testville-pd").write_text("")
        self.assertEqual(self.dry(str(FIXTURE)),
                         (1, "agencies/testville-pd: exists and is not a folder\nimported=0 duplicates=0 errors=1\n"))
        other = self.dir / "other"
        (other / "agencies").mkdir(parents=True)
        (self.dir / "linked").mkdir()
        (self.dir / "linked" / "agencies").symlink_to(other / "agencies", target_is_directory=True)
        code, out = self.dry(str(FIXTURE), store=self.dir / "linked")
        self.assertEqual((code, out.splitlines()[0]), (1, "agencies: is a symlink; refusing"))
        self.assertEqual(list((other / "agencies").iterdir()), [])

    def test_dry_run_outside_import_or_with_check_is_a_usage_error(self):
        self.assertEqual(self.imp(str(FIXTURE))[0], 0)
        before = tree(self.store)
        for argv in (["--check", str(self.store)], ["import", str(FIXTURE), "--check", str(self.store), "--store",
                     str(self.store), "--added-by", "tester"], ["status", "--store", str(self.store)],
                     ["coverage", "--store", str(self.store), "--agency", "testville-pd", "--inventory", str(FIXTURE)]):
            done = subprocess.run([sys.executable, "-B", str(TOOL), *argv, "--dry-run"], capture_output=True, text=True)
            self.assertEqual((done.returncode, done.stdout), (2, ""), argv)
            self.assertIn("--dry-run belongs to import only", done.stderr)
        self.assertEqual(tree(self.store), before)

    def test_store_inside_repository_is_refused(self):
        inside = ROOT / "tests" / "not-a-store"
        self.assertEqual(self.dry(str(FIXTURE), store=inside), (1, "store inside repository\n"))
        self.assertFalse(inside.exists())


if __name__ == "__main__":
    unittest.main()
