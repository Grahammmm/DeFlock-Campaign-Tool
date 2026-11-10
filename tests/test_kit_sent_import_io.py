"""kit-sent-import: I/O failures after the CSV is open are a fixed, value-free exit-2 refusal. Offline, synthetic."""
import errno
import os
import stat
import unittest
from unittest import mock

from campaign_tool import kit, kit_import_sent
from tests import test_kit_sent_import as base

FIXED = "csv cannot be read; nothing was written"
CANARIES = ("PRIVATE-IO-DETAIL-CANARY", "PRIVATE-CSV-PATH-CANARY")


def eio(path):
    return OSError(errno.EIO, "PRIVATE-IO-DETAIL-CANARY", str(path))


class SentImportIoTests(unittest.TestCase):
    setUp = base.KitSentImportTests.setUp
    csv, do, api, stored = (base.KitSentImportTests.csv, base.KitSentImportTests.do,
                            base.KitSentImportTests.api, base.KitSentImportTests.stored)

    def make(self):
        self.path = self.csv(f"{self.a},,2026-03-02,email,,", f"{self.b},,2026-03-03,email,,",
                             name="PRIVATE-CSV-PATH-CANARY.csv")
        self.before = base.snapshot(self.root)
        self.input_bytes = self.path.read_bytes()

    def forbid_downstream(self):
        for target in ("_agencies",):
            patcher = mock.patch.object(kit_import_sent, target, side_effect=AssertionError("downstream work"))
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("_write_json",):
            patcher = mock.patch.object(kit, target, side_effect=AssertionError("writer called"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def expect_refusal(self):
        with self.assertRaises(kit_import_sent.SentImportError) as raised:
            self.api(self.path, apply=True)
        self.assertEqual(str(raised.exception), FIXED)
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertTrue(raised.exception.__suppress_context__)
        for extra in ((), ("--apply",)):
            code, out, err = self.do(self.path, *extra)
            self.assertEqual((code, out, err), (2, "", f"Stopped: {FIXED}\n"))
        self.assertEqual(base.snapshot(self.root), self.before)
        self.assertEqual(self.path.read_bytes(), self.input_bytes)

    def csv_inode(self):
        return self.path.stat().st_ino

    def test_fstat_error_is_private_exit_two_in_preview_and_apply(self):
        self.make()
        self.forbid_downstream()
        real, closed = os.fstat, []
        real_close = os.close

        def fstat(fd):
            result = real(fd)
            if result.st_ino == self.csv_inode():
                closed.append(fd)
                raise eio(self.path)
            return result

        def close(fd):
            if fd in closed:
                closed.append(("closed", fd))
            return real_close(fd)

        with mock.patch("os.fstat", fstat), mock.patch("os.close", close):
            self.expect_refusal()
        fds = {c for c in closed if not isinstance(c, tuple)}
        self.assertTrue(fds)
        for fd in fds:
            self.assertIn(("closed", fd), closed)

    def test_fdopen_construction_error_closes_raw_owned_fd(self):
        self.make()
        self.forbid_downstream()
        bystander = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(lambda: os.fstat(bystander) and os.close(bystander))
        real_fdopen, real_close, seen, closes = os.fdopen, os.close, [], []

        def fdopen(fd, *args, **kwargs):
            if os.fstat(fd).st_ino == self.csv_inode():
                seen.append(fd)
                raise eio(self.path)
            return real_fdopen(fd, *args, **kwargs)

        def close(fd):
            closes.append(fd)
            return real_close(fd)

        with mock.patch("os.fdopen", fdopen), mock.patch("os.close", close):
            self.expect_refusal()
        self.assertEqual(len(seen), 3)
        for fd in set(seen):
            self.assertEqual(closes.count(fd), seen.count(fd))  # closed exactly once per attempt
        self.assertTrue(stat.S_ISCHR(os.fstat(bystander).st_mode))  # unrelated descriptor untouched

    def test_read_error_closes_transferred_stream_without_double_close(self):
        self.make()
        self.forbid_downstream()
        real_fdopen, real_close = os.fdopen, os.close
        sizes, stream_closes, raw_closes, opened = [], [], [], []

        class Wrapper:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                return self

            def read(self, size=-1):
                sizes.append(size)
                raise eio(base_path[0])

            def __exit__(self, *exc):
                stream_closes.append(1)
                return self.inner.__exit__(*exc)

        base_path = [self.path]

        def fdopen(fd, *args, **kwargs):
            inner = real_fdopen(fd, *args, **kwargs)
            if os.fstat(fd).st_ino == self.csv_inode():
                opened.append(fd)
                return Wrapper(inner)
            return inner

        def close(fd):
            raw_closes.append(fd)
            return real_close(fd)

        real_open = os.open
        reopened = []

        def spy_open(path, *args, **kwargs):
            reopened.append(str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch("os.fdopen", fdopen), mock.patch("os.close", close), mock.patch("os.open", spy_open):
            self.expect_refusal()
        self.assertEqual(set(sizes), {kit_import_sent.MAX_BYTES + 1})
        self.assertEqual(len(stream_closes), len(sizes))
        self.assertFalse([fd for fd in raw_closes if fd in opened])  # the stream, not the raw fd, closed it
        self.assertEqual(reopened.count(str(self.path)), 3)  # one open per attempt, no reopen

    def wrap_stream(self, behaviour):
        real_fdopen = os.fdopen

        class Wrapper:
            def __init__(self, inner):
                self.inner = inner
                self.data = None

            def __enter__(self):
                return self

            def read(self, size=-1):
                self.data = self.inner.read(size)
                if behaviour == "partial":
                    raise eio(base_path)
                return self.data

            def __exit__(self, *exc):
                self.inner.close()
                if behaviour == "close":
                    raise eio(base_path)
                return False

        base_path = self.path

        def fdopen(fd, *args, **kwargs):
            inner = real_fdopen(fd, *args, **kwargs)
            return Wrapper(inner) if os.fstat(fd).st_ino == self.csv_inode() else inner

        return mock.patch("os.fdopen", fdopen)

    def test_partial_read_then_error_has_no_imported_prefix(self):
        for existing in (False, True):
            self.make()
            if existing:
                self.assertEqual(self.do(self.csv(f"{self.c},,2026-03-04,email,,", name="seed.csv"), "--apply")[0], 0)
                self.before = base.snapshot(self.root)
            else:
                self.assertFalse(self.sent.exists())
            self.forbid_downstream()
            with self.wrap_stream("partial"):
                self.expect_refusal()
            self.assertEqual(self.sent.exists(), existing)
            mock.patch.stopall()
            base.KitSentImportTests.setUp(self)  # fresh campaign for the other case

    def test_close_error_after_complete_read_is_refusal(self):
        self.make()
        self.forbid_downstream()
        with self.wrap_stream("close"):
            self.expect_refusal()

    def test_raw_cleanup_error_is_refusal(self):
        self.make()
        self.forbid_downstream()
        real_fstat, real_close = os.fstat, os.close

        def fstat(fd):
            result = real_fstat(fd)
            if result.st_ino == self.csv_inode():
                real_close(fd)  # the seam closes the real fd, then both fstat and the cleanup close fail
                raise eio(self.path)
            return result

        with mock.patch("os.fstat", fstat):
            self.expect_refusal()

    def test_input_io_refusal_precedes_downstream_work(self):
        self.make()
        self.forbid_downstream()
        for name in ("read_sent",):
            if hasattr(kit_import_sent.kit_track, name):
                patcher = mock.patch.object(kit_import_sent.kit_track, name,
                                            side_effect=AssertionError("store read"))
                patcher.start()
                self.addCleanup(patcher.stop)
        with self.wrap_stream("partial"):
            self.expect_refusal()

    def test_programmer_errors_are_not_swallowed(self):
        self.make()
        with mock.patch("os.fstat", side_effect=AssertionError("bug")):
            with self.assertRaises(AssertionError):
                self.api(self.path)

    def test_success_and_existing_validation_contracts_are_unchanged(self):
        for body, name in ((b"\xef\xbb\xbf", "bom"), (b"", "plain")):
            for eol in (b"\n", b"\r\n"):
                for final in (True, False):
                    rows = eol.join([base.HEADER.encode(), f"{self.a},,2026-03-02,email,,".encode()])
                    path = self.tmp / f"ok-{name}-{len(eol)}-{final}.csv"
                    path.write_bytes(body + rows + (eol if final else b""))
                    result = self.api(path)
                    self.assertEqual(result["checked"], 1)
                    self.assertFalse(result["written"])
        path = self.csv(f"{self.a},,2026-03-02,email,,")
        calls = []
        real = kit._write_json
        with mock.patch.object(kit, "_write_json", side_effect=lambda *a, **k: calls.append(1) or real(*a, **k)):
            self.assertEqual(self.do(path, "--apply")[0], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(stat.S_IMODE(self.sent.stat().st_mode), 0o600)
        self.assertIn(self.a, self.stored())
        target = self.tmp / "target.csv"
        target.write_text("x")
        link = self.tmp / "link.csv"
        link.symlink_to(target)
        with self.assertRaisesRegex(kit_import_sent.SentImportError, "symlinks are refused"):
            self.api(link)
        with self.assertRaisesRegex(kit_import_sent.SentImportError, "regular file"):
            self.api(self.tmp)
        big = self.tmp / "big.csv"
        big.write_bytes(b"a" * (kit_import_sent.MAX_BYTES + 1))
        with self.assertRaisesRegex(kit_import_sent.SentImportError, "larger than 512 KiB"):
            self.api(big)


if __name__ == "__main__":
    unittest.main()
