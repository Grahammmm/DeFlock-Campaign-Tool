"""Health-alert staging: stale partial leftovers never poison, leak or corrupt a recording pass."""
from datetime import timedelta
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
import stat
import subprocess
import sys
import unittest
from unittest import mock

from campaign_tool.records import pipeline_health as ph
from tests.records.test_pipeline_health import NOW, REPO, Base

FINAL = "health-arrival_backlog-arrival_not_consumed.json"
OLD_TMP = "." + FINAL + ".tmp"
REFUSAL = "records pipeline-health: refused alerts_unsafe\n"


class StagingTests(Base):
    def setUp(self):
        super().setUp()
        self.write_stamp(NOW - timedelta(minutes=45))
        self.alerts = self.target_dir / "alerts"
        self.final = self.alerts / FINAL
        self.old = self.alerts / OLD_TMP

    def argv(self):
        return ["--runtime-dir", str(self.runtime), "--target", "t", "--root", str(self.root),
                "--mail-store", str(self.store), "--now", NOW.isoformat(), "--json", "--record-alerts"]

    def run_cli(self):
        cmd = [sys.executable, "-B", "-m", "campaign_tool.records", "pipeline-health", *self.argv()]
        return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=30)

    def run_main(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ph.main(self.argv())
        return code, out.getvalue(), err.getvalue()

    def two_passes(self):
        for _ in range(2):
            done = self.run_cli()
            self.assertEqual((done.returncode, done.stderr), (3, ""))
        info = os.lstat(self.final)
        self.assertEqual((stat.S_IMODE(info.st_mode), info.st_nlink), (0o600, 1))
        self.assertEqual(json.loads(self.final.read_text())["count"], 2)

    def test_stale_partial_mode_does_not_poison_retry(self):
        self.old.write_text('{"partial":')
        os.chmod(self.old, 0o644)
        before = os.lstat(self.old)
        self.two_passes()
        after = os.lstat(self.old)
        self.assertEqual(self.old.read_text(), '{"partial":')
        self.assertEqual((after.st_ino, stat.S_IMODE(after.st_mode)), (before.st_ino, 0o644))

    def test_stale_readonly_partial_does_not_traceback(self):
        self.old.write_text('{"partial":')
        os.chmod(self.old, 0o400)
        self.two_passes()
        self.assertEqual(self.old.read_text(), '{"partial":')
        self.assertEqual(stat.S_IMODE(os.lstat(self.old).st_mode), 0o400)

    def test_stale_hardlink_never_changes_other_name(self):
        other = self.base / "unrelated.txt"
        other.write_text("keep")
        os.chmod(other, 0o640)
        os.link(other, self.old)
        self.two_passes()
        self.assertEqual((other.read_text(), stat.S_IMODE(os.lstat(other).st_mode)), ("keep", 0o640))
        self.assertEqual(os.lstat(self.old).st_ino, os.lstat(other).st_ino)
        self.assertNotEqual(os.lstat(self.final).st_ino, os.lstat(other).st_ino)

    def test_stale_special_entries_are_not_opened(self):
        elsewhere = self.base / "target.txt"
        elsewhere.write_text("keep")
        for kind in ("symlink", "directory", "fifo"):
            with self.subTest(kind=kind):
                for path in self.alerts.iterdir():
                    if path.name.startswith("."):
                        path.unlink() if not path.is_dir() or path.is_symlink() else path.rmdir()
                if self.final.exists():
                    self.final.unlink()
                if kind == "symlink":
                    self.old.symlink_to(elsewhere)
                elif kind == "directory":
                    self.old.mkdir(mode=0o700)
                else:
                    os.mkfifo(self.old, 0o600)
                self.two_passes()
                self.assertEqual(elsewhere.read_text(), "keep")
                mode = os.lstat(self.old).st_mode
                self.assertTrue({"symlink": stat.S_ISLNK, "directory": stat.S_ISDIR, "fifo": stat.S_ISFIFO}[kind](mode))

    def leftovers(self):
        return sorted(name for name in os.listdir(self.alerts) if name.startswith("."))

    def test_failed_stage_preserves_final_and_allows_retry(self):
        self.assertEqual(self.run_main()[0], 3)
        stray = self.alerts / ".unrelated.tmp"
        stray.write_text("x")
        good = self.final.read_bytes()
        for name in ("write", "fsync", "replace"):
            with self.subTest(op=name), mock.patch.object(ph.os, name, side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    ph.write_alerts(self.assess(), self.alerts)
            self.assertEqual(self.final.read_bytes(), good)
            self.assertEqual(self.leftovers(), [".unrelated.tmp"])
        self.assertEqual(self.run_main()[0], 3)
        self.assertEqual(json.loads(self.final.read_text())["count"], 2)
        self.assertEqual(stray.read_text(), "x")

    def test_record_alerts_io_refusal_is_path_free(self):
        for name in ("mkstemp", "write", "replace"):
            with self.subTest(op=name):
                target = mock.patch.object(ph.tempfile if name == "mkstemp" else ph.os, name,
                                           side_effect=PermissionError(13, "denied", str(self.alerts / "SECRET")))
                with target:
                    code, out, err = self.run_main()
                self.assertEqual((code, out, err), (2, "", REFUSAL))
        self.assertFalse(self.final.exists())

    def test_private_mode_and_normal_clear_control(self):
        wrapper = self.alerts / "notify-failed-run_x.json"
        wrapper.write_text('{"wrapper": 1}')
        os.umask(0o000)
        try:
            self.two_passes()
        finally:
            os.umask(0o077)
        for path in (self.runtime / "arrival").iterdir():
            path.unlink()
        self.assertEqual(self.run_main()[2], "")
        self.assertEqual(sorted(os.listdir(self.alerts)),
                         [FINAL[:-5] + ".cleared.json", "health-notify-notify_failed.json",
                          "notify-failed-run_x.json"])
        info = os.lstat(self.alerts / (FINAL[:-5] + ".cleared.json"))
        self.assertEqual((stat.S_IMODE(info.st_mode), info.st_nlink), (0o600, 1))
        self.assertEqual(wrapper.read_text(), '{"wrapper": 1}')


if __name__ == "__main__":
    unittest.main()
