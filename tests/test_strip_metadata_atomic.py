"""RF1925-10: strip-metadata publishes the cleaned copy only after a complete, synced write; a failed write is retryable.

Synthetic bytes only. Failures are injected at the real os-level seams strip_file uses (write, fsync, close, link).
"""
import errno
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import image_meta
from campaign_tool.image_meta import MAX_BYTES, strip_file
from tests.test_image_meta import JPEG_META, GPS_VALUE, chunk, jpeg, png, webp, riff, tiff

REPO_ROOT = Path(__file__).resolve().parents[1]
DIRTY = jpeg(JPEG_META["exif"], JPEG_META["com"])
CLEAN = jpeg()
STORAGE_FULL = OSError(errno.ENOSPC, "synthetic storage full")


def snapshot(directory):
    """Name -> (kind, inode, bytes) for every entry below ``directory`` without following symlinks."""
    found = {}
    for path in sorted(Path(directory).rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink():
            found[relative] = ("link", os.readlink(path))
        elif path.is_dir():
            found[relative] = ("dir", path.stat().st_ino)
        else:
            found[relative] = ("file", path.stat().st_ino, path.read_bytes())
    return found


class StripBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-strip-atomic-")
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.source = self.dir / "photo.jpg"
        self.source.write_bytes(DIRTY)
        self.source.chmod(0o640)
        self.target = self.dir / "copy.jpg"

    def assert_source_untouched(self):
        self.assertEqual(self.source.read_bytes(), DIRTY)
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o640)

    def assert_retry_succeeds(self):
        self.assertFalse(os.path.lexists(self.target))
        result = strip_file(self.source, self.target)
        self.assertEqual(result, {"removed": ["EXIF GPS", "comment"], "written": str(self.target)})
        self.assertEqual(self.target.read_bytes(), CLEAN)
        self.assertNotIn(GPS_VALUE, self.target.read_bytes())


class PreparationFailureTests(StripBase):
    def test_prefix_write_failure_has_no_final_and_retry_succeeds(self):
        real_write = os.write
        state = {"seen": 0}

        def prefix_then_full(fd, data):
            if state["seen"]:
                raise STORAGE_FULL
            state["seen"] = real_write(fd, bytes(data[:11]))
            return state["seen"]
        with mock.patch.object(image_meta.os, "write", side_effect=prefix_then_full):
            with self.assertRaises(OSError) as caught:
                strip_file(self.source, self.target)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(state["seen"], 11)  # the real flushed prefix existed, but never under the final name
        self.assertFalse(os.path.lexists(self.target))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["photo.jpg"])  # only this call's stage, now gone
        self.assert_source_untouched()
        self.assert_retry_succeeds()

    def test_flush_fsync_and_close_failure_do_not_publish(self):
        real_open, real_close = os.open, os.close
        for seam in ("fsync", "close"):
            with self.subTest(seam=seam):
                staged = []
                events = []

                def open_spy(path, *args, **kwargs):
                    fd = real_open(path, *args, **kwargs)
                    if os.path.basename(path) == "payload":
                        staged.append(fd)
                    return fd

                def fsync_fail(fd):
                    events.append("fsync")
                    raise OSError(errno.EIO, "synthetic fsync failure")

                def close_fail(fd):
                    real_close(fd)
                    if fd in staged:
                        staged.remove(fd)  # descriptor numbers are reused by the cleanup's own directory scans
                        events.append("close")
                        raise OSError(errno.EIO, "synthetic close failure")

                patches = [mock.patch.object(image_meta.os, "open", side_effect=open_spy)]
                patches.append(mock.patch.object(image_meta.os, seam, side_effect={"fsync": fsync_fail, "close": close_fail}[seam]))
                with patches[0], patches[1], mock.patch.object(image_meta.os, "link", side_effect=AssertionError("published")):
                    with self.assertRaises(OSError) as caught:
                        strip_file(self.source, self.target)
                self.assertEqual(caught.exception.errno, errno.EIO)
                self.assertFalse(os.path.lexists(self.target))
                self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["photo.jpg"])
                self.assert_source_untouched()
                self.assert_retry_succeeds()
                self.target.unlink()

    def test_fsync_and_close_precede_link(self):
        events = []
        real_fsync, real_close, real_link = os.fsync, os.close, os.link
        with mock.patch.object(image_meta.os, "fsync", side_effect=lambda fd: (events.append("fsync"), real_fsync(fd))[1]), \
                mock.patch.object(image_meta.os, "close", side_effect=lambda fd: (events.append("close"), real_close(fd))[1]), \
                mock.patch.object(image_meta.os, "link", side_effect=lambda a, b: (events.append("link"), real_link(a, b))[1]):
            strip_file(self.source, self.target)
        self.assertEqual(events[:3], ["fsync", "close", "link"])  # later closes belong to the stage cleanup
        self.assertEqual(self.target.read_bytes(), CLEAN)

    def test_short_writes_are_completed_and_zero_progress_fails(self):
        real_write = os.write
        calls = []

        def five_at_a_time(fd, data):
            calls.append(1)
            return real_write(fd, bytes(data[:5]))
        with mock.patch.object(image_meta.os, "write", side_effect=five_at_a_time):
            strip_file(self.source, self.target)
        self.assertGreater(len(calls), 1)
        self.assertEqual(self.target.read_bytes(), CLEAN)
        self.target.unlink()
        with mock.patch.object(image_meta.os, "write", return_value=0):
            with self.assertRaises(OSError):
                strip_file(self.source, self.target)
        self.assertFalse(os.path.lexists(self.target))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["photo.jpg"])
        self.assert_retry_succeeds()

    def test_interrupt_before_commit_leaves_no_final_copy(self):
        with mock.patch.object(image_meta.os, "fsync", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                strip_file(self.source, self.target)
        self.assertFalse(os.path.lexists(self.target))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["photo.jpg"])
        self.assert_retry_succeeds()

    def test_abrupt_exit_before_commit_leaves_no_final_and_retry_works(self):
        script = self.dir / "child.py"
        script.write_text(
            "import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from campaign_tool import image_meta\n"
            "os.fsync = lambda fd: os._exit(7)\n"
            "image_meta.strip_file(sys.argv[2], sys.argv[3])\n")
        done = subprocess.run([sys.executable, "-B", str(script), str(REPO_ROOT), str(self.source), str(self.target)],
                              capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 7)
        self.assertFalse(os.path.lexists(self.target))
        remnants = [p for p in self.dir.iterdir() if p.name not in ("photo.jpg", "child.py")]
        self.assertEqual(len(remnants), 1)
        self.assertEqual(remnants[0].stat().st_mode & 0o777, 0o700)  # the surviving stage is private
        self.assert_retry_succeeds()
        self.assertTrue(remnants[0].exists())  # no stale-stage sweeping


