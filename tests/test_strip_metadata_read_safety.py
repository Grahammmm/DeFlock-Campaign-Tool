"""RF1315-3: strip-metadata reads only a bounded regular-file source and closes every source descriptor it acquires.

Synthetic bytes, real FIFOs and real file descriptors only. A FIFO is never read: the test keeps its own writer
end open (O_RDWR never blocks) so nothing here can wait on a reader or a writer.
"""
import errno
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import image_meta
from campaign_tool.image_meta import MAX_BYTES, UNREADABLE, Unreadable, strip_file
from tests.test_image_meta import GPS_VALUE, JPEG_META, jpeg, png, segment, webp
from tests.test_strip_metadata_atomic import snapshot

REPO_ROOT = Path(__file__).resolve().parents[1]
DIRTY = jpeg(JPEG_META["exif"], JPEG_META["com"])
CLEAN = jpeg()


def fd_open(fd):
    try:
        os.fstat(fd)
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return False
        raise
    return True


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-strip-read-")
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.source = self.dir / "photo.jpg"
        self.source.write_bytes(DIRTY)
        self.source.chmod(0o640)
        self.out = self.dir / "out" / "copy.jpg"
        self.out.parent.mkdir()

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content", "strip-metadata", *args],
                              cwd=REPO_ROOT, capture_output=True, timeout=30)

    def fifo(self, name="feed.jpg"):
        path = self.dir / name
        os.mkfifo(path)
        keeper = os.open(path, os.O_RDWR | os.O_NONBLOCK)  # test-owned end: never blocks, keeps a "writer" present
        self.addCleanup(os.close, keeper)
        return path, keeper

    def assert_refused_without_output(self, source):
        before = snapshot(self.out.parent)
        with self.assertRaises(Unreadable) as caught:
            strip_file(source, self.out)
        self.assertEqual(str(caught.exception), UNREADABLE)
        self.assertEqual(snapshot(self.out.parent), before)

    def assert_healthy_retry(self):
        self.assertEqual(strip_file(self.source, self.out), {"removed": ["EXIF GPS", "comment"], "written": str(self.out)})
        self.assertEqual(self.out.read_bytes(), CLEAN)

    def opened_fds(self, source):
        """Wrap os.open; record the real descriptors returned for ``source``."""
        real, seen = os.open, []

        def wrapper(path, *args, **kwargs):
            fd = real(path, *args, **kwargs)
            if os.fspath(path) == os.fspath(source):
                seen.append(fd)
            return fd
        return wrapper, seen


