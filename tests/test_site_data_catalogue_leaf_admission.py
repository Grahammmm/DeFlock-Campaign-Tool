"""The catalogue reader reads only the regular leaf it inspected, never a late replacement."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import site_data_catalogue as cat

UNREADABLE = "cannot be read"


class LeafAdmissionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.public = root / "public"
        (self.public / "data").mkdir(parents=True)
        self.file = self.public / "data" / "f.json"
        self.file.write_bytes(b"inside")
        self.outside = root / "outside.json"
        self.outside.write_bytes(b"inside")  # matching bytes must not authorise a replacement

    def _after_lstat(self, action):
        """Run the real leaf lstat, then ``action`` before returning the captured result."""
        real, target = os.lstat, str(self.file)

        def lstat(path, *a, **k):
            result = real(path, *a, **k)
            if str(path) == target:
                action()
            return result

        return mock.patch.object(cat.os, "lstat", lstat)

    def _fdopen_spy(self):
        spy = mock.Mock(wraps=os.fdopen)
        return mock.patch.object(cat.os, "fdopen", spy), spy

    def test_late_leaf_symlink_never_reads_external_twin(self):
        def swap():
            self.file.unlink()
            self.file.symlink_to(self.outside)

        patch, spy = self._fdopen_spy()
        with self._after_lstat(swap), patch:
            self.assertEqual(cat._read_regular(self.public, "data/f.json"), UNREADABLE)
        spy.assert_not_called()

    def test_equal_length_regular_replacement_refuses_before_read(self):
        held = self.file.with_name("held.json")
        inspected = []

        def swap():
            # Hold the inspected inode via a hard link so the unlinked inode cannot be
            # recycled for the replacement; the replacement is then a different inode.
            inspected.append(os.stat(self.file))
            os.link(self.file, held)
            self.file.unlink()
            self.file.write_bytes(b"inside")

        patch, spy = self._fdopen_spy()
        with self._after_lstat(swap), patch:
            self.assertEqual(cat._read_regular(self.public, "data/f.json"), UNREADABLE)
        spy.assert_not_called()
        replacement = os.stat(self.file)
        self.assertNotEqual((inspected[0].st_dev, inspected[0].st_ino), (replacement.st_dev, replacement.st_ino))
        held.unlink()
        self.assertEqual(cat._read_regular(self.public, "data/f.json"), b"inside")

    def test_fifo_and_directory_replacements_are_nonblocking_refusals(self):
        real_open = os.open
        flags_seen = []

        def guarded(path, flags, *a, **k):
            flags_seen.append(flags)
            if not flags & os.O_NONBLOCK:
                raise AssertionError("blocking open")
            return real_open(path, flags, *a, **k)

        def to_fifo():
            self.file.unlink()
            os.mkfifo(self.file)

        def to_dir():
            self.file.unlink()
            self.file.mkdir()

        for name, swap in (("fifo", to_fifo), ("dir", to_dir)):
            with self.subTest(name):
                self.file.write_bytes(b"x")
                patch, spy = self._fdopen_spy()
                with self._after_lstat(swap), mock.patch.object(cat.os, "open", guarded), patch:
                    self.assertEqual(cat._read_regular(self.public, "data/f.json"), "is not a regular file")
                spy.assert_not_called()
                if name == "fifo":
                    self.file.unlink()
                else:
                    self.file.rmdir()
        self.assertTrue(flags_seen)

    def test_disappearance_before_open_is_a_problem_not_traceback(self):
        self.assertIsNone(cat._read_regular(self.public, "data/missing.json"))
        with self._after_lstat(self.file.unlink):
            self.assertEqual(cat._read_regular(self.public, "data/f.json"), UNREADABLE)

    def test_descriptor_closed_once_at_every_failure_seam(self):
        real_open, real_close, fds, closes = os.open, os.close, [], []

        def tracking(path, *a, **k):
            fds.append(real_open(path, *a, **k))
            return fds[-1]

        def close(fd):
            closes.append(fd)
            real_close(fd)

        def boom(*a, **k):
            raise OSError("secret-marker")

        seams = {"fstat": mock.patch.object(cat.os, "fstat", boom),
                 "fdopen": mock.patch.object(cat.os, "fdopen", boom)}
        for name, patch in seams.items():
            with self.subTest(name):
                fds.clear()
                closes.clear()
                with mock.patch.object(cat.os, "open", tracking), mock.patch.object(cat.os, "close", close), patch:
                    result = cat._read_regular(self.public, "data/f.json")
                self.assertEqual(result, UNREADABLE)
                self.assertEqual(closes, fds)
        # close failure: real close happens, no bytes accepted
        fds.clear()

        def failing_close(fd):
            real_close(fd)
            raise OSError("secret-marker")

        with mock.patch.object(cat.os, "open", tracking), mock.patch.object(cat.os, "close", failing_close):
            self.assertEqual(cat._read_regular(self.public, "data/f.json"), UNREADABLE)
        with self.assertRaises(OSError):
            os.fstat(fds[0])

    def test_rename_after_admission_keeps_read_on_admitted_inode(self):
        real = os.fstat

        def fstat(fd, *a, **k):
            result = real(fd, *a, **k)
            self.file.rename(self.file.with_name("parked.json"))
            self.file.write_bytes(b"replaced-after-admission")
            return result

        with mock.patch.object(cat.os, "fstat", fstat):
            self.assertEqual(cat._read_regular(self.public, "data/f.json"), b"inside")

    def test_descriptor_size_over_cap_refuses_before_reading(self):
        def grow():
            self.file.write_bytes(b"y" * 40)

        patch, spy = self._fdopen_spy()
        with self._after_lstat(grow), patch:
            self.assertEqual(cat._read_regular(self.public, "data/f.json", 16), "is too large to check")
        spy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
