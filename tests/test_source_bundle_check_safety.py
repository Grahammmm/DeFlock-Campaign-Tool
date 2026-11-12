"""RF0440-2: check_source_bundle reads only the bounded, descriptor-verified regular file it inspected (synthetic files)."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import source_bundle
from campaign_tool.content import ContentError
from campaign_tool.source_bundle import DIFFERS, check_source_bundle
from tests import test_source_bundle as fixtures


class CheckReadSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-check-safety-")
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.public = self.dir / "public"
        (self.public / "data").mkdir(parents=True)
        self.target = self.public / "data" / "sources.zip"
        self.target.write_bytes(b"x")
        self.outside = self.dir / "outside.bin"
        self.outside.write_bytes(b"x")  # same bytes as expected

    def at_open(self, action):
        """Run ``action`` right before the real descriptor open of the checked file (after inspection)."""
        real_open, target = os.open, str(self.target)

        def wrapper(path, flags, *args, **kwargs):
            if str(path) == target:
                action()
            return real_open(path, flags, *args, **kwargs)
        return mock.patch("os.open", wrapper)

    def assert_differs(self, expected=b"x"):
        with self.assertRaises(ContentError) as caught:
            check_source_bundle(self.public, expected)
        self.assertEqual(str(caught.exception), DIFFERS)
        self.assertIsNone(caught.exception.__cause__)

    def test_healthy_and_absent_semantics_unchanged(self):
        self.assertIsNone(check_source_bundle(self.public, b"x"))
        self.target.write_bytes(b"")
        self.assertIsNone(check_source_bundle(self.public, b""))
        self.assert_differs(b"x")
        self.assertIsNone(check_source_bundle(self.dir / "absent", b"x"))
        self.target.unlink()
        self.assert_differs(b"x")
        self.assertIsNone(check_source_bundle(self.public, None))

    def test_late_symlink_is_refused_even_when_target_matches(self):
        for destination in (self.outside, self.dir / "dangling"):
            def action(destination=destination):
                os.rename(self.target, self.dir / "parked")
                os.symlink(destination, self.target)
            with self.subTest(destination=destination.name), self.at_open(action):
                self.assert_differs()
            self.target.unlink()
            os.rename(self.dir / "parked", self.target)

    def test_equal_size_different_inode_is_refused_before_read(self):
        def action():
            os.rename(self.target, self.dir / "parked")
            self.target.write_bytes(b"x")
        reads = []
        real_read = os.read
        with self.at_open(action), mock.patch("os.read", lambda fd, n: reads.append(n) or real_read(fd, n)):
            self.assert_differs()
        self.assertEqual(reads, [])

    def test_fifo_arrival_is_opened_nonblocking_and_refused(self):
        flags = []

        def action():
            os.rename(self.target, self.dir / "parked")
            os.mkfifo(self.target)
        real_open, target = os.open, str(self.target)

        def wrapper(path, flag, *args, **kwargs):
            if str(path) == target:
                action()
                flags.append(flag)
                self.assertTrue(flag & os.O_NONBLOCK and flag & os.O_NOFOLLOW)
            return real_open(path, flag, *args, **kwargs)
        with mock.patch("os.open", wrapper):
            self.assert_differs()
        self.assertEqual(len(flags), 1)

    def test_growth_is_bounded_to_expected_plus_one(self):
        requested, returned = [], []
        real_read = os.read

        def grow_then_read(fd, n):
            if not requested:
                with open(self.target, "ab") as handle:
                    handle.write(b"y" * 65536)
            requested.append(n)
            data = real_read(fd, n)
            returned.append(len(data))
            return data
        with mock.patch("os.read", grow_then_read):
            self.assert_differs()
        self.assertLessEqual(max(requested), 2)
        self.assertLessEqual(sum(returned), 2)

    def test_shrink_wrong_bytes_and_wrong_size_are_refused(self):
        self.target.write_bytes(b"y")
        self.assert_differs()
        self.assert_differs(b"xx")
        self.assert_differs(b"")

    def test_failures_are_fixed_and_descriptor_is_closed(self):
        closed, opened = [], []
        real_open, real_close = os.open, os.close
        target = str(self.target)

        def tracking_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if str(path) == target:
                opened.append(fd)
            return fd

        def tracking_close(fd):
            closed.append(fd)
            return real_close(fd)
        for name in ("read", "fstat"):
            opened.clear()
            closed.clear()
            with self.subTest(name=name), mock.patch("os.open", tracking_open), mock.patch("os.close", tracking_close), \
                    mock.patch("os." + name, side_effect=OSError("SECRET-PATH-SENTINEL")):
                self.assert_differs()
            self.assertEqual(opened, closed)
        real_lstat = os.lstat

        def failing(path, *args, **kwargs):
            if str(path) == target:
                raise OSError("SECRET-PATH-SENTINEL")
            return real_lstat(path, *args, **kwargs)
        with mock.patch("os.lstat", failing):
            self.assert_differs()

    def test_missing_nofollow_support_refuses(self):
        with mock.patch.object(os, "O_NOFOLLOW", 0, create=True):
            self.assert_differs()

    def test_direct_read_copy_keeps_its_own_diagnostic(self):
        with self.assertRaises(ContentError) as caught:
            source_bundle.read_copy(self.target, 5)
        self.assertEqual(str(caught.exception), source_bundle.CHANGED)


class BuildCheckTests(fixtures.Fixture):
    def test_build_check_refuses_before_rebuild_after_late_substitution(self):
        fixtures.run_build(self.root)
        archive = self.public / "data" / "sources.zip"
        good = archive.read_bytes()
        outside = self.root / "outside.zip"
        outside.write_bytes(good)  # a link target with identical bytes
        real_check, calls = source_bundle.check_source_bundle, []

        def racing(public, expected):
            calls.append(1)
            real_open, target = os.open, str(archive)

            def wrapper(path, flags, *args, **kwargs):
                if str(path) == target:
                    os.rename(archive, self.root / "parked.zip")
                    os.symlink(outside, archive)
                return real_open(path, flags, *args, **kwargs)
            with mock.patch("os.open", wrapper):
                return real_check(public, expected)
        with mock.patch.object(source_bundle, "check_source_bundle", racing), \
                mock.patch("campaign_tool.site.build_site", side_effect=AssertionError("rebuilt")):
            with self.assertRaisesRegex(ContentError, "differs from rebuilt public-source bundle"):
                fixtures.run_build(self.root, check=True)
        self.assertEqual(calls, [1])
        self.assertTrue(stat.S_ISLNK(os.lstat(archive).st_mode))
        os.unlink(archive)
        os.rename(self.root / "parked.zip", archive)
        self.assertEqual(fixtures.run_build(self.root, check=True), "")  # healthy control


if __name__ == "__main__":
    unittest.main()