class PublicationTests(StripBase):
    def test_target_arrival_at_publication_is_never_replaced(self):
        real_link = os.link

        def regular(path):
            Path(path).write_bytes(b"competing sentinel")

        def competing_clean(path):
            Path(path).write_bytes(CLEAN)

        def symlink(path):
            Path(path).symlink_to(self.dir / "photo.jpg")

        def dangling(path):
            Path(path).symlink_to(self.dir / "nowhere.jpg")

        def directory(path):
            Path(path).mkdir()
        for name, arrive in (("regular", regular), ("competing-clean", competing_clean), ("symlink", symlink),
                             ("dangling", dangling), ("directory", directory)):
            with self.subTest(arrival=name):
                def late(payload, destination, arrive=arrive):
                    arrive(destination)
                    return real_link(payload, destination)
                with mock.patch.object(image_meta.os, "link", side_effect=late):
                    with self.assertRaises(ValueError) as caught:
                        strip_file(self.source, self.target)
                self.assertEqual(str(caught.exception), f"{self.target}: already exists; nothing written")
                self.assertTrue(os.path.lexists(self.target))
                if name == "regular":
                    self.assertEqual(self.target.read_bytes(), b"competing sentinel")
                elif name == "competing-clean":
                    self.assertEqual(self.target.read_bytes(), CLEAN)
                elif name == "symlink":
                    self.assertEqual(os.readlink(self.target), str(self.dir / "photo.jpg"))
                elif name == "dangling":
                    self.assertEqual(os.readlink(self.target), str(self.dir / "nowhere.jpg"))
                else:
                    self.assertTrue(self.target.is_dir() and not os.listdir(self.target))
                self.assertFalse((self.dir / "nowhere.jpg").exists())
                self.assertEqual(sorted(p.name for p in self.dir.iterdir()), sorted(["photo.jpg", "copy.jpg"]))
                self.assert_source_untouched()
                if self.target.is_dir() and not self.target.is_symlink():
                    self.target.rmdir()
                else:
                    self.target.unlink()

    def test_unsupported_link_fails_safely_without_final(self):
        with mock.patch.object(image_meta.os, "link", side_effect=OSError(errno.EPERM, "links unsupported")):
            with self.assertRaises(OSError):
                strip_file(self.source, self.target)
        self.assertFalse(os.path.lexists(self.target))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["photo.jpg"])

    def test_post_commit_cleanup_failure_keeps_complete_final(self):
        real = image_meta.tempfile.TemporaryDirectory

        class FailingCleanup(real):
            def cleanup(self):
                super().cleanup()
                raise OSError(errno.EBUSY, "synthetic cleanup failure")
        with mock.patch.object(image_meta.tempfile, "TemporaryDirectory", FailingCleanup):
            with self.assertRaises(OSError):
                strip_file(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), CLEAN)  # never removed as a rollback
        with self.assertRaises(ValueError) as caught:  # a retry honestly meets the complete file
            strip_file(self.source, self.target)
        self.assertIn("already exists; nothing written", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), CLEAN)

    def test_original_target_and_unrelated_entries_survive_failures(self):
        existing = self.dir / "existing.jpg"
        existing.write_bytes(b"organizer file")
        sentinel = self.dir / "sibling.txt"
        sentinel.write_bytes(b"sibling")
        other_stage = self.dir / ".strip-metadata-other"
        other_stage.mkdir(mode=0o700)
        (other_stage / "payload").write_bytes(b"another invocation's half-written payload")
        before = snapshot(self.dir)
        with self.assertRaises(ValueError):
            strip_file(self.source, existing)
        self.assertEqual(snapshot(self.dir), before)
        with mock.patch.object(image_meta.os, "fsync", side_effect=STORAGE_FULL):
            with self.assertRaises(OSError):
                strip_file(self.source, self.target)
        self.assertEqual(snapshot(self.dir), before)
        self.assert_retry_succeeds()
        after = snapshot(self.dir)
        after.pop("copy.jpg")
        self.assertEqual(after, before)


