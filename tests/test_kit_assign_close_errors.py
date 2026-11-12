"""RF1550-4: a failing close of the kit/root directory descriptor neither strands the other one nor masks an error.

Real descriptors; the injected fault really closes the descriptor first and then raises EIO. Synthetic only.
"""
import contextlib
import errno
import json
import os
import unittest

from campaign_tool import kit_assign
from tests import test_kit_assign as existing

REAL_OPEN, REAL_CLOSE, REAL_FSTAT = os.open, os.close, os.fstat
EIO = OSError(errno.EIO, "SYNTHETIC-CLOSE")


def alive(fd):
    try:
        REAL_FSTAT(fd)
        return True
    except OSError:
        return False


class CloseErrorTests(unittest.TestCase):
    setUp = existing.KitAssignTests.setUp
    ok = existing.KitAssignTests.ok
    put_sent = existing.KitAssignTests.put_sent
    write_store = existing.KitAssignTests.write_store

    def prepare(self):
        self.put_sent({self.a: existing.imported("2026-03-02"), self.b: existing.imported("2026-03-02")})
        self.write_store({self.a: "Prior"})
        self.before = self.store.read_bytes()

    @contextlib.contextmanager
    def faults(self, fail=(), fstat_error=None):
        """Record owned directory descriptors ("root", "kit"); close attempts per name; fail those named in ``fail``."""
        rec = {"fd": {}, "attempts": {"root": 0, "kit": 0}, "live": {}}
        by_fd = {}

        def opened(path, flags, *args, **kwargs):
            fd = REAL_OPEN(path, flags, *args, **kwargs)
            if flags & os.O_DIRECTORY:
                name = "kit" if path == "kit" and kwargs.get("dir_fd") is not None else \
                    "root" if str(path) == str(self.root) else None
                if name:
                    rec["fd"][name] = fd
                    by_fd[fd] = name
            return fd

        def closed(fd):
            name = by_fd.pop(fd, None)
            if name is None:
                return REAL_CLOSE(fd)
            rec["attempts"][name] += 1
            REAL_CLOSE(fd)
            if name in fail:
                raise OSError(errno.EIO, "SYNTHETIC-CLOSE")

        def fstat(fd):
            if fstat_error is not None and fd in by_fd and by_fd[fd] == "kit":
                raise fstat_error
            return REAL_FSTAT(fd)

        from unittest import mock
        with mock.patch("os.open", opened), mock.patch("os.close", closed), mock.patch("os.fstat", fstat):
            yield rec
        rec["live"] = {n: alive(fd) for n, fd in rec["fd"].items()}

    def test_root_close_failure_releases_admitted_kit_before_yield(self):
        self.prepare()
        ran = []
        for fail in (("root",), ("root", "kit")):
            with self.faults(fail) as rec:
                with self.assertRaises(OSError) as ctx:
                    with kit_assign._open_kit(self.root):
                        ran.append(1)
            self.assertEqual(ctx.exception.errno, errno.EIO)
            self.assertEqual(rec["attempts"], {"root": 1, "kit": 1})
            self.assertEqual(rec["live"], {"root": False, "kit": False})
        self.assertEqual(ran, [])
        with self.faults(("root",)) as rec:
            with self.assertRaises(OSError):
                kit_assign.read_assignments(self.root)
        self.assertFalse(rec["live"]["kit"])
        with self.faults(("root",)) as rec:
            with self.assertRaises(OSError):
                kit_assign.assign(self.root, self.a, to="Next")
        self.assertFalse(rec["live"]["kit"])
        self.assertEqual(self.store.read_bytes(), self.before)
        self.assertEqual(kit_assign.read_assignments(self.root), {self.a: "Prior"})

    def test_admission_failure_survives_both_directory_close_errors(self):
        self.prepare()
        for primary in (OSError(errno.EACCES, "primary"), RuntimeError("primary"), KeyboardInterrupt(), SystemExit(3)):
            with self.faults(("root", "kit"), fstat_error=primary) as rec:
                with self.assertRaises(type(primary)) as ctx:
                    with kit_assign._open_kit(self.root):
                        self.fail("body must not run")
            self.assertIs(ctx.exception, primary)
            self.assertEqual(rec["attempts"], {"root": 1, "kit": 1})
            self.assertEqual(rec["live"], {"root": False, "kit": False})

    def test_body_failure_survives_kit_close_error(self):
        self.prepare()
        for primary in (ValueError("primary"), RuntimeError("primary"), KeyboardInterrupt(), SystemExit(3)):
            with self.faults(("kit",)) as rec:
                with self.assertRaises(type(primary)) as ctx:
                    with kit_assign._open_kit(self.root):
                        raise primary
            self.assertIs(ctx.exception, primary)
            self.assertEqual(rec["attempts"], {"root": 1, "kit": 1})
        self.store.write_text("{not json", encoding="utf-8")
        with self.faults(("kit",)):
            with self.assertRaises(ValueError) as ctx:
                kit_assign.read_assignments(self.root)
        self.assertIn("unreadable", str(ctx.exception))
        self.assertEqual(self.store.read_text(encoding="utf-8"), "{not json")

    def test_close_only_failure_does_not_rollback_committed_assignment(self):
        self.prepare()
        with self.faults(("kit",)) as rec:
            with self.assertRaises(OSError):
                kit_assign.read_assignments(self.root)
        self.assertEqual(rec["attempts"]["kit"], 1)
        with self.faults(("kit",)):
            with self.assertRaises(OSError):
                kit_assign.assign(self.root, self.a, to="Prior")  # no-op: still an error, nothing written
        self.assertEqual(self.store.read_bytes(), self.before)
        with self.faults(("kit",)):
            with self.assertRaises(OSError):
                kit_assign.assign(self.root, self.a, to="Next")
        self.assertEqual(json.loads(self.store.read_text(encoding="utf-8"))["assignments"], {self.a: "Next"})
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)
        with self.faults(("kit",)):
            with self.assertRaises(OSError):
                kit_assign.assign(self.root, self.a, clear=True)
        self.assertEqual(json.loads(self.store.read_text(encoding="utf-8"))["assignments"], {})
        self.assertEqual(kit_assign.assign(self.root, self.a, to="Last")["changed"], True)  # healthy retry

    def test_close_error_never_retries_recycled_descriptor(self):
        self.prepare()
        sentinel = REAL_OPEN(os.devnull, os.O_RDONLY)
        self.addCleanup(lambda: alive(sentinel) and REAL_CLOSE(sentinel))
        real_close = os.close
        calls = []
        from unittest import mock

        def closed(fd):
            calls.append(fd)
            real_close(fd)
            if fd == self.kit_fd:
                raise OSError(errno.EIO, "SYNTHETIC-CLOSE")

        opened_fds = []

        def opened(path, flags, *args, **kwargs):
            fd = REAL_OPEN(path, flags, *args, **kwargs)
            if path == "kit":
                self.kit_fd = fd
            opened_fds.append(fd)
            return fd

        self.kit_fd = -1
        with mock.patch("os.open", opened), mock.patch("os.close", closed):
            with self.assertRaises(OSError):
                with kit_assign._open_kit(self.root):
                    pass
        self.assertEqual(calls.count(self.kit_fd), 1)
        self.assertTrue(alive(sentinel))
        # a recycled integer holding an unrelated descriptor is never closed by the release path
        self.assertEqual(len(calls), len(set(calls)))

    def test_absence_and_acquisition_failures_close_only_owned_resources(self):
        self.prepare()
        with self.faults(("root", "kit")) as rec:
            with kit_assign._open_kit(self.tmp / "absent") as kit_fd:
                self.assertIsNone(kit_fd)
        self.assertEqual(rec["attempts"], {"root": 0, "kit": 0})
        outside = self.tmp / "outside"
        outside.mkdir()
        os.rename(self.kit, self.tmp / "kit-away")
        with self.faults(("root",)) as rec:
            with self.assertRaises(OSError):
                with kit_assign._open_kit(self.root):
                    self.fail("no body on root release failure")
        self.assertEqual(rec["attempts"], {"root": 1, "kit": 0})
        self.kit.symlink_to(outside)
        with self.faults(("root",)) as rec:
            with self.assertRaises(ValueError) as ctx:
                kit_assign.read_assignments(self.root)
        self.assertIn("symlink", str(ctx.exception))
        self.assertEqual(rec["attempts"]["root"], 1)
        self.assertFalse(rec["live"]["root"])
        self.kit.unlink()
        self.kit.write_text("x", encoding="utf-8")
        with self.faults(("root",)) as rec:
            with self.assertRaises(ValueError) as ctx:
                kit_assign.read_assignments(self.root)
        self.assertIn("unreadable", str(ctx.exception))
        self.assertEqual(rec["attempts"]["root"], 1)

    def test_normal_scope_assignment_bytes_and_noop_are_compatible(self):
        self.prepare()
        self.assertEqual(kit_assign.read_assignments(self.root), {self.a: "Prior"})
        self.assertEqual(kit_assign.read_assignments(self.tmp / "absent"), {})
        self.assertFalse(kit_assign.assign(self.root, self.a, to="Prior")["changed"])
        self.assertEqual(self.store.read_bytes(), self.before)
        self.assertTrue(kit_assign.assign(self.root, self.b, to="Second")["changed"])
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)
        self.assertTrue(kit_assign.assign(self.root, self.b, clear=True)["changed"])
        self.assertEqual(json.loads(self.store.read_bytes())["assignments"], {self.a: "Prior"})
        self.assertFalse(kit_assign.assign(self.root, self.b, clear=True)["changed"])
        self.assertEqual(sorted(p.name for p in self.kit.iterdir() if p.name.endswith(".tmp")), [])


if __name__ == "__main__":
    unittest.main()
