"""kit_export.write_rows refuses to report success when the output name no longer is the file it created. Offline, synthetic."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_export, kit_track  # noqa: E402

REAL_FSYNC = os.fsync
REAL_FDOPEN = os.fdopen
MESSAGE = "csv output changed or cannot be verified; export not confirmed"


class SuccessIdentityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.target = self.dir / "t.csv"
        self.canary = self.dir / "canary.txt"
        self.canary.write_bytes(b"canary")

    def moved(self, replace):
        def substitute():
            os.rename(self.target, self.dir / "parked.csv")
            if replace:
                replace(self.target)
        return substitute

    def write_after_fsync(self, substitute):
        """Real fsync succeeds, then the name is substituted without any error being raised."""
        def fsync(fd):
            REAL_FSYNC(fd)
            substitute()
        with mock.patch.object(os, "fsync", side_effect=fsync):
            with self.assertRaises(ValueError) as ctx:
                kit_export.write_rows(self.target, "t.csv", ("a",), [["1"]])
        self.assertEqual(str(ctx.exception), MESSAGE)
        self.assertEqual((self.dir / "parked.csv").read_bytes(), b"a\r\n1\r\n")
        self.assertEqual(self.canary.read_bytes(), b"canary")
        return ctx.exception

    def test_successful_fsync_replacement_refuses_instead_of_returning(self):
        self.write_after_fsync(self.moved(lambda t: t.write_bytes(b"unrelated")))
        self.assertEqual(self.target.read_bytes(), b"unrelated")

    def test_symlink_directory_and_missing_targets_are_not_success(self):
        self.write_after_fsync(self.moved(lambda t: os.symlink(self.canary, t)))
        self.assertTrue(self.target.is_symlink())
        os.unlink(self.target)
        os.rename(self.dir / "parked.csv", self.dir / "old.csv")
        self.write_after_fsync(self.moved(lambda t: t.mkdir()))
        self.assertTrue(self.target.is_dir())
        self.target.rmdir()
        os.rename(self.dir / "parked.csv", self.dir / "old2.csv")
        self.write_after_fsync(self.moved(None))
        self.assertFalse(os.path.lexists(self.target))

    def test_successful_stream_close_substitution_is_checked(self):
        substitute = self.moved(lambda t: t.write_bytes(b"unrelated"))

        class Closing:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self.stream

            def __exit__(self, *exc):
                self.stream.close()
                substitute()
                return False

        with mock.patch.object(os, "fdopen", side_effect=lambda *a, **k: Closing(REAL_FDOPEN(*a, **k))):
            with self.assertRaises(ValueError) as ctx:
                kit_export.write_rows(self.target, "t.csv", ("a",), [["1"]])
        self.assertEqual(str(ctx.exception), MESSAGE)
        self.assertEqual(self.target.read_bytes(), b"unrelated")
        self.assertEqual((self.dir / "parked.csv").read_bytes(), b"a\r\n1\r\n")

    def test_final_metadata_oserror_is_fixed_and_cleanup_is_secondary(self):
        real_lstat = os.lstat
        for error in (PermissionError("secret-private-text"), FileNotFoundError("secret-private-text"), OSError("secret")):
            for cleanup_fails in (False, True):
                calls = []

                def lstat(path, *a, **k):
                    calls.append(path)
                    if len(calls) == 1 or cleanup_fails:
                        raise error
                    return real_lstat(path, *a, **k)

                with mock.patch.object(os, "lstat", side_effect=lstat):
                    with self.assertRaises(ValueError) as ctx:
                        kit_export.write_rows(self.target, "t.csv", ("a",), [["1"]])
                self.assertEqual(str(ctx.exception), MESSAGE)
                self.assertIsNone(ctx.exception.__cause__)
                self.assertTrue(ctx.exception.__suppress_context__)
                self.assertEqual(os.path.lexists(self.target), cleanup_fails)
                if cleanup_fails:
                    os.unlink(self.target)

    def test_original_write_failure_keeps_precedence(self):
        boom = OSError("synthetic-fsync-fault")
        with mock.patch.object(os, "fsync", side_effect=boom):
            with self.assertRaises(OSError) as ctx:
                kit_export.write_rows(self.target, "t.csv", ("a",), [["1"]])
        self.assertIs(ctx.exception, boom)
        self.assertFalse(os.path.lexists(self.target))

    def test_non_oserror_at_final_observation_is_not_translated(self):
        sentinel = RuntimeError("sentinel")
        real_lstat = os.lstat
        calls = []

        def lstat(path, *a, **k):
            calls.append(path)
            if len(calls) == 1:
                raise sentinel
            return real_lstat(path, *a, **k)

        with mock.patch.object(os, "lstat", side_effect=lstat):
            with self.assertRaises(RuntimeError) as ctx:
                kit_export.write_rows(self.target, "t.csv", ("a",), [["1"]])
        self.assertIs(ctx.exception, sentinel)

    def test_status_csv_never_confirms_foreign_csv(self):
        report = {"requests": [], "law_package_status": "x", "note": "n"}
        substitute = self.moved(lambda t: t.write_bytes(b"unrelated"))

        def fsync(fd):
            REAL_FSYNC(fd)
            substitute()
        with mock.patch.object(kit_track, "status", return_value=report), \
                mock.patch.object(os, "fsync", side_effect=fsync):
            with self.assertRaises(ValueError) as ctx:
                kit_export.status_csv(self.dir / "root", self.target)
        self.assertEqual(str(ctx.exception), MESSAGE)

    def test_healthy_bytes_and_permissions_stay_unchanged(self):
        self.assertIsNone(kit_export.write_rows(self.target, "t.csv", ("a", "b"), [["é", "'=x"]]))
        self.assertEqual(self.target.read_bytes(), "a,b\r\né,'=x\r\n".encode("utf-8"))
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o600)
        with self.assertRaises(ValueError):
            kit_export.write_rows(self.target, "t.csv", ("a",), [])
        self.assertEqual(self.target.read_bytes(), "a,b\r\né,'=x\r\n".encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
