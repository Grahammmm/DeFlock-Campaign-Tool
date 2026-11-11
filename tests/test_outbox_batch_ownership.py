"""Batch-export member descriptors stay pinned until ownership cleanup finishes (synthetic data only)."""
import errno
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from campaign_tool import outbox as ob, outbox_batch as batch

REAL_OPEN, REAL_FSTAT, REAL_STAT, REAL_UNLINK = os.open, os.fstat, os.stat, os.unlink
FAILED = "partial local output remains"


def fd_open(fd):
    try:
        REAL_FSTAT(fd)
        return True
    except OSError:
        return False


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.box = ob.Outbox(self.root / "private" / "outbox.sqlite")
        self.addCleanup(self.box.close)
        self.out = self.root / "letters"
        for target in ("send", "send_email", "file_muckrock"):
            owner = ob.Outbox if target == "send" else ob
            guard = patch.object(owner, target, side_effect=AssertionError("batch must never send"))
            guard.start()
            self.addCleanup(guard.stop)

    def propose(self, request="one", agency="Agency A"):
        draft = ob.RequestDraft(request, agency, 1, "email", "Synthetic letter",
                                f"Dear {agency}, records please.", "records@example.invalid")
        return self.box.propose(draft)["idempotency_key"]

    def keys(self, count=2):
        return sorted(self.propose(f"r{i}", f"Agency {i}") for i in range(count))

    @staticmethod
    def hooked_open(on_open, fail_at=None):
        """Forward os.open; call on_open(n, fd) after the n-th member creation, or fail the n-th."""
        state = {"n": 0, "fds": []}

        def opened(path, flags, *args, **kwargs):
            if flags & os.O_EXCL:
                state["n"] += 1
                if state["n"] == fail_at:
                    raise OSError(errno.ENOSPC, "synthetic open failure")
            fd = REAL_OPEN(path, flags, *args, **kwargs)
            if flags & os.O_EXCL:
                state["fds"].append(fd)
                if on_open:
                    on_open(state["n"], fd)
            return fd
        return state, opened

    def test_completed_payload_fd_is_pinned_until_export_cleanup_finishes(self):
        keys = self.keys()
        seen = {}
        original = {}

        def on_open(n, fd):
            if n == 1:
                original["id"] = (REAL_FSTAT(fd).st_dev, REAL_FSTAT(fd).st_ino)
            if n == 2:  # first wrapper has already closed; first fd must still be live and identical
                first = state["fds"][0]
                seen["during_open"] = fd_open(first) and (REAL_FSTAT(first).st_dev, REAL_FSTAT(first).st_ino) == original["id"]

        real_unlink = os.unlink

        def unlink(name, *args, **kwargs):
            first = state["fds"][0]
            seen.setdefault("during_unlink", []).append(fd_open(first) and fd_open(state["fds"][1]))
            return real_unlink(name, *args, **kwargs)

        state, opened = self.hooked_open(on_open, fail_at=3)  # INDEX creation fails
        with patch.object(batch.os, "open", side_effect=opened), patch.object(batch.os, "unlink", side_effect=unlink):
            with self.assertRaises(ob.OutboxError):
                batch.export(self.box, self.out)
        self.assertTrue(seen["during_open"])
        self.assertEqual(seen["during_unlink"], [True, True])
        self.assertFalse(any(fd_open(fd) for fd in state["fds"]))
        self.assertFalse(self.out.exists())
        self.assertEqual(len(keys), 2)

    def test_unlinked_and_recreated_completed_payload_is_foreign(self):
        keys = self.keys()
        for existing in (False, True):
            with self.subTest(existing=existing):
                out = self.root / ("existing" if existing else "absent")
                if existing:
                    out.mkdir()
                target = out / (keys[0] + ".txt")

                def on_open(n, fd):
                    if n == 2:
                        target.unlink()
                        target.write_bytes(b"foreign")

                state, opened = self.hooked_open(on_open, fail_at=3)
                with patch.object(batch.os, "open", side_effect=opened):
                    with self.assertRaisesRegex(ob.OutboxError, FAILED):
                        batch.export(self.box, out)
                self.assertEqual(target.read_bytes(), b"foreign")
                self.assertTrue(out.is_dir())
                self.assertFalse(any(fd_open(fd) for fd in state["fds"]))

    def test_unlinked_and_recreated_payload_via_cli_refuses(self):
        keys = self.keys()
        target = self.out / (keys[0] + ".txt")

        def on_open(n, fd):
            if n == 2:
                target.unlink()
                target.write_bytes(b"foreign")

        state, opened = self.hooked_open(on_open, fail_at=3)
        buf = io.StringIO()
        with patch.object(batch.os, "open", side_effect=opened), redirect_stdout(buf):
            code = ob.main(["--journal", str(self.box.path), "payload-batch", "--out", str(self.out)])
        self.assertEqual(code, 2)
        rows = [json.loads(line) for line in buf.getvalue().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "refused")
        self.assertIn(FAILED, rows[0]["reason"])
        self.assertEqual(target.read_bytes(), b"foreign")

    def test_partial_member_stream_exit_uses_a_still_pinned_original(self):
        self.keys(1)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        for kind in ("regular", "symlink", "directory", "missing"):
            with self.subTest(kind=kind):
                name = "member" + kind
                target = self.root / name
                foreign = self.root / ("foreign" + kind)
                foreign.write_bytes(b"foreign")
                raw_fds = []
                real_fdopen = os.fdopen

                class Failing:
                    def __init__(inner, real):
                        inner.real = real

                    def __enter__(inner):
                        return inner

                    def write(inner, data):
                        inner.real.write(data)

                    def __exit__(inner, *args):
                        inner.real.close()
                        raw_fds.append(inner.real.fileno() if not inner.real.closed else None)
                        target.unlink()
                        if kind == "regular":
                            target.write_bytes(b"replacement")
                        elif kind == "symlink":
                            target.symlink_to(foreign)
                        elif kind == "directory":
                            target.mkdir()
                        raise OSError(errno.EIO, "synthetic close failure")

                created = []

                def opened(path, flags, *a, **k):
                    result = REAL_OPEN(path, flags, *a, **k)
                    created.append(result)
                    return result

                def wrapped(*a, **k):
                    return Failing(real_fdopen(*a, **k))

                with patch.object(batch.os, "open", side_effect=opened), patch.object(batch.os, "fdopen", side_effect=wrapped):
                    if kind == "missing":
                        with self.assertRaises(OSError):
                            batch._write(fd, name, b"data")
                    else:
                        with self.assertRaisesRegex(ob.OutboxError, FAILED):
                            batch._write(fd, name, b"data")
                self.assertFalse(fd_open(created[0]))  # released on the failure path
                if kind == "regular":
                    self.assertEqual(target.read_bytes(), b"replacement")
                elif kind == "symlink":
                    self.assertTrue(target.is_symlink())
                elif kind == "directory":
                    self.assertTrue(target.is_dir())
                else:
                    self.assertFalse(os.path.lexists(target))
                self.assertEqual(foreign.read_bytes(), b"foreign")

    def test_retained_members_close_on_success_and_every_acquisition_failure(self):
        self.keys()
        state, opened = self.hooked_open(None)
        with patch.object(batch.os, "open", side_effect=opened):
            result = batch.export(self.box, self.out)
        self.assertEqual(result["written"], 2)
        self.assertEqual(len(state["fds"]), 3)
        self.assertFalse(any(fd_open(fd) for fd in state["fds"]))
        for member in sorted(self.out.iterdir()):
            self.assertEqual(member.stat().st_mode & 0o777, 0o600)
        for attr, args in (("fdopen", None), ("fchmod", None)):
            for number in (1, 2):
                with self.subTest(attr=attr, number=number):
                    out = self.root / f"fail-{attr}-{number}"
                    state, opened = self.hooked_open(None)
                    real = getattr(os, attr)
                    count = {"n": 0}

                    def failing(*a, _real=real, _count=count, _number=number, **k):
                        _count["n"] += 1
                        if _count["n"] == _number:
                            raise OSError(errno.EIO, "synthetic")
                        return _real(*a, **k)

                    with patch.object(batch.os, "open", side_effect=opened), patch.object(batch.os, attr, side_effect=failing):
                        with self.assertRaises(ob.OutboxError):
                            batch.export(self.box, out)
                    self.assertFalse(any(fd_open(fd) for fd in state["fds"]))
                    self.assertFalse(out.exists())

    def test_descriptor_pressure_rolls_back_without_early_release(self):
        self.keys(4)
        self.out.mkdir()
        self.out.chmod(0o750)
        live = {}

        def on_open(n, fd):
            if n == 4:
                live["earlier"] = [fd_open(x) for x in state["fds"][:3]]

        state, opened = self.hooked_open(on_open)
        failing_open = {"n": 0}

        def pressure(path, flags, *a, **k):
            if flags & os.O_EXCL:
                failing_open["n"] += 1
                if failing_open["n"] == 5:
                    live["at_fault"] = [fd_open(x) for x in state["fds"]]
                    raise OSError(errno.EMFILE, "synthetic too many open files")
            return opened(path, flags, *a, **k)

        with patch.object(batch.os, "open", side_effect=pressure):
            with self.assertRaises(ob.OutboxError):
                batch.export(self.box, self.out)
        self.assertEqual(live["earlier"], [True] * 3)
        self.assertEqual(live["at_fault"], [True] * 4)
        self.assertFalse(any(fd_open(x) for x in state["fds"]))
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o750)
        self.assertEqual(batch.export(self.box, self.out)["written"], 4)
        self.assertEqual(len(list(self.out.iterdir())), 5)

    def test_uncertain_or_failed_ownership_cleanup_preserves_foreign_and_continues(self):
        keys = self.keys(2)
        first, second = self.out / (keys[0] + ".txt"), self.out / (keys[1] + ".txt")
        state, opened = self.hooked_open(None, fail_at=3)
        real_stat = os.stat

        def failing_stat(name, *a, **k):
            if name == keys[0] + ".txt" and k.get("dir_fd") is not None:
                raise OSError(errno.EIO, "synthetic stat failure")
            return real_stat(name, *a, **k)

        with patch.object(batch.os, "open", side_effect=opened), patch.object(batch.os, "stat", side_effect=failing_stat):
            with self.assertRaisesRegex(ob.OutboxError, FAILED):
                batch.export(self.box, self.out)
        self.assertTrue(first.exists())  # uncertain: kept
        self.assertFalse(second.exists())  # still attempted and removed
        self.assertFalse(any(fd_open(x) for x in state["fds"]))

    def test_success_and_ordinary_retry_bytes_unchanged(self):
        self.keys()
        state, opened = self.hooked_open(None, fail_at=2)
        with patch.object(batch.os, "open", side_effect=opened):
            with self.assertRaises(ob.OutboxError):
                batch.export(self.box, self.out)
        self.assertFalse(self.out.exists())
        self.assertEqual(batch.export(self.box, self.out)["written"], 2)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)
        names = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(len(names), 3)
        self.assertIn("INDEX.md", names)
        for p in self.out.iterdir():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertTrue(stat.S_ISREG(p.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
