"""Synthetic regression tests for snapshot staging ownership."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import schedule_snapshot as ss

DATA = {"schema": ss.SCHEMA, "generated_at": "2026-01-01T00:00:00+00:00", "runs": []}
RAW = json.dumps(DATA).encode()
EXPECTED = (json.dumps(DATA, sort_keys=True) + "\n").encode()
ERROR = "schedule snapshot: refused schedule_unreadable\n"


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / "schedule.json"
        self.legacy = self.root / "schedule.json.tmp"

    def invoke(self, raw=RAW):
        stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with stdin, patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ss.main(["--out", str(self.output)])
        return code, out.getvalue(), err.getvalue()

    def test_legacy_tmp_hardlink_never_mutates_foreign_inode(self):
        victim = self.root / "victim"
        victim.write_bytes(b"synthetic foreign bytes")
        victim.chmod(0o644)
        os.link(victim, self.legacy)
        before = victim.stat()
        self.assertEqual(self.invoke(), (0, "", ""))
        after = victim.stat()
        self.assertEqual(victim.read_bytes(), b"synthetic foreign bytes")
        self.assertEqual(self.legacy.read_bytes(), b"synthetic foreign bytes")
        self.assertEqual((after.st_ino, after.st_mode, after.st_mtime_ns, after.st_nlink),
                         (before.st_ino, before.st_mode, before.st_mtime_ns, before.st_nlink))
        self.assertNotEqual(self.output.stat().st_ino, before.st_ino)
        self.assertEqual(self.output.read_bytes(), EXPECTED)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)

    def test_existing_regular_symlink_dangling_and_fifo_tmp_entries_are_untouched(self):
        for kind in ("regular", "symlink", "dangling", "directory", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory)
                legacy, output = root / "schedule.json.tmp", root / "schedule.json"
                victim = root / "victim"
                victim.write_bytes(b"foreign")
                if kind == "regular":
                    legacy.write_bytes(b"foreign")
                    legacy.chmod(0o666)
                elif kind == "symlink":
                    legacy.symlink_to(victim)
                elif kind == "dangling":
                    legacy.symlink_to(root / "absent")
                elif kind == "directory":
                    legacy.mkdir()
                else:
                    os.mkfifo(legacy)
                before = legacy.lstat()
                link = os.readlink(legacy) if legacy.is_symlink() else None
                result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records.schedule_snapshot",
                                         "--out", str(output)], input=RAW, capture_output=True, timeout=5)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
                after = legacy.lstat()
                self.assertEqual((before.st_ino, before.st_mode, before.st_mtime_ns, before.st_nlink),
                                 (after.st_ino, after.st_mode, after.st_mtime_ns, after.st_nlink))
                self.assertEqual(victim.read_bytes(), b"foreign")
                if kind == "regular":
                    self.assertEqual(legacy.read_bytes(), b"foreign")
                if link is not None:
                    self.assertEqual(os.readlink(legacy), link)
                self.assertEqual(output.read_bytes(), EXPECTED)

    def test_write_flush_fsync_replace_failures_preserve_old_snapshot(self):
        for stage in ("allocation", "fdopen", "write", "flush", "fsync", "replace"):
            with self.subTest(stage=stage):
                self.output.write_bytes(b"old snapshot")
                self.legacy.write_bytes(b"unrelated")
                before = set(self.root.iterdir())
                fds_before = len(os.listdir("/proc/self/fd"))
                calls = []
                real_mkstemp, real_fdopen = tempfile.mkstemp, os.fdopen
                real_fsync, real_replace = os.fsync, os.replace

                def allocate(*args, **kwargs):
                    if stage == "allocation":
                        calls.append(stage)
                        raise OSError("synthetic allocation failure")
                    return real_mkstemp(*args, **kwargs)

                class Wrapper:
                    def __init__(self, handle):
                        self.handle = handle
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        self.handle.close()
                    def write(self, text):
                        if stage == "write":
                            calls.append(stage)
                            raise OSError("synthetic write failure")
                        return self.handle.write(text)
                    def flush(self):
                        if stage == "flush":
                            calls.append(stage)
                            raise OSError("synthetic flush failure")
                        return self.handle.flush()
                    def fileno(self):
                        return self.handle.fileno()

                def fdopen(fd, *args, **kwargs):
                    if stage == "fdopen":
                        calls.append(stage)
                        raise OSError("synthetic fdopen failure")
                    return Wrapper(real_fdopen(fd, *args, **kwargs))

                def fsync(fd):
                    if stage == "fsync":
                        calls.append(stage)
                        raise OSError("synthetic fsync failure")
                    return real_fsync(fd)

                def replace(*args):
                    if stage == "replace":
                        calls.append(stage)
                        raise OSError("synthetic replace failure")
                    return real_replace(*args)

                with patch.object(tempfile, "mkstemp", side_effect=allocate), patch.object(os, "fdopen", side_effect=fdopen), \
                     patch.object(os, "fsync", side_effect=fsync), patch.object(os, "replace", side_effect=replace):
                    result = self.invoke()
                self.assertEqual(result, (2, "", ERROR))
                self.assertEqual(calls, [stage])
                self.assertEqual(self.output.read_bytes(), b"old snapshot")
                self.assertEqual(self.legacy.read_bytes(), b"unrelated")
                self.assertEqual(set(self.root.iterdir()), before)
                self.assertEqual(len(os.listdir("/proc/self/fd")), fds_before)

    def test_new_private_temporary_inode_is_never_reopened(self):
        real_mkstemp, real_fdopen, real_replace = tempfile.mkstemp, os.fdopen, os.replace
        acquired, opened, replaced = [], [], []

        def allocate(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            self.assertEqual(Path(path).parent, self.root)
            self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o600)
            acquired.append((fd, path, os.fstat(fd).st_ino))
            return fd, path

        def fdopen(fd, *args, **kwargs):
            opened.append(fd)
            return real_fdopen(fd, *args, **kwargs)

        def replace(src, dst):
            self.assertEqual(Path(src).stat().st_ino, acquired[-1][2])
            self.assertEqual(stat.S_IMODE(Path(src).stat().st_mode), 0o600)
            replaced.append(src)
            return real_replace(src, dst)

        with patch.object(tempfile, "mkstemp", side_effect=allocate), patch.object(os, "fdopen", side_effect=fdopen), \
             patch.object(os, "replace", side_effect=replace):
            self.assertEqual(self.invoke(), (0, "", ""))
            self.assertEqual(self.invoke(), (0, "", ""))
        self.assertEqual(len(acquired), 2)
        self.assertNotEqual(acquired[0][1], acquired[1][1])
        self.assertEqual(opened, [entry[0] for entry in acquired])
        self.assertEqual(replaced, [entry[1] for entry in acquired])
        for fd, _, _ in acquired:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_invalid_input_never_allocates_temporary(self):
        self.output.write_bytes(b"old")
        self.legacy.write_bytes(b"foreign")
        future = dict(DATA, generated_at="9999-01-01T00:00:00+00:00")
        for raw in (b"not json", b"x" * (ss.MAX_BYTES + 1), json.dumps(future).encode()):
            with self.subTest(raw=raw[:40]), patch.object(tempfile, "mkstemp") as alloc, patch.object(os, "replace") as replace:
                self.assertEqual(self.invoke(raw), (2, "", ERROR))
                alloc.assert_not_called()
                replace.assert_not_called()
                self.assertEqual(self.output.read_bytes(), b"old")
                self.assertEqual(self.legacy.read_bytes(), b"foreign")

    def test_output_contract_and_existing_export_path_unchanged(self):
        self.output.write_bytes(b"old")
        self.assertEqual(self.invoke(), (0, "", ""))
        self.assertEqual(self.output.read_bytes(), EXPECTED)
        self.assertEqual(self.invoke(), (0, "", ""))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(ss.main(["--reports", str(self.root)]), 0)
        exported = json.loads(out.getvalue())
        self.assertEqual(set(exported), {"schema", "generated_at", "runs"})
        self.assertEqual(exported["runs"], [])
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
