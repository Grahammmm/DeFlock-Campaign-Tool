"""A regular .git pointer replaced after admission is refused as the generic unchecked row. Synthetic files only."""

import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import git_exposure  # noqa: E402
from campaign_tool.git_exposure import git_exposure_row  # noqa: E402

GENERIC = {"key": "git_exposure", "state": "todo", "label": "Git exposure: not checked",
           "next": "Rerun doctor --checklist once git can read this work tree; "
                   "do not commit campaign files until checked.", "owner": "organizer"}

CHILD = textwrap.dedent("""
    import json, os, sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from campaign_tool import git_exposure
    marker = Path(sys.argv[2]) / ".git"
    real_open = os.open
    def swap(path, *a, **kw):
        if Path(path) == marker:
            os.rename(marker, marker.parent / "original-marker")
            os.mkfifo(marker)
        return real_open(path, *a, **kw)
    git_exposure.os.open = swap
    git_exposure._git = lambda *a, **kw: sys.exit(9)
    print(json.dumps(git_exposure.git_exposure_row(Path(sys.argv[2]) / "camp")))
""")


class MarkerAdmission(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.camp = self.base / "camp"
        self.camp.mkdir()
        self.marker = self.base / ".git"
        self.marker.write_text("gitdir: nowhere\n")

    def swapper(self, replace, opened=None):
        real_open = os.open
        marker = self.marker

        def hook(path, *a, **kw):
            if Path(path) == marker:
                replace()
            fd = real_open(path, *a, **kw)
            if opened is not None and Path(path) == marker:
                opened.append(fd)
            return fd

        return mock.patch.object(git_exposure.os, "open", hook)

    def no_git(self):
        return mock.patch.object(git_exposure, "_git", side_effect=AssertionError("git must not run"))

    def to_fifo(self):
        os.rename(self.marker, self.base / "original-marker")
        os.mkfifo(self.marker)

    def test_marker_swapped_to_writerless_fifo_refuses_without_waiting(self):
        child = subprocess.run([sys.executable, "-B", "-c", CHILD, str(REPO_ROOT), str(self.base)],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn('"label": "Git exposure: not checked"', child.stdout)
        self.assertNotIn("Traceback", child.stderr)

    def test_in_process_fifo_replacement_is_generic_and_closes_descriptor(self):
        opened = []
        with self.swapper(self.to_fifo, opened), self.no_git():
            self.assertEqual(git_exposure_row(self.camp), GENERIC)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError) as ctx:
            os.fstat(opened[0])
        self.assertEqual(ctx.exception.errno, errno.EBADF)

    def test_marker_symlink_substitution_never_reads_target(self):
        canary = self.base / "canary"
        canary.write_text("gitdir: canary\n")

        def to_link():
            os.rename(self.marker, self.base / "original-marker")
            self.marker.symlink_to(canary)

        reads = []
        with self.swapper(to_link), mock.patch.object(git_exposure.os, "read", side_effect=reads.append), \
                self.no_git():
            self.assertEqual(git_exposure_row(self.camp), GENERIC)
        self.assertEqual(reads, [])
        self.assertEqual(canary.read_text(), "gitdir: canary\n")

    def test_marker_different_regular_inode_is_refused_before_prefix_read(self):
        def to_other():
            os.rename(self.marker, self.base / "original-marker")
            self.marker.write_text("gitdir: other\n")

        reads = []
        with self.swapper(to_other), mock.patch.object(git_exposure.os, "read", side_effect=reads.append), \
                self.no_git():
            self.assertEqual(git_exposure_row(self.camp), GENERIC)
        self.assertEqual(reads, [])

    def test_open_flags_and_bounded_read_on_normal_path(self):
        calls = []
        real_open = os.open

        def spy(path, flags, *a, **kw):
            if Path(path) == self.marker:
                calls.append(flags)
            return real_open(path, flags, *a, **kw)

        with mock.patch.object(git_exposure.os, "open", spy):
            git_exposure._read_marker_prefix(self.marker, os.lstat(self.marker))
        want = os.O_NONBLOCK | os.O_NOFOLLOW
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0] & want, want)
        self.assertEqual(calls[0] & (os.O_WRONLY | os.O_RDWR), 0)

    def test_descriptor_closes_when_fstat_or_read_fails(self):
        for name in ("fstat", "read"):
            closed = []
            real_close = os.close

            def close(fd):
                closed.append(fd)
                real_close(fd)

            with mock.patch.object(git_exposure.os, name, side_effect=PermissionError("synthetic")), \
                    mock.patch.object(git_exposure.os, "close", close):
                with self.assertRaises(git_exposure._Unchecked):
                    git_exposure._read_marker_prefix(self.marker, os.lstat(self.marker))
            self.assertEqual(len(closed), 1, name)

    def test_unexpected_exception_is_not_hidden_and_descriptor_closes(self):
        closed = []
        real_close = os.close

        def close(fd):
            closed.append(fd)
            real_close(fd)

        with mock.patch.object(git_exposure.os, "read", side_effect=RuntimeError("bug")), \
                mock.patch.object(git_exposure.os, "close", close):
            with self.assertRaises(RuntimeError):
                git_exposure._read_marker_prefix(self.marker, os.lstat(self.marker))
        self.assertEqual(len(closed), 1)

    def test_missing_after_admission_and_missing_capability_stay_generic(self):
        with self.swapper(lambda: os.unlink(self.marker)), self.no_git():
            self.assertEqual(git_exposure_row(self.camp), GENERIC)
        self.marker.write_text("gitdir: nowhere\n")
        with mock.patch.object(git_exposure.os, "O_NOFOLLOW", None), self.no_git():
            self.assertEqual(git_exposure_row(self.camp), GENERIC)

    def test_refused_marker_is_not_counted_as_a_second_marker(self):
        inner = self.camp / ".git"
        inner.mkdir()
        self.to_fifo()
        with self.no_git():
            self.assertEqual(git_exposure_row(self.camp), GENERIC)

    def test_checker_adds_no_mutation_after_actor_change(self):
        self.to_fifo()
        before = sorted((str(p), p.lstat().st_mode, p.lstat().st_mtime_ns) for p in self.base.rglob("*"))
        with self.no_git():
            git_exposure_row(self.camp)
            git_exposure_row(self.camp)
        after = sorted((str(p), p.lstat().st_mode, p.lstat().st_mtime_ns) for p in self.base.rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
