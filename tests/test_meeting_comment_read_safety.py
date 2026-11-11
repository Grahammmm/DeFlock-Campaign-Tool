"""RF1155-6: the shared comment-receipt reader enforces its 2 MiB ceiling while reading. Synthetic only.

Run from the repository root: python3 -B -W error::ResourceWarning -m unittest tests.test_meeting_comment_read_safety -q
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import meeting_comments as mc  # noqa: E402
from campaign_tool.content import MAX_JSON_BYTES  # noqa: E402

GOOD = {"on": "2026-03-12", "channel": "email", "recorded_at": "2026-03-12T00:00:00Z"}
EMPTY = b'{"schema_version": 1, "comments": {}}'


def padded(size):
    return EMPTY + b" " * (size - len(EMPTY))


class ReadSafetyTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-read-safety-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "kit").mkdir()
        self.store = self.root / "kit" / "comments-sent.json"

    def spy(self):
        """Record every os.read request/return and every os.close; wrap the real calls."""
        log = {"requested": [], "returned": 0, "opened": [], "closed": []}
        real_read, real_open, real_close = os.read, os.open, os.close

        def read(fd, n):
            log["requested"].append(n)
            data = real_read(fd, n)
            log["returned"] += len(data)
            return data

        def opened(path, flags, *a, **k):
            fd = real_open(path, flags, *a, **k)
            if str(path) == str(self.store):
                log["opened"].append((fd, flags))
            return fd

        def closed(fd):
            log["closed"].append(fd)
            return real_close(fd)

        patches = [mock.patch("os.read", read), mock.patch("os.open", opened), mock.patch("os.close", closed)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return log

    def test_oversize_receipt_is_refused_before_payload_read(self):
        self.store.write_bytes(padded(4 * MAX_JSON_BYTES + 1))
        before = self.store.read_bytes()
        log = self.spy()
        with mock.patch("json.loads", side_effect=AssertionError("parsed")):
            with self.assertRaises(mc.MeetingCommentError) as caught:
                mc.read(self.root)
        self.assertEqual(log["returned"], 0)
        self.assertEqual([fd for fd, _ in log["opened"]], log["closed"])
        self.assertIn("(ContentError)", str(caught.exception))
        self.assertEqual(self.store.read_bytes(), before)

    def test_real_reader_boundaries_count_utf8_bytes(self):
        for size in (MAX_JSON_BYTES - 1, MAX_JSON_BYTES):
            self.store.write_bytes(padded(size))
            self.assertEqual(mc.read(self.root), {})
        self.store.write_bytes(padded(MAX_JSON_BYTES + 1))
        with self.assertRaises(mc.MeetingCommentError):
            mc.read(self.root)
        # Multibyte key: 2-byte characters make the byte size exceed the character count.
        key = "é" * 40
        body = json.dumps({"schema_version": 1, "comments": {key: GOOD}}, ensure_ascii=False).encode()
        self.store.write_bytes(body + b" " * (MAX_JSON_BYTES - len(body)))
        self.assertEqual(mc.read(self.root), {key: GOOD})
        self.store.write_bytes(body + b" " * (MAX_JSON_BYTES - len(body) + 1))
        with self.assertRaises(mc.MeetingCommentError):
            mc.read(self.root)

    def test_reader_limit_ignores_patched_writer_cap(self):
        self.store.write_bytes(padded(4096))
        with mock.patch.object(mc, "MAX_JSON_BYTES", 10):
            self.assertEqual(mc.read(self.root), {})

    def test_growth_after_fstat_never_parses_overflow(self):
        self.store.write_bytes(EMPTY)
        real_fstat = os.fstat
        grown = []

        def fstat(fd):
            info = real_fstat(fd)
            if not grown:
                grown.append(True)
                with open(self.store, "ab") as handle:
                    handle.write(b" " * (4 * MAX_JSON_BYTES))
            return info

        log = self.spy()
        with mock.patch("os.fstat", fstat), mock.patch("json.loads", side_effect=AssertionError("parsed")):
            with self.assertRaises(mc.MeetingCommentError):
                mc.read(self.root)
        self.assertEqual(log["returned"], MAX_JSON_BYTES + 1)
        self.assertLessEqual(sum(log["requested"]), MAX_JSON_BYTES + 1)
        self.assertEqual(len(log["opened"]), 1)
        self.assertEqual([fd for fd, _ in log["opened"]], log["closed"])

    def test_short_reads_and_shrink_preserve_whole_document_validation(self):
        body = json.dumps({"schema_version": 1, "comments": {"m1": GOOD}}).encode()
        self.store.write_bytes(body)
        real_read = os.read
        with mock.patch("os.read", lambda fd, n: real_read(fd, min(n, 7))):
            self.assertEqual(mc.read(self.root), {"m1": GOOD})
        real_fstat = os.fstat

        def fstat(fd):
            info = real_fstat(fd)
            self.store.write_bytes(b"")
            return info

        self.store.write_bytes(body)
        with mock.patch("os.fstat", fstat):
            with self.assertRaises(mc.MeetingCommentError):
                mc.read(self.root)

    def test_special_files_and_swaps_never_read_payload(self):
        outside = self.root / "outside.json"
        outside.write_bytes(EMPTY)
        self.store.symlink_to(outside)
        with self.assertRaises(mc.MeetingCommentError):
            mc.read(self.root)
        self.store.unlink()
        self.store.mkdir()
        log = self.spy()
        with self.assertRaises(mc.MeetingCommentError):
            mc.read(self.root)
        self.store.rmdir()
        os.mkfifo(self.store)
        with self.assertRaises(mc.MeetingCommentError):
            mc.read(self.root)
        self.assertEqual(log["requested"], [])
        self.assertEqual([fd for fd, _ in log["opened"]], log["closed"])
        self.assertEqual(outside.read_bytes(), EMPTY)
        # Regular-to-symlink swap at the open seam: O_NOFOLLOW refuses the target.
        self.store.unlink()
        self.store.write_bytes(EMPTY)
        real_open = os.open

        def swap(path, flags, *a, **k):
            if str(path) == str(self.store):
                self.store.unlink()
                self.store.symlink_to(outside)
            return real_open(path, flags, *a, **k)

        with mock.patch("os.open", swap), self.assertRaises(mc.MeetingCommentError):
            mc.read(self.root)
        self.assertTrue(log["opened"][-1][1] & os.O_NOFOLLOW and log["opened"][-1][1] & os.O_NONBLOCK)

    def test_descriptor_flags_and_closure_on_every_exit(self):
        cases = {
            "ok": EMPTY,
            "utf8": b"\xff\xfe",
            "json": b"{not json",
            "shape": b"[]",
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.store.write_bytes(data)
                log = self.spy()
                try:
                    mc.read(self.root)
                except mc.MeetingCommentError as exc:
                    self.assertNotIn(str(self.store), str(exc))
                self.assertEqual(len(log["opened"]), 1)
                fd, flags = log["opened"][0]
                self.assertTrue(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK)
                self.assertEqual(log["closed"], [fd])
        self.store.write_bytes(EMPTY)
        for target in ("fstat", "read"):
            with self.subTest(target):
                log = self.spy()
                with mock.patch(f"os.{target}", side_effect=OSError("CANARY /secret")):
                    with self.assertRaises(mc.MeetingCommentError) as caught:
                        mc.read(self.root)
                self.assertNotIn("CANARY", str(caught.exception))
                self.assertEqual([fd for fd, _ in log["opened"]], log["closed"])

    def test_absent_store_and_schema_parity(self):
        self.assertEqual(mc.read(self.root), {})
        entries = {"a": dict(GOOD, letter_sha256="b" * 64, letter_bytes=0), "z": dict(GOOD, channel="in_person")}
        self.store.write_text(json.dumps({"schema_version": 1, "comments": entries}))
        self.assertEqual(mc.read(self.root), entries)
        self.store.write_text(json.dumps({"schema_version": 1, "comments": {"a": {"on": "bad"}}}))
        with self.assertRaises(mc.MeetingCommentError):
            mc.read(self.root)
        self.store.write_text(json.dumps({"schema_version": 2, "comments": {}}))
        with self.assertRaises(mc.MeetingCommentError):
            mc.read(self.root)

    def test_record_refuses_oversize_old_store_before_writing(self):
        self.store.write_bytes(padded(MAX_JSON_BYTES + 1))
        before = self.store.read_bytes()
        with mock.patch.object(mc, "_letter", side_effect=AssertionError("letter")), \
                mock.patch.object(mc, "_write_json", side_effect=AssertionError("write")):
            # read() is the first store access in record(); it must refuse without reaching letter or writer.
            with self.assertRaises(mc.MeetingCommentError):
                mc.read(self.root)
        self.assertEqual(self.store.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
