"""RF1751-3: saved-feed reads refuse FIFOs without blocking. Synthetic data only; no network."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import meetings  # noqa: E402
from campaign_tool.meetings import MAX_FEED_BYTES, MeetingsError, read_feed_file  # noqa: E402

GRANICUS = REPO_ROOT / "tests" / "fixtures" / "granicus-feed.xml"
AGENDA = REPO_ROOT / "tests" / "fixtures" / "agendacenter-rss.xml"
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TIMEOUT = 20

DIRECT = (
    "import sys\n"
    f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
    "from campaign_tool.meetings import MeetingsError, read_feed_file\n"
    "try:\n    read_feed_file(sys.argv[1])\nexcept MeetingsError as e:\n    print('REFUSED', e)\n"
)
SEAM = (
    "import os, sys\n"
    f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
    "from campaign_tool.meetings import MeetingsError, read_feed_file\n"
    "path = sys.argv[1]\n"
    "real = os.open\n"
    "def swap(p, flags, *a, **k):\n"
    "    os.unlink(path); os.mkfifo(path)\n"
    "    return real(p, flags, *a, **k)\n"
    "os.open = swap\n"
    "try:\n    read_feed_file(path)\nexcept MeetingsError as e:\n    print('REFUSED', e)\n"
)


def py(code, *args):
    return subprocess.run([sys.executable, "-B", "-c", code, *args], capture_output=True, text=True, timeout=TIMEOUT)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class Direct(Base):
    def test_fifo_without_writer_refused_without_blocking(self):
        fifo = self.tmp / "f.xml"
        os.mkfifo(fifo)
        proc = py(DIRECT, str(fifo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REFUSED", proc.stdout)
        self.assertIn("not a regular file", proc.stdout)

    def test_regular_replaced_by_fifo_at_open_is_refused(self):
        leaf = self.tmp / "f.xml"
        leaf.write_bytes(b"<rss/>")
        proc = py(SEAM, str(leaf))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REFUSED", proc.stdout)
        self.assertIn("not a regular file", proc.stdout)

    def test_nonregular_and_symlink_controls(self):
        sentinel = self.tmp / "sentinel.xml"
        sentinel.write_bytes(b"foreign")
        live, dangling, fifo = self.tmp / "live.xml", self.tmp / "dangling.xml", self.tmp / "f.xml"
        live.symlink_to(sentinel)
        dangling.symlink_to(self.tmp / "missing.xml")
        os.mkfifo(fifo)
        for path in (self.tmp, fifo, live, dangling):
            with self.assertRaises(MeetingsError, msg=str(path)):
                read_feed_file(str(path))
        self.assertEqual(sentinel.read_bytes(), b"foreign")

    def test_regular_file_boundaries_and_feed_bytes(self):
        for name, size in (("empty", 0), ("exact", MAX_FEED_BYTES)):
            path = self.tmp / name
            path.write_bytes(b"x" * size)
            self.assertEqual(read_feed_file(str(path)), b"x" * size)
        over = self.tmp / "over"
        over.write_bytes(b"x" * (MAX_FEED_BYTES + 1))
        with self.assertRaisesRegex(MeetingsError, "larger than 2 MiB"):
            read_feed_file(str(over))
        for fixture in (GRANICUS, AGENDA):
            self.assertEqual(read_feed_file(str(fixture)), fixture.read_bytes())

    def test_unavailable_nonblocking_capability_fails_closed(self):
        path = self.tmp / "a.xml"
        path.write_bytes(b"<rss/>")
        for flag in ("O_NONBLOCK", "O_NOFOLLOW"):
            with mock.patch.dict(os.__dict__):
                del os.__dict__[flag]
                with mock.patch.object(os, "open", side_effect=AssertionError("opened")):
                    with self.assertRaises(MeetingsError):
                        read_feed_file(str(path))


class Descriptors(Base):
    def spy(self):
        opened, closed = [], []
        real_open, real_close = os.open, os.close

        def op(*a, **k):
            fd = real_open(*a, **k)
            opened.append(fd)
            return fd

        def cl(fd):
            closed.append(fd)
            return real_close(fd)

        return opened, closed, mock.patch.object(os, "open", op), mock.patch.object(os, "close", cl)

    def check(self, path, error, **patches):
        opened, closed, p_open, p_close = self.spy()
        keep = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, keep)
        with p_open, p_close, mock.patch.multiple(os, **patches) if patches else mock.patch.dict(os.__dict__):
            with self.assertRaises(error):
                read_feed_file(str(path))
        self.assertEqual(len(opened), 1)
        self.assertEqual(sorted(closed), sorted(opened), "each acquired fd closes exactly once")
        os.fstat(keep)  # unrelated descriptors stay valid

    def test_descriptor_ownership_on_all_exit_paths(self):
        reg = self.tmp / "r.xml"
        reg.write_bytes(b"<rss/>")
        fifo = self.tmp / "f.xml"
        os.mkfifo(fifo)
        self.check(fifo, MeetingsError)
        self.check(reg, RuntimeError, fstat=mock.Mock(side_effect=RuntimeError("fstat")))
        self.check(reg, RuntimeError, fdopen=mock.Mock(side_effect=RuntimeError("fdopen")))
        # ownership moves to the stream after fdopen: a read failure closes it once, never twice
        opened, closed, p_open, p_close = self.spy()
        boom = mock.Mock(side_effect=OSError("read"))
        real_fdopen = os.fdopen

        def failing(fd, mode):
            handle = real_fdopen(fd, mode)
            handle.read = boom
            return handle

        with p_open, p_close, mock.patch.object(os, "fdopen", failing):
            with self.assertRaises(OSError):
                read_feed_file(str(reg))
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):  # the stream closed its descriptor
            os.fstat(opened[0])


class Cli(Base):
    def run_cli(self, root, platform, client, feed, *extra):
        argv = [sys.executable, "-B", "-m", "campaign_tool", "meetings", "--directory", str(root), "--platform", platform,
                "--client", client, "--from-xml", str(feed), "--since", "2026-12-01", *extra]
        return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, cwd=REPO_ROOT)

    def snapshot(self, root):
        return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in sorted(root.rglob("*")) if p.is_file()}

    def test_xml_fifo_cli_preserves_existing_meeting_state(self):
        cases = (("granicus", "examplecity.granicus.com", GRANICUS, ("--view-id", "7")),
                 ("agendacenter", "example-city.example.org", AGENDA, ()))
        for platform, client, fixture, extra in cases:
            with self.subTest(platform=platform):
                root = self.tmp / platform
                shutil.copytree(EXAMPLE, root)
                good = self.run_cli(root, platform, client, fixture, *extra)
                self.assertEqual(good.returncode, 0, good.stderr)
                fifo = self.tmp / f"{platform}.fifo"
                os.mkfifo(fifo)
                before = self.snapshot(root)
                bad = self.run_cli(root, platform, client, fifo, *extra)
                self.assertEqual(bad.returncode, 1, bad.stderr)
                self.assertEqual(bad.stdout, "")
                self.assertIn("Stopped:", bad.stderr)
                self.assertIn("not a regular file", bad.stderr)
                self.assertNotIn("Traceback", bad.stderr)
                self.assertEqual(self.snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
