"""Agency-card --check descriptor admission: nonregular inputs are refused, never waited on; synthetic files only."""
import contextlib
import errno
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import agency_cards as cards

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "examples/fictional-campaign/agency-cards.json").read_text())
REFUSED = ("Agency-card check stopped: config file missing, unreadable, over 2 MB, "
           "not UTF-8 JSON or not an object\n")
CANARY = "Fictional-check-canary"
REAL_OPEN, REAL_FSTAT, REAL_FDOPEN = os.open, os.fstat, os.fdopen


def run_check(path):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cards.check(str(path))
    return code, out.getvalue(), err.getvalue()


def closed(fd):
    try:
        REAL_FSTAT(fd)
    except OSError as exc:
        return exc.errno == errno.EBADF
    return False


def snapshot(path):
    st = os.stat(path)
    return st.st_mode, st.st_ino, st.st_mtime_ns, Path(path).read_bytes()


class Spy(io.BufferedReader):
    """Real stream that counts reads and effective closes."""
    def __init__(self, fd, mode="rb"):
        super().__init__(io.FileIO(fd, "rb"))
        self.reads, self.closes = [], 0

    def read(self, size=-1):
        self.reads.append(size)
        return super().read(size)

    def close(self):
        if not self.closed:
            self.closes += 1
        super().close()


class FailingRead(Spy):
    def read(self, size=-1):
        raise OSError(errno.EIO, CANARY)


