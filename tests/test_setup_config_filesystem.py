"""Synthetic filesystem-type admission checks for campaign.json in guided setup; offline."""
import io
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from campaign_tool import setup_cmd

FIXED = ("campaign.json could not be read as a supported campaign configuration; "
         "nothing changed. Repair it and run setup again.")
ANSWERS = {"name": "Synthetic Campaign", "county": "San Luis Obispo", "state": "CA", "location": ""}
FLAGS = ["--name", "Synthetic Campaign", "--county", "San Luis Obispo", "--state", "CA", "--yes"]
DOWNSTREAM = ("campaign_tool.cli.initialize", "campaign_tool.cli.read_config", "campaign_tool.kit.build_kit",
              "campaign_tool.site.build_site", "campaign_tool.cli.doctor_report")
REPO = Path(__file__).resolve().parent.parent


class FilesystemTypeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "campaign"
        self.root.mkdir()
        self.entry = self.root / "campaign.json"

    def refuse(self, names=None):
        before = sorted(os.listdir(self.root))
        lines = []
        with ExitStack() as stack:
            mocks = {n: stack.enter_context(patch(n)) for n in DOWNSTREAM}
            with self.assertRaises(ValueError) as ctx:
                setup_cmd.run_setup(self.root, ANSWERS, out=lines.append)
        for mock in mocks.values():
            mock.assert_not_called()
        self.assertEqual(str(ctx.exception), f"Stopped at step 1 (Campaign file): {FIXED}. "
                         "Fix it and run setup again; finished steps are kept.")
        self.assertEqual(lines, ["Step 1 of 4: Campaign file … stopped"])
        self.assertNotIn(str(self.root), str(ctx.exception))
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_writerless_fifo_stops_before_open_direct_and_cli(self):
        os.mkfifo(self.entry)
        self.refuse()
        proc = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "setup", "--directory",
                               str(self.root)] + FLAGS, cwd=REPO, capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout.count("stopped"), 1)
        self.assertIn(FIXED, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(os.listdir(self.root), ["campaign.json"])

    def test_fifo_symlink_and_available_bytes_do_not_bypass_type_gate(self):
        fifo = Path(self.tmp.name) / "outside.fifo"
        os.mkfifo(fifo)
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.write(fd, b'{"name": "x"}')
            self.entry.symlink_to(fifo)
            self.refuse()
        finally:
            os.close(fd)

    def test_only_absence_initializes(self):
        with ExitStack() as stack:
            init = stack.enter_context(patch("campaign_tool.cli.initialize"))
            read = stack.enter_context(patch("campaign_tool.cli.read_config"))
            for name in ("build_kit",):
                stack.enter_context(patch("campaign_tool.kit." + name))
            stack.enter_context(patch("campaign_tool.site.build_site"))
            stack.enter_context(patch("campaign_tool.cli.doctor_report", return_value={"next_actions": []}))
            setup_cmd.run_setup(self.root, ANSWERS, out=lambda _s: None)
            init.assert_called_once()
            read.assert_not_called()

    def test_unusable_entries_are_refused_not_initialized(self):
        cases = {
            "directory": lambda: self.entry.mkdir(),
            "dangling": lambda: self.entry.symlink_to(self.root / "missing"),
            "loop": lambda: self.entry.symlink_to(self.entry),
            "dirlink": lambda: self.entry.symlink_to(self.root),
        }
        for name, build in cases.items():
            with self.subTest(name):
                build()
                self.refuse()
                if self.entry.is_dir() and not self.entry.is_symlink():
                    self.entry.rmdir()
                else:
                    self.entry.unlink()

    def test_unix_socket_refused(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("no unix sockets")
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(self.entry))
        self.refuse()

    def test_entry_inspection_errors_use_fixed_private_safe_refusal(self):
        real_lstat, real_stat = Path.lstat, Path.stat
        for which in ("lstat", "stat"):
            with self.subTest(which):
                self.entry.write_text("{}")
                err = PermissionError(13, "secret-marker-path /private/x")

                def boom(self_path, *a, **k):
                    if self_path == self.entry:
                        raise err
                    return (real_lstat if which == "lstat" else real_stat)(self_path, *a, **k)
                with patch.object(Path, which, boom):
                    self.refuse()
                self.entry.unlink()

    def test_engine_refusal_still_wins_before_type_inspection(self):
        os.mkfifo(self.entry)
        with ExitStack() as stack:
            stack.enter_context(patch("campaign_tool.git_exposure.refuse_engine_directory",
                                      side_effect=ValueError("engine diagnostic")))
            read = stack.enter_context(patch("campaign_tool.cli.read_config"))
            with self.assertRaises(ValueError) as ctx:
                setup_cmd.run_setup(self.root, ANSWERS, out=lambda _s: None)
        self.assertIn("engine diagnostic", str(ctx.exception))
        self.assertNotIn(FIXED, str(ctx.exception))
        read.assert_not_called()

    def test_regular_file_and_symlink_keep_shared_validation_and_resume(self):
        os.mkfifo(self.entry)
        self.refuse()
        self.entry.unlink()
        from types import SimpleNamespace
        from campaign_tool import cli
        ref = Path(self.tmp.name) / "ref"
        with patch("sys.stdout", new=io.StringIO()):
            cli.initialize(SimpleNamespace(**ANSWERS), ref)
        real = Path(self.tmp.name) / "real.json"
        real.write_bytes((ref / "campaign.json").read_bytes())
        self.entry.symlink_to(real)
        before = real.read_bytes()
        with ExitStack() as stack:
            stack.enter_context(patch("campaign_tool.kit.build_kit"))
            stack.enter_context(patch("campaign_tool.site.build_site"))
            stack.enter_context(patch("campaign_tool.cli.doctor_report", return_value={"next_actions": []}))
            result = setup_cmd.run_setup(self.root, ANSWERS, out=lambda _s: None)
        self.assertEqual(result["steps"], ["kept", "done", "done", "done"])
        self.assertEqual(real.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
