"""The --lint CLI refuses FIFO/nonregular inputs without blocking later files (RF0925-5)."""
import contextlib
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool import markdown_lite
from campaign_tool.markdown_lite import MAX_BYTES, main

ROOT = Path(__file__).resolve().parents[1]
DIRTY = "ok\n# Top\n[a](http://example.invalid)\n"
FINDINGS = ["{p}:2: heading-level: only \"## \" headings render",
            "{p}:3: link-dropped: link target is not absolute https"]
REFUSED = "{p}: cannot lint (not a regular file)"
HAS_FIFO = hasattr(os, "mkfifo")


def fd_is_closed(fd):
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.clean = self.dir / "clean.md"
        self.clean.write_text("## Heading\n", encoding="utf-8")
        self.dirty = self.dir / "dirty.md"
        self.dirty.write_text(DIRTY, encoding="utf-8")

    def fifo(self, name="copy.md"):
        path = self.dir / name
        os.mkfifo(path)
        return path

    def run_cli(self, *paths):
        """Run the real module in a child with a watchdog; a timeout is a failure."""
        child = subprocess.Popen([sys.executable, "-B", "-m", "campaign_tool.markdown_lite", "--lint",
                                  *map(str, paths)], cwd=ROOT, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            out, err = child.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            self.fail("lint CLI blocked on a nonregular input")
        return child.returncode, out.splitlines(), err

    def run_main(self, *paths):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--lint", *map(str, paths)])
        return code, out.getvalue().splitlines(), err.getvalue()


@unittest.skipUnless(HAS_FIFO, "os.mkfifo unsupported")
class FifoProcessTests(Base):
    def test_fifo_without_writer_exits_two_and_continues(self):
        pipe = self.fifo()
        for order in ((pipe, self.clean, self.dirty), (self.clean, pipe, self.dirty),
                      (self.clean, self.dirty, pipe)):
            with self.subTest(first=order[0].name):
                code, out, err = self.run_cli(*order)
                self.assertEqual(code, 2)
                self.assertEqual(out, [line.format(p=self.dirty) for line in FINDINGS] + ["files=2 findings=2"])
                self.assertEqual(err.splitlines(), [REFUSED.format(p=pipe)])
        self.assertTrue(stat.S_ISFIFO(os.stat(pipe).st_mode))

    def test_fifo_with_available_bytes_and_retained_writer_is_refused(self):
        pipe = self.fifo()
        writer = os.open(pipe, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.write(writer, b"## queued synthetic heading\n")
            code, out, err = self.run_cli(pipe, self.clean)
            self.assertEqual((code, out), (2, ["files=1 findings=0"]))
            self.assertEqual(err.splitlines(), [REFUSED.format(p=pipe)])
            self.assertEqual(os.read(writer, 100), b"## queued synthetic heading\n")  # not consumed
        finally:
            os.close(writer)

    def test_symlink_to_fifo_refused_regular_link_accepted(self):
        pipe = self.fifo("real.pipe")
        direct = self.dir / "direct.md"
        direct.symlink_to(pipe)
        relative = self.dir / "relative.md"
        relative.symlink_to("real.pipe")
        good = self.dir / "link.md"
        good.symlink_to("clean.md")
        hard = self.dir / "hard.md"
        os.link(self.clean, hard)
        code, out, err = self.run_cli(direct, relative, good, hard)
        self.assertEqual((code, out), (2, ["files=2 findings=0"]))
        self.assertEqual(err.splitlines(), [REFUSED.format(p=direct), REFUSED.format(p=relative)])


class AdmissionTests(Base):
    def test_directories_and_dangling_links(self):
        link = self.dir / "dirlink"
        link.symlink_to(self.dir)
        dangling = self.dir / "dangling.md"
        dangling.symlink_to("nowhere.md")
        code, out, err = self.run_main(self.dir, link, dangling, self.clean)
        self.assertEqual((code, out), (2, ["files=1 findings=0"]))
        self.assertEqual(err.splitlines(), [REFUSED.format(p=self.dir), REFUSED.format(p=link),
                                            f"{dangling}: cannot lint (No such file or directory)"])

    def test_boundaries_match_legacy_cli(self):
        cases = {"empty": b"", "exact": b"a" * MAX_BYTES, "multibyte": "é".encode() * (MAX_BYTES // 2)}
        for name, body in cases.items():
            (self.dir / name).write_bytes(body)
            with self.subTest(name=name):
                self.assertEqual(self.run_main(self.dir / name), (0, ["files=1 findings=0"], ""))
        over = self.dir / "over"
        over.write_bytes(b"a" * (MAX_BYTES + 1))
        bad = self.dir / "bad"
        bad.write_bytes(b"SECRET-CANARY \xff")
        code, out, err = self.run_main(over, bad, self.dirty, self.dirty)
        self.assertEqual((code, out[-1]), (2, "files=2 findings=4"))
        self.assertEqual(err.splitlines(), [f"{over}: cannot lint (over MAX_BYTES)", f"{bad}: cannot lint (not UTF-8)"])
        self.assertNotIn("CANARY", err)

    def test_read_is_bounded_by_actual_bytes(self):
        sizes = []
        real_fdopen = os.fdopen

        def spy(fd, *args, **kwargs):
            stream = real_fdopen(fd, *args, **kwargs)
            read = stream.read
            stream.read = lambda n=-1: sizes.append(n) or read(n)
            return stream
        with mock.patch.object(markdown_lite.os, "fdopen", spy):
            self.assertEqual(self.run_main(self.clean)[0], 0)
        self.assertEqual(sizes, [MAX_BYTES + 1])

    def test_usage_errors_do_no_io_and_inputs_untouched(self):
        with mock.patch.object(markdown_lite.os, "open", side_effect=AssertionError("IO")):
            for argv in ((), ("--lint",), ("--fix", str(self.clean)), ("--lint", "-v", str(self.clean))):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(main(list(argv)), 2)
                self.assertTrue(err.getvalue().startswith("usage:"))
        before = {p: (p.read_bytes(), os.stat(p).st_mtime_ns, os.stat(p).st_mode) for p in (self.clean, self.dirty)}
        listing = sorted(os.listdir(self.dir))
        self.assertEqual(self.run_main(self.clean, self.dirty)[0], 1)
        self.assertEqual({p: (p.read_bytes(), os.stat(p).st_mtime_ns, os.stat(p).st_mode) for p in before}, before)
        self.assertEqual(sorted(os.listdir(self.dir)), listing)


class DescriptorTests(Base):
    def opened(self):
        """Patch os.open to record the real descriptors it returns and the flags it was given."""
        real, log = os.open, []

        def spy(path, flags, *args, **kwargs):
            fd = real(path, flags, *args, **kwargs)
            log.append((fd, flags))
            return fd
        return mock.patch.object(markdown_lite.os, "open", spy), log

    @unittest.skipUnless(HAS_FIFO, "os.mkfifo unsupported")
    def test_regular_to_fifo_swap_after_preflight_is_nonblocking(self):
        target = self.dir / "swap.md"
        target.write_text("## x\n", encoding="utf-8")
        real, log = os.open, []

        def swap_then_open(path, flags, *args, **kwargs):
            if str(path) == str(target):  # the preflight already saw a regular file
                os.unlink(target)
                os.mkfifo(target)
            fd = real(path, flags, *args, **kwargs)
            log.append((fd, flags))
            return fd
        reads = []
        real_fdopen = os.fdopen

        def no_read_fdopen(*args, **kwargs):
            reads.append(args)
            return real_fdopen(*args, **kwargs)
        with mock.patch.object(markdown_lite.os, "open", swap_then_open), \
                mock.patch.object(markdown_lite.os, "fdopen", no_read_fdopen):
            code, out, err = self.run_main(target, self.clean)
        self.assertEqual((code, out), (2, ["files=1 findings=0"]))
        self.assertEqual(err.splitlines(), [REFUSED.format(p=target)])
        self.assertEqual(len(reads), 1)  # only the regular successor became a stream
        self.assertTrue(log[0][1] & os.O_NONBLOCK)
        self.assertTrue(all(fd_is_closed(fd) for fd, _ in log))

    def test_regular_to_directory_swap_after_preflight_is_refused(self):
        target = self.dir / "swap.md"
        target.write_text("## x\n", encoding="utf-8")
        real, log = os.open, []

        def swap_then_open(path, flags, *args, **kwargs):
            if str(path) == str(target):
                os.unlink(target)
                os.mkdir(target)
            fd = real(path, flags, *args, **kwargs)
            log.append(fd)
            return fd
        with mock.patch.object(markdown_lite.os, "open", swap_then_open):
            code, out, err = self.run_main(target, self.clean)
        self.assertEqual((code, out), (2, ["files=1 findings=0"]))
        self.assertEqual(err.splitlines(), [REFUSED.format(p=target)])
        self.assertTrue(all(fd_is_closed(fd) for fd in log))

    def test_raw_fd_closes_on_type_fstat_and_constructor_failures(self):
        keep = os.open(self.clean, os.O_RDONLY)
        self.addCleanup(lambda: fd_is_closed(keep) or os.close(keep))
        real_stat = os.fstat
        nonregular = os.stat_result((stat.S_IFIFO | 0o600,) + (0,) * 9)
        cases = {
            "type": (mock.patch.object(markdown_lite.os, "fstat", lambda fd: nonregular), REFUSED),
            "fstat": (mock.patch.object(markdown_lite.os, "fstat", side_effect=OSError(5, "Input/output error")),
                      "{p}: cannot lint (Input/output error)"),
            "fdopen": (mock.patch.object(markdown_lite.os, "fdopen", side_effect=OSError(24, "Too many open files")),
                       "{p}: cannot lint (Too many open files)"),
        }
        for name, (patch, expected) in cases.items():
            for _ in range(3):  # repeated refusals do not accumulate descriptors
                spy, log = self.opened()
                with self.subTest(case=name), spy, patch:
                    code, out, err = self.run_main(self.clean, self.dirty)
                self.assertEqual(code, 2)
                self.assertEqual(err.splitlines(), [expected.format(p=self.clean), expected.format(p=self.dirty)])
                self.assertEqual(len(log), 2)
                self.assertTrue(all(fd_is_closed(fd) for fd, _ in log))
        self.assertIsNotNone(real_stat(keep))  # unrelated descriptor still live

    def test_open_failure_does_not_close_unacquired_fd(self):
        keep = os.open(self.clean, os.O_RDONLY)
        self.addCleanup(lambda: fd_is_closed(keep) or os.close(keep))
        with mock.patch.object(markdown_lite.os, "open", side_effect=PermissionError(13, "Permission denied")), \
                mock.patch.object(markdown_lite.os, "close", side_effect=AssertionError("close")):
            code, _, err = self.run_main(self.clean)
        self.assertEqual((code, err.strip()), (2, f"{self.clean}: cannot lint (Permission denied)"))
        self.assertFalse(fd_is_closed(keep))

    def test_transferred_stream_closes_once_on_read_error_and_success(self):
        real_fdopen = os.fdopen
        streams = []

        def spy(fd, *args, **kwargs):
            stream = real_fdopen(fd, *args, **kwargs)
            streams.append((fd, stream))
            return stream
        with mock.patch.object(markdown_lite.os, "fdopen", spy):
            self.assertEqual(self.run_main(self.clean)[0], 0)
        self.assertTrue(streams[0][1].closed and fd_is_closed(streams[0][0]))

        class Failing:
            def __init__(self, stream):
                self.stream = stream

            def read(self, size=-1):
                raise OSError(5, "Input/output error")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.stream.close()

        def failing(fd, *args, **kwargs):
            stream = real_fdopen(fd, *args, **kwargs)
            streams.append((fd, stream))
            return Failing(stream)
        streams.clear()
        with mock.patch.object(markdown_lite.os, "fdopen", failing):
            code, out, err = self.run_main(self.clean, self.dirty)
        self.assertEqual((code, out), (2, ["files=0 findings=0"]))
        self.assertEqual(err.splitlines(), [f"{p}: cannot lint (Input/output error)" for p in (self.clean, self.dirty)])
        self.assertTrue(all(s.closed and fd_is_closed(fd) for fd, s in streams))

    def test_close_failure_does_not_replace_primary_error(self):
        spy, log = self.opened()
        with spy, mock.patch.object(markdown_lite.os, "fdopen", side_effect=OSError(24, "Too many open files")), \
                mock.patch.object(markdown_lite.os, "close", side_effect=OSError(9, "Bad file descriptor")):
            code, _, err = self.run_main(self.clean)
        self.assertEqual((code, err.strip()), (2, f"{self.clean}: cannot lint (Too many open files)"))
        os.close(log[0][0])  # harness cleans the deliberately retained fd

    def test_fd_zero_is_closed_in_isolated_child(self):
        script = self.dir / "fd0.py"
        script.write_text(
            "import os, sys\n"
            "from unittest import mock\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from campaign_tool import markdown_lite as m\n"
            "os.close(0)\n"
            "nonregular = os.stat_result((0o010600,) + (0,) * 9)\n"
            "with mock.patch.object(m.os, 'fstat', lambda fd: nonregular):\n"
            "    try:\n"
            "        m._read_regular(sys.argv[1])\n"
            "    except ValueError as error:\n"
            "        print(error)\n"
            "try:\n"
            "    os.fstat(0)\n"
            "except OSError:\n"
            "    print('closed')\n", encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(script), str(self.clean)], cwd=ROOT,
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.stdout.splitlines(), ["not a regular file", "closed"], result.stderr)


if __name__ == "__main__":
    unittest.main()
