"""RF2315-2: source_bundle.read_copy reads only the regular file it inspected (synthetic files, no network)."""
import os
import shutil
import tempfile
import unittest
import zipfile
import io
from pathlib import Path
from unittest import mock

from campaign_tool import source_bundle
from campaign_tool.content import ContentError
from campaign_tool.source_bundle import CHANGED, plan_bundle, read_copy, rebuild_from_staged
from tests.test_source_bundle import BASE, Fixture, entry, run_build


class ReadCopyBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-read-safety-")
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.target = self.dir / "a.pdf"
        self.target.write_bytes(b"PUBLIC-COPY")
        self.outside = self.dir / "outside.bin"
        self.outside.write_bytes(b"PRIVATE-XYZ")  # same length as the public copy

    def swap_at_open(self, action, calls=None):
        """Run ``action`` right before the real open of ``self.target`` (after the inspection); record open flags and reads."""
        real_open, target = os.open, str(self.target)

        def wrapper(path, flags, *args, **kwargs):
            if str(path) == target:
                if calls is not None:
                    calls.append(flags)
                action()
            return real_open(path, flags, *args, **kwargs)
        return mock.patch("os.open", wrapper)

    def assert_refused(self, call):
        with self.assertRaises(ContentError) as caught:
            call()
        self.assertEqual(str(caught.exception), CHANGED)
        self.assertIsNone(caught.exception.__cause__)


class ReadCopyRaceTests(ReadCopyBase):
    def test_ordinary_read_and_bounds(self):
        self.assertEqual(read_copy(self.target, 11), b"PUBLIC-COPY")
        empty = self.dir / "empty"
        empty.write_bytes(b"")
        self.assertEqual(read_copy(empty, 0), b"")
        self.assert_refused(lambda: read_copy(self.target, 10))
        self.assert_refused(lambda: read_copy(self.target, 12))

    def test_symlink_installed_after_inspection(self):
        for destination in (self.outside, self.dir / "dangling", self.target.with_name("a.moved")):
            def action(destination=destination):
                os.rename(self.target, self.target.with_name("a.moved")) if destination.name != "a.moved" else os.rename(self.target, destination)
                os.symlink(destination, self.target)
            with self.subTest(destination=destination.name), self.swap_at_open(action), \
                    mock.patch("os.read", side_effect=AssertionError("payload read")):
                self.assert_refused(lambda: read_copy(self.target, 11))
            os.unlink(self.target)
            self.target.with_name("a.moved").rename(self.target)

    def test_same_size_inode_replacement_before_open(self):
        def action():
            os.rename(self.target, self.dir / "original")
            os.rename(self.outside, self.target)
        with self.swap_at_open(action), mock.patch("os.read", side_effect=AssertionError("payload read")):
            self.assert_refused(lambda: read_copy(self.target, 11))

    def test_fifo_substitution_uses_nonblocking_open_and_refuses_before_read(self):
        flags = []

        def action():
            os.unlink(self.target)
            os.mkfifo(self.target)
        with self.swap_at_open(action, flags), mock.patch("os.read", side_effect=AssertionError("payload read")):
            self.assert_refused(lambda: read_copy(self.target, 11))
        self.assertTrue(flags[0] & os.O_NOFOLLOW and flags[0] & os.O_NONBLOCK)
        fifo, directory, link = self.dir / "fifo", self.dir / "dir", self.dir / "link"
        os.mkfifo(fifo)
        directory.mkdir()
        os.symlink(self.target, link)
        for path in (fifo, directory, link):
            self.assert_refused(lambda path=path: read_copy(path, 11))

    def test_post_read_size_change_and_bounded_request(self):
        requests = []
        real_read = os.read

        def grow(fd, count):
            requests.append(count)
            data = real_read(fd, count)
            with open(self.target, "ab") as handle:
                handle.write(b"+")
            return data
        with mock.patch("os.read", grow):
            self.assert_refused(lambda: read_copy(self.target, 11))
        self.assertEqual(requests, [12, 1])  # never asked for more than expected_size + 1; the loop ends on the short read
        self.assertTrue(max(requests) <= 12)

    def test_errors_close_owned_descriptor_once_and_leak_nothing(self):
        for phase in ("lstat", "open", "fstat", "read"):
            closed, opened = [], []
            real_open, real_close = os.open, os.close

            def tracking_open(path, flags, *a, **k):
                fd = real_open(path, flags, *a, **k)
                opened.append(fd)
                return fd
            patches = {"lstat": mock.patch("os.lstat", side_effect=OSError("SECRET-OS-TEXT /sentinel")),
                       "open": mock.patch("os.open", side_effect=OSError("SECRET-OS-TEXT /sentinel")),
                       "fstat": mock.patch("os.fstat", side_effect=OSError("SECRET-OS-TEXT /sentinel")),
                       "read": mock.patch("os.read", side_effect=OSError("SECRET-OS-TEXT /sentinel"))}
            with self.subTest(phase=phase), patches[phase] if phase in ("lstat", "open") else mock.patch("os.open", tracking_open):
                with mock.patch("os.close", lambda fd: (closed.append(fd), real_close(fd))[1]):
                    if phase in ("fstat", "read"):
                        with patches[phase]:
                            self.assert_refused(lambda: read_copy(self.target, 11))
                    else:
                        self.assert_refused(lambda: read_copy(self.target, 11))
                self.assertEqual(closed, opened if phase in ("fstat", "read") else [])
                if phase in ("fstat", "read"):
                    self.assertEqual(len(closed), 1)

    def test_missing_safe_open_flags_fail_closed(self):
        with mock.patch.object(os, "O_NOFOLLOW", 0, create=True), mock.patch("os.open", side_effect=AssertionError("opened")):
            self.assert_refused(lambda: read_copy(self.target, 11))


