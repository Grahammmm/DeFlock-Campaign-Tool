"""Officials reader admits the opened descriptor before consuming payload bytes."""
import contextlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import officials_cmd as oc
from campaign_tool.content import MAX_JSON_BYTES
from tests import test_officials_commands as fixtures

NAME = "officials.json"


class Counting:
    """Delegating real stream that records every requested and returned payload read."""

    def __init__(self, stream, log):
        self._stream, self._log = stream, log

    def read(self, *args):
        data = self._stream.read(*args)
        self._log.append(len(data))
        return data

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *exc):
        return self._stream.__exit__(*exc)


class ReadAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-read-admission-")
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.path = self.dir / NAME
        self.path.write_bytes(b'{"a": 1}')
        self.fd = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY)
        self.keeper = None
        self.addCleanup(os.close, self.fd)

    def _other(self):
        other = self.dir / "other"
        other.write_bytes(b'{"b": 2}')
        return other

    @contextlib.contextmanager
    def seam(self, after_stat=None):
        """Run `after_stat` once, just before the real relative open (after the real pathname stat); count payload reads."""
        reads, real_open, real_fdopen = [], os.open, os.fdopen
        opened = []

        def opening(path, flags, *args, **kwargs):
            if str(path) == NAME and after_stat is not None and not opened:
                after_stat()
            opened.append(path)
            return real_open(path, flags, *args, **kwargs)

        def wrapping(fd, *args, **kwargs):
            return Counting(real_fdopen(fd, *args, **kwargs), reads)

        with mock.patch.object(os, "open", opening), mock.patch.object(os, "fdopen", wrapping):
            yield reads

    def test_normal_read_unchanged(self):
        with self.seam() as reads:
            data, info = oc._read(self.fd, NAME, "x")
        self.assertEqual(data, b'{"a": 1}')
        self.assertEqual(info.st_size, 8)
        self.assertEqual(reads, [8])

    def test_substituted_regular_inode_is_rejected_before_read(self):
        def swap():
            other = self.dir / "other"
            other.write_bytes(b'{"b": 2}')  # same size, different inode
            os.replace(other, self.path)

        with self.seam(swap) as reads:
            with self.assertRaisesRegex(oc.OfficialsError, "changed while it was being read"):
                oc._read(self.fd, NAME, "x")
        self.assertEqual(reads, [])

    def test_populated_fifo_replacement_is_unread(self):
        fifo = self.dir / "fifo"

        def swap():
            os.mkfifo(fifo)
            self.keeper = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            self.addCleanup(os.close, self.keeper)
            os.write(self.keeper, b"synthetic-canary")
            os.replace(fifo, self.path)

        with self.seam(swap) as reads:
            with self.assertRaisesRegex(oc.OfficialsError, "changed while it was being read"):
                oc._read(self.fd, NAME, "x")
        self.assertEqual(reads, [])
        self.assertIsNotNone(self.keeper)
        self.assertEqual(os.read(self.keeper, 64), b"synthetic-canary")  # payload still unconsumed

    def test_static_fifo_refuses_before_open(self):
        os.unlink(self.path)
        os.mkfifo(self.path)
        with self.seam() as reads:
            with self.assertRaisesRegex(oc.OfficialsError, "must be a regular file"):
                oc._read(self.fd, NAME, "x")
        self.assertEqual(reads, [])

    def test_growth_before_open_is_refused_unread(self):
        def grow():
            os.truncate(self.path, MAX_JSON_BYTES + 100)

        with self.seam(grow) as reads:
            with self.assertRaisesRegex(oc.OfficialsError, "file too large"):
                oc._read(self.fd, NAME, "x")
        self.assertEqual(reads, [])

    def test_exact_cap_admitted_and_post_fstat_growth_still_caught(self):
        self.path.write_bytes(b" " * (MAX_JSON_BYTES - 2) + b"{}")
        with self.seam() as reads:
            data, _ = oc._read(self.fd, NAME, "x")
        self.assertEqual(len(data), MAX_JSON_BYTES)
        self.assertEqual(reads, [MAX_JSON_BYTES])

        real_fstat = os.fstat

        def growing(fd):
            info = real_fstat(fd)
            if stat.S_ISREG(info.st_mode) and info.st_size == MAX_JSON_BYTES:
                os.truncate(self.path, MAX_JSON_BYTES + 1)  # grows after the in-bound fstat
            return info

        with self.seam() as reads, mock.patch.object(os, "fstat", growing):
            with self.assertRaisesRegex(oc.OfficialsError, "file too large"):
                oc._read(self.fd, NAME, "x")
        self.assertEqual(reads, [MAX_JSON_BYTES + 1])

    def test_refusals_close_the_descriptor_once(self):
        opened = []

        def tracking_seam():
            real_inner = os.open  # the seam's own forwarding open

            def tracking(path, flags, *a, **k):
                fd = real_inner(path, flags, *a, **k)
                if str(path) == NAME:
                    opened.append(fd)
                return fd

            return mock.patch.object(os, "open", tracking)

        cases = [lambda: os.truncate(self.path, MAX_JSON_BYTES + 5),
                 lambda: os.replace(self._other(), self.path)]
        for after_stat in cases:
            opened.clear()
            with self.seam(after_stat), tracking_seam():
                with self.assertRaises(oc.OfficialsError):
                    oc._read(self.fd, NAME, "x")
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError):  # the one descriptor opened is closed after refusal
                os.fstat(opened[0])
            self.path.write_bytes(b'{"a": 1}')


class CallerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OfficialsCommandTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.file = self.fixture.file
        self.fixture.write([])

    def test_list_officials_refuses_grown_roster_unread_and_writes_nothing(self):
        before = sorted(p.name for p in self.fixture.content.iterdir())
        real_open, real_fdopen = os.open, os.fdopen
        reads = []

        def opening(path, flags, *a, **k):
            if str(path) == "officials.json":
                os.truncate(self.file, MAX_JSON_BYTES + 100)
            return real_open(path, flags, *a, **k)

        def wrapping(fd, *a, **k):
            return Counting(real_fdopen(fd, *a, **k), reads)

        with mock.patch.object(os, "open", opening), mock.patch.object(os, "fdopen", wrapping):
            with self.assertRaisesRegex(Exception, "file too large"):
                oc.list_officials(self.root, today=fixtures.TODAY)
        self.assertEqual([r for r in reads if r >= MAX_JSON_BYTES], [])
        self.assertEqual(sorted(p.name for p in self.fixture.content.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
