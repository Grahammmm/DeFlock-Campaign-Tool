"""RF1831-2: the events.json payload is read only through a no-follow regular-file descriptor. Synthetic data only."""
import os
import threading
import unittest
from unittest import mock

from campaign_tool import content_events
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.content_events import read_events
from campaign_tool.site import build_site

from tests.test_site_events import TODAY, EventsCase

REAL_OS_OPEN, REAL_OPEN, REAL_CLOSE = os.open, open, os.close
REFUSAL = r"^events\.json: must be a regular file, not a symlink or folder$"


class LeafAdmissionTests(EventsCase):
    def swap_at_open(self, make):
        """At the payload open (after the pathname checks) replace the leaf using ``make(path)``."""
        def opener(path, flags, *args, **kwargs):
            if os.fspath(path) == str(self.path):
                self.path.unlink()
                make(self.path)
            return REAL_OS_OPEN(path, flags, *args, **kwargs)
        return mock.patch.object(content_events.os, "open", opener)

    def outside_link(self, path):
        outside = self.root / "outside.json"
        outside.write_bytes(b"[]")
        path.symlink_to(outside)

    def run_bounded(self, call):
        """Run ``call`` with a watchdog so a blocking FIFO open/read fails the test instead of hanging."""
        box = {}

        def target():
            try:
                box["result"] = call()
            except BaseException as exc:  # noqa: BLE001 - reported to the test thread
                box["error"] = exc
        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive(), "reader blocked")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def test_symlink_swap_at_payload_open_is_never_read(self):
        self.put(b"[]")
        with self.swap_at_open(self.outside_link):
            with self.assertRaisesRegex(ContentError, REFUSAL):
                read_events(self.root)
        self.path.unlink()
        self.put(b"[]")
        problems = ["earlier"]
        with self.swap_at_open(self.outside_link):
            self.assertIsNone(read_events(self.root, problems))
        self.assertEqual(problems, ["earlier", "events.json: must be a regular file, not a symlink or folder"])

    def test_fifo_and_directory_swaps_refuse_before_read(self):
        for make in (os.mkfifo, os.mkdir):
            self.put(b"[]")
            with self.swap_at_open(make), mock.patch.object(content_events, "open", side_effect=AssertionError("opened"), create=True):
                with self.assertRaisesRegex(ContentError, REFUSAL):
                    self.run_bounded(lambda: read_events(self.root))
            self.path.rmdir() if self.path.is_dir() else self.path.unlink()

    def test_pretransfer_failures_close_only_owned_fd(self):
        self.put(b"[]")
        opened, closed = [], []

        def spy_open(path, flags, *args, **kwargs):
            fd = REAL_OS_OPEN(path, flags, *args, **kwargs)
            if os.fspath(path) == str(self.path):
                opened.append(fd)
            return fd

        def spy_close(fd):
            closed.append(fd)
            return REAL_CLOSE(fd)
        sentinel = REAL_OS_OPEN(os.devnull, os.O_RDONLY)
        self.addCleanup(REAL_CLOSE, sentinel)
        with mock.patch.object(content_events.os, "open", spy_open), mock.patch.object(content_events.os, "close", spy_close):
            with mock.patch.object(content_events.os, "fstat", side_effect=OSError("boom")):
                with self.assertRaisesRegex(ContentError, r"^events\.json: could not be read$"):
                    read_events(self.root)
            with mock.patch.object(content_events, "open", side_effect=MemoryError, create=True):
                with self.assertRaises(MemoryError):
                    read_events(self.root)
            self.assertEqual(read_events(self.root), [])
        self.assertEqual(closed, opened[:2])  # the two failed attempts close their fd once; the stream handles the third
        self.assertEqual(len(opened), 3)
        os.fstat(sentinel)  # unrelated descriptor still live

    def test_regular_replacement_and_postopen_rename_keep_opened_bytes(self):
        self.put(b"[1]")
        replacement = self.root / "new.json"

        def make(path):
            path.write_bytes(b"[]")
        with self.swap_at_open(make):
            self.assertEqual(read_events(self.root), [])  # eligible regular replacement parses normally
        self.path.unlink()
        self.put(b"[]")

        def stream_open(fd, mode="r"):
            stream = REAL_OPEN(fd, mode)
            self.path.rename(replacement)
            self.outside_link(self.path)
            return stream
        with mock.patch.object(content_events, "open", stream_open, create=True):
            self.assertEqual(read_events(self.root), [])  # the already opened inode is read, not the new link

    def test_leaf_refusal_preserves_consumers_and_recovers(self):
        self.put(b"[]")
        before = self.build()
        with self.swap_at_open(self.outside_link):
            self.assertIn("events.json: must be a regular file, not a symlink or folder", content_problems(self.root))
        self.path.unlink()
        self.put(b"[]")
        with self.swap_at_open(self.outside_link):
            with self.assertRaisesRegex(ContentError, REFUSAL):
                build_site(self.root, today=TODAY)
        self.assertEqual(self.tree(), before)
        self.path.unlink()
        self.put(b"[]")
        self.assertEqual(read_events(self.root), [])
        build_site(self.root, today=TODAY)


if __name__ == "__main__":
    unittest.main()