class ContractTests(StripBase):
    def test_default_explicit_and_format_controls_keep_contract(self):
        controls = (
            (".jpg", DIRTY, CLEAN, ["EXIF GPS", "comment"]),
            (".png", png(chunk(b"tEXt", b"k\0v"), chunk(b"eXIf", tiff())), png(), ["EXIF GPS", "comment"]),
            (".webp", webp(riff(b"EXIF", tiff()), riff(b"XMP ", b"<x/>"), flags=0x1C), webp(flags=0x10),
             ["EXIF GPS", "XMP"]),
            (".jpg", CLEAN, CLEAN, []),
        )
        for suffix, dirty, clean, _ in controls:
            with self.subTest(suffix=suffix, clean=dirty == clean):
                folder = Path(tempfile.mkdtemp(dir=self.dir))
                source = folder / f"pic{suffix}"
                source.write_bytes(dirty)
                result = strip_file(source)  # default <stem>.clean<suffix> beside the original
                default = folder / f"pic.clean{suffix}"
                self.assertEqual(list(result), ["removed", "written"])
                self.assertEqual(result["written"], str(default))
                self.assertEqual(default.read_bytes(), clean)
                self.assertEqual(source.read_bytes(), dirty)
                self.assertEqual(sorted(p.name for p in folder.iterdir()), sorted([source.name, default.name]))
                explicit = folder / f"custom{suffix}"
                result = strip_file(source, str(explicit))
                self.assertEqual(result["written"], str(explicit))
                self.assertEqual(explicit.read_bytes(), clean)
                with self.assertRaises(ValueError):  # no-overwrite promise
                    strip_file(source, explicit)
        self.assertEqual(strip_file(self.source, self.dir / "kinds.jpg")["removed"], ["EXIF GPS", "comment"])

    def test_published_mode_follows_umask(self):
        old = os.umask(0o022)
        try:
            strip_file(self.source, self.dir / "a.jpg")
            os.umask(0o077)
            strip_file(self.source, self.dir / "b.jpg")
            os.umask(0o000)
            strip_file(self.source, self.dir / "c.jpg")
        finally:
            os.umask(old)
        self.assertEqual([(self.dir / n).stat().st_mode & 0o777 for n in ("a.jpg", "b.jpg", "c.jpg")],
                         [0o644, 0o600, 0o644])

    def test_parser_and_public_folder_refusals_create_no_stage(self):
        (self.dir / "public").mkdir()
        existing = self.dir / "existing.jpg"
        existing.write_bytes(b"organizer file")
        link = self.dir / "link.jpg"
        link.symlink_to(self.source)
        broken = self.dir / "broken.jpg"
        broken.write_bytes(jpeg()[:-2])
        oversized = self.dir / "big.jpg"
        oversized.write_bytes(jpeg() + bytes(MAX_BYTES))
        svg = self.dir / "logo.svg"
        svg.write_bytes(b"<svg/>")
        cases = [(link, self.dir / "l.jpg", "is a symlink"),
                 (self.source, existing, "already exists"),
                 (self.source, self.dir / "public" / "x.jpg", "inside a public/ folder"),
                 (broken, self.dir / "b.jpg", "unreadable image"),
                 (oversized, self.dir / "o.jpg", "unreadable image"),
                 (svg, self.dir / "s.svg", "only")]
        before = snapshot(self.dir)
        with mock.patch.object(image_meta.tempfile, "TemporaryDirectory", side_effect=AssertionError("stage made")):
            for source, target, reason in cases:
                with self.subTest(reason=reason, source=source.name):
                    with self.assertRaises(ValueError) as caught:
                        strip_file(source, target)
                    self.assertIn(reason, str(caught.exception))
                    self.assertNotIn(str(GPS_VALUE), str(caught.exception))
        self.assertEqual(snapshot(self.dir), before)


if __name__ == "__main__":
    unittest.main()
