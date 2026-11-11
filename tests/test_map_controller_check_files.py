"""Map --check admits only regular descriptors and never blocks on a FIFO."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import map_controller

ROOT = Path(__file__).resolve().parent.parent
FIXED = ("Map check stopped: config file missing, unreadable, over 2 MB, "
         "not UTF-8 JSON or not an object\n")


def run_check(path):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = map_controller.check(str(path))
    return code, out.getvalue(), err.getvalue()


class MapCheckFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.good = self.dir / "good.json"
        self.good.write_text(json.dumps({"schema_version": 1}))

    def test_fifo_without_writer_refuses_promptly_via_both_entrypoints(self):
        fifo = self.dir / "fifo"
        os.mkfifo(fifo)
        for cmd in ([sys.executable, "-m", "campaign_tool.map_controller", "--check", str(fifo)],
                    [sys.executable, str(ROOT / "campaign_tool" / "map_controller.py"), "--check", str(fifo)]):
            proc = subprocess.run(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=20)
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (2, "", FIXED))

    def test_nonregular_descriptor_refused_before_read(self):
        fifo = self.dir / "fifo"
        os.mkfifo(fifo)
        link = self.dir / "fifolink"
        link.symlink_to(fifo)
        with mock.patch.object(map_controller.json, "loads", side_effect=AssertionError("parsed")):
            for target in (fifo, link, self.dir):
                self.assertEqual(run_check(target), (2, "", FIXED))
            self.assertEqual(run_check(self.dir / "missing"), (2, "", FIXED))

    def test_regular_and_symlink_to_regular_accepted(self):
        link = self.dir / "link.json"
        link.symlink_to(self.good)
        want = run_check(self.good)
        self.assertEqual(want[0] in (0, 1), True)
        self.assertEqual(run_check(link), want)

    def test_descriptor_closed_on_every_path(self):
        opened = []
        real_open = os.open

        def spy(*a, **k):
            fd = real_open(*a, **k)
            opened.append(fd)
            return fd

        def closed(fd):
            with self.assertRaises(OSError):
                os.fstat(fd)

        fifo = self.dir / "fifo"
        os.mkfifo(fifo)
        with mock.patch.object(map_controller.os, "open", spy):
            run_check(self.good)
            run_check(fifo)
            with mock.patch.object(map_controller.os, "fdopen", side_effect=OSError("boom")):
                self.assertEqual(run_check(self.good), (2, "", FIXED))
            with mock.patch.object(map_controller.os, "fdopen", side_effect=MemoryError):
                with self.assertRaises(MemoryError):
                    run_check(self.good)
            with mock.patch.object(map_controller.os, "fstat", side_effect=OSError("boom")):
                self.assertEqual(run_check(self.good), (2, "", FIXED))
            bad = self.dir / "bad.json"
            bad.write_text("[" * 100000)
            self.assertEqual(run_check(bad), (2, "", FIXED))
        self.assertEqual(len(opened), 6)
        for fd in opened:
            closed(fd)

    def test_cap_plus_one_actual_read(self):
        cap = map_controller.MAX_BYTES
        pad = self.dir / "cap.json"
        body = b'{"x": "' 
        pad.write_bytes(body + b"a" * (cap - len(body) - 2) + b'"}')
        self.assertEqual(pad.stat().st_size, cap)
        self.assertEqual(run_check(pad)[0], 1)  # parsed (unknown key problem), not refused
        pad.write_bytes(pad.read_bytes() + b" ")
        self.assertEqual(run_check(pad), (2, "", FIXED))

    def test_check_does_not_touch_stdin(self):
        with mock.patch.object(sys, "stdin", None):
            self.assertIn(run_check(self.good)[0], (0, 1))


if __name__ == "__main__":
    unittest.main()
