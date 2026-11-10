"""RF2205-3: campaign-register staging is exclusive and private; a pre-existing `campaigns.json.tmp` is never touched.

Synthetic real-filesystem tests only: a throwaway directory, a synthetic campaign.json, no network, nothing from any
real campaign.
"""
import argparse
import contextlib
import errno
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import registry

EVENTS = {"open", "os.remove", "os.rename", "os.listdir", "os.scandir", "os.chmod", "os.truncate", "os.utime"}
RECORD = None  # while a list, every file operation of this process is appended to it as (event, absolute path)


def _record(event, args):
    if RECORD is not None and event in EVENTS and args and isinstance(args[0], (str, bytes, os.PathLike)):
        RECORD.append((event, os.path.abspath(os.fsdecode(args[0]))))
        if event == "os.rename" and isinstance(args[1], (str, bytes, os.PathLike)):
            RECORD.append(("os.rename-to", os.path.abspath(os.fsdecode(args[1]))))


sys.addaudithook(_record)

ROW = {"name": "alpha", "directory": "/nonexistent-synthetic/alpha", "added_at": "2026-01-01T00:00:00Z"}
OLD = json.dumps([ROW], indent=2) + "\n"


def snapshot(path):
    """Everything observable about a directory entry without following it."""
    info = os.lstat(path)
    out = {"mode": info.st_mode, "ino": info.st_ino, "nlink": info.st_nlink, "mtime": info.st_mtime_ns,
           "size": info.st_size}
    if stat.S_ISLNK(info.st_mode):
        out["target"] = os.readlink(path)
    elif stat.S_ISREG(info.st_mode):
        out["bytes"] = Path(path).read_bytes()
    elif stat.S_ISDIR(info.st_mode):
        out["entries"] = sorted(os.listdir(path))
    return out


