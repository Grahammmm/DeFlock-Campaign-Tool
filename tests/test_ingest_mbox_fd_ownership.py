"""RF0240-2: `_read_mbox` owns its raw descriptor across fstat/fdopen/read failures. Synthetic files only.
Run from the repository root: python3 -B -W error::ResourceWarning -m unittest tests.test_ingest_mbox_fd_ownership -q
"""

import errno
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from campaign_tool import ingest_mbox

REAL_OPEN, REAL_FSTAT, REAL_FDOPEN, REAL_CLOSE = os.open, os.fstat, os.fdopen, os.close
MBOX = b"From a@example.org Tue Nov  3 13:00:00 2026\nSubject: x\n\nbody\n\n"
FAULTS = (OSError("injected"), MemoryError(), KeyboardInterrupt())


def is_open(fd):
    try:
        REAL_FSTAT(fd)
        return True
    except OSError as err:
        assert err.errno == errno.EBADF
        return False


class Tracker:
    def __init__(self):
        self.acquired, self.closed = [], []

    def open(self, *a, **k):
        fd = REAL_OPEN(*a, **k)
        self.acquired.append(fd)
        return fd

    def close(self, fd):
        self.closed.append(fd)
        REAL_CLOSE(fd)


class FdOwnership(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "box.mbox"
        self.path.write_bytes(MBOX)
        self.t = Tracker()

    def run_read(self, **patches):
        with mock.patch("os.open", self.t.open), mock.patch("os.close", self.t.close):
            with mock.patch.multiple("os", **patches) if patches else mock.patch("os.getpid"):
                return ingest_mbox._read_mbox(str(self.path))

    def assert_closed_once(self):
        self.assertEqual(len(self.t.acquired), 1)
        self.assertEqual(self.t.closed, self.t.acquired)
        self.assertFalse(is_open(self.t.acquired[0]))

    def test_healthy_read_closes_once(self):
        self.assertEqual(self.run_read(), MBOX)
        self.assert_closed_once()

    def test_fstat_failure_closes_source_and_keeps_exception(self):
        for fault in FAULTS:
            with self.subTest(fault=type(fault).__name__):
                self.t = Tracker()
                with self.assertRaises(type(fault)) as cm:
                    self.run_read(fstat=mock.Mock(side_effect=fault))
                self.assertIs(cm.exception, fault)
                self.assert_closed_once()

    def test_fdopen_failure_closes_source_and_keeps_exception(self):
        seen = []
        for fault in FAULTS:
            with self.subTest(fault=type(fault).__name__):
                self.t = Tracker()

                def boom(*a, **k):
                    seen.append(k)
                    raise fault

                with self.assertRaises(type(fault)) as cm:
                    self.run_read(fdopen=boom)
                self.assertIs(cm.exception, fault)
                self.assert_closed_once()
        self.assertTrue(all(k.get("closefd") is False for k in seen))

    def test_read_failure_and_overflow_close_once(self):
        class Boom:
            def __init__(self, fault):
                self.fault = fault

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                raise self.fault

        for fault in FAULTS[:2]:
            self.t = Tracker()
            with self.assertRaises(type(fault)):
                self.run_read(fdopen=lambda *a, **k: Boom(fault))
            self.assert_closed_once()
        self.t = Tracker()
        with mock.patch.object(ingest_mbox, "MAX_MBOX_BYTES", len(MBOX) - 1):
            with self.assertRaisesRegex(ValueError, "Alpha limit"):
                self.run_read()
        self.assert_closed_once()
        self.t = Tracker()
        with mock.patch.object(ingest_mbox, "MAX_MBOX_BYTES", len(MBOX)):
            self.assertEqual(self.run_read(), MBOX)
        self.assert_closed_once()

    def test_nonregular_and_open_failure_contract(self):
        msg = "--mbox must be a regular, non-symlink file"
        for target in (self.dir,):
            self.t = Tracker()
            with self.assertRaisesRegex(ValueError, msg):
                with mock.patch("os.open", self.t.open), mock.patch("os.close", self.t.close):
                    ingest_mbox._read_mbox(str(target))
            self.assert_closed_once()
        fifo = self.dir / "fifo"
        os.mkfifo(fifo)
        self.t = Tracker()
        with self.assertRaisesRegex(ValueError, msg):
            with mock.patch("os.open", self.t.open), mock.patch("os.close", self.t.close):
                ingest_mbox._read_mbox(str(fifo))
        self.assert_closed_once()
        link = self.dir / "link"
        link.symlink_to(self.path)
        for missing in (self.dir / "nope", link):
            self.t = Tracker()
            with self.assertRaisesRegex(ValueError, msg):
                with mock.patch("os.open", self.t.open), mock.patch("os.close", self.t.close):
                    ingest_mbox._read_mbox(str(missing))
            self.assertEqual((self.t.acquired, self.t.closed), ([], []))

    def test_descriptor_zero_is_closed(self):
        saved = os.dup(0)
        try:
            REAL_CLOSE(0)
            with mock.patch("os.fstat", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    ingest_mbox._read_mbox(str(self.path))
            self.assertFalse(is_open(0))  # opened as fd 0, then released
            self.assertEqual(ingest_mbox._read_mbox(str(self.path)), MBOX)
            self.assertFalse(is_open(0))
        finally:
            os.dup2(saved, 0)
            REAL_CLOSE(saved)

    def test_no_redundant_close_of_reused_descriptor(self):
        sentinel = REAL_OPEN(os.devnull, os.O_RDONLY)
        self.addCleanup(lambda: is_open(sentinel) and REAL_CLOSE(sentinel))
        self.assertEqual(self.run_read(), MBOX)
        self.assertTrue(is_open(sentinel))
        self.assertEqual(self.t.closed, self.t.acquired)

    def test_preview_and_apply_fail_before_downstream(self):
        for dry in (True, False):
            for seam in ("fstat", "fdopen"):
                with self.subTest(dry=dry, seam=seam):
                    self.t = Tracker()
                    args = types.SimpleNamespace(mbox=str(self.path), source_id="s", dry_run=dry)
                    with mock.patch.object(ingest_mbox, "_split") as split, \
                            mock.patch.object(ingest_mbox, "database") as db, \
                            mock.patch.object(ingest_mbox, "_ledger_lookup") as lookup, \
                            mock.patch.object(ingest_mbox, "_require_receipt_name") as name, \
                            mock.patch("os." + seam, side_effect=OSError("injected")), \
                            mock.patch("os.open", self.t.open), mock.patch("os.close", self.t.close):
                        with self.assertRaises(OSError):
                            ingest_mbox.ingest_mbox(args, self.dir)
                    for m in (split, db, lookup, name):
                        m.assert_not_called()
                    self.assert_closed_once()
                    self.assertEqual(self.path.read_bytes(), MBOX)


if __name__ == "__main__":
    unittest.main()