class CheckFiles(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.opened = []

    def write(self, value, name="config.json"):
        path = self.dir / name
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
        return path

    def fifo(self, name="pipe.json"):
        path = self.dir / name
        os.mkfifo(path)
        return path

    def recording_open(self, after=None):
        def opener(path, flags, *args, **kwargs):
            fd = REAL_OPEN(path, flags, *args, **kwargs)
            self.opened.append(fd)
            if after:
                after(fd)
            return fd
        return mock.patch.object(cards.os, "open", side_effect=opener)

    def assert_refused(self, result):
        self.assertEqual(result, (2, "", REFUSED))

    def test_fifo_without_writer_refuses_via_module_and_script(self):
        fifo = self.fifo()
        for argv in ([sys.executable, "-B", "-m", "campaign_tool.agency_cards", "--check", str(fifo)],
                     [sys.executable, "-B", str(ROOT / "campaign_tool/agency_cards.py"), "--check", str(fifo)]):
            with self.subTest(argv=argv[2:4]):
                done = subprocess.run(argv, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True,
                                      text=True, timeout=30)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (2, "", REFUSED))

    def test_nonregular_descriptor_is_refused_before_payload_read(self):
        fifo = self.fifo()
        link = self.dir / "fifo-link.json"
        link.symlink_to(fifo)
        sock_dir = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(sock_dir.cleanup)
        sock_path = Path(sock_dir.name) / "s"
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(sock_path))
        dangling = self.dir / "dangling.json"
        dangling.symlink_to(self.dir / "absent.json")
        for label, path in (("fifo", fifo), ("fifo link", link), ("directory", self.dir),
                            ("socket", sock_path), ("missing", self.dir / "absent.json"), ("dangling", dangling)):
            with self.subTest(label=label), self.recording_open(), \
                    mock.patch.object(cards.os, "fdopen", wraps=REAL_FDOPEN) as fdopen, \
                    mock.patch.object(cards.json, "loads") as loads, \
                    mock.patch.object(cards, "render") as render, \
                    mock.patch.object(cards, "ASSET") as asset:
                self.assert_refused(run_check(path))
                fdopen.assert_not_called()
                loads.assert_not_called()
                render.assert_not_called()
                self.assertEqual(asset.mock_calls, [])
                self.assertTrue(all(closed(fd) for fd in self.opened), label)
            self.opened.clear()

    def test_regular_and_symlink_inputs_keep_healthy_and_problem_outputs(self):
        bad = dict(CONFIG, source_page="x/y", default_fragment="Bad Slug", extra=1)
        good, broken = self.write(CONFIG, "good.json"), self.write(bad, "bad.json")
        expected = cards.problems(bad)
        self.assertGreater(len(expected), 2)
        for name, target in (("good", good), ("bad", broken)):
            link = self.dir / (name + "-link.json")
            link.symlink_to(target)
            before, members = snapshot(target), sorted(os.listdir(self.dir))
            for path in (target, link):
                with self.subTest(path=path.name):
                    if name == "good":
                        self.assertEqual(run_check(path), (0, "problems=0\n", ""))
                    else:
                        self.assertEqual(run_check(path),
                                         (1, "\n".join(expected + [f"problems={len(expected)}"]) + "\n", ""))
            self.assertEqual(snapshot(target), before)
            self.assertEqual(sorted(os.listdir(self.dir)), members)

    def test_post_open_replacement_does_not_reopen_pathname(self):
        path = self.write(CONFIG)
        replacement = self.fifo("replacement.json")

        def swap_in_fifo(_fd):
            os.replace(replacement, path)

        with self.recording_open(after=swap_in_fifo):
            self.assertEqual(run_check(path), (0, "problems=0\n", ""))
        self.assertTrue(closed(self.opened[0]))
        self.assertEqual(len(self.opened), 1)
        path = self.write(CONFIG, "second.json")
        other = self.write(b"[]", "other.json")
        with self.recording_open(after=lambda _fd: os.replace(other, path)):
            self.assertEqual(run_check(path), (0, "problems=0\n", ""))

    def test_fifo_descriptor_substituted_at_acquisition_fails_fstat_admission(self):
        path = self.write(CONFIG)
        fifo = self.fifo()

        def opener(_path, flags, *args, **kwargs):
            fd = REAL_OPEN(fifo, flags, *args, **kwargs)
            self.opened.append(fd)
            return fd

        with mock.patch.object(cards.os, "open", side_effect=opener), \
                mock.patch.object(cards.os, "fdopen", wraps=REAL_FDOPEN) as fdopen:
            self.assert_refused(run_check(path))
            fdopen.assert_not_called()
        self.assertTrue(closed(self.opened[0]))

    def test_descriptor_lifetime_before_and_after_stream_transfer(self):
        valid, bad = self.write(CONFIG, "valid.json"), self.write(dict(CONFIG, extra=1), "bad.json")
        sentinel = REAL_OPEN(valid, os.O_RDONLY)
        self.addCleanup(lambda: closed(sentinel) or os.close(sentinel))
        streams = []

        def spy_fdopen(cls):
            def make(fd, mode="rb"):
                streams.append(cls(fd, mode))
                return streams[-1]
            return mock.patch.object(cards.os, "fdopen", side_effect=make)

        cases = [("success", valid, Spy, 0), ("validation problems", bad, Spy, 1),
                 ("read OSError", valid, FailingRead, 2)]
        for label, path, cls, code in cases:
            with self.subTest(label=label), self.recording_open(), spy_fdopen(cls):
                result = run_check(path)
                self.assertEqual(result[0], code)
                self.assertNotIn(CANARY, result[1] + result[2])
                self.assertTrue(closed(self.opened[-1]))
                self.assertEqual(streams[-1].closes, 1)
                self.assertTrue(streams[-1].closed)
        for label, raw in (("not UTF-8", b'{"a": "\xff"}'), ("not JSON", b"{"),
                           ("deep", b"[" * 100000 + b"]" * 100000), ("non-finite", b'{"a": NaN}')):
            with self.subTest(label=label), self.recording_open(), spy_fdopen(Spy):
                self.assert_refused(run_check(self.write(raw, "raw.json")))
                self.assertTrue(closed(self.opened[-1]))
                self.assertEqual(streams[-1].closes, 1)
        with self.subTest(label="fstat OSError"), self.recording_open(), \
                mock.patch.object(cards.os, "fstat", side_effect=OSError(errno.EIO, CANARY)):
            self.assert_refused(run_check(valid))
            self.assertTrue(closed(self.opened[-1]))
        for label, error in (("fdopen OSError", OSError(errno.EIO, CANARY)), ("fdopen MemoryError", MemoryError())):
            with self.subTest(label=label), self.recording_open(), \
                    mock.patch.object(cards.os, "fdopen", side_effect=error):
                if isinstance(error, MemoryError):
                    with self.assertRaises(MemoryError):
                        run_check(valid)
                else:
                    self.assert_refused(run_check(valid))
                self.assertTrue(closed(self.opened[-1]))
        with self.subTest(label="unexpected exception"), self.recording_open(), \
                mock.patch.object(cards, "problems", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_check(valid)
            self.assertTrue(closed(self.opened[-1]))
        self.assertFalse(closed(sentinel))
        self.assertTrue(os.read(sentinel, 1))

    def test_nonblocking_capability_missing_refuses_without_opening(self):
        path = self.write(CONFIG)
        with mock.patch.object(cards.os, "O_NONBLOCK", new=None), mock.patch.object(cards.os, "open") as opener:
            self.assert_refused(run_check(path))
            opener.assert_not_called()

    def test_open_requests_nonblocking_read_only(self):
        with self.recording_open() as opener:
            run_check(self.write(CONFIG))
        flags = opener.call_args.args[1]
        self.assertTrue(flags & os.O_NONBLOCK)
        self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)

    def test_cap_plus_one_read_and_growth_still_refuse_before_decode(self):
        raw = json.dumps(CONFIG).encode()
        exact = self.write(raw, "exact.json")
        with mock.patch.object(cards, "MAX_BYTES", len(raw)):
            self.assertEqual(run_check(exact), (0, "problems=0\n", ""))
            self.assert_refused(run_check(self.write(raw + b" ", "plus-one.json")))
        multibyte = self.write('{"a": "éé"}'.encode(), "multibyte.json")
        size = multibyte.stat().st_size
        self.assertLess(len('{"a": "éé"}'), size)
        with mock.patch.object(cards, "MAX_BYTES", size):
            self.assertEqual(run_check(multibyte)[0], 1)
        with mock.patch.object(cards, "MAX_BYTES", size - 1):
            self.assert_refused(run_check(multibyte))
        # grow the file after the descriptor was admitted: the read stays bounded and refuses before decoding
        grow = self.write(b'{"a": 1}', "grow.json")

        def grow_after_fstat(fd):
            result = REAL_FSTAT(fd)
            with open(grow, "ab") as more:
                more.write(b" " * 500)
            return result

        streams = []

        def make(fd, mode="rb"):
            streams.append(Spy(fd, mode))
            return streams[-1]

        with mock.patch.object(cards, "MAX_BYTES", 64), mock.patch.object(cards.os, "fstat", side_effect=grow_after_fstat), \
                mock.patch.object(cards.os, "fdopen", side_effect=make), \
                mock.patch.object(cards.json, "loads") as loads:
            self.assert_refused(run_check(grow))
            loads.assert_not_called()
        self.assertEqual(streams[0].reads, [65])

    def test_check_never_reads_stdin_renders_or_mutates(self):
        valid, bad = self.write(CONFIG, "valid.json"), self.write(dict(CONFIG, extra=1), "bad.json")
        fifo = self.fifo()
        inputs = [valid, bad, fifo, self.dir, self.dir / "absent.json", self.write(b"[]", "list.json")]
        members, before = sorted(os.listdir(self.dir)), {p: snapshot(p) for p in (valid, bad)}
        stdin = mock.Mock()
        stdin.buffer.read.side_effect = AssertionError("stdin read")
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(cards, "render") as render, \
                mock.patch.object(cards, "ASSET") as asset:
            for path in inputs:
                run_check(path)
                run_check(path)
            render.assert_not_called()
            self.assertEqual(asset.mock_calls, [])
        stdin.buffer.read.assert_not_called()
        self.assertEqual(sorted(os.listdir(self.dir)), members)
        self.assertEqual({p: snapshot(p) for p in (valid, bad)}, before)
        for path in (fifo, self.dir, self.dir / "absent.json"):
            self.assert_refused(run_check(path))


if __name__ == "__main__":
    unittest.main()