class Staging(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-staging-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.directory = self.base / "config"
        self.directory.mkdir(mode=0o700)
        self.register = self.directory / "campaigns.json"
        self.legacy = self.directory / "campaigns.json.tmp"
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.sentinel = self.outside / "sentinel.txt"
        self.sentinel.write_bytes(b"unrelated synthetic bytes\n")
        os.utime(self.sentinel, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        self.sentinel.chmod(0o644)
        self.campaign = self.base / "A"
        self.campaign.mkdir()
        (self.campaign / "campaign.json").write_text(
            json.dumps({"schema_version": 1, "name": "A", "county": "Synthetic", "state": "CA"}), encoding="utf-8")
        patch = mock.patch.dict(os.environ, {registry.ENV: str(self.register)})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.stop_recording)

    def stop_recording(self):
        global RECORD
        RECORD = None

    def seed(self, text=OLD):
        fd = os.open(self.register, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        self.register.chmod(0o600)

    def others(self):
        return sorted(p.name for p in self.directory.iterdir() if p.name not in ("campaigns.json", "campaigns.json.tmp"))

    def run_cli(self, command, *values):
        args = argparse.Namespace(campaigns_command=command, name=values[0],
                                  directory=values[1] if len(values) > 1 else None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = registry.run(args)
        return code, out.getvalue(), err.getvalue()

    # -- a pre-existing entry at the old predictable name ---------------------------------------------------------------

    def assert_hardlink_untouched(self, action):
        os.link(self.sentinel, self.legacy)
        before = (snapshot(self.sentinel), snapshot(self.legacy))
        self.assertEqual(before[0]["nlink"], 2)
        action()
        self.assertEqual((snapshot(self.sentinel), snapshot(self.legacy)), before)
        self.assertEqual(os.stat(self.sentinel).st_mode & 0o777, 0o644)
        self.assertEqual(self.sentinel.read_bytes(), b"unrelated synthetic bytes\n")
        self.assertNotEqual(os.stat(self.register).st_ino, os.stat(self.sentinel).st_ino)
        self.assertTrue(stat.S_ISREG(os.lstat(self.register).st_mode))
        self.assertEqual(os.stat(self.register).st_mode & 0o777, 0o600)
        self.assertEqual(self.others(), [])

    def test_remove_preserves_legacy_hardlink_temp_and_external_inode(self):
        self.seed()
        self.assert_hardlink_untouched(lambda: registry.remove("alpha"))
        self.assertEqual(self.register.read_bytes(), b"[]\n")

    def test_add_preserves_legacy_hardlink_temp_and_external_inode(self):
        self.assert_hardlink_untouched(lambda: registry.add("A", self.campaign))
        entries = json.loads(self.register.read_text(encoding="utf-8"))
        self.assertEqual([(e["name"], e["directory"]) for e in entries], [("A", str(self.campaign))])

    def test_legacy_temp_entries_are_ignored_not_consumed(self):
        def ordinary():
            self.legacy.write_bytes(b"ordinary legacy bytes\n")
            self.legacy.chmod(0o640)

        def symlink():
            os.symlink(self.sentinel, self.legacy)

        def dangling():
            os.symlink(self.base / "does-not-exist", self.legacy)

        def directory():
            self.legacy.mkdir()
            (self.legacy / "keep").write_bytes(b"x")

        for label, make in (("ordinary", ordinary), ("symlink", symlink), ("dangling", dangling), ("directory", directory)):
            with self.subTest(label):
                if self.legacy.is_symlink() or self.legacy.exists():
                    if self.legacy.is_dir() and not self.legacy.is_symlink():
                        (self.legacy / "keep").unlink()
                        self.legacy.rmdir()
                    else:
                        self.legacy.unlink()
                self.register.unlink(missing_ok=True)
                make()
                before = (snapshot(self.legacy), snapshot(self.sentinel))
                registry.add("A", self.campaign)
                registry.remove("A")
                registry.add("A", self.campaign)
                self.assertEqual((snapshot(self.legacy), snapshot(self.sentinel)), before)
                self.assertEqual([e["name"] for e in json.loads(self.register.read_text(encoding="utf-8"))], ["A"])
                self.assertEqual(os.stat(self.register).st_mode & 0o777, 0o600)
                self.assertEqual(self.others(), [])

    # -- the stage itself -----------------------------------------------------------------------------------------------

    def test_stage_is_unique_private_descriptor_backed_and_complete_before_replace(self):
        self.seed()
        old_inode = os.stat(self.register).st_ino
        seen, allocated = {}, []
        real_mkstemp, real_replace = tempfile.mkstemp, os.replace

        def mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            allocated.append((fd, name))
            return fd, name

        def replace(source, target):
            fd, name = allocated[0]
            seen.update(source=str(source), target=str(target), mode=os.stat(source).st_mode & 0o777,
                        ino=os.stat(source).st_ino, bytes=Path(source).read_bytes(), listing=self.others())
            with self.assertRaises(OSError) as closed:  # the descriptor is released before publication
                os.fstat(fd)
            seen["closed"] = closed.exception.errno == errno.EBADF
            return real_replace(source, target)

        entries = [ROW, {"name": "beta", "directory": "/nonexistent-synthetic/β", "added_at": "x"}]
        old_umask = os.umask(0o000)
        try:
            with mock.patch.object(tempfile, "mkstemp", mkstemp), mock.patch.object(os, "replace", replace):
                registry._store(self.register, entries)
        finally:
            os.umask(old_umask)
        self.assertEqual(len(allocated), 1)
        stage = Path(seen["source"])
        self.assertEqual(stage, Path(allocated[0][1]))
        self.assertEqual(stage.parent, self.directory)
        self.assertTrue(stage.name.startswith("campaigns.json.") and stage.name.endswith(".tmp"))
        self.assertNotEqual(stage.name, "campaigns.json.tmp")
        self.assertEqual(seen["target"], str(self.register))
        self.assertEqual(seen["mode"], 0o600)
        self.assertNotEqual(seen["ino"], old_inode)
        self.assertEqual(seen["bytes"], (json.dumps(entries, indent=2) + "\n").encode("utf-8"))
        self.assertTrue(seen["closed"])
        self.assertEqual(self.register.read_bytes(), seen["bytes"])
        self.assertEqual(os.stat(self.register).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.register).st_ino, seen["ino"])
        self.assertFalse(stage.exists())
        self.assertEqual(self.others(), [])

    def faulty(self, mode):
        """An os.fdopen that hands back the real file, wrapped so that one stage of the write fails."""
        real = os.fdopen
        test = self

        class File:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

            def fileno(self):
                return self.inner.fileno()

            def write(self, text):
                if mode == "write":
                    self.inner.write(text[:7])
                    raise OSError(errno.ENOSPC, "synthetic partial write")
                return self.inner.write(text)

            def flush(self):
                if mode == "flush":
                    raise OSError(errno.EIO, "synthetic flush")
                return self.inner.flush()

            def close(self):
                self.inner.close()
                if mode == "close":
                    raise OSError(errno.EIO, "synthetic close")

        def fdopen(fd, *args, **kwargs):
            if mode == "fdopen":
                raise OSError(errno.EMFILE, "synthetic fdopen")
            return File(real(fd, *args, **kwargs))
        return fdopen

    def test_stage_failures_preserve_register_and_release_descriptor(self):
        keep = self.directory / "campaigns.json.keep.tmp"
        os.link(self.sentinel, self.legacy)
        for mode in ("fdopen", "write", "flush", "fsync", "close", "replace"):
            with self.subTest(mode):
                self.seed()
                keep.write_bytes(b"unrelated sibling\n")
                before = (snapshot(self.register), snapshot(keep), snapshot(self.legacy), snapshot(self.sentinel))
                fds, real_mkstemp = [], tempfile.mkstemp

                def mkstemp(*args, **kwargs):
                    result = real_mkstemp(*args, **kwargs)
                    fds.append(result[0])
                    return result
                patches = [mock.patch.object(tempfile, "mkstemp", mkstemp)]
                if mode == "fsync":
                    patches.append(mock.patch.object(os, "fsync", side_effect=OSError(errno.EIO, "synthetic fsync")))
                elif mode == "replace":
                    patches.append(mock.patch.object(os, "replace", side_effect=OSError(errno.EXDEV, "synthetic replace")))
                else:
                    patches.append(mock.patch.object(os, "fdopen", self.faulty(mode)))
                with contextlib.ExitStack() as stack:
                    for p in patches:
                        stack.enter_context(p)
                    with self.assertRaises(OSError):
                        registry._store(self.register, [])
                self.assertEqual((snapshot(self.register), snapshot(keep), snapshot(self.legacy),
                                  snapshot(self.sentinel)), before)
                self.assertEqual(self.others(), ["campaigns.json.keep.tmp"])  # the owned stage is gone
                self.assertEqual(len(fds), 1)
                with self.assertRaises(OSError) as closed:
                    os.fstat(fds[0])
                self.assertEqual(closed.exception.errno, errno.EBADF)
                registry._store(self.register, [])  # retry
                self.assertEqual(self.register.read_bytes(), b"[]\n")
                self.assertEqual(self.others(), ["campaigns.json.keep.tmp"])

    def test_stage_allocation_failure_leaves_nothing_to_clean(self):
        self.seed()
        before = snapshot(self.register)
        with mock.patch.object(tempfile, "mkstemp", side_effect=OSError(errno.ENOSPC, "synthetic allocation")), \
                mock.patch.object(os, "unlink") as unlink:
            with self.assertRaises(OSError):
                registry._store(self.register, [])
        unlink.assert_not_called()
        self.assertEqual(snapshot(self.register), before)
        self.assertEqual(self.others(), [])

    def test_primary_error_survives_a_cleanup_failure_and_only_the_owned_stage_remains(self):
        self.seed()
        keep = self.directory / "campaigns.json.keep.tmp"
        keep.write_bytes(b"unrelated sibling\n")
        with mock.patch.object(os, "replace", side_effect=OSError(errno.EXDEV, "primary failure")), \
                mock.patch.object(os, "unlink", side_effect=OSError(errno.EPERM, "cleanup failure")):
            with self.assertRaises(OSError) as caught:
                registry._store(self.register, [])
        self.assertIn("primary failure", str(caught.exception))
        self.assertEqual(self.register.read_text(encoding="utf-8"), OLD)
        left = self.others()
        self.assertEqual(len(left), 2)
        self.assertIn("campaigns.json.keep.tmp", left)
        self.assertTrue(all(n.startswith("campaigns.json.") and n.endswith(".tmp") for n in left))

    # -- the CLI handler ------------------------------------------------------------------------------------------------

    def test_cli_add_remove_staging_failure_is_stopped_and_retryable(self):
        # first-ever add: the transaction's existing empty lock/register file is created before the failing stage
        with mock.patch.object(os, "replace", side_effect=OSError(errno.EXDEV, "synthetic replace")):
            code, out, err = self.run_cli("add", "A", self.campaign)
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("Stopped: "))
        self.assertEqual(self.register.read_bytes(), b"")
        self.assertEqual(self.others(), [])
        code, out, err = self.run_cli("add", "A", self.campaign)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("registered", out)
        good = self.register.read_bytes()
        with mock.patch.object(os, "fsync", side_effect=OSError(errno.EIO, "synthetic fsync")):
            code, out, err = self.run_cli("remove", "A")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("Stopped: "))
        self.assertEqual(self.register.read_bytes(), good)
        self.assertEqual(self.others(), [])
        code, out, err = self.run_cli("remove", "A")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("unregistered", out)
        self.assertEqual(self.register.read_bytes(), b"[]\n")

    def test_remove_never_opens_campaign_and_staging_cleanup_is_narrow(self):
        global RECORD
        gone = self.base / "gone"
        unreadable = self.base / "unreadable"
        unreadable.mkdir()
        (unreadable / "secret.txt").write_bytes(b"synthetic campaign record\n")
        os.link(self.sentinel, self.legacy)
        rows = [dict(ROW, name="gone", directory=str(gone)), dict(ROW, name="locked", directory=str(unreadable))]
        self.seed(json.dumps(rows, indent=2) + "\n")
        unreadable.chmod(0o000)
        self.addCleanup(unreadable.chmod, 0o700)
        stages, real_mkstemp = [], tempfile.mkstemp

        def mkstemp(*args, **kwargs):
            result = real_mkstemp(*args, **kwargs)
            stages.append(result[1])
            return result
        with mock.patch.object(tempfile, "mkstemp", mkstemp):
            RECORD = []
            with mock.patch.object(os, "replace", side_effect=OSError(errno.EXDEV, "synthetic replace")):
                self.assertEqual(self.run_cli("remove", "gone")[0], 1)
            failed, RECORD = RECORD, []
            self.assertEqual(self.run_cli("remove", "gone")[0], 0)
            self.assertEqual(self.run_cli("remove", "locked")[0], 0)
            done, RECORD = RECORD, None
        self.assertEqual(len(stages), 3)
        for events in (failed, done):
            for event, path in events:
                for forbidden in (gone, unreadable, self.legacy, self.sentinel):
                    self.assertFalse(path == str(forbidden) or path.startswith(str(forbidden) + os.sep), (event, path))
        self.assertEqual([p for e, p in failed if e == "os.remove"], [stages[0]])  # exactly the stage it allocated
        self.assertEqual([p for e, p in done if e == "os.remove"], [])  # a successful publish deletes nothing
        self.assertEqual([(e, p) for e, p in done if e == "os.rename"], [("os.rename", stages[1]), ("os.rename", stages[2])])
        unreadable.chmod(0o700)
        self.assertEqual((unreadable / "secret.txt").read_bytes(), b"synthetic campaign record\n")
        self.assertEqual(self.register.read_bytes(), b"[]\n")


if __name__ == "__main__":
    unittest.main()
