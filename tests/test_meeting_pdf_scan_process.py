"""Real synthetic child converters for the bounded agenda PDF transport.

Each test puts a generated Python script named ``pdftotext`` first on PATH, so the
scanner's exact argv runs a real child. Needs no pdftotext and no network."""
import errno
import gc
import hashlib
import os
import selectors
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import Mock, patch

from campaign_tool import meeting_pdf_scan as pdf

URL = "https://alpha.legistar.com/agenda.pdf"
DATA = b"%PDF-1.4 synthetic"
BIG = 1024 * 1024  # larger than any pipe buffer
CAP = 8 * 1024 * 1024  # pdf.MAX_TEXT_BYTES
ARGV_CHECK = "import os, sys, time, hashlib\nif sys.argv[1:] != ['-layout', '-', '-']:\n    sys.exit(9)\n"
REAL_POPEN = subprocess.Popen
REAL_SELECTOR = selectors.DefaultSelector


def opener(data=DATA):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.getcode.return_value = 200
    response.geturl.return_value = URL
    response.read.return_value = data
    response.getheader.return_value = None
    return Mock(open=Mock(return_value=response))


class ChildTransportTests(unittest.TestCase):
    def setUp(self):
        self.unraisable = []
        hook, sys.unraisablehook = sys.unraisablehook, self.unraisable.append
        self.addCleanup(setattr, sys, "unraisablehook", hook)
        caught = warnings.catch_warnings()
        caught.__enter__()
        self.addCleanup(caught.__exit__, None, None, None)
        warnings.simplefilter("error", ResourceWarning)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = Path(tmp.name)
        self.procs, self.selectors = [], []
        self.spawn = self.start(patch.object(subprocess, "Popen", side_effect=self.record(REAL_POPEN, self.procs)))
        self.start(patch.object(selectors, "DefaultSelector", side_effect=self.record(REAL_SELECTOR, self.selectors)))
        self.start(patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}))

    def start(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    @staticmethod
    def record(real, store):
        def make(*args, **kwargs):
            store.append(real(*args, **kwargs))
            return store[-1]
        return make

    def tearDown(self):
        gc.collect()
        self.assertEqual([u.exc_value for u in self.unraisable], [])

    def child(self, body):
        script = self.bin / "pdftotext"
        script.write_text(f"#!{sys.executable}\n" + ARGV_CHECK + body, encoding="utf-8")
        script.chmod(0o700)

    def assert_reaped(self):
        self.assertEqual(len(self.procs), 1)
        proc = self.procs[0]
        self.assertIsNotNone(proc.returncode)
        self.assertTrue(proc.stdin.closed and proc.stdout.closed)
        with self.assertRaises(ChildProcessError):
            os.waitpid(proc.pid, os.WNOHANG)
        self.assertTrue(self.selectors and all(s.get_map() is None for s in self.selectors))

    def convert(self, data=DATA, **kw):
        try:
            return pdf._convert(data, **kw)
        finally:
            self.assert_reaped()

    def emit(self, size, prefix=b"ALPR "):
        self.child(f"sys.stdin.buffer.read()\nsys.stdout.buffer.write({prefix!r} + b'x' * ({size} - {len(prefix)}))\n")

    def test_exact_argv_stdin_and_pipes(self):
        self.child("sys.stdout.buffer.write(hashlib.sha256(sys.stdin.buffer.read()).hexdigest().encode())\n")
        self.assertEqual(self.convert(), hashlib.sha256(DATA).hexdigest().encode())
        self.assertEqual(self.spawn.call_args.args, (["pdftotext", "-layout", "-", "-"],))
        self.assertEqual(self.spawn.call_args.kwargs, {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                                                       "stderr": subprocess.DEVNULL})
        self.assertEqual((pdf.MAX_TEXT_BYTES, pdf.CONVERT_SECONDS), (8 * 1024 * 1024, 30))

    def test_output_cap_exact_limit_succeeds(self):
        self.emit(CAP)
        self.assertEqual(len(self.convert()), CAP)

    def test_output_cap_plus_one_refused(self):
        self.emit(CAP + 1)
        with self.assertRaises(pdf._TextTooLarge):
            self.convert()

    def test_empty_output_is_legal(self):
        self.child("sys.stdin.buffer.read()\n")
        self.assertEqual(self.convert(), b"")

    def test_cap_counts_utf8_bytes_across_chunks(self):
        self.child("sys.stdin.buffer.read()\nsys.stdout.buffer.write('é'.encode() * 100_000)\n")
        self.assertEqual(self.convert(limit=200_000).decode("utf-8"), "é" * 100_000)  # 200,000 bytes

    def test_utf8_characters_under_cap_but_bytes_over_refused(self):
        self.child("sys.stdin.buffer.read()\nsys.stdout.buffer.write('é'.encode() * 100_000)\n")
        with self.assertRaises(pdf._TextTooLarge):
            self.convert(limit=199_999)

    def test_child_writes_before_reading_input_does_not_deadlock(self):
        self.child(f"sys.stdout.buffer.write(b'y' * {BIG}); sys.stdout.buffer.flush()\n"
                   "sys.stdout.buffer.write(hashlib.sha256(sys.stdin.buffer.read()).hexdigest().encode())\n")
        data = b"%PDF-" + b"z" * BIG
        self.assertEqual(self.convert(data, seconds=20), b"y" * BIG + hashlib.sha256(data).hexdigest().encode())

    def assert_deadline(self, body, data=DATA):
        self.child(body)
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.convert(data, seconds=0.5)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(self.procs[0].returncode, -9)

    def test_deadline_covers_child_not_reading_stdin(self):
        self.assert_deadline("time.sleep(60)\n", b"%PDF-" + b"z" * BIG)

    def test_deadline_covers_silent_running_child(self):
        self.assert_deadline("sys.stdin.buffer.read()\ntime.sleep(60)\n")

    def test_deadline_covers_wait_after_stdout_eof(self):
        self.assert_deadline("sys.stdin.buffer.read()\nos.close(1)\ntime.sleep(60)\n")

    def test_deadline_not_refreshed_by_trickling_output(self):
        self.assert_deadline("sys.stdin.buffer.read()\nwhile True:\n"
                             "    sys.stdout.buffer.write(b'a'); sys.stdout.buffer.flush(); time.sleep(0.05)\n")

    def test_nonzero_exit_before_and_after_output_refused(self):
        for body in ("sys.stdin.buffer.read()\nsys.exit(3)\n",
                     "sys.stdin.buffer.read()\nsys.stdout.buffer.write(b'ALPR'); sys.stdout.flush()\nsys.exit(3)\n"):
            with self.subTest(body=body):
                self.procs.clear()
                self.child(body)
                with self.assertRaises(subprocess.CalledProcessError):
                    self.convert()

    def test_early_stdin_close_is_broken_pipe(self):
        self.child("os.close(0)\nsys.stdout.buffer.write(b'ALPR')\n")
        with self.assertRaises(BrokenPipeError):
            self.convert(b"%PDF-" + b"z" * BIG)

    def test_injected_read_and_write_errors_close_everything(self):
        for name in ("read", "write"):
            with self.subTest(name=name):
                self.procs.clear()
                self.selectors.clear()
                self.child("sys.stdout.buffer.write(b'ALPR'); sys.stdout.flush()\nsys.stdin.buffer.read()\ntime.sleep(60)\n")
                real = getattr(os, name)

                def failing(fd, *a, _real=real, _name=name):
                    pipe = self.procs and (self.procs[0].stdout if _name == "read" else self.procs[0].stdin)
                    if pipe and not pipe.closed and fd == pipe.fileno():
                        raise OSError(errno.EIO, "injected")
                    return _real(fd, *a)
                with patch.object(os, name, side_effect=failing), self.assertRaises(OSError) as raised:
                    self.convert(seconds=20)
                self.assertEqual(raised.exception.errno, errno.EIO)

    def test_scanner_overflow_after_keyword_never_becomes_a_lead(self):
        self.emit(CAP + 1)
        http = opener()
        scans = pdf.AgendaScans(http)
        client = Mock(source="legistar", client="alpha")
        self.assertEqual(scans.meeting(client, "Alpha Council", URL), {})
        self.assertEqual(scans.meeting(client, "Alpha Council", URL), {})
        self.assertEqual(http.open.call_count, 1)
        self.assertEqual(scans.counts, {"failed": 1})
        self.assertEqual(scans.cache[(("legistar", "alpha", "Alpha Council"), URL)],
                         {"pdf_scan": "failed", "pdf_sha256": hashlib.sha256(DATA).hexdigest(),
                          "pages": 0, "pages_matched": [], "matched_terms": []})
        self.assert_reaped()

    def test_scanner_refusals_and_missing_converter(self):
        for body in ("os.close(0)\nsys.stdout.buffer.write(b'ALPR')\n",
                     "sys.stdin.buffer.read()\nsys.stdout.buffer.write(b'ALPR')\nsys.exit(1)\n"):
            with self.subTest(body=body):
                self.procs.clear()
                self.child(body)
                data = b"%PDF-" + b"z" * BIG
                self.assertEqual(pdf.scan(URL, opener(data), platform_host_rule=lambda u: True),
                                 {"pdf_scan": "failed", "pdf_sha256": hashlib.sha256(data).hexdigest(),
                                  "pages": 0, "pages_matched": [], "matched_terms": []})
                self.assert_reaped()
        (self.bin / "pdftotext").unlink()
        with patch.dict(os.environ, {"PATH": str(self.bin)}):
            self.assertEqual(pdf.scan(URL, opener(), platform_host_rule=lambda u: True)["pdf_scan"],
                             "unavailable_pdftotext")

    def test_scanner_success_through_real_child(self):
        self.child("sys.stdin.buffer.read()\nsys.stdout.buffer.write(b'Budget\\fParks\\fALPR renewal\\f')\n")
        self.assertEqual(pdf.scan(URL, opener(), platform_host_rule=lambda u: True),
                         {"pdf_scan": "ok", "pdf_sha256": hashlib.sha256(DATA).hexdigest(),
                          "pages": 3, "pages_matched": [3], "matched_terms": ["alpr"]})
        self.assert_reaped()


if __name__ == "__main__":
    unittest.main()