class SpecialFileTests(Base):
    def test_writerless_fifo_is_prompt_refusal_before_payload_or_output(self):
        path = self.dir / "feed.jpg"
        os.mkfifo(path)  # no writer exists; before RF1315-3 the open blocked (the child timeout is a test failure)
        before = sorted(os.listdir(self.dir))  # not snapshot(): reading the FIFO would block the test itself
        result = self.cli(str(path), "--out", str(self.out))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"Stopped:", result.stderr)
        self.assertIn(UNREADABLE.encode(), result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertEqual(sorted(os.listdir(self.dir)), before)
        self.assertFalse(os.path.lexists(self.out))
        self.assertTrue(stat.S_ISFIFO(os.lstat(path).st_mode))

    def test_fifo_with_valid_image_bytes_is_not_an_image_file(self):
        path, keeper = self.fifo()
        os.write(keeper, DIRTY)
        with mock.patch.object(image_meta, "metadata_kinds", side_effect=AssertionError("parsed")):
            self.assert_refused_without_output(path)
        os.set_blocking(keeper, False)
        self.assertEqual(os.read(keeper, len(DIRTY) + 1), DIRTY)  # no payload byte was consumed
        self.assertEqual(os.listdir(self.out.parent), [])

    def test_directory_source_is_refused_without_output(self):
        directory = self.dir / "folder.jpg"
        directory.mkdir()
        self.assert_refused_without_output(directory)

    def test_descriptor_type_gate_survives_leaf_replacement(self):
        real = os.open
        for kind in ("fifo", "directory"):
            with self.subTest(kind=kind):
                swapped = []

                def swap_then_open(path, *args, **kwargs):
                    if os.fspath(path) == os.fspath(self.source) and not swapped:
                        swapped.append(1)
                        self.source.unlink()
                        if kind == "fifo":
                            os.mkfifo(self.source)
                        else:
                            self.source.mkdir()
                    return real(path, *args, **kwargs)
                with mock.patch.object(image_meta.os, "open", swap_then_open):
                    self.assert_refused_without_output(self.source)
                self.assertEqual(swapped, [1])
                if kind == "fifo":
                    self.source.unlink()
                else:
                    self.source.rmdir()
                self.source.write_bytes(DIRTY)

    def test_symlink_replacement_at_acquisition_is_refused_and_target_unread(self):
        external = self.dir / "external.jpg"
        external.write_bytes(DIRTY)
        real = os.open

        def swap_then_open(path, *args, **kwargs):
            if os.fspath(path) == os.fspath(self.source):
                self.source.unlink()
                self.source.symlink_to(external)
            return real(path, *args, **kwargs)
        before = snapshot(self.out.parent)
        with mock.patch.object(image_meta.os, "open", swap_then_open):
            with self.assertRaises(OSError) as caught:
                strip_file(self.source, self.out)
        self.assertEqual(caught.exception.errno, errno.ELOOP)
        self.assertEqual(external.read_bytes(), DIRTY)
        self.assertEqual(snapshot(self.out.parent), before)

    def test_open_inode_is_not_redirected_by_later_path_replacement(self):
        other = self.dir / "other.jpg"
        other.write_bytes(jpeg(JPEG_META["xmp"]))
        real_fstat, replaced = os.fstat, []

        def replace_then_fstat(fd, *args, **kwargs):
            if not replaced:
                replaced.append(1)
                os.replace(other, self.source)  # the pathname now names different bytes
            return real_fstat(fd, *args, **kwargs)
        with mock.patch.object(image_meta.os, "fstat", replace_then_fstat):
            self.assertEqual(strip_file(self.source, self.out),
                             {"removed": ["EXIF GPS", "comment"], "written": str(self.out)})
        self.assertEqual(self.out.read_bytes(), CLEAN)  # original inode's bytes, never the replacement's


class DescriptorOwnershipTests(Base):
    def run_with(self, patches):
        wrapper, seen = self.opened_fds(self.source)
        sentinel = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(lambda: fd_open(sentinel) and os.close(sentinel))
        with mock.patch.object(image_meta.os, "open", wrapper), patches():
            try:
                strip_file(self.source, self.out)
            except BaseException as exc:
                raised = exc
            else:
                raised = None
        self.assertEqual(len(seen), 1)
        self.assertFalse(fd_open(seen[0]), "source descriptor leaked")
        self.assertTrue(fd_open(sentinel), "an unrelated descriptor was closed")
        return raised

    def test_success_closes_the_source_stream(self):
        self.assertIsNone(self.run_with(mock.MagicMock))
        self.assertEqual(self.out.read_bytes(), CLEAN)

    def test_raw_descriptor_is_closed_when_construction_fails(self):
        for failure in (OSError(errno.EMFILE, "synthetic"), MemoryError(), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                raised = self.run_with(lambda: mock.patch.object(image_meta.os, "fdopen", side_effect=failure))
                self.assertIs(raised, failure)
                self.assertFalse(os.path.lexists(self.out))

    def test_raw_descriptor_is_closed_when_fstat_fails(self):
        failure = OSError(errno.EIO, "synthetic fstat failure")
        raised = self.run_with(lambda: mock.patch.object(image_meta.os, "fstat", side_effect=failure))
        self.assertIs(raised, failure)
        self.assertFalse(os.path.lexists(self.out))

    def test_descriptor_is_closed_when_type_or_size_gate_refuses(self):
        self.source.unlink()
        self.source.mkdir()
        self.assertIsInstance(self.run_with(mock.MagicMock), Unreadable)
        self.source.rmdir()
        self.source.write_bytes(b"")
        os.truncate(self.source, MAX_BYTES + 1)  # sparse, over the configured cap
        self.assertIsInstance(self.run_with(mock.MagicMock), Unreadable)

    def test_read_failure_closes_the_stream(self):
        real_fdopen, streams = os.fdopen, []
        failure = OSError(errno.EIO, "synthetic read failure")

        def fdopen(fd, *args, **kwargs):
            stream = real_fdopen(fd, *args, **kwargs)
            streams.append(stream)
            stream.read = mock.Mock(side_effect=failure)
            return stream
        raised = self.run_with(lambda: mock.patch.object(image_meta.os, "fdopen", fdopen))
        self.assertIs(raised, failure)
        self.assertTrue(streams[0].closed)
        self.assertFalse(os.path.lexists(self.out))


def padded(extra):
    """A valid JPEG of exactly ``len(jpeg()) + extra`` bytes: the surplus is COM segments (a metadata kind)."""
    segments, left = [], extra
    while left:
        size = min(left, 65537)
        if 0 < left - size < 4:  # never leave a remainder too small to be a segment
            size -= 4
        segments.append(segment(0xFE, b"x" * (size - 4)))
        left -= size
    return jpeg(*segments)


class ByteBoundTests(Base):
    def test_descriptor_size_over_cap_is_refused_before_a_stream_is_built(self):
        self.source.write_bytes(b"")
        os.truncate(self.source, MAX_BYTES + 1)
        with mock.patch.object(image_meta.os, "fdopen", side_effect=AssertionError("stream built")):
            self.assert_refused_without_output(self.source)

    def test_file_that_grows_after_fstat_is_refused_on_the_actual_length(self):
        real_fstat, real_fdopen, reads = os.fstat, os.fdopen, []

        def grow_after_fstat(fd, *args, **kwargs):
            info = real_fstat(fd, *args, **kwargs)
            os.truncate(self.source, MAX_BYTES + 10)
            return info

        def recording_fdopen(fd, *args, **kwargs):
            stream = real_fdopen(fd, *args, **kwargs)
            real_read = stream.read
            stream.read = lambda n=-1: reads.append(n) or real_read(n)
            return stream
        before = snapshot(self.out.parent)
        with mock.patch.object(image_meta.os, "fstat", grow_after_fstat), \
                mock.patch.object(image_meta.os, "fdopen", recording_fdopen), \
                mock.patch.object(image_meta, "metadata_kinds", side_effect=AssertionError("parsed")), \
                mock.patch.object(image_meta, "strip", side_effect=AssertionError("parsed")):
            with self.assertRaises(Unreadable):
                strip_file(self.source, self.out)
        self.assertEqual(reads, [MAX_BYTES + 1])
        self.assertEqual(snapshot(self.out.parent), before)

    def test_exactly_the_cap_is_accepted_and_one_more_byte_is_refused(self):
        exact = padded(MAX_BYTES - len(jpeg()))
        self.assertEqual(len(exact), MAX_BYTES)
        self.source.write_bytes(exact)
        self.assertEqual(strip_file(self.source, self.out)["removed"], ["comment"])
        self.assertEqual(self.out.read_bytes(), CLEAN)
        over = padded(MAX_BYTES + 1 - len(jpeg()))
        self.assertEqual(len(over), MAX_BYTES + 1)
        self.source.write_bytes(over)
        self.out.unlink()
        self.assert_refused_without_output(self.source)

    def test_malformed_small_image_keeps_the_unreadable_error(self):
        self.source.write_bytes(jpeg()[:-2])
        self.assert_refused_without_output(self.source)


class OutputTransactionUntouchedTests(Base):
    def test_source_failure_never_starts_the_existing_output_transaction(self):
        path, keeper = self.fifo()
        directory = self.dir / "folder.jpg"
        directory.mkdir()
        big = self.dir / "big.jpg"
        big.write_bytes(b"")
        os.truncate(big, MAX_BYTES + 1)
        sentinel = self.out.parent / "sentinel.txt"
        sentinel.write_bytes(b"organizer file")
        before = snapshot(self.out.parent)
        mode = self.source.stat().st_mode
        with mock.patch.object(image_meta.tempfile, "TemporaryDirectory", side_effect=AssertionError("staged")), \
                mock.patch.object(image_meta.os, "link", side_effect=AssertionError("linked")), \
                mock.patch.object(image_meta.os, "write", side_effect=AssertionError("written")):
            for source in (path, directory, big, self.dir / "missing.jpg"):
                with self.subTest(source=source.name):
                    with self.assertRaises((Unreadable, FileNotFoundError)):
                        strip_file(source, self.out)
        self.assertEqual(snapshot(self.out.parent), before)
        self.assertEqual(self.source.read_bytes(), DIRTY)
        self.assertEqual(self.source.stat().st_mode, mode)
        self.assert_healthy_retry()


class CliContractTests(Base):
    def test_regular_formats_keep_the_cli_contract(self):
        cases = {"a.jpg": DIRTY, "b.PNG": png(), "c.webp": webp()}
        for name, data in cases.items():
            with self.subTest(name=name):
                source = self.dir / name
                source.write_bytes(data)
                target = self.out.parent / f"clean-{name}"
                result = self.cli(str(source), "--out", str(target))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(GPS_VALUE, result.stdout)
                self.assertEqual(source.read_bytes(), data)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.cli(str(target), "--out", str(self.out.parent / f"again-{name}")).returncode, 0)

    def test_static_symlink_and_malformed_refusals_are_unchanged(self):
        link = self.dir / "link.jpg"
        link.symlink_to(self.source)
        result = self.cli(str(link), "--out", str(self.out))
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"is a symlink", result.stderr)
        (self.dir / "bad.jpg").write_bytes(jpeg()[:-2])
        result = self.cli(str(self.dir / "bad.jpg"), "--out", str(self.out))
        self.assertEqual(result.returncode, 1)
        self.assertIn(UNREADABLE.encode(), result.stderr)
        self.assertEqual(os.listdir(self.out.parent), [])

    def test_directory_source_is_a_controlled_cli_refusal(self):
        (self.dir / "folder.jpg").mkdir()
        result = self.cli(str(self.dir / "folder.jpg"), "--out", str(self.out))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(UNREADABLE.encode(), result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
