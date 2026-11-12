"""RF0440-3: _temporary gives each descriptor exactly one closing owner (synthetic data, no network)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import supporters as s
from tests.test_supporters import TODAY, Base

REAL_OPEN, REAL_CLOSE, REAL_FDOPEN = os.open, os.close, os.fdopen


class Wrapped:
    """Real stream; optional failure, and an unrelated sentinel opened right after real closure."""

    def __init__(self, handle, owner, fail=None, exit_error=None):
        self.handle, self.owner, self.fail, self.exit_error = handle, owner, fail, exit_error

    def __enter__(self):
        self.handle.__enter__()
        return self

    def fileno(self):
        return self.handle.fileno()

    def write(self, data):
        if self.fail == "write":
            raise OSError(28, "synthetic write failure")
        return self.handle.write(data)

    def flush(self):
        if self.fail == "flush":
            raise OSError(28, "synthetic flush failure")
        return self.handle.flush()

    def __exit__(self, *args):
        fd = self.handle.fileno()
        result = self.handle.__exit__(*args)
        sentinel = REAL_OPEN(os.devnull, os.O_RDONLY)
        self.owner.sentinels.append(sentinel)
        self.owner.reused.append(sentinel == fd)
        if self.exit_error:
            raise self.exit_error
        return result


class WriterOwnership(Base):
    def setUp(self):
        super().setUp()
        self.sentinels, self.reused, self.today = [], [], TODAY
        self.addCleanup(self.close_sentinels)
        self.folder = self.tmp / "folder"
        self.folder.mkdir()

    def close_sentinels(self):
        for fd in self.sentinels:
            try:
                REAL_CLOSE(fd)
            except OSError:
                pass

    def patched(self, **kw):
        def fdopen(fd, mode):
            handle = REAL_FDOPEN(fd, mode)
            return Wrapped(handle, self, **kw) if mode == "wb" else handle
        return mock.patch.object(s.os, "fdopen", fdopen)

    def alive(self):
        for fd in self.sentinels:
            os.fstat(fd)

    def test_write_flush_fsync_and_exit_failure_never_close_reused_fd(self):
        cases = [dict(fail="write"), dict(fail="flush"), dict(exit_error=OSError(5, "synthetic exit"))]
        for kw in cases:
            for mode in (0o600, 0o644):
                with self.subTest(kw=kw, mode=mode), self.patched(**kw):
                    with self.assertRaises(OSError):
                        s._temporary(self.folder, b"x", mode)
                    self.alive()
                    self.assertEqual(os.listdir(self.folder), [])
        self.assertTrue(self.reused and all(self.reused))

    def test_fsync_failure_never_closes_reused_fd(self):
        with self.patched(), mock.patch.object(s.os, "fsync", side_effect=OSError(5, "synthetic fsync")):
            with self.assertRaises(OSError):
                s._temporary(self.folder, b"x", 0o600)
        self.alive()
        self.assertTrue(all(self.reused))
        self.assertEqual(os.listdir(self.folder), [])

    def test_after_handoff_base_exceptions_propagate_same_object(self):
        for exc in (RuntimeError("r"), KeyboardInterrupt(), SystemExit(3)):
            with self.subTest(exc=type(exc).__name__), self.patched(exit_error=exc):
                with self.assertRaises(type(exc)) as ctx:
                    s._temporary(self.folder, b"x", 0o600)
                self.assertIs(ctx.exception, exc)
                self.alive()
                self.assertEqual(os.listdir(self.folder), [])

    def test_before_handoff_failures_close_raw_owner_once(self):
        for exc in (OSError(1, "chmod"), RuntimeError("r"), KeyboardInterrupt(), SystemExit(2)):
            for seam in ("fchmod", "fdopen"):
                with self.subTest(exc=type(exc).__name__, seam=seam):
                    closed, opened = [], []
                    real_mkstemp = tempfile.mkstemp

                    def mkstemp(*a, **k):
                        fd, name = real_mkstemp(*a, **k)
                        opened.append(fd)
                        return fd, name

                    def close(fd):
                        closed.append(fd)
                        REAL_CLOSE(fd)
                    with mock.patch.object(s.tempfile, "mkstemp", mkstemp), \
                            mock.patch.object(s.os, seam, side_effect=exc), \
                            mock.patch.object(s.os, "close", close):
                        with self.assertRaises(type(exc)) as ctx:
                            s._temporary(self.folder, b"x", 0o600)
                    self.assertIs(ctx.exception, exc)
                    self.assertEqual(closed, opened)
                    self.assertEqual(len(closed), 1)
                    self.assertEqual(os.listdir(self.folder), [])

    def test_mkstemp_failure_owns_nothing(self):
        with mock.patch.object(s.tempfile, "mkstemp", side_effect=OSError(28, "full")), \
                mock.patch.object(s.os, "close") as close, mock.patch.object(s, "_discard") as discard:
            with self.assertRaises(OSError):
                s._temporary(self.folder, b"x", 0o600)
        close.assert_not_called()
        discard.assert_not_called()

    def test_cleanup_close_error_does_not_replace_primary(self):
        primary = OSError(1, "chmod")

        def close(fd):
            REAL_CLOSE(fd)
            raise OSError(9, "synthetic cleanup")
        with mock.patch.object(s.os, "fchmod", side_effect=primary), mock.patch.object(s.os, "close", close):
            with self.assertRaises(OSError) as ctx:
                s._temporary(self.folder, b"x", 0o600)
        self.assertIs(ctx.exception, primary)
        self.assertEqual(os.listdir(self.folder), [])

    def test_success_bytes_and_modes(self):
        for mode in (0o600, 0o644):
            name = s._temporary(self.folder, b"payload", mode)
            self.assertEqual(Path(name).read_bytes(), b"payload")
            self.assertEqual(os.stat(name).st_mode & 0o777, mode)

    def test_add_failure_preserves_stores_and_sentinel(self):
        self.add()
        before = (self.public_file.read_bytes(), self.private_file.read_bytes())
        for kw in (dict(fail="write"), dict(exit_error=OSError(5, "x"))):
            with self.subTest(kw=kw), self.patched(**kw):
                with self.assertRaises(s.SupporterError):
                    self.add(name="Other Synthetic Org")
            self.alive()
            self.assertEqual((self.public_file.read_bytes(), self.private_file.read_bytes()), before)
            self.assertEqual([p for p in self.root.rglob(".supporters-*")], [])
        self.add(name="Other Synthetic Org")

    def test_grant_failure_preserves_private_and_sentinel(self):
        sid = self.add()["id"]
        before = (self.public_file.read_bytes(), self.private_file.read_bytes())
        for kw in (dict(fail="write"), dict(exit_error=OSError(5, "x"))):
            with self.subTest(kw=kw), self.patched(**kw):
                with self.assertRaises(OSError):
                    s.set_cosign_consent(self.root, sid, cosign=["any"], confirmed_on="2026-09-02", how="email",
                                         today=self.today)
            self.alive()
            self.assertEqual((self.public_file.read_bytes(), self.private_file.read_bytes()), before)
            self.assertEqual([p for p in self.root.rglob(".supporters-*")], [])
        s.set_cosign_consent(self.root, sid, cosign=["any"], confirmed_on="2026-09-02", how="email", today=self.today)

    def test_rollback_restoration_temporary_obeys_handoff(self):
        self.add()
        real_replace = os.replace
        calls = []

        def replace(a, b):
            calls.append(b)
            if len(calls) == 2:
                raise OSError(5, "synthetic replace failure")
            return real_replace(a, b)
        real_temporary = s._temporary
        seen = []

        def temporary(folder, data, mode):
            seen.append(1)
            if len(calls) >= 1 and len(seen) > 2:
                with self.patched(fail="write"):
                    return real_temporary(folder, data, mode)
            return real_temporary(folder, data, mode)
        with mock.patch.object(s.os, "replace", replace), mock.patch.object(s, "_temporary", temporary):
            with self.assertRaises(s.SupporterError):
                self.add(name="Other Synthetic Org")
        self.alive()
        self.assertEqual([p for p in self.root.rglob(".supporters-*")], [])


if __name__ == "__main__":
    unittest.main()
