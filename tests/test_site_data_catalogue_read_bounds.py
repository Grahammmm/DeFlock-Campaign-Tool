"""The catalogue reader enforces its byte cap on the bytes actually read, even if the file grows after the size check."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import site_data_catalogue as cat

TOO_LARGE = "is too large to check"


class _Spy:
    """Wrap the real ``os.fdopen``, recording every read request and handle closure."""

    def __init__(self):
        self.requests, self.closed = [], []
        self._real = os.fdopen

    def __call__(self, fd, *args, **kwargs):
        handle = self._real(fd, *args, **kwargs)
        spy = self

        class Wrapped:
            def read(self, size=-1):
                spy.requests.append(size)
                return handle.read(size)

            def close(self):
                handle.close()
                spy.closed.append(True)

        return Wrapped()


class ReadBoundTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.public = Path(tmp.name)
        (self.public / "data").mkdir()
        self.file = self.public / "data" / "f.json"

    def _grow_after_admission(self, final_size):
        """Real fstat of the admitted descriptor, then grow the real file before returning it."""
        real = os.fstat

        def fstat(fd, *args, **kwargs):
            result = real(fd, *args, **kwargs)
            self.file.write_bytes(b"x" * final_size)
            return result

        return mock.patch.object(cat.os, "fstat", fstat)

    def test_growth_after_stat_obeys_inclusive_cap(self):
        for final, expected_ok in ((15, True), (16, True), (17, False), (5000, False)):
            with self.subTest(final=final):
                self.file.write_bytes(b"ab")
                spy = _Spy()
                with self._grow_after_admission(final), mock.patch.object(cat.os, "fdopen", spy):
                    result = cat._read_regular(self.public, "data/f.json", 16)
                self.assertEqual(result, b"x" * final if expected_ok else TOO_LARGE)
                self.assertEqual(spy.requests, [17])
                self.assertEqual(spy.closed, [True])

    def test_static_oversize_never_opens_and_edge_sizes_work(self):
        self.file.write_bytes(b"y" * 17)
        spy = _Spy()
        with mock.patch.object(cat.os, "fdopen", spy), mock.patch.object(cat.os, "open") as opener:
            self.assertEqual(cat._read_regular(self.public, "data/f.json", 16), TOO_LARGE)
        opener.assert_not_called()
        self.assertEqual(spy.requests, [])
        for body in (b"", b"z" * 16):
            self.file.write_bytes(body)
            self.assertEqual(cat._read_regular(self.public, "data/f.json", 16), body)
        self.assertIsNone(cat._read_regular(self.public, "data/missing.json", 16))
        self.assertEqual(cat._read_regular(self.public, "data", 16), "is not a regular file")

    def test_read_error_maps_to_fixed_reason_and_closes(self):
        self.file.write_bytes(b"ok")
        opened = []
        real_open = os.open

        def tracking(path, *a, **k):
            fd = real_open(path, *a, **k)
            opened.append(fd)
            return fd

        def failing(fd, *a, **k):
            raise OSError("secret-marker")

        with mock.patch.object(cat.os, "open", tracking), mock.patch.object(cat.os, "fdopen", failing):
            self.assertEqual(cat._read_regular(self.public, "data/f.json", 16), "cannot be read")
        with self.assertRaises(OSError):
            os.fstat(opened[0])

    def test_default_cap_request_is_limit_plus_one(self):
        self.file.write_bytes(b"{}")
        spy = _Spy()
        with mock.patch.object(cat.os, "fdopen", spy):
            self.assertEqual(cat._read_regular(self.public, "data/f.json"), b"{}")
            cat._read_regular(self.public, "data/f.json", cat.SOURCE_ZIP_MAX)
        self.assertEqual(spy.requests, [cat.MAX_BYTES + 1, cat.SOURCE_ZIP_MAX + 1])


if __name__ == "__main__":
    unittest.main()
