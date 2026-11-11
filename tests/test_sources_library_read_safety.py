"""Synthetic tests: the sources.json reader admits only an absent leaf or a bounded regular file."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import sources_cmd
from campaign_tool.content import MAX_JSON_BYTES, ContentError


class ReadExistingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "sources.json"

    def test_cap_unchanged(self):
        self.assertEqual(MAX_JSON_BYTES, 2097152)

    def test_absent_and_valid_preserve_raw_text(self):
        self.assertEqual(sources_cmd._read_existing(self.path), (None, []))
        self.assertFalse(self.path.exists())
        self.path.write_bytes(b"[\r\n]\r\n")
        self.assertEqual(sources_cmd._read_existing(self.path), ("[\r\n]\r\n", []))
        self.path.write_bytes(b"null")
        self.assertEqual(sources_cmd._read_existing(self.path), ("null", None))

    def test_bad_utf8_and_json(self):
        for data in (b"\xff[]", b"[1,"):
            self.path.write_bytes(data)
            with self.assertRaisesRegex(ContentError, "not valid UTF-8 JSON"):
                sources_cmd._read_existing(self.path)

    def test_exact_cap_accepted_cap_plus_one_refused_before_open(self):
        self.path.write_bytes(b"[]" + b" " * (MAX_JSON_BYTES - 2))
        raw, doc = sources_cmd._read_existing(self.path)
        self.assertEqual((len(raw), doc), (MAX_JSON_BYTES, []))
        self.path.write_bytes(b"[]" + b" " * (MAX_JSON_BYTES - 1))
        with mock.patch.object(sources_cmd.os, "open", side_effect=AssertionError("opened")):
            with self.assertRaisesRegex(ContentError, "file too large"):
                sources_cmd._read_existing(self.path)

    def test_multibyte_counts_bytes(self):
        body = '["' + "\u00e9" * (MAX_JSON_BYTES // 2) + '"]'
        self.assertLess(len(body), MAX_JSON_BYTES)
        self.path.write_bytes(body.encode())
        with self.assertRaisesRegex(ContentError, "file too large"):
            sources_cmd._read_existing(self.path)

    def test_growth_after_fstat_reads_only_cap_plus_one(self):
        self.path.write_bytes(b"[]")
        real_fstat = os.fstat
        real_fdopen = os.fdopen
        reads = []

        def grow(fd):
            result = real_fstat(fd)
            with open(self.path, "ab") as fh:
                fh.write(b" " * (MAX_JSON_BYTES + 100))
            return result

        def spy(fd, *a, **k):
            stream = real_fdopen(fd, *a, **k)
            return _ReadSpy(stream, reads)

        with mock.patch.object(sources_cmd.os, "fstat", grow), mock.patch.object(sources_cmd.os, "fdopen", spy), \
                mock.patch.object(sources_cmd.json, "loads", side_effect=AssertionError("parsed")):
            with self.assertRaisesRegex(ContentError, "file too large"):
                sources_cmd._read_existing(self.path)
        self.assertEqual(reads, [(MAX_JSON_BYTES + 1, MAX_JSON_BYTES + 1)])

    def test_fifo_directory_and_symlinks_refused_without_open(self):
        fifo = self.dir / "fifo"
        os.mkfifo(fifo)
        directory = self.dir / "dir"
        directory.mkdir()
        target = self.dir / "target"
        target.write_text("[]")
        live = self.dir / "live"
        live.symlink_to(target)
        dangling = self.dir / "dangling"
        dangling.symlink_to(self.dir / "nowhere")
        with mock.patch.object(sources_cmd.os, "open", side_effect=AssertionError("opened")):
            for special in (fifo, directory):
                with self.assertRaisesRegex(ContentError, "must be a regular file"):
                    sources_cmd._read_existing(special)
            for link in (live, dangling):
                with self.assertRaisesRegex(ContentError, "symlinks are not read"):
                    sources_cmd._read_existing(link)
        self.assertTrue(stat.S_ISFIFO(os.lstat(fifo).st_mode))
        self.assertEqual(target.read_text(), "[]")

    def test_swap_to_symlink_or_fifo_at_open_seam_refused(self):
        target = self.dir / "target"
        target.write_text("[]")
        real_open = os.open

        def swap_link(p, flags, *a, **k):
            os.unlink(p)
            os.symlink(target, p)
            return real_open(p, flags, *a, **k)

        def swap_fifo(p, flags, *a, **k):
            os.unlink(p)
            os.mkfifo(p)
            return real_open(p, flags, *a, **k)

        self.path.write_bytes(b"[]")
        with mock.patch.object(sources_cmd.os, "open", swap_link):
            with self.assertRaisesRegex(ContentError, "symlinks are not read"):
                sources_cmd._read_existing(self.path)
        os.unlink(self.path)
        self.path.write_bytes(b"[]")
        with mock.patch.object(sources_cmd.os, "open", swap_fifo):
            with self.assertRaisesRegex(ContentError, "must be a regular file"):
                sources_cmd._read_existing(self.path)

    def test_disappearance_after_inspection_is_refusal_not_absence(self):
        self.path.write_bytes(b"[]")
        real_open = os.open

        def vanish(p, flags, *a, **k):
            os.unlink(p)
            return real_open(p, flags, *a, **k)

        with mock.patch.object(sources_cmd.os, "open", vanish):
            with self.assertRaises(FileNotFoundError):
                sources_cmd._read_existing(self.path)

    def test_descriptor_closed_on_faults_and_success(self):
        self.path.write_bytes(b"[]")
        before = set(os.listdir("/proc/self/fd"))
        with mock.patch.object(sources_cmd.os, "fdopen", side_effect=MemoryError):
            with self.assertRaises(MemoryError):
                sources_cmd._read_existing(self.path)
        self.assertEqual(set(os.listdir("/proc/self/fd")), before)
        with mock.patch.object(sources_cmd.os, "fstat", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                sources_cmd._read_existing(self.path)
        self.assertEqual(set(os.listdir("/proc/self/fd")), before)
        self.path.write_bytes(b"[1,")
        with self.assertRaises(ContentError):
            sources_cmd._read_existing(self.path)
        self.assertEqual(set(os.listdir("/proc/self/fd")), before)
        self.path.write_bytes(b"[]")
        self.assertEqual(sources_cmd._read_existing(self.path), ("[]", []))
        self.assertEqual(set(os.listdir("/proc/self/fd")), before)


class _ReadSpy:
    def __init__(self, stream, log):
        self._stream = stream
        self._log = log

    def read(self, n=-1):
        data = self._stream.read(n)
        self._log.append((n, len(data)))
        return data

    def fileno(self):
        return self._stream.fileno()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._stream.close()


class CallerTests(unittest.TestCase):
    def test_list_sources_refuses_fifo_and_oversize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "content").mkdir()
            path = root / "content" / "sources.json"
            os.mkfifo(path)
            with self.assertRaisesRegex(ContentError, "must be a regular file"):
                sources_cmd.list_sources(root)
            os.unlink(path)
            path.write_bytes(b"[]" + b" " * MAX_JSON_BYTES)
            with self.assertRaisesRegex(ContentError, "file too large"):
                sources_cmd.list_sources(root)
            self.assertEqual(path.stat().st_size, MAX_JSON_BYTES + 2)


if __name__ == "__main__":
    unittest.main()
