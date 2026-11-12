"""kit_export.write_rows failure cleanup removes only the file this call created. Offline, synthetic."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_export  # noqa: E402

REAL_FSYNC = os.fsync


class Boom(OSError):
    pass


class WriteRowsRollbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.target = self.dir / "t.csv"

    def fail_with(self, substitute=None, error=None):
        """Run write_rows with an fsync fault that first substitutes the pathname; return the raised error."""
        error = error or Boom("synthetic-fsync-fault")

        def fsync(fd):
            if substitute:
                substitute(self.target)
            raise error

        with mock.patch.object(os, "fsync", side_effect=fsync):
            with self.assertRaises(OSError) as ctx:
                kit_export.write_rows(self.target, "t.csv", ("a",), [["1"]])
        self.assertIs(ctx.exception, error)
        return ctx.exception

    def moved(self, replace):
        def substitute(target):
            os.rename(target, self.dir / "moved.csv")
            replace(target)
        return substitute

    def test_unchanged_partial_is_removed(self):
        self.fail_with()
        self.assertFalse(os.path.lexists(self.target))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_replacement_file_is_preserved(self):
        self.fail_with(self.moved(lambda t: t.write_bytes(b"keep")))
        self.assertEqual(self.target.read_bytes(), b"keep")
        self.assertTrue((self.dir / "moved.csv").exists())

    def test_symlink_replacement_is_preserved_and_not_followed(self):
        canary = self.dir / "canary.txt"
        canary.write_bytes(b"canary")
        self.fail_with(self.moved(lambda t: os.symlink(canary, t)))
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(canary.read_bytes(), b"canary")

    def test_directory_replacement_is_preserved(self):
        self.fail_with(self.moved(lambda t: t.mkdir()))
        self.assertTrue(self.target.is_dir())

    def test_disappeared_target_does_not_mask_original_error(self):
        self.fail_with(lambda t: os.rename(t, self.dir / "moved.csv"))
        self.assertFalse(os.path.lexists(self.target))
        self.assertTrue((self.dir / "moved.csv").exists())

    def test_cleanup_errors_are_secondary(self):
        with mock.patch.object(os, "unlink", side_effect=PermissionError("no")):
            self.fail_with()
        self.assertTrue(self.target.exists())  # own partial left, original failure survived
        with mock.patch.object(os, "lstat", side_effect=PermissionError("no")):
            self.target.unlink()
            self.fail_with()

    def test_identity_capture_failure_is_conservative_and_closes_fd(self):
        opened = []
        real_open = os.open

        def tracking_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        error = Boom("fstat-fault")
        real_fstat = os.fstat

        def regular_fault(fd):  # only the created CSV's identity capture fails; directory fstats stay real
            info = real_fstat(fd)
            if stat.S_ISREG(info.st_mode):
                raise error
            return info

        with mock.patch.object(os, "open", side_effect=tracking_open), \
                mock.patch.object(os, "fstat", side_effect=regular_fault):
            with self.assertRaises(Boom) as ctx:
                kit_export.write_rows(self.target, "t.csv", ("a",), [["1"]])
        self.assertIs(ctx.exception, error)
        self.assertTrue(self.target.exists())  # ownership unknown: left in place
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)  # every acquired descriptor was closed

    def test_base_exception_unwinds_and_later_write_succeeds(self):
        class Stop(BaseException):
            pass

        with mock.patch.object(os, "fsync", side_effect=Stop()):
            with self.assertRaises(Stop):
                kit_export.write_rows(self.target, "t.csv", ("a",), [["1"]])
        self.assertFalse(os.path.lexists(self.target))
        kit_export.write_rows(self.target, "t.csv", ("a",), [["=1"]])
        self.assertEqual(self.target.read_bytes(), b"a\r\n=1\r\n")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_existing_target_is_still_refused(self):
        self.target.write_bytes(b"x")
        with self.assertRaises(ValueError):
            kit_export.write_rows(self.target, "t.csv", ("a",), [])
        self.assertEqual(self.target.read_bytes(), b"x")


if __name__ == "__main__":
    unittest.main()
