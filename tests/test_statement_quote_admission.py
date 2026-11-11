"""RF0330-5: the statement --quote-file descriptor is admitted before any read and never leaks.

Synthetic files only; nothing is fetched. The os.open seam substitutes the quote after the initial lstat.
"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import statements as st

CHANGED = "--quote-file: changed while it was being read; nothing was written"
CANNOT = "--quote-file: cannot be read; nothing was written"
TOO_BIG = "--quote-file: file too large for a quote of at most 400 characters; nothing was written"
CANARY = "Unmistakable Canary"


class QuoteAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-quote-admit-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.quote = self.tmp / "quote.txt"
        self.quote.write_bytes(b"Ordinary quote.")

    def refusal(self, path=None):
        with self.assertRaises(st.StatementError) as caught:
            st._quote_file(path or self.quote)
        self.assertIsNone(caught.exception.__cause__)
        return str(caught.exception)

    def swap_on_open(self, replace):
        real_open = os.open
        fired = []

        def forwarding(path, *args, **kwargs):
            if str(path) == str(self.quote) and not fired:
                fired.append(True)
                replace()
            return real_open(path, *args, **kwargs)
        return mock.patch.object(st.os, "open", forwarding)

    def no_reads(self):
        """Fail the test if any payload read reaches a stream or raw fd."""
        return mock.patch.object(st.os, "read", side_effect=AssertionError("payload read")), \
            mock.patch.object(st.os, "fdopen", side_effect=AssertionError("stream built"))

    def test_regular_quote_is_returned_exactly(self):
        self.quote.write_bytes(b"  Cafe\xcc\x81 \r\n")
        self.assertEqual(st._quote_file(self.quote), "  Cafe\u0301 \r\n")
        self.quote.write_bytes(b"x" * st.MAX_QUOTE_FILE)
        self.assertEqual(len(st._quote_file(self.quote)), st.MAX_QUOTE_FILE)
        self.quote.write_bytes(b"x" * (st.MAX_QUOTE_FILE + 1))
        self.assertEqual(self.refusal(), TOO_BIG)

    def test_fdopen_constructor_failure_closes_raw_fd_once(self):
        captured = []
        real_open = os.open

        def recording(*args, **kwargs):
            captured.append(real_open(*args, **kwargs))
            return captured[-1]
        with mock.patch.object(st.os, "open", recording), \
                mock.patch.object(st.os, "fdopen", side_effect=OSError(24, "EMFILE-canary")):
            message = self.refusal()
        self.assertEqual(message, CANNOT)
        self.assertNotIn("EMFILE", message)
        self.assertEqual(len(captured), 1)
        with self.assertRaises(OSError):
            os.fstat(captured[0])

    def test_fstat_failure_and_unexpected_exception_release_fd(self):
        captured = []
        real_open = os.open

        def recording(*args, **kwargs):
            captured.append(real_open(*args, **kwargs))
            return captured[-1]
        with mock.patch.object(st.os, "open", recording), \
                mock.patch.object(st.os, "fstat", side_effect=OSError(5, "EIO-canary")):
            self.assertEqual(self.refusal(), CANNOT)
        with mock.patch.object(st.os, "open", recording), \
                mock.patch.object(st.os, "fdopen", side_effect=RuntimeError("unexpected")):
            with self.assertRaises(RuntimeError):
                st._quote_file(self.quote)
        self.assertEqual(len(captured), 2)
        for fd in captured:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_substituted_fifo_is_refused_before_any_read(self):
        def replace():
            self.quote.unlink()
            os.mkfifo(self.quote)
        keeper = []

        def populate():
            replace()
            fd = os.open(self.quote, os.O_RDWR | os.O_NONBLOCK)
            keeper.append(fd)
            os.write(fd, CANARY.encode())
        self.addCleanup(lambda: [os.close(fd) for fd in keeper])
        read_patch, fdopen_patch = self.no_reads()
        with self.swap_on_open(populate), read_patch, fdopen_patch:
            self.assertEqual(self.refusal(), CHANGED)

    def test_substituted_inode_and_directory_are_refused_before_any_read(self):
        def other_file():
            self.quote.unlink()
            self.quote.write_bytes(CANARY.encode())

        def directory():
            self.quote.unlink()
            self.quote.mkdir()
        for replace in (other_file, directory):
            with self.subTest(replace=replace.__name__):
                self.quote.unlink(missing_ok=True) if self.quote.is_file() else None
                if not self.quote.exists():
                    self.quote.write_bytes(b"Ordinary quote.")
                read_patch, fdopen_patch = self.no_reads()
                with self.swap_on_open(replace), read_patch, fdopen_patch:
                    self.assertEqual(self.refusal(), CHANGED)
                if self.quote.is_dir():
                    self.quote.rmdir()

    def test_replacement_symlink_stays_unfollowed(self):
        target = self.tmp / "target.txt"
        target.write_bytes(CANARY.encode())

        def link():
            self.quote.unlink()
            self.quote.symlink_to(target)
        with self.swap_on_open(link):
            self.assertEqual(self.refusal(), CANNOT)

    def test_growth_before_open_is_refused_unread_and_growth_after_fstat_is_caught(self):
        read_patch, fdopen_patch = self.no_reads()

        def grow():
            self.quote.write_bytes(b"x" * (st.MAX_QUOTE_FILE + 1))
        with self.swap_on_open(grow), read_patch, fdopen_patch:
            self.assertEqual(self.refusal(), TOO_BIG)
        # growth between fstat and read: the actual-byte guard still refuses before decoding
        self.quote.write_bytes(b"Ordinary quote.")
        real_fstat = os.fstat

        def fstat_then_grow(fd):
            result = real_fstat(fd)
            grow()
            return result
        with mock.patch.object(st.os, "fstat", fstat_then_grow):
            self.assertEqual(self.refusal(), TOO_BIG)

    def test_add_statement_refusal_writes_nothing(self):
        root = self.tmp / "campaign"
        shutil.copytree(Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign", root,
                        ignore=shutil.ignore_patterns("public"))
        (root / "content" / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": [
            {"agency_id": "ca-cedar-city-council", "officials": [{"name": "Ash Example", "role": "Council member",
                                              "source_url": "https://cedar.example/c", "checked_on": "2026-09-01"}]}]}),
            encoding="utf-8")
        before = sorted((p.relative_to(root), p.read_bytes()) for p in root.rglob("*") if p.is_file())

        def other_file():
            self.quote.unlink()
            self.quote.write_bytes(CANARY.encode())
        with self.swap_on_open(other_file), mock.patch.object(st, "_install") as install:
            with self.assertRaises(st.StatementError) as caught:
                st.add_statement(root, agency_id="ca-cedar-city-council", official="Ash Example", said_on="2026-10-01", quote_file=self.quote,
                                 context="meeting", source_url="https://cedar.example/m", checked_on="2026-10-10",
                                 today=date(2026, 10, 20))
        self.assertIn("changed while it was being read", str(caught.exception))
        self.assertNotIn(CANARY, str(caught.exception))
        install.assert_not_called()
        after = sorted((p.relative_to(root), p.read_bytes()) for p in root.rglob("*") if p.is_file())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
