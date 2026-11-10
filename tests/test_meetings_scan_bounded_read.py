"""RF0115-2: the kit/meetings.json scan reader enforces its 4 MiB ceiling on bytes actually read (synthetic, offline)."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import cli
from campaign_tool import meetings_manual as mm
from campaign_tool.meeting_stage import MeetingStageError, stage
from campaign_tool.meetings_manual import MAX_BYTES, ManualMeetingError, read_meetings, record_manual

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 10)
SIZE_ERROR = "kit/meetings.json: larger than 4 MiB"
ROW = {"id": "synthetic-1", "body": "Smallville Council", "starts_at": "2026-11-20T18:30:00-08:00"}
BASE = dict(body="Smallville Water Board", agency_id="ca-cedar-police", starts_at="2026-11-20T18:30:00-08:00",
            agenda_url="https://example.invalid/agenda", agenda_item="Item 5", source_url="https://example.invalid/p",
            checked_on="2026-11-09")


def padded(size, meetings=None):
    head = json.dumps({"schema_version": 1, "meetings": [ROW] if meetings is None else meetings}).encode()
    return head + b" " * (size - len(head))


class ScanBoundTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-scan-bound-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.scan = self.root / "kit" / "meetings.json"
        self.manual = self.root / "kit" / "meetings-manual.json"
        self.scan.parent.mkdir(exist_ok=True)
        self.scan.write_bytes(padded(215))

    @contextlib.contextmanager
    def grow_after_preflight(self, extra=4 * MAX_BYTES):
        """Append whitespace to the same inode right after the reader's explicit size stat."""
        real_stat, scan, state = Path.stat, self.scan, {"done": False}

        def stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == scan and not state["done"] and state["calls"] >= 1:
                state["done"] = True
                with open(scan, "ab") as handle:
                    handle.write(b" " * extra)
            if path == scan:
                state["calls"] += 1
            return result

        state["calls"] = 0
        with mock.patch.object(Path, "stat", stat):
            yield
        self.assertTrue(state["done"], "growth hook never fired")

    @contextlib.contextmanager
    def read_log(self):
        sizes, real_open = [], open

        def spy(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if "rb" in mode and Path(path) == self.scan:
                real_read = handle.read
                handle.read = lambda n=-1: (sizes.append(n), real_read(n))[1]
                sizes.append("opened")
            return handle

        with mock.patch.object(mm, "open", spy, create=True):
            yield sizes

    def test_growth_after_preflight_refuses_with_bounded_read(self):
        with self.read_log() as sizes, self.grow_after_preflight():
            with self.assertRaises(ManualMeetingError) as caught:
                read_meetings(self.root)
        self.assertEqual(str(caught.exception), SIZE_ERROR)
        self.assertEqual([s for s in sizes if s != "opened"], [MAX_BYTES + 1])
        self.assertFalse(self.manual.exists())

    def test_boundaries_and_error_precedence(self):
        self.scan.write_bytes(padded(MAX_BYTES))
        self.assertEqual(read_meetings(self.root)[0]["id"], "synthetic-1")
        self.scan.write_bytes(padded(MAX_BYTES - 1))
        self.assertEqual(len(read_meetings(self.root)), 1)
        self.scan.write_bytes(padded(MAX_BYTES + 1))
        with self.assertRaises(ManualMeetingError) as caught:
            read_meetings(self.root)
        self.assertEqual(str(caught.exception), SIZE_ERROR)
        # multibyte: character count under the cap, UTF-8 bytes over it
        row = dict(ROW, body="é" * (MAX_BYTES // 2 + 10))
        self.scan.write_bytes(json.dumps({"schema_version": 1, "meetings": [row]}, ensure_ascii=False).encode())
        with self.assertRaises(ManualMeetingError) as caught:
            read_meetings(self.root)
        self.assertEqual(str(caught.exception), SIZE_ERROR)
        # grown and also invalid JSON: size error wins over the parse diagnostic
        self.scan.write_bytes(b"{not json")
        with self.grow_after_preflight(), self.assertRaises(ManualMeetingError) as caught:
            mm._read_scan(self.root)
        self.assertEqual(str(caught.exception), SIZE_ERROR)

    def test_existing_diagnostics_and_stream_closure(self):
        for raw, needle in ((b"\xff\xfe", "UnicodeDecodeError"), (b"{nope", "JSONDecodeError"), (b"", "JSONDecodeError"),
                            (b'{"schema_version": 2, "meetings": []}', "unsupported schema_version"),
                            (b'{"schema_version": 1, "meetings": {}}', "meetings must be a list")):
            self.scan.write_bytes(raw)
            with self.assertRaises(ManualMeetingError) as caught:
                mm._read_scan(self.root)
            self.assertIn(needle, str(caught.exception))
        self.scan.write_bytes(padded(215))
        closed = []
        real_open = open

        def spy(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            closed.append(handle)
            return handle

        with mock.patch.object(mm, "open", spy, create=True):
            mm._read_scan(self.root)
        self.assertTrue(closed and all(h.closed for h in closed))

    def test_manual_record_refuses_grown_scan_without_writes(self):
        with self.grow_after_preflight(), self.assertRaises(ManualMeetingError) as caught:
            record_manual(self.root, "manual-x", **BASE, today=TODAY)
        self.assertEqual(str(caught.exception), SIZE_ERROR)
        self.assertFalse(self.manual.exists())
        # in-bound control still records
        self.scan.write_bytes(padded(215))
        self.assertEqual(record_manual(self.root, "manual-x", **BASE, today=TODAY)["status"], "scheduled")

    def test_stage_refuses_grown_scan_without_writing(self):
        target = self.root / "content" / "meetings" / "synthetic-1.json"
        with self.grow_after_preflight(), self.assertRaises(MeetingStageError) as caught:
            stage(self.root, "synthetic-1", agency_id="ca-cedar-police", today=TODAY)
        self.assertEqual(str(caught.exception), SIZE_ERROR)
        self.assertFalse(target.exists())

    def test_cli_add_refuses_grown_scan(self):
        out, err = io.StringIO(), io.StringIO()
        argv = ["meetings-add", "--directory", str(self.root), "--id", "manual-y", "--body", BASE["body"], "--agency-id", BASE["agency_id"],
                "--starts-at", BASE["starts_at"], "--agenda-url", BASE["agenda_url"], "--item-title", BASE["agenda_item"],
                "--source-url", BASE["source_url"], "--checked-on", BASE["checked_on"]]
        with self.grow_after_preflight(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                with mock.patch("sys.argv", ["campaign_tool", *argv]):
                    code = cli.main()
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 1, err.getvalue())
        self.assertEqual(out.getvalue(), "")
        self.assertIn(SIZE_ERROR, err.getvalue())
        self.assertFalse(self.manual.exists())

    def test_absent_scan_unchanged(self):
        self.scan.unlink()
        self.assertIsNone(mm._read_scan(self.root))
        self.assertEqual(read_meetings(self.root, allow_empty=True), [])
        self.assertFalse(self.manual.exists())


if __name__ == "__main__":
    unittest.main()
