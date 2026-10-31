import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "flock"
SEED, REAL_FDOPEN, EXISTS = str(FIXTURES / "seed"), os.fdopen, (1, "out: exists; refusing to overwrite\n")
SPEC = importlib.util.spec_from_file_location("_test_region_io", ROOT / "tools" / "flock_region.py")
REGION = importlib.util.module_from_spec(SPEC)  # the tool, loaded by file path and run in-process
SPEC.loader.exec_module(REGION)


def failing_fdopen(exc):  # a real handle (the fd is closed) that writes part of the draft, then raises exc
    class Handle:
        def __init__(self, fd, *args, **kwargs):
            self.real = REAL_FDOPEN(fd, *args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *info):
            self.real.close()

        def write(self, data):
            self.real.write(data[:10])
            self.real.flush()
            raise exc
    return mock.patch.object(REGION.os, "fdopen", Handle)


def main(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = REGION.main(list(argv))
    return code, buf.getvalue()


class RegionIoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.draft = self.tmp / "drafts" / "draft.json"

    def new(self, out=None):
        return main("new", "--county-fips", "06999", "--region", "t", "--out", str(out or self.draft), "--seed-dir", SEED)

    def assert_new_ok(self):
        code, out = self.new()
        self.assertEqual(code, 0, out)
        self.assertEqual(os.stat(self.draft).st_mode & 0o777, 0o600)
        self.assertEqual(len(json.loads(self.draft.read_text(encoding="utf-8"))["agencies"]), 2)

    def test_happy_path(self):
        self.assert_new_ok()
        self.assertEqual(self.new(), EXISTS)

    def test_write_failure_removes_draft_and_names_the_error(self):
        for exc, name in ((OSError(28, "No space left on device"), "OSError"), (PermissionError(), "PermissionError")):
            with self.subTest(name), failing_fdopen(exc):
                self.assertEqual(self.new(), (1, "io: %s\n" % name))
            self.assertFalse(self.draft.exists() or self.draft.is_symlink())
        self.assert_new_ok()  # a rerun is not refused with "out: exists"

    def test_interrupt_during_write_is_reraised_and_draft_removed(self):
        with failing_fdopen(KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            self.new()
        self.assertFalse(self.draft.exists())
        self.assert_new_ok()

    def test_cleanup_failure_still_reports_the_write_error(self):
        with failing_fdopen(OSError()), mock.patch.object(REGION.os, "unlink", side_effect=PermissionError()):
            self.assertEqual(self.new(), (1, "io: OSError\n"))

    def test_existing_draft_is_never_touched(self):
        self.draft.parent.mkdir()
        self.draft.write_bytes(b"older draft\n")
        for patched in (False, True):  # a failing writer must not reach an older draft either
            with self.subTest(patched=patched), failing_fdopen(OSError()) if patched else contextlib.nullcontext():
                self.assertEqual(self.new(), EXISTS)
            self.assertEqual(self.draft.read_bytes(), b"older draft\n")

    def test_dangling_symlink_is_refused_and_untouched(self):
        self.draft.parent.mkdir()
        target = self.tmp / "missing" / "target.json"
        self.draft.symlink_to(target)
        self.assertEqual(self.new(), EXISTS)
        self.assertEqual(os.readlink(self.draft), str(target))
        self.assertFalse(target.exists() or target.parent.exists())

    def test_out_parent_is_a_regular_file(self):
        parent = self.tmp / "afile"
        parent.write_bytes(b"x")
        code, out = self.new(parent / "draft.json")  # main returns: no traceback
        self.assertEqual(code, 1)
        self.assertIn(out, ("io: FileExistsError\n", "io: NotADirectoryError\n"))  # the class only, never the path
        self.assertEqual(parent.read_bytes(), b"x")

    def test_out_inside_repository_is_refused(self):
        inside = ROOT / "tests" / "region-io-draft.json"
        self.assertEqual(self.new(inside), (1, "out inside repository\n"))
        self.assertFalse(inside.exists())

    def test_check_and_init_name_io_errors(self):
        with mock.patch.object(REGION, "load_config", side_effect=IsADirectoryError(21, "x", str(self.tmp))):
            self.assertEqual(main("--check", str(self.tmp)), (1, "io: IsADirectoryError\n"))
        store = self.tmp / "afile"
        store.write_bytes(b"x")  # init must create store/agencies under a regular file
        code, out = main("init", str(FIXTURES / "region-sample.json"), "--store", str(store), "--seed-dir", SEED)
        self.assertEqual((code, out[:4]), (1, "io: "))


if __name__ == "__main__":
    unittest.main()
