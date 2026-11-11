"""ledger_list._snapshot releases every descriptor it acquires when stream construction fails (synthetic only)."""
import errno
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import ledger_list

REFUSAL = "private/ledger.sqlite is not a readable ledger"


class Sentinel(BaseException):
    pass


def is_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError as error:
        if error.errno == errno.EBADF:
            return False
        raise


class SnapshotOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-fd-ownership-")
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
        self.sentinel = os.open(self.ledger, os.O_RDONLY)
        self.addCleanup(lambda: is_open(self.sentinel) and os.close(self.sentinel))
        self.opened, self.dups = [], []

    def run_with_fdopen(self, fdopen, sqlite_allowed=False):
        real_open, real_dup = os.open, os.dup

        def tracking_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            self.opened.append(fd)
            return fd

        def tracking_dup(fd):
            new = real_dup(fd)
            self.dups.append(new)
            return new

        def cleanup():
            for fd in self.dups:
                if is_open(fd):
                    os.close(fd)
        self.addCleanup(cleanup)  # a leaking implementation must not leak into other tests
        real_connect = sqlite3.connect
        calls = []

        def connect(target, *args, **kwargs):
            calls.append(target)
            if not sqlite_allowed:
                raise AssertionError("sqlite must not open")
            return real_connect(target, *args, **kwargs)
        with mock.patch("os.open", tracking_open), mock.patch("os.dup", tracking_dup), \
                mock.patch("os.fdopen", fdopen), mock.patch("sqlite3.connect", connect):
            result = ledger_list._snapshot(self.ledger)
        self.assertEqual(calls, [":memory:"])  # only reached on success
        return result

    def assertReleased(self):
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(len(self.dups), 1)
        self.assertEqual([is_open(fd) for fd in self.opened + self.dups], [False, False])
        self.assertTrue(is_open(self.sentinel), "an unrelated descriptor was closed")

    def test_fdopen_oserror_closes_original_and_duplicate_before_fixed_refusal(self):
        def failing(fd, mode):
            raise OSError("synthetic-secret-canary")
        with self.assertRaises(ValueError) as caught:
            self.run_with_fdopen(failing)
        self.assertEqual(str(caught.exception), REFUSAL)
        self.assertNotIn("canary", repr(caught.exception))
        self.assertReleased()

    def test_memoryerror_and_baseexception_unwind_without_reclassification(self):
        for error in (MemoryError("synthetic-allocation-failure"), Sentinel()):
            with self.subTest(error=type(error).__name__):
                self.opened, self.dups = [], []

                def failing(fd, mode, error=error):
                    raise error
                with self.assertRaises(type(error)) as caught:
                    self.run_with_fdopen(failing)
                self.assertIs(caught.exception, error)
                self.assertReleased()

    def test_repeated_constructor_refusals_do_not_accumulate_and_then_recover(self):
        def failing(fd, mode):
            raise OSError
        for _ in range(12):
            with self.assertRaises(ValueError):
                self.run_with_fdopen(failing)
        self.assertEqual(len(self.dups), 12)
        self.assertFalse(any(is_open(fd) for fd in self.opened + self.dups))
        self.assertTrue(is_open(self.sentinel))
        self.assertEqual(ledger_list.list_receipts(self.root), {"receipts": [], "count": 0})

    def test_constructor_that_already_closed_the_duplicate_keeps_original_exception(self):
        def closing(fd, mode):
            os.close(fd)
            raise OSError
        with self.assertRaises(ValueError) as caught:
            self.run_with_fdopen(closing)
        self.assertEqual(str(caught.exception), REFUSAL)
        self.assertReleased()

    def test_dup_failure_closes_only_the_original(self):
        with mock.patch("os.dup", side_effect=OSError(errno.EMFILE, "synthetic")):
            with self.assertRaises(ValueError) as caught:
                real_open = os.open
                with mock.patch("os.open", lambda *a, **k: self.opened.append(real_open(*a, **k)) or self.opened[-1]):
                    ledger_list._snapshot(self.ledger)
        self.assertEqual(str(caught.exception), REFUSAL)
        self.assertEqual(len(self.opened), 1)
        self.assertFalse(is_open(self.opened[0]))
        self.assertTrue(is_open(self.sentinel))

    def test_successful_transfer_closes_once_without_over_close(self):
        real_fdopen = os.fdopen
        streams = []

        def forwarding(fd, mode):
            streams.append(real_fdopen(fd, mode))
            return streams[-1]
        db = self.run_with_fdopen(forwarding, sqlite_allowed=True)
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM receipts").fetchone(), (0,))
        except AssertionError:
            raise
        finally:
            db.close()
        self.assertTrue(streams[0].closed)
        self.assertReleased()
