"""External manual journal: synthetic read-only snapshot, real loopback requests."""
import contextlib
import hashlib
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import admin_panel as ap
from campaign_tool.records import console_manual_events as cm
from tests.records import test_console_manual_events as fixture


class ManualDirectory(unittest.TestCase):
    request = fixture.ManualEvents.request
    form = fixture.ManualEvents.form
    close_server = fixture.ManualEvents.close_server

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "snapshot"
        self.directory = Path(self.tmp.name) / "events"
        self.directory.mkdir(mode=0o700)
        fixture.setup(self.project)
        self.journal = self.directory / "manual-events.jsonl"
        self.server = ap.build_server(self.project, fixture.RUN, actor="Operator <one>", system=False,
                                      manual_events_dir=self.directory)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.origin = "http://127.0.0.1:%d" % self.server.server_port
        self.readonly(True)
        self.addCleanup(self.readonly, False)
        self.before = self.tree()

    def readonly(self, enabled):
        for path in [self.project, *self.project.rglob("*")]:
            os.chmod(path, (0o500 if enabled else 0o700) if path.is_dir() else (0o400 if enabled else 0o600))

    def tree(self):
        return {str(p.relative_to(self.project)): (p.stat().st_mode,
                hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
                for p in [self.project, *self.project.rglob("*")]}

    def tearDown(self):
        self.assertEqual(self.tree(), self.before)

    def test_record_duplicate_conflict_and_correction(self):
        fields = self.form()
        with patch.object(cm.tl, "write_run_timeline", side_effect=AssertionError("snapshot write")):
            status, page, _ = self.request("POST", "/manual-event", fields)
            self.assertEqual(status, 200)
            self.assertIn("Recorded (event " + fields["id"] + ")", page)
            self.assertIn("after the next data refresh", page)
            self.assertIn('href="/agency?agency=agency-one"', page)
            original = self.journal.read_bytes()
            self.assertEqual(len(original.splitlines()), 1)
            self.assertEqual(self.request("POST", "/manual-event", fields)[0], 200)
            self.assertEqual(self.journal.read_bytes(), original)
            self.assertEqual(self.request("POST", "/manual-event", fields | {"summary": "changed"})[0], 409)
            self.assertEqual(self.journal.read_bytes(), original)
            correction = self.form() | {"corrects": fields["id"], "summary": "Correction"}
            self.assertEqual(self.request("POST", "/manual-event", correction)[0], 200)
            self.assertTrue(self.journal.read_bytes().startswith(original))
            self.assertEqual(len(self.journal.read_bytes().splitlines()), 2)
        for name in ("manual-events.jsonl", "manual-events.lock"):
            self.assertEqual((self.directory / name).stat().st_mode & 0o777, 0o600)

    def test_unknown_correction_and_malformed_journal(self):
        fields = self.form()
        self.assertEqual(self.request("POST", "/manual-event", fields | {"corrects": "absent"})[0], 409)
        for raw in (b'{}\n', b'{"partial":'):
            self.journal.write_bytes(raw)
            os.chmod(self.journal, 0o600)
            self.assertEqual(self.request("POST", "/manual-event", fields)[0], 503)
            self.assertEqual(self.journal.read_bytes(), raw)

    def test_cancel_expired_stale_and_guards_write_nothing(self):
        fields = self.form()
        self.request("GET", "/agency?agency=agency-one")  # Cancel is navigation.
        self.assertFalse(self.journal.exists())
        with patch.object(cm.time, "time", return_value=cm.time.time() + cm.TTL + 2):
            self.assertEqual(self.request("POST", "/manual-event", fields)[0], 410)
        with patch.object(cm, "_register", return_value=({"requests": []}, "changed")):
            self.assertEqual(self.request("POST", "/manual-event", fields)[0], 409)
        self.assertEqual(self.request("POST", "/manual-event", fields | {"csrf_token": "bad"})[0], 403)
        self.assertEqual(self.request("POST", "/manual-event", fields, {"Origin": "https://evil.test"})[0], 403)
        self.assertFalse(self.journal.exists())

    def test_symlink_and_special_journal_or_lock_refused(self):
        fields = self.form()
        target = Path(self.tmp.name) / "untouched"
        target.write_bytes(b"unchanged")
        for name in ("manual-events.jsonl", "manual-events.lock"):
            path = self.directory / name
            path.unlink(missing_ok=True)
            path.symlink_to(target)
            self.assertEqual(self.request("POST", "/manual-event", fields)[0], 503)
            self.assertEqual(target.read_bytes(), b"unchanged")
            path.unlink()
            os.mkfifo(path, 0o600)
            self.assertEqual(self.request("POST", "/manual-event", fields)[0], 503)
            path.unlink()

    def test_invalid_directory_cli_and_builder(self):
        link = Path(self.tmp.name) / "link"
        link.symlink_to(self.directory, target_is_directory=True)
        broad = Path(self.tmp.name) / "broad"
        broad.mkdir(mode=0o750)
        broad.chmod(0o750)
        private_parent = Path(self.tmp.name)
        private_parent.chmod(0o700)
        cases = [link, broad, self.project, self.project / "run-outputs", private_parent,
                 Path("relative"), private_parent / "missing", private_parent / "file"]
        cases[-1].write_text("not a directory")
        for directory in cases:
            with self.subTest(directory=directory):
                with self.assertRaisesRegex(ap.PanelError, "manual_events_dir_invalid"):
                    ap.build_server(self.project, fixture.RUN, actor="operator", manual_events_dir=directory)
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    result = ap.main(["--project", str(self.project), "--run-id", fixture.RUN,
                                      "--actor", "operator", "--manual-events", str(directory)])
                self.assertEqual(result, 2)
                self.assertEqual(output.getvalue().strip(), "manual_events_dir_invalid")
        info = self.directory.stat()
        with patch.object(cm.os, "geteuid", return_value=info.st_uid + 1):
            with self.assertRaisesRegex(ap.PanelError, "manual_events_dir_invalid"):
                ap.build_server(self.project, fixture.RUN, actor="operator", manual_events_dir=self.directory)


if __name__ == "__main__":
    unittest.main()
