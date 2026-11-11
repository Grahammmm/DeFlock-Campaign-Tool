"""kit-preserve failed-output rollback removes only the entry this call created. Offline, synthetic."""
import contextlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import test_kit_preserve as base  # noqa: E402  (module alias: its TestCase is not inherited)
from campaign_tool import cli as _cli  # noqa: E402,F401
# The CLI imports sibling modules lazily and some probe os.open at import; load them before os.open is mocked.
import importlib  # noqa: E402
for _name in ("kit_assign", "kit_track", "kit_sent", "kit_contact"):
    try:
        importlib.import_module(f"campaign_tool.{_name}")
    except ImportError:
        pass

A = base.A


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.fx = base.PreserveTests("test_main_named_and_followon_isolation")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.root = self.fx.root
        self.outside = self.root.parent / "outside.txt"
        self.outside.write_bytes(b"outside")
        self.folder = self.root / "kit" / "preservation"
        self.parked = self.root.parent / "parked-original"

    def leaf(self, request="main"):
        return self.folder / f"{A}--{request}.md"

    def substitute(self, kind, target):
        os.rename(target, self.parked)  # the genuine owned inode moves aside
        if kind == "file":
            target.write_bytes(b"foreign")
        elif kind == "symlink":
            os.symlink(self.outside, target)
        elif kind == "ownedlink":
            os.symlink(self.parked, target)
        elif kind == "hardlink":
            os.link(self.outside, target)
        elif kind == "directory":
            os.mkdir(target)
        elif kind == "fifo":
            os.mkfifo(target)
        elif kind == "ownedhardlink":
            os.link(self.parked, target)

    @contextlib.contextmanager
    def failing(self, request, phase, kind, error=None):
        """Forward real os.open/fdopen; at `phase` swap the leaf for `kind`, then raise `error`."""
        target = self.leaf(request)
        error = error or OSError(5, "synthetic failure")
        real_fdopen, real_close = os.fdopen, os.close
        events = {"fd": [], "closed": [], "order": []}
        real_open, real_unlink = os.open, os.unlink

        def opening(name, flags, *a, **k):
            fd = real_open(name, flags, *a, **k)
            if Path(name) == target:
                events["fd"].append(fd)
            return fd

        def hit():
            self.substitute(kind, target)
            raise error

        class Stream:
            def __init__(self, real):
                self.real = real

            def __enter__(self):
                return self

            def write(self, text):
                if phase == "write":
                    self.real.write(text[:10])
                    hit()
                self.real.write(text)

            def __exit__(self, *exc):
                self.real.close()
                if phase == "close" and not exc[0]:
                    hit()

        def fdopening(fd, *a, **k):
            if phase == "construct":
                hit()
            return Stream(real_fdopen(fd, *a, **k))

        def closing(fd):
            if events["fd"] and fd == events["fd"][0]:
                events["closed"].append(fd)
                events["order"].append("close")
            return real_close(fd)

        def unlinking(name, *a, **k):
            events["order"].append("unlink:" + str(Path(name).name))
            if events["fd"]:
                os.fstat(events["fd"][0])  # still pinned (raises EBADF if closed)
            return real_unlink(name, *a, **k)

        with mock.patch("os.open", side_effect=opening), mock.patch("os.fdopen", side_effect=fdopening), \
                mock.patch("os.close", side_effect=closing), mock.patch("os.unlink", side_effect=unlinking), \
                mock.patch("socket.socket", side_effect=AssertionError("network used")):
            yield error, events

    def check_fd_closed(self, events):
        self.assertEqual(len(events["fd"]), 1)
        self.assertEqual(events["closed"], events["fd"])
        with self.assertRaises(OSError):
            os.fstat(events["fd"][0])

    def test_foreign_entries_survive_every_failure_phase(self):
        self.fx.send(A, "named", "2026-03-02")
        cases = {"file": b"foreign", "symlink": None, "ownedlink": None, "hardlink": b"outside"}
        for request in ("main", "named"):
            for phase in ("construct", "write", "close"):
                for kind, content in cases.items():
                    with self.subTest(request=request, phase=phase, kind=kind):
                        target = self.leaf(request)
                        with self.failing(request, phase, kind) as (error, events):
                            with self.assertRaises(OSError) as caught:
                                self.fx.make(A, request)
                        self.assertIs(caught.exception, error)
                        self.check_fd_closed(events)
                        self.assertNotIn("unlink:" + target.name, events["order"])
                        self.assertEqual(os.path.islink(target), kind in ("symlink", "ownedlink"))
                        if content is not None:
                            self.assertEqual(target.read_bytes(), content)
                        self.assertEqual(self.outside.read_bytes(), b"outside")
                        self.assertTrue(self.parked.exists())
                        os.unlink(target)
                        os.unlink(self.parked)

    def test_cleanup_skips_missing_directory_fifo_and_keeps_primary(self):
        for kind in ("missing", "directory", "fifo", "ownedhardlink"):
            for error in (OSError(5, "x"), ValueError("x"), MemoryError(), KeyboardInterrupt()):
                with self.subTest(kind=kind, error=type(error).__name__):
                    target = self.leaf()
                    with self.failing("main", "construct", kind, error) as (_, events):
                        with self.assertRaises(type(error)) as caught:
                            self.fx.make()
                    self.assertIs(caught.exception, error)
                    self.check_fd_closed(events)
                    if kind == "missing":
                        self.assertFalse(os.path.lexists(target))
                    elif kind == "ownedhardlink":
                        # same inode reached through another name is still this call's file: removed
                        self.assertFalse(os.path.lexists(target))
                    else:
                        self.assertTrue(os.path.lexists(target))
                        (os.rmdir if kind == "directory" else os.unlink)(target)
                    os.unlink(self.parked)

    def test_unchanged_partial_file_is_removed_and_retry_works(self):
        baseline = self.fx.make()
        expected = self.leaf().read_bytes()
        self.leaf().unlink()
        for phase in ("construct", "write", "close"):
            with self.subTest(phase=phase):
                # no substitution: kind "none" leaves the (renamed) file; emulate by not parking
                target = self.leaf()
                with self.failing("main", phase, "unchanged") as (error, events):
                    with mock.patch.object(self, "substitute", lambda kind, t: None):
                        with self.assertRaises(OSError) as caught:
                            self.fx.make()
                self.assertIs(caught.exception, error)
                self.check_fd_closed(events)
                self.assertFalse(os.path.lexists(target))
                self.assertTrue(self.folder.is_dir())
        self.assertEqual(self.fx.make(), baseline)
        self.assertEqual(self.leaf().read_bytes(), expected)
        self.assertEqual(oct(self.leaf().stat().st_mode & 0o777), "0o600")

    def test_uncertain_ownership_and_unlink_error_keep_entry_and_primary(self):
        real_stat, real_fstat = os.stat, os.fstat
        for mode in ("stat", "fstat", "unlink"):
            with self.subTest(mode=mode):
                target = self.leaf()
                with self.failing("main", "construct", "file") as (error, events):
                    if mode == "stat":
                        patch = mock.patch("os.stat", side_effect=lambda p, *a, **k: (_ for _ in ()).throw(
                            PermissionError(13, "x")) if Path(p) == target else real_stat(p, *a, **k))
                    elif mode == "fstat":
                        patch = mock.patch("os.fstat", side_effect=lambda fd: (_ for _ in ()).throw(
                            OSError(5, "fstat")) if events["fd"] and fd == events["fd"][0] else real_fstat(fd))
                    else:
                        # owned entry (no substitution) but unlink fails
                        patch = contextlib.nullcontext()
                    with patch:
                        if mode == "unlink":
                            with mock.patch.object(self, "substitute", lambda k, t: None), \
                                    mock.patch("os.unlink", side_effect=PermissionError(13, "unlink")):
                                with self.assertRaises(OSError) as caught:
                                    self.fx.make()
                        else:
                            with self.assertRaises(OSError) as caught:
                                self.fx.make()
                self.assertIs(caught.exception, error)
                self.assertTrue(os.path.lexists(target))
                self.assertEqual(len(events["closed"]), 1)
                os.unlink(target)
                if mode != "unlink":
                    os.unlink(self.parked)

    def test_pinned_until_rollback_then_single_close_and_close_error_keeps_primary(self):
        target = self.leaf()
        with self.failing("main", "write", "unchanged") as (error, events):
            with mock.patch.object(self, "substitute", lambda k, t: None):
                with self.assertRaises(OSError) as caught:
                    self.fx.make()
        self.assertIs(caught.exception, error)
        self.assertEqual(events["order"], ["unlink:" + target.name, "close"])
        real_close = os.close
        primary = PermissionError(13, "primary")

        def failing_close(fd):
            real_close(fd)
            if events["fd"] and fd == events["fd"][0]:
                raise OSError(5, "close")

        with self.failing("main", "construct", "unchanged", primary) as (_, events):
            with mock.patch.object(self, "substitute", lambda k, t: None):
                with mock.patch("os.close", side_effect=lambda fd: (events["closed"].append(fd) if events["fd"] and fd == events["fd"][0] else None, failing_close(fd))):
                    with self.assertRaises(PermissionError) as caught:
                        self.fx.make()
        self.assertIs(caught.exception, primary)
        self.assertEqual(len(events["closed"]), 1)
        self.assertFalse(os.path.lexists(target))

    def test_final_close_error_on_success_propagates_and_keeps_output(self):
        real_open, real_close = os.open, os.close
        target = self.leaf()
        seen = []

        def opening(name, *a, **k):
            fd = real_open(name, *a, **k)
            if Path(name) == target:
                seen.append(fd)
            return fd

        def closing(fd):
            real_close(fd)
            if seen and fd == seen[0]:
                raise OSError(5, "final close")

        with mock.patch("os.open", side_effect=opening), mock.patch("os.close", side_effect=closing):
            with self.assertRaisesRegex(OSError, "final close"):
                self.fx.make()
        self.assertTrue(target.is_file())
        self.assertGreater(target.stat().st_size, 0)

    def test_actual_cli_keeps_replacement_and_original_stopped_error(self):
        self.fx.send(A, "named", "2026-03-02")
        for request in ("main", "named"):
            for phase in ("construct", "write", "close"):
                for kind in ("file", "directory", "missing"):
                    with self.subTest(request=request, phase=phase, kind=kind):
                        target = self.leaf(request)
                        with self.failing(request, phase, kind, OSError(5, "synthetic")):
                            code, out, err = base.run("kit-preserve", "--directory", self.root, "--agency", A,
                                                      "--request", request)
                        self.assertEqual((code, out), (1, ""))
                        self.assertIn("Stopped:", err)
                        self.assertNotIn("Traceback", err)
                        self.assertNotIn("Nothing was sent", err)
                        if kind == "file":
                            self.assertEqual(target.read_bytes(), b"foreign")
                            code, out, err = base.run("kit-preserve", "--directory", self.root, "--agency", A,
                                                      "--request", request)
                            self.assertEqual(code, 1)  # retry at the preserved name still refuses
                        if os.path.isdir(target) and not os.path.islink(target):
                            os.rmdir(target)
                        elif os.path.lexists(target):
                            os.unlink(target)
                        os.unlink(self.parked)
                        code, out, err = base.run("kit-preserve", "--directory", self.root, "--agency", A,
                                                  "--request", request)
                        self.assertEqual(code, 0)
                        os.unlink(target)


if __name__ == "__main__":
    unittest.main()