class PlanAndBuildTests(Fixture):
    def planned(self, root=None):
        return [(self.content / "public-assets" / "docs" / "a.pdf", Path("docs/a.pdf"))]

    def race(self, victim, kind):
        real_open, target = os.open, str(victim)

        def wrapper(path, flags, *a, **k):
            if str(path) == target:
                secret = victim.with_name("secret.bin")
                secret.write_bytes(b"P" * victim.stat().st_size)
                os.rename(victim, victim.with_name("aside"))
                if kind == "symlink":
                    os.symlink(secret, victim)
                else:
                    os.rename(secret, victim)
            return real_open(path, flags, *a, **k)
        return mock.patch("os.open", wrapper)

    def sources(self):
        return [entry(1, BASE + "/docs/a.pdf", "Alpha")]

    def test_plan_rejects_late_substitution_and_healthy_control(self):
        victim = self.planned()[0][0]
        healthy = plan_bundle(self.sources(), BASE, self.planned(), {})
        self.assertIsNotNone(healthy)
        with zipfile.ZipFile(io.BytesIO(healthy)) as archive:
            self.assertEqual(archive.read("files/docs/a.pdf"), victim.read_bytes())
        for kind in ("symlink", "inode"):
            with self.subTest(kind=kind), self.race(victim, kind):
                with self.assertRaises(ContentError) as caught:
                    plan_bundle(self.sources(), BASE, self.planned(), {})
                self.assertEqual(str(caught.exception), CHANGED)
                self.assertNotIn("secret", str(caught.exception))
            aside = victim.with_name("aside")
            if os.path.lexists(victim):
                os.unlink(victim)
            aside.rename(victim)
            victim.with_name("secret.bin").unlink(missing_ok=True)

    def test_staged_rebuild_uses_same_safe_reader(self):
        victim = self.planned()[0][0]
        staged = self.dir_staged = Path(self.public.parent) / "staged"
        (staged / "docs").mkdir(parents=True)
        copy = staged / "docs" / "a.pdf"
        shutil.copyfile(victim, copy)
        expected = plan_bundle(self.sources(), BASE, self.planned(), {})
        rebuild_from_staged(self.sources(), BASE, ["docs/a.pdf"], staged, {}, expected)  # healthy control
        for kind in ("symlink", "inode"):
            with self.subTest(kind=kind), self.race(copy, kind):
                with self.assertRaises(ContentError) as caught:
                    rebuild_from_staged(self.sources(), BASE, ["docs/a.pdf"], staged, {}, expected)
                self.assertEqual(str(caught.exception), CHANGED)
            if os.path.lexists(copy):
                os.unlink(copy)
            copy.with_name("aside").rename(copy)
            copy.with_name("secret.bin").unlink(missing_ok=True)

    def test_build_reader_refusal_keeps_last_good_public_tree(self):
        run_build(self.root)
        zip_path = self.public / "data" / "sources.zip"
        before = {p.relative_to(self.public): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}
        asset = self.content / "public-assets" / "docs" / "a.pdf"
        # the race fires on the SECOND open of the asset: the first is the initial plan, the second the staged revalidation
        for phase in (1, 2):
            seen = []
            real_open = os.open

            def wrapper(path, flags, *a, **k):
                if Path(path).name == "a.pdf" and flags & os.O_NOFOLLOW:
                    seen.append(str(path))
                    if len(seen) == phase:
                        secret = Path(path).with_name("secret.bin")
                        secret.write_bytes(b"P" * Path(path).stat().st_size)
                        os.rename(path, str(path) + ".aside")
                        os.symlink(secret, path)
                return real_open(path, flags, *a, **k)
            with self.subTest(phase=phase), mock.patch("os.open", wrapper):
                with self.assertRaises(ContentError) as caught:
                    run_build(self.root)
                self.assertEqual(str(caught.exception), CHANGED)
            if phase == 1 or seen:
                pass
            self.assertGreaterEqual(len(seen), phase)  # the race really fired
            after = {p.relative_to(self.public): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}
            self.assertEqual(before, after)
            self.assertTrue(zip_path.is_file())
            for leftover in self.root.glob("*staging*"):
                self.fail(f"staging residue {leftover}")
            if os.path.lexists(asset):
                os.unlink(asset)
            Path(str(asset) + ".aside").rename(asset) if os.path.lexists(str(asset) + ".aside") else None
            asset.with_name("secret.bin").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
