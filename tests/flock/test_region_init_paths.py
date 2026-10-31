import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_region.py"
FIXTURES = ROOT / "tests" / "fixtures" / "flock"
SEED, SAMPLE = str(FIXTURES / "seed"), str(FIXTURES / "region-sample.json")
COUNTS = "agencies=3 folders_created=%d watch_rows=3 watch_dropped=0 roster_rows=3\n"
CITY, SHERIFF = "agencies/sample-city-police", "agencies/sample-county-sheriff"  # the two in-scope (yes|hold) agencies
REGION = "regions/sample-county"


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args, "--seed-dir", SEED], capture_output=True, text=True,
                          check=False)
    return done.returncode, done.stdout + done.stderr


def tree(top):  # every entry under top, symlinks not followed: rel -> (kind, bytes | link target | None)
    out = {}
    for base, dirs, files in os.walk(top):
        for name in dirs + files:
            path = Path(base) / name
            rel = path.relative_to(top).as_posix()
            out[rel] = (("link", os.readlink(path)) if path.is_symlink() else ("dir", None) if path.is_dir()
                        else ("file", path.read_bytes()))
    return out


class RegionInitPathsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = self.tmp / "store"
        self.outside = self.tmp / "outside"  # a real folder outside the store, the target of the links
        self.outside.mkdir()
        (self.outside / "keep.txt").write_bytes(b"keep\n")

    def init(self):
        return run("init", SAMPLE, "--store", str(self.store))

    def link(self, rel, target):
        path = self.store / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target, target_is_directory=True)

    def assert_refused(self, *lines):
        before, outside = tree(self.store), tree(self.outside)
        code, out = self.init()
        self.assertEqual((code, out), (1, "\n".join(lines + ("errors=%d" % len(lines),)) + "\n"))
        self.assertNotIn("Traceback", out)
        self.assertNotIn(str(self.tmp), out)  # the path relative to the store, never the absolute path
        self.assertEqual(tree(self.store), before)  # nothing created, for good agencies either
        self.assertEqual(tree(self.outside), outside)  # the link target is untouched
        for name in ("WATCHLIST.csv", "ROSTER.csv", "PLAN.md"):
            self.assertEqual(list(self.tmp.rglob(name)), [])  # no region file written anywhere

    def test_clean_store_and_rerun(self):
        self.assertEqual(self.init(), (0, COUNTS % 2))
        self.assertEqual(sorted(p for p, (k, _) in tree(self.store).items() if k == "dir"),
                         ["agencies", CITY, SHERIFF, "regions", REGION])
        before = tree(self.store)
        stamps = {p: os.stat(p).st_mtime_ns for p in self.store.rglob("*.*")}
        self.assertEqual(self.init(), (0, COUNTS % 0))  # re-run: nothing new, unchanged files left untouched
        self.assertEqual(tree(self.store), before)
        self.assertEqual({p: os.stat(p).st_mtime_ns for p in self.store.rglob("*.*")}, stamps)

    def test_agency_symlink_to_outside_folder(self):
        self.link(CITY, self.outside)
        self.assert_refused(CITY + ": is a symlink; refusing")

    def test_agency_is_a_regular_file(self):
        (self.store / "agencies").mkdir(parents=True)
        (self.store / SHERIFF).write_bytes(b"x")
        self.assert_refused(SHERIFF + ": exists and is not a folder")

    def test_dangling_agency_symlink(self):
        self.link(CITY, self.tmp / "missing")
        self.assert_refused(CITY + ": is a symlink; refusing")
        self.assertFalse((self.tmp / "missing").exists())

    def test_region_folder_symlink(self):
        self.link(REGION, self.outside)
        self.assert_refused(REGION + ": is a symlink; refusing")

    def test_regions_and_agencies_roots(self):
        for rel, kind in (("agencies", "link"), ("regions", "link"), ("agencies", "file"), ("regions", "file")):
            with self.subTest(rel=rel, kind=kind):
                self.store.mkdir(exist_ok=True)
                if kind == "link":
                    self.link(rel, self.outside)
                else:
                    (self.store / rel).write_bytes(b"x")
                self.assert_refused(rel + (": is a symlink; refusing" if kind == "link" else ": exists and is not a folder"))
                (self.store / rel).unlink()

    def test_two_bad_agencies_both_named(self):
        self.link(CITY, self.outside)
        (self.store / SHERIFF).write_bytes(b"x")
        self.assert_refused(CITY + ": is a symlink; refusing", SHERIFF + ": exists and is not a folder")


if __name__ == "__main__":
    unittest.main()
