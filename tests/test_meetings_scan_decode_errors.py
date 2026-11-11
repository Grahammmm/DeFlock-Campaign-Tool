"""RF0300-2: decoder RecursionError / non-syntax ValueError in kit/meetings.json become file-named domain errors (synthetic, offline)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import meetings_manual as mm
from campaign_tool.meeting_stage import MeetingStageError, stage
from campaign_tool.meetings_manual import ManualMeetingError, cancel_manual, read_meetings, record_manual
from campaign_tool.today import _unstaged_meetings

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 10)
BASE = dict(body="Smallville Water Board", agency_id="ca-cedar-police", starts_at="2026-11-20T18:30:00-08:00",
            agenda_url="https://example.invalid/agenda", agenda_item="Item 5", source_url="https://example.invalid/p",
            checked_on="2026-11-09")
HEAD = b'{"schema_version": 1, "meetings": [], "note": '
DEEP_ARRAY = HEAD + b"[" * 20000 + b"]" * 20000 + b"}"
DEEP_OBJECT = HEAD + b'{"a": ' * 10000 + b"1" + b"}" * 10000 + b"}"
BIG_INT = HEAD + b"9" * 5000 + b"}"
NEG_BIG_INT = HEAD + b"-" + b"9" * 5000 + b"}"
CASES = {"array": (DEEP_ARRAY, "RecursionError"), "object": (DEEP_OBJECT, "RecursionError"),
         "int": (BIG_INT, "ValueError"), "negint": (NEG_BIG_INT, "ValueError")}


def message(kind):
    return f"kit/meetings.json: not UTF-8 JSON ({kind})"


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(Path(root).rglob("*")) if p.is_file()}


class ScanDecodeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-scan-decode-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.scan = self.root / "kit" / "meetings.json"
        self.scan.parent.mkdir(exist_ok=True)
        self.manual = self.root / "kit" / "meetings-manual.json"

    def put(self, name):
        raw, kind = CASES[name]
        self.scan.write_bytes(raw)
        return kind

    def test_real_scan_decoder_failures_become_domain_errors(self):
        if sys.get_int_max_str_digits() not in range(1, 5000):
            self.skipTest("interpreter integer conversion limit is not active below 5000 digits")
        for name in CASES:
            kind = self.put(name)
            with self.subTest(name), self.assertRaises(ManualMeetingError) as caught:
                mm._read_scan(self.root)
            self.assertEqual(str(caught.exception), message(kind))
            self.assertTrue(caught.exception.__suppress_context__)
            with self.assertRaises(ManualMeetingError):
                read_meetings(self.root)

    def test_ordinary_nested_metadata_still_accepted(self):
        self.scan.write_bytes(HEAD + b"[" * 50 + b"]" * 50 + b', "n": ' + b"9" * 100 + b"}")
        self.assertEqual(mm._read_scan(self.root), [])

    def test_lifecycle_refuses_before_save(self):
        record_manual(self.root, "manual-keep", **BASE, today=TODAY)
        self.scan.write_bytes(b'{"schema_version": 1, "meetings": []}')
        for name in ("array", "object", "int"):
            kind = self.put(name)
            before = snapshot(self.root)
            with self.subTest(name), mock.patch.object(mm, "_save", side_effect=AssertionError("saved")):
                for call in (lambda: record_manual(self.root, "manual-new", **BASE, today=TODAY),
                             lambda: record_manual(self.root, "manual-keep", **BASE, replace=True, on="2026-11-10",
                                                   reason="fix", today=TODAY),
                             lambda: cancel_manual(self.root, "manual-keep", on="2026-11-10", reason="gone", today=TODAY)):
                    with self.assertRaises(ManualMeetingError) as caught:
                        call()
                    self.assertEqual(str(caught.exception), message(kind))
            self.assertEqual(snapshot(self.root), before)

    def test_stage_refuses_before_output(self):
        self.put("array")
        before = snapshot(self.root)
        with self.assertRaises(MeetingStageError) as caught:
            stage(self.root, "synthetic-1", agency_id="ca-cedar-police", today=TODAY)
        self.assertEqual(str(caught.exception), message("RecursionError"))
        self.assertEqual(snapshot(self.root), before)

    def run_cli(self, *argv):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *argv], capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parents[1], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_real_cli_one_stopped_line(self):
        add = ["meetings-add", "--directory", str(self.root), "--id", "manual-y", "--body", BASE["body"], "--agency-id",
               BASE["agency_id"], "--starts-at", "2099-11-20T18:30:00-08:00", "--agenda-url", BASE["agenda_url"],
               "--item-title", BASE["agenda_item"], "--source-url", BASE["source_url"], "--checked-on", "2026-11-09"]
        cancel = ["meetings-add", "--directory", str(self.root), "--id", "manual-y", "--cancel", "--on", "2026-11-10",
                  "--reason", "gone"]
        staging = ["meetings-stage", "--directory", str(self.root), "--id", "synthetic-1", "--agency-id", BASE["agency_id"]]
        for name in ("array", "int"):
            kind = self.put(name)
            before = snapshot(self.root)
            for argv in (add, cancel, staging):
                with self.subTest(name, verb=argv[0]):
                    done = self.run_cli(*argv)
                    self.assertEqual(done.returncode, 1, done.stderr[-400:])
                    self.assertEqual(done.stdout, "")
                    self.assertEqual(done.stderr, f"Stopped: {message(kind)}\n")
            self.assertEqual(snapshot(self.root), before)

    def test_today_keeps_one_unreadable_item(self):
        self.put("array")
        items = _unstaged_meetings(self.root, TODAY, "", set())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "unreadable")
        self.assertIn(message("RecursionError"), items[0]["what"])

    def test_catch_is_narrow_and_existing_diagnostics_stay(self):
        self.scan.write_bytes(b'{"schema_version": 1, "meetings": []}')
        for exc in (RuntimeError("x"), MemoryError()):
            with mock.patch.object(mm.json, "loads", side_effect=exc), self.assertRaises(type(exc)):
                mm._read_scan(self.root)
        with mock.patch.object(mm.json, "loads", side_effect=RecursionError("secret-canary")):
            with self.assertRaises(ManualMeetingError) as caught:
                mm._read_scan(self.root)
        self.assertEqual(str(caught.exception), message("RecursionError"))
        for raw, kind in ((b"\xff\xfe", "UnicodeDecodeError"), (b"{nope", "JSONDecodeError")):
            self.scan.write_bytes(raw)
            with self.assertRaises(ManualMeetingError) as caught:
                mm._read_scan(self.root)
            self.assertEqual(str(caught.exception), message(kind))


if __name__ == "__main__":
    unittest.main()
