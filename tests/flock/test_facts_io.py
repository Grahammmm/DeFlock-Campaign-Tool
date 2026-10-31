"""RF1035-4: flock_facts names io errors and refuses symlinked or non-folder agency paths before writing."""
import contextlib
import csv
import importlib.util
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("flock_facts_io_under_test", ROOT / "tools" / "flock_facts.py")
facts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(facts)


def row(agency, value):
    return [agency, "REQ-1", "camera_count", value, "ab" * 32, "1", "quoted text"]


def files(top):
    return sorted(str(p.relative_to(top)) for p in Path(top).rglob("*") if p.is_file())


class FactsIoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = self.dir / "store"
        self.other = self.dir / "elsewhere"
        self.other.mkdir()

    def src(self, rows):
        path = self.dir / "in.csv"
        with open(path, "w", newline="") as handle:
            csv.writer(handle).writerows([facts.V1] + rows)
        return str(path)

    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = facts.main(list(argv))
        return code, out.getvalue()

    def do_import(self, src, store=None):
        return self.main("import", src, "--store", str(store or self.store), "--added-by", "t", "--date", "2026-10-31")

    def assert_refused(self, code, out, line):
        self.assertEqual(code, 1)
        self.assertIn(line, out.splitlines())
        self.assertIn("imported=0 duplicates=0 errors=", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn(str(self.dir), out)

    def test_happy_path_writes_private_facts_file(self):
        code, out = self.do_import(self.src([row("Town A", "3")]))
        self.assertEqual((code, out.strip()), (0, "imported=1 duplicates=0 errors=0 agencies=1"))
        target = self.store / "agencies" / "town-a" / "FACTS.csv"
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(files(self.store), ["agencies/town-a/FACTS.csv"])

    def test_symlinked_agency_folder_refused(self):
        (self.store / "agencies").mkdir(parents=True)
        os.symlink(self.other, self.store / "agencies" / "town-a")
        code, out = self.do_import(self.src([row("Town A", "3")]))
        self.assert_refused(code, out, "agencies/town-a: is a symlink; refusing")
        self.assertEqual(files(self.other), [])
        self.assertEqual(files(self.store), [])

    def test_symlinked_agencies_refused_before_any_write(self):
        self.store.mkdir()
        os.symlink(self.other, self.store / "agencies")
        code, out = self.do_import(self.src([row("Town A", "3")]))
        self.assert_refused(code, out, "agencies: is a symlink; refusing")
        self.assertEqual(files(self.other), [])
        self.assertFalse((self.other / "town-a").exists())

    def test_agency_path_is_regular_file(self):
        (self.store / "agencies").mkdir(parents=True)
        (self.store / "agencies" / "town-a").write_bytes(b"keep me\n")
        code, out = self.do_import(self.src([row("Town A", "3")]))
        self.assert_refused(code, out, "agencies/town-a: exists and is not a folder")
        self.assertEqual((self.store / "agencies" / "town-a").read_bytes(), b"keep me\n")
        self.assertEqual(files(self.store), ["agencies/town-a"])

    def test_agencies_is_regular_file(self):
        self.store.mkdir()
        (self.store / "agencies").write_bytes(b"x")
        code, out = self.do_import(self.src([row("Town A", "3")]))
        self.assert_refused(code, out, "agencies: exists and is not a folder")
        self.assertEqual((self.store / "agencies").read_bytes(), b"x")

    def test_one_refused_agency_blocks_all_agencies(self):
        (self.store / "agencies").mkdir(parents=True)
        os.symlink(self.other, self.store / "agencies" / "town-b")
        code, out = self.do_import(self.src([row("Town A", "3"), row("Town B", "4")]))
        self.assert_refused(code, out, "agencies/town-b: is a symlink; refusing")
        self.assertEqual(files(self.store), [])
        self.assertEqual(files(self.other), [])

    def test_missing_or_directory_src_named(self):
        self.assertEqual(self.do_import(str(self.dir / "missing.csv")), (1, "io: FileNotFoundError\n"))
        code, out = self.do_import(str(self.other))
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("io: ") and out.count("\n") == 1, out)
        self.assertFalse(self.store.exists())

    def test_non_utf8_src_and_check_missing_named(self):
        bad = self.dir / "bad.csv"
        bad.write_bytes(b"\xff\xfe\x00")
        self.assertEqual(self.do_import(str(bad)), (1, "io: UnicodeDecodeError\n"))
        self.assertEqual(self.main("--check", str(bad)), (1, "io: UnicodeDecodeError\n"))
        self.assertEqual(self.main("--check", str(self.dir / "missing.csv")), (1, "io: FileNotFoundError\n"))
        self.assertFalse(self.store.exists())

    def test_store_inside_repository_unchanged(self):
        inside = ROOT / ".rf1035-4-store-never-created"
        self.assertEqual(self.do_import(self.src([row("Town A", "3")]), inside), (1, "store inside repository\n"))
        self.assertFalse(inside.exists())


if __name__ == "__main__":
    unittest.main()
