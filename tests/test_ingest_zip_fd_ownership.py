"""RF1420-3: `ingest --zip` closes the archive source descriptor even when building its stream fails.

`_read_archive` owns the raw descriptor from `os.open` until one `os.close`; the stream is non-owning.
Every file here is synthetic. Run from the repository root:
python3 -B -W error::ResourceWarning -m unittest tests.test_ingest_zip_fd_ownership -q
"""

import argparse
import contextlib
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool import ingest_zip as iz
from tests import test_ingest_zip as zip_fixtures  # module alias: its TestCase is not re-collected here

REPO_ROOT = Path(__file__).resolve().parents[1]
DIR_TEXT = "--zip must be a regular, non-symlink file"
CAP_TEXT = "Alpha limit: 200 MiB per archive; nothing was imported"


def is_open(fd):
    try:
        os.fstat(fd)
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return False
        raise
    return True


class BadReadFileIO(io.FileIO):
    def readinto(self, buffer):
        raise OSError(errno.EIO, "synthetic read fault")


class OsProxy:
    """`os` for the ZIP module only: forwards to the real calls and records the source descriptor's life.

    After the source descriptor is really closed a sentinel is opened at once, so a second close of the
    recycled number would close the sentinel instead and be detected."""

    def __init__(self, path, fdopen=None, fstat=None):
        self.path, self._fdopen, self._fstat = os.fspath(path), fdopen, fstat
        self.fds, self.closes, self.fdopen_kwargs, self.sentinels = [], [], [], []

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, path, flags, *args):
        fd = os.open(path, flags, *args)
        if os.fspath(path) == self.path:
            self.fds.append(fd)
        return fd

    def fdopen(self, fd, *args, **kwargs):
        self.fdopen_kwargs.append(kwargs)
        return (self._fdopen or os.fdopen)(fd, *args, **kwargs)

    def fstat(self, fd):
        return (self._fstat or os.fstat)(fd)

    def close(self, fd):
        os.close(fd)
        if fd in self.fds:
            self.closes.append(fd)
            self.sentinels.append(os.open(os.devnull, os.O_RDONLY))

    def release(self):
        for fd in self.sentinels:
            os.close(fd)


class FdOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-zip-fd-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.archive = self.base / "a.zip"
        self.payload = zip_fixtures.build_zip([("x.txt", b"synthetic x\n", {})])
        self.archive.write_bytes(self.payload)
        self.directory = self.base / "directory.zip"
        self.directory.mkdir()

    def read(self, path, **proxy_options):
        """Run `_read_archive` with a forwarding `os`; return (proxy, result or raised exception)."""
        proxy = OsProxy(path, **proxy_options)
        self.addCleanup(proxy.release)
        try:
            with mock.patch.object(iz, "os", proxy):
                outcome = iz._read_archive(str(path))
        except BaseException as exc:  # KeyboardInterrupt included: the test checks it propagates unchanged
            outcome = exc
        for fd in proxy.fds:  # never leave a leak from old code open in the test process
            if fd not in proxy.closes and is_open(fd):
                self.addCleanup(os.close, fd)
        return proxy, outcome

    def assert_one_close(self, proxy):
        self.assertEqual(len(proxy.fds), 1)
        fd = proxy.fds[0]
        self.assertFalse(is_open(fd) and fd not in proxy.closes, "source descriptor left open")
        self.assertEqual(proxy.closes, [fd], "source descriptor must be closed exactly once by its owner")
        for sentinel in proxy.sentinels:
            self.assertTrue(is_open(sentinel), "a recycled descriptor number was closed a second time")
        self.assertEqual(proxy.fdopen_kwargs[-1:], [{"closefd": False}])

    def test_real_directory_constructor_failure_closes_source(self):
        for _ in range(5):
            proxy, outcome = self.read(self.directory)
            self.assertIsInstance(outcome, IsADirectoryError)
            self.assert_one_close(proxy)

    def test_constructor_faults_close_raw_fd_and_preserve_exception(self):
        before = self.archive.read_bytes()
        for fault in (OSError(errno.EIO, "synthetic"), MemoryError(), KeyboardInterrupt()):
            def failing(fd, *args, **kwargs):
                raise fault
            proxy, outcome = self.read(self.archive, fdopen=failing)
            self.assertIs(outcome, fault)
            self.assert_one_close(proxy)
        built = []

        def builds_then_fails(fd, *args, **kwargs):
            built.append(os.fdopen(fd, *args, **kwargs))
            built[-1].close()
            raise OSError(errno.EIO, "synthetic after stream")
        proxy, outcome = self.read(self.archive, fdopen=builds_then_fails)
        self.assertIsInstance(outcome, OSError)
        self.assert_one_close(proxy)
        self.assertTrue(built[0].closed)
        self.assertEqual(self.archive.read_bytes(), before)

    def test_later_unwind_and_success_have_one_cleanup_owner(self):
        proxy, outcome = self.read(self.archive)
        self.assertEqual(outcome, self.payload)
        self.assert_one_close(proxy)
        empty = self.base / "empty.zip"
        empty.write_bytes(b"")
        proxy, outcome = self.read(empty)
        self.assertEqual(outcome, b"")
        self.assert_one_close(proxy)

        fstat_fault = OSError(errno.EIO, "synthetic fstat")

        def bad_fstat(fd):
            raise fstat_fault
        proxy, outcome = self.read(self.archive, fstat=bad_fstat)
        self.assertIs(outcome, fstat_fault)
        self.assert_one_close(proxy)

        streams = []

        def bad_read_stream(fd, mode, closefd=True):
            streams.append(io.BufferedReader(BadReadFileIO(fd, "r", closefd=closefd)))
            return streams[-1]
        proxy, outcome = self.read(self.archive, fdopen=bad_read_stream)
        self.assertIsInstance(outcome, OSError)
        self.assertEqual(outcome.errno, errno.EIO)
        self.assertTrue(streams[0].closed)
        self.assert_one_close(proxy)

        with mock.patch.object(iz, "MAX_ARCHIVE_BYTES", len(self.payload) - 1):
            proxy, outcome = self.read(self.archive)
        self.assertIsInstance(outcome, ValueError)
        self.assertEqual(str(outcome), CAP_TEXT)
        self.assert_one_close(proxy)

    def test_open_refusals_nonblocking_types_and_byte_caps_unchanged(self):
        (self.base / "link.zip").symlink_to(self.archive)
        (self.base / "dangling.zip").symlink_to(self.base / "absent")
        for name in ("missing.zip", "link.zip", "dangling.zip"):
            proxy, outcome = self.read(self.base / name)
            self.assertIsInstance(outcome, ValueError)
            self.assertEqual(str(outcome), DIR_TEXT)
            self.assertEqual((proxy.fds, proxy.closes, proxy.fdopen_kwargs), ([], [], []))
        fifo = self.base / "fifo.zip"
        os.mkfifo(fifo)
        proxy, outcome = self.read(fifo)  # no writer: a blocking read would hang this test
        self.assertIsInstance(outcome, ValueError)
        self.assertEqual(str(outcome), DIR_TEXT)
        self.assert_one_close(proxy)
        proxy, outcome = self.read(self.directory)
        self.assertIsInstance(outcome, IsADirectoryError)
        self.assert_one_close(proxy)

        size = len(self.payload)
        for cap, refused in ((size - 1, True), (size, False), (size + 1, False)):
            requests = []

            class Recording(io.BufferedReader):
                def read(self, size=-1):
                    requests.append(size)
                    return super().read(size)

            def recording(fd, mode, closefd=True):
                return Recording(io.FileIO(fd, "r", closefd=closefd))
            with mock.patch.object(iz, "MAX_ARCHIVE_BYTES", cap):
                proxy, outcome = self.read(self.archive, fdopen=recording)
            self.assertEqual(requests, [cap + 1])
            self.assertEqual(str(outcome) if refused else outcome, CAP_TEXT if refused else self.payload)
            self.assert_one_close(proxy)
        self.assertEqual(iz.MAX_ARCHIVE_BYTES, 200 * 1024 * 1024)


class ImporterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-zip-fd-cli-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root, self.fresh = self.base / "campaign", self.base / "fresh"
        self.archive = self.base / "a.zip"
        self.archive.write_bytes(zip_fixtures.build_zip([("x.txt", b"synthetic x\n", {}),
                                                         ("y.txt", b"synthetic y\n", {})]))
        self.directory = self.base / "directory.zip"
        self.directory.mkdir()
        self.cli("init", "--name", "Synthetic Campaign", "--county", "Synthetic County", "--state", "ca")

    def cli(self, command, *arguments, returncode=0):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", command, "--directory", str(self.root), *map(str, arguments)],
            cwd=self.base, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, returncode, f"stdout={result.stdout!r}; stderr={result.stderr!r}")
        self.assertNotIn("Traceback", result.stderr)
        return result

    def snapshot(self, root):
        if not root.exists():
            return None
        return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None, stat.S_IMODE(p.lstat().st_mode),
                                           p.lstat().st_mtime_ns) for p in sorted(root.rglob("*"))}

    def test_preview_apply_stop_before_campaign_work(self):
        self.cli("ingest", "--zip", self.archive, "--source-id", "synthetic-1")  # populated campaign
        tripwire = mock.Mock(side_effect=AssertionError("campaign work reached"))
        names = ("_open_archive", "_ledger_lookup", "database", "_previewer", "_store_bytes")

        def failing(fd, *args, **kwargs):
            raise OSError(errno.EIO, "synthetic constructor")
        for root in (self.root, self.fresh):
            for dry in (True, False):
                for source, fdopen in ((self.directory, None), (self.archive, failing)):
                    before = self.snapshot(root)
                    proxy = OsProxy(source, fdopen=fdopen)
                    self.addCleanup(proxy.release)
                    args = argparse.Namespace(zip=str(source), source_id="synthetic-2", dry_run=dry)
                    out = io.StringIO()
                    with contextlib.ExitStack() as stack:
                        for name in names:
                            stack.enter_context(mock.patch.object(iz, name, tripwire))
                        stack.enter_context(mock.patch.object(iz, "os", proxy))
                        stack.enter_context(contextlib.redirect_stdout(out))
                        with self.assertRaises(OSError):
                            iz.ingest_zip(args, root)
                    self.assertEqual(out.getvalue(), "")
                    tripwire.assert_not_called()
                    self.assertEqual(len(proxy.fds), 1)
                    self.assertFalse(is_open(proxy.fds[0]) and not proxy.closes, "source descriptor left open")
                    self.assertEqual(proxy.closes, proxy.fds)
                    self.assertEqual(self.snapshot(root), before)
        self.assertFalse(self.fresh.exists())

    def test_healthy_retry_and_cli_preserve_intake_contract(self):
        proxy = OsProxy(self.archive, fdopen=mock.Mock(side_effect=MemoryError()))
        self.addCleanup(proxy.release)
        with mock.patch.object(iz, "os", proxy), self.assertRaises(MemoryError):
            iz._read_archive(str(self.archive))
        self.assertEqual(proxy.closes, proxy.fds)
        healthy = OsProxy(self.archive)
        self.addCleanup(healthy.release)
        with mock.patch.object(iz, "os", healthy):
            self.assertEqual(iz._read_archive(str(self.archive)), self.archive.read_bytes())
        self.assertEqual(healthy.closes, healthy.fds)

        stopped = self.cli("ingest", "--zip", self.directory, "--source-id", "synthetic-1", returncode=1)
        self.assertEqual(stopped.stdout, "")
        self.assertTrue(stopped.stderr.startswith("Stopped:"), stopped.stderr)

        dry = self.cli("ingest", "--zip", self.archive, "--source-id", "synthetic-1", "--dry-run").stdout
        body, footer = dry.rstrip("\n").rsplit("\n", 1)
        self.assertEqual(footer, "Dry run: nothing was written.")
        applied = json.loads(self.cli("ingest", "--zip", self.archive, "--source-id", "synthetic-1").stdout)
        replay = json.loads(self.cli("ingest", "--zip", self.archive, "--source-id", "synthetic-1").stdout)
        preview = json.loads(body)
        self.assertEqual(set(applied), set(replay))
        self.assertEqual(applied["skipped"], replay["skipped"])
        dump = lambda report: json.dumps(report, sort_keys=True)
        self.assertIn(hashlib.sha256(self.archive.read_bytes()).hexdigest(), dump(applied))
        for member in (b"synthetic x\n", b"synthetic y\n"):
            self.assertIn(hashlib.sha256(member).hexdigest(), dump(applied))
            self.assertIn(hashlib.sha256(member).hexdigest(), dump(preview))
            self.assertIn(hashlib.sha256(member).hexdigest(), dump(replay))


if __name__ == "__main__":
    unittest.main()
