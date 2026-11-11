"""RF0540-5: recent closes each raw descriptor once, even when stream construction fails.

Synthetic scratch files only; no network. Run: python3 -B -m unittest tests.test_recent_descriptor_ownership -q
"""
import errno
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import recent  # noqa: E402

TODAY = date(2026, 11, 4)
REAL_OPEN, REAL_CLOSE, REAL_FDOPEN = os.open, os.close, os.fdopen


def is_open(fd):
    try:
        os.fstat(fd)
    except OSError as exc:
        return exc.errno != errno.EBADF
    return True


class Tracker:
    """Forward real os.open/os.close from recent, recording the descriptors acquired and the close attempts."""

    def __init__(self):
        self.opened, self.closed, self.paths = [], [], {}

    def open(self, *args, **kwargs):
        fd = REAL_OPEN(*args, **kwargs)
        self.opened.append(fd)
        self.paths[fd] = str(args[0])
        return fd

    def close(self, fd):
        self.closed.append(fd)
        REAL_CLOSE(fd)


class DescriptorOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-recent-fd-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign root"
        self.root.mkdir()
        (self.root / "campaign.json").write_text(json.dumps(
            {"schema_version": 1, "name": "Synthetic", "county": "San Luis Obispo", "state": "CA"}), encoding="utf-8")
        meetings = self.root / "content" / "meetings"
        meetings.mkdir(parents=True)
        (meetings / "good.json").write_text(json.dumps(
            {"id": "good", "body": "Synthetic Council", "starts_at": "2026-11-03T18:00:00-08:00"}), encoding="utf-8")
        self.sources = self.root / "content" / "sources.json"
        self.sources.write_text("[]", encoding="utf-8")
        self.tracker = Tracker()
        for name, replacement in (("open", self.tracker.open), ("close", self.tracker.close)):
            patch = mock.patch.object(recent.os, name, replacement)
            patch.start()
            self.addCleanup(patch.stop)

    def assert_all_closed_once(self, count=1):
        self.assertEqual(len(self.tracker.opened), count)
        self.assertEqual(self.tracker.closed, self.tracker.opened)
        for fd in self.tracker.opened:
            self.assertFalse(is_open(fd))

    def failing_fdopen(self, error, wrap=False):
        def fdopen(fd, *args, **kwargs):
            self.assertIs(kwargs.get("closefd"), False)
            if wrap:
                REAL_FDOPEN(fd, *args, **kwargs).close()
                self.assertTrue(is_open(fd))
            raise error
        return mock.patch.object(recent.os, "fdopen", fdopen)

    def test_store_constructor_failure_releases_raw_descriptor(self):
        before = self.sources.stat()
        for error in (OSError(errno.EIO, "boom"), RuntimeError("boom"), KeyboardInterrupt()):
            for wrap in (False, True):
                self.tracker.opened.clear()
                self.tracker.closed.clear()
                with self.failing_fdopen(error, wrap), self.assertRaises(type(error)) as caught:
                    recent._store(self.root, "content/sources.json")
                self.assertIs(caught.exception, error)
                self.assert_all_closed_once()
        after = self.sources.stat()
        self.assertEqual((before.st_mtime_ns, before.st_mode), (after.st_mtime_ns, after.st_mode))

    def test_markdown_constructor_failure_closes_fd_without_path_rollback(self):
        target = self.tmp / "out.md"
        error = OSError(errno.EIO, "boom")
        for wrap in (False, True):
            target.unlink(missing_ok=True)
            self.tracker.opened.clear()
            self.tracker.closed.clear()
            with self.failing_fdopen(error, wrap), self.assertRaises(OSError) as caught:
                recent._write_new(target, "text")
            self.assertIs(caught.exception, error)
            self.assert_all_closed_once()
            self.assertEqual(target.read_bytes(), b"")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        with self.assertRaises(recent.UsageError):
            recent._write_new(target, "again")
        fresh = self.tmp / "fresh.md"
        recent._write_new(fresh, "ok\n")
        self.assertEqual(fresh.read_bytes(), b"ok\n")

    def test_failed_acquisition_closes_nothing(self):
        existing = self.tmp / "exists.md"
        existing.write_text("keep", encoding="utf-8")
        with self.assertRaises(recent.UsageError):
            recent._write_new(existing, "new")
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
        self.assertEqual((self.tracker.opened, self.tracker.closed), ([], []))

    def test_success_closes_once_after_stream_and_bytes_unchanged(self):
        self.assertEqual(recent._store(self.root, "content/sources.json"), [])
        target = self.tmp / "ok.md"
        recent._write_new(target, "héllo\n")
        self.assertEqual(target.read_bytes(), "héllo\n".encode())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assert_all_closed_once(2)

    def test_body_failure_closes_once(self):
        with mock.patch.object(recent.os, "fchmod", side_effect=OSError(errno.EIO, "chmod")):
            with self.assertRaises(OSError):
                recent._write_new(self.tmp / "b.md", "x")
        self.assert_all_closed_once()
        self.tracker.opened.clear()
        self.tracker.closed.clear()
        (self.root / "content" / "big.json").write_bytes(b"[]")
        with self.assertRaises(ValueError):
            recent._store(self.root, "content/big.json", limit=1)  # refused before open
        self.assertEqual(self.tracker.opened, [])
        with mock.patch.object(recent.os, "fstat", return_value=os.stat(self.tmp)):
            with self.assertRaises(ValueError):
                recent._store(self.root, "content/sources.json")
        self.assert_all_closed_once()

    def test_close_error_preserves_primary_and_never_retries(self):
        def close_then_eio(fd):
            self.tracker.close(fd)
            raise OSError(errno.EIO, "close")
        primary = RuntimeError("primary")
        with mock.patch.object(recent.os, "close", close_then_eio), self.failing_fdopen(primary):
            with self.assertRaises(RuntimeError) as caught:
                recent._store(self.root, "content/sources.json")
        self.assertIs(caught.exception, primary)
        self.assert_all_closed_once()
        self.tracker.opened.clear()
        self.tracker.closed.clear()
        with mock.patch.object(recent.os, "close", close_then_eio):
            with self.assertRaises(OSError) as caught:
                recent._store(self.root, "content/sources.json")
        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertEqual(len(self.tracker.closed), 1)

    def test_report_continues_after_source_stream_failure(self):
        for _ in range(3):
            self.tracker.opened.clear()
            self.tracker.closed.clear()
            out, err = io.StringIO(), io.StringIO()
            real = recent.os.fdopen

            def fdopen(fd, mode, *args, **kwargs):
                if self.tracker.paths.get(fd, "").endswith("sources.json"):
                    raise OSError(errno.EIO, "CANARY-private-error")
                return real(fd, mode, *args, **kwargs)
            with mock.patch.object(recent.os, "fdopen", fdopen):
                code = recent.run_cli(self.root, "2026-11-01", today=TODAY, out=out, err=err)
            self.assertEqual(code, 3)
            text = out.getvalue() + err.getvalue()
            self.assertEqual(text.count("content/sources.json unreadable"), 1)
            self.assertIn("Synthetic Council", text)
            self.assertNotIn("CANARY", text)
            self.assertTrue(self.tracker.opened)
            for fd in self.tracker.opened:
                self.assertIn(fd, self.tracker.closed)
                self.assertFalse(is_open(fd))


if __name__ == "__main__":
    unittest.main()
