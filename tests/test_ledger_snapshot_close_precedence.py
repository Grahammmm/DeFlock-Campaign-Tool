"""ledger_list._snapshot keeps the primary failure when a raw-descriptor close also reports EIO (synthetic only)."""
import contextlib
import errno
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli, ledger_list

REFUSAL = "private/ledger.sqlite is not a readable ledger"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"


class Custom(BaseException):
    pass


def is_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError as error:
        if error.errno == errno.EBADF:
            return False
        raise


class FailingReader(io.BufferedReader):
    def read(self, size=-1):
        raise RuntimeError("SYNTHETIC_READ")


class MemoryProxy:
    """Forwards to a real memory connection and counts close calls."""

    def __init__(self, real, deserialize_error=None):
        self.real, self.closes, self.deserialize_error = real, 0, deserialize_error

    def deserialize(self, data):
        if self.deserialize_error:
            raise self.deserialize_error
        return self.real.deserialize(data)

    def close(self):
        self.closes += 1
        self.real.close()

    def __getattr__(self, name):
        return getattr(self.real, name)


class ClosePrecedenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-close-precedence-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "private").mkdir()
        self.ledger = self.root / "private" / "ledger.sqlite"
        db = sqlite3.connect(self.ledger)
        db.execute("CREATE TABLE objects (sha256 TEXT, byte_count INTEGER)")
        db.execute("CREATE TABLE receipts (receipt_id INTEGER, object_sha256 TEXT, source_id TEXT, "
                   "original_name TEXT, received_at TEXT)")
        db.commit()
        db.close()
        self.original = self.ledger.read_bytes()
        self.attempts = {}  # fd -> close attempts on that generation
        self.proxies = []
        self.leaf = self.directory = None

    def run_snapshot(self, fail=("leaf", "directory"), fdopen=None, deserialize_error=None, after_close=None):
        """Run _snapshot with real forwarding closes: the descriptor is really released, then EIO is reported."""
        real_open, real_close, real_connect = os.open, os.close, sqlite3.connect

        def tracking_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if kwargs.get("dir_fd") is not None:
                self.leaf = fd
            elif flags & os.O_DIRECTORY:
                self.directory = fd
            return fd

        def closing(fd):
            real_close(fd)
            self.attempts[fd] = self.attempts.get(fd, 0) + 1
            if after_close:
                after_close(fd)
            if (fd == self.leaf and "leaf" in fail) or (fd == self.directory and "directory" in fail):
                raise OSError(errno.EIO, "SYNTHETIC_SECONDARY_CLOSE")

        def connect(*args, **kwargs):
            proxy = MemoryProxy(real_connect(*args, **kwargs), deserialize_error)
            self.proxies.append(proxy)
            return proxy
        patches = [mock.patch("os.open", tracking_open), mock.patch("os.close", closing),
                   mock.patch("sqlite3.connect", connect)]
        if fdopen:
            patches.append(mock.patch("os.fdopen", fdopen))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return ledger_list._snapshot(self.ledger)

    def assertEachClosedOnce(self, *names):
        for name in names:
            self.assertEqual(self.attempts.get(getattr(self, name)), 1, name)

    def assertUnchanged(self):
        self.assertEqual(self.ledger.read_bytes(), self.original)
        self.assertEqual(sorted(p.name for p in self.ledger.parent.iterdir()), ["ledger.sqlite"])

    def test_constructor_primary_identity_survives_each_raw_close_eio(self):
        primaries = [(MemoryError("SYNTHETIC_PRIMARY"), MemoryError), (RuntimeError("p"), RuntimeError),
                     (KeyboardInterrupt(), KeyboardInterrupt), (SystemExit(3), SystemExit), (Custom(), Custom)]
        for fail in (("leaf",), ("directory",), ("leaf", "directory")):
            for error, expected in primaries:
                self.attempts.clear()
                dups = []

                def fdopen(dup, mode, error=error, dups=dups):
                    dups.append(dup)
                    raise error
                with self.assertRaises(expected) as caught:
                    self.run_snapshot(fail=fail, fdopen=fdopen)
                self.assertIs(caught.exception, error)
                self.assertEachClosedOnce("leaf", "directory")
                self.assertFalse(is_open(dups[0]))
            self.attempts.clear()
            with self.assertRaises(ValueError) as caught:  # expected I/O failure keeps the fixed refusal
                self.run_snapshot(fail=fail, fdopen=mock.Mock(side_effect=OSError(errno.EMFILE, "x")))
            self.assertEqual(str(caught.exception), REFUSAL)
            self.assertEachClosedOnce("leaf", "directory")
        self.assertUnchanged()

    def test_specific_size_and_sidecar_refusal_survive_directory_release(self):
        with mock.patch.object(ledger_list, "MAX_LEDGER_BYTES", 1):
            with self.assertRaises(ValueError) as caught:
                self.run_snapshot()
        self.assertEqual(str(caught.exception), ledger_list.LEDGER_SIZE_ERROR)
        self.assertEachClosedOnce("leaf", "directory")
        self.assertEqual(self.proxies, [])  # no SQLite work after the admission refusal
        self.attempts.clear()
        sidecar = self.ledger.parent / "ledger.sqlite-wal"
        sidecar.write_bytes(b"")
        with self.assertRaises(ValueError) as caught:
            self.run_snapshot(fail=("directory",))
        self.assertEqual(str(caught.exception), ledger_list.SIDECAR_ERROR)
        self.assertEqual(self.proxies, [])
        sidecar.unlink()
        self.assertUnchanged()

    def test_sidecar_appearing_during_read_survives_raw_close_eio(self):
        sidecar = self.ledger.parent / "ledger.sqlite-journal"

        def appear(fd):
            if fd == self.leaf:
                sidecar.write_bytes(b"")
        with self.assertRaises(ValueError) as caught:
            self.run_snapshot(after_close=appear)
        self.assertEqual(str(caught.exception), REFUSAL)  # the leaf close is the only primary here: cleanup-only
        sidecar.unlink()

    def test_fstat_dup_and_read_failure_cleanup_classification(self):
        real_fstat = os.fstat
        for error, expected in ((OSError(errno.EIO, "x"), ValueError), (MemoryError("p"), MemoryError)):
            self.attempts.clear()
            calls = []

            def fstat(fd, error=error, calls=calls):
                calls.append(fd)
                if len(calls) == 2:  # first is the directory, second the ledger leaf
                    raise error
                return real_fstat(fd)
            with mock.patch("os.fstat", fstat), self.assertRaises(expected) as caught:
                self.run_snapshot()
            if expected is MemoryError:
                self.assertIs(caught.exception, error)
            else:
                self.assertEqual(str(caught.exception), REFUSAL)
            self.assertEachClosedOnce("leaf", "directory")
        for error, expected in ((OSError(errno.EMFILE, "x"), ValueError), (Custom(), Custom)):
            self.attempts.clear()
            with mock.patch("os.dup", side_effect=error), self.assertRaises(expected) as caught:
                self.run_snapshot()
            if expected is Custom:
                self.assertIs(caught.exception, error)
            else:
                self.assertEqual(str(caught.exception), REFUSAL)
            self.assertEachClosedOnce("leaf", "directory")
        self.attempts.clear()
        with mock.patch("os.fdopen", lambda dup, mode: FailingReader(io.FileIO(dup, "rb"))), \
                self.assertRaises(RuntimeError):
            self.run_snapshot()
        self.assertEachClosedOnce("leaf", "directory")

    def test_close_only_failure_never_returns_snapshot_or_report(self):
        for fail in (("leaf",), ("directory",), ("leaf", "directory")):
            self.attempts.clear()
            self.proxies.clear()
            with self.assertRaises(ValueError) as caught:
                self.run_snapshot(fail=fail)
            self.assertEqual(str(caught.exception), REFUSAL)
            self.assertEachClosedOnce("leaf", "directory")
            for proxy in self.proxies:
                self.assertEqual(proxy.closes, 1)  # an allocated memory connection never escapes
            with self.assertRaises(ValueError):
                with mock.patch("os.close", side_effect=OSError(errno.EIO, "x")):
                    ledger_list.list_receipts(self.root)  # no report on a close-only failure
        self.attempts.clear()
        db = self.run_snapshot(fail=())  # a healthy next call succeeds and stays caller-owned
        self.assertEqual(db.closes, 0)
        db.close()
        self.assertUnchanged()

    def test_first_close_failure_wins_and_remaining_release_runs(self):
        error = Custom()

        def fdopen(dup, mode):
            raise error
        self.proxies.clear()
        with self.assertRaises(Custom) as caught:
            self.run_snapshot(fdopen=fdopen)
        self.assertIs(caught.exception, error)
        self.assertEachClosedOnce("leaf", "directory")
        # release-only: both raw closes fail with no primary; the first (leaf) failure is the one reported
        self.attempts.clear()
        with self.assertRaises(ValueError) as caught:
            self.run_snapshot()
        self.assertIsNone(caught.exception.__cause__)
        self.assertEachClosedOnce("leaf", "directory")
        for proxy in self.proxies:
            self.assertEqual(proxy.closes, 1)

    def test_reused_fd_generation_is_not_closed_again(self):
        sentinels = []

        def reuse(fd):
            if fd == self.leaf and not sentinels:
                sentinels.append(os.open(self.ledger, os.O_RDONLY))  # takes the just-released number
        self.addCleanup(lambda: [os.close(s) for s in sentinels if is_open(s)])
        with self.assertRaises(ValueError):
            self.run_snapshot(fail=("leaf",), after_close=reuse)
        self.assertEqual(sentinels, [self.leaf])  # the number really was reused
        self.assertTrue(is_open(sentinels[0]))  # and never closed a second time by the snapshot
        self.assertEqual(self.attempts[self.leaf], 1)

    def test_api_and_actual_cli_specific_refusal_are_read_only(self):
        campaign = self.root / "campaign"
        shutil.copytree(EXAMPLE, campaign, ignore=shutil.ignore_patterns("public"))
        (campaign / "private").mkdir(exist_ok=True)
        shutil.copy2(self.ledger, campaign / "private" / "ledger.sqlite")
        self.ledger = campaign / "private" / "ledger.sqlite"
        self.root = campaign
        before = self.ledger.stat()
        siblings = sorted(p.name for p in self.ledger.parent.iterdir())

        def eio_close(fd, real=os.close):
            real(fd)
            raise OSError(errno.EIO, "SYNTHETIC_SECONDARY_CLOSE")
        with mock.patch.object(ledger_list, "MAX_LEDGER_BYTES", 1), mock.patch("os.close", eio_close):
            with self.assertRaises(ValueError) as caught:
                ledger_list.list_receipts(self.root)
            self.assertEqual(str(caught.exception), ledger_list.LEDGER_SIZE_ERROR)
            out, err = io.StringIO(), io.StringIO()
            argv = ["campaign_tool", "status", "--directory", str(self.root), "--list"]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as exit_info:
                    cli.main()
        self.assertEqual(exit_info.exception.code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("Stopped: " + ledger_list.LEDGER_SIZE_ERROR, err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("SYNTHETIC_SECONDARY_CLOSE", err.getvalue())
        after = self.ledger.stat()
        self.assertEqual((before.st_mode, before.st_mtime_ns), (after.st_mode, after.st_mtime_ns))
        self.assertEqual(sorted(p.name for p in self.ledger.parent.iterdir()), siblings)
        self.assertEqual(self.ledger.read_bytes(), self.original)

    def test_aborted_memory_snapshot_releases_connection_without_masking_primary(self):
        for error in (MemoryError("p"), Custom(), sqlite3.DatabaseError("db")):
            self.proxies.clear()
            self.attempts.clear()
            if isinstance(error, sqlite3.Error):
                with self.assertRaises(ValueError) as caught:
                    self.run_snapshot(fail=("directory",), deserialize_error=error)
                self.assertEqual(str(caught.exception), REFUSAL)
            else:
                with self.assertRaises(type(error)) as caught:
                    self.run_snapshot(fail=("directory",), deserialize_error=error)
                self.assertIs(caught.exception, error)
            self.assertEqual([p.closes for p in self.proxies], [1])
            self.assertEachClosedOnce("leaf", "directory")
        self.proxies.clear()
        with self.assertRaises(ValueError):  # directory release failure after allocation
            self.run_snapshot(fail=("directory",))
        self.assertEqual([p.closes for p in self.proxies], [1])

    def test_healthy_snapshot_contract_remains(self):
        db = self.run_snapshot(fail=())
        self.assertEqual(db.execute("SELECT count(*) FROM receipts").fetchone(), (0,))
        db.close()
        self.assertEqual(ledger_list.list_receipts(self.root), {"receipts": [], "count": 0})
        self.assertUnchanged()


if __name__ == "__main__":
    unittest.main()
