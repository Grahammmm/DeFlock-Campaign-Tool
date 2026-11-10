"""The catalogue reader enforces its byte cap on the bytes actually read, even if the file grows after the size check."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import site_data_catalogue as cat

TOO_LARGE = "is too large to check"


class _Spy:
    """Wrap the real ``open`` on one path, recording every read request."""

    def __init__(self, target):
        self.target, self.requests, self.closed = str(target), [], []
        self._real = open

    def __call__(self, file, *args, **kwargs):
        handle = self._real(file, *args, **kwargs)
        if str(file) != self.target:
            return handle
        spy = self

        class Wrapped:
            def read(self, size=-1):
                spy.requests.append(size)
                return handle.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
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

    def _grow_after_final_stat(self, final_size):
        """Real lstat of the file, then grow the real file before returning it."""
        real, target, calls = os.lstat, str(self.file), []

        def lstat(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if str(path) == target:
                calls.append(1)
                if len(calls) == 2:  # the size check; the first is the path walk
                    self.file.write_bytes(b"x" * final_size)
            return result

        return mock.patch.object(cat.os, "lstat", lstat)

    def test_growth_after_stat_obeys_inclusive_cap(self):
        for final, expected_ok in ((15, True), (16, True), (17, False), (5000, False)):
            with self.subTest(final=final):
                self.file.write_bytes(b"ab")
                spy = _Spy(self.file)
                with self._grow_after_final_stat(final), mock.patch("builtins.open", spy):
                    result = cat._read_regular(self.public, "data/f.json", 16)
                self.assertEqual(result, b"x" * final if expected_ok else TOO_LARGE)
                self.assertEqual(spy.requests, [17])
                self.assertEqual(spy.closed, [True])

    def test_static_oversize_never_opens_and_edge_sizes_work(self):
        self.file.write_bytes(b"y" * 17)
        spy = _Spy(self.file)
        with mock.patch("builtins.open", spy):
            self.assertEqual(cat._read_regular(self.public, "data/f.json", 16), TOO_LARGE)
        self.assertEqual(spy.requests, [])
        for body in (b"", b"z" * 16):
            self.file.write_bytes(body)
            self.assertEqual(cat._read_regular(self.public, "data/f.json", 16), body)
        self.assertIsNone(cat._read_regular(self.public, "data/missing.json", 16))
        self.assertEqual(cat._read_regular(self.public, "data", 16), "is not a regular file")

    def test_read_error_maps_to_fixed_reason_and_closes(self):
        self.file.write_bytes(b"ok")
        real_open = open

        def failing(file, *a, **k):
            if str(file) == str(self.file):
                raise OSError("secret-marker")
            return real_open(file, *a, **k)

        with mock.patch("builtins.open", failing):
            self.assertEqual(cat._read_regular(self.public, "data/f.json", 16), "cannot be read")

    def test_default_cap_request_is_limit_plus_one(self):
        self.file.write_bytes(b"{}")
        spy = _Spy(self.file)
        with mock.patch("builtins.open", spy):
            self.assertEqual(cat._read_regular(self.public, "data/f.json"), b"{}")
            cat._read_regular(self.public, "data/f.json", cat.SOURCE_ZIP_MAX)
        self.assertEqual(spy.requests, [cat.MAX_BYTES + 1, cat.SOURCE_ZIP_MAX + 1])


if __name__ == "__main__":
    unittest.main()
