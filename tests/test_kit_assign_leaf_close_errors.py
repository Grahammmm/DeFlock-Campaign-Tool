"""RF1045-1: a failing release of the assignments leaf descriptor or stream never masks the original failure.

Real files and descriptors. Each fault is local to the assignments.json leaf: a close fault really closes the
descriptor first and then raises EIO carrying a private canary, so a leak of that text would be visible.
Synthetic only; nothing is sent.
"""
import contextlib
import errno
import json
import os
import unittest
from unittest import mock

from campaign_tool import kit_assign
from tests import test_kit_assign as existing

REAL_OPEN, REAL_CLOSE, REAL_FSTAT, REAL_FDOPEN = os.open, os.close, os.fstat, os.fdopen
CANARY = "SYNTHETIC_PRIVATE_CLOSE_CANARY"
FIXED = "kit/assignments.json unreadable: file changed or could not be read"
UNEXPECTED = (MemoryError("primary"), RuntimeError("primary"), KeyboardInterrupt(), SystemExit(3))


def alive(fd):
    try:
        REAL_FSTAT(fd)
        return True
    except OSError:
        return False


def eio():
    return OSError(errno.EIO, CANARY)


class Stream:
    """A real owning stream; close() really releases the file, then optionally reports EIO."""

    def __init__(self, real, rec, read_error=None, close_error=None, reuse=False):
        self.real, self.rec, self.read_error, self.close_error, self.reuse = real, rec, read_error, close_error, reuse
        self.fd = real.fileno()

    def read(self, size=-1):
        self.rec["reads"] += 1
        if self.read_error is not None:
            raise self.read_error
        return self.real.read(size)

    def close(self):
        self.rec["stream_closes"] += 1
        self.real.close()
        if self.reuse:
            self.rec["sentinel"] = REAL_OPEN(os.devnull, os.O_RDONLY)
        if self.close_error is not None:
            raise self.close_error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class LeafTests(unittest.TestCase):
    setUp = existing.KitAssignTests.setUp
    ok = existing.KitAssignTests.ok
    stopped = existing.KitAssignTests.stopped
    put_sent = existing.KitAssignTests.put_sent
    write_store = existing.KitAssignTests.write_store
    stored = existing.KitAssignTests.stored

    def prepare(self):
        self.put_sent({self.a: existing.imported("2026-03-02"), self.b: existing.imported("2026-03-02")})
        self.write_store({self.a: "Prior"})
        self.before = self.store.read_bytes()

    @contextlib.contextmanager
    def faults(self, raw_close=False, fstat_error=None, fdopen_error=None, stream=None, reuse=False, before_open=None):
        """Track the leaf descriptor and the directory descriptors; inject only the named faults on the leaf.

        raw_close: the raw leaf close really closes, then raises EIO. fstat_error: the leaf's fstat raises it.
        fdopen_error: the stream constructor raises it. stream: dict of Stream keywords for a wrapped real stream.
        reuse: after a real close, an unrelated sentinel descriptor takes the freed number before EIO is reported.
        before_open: called just before the leaf is opened (a real swap or growth of the file)."""
        rec = {"leaf": None, "dirs": [], "raw_closes": 0, "stream_closes": 0, "reads": 0, "fdopens": 0,
               "closes": [], "sentinel": None}

        def opened(path, flags, *args, **kwargs):
            if path == "assignments.json" and kwargs.get("dir_fd") is not None and before_open:
                before_open()
            fd = REAL_OPEN(path, flags, *args, **kwargs)
            if flags & os.O_DIRECTORY:
                rec["dirs"].append(fd)
            elif path == "assignments.json" and kwargs.get("dir_fd") is not None:
                rec["leaf"] = fd
            return fd

        def closed(fd):
            if rec["leaf"] is not None:  # closes after the leaf was opened; earlier ones may reuse the number
                rec["closes"].append(fd)
            if fd != rec["leaf"]:
                return REAL_CLOSE(fd)
            rec["raw_closes"] += 1
            REAL_CLOSE(fd)
            if reuse:
                rec["sentinel"] = REAL_OPEN(os.devnull, os.O_RDONLY)
            if raw_close:
                raise eio()

        def fstat(fd):
            if fstat_error is not None and fd == rec["leaf"]:
                raise fstat_error
            return REAL_FSTAT(fd)

        def fdopen(fd, *args, **kwargs):
            rec["fdopens"] += 1
            if fdopen_error is not None:
                raise fdopen_error
            real = REAL_FDOPEN(fd, *args, **kwargs)
            return Stream(real, rec, reuse=reuse, **stream) if stream is not None else real

        try:
            with mock.patch("os.open", opened), mock.patch("os.close", closed), mock.patch("os.fstat", fstat), \
                    mock.patch("os.fdopen", fdopen):
                yield rec
        finally:
            rec["sentinel_alive"] = rec["sentinel"] is not None and alive(rec["sentinel"])
            if rec["sentinel_alive"]:  # the unrelated sentinel is the test's to release, not the reader's
                REAL_CLOSE(rec["sentinel"])
            rec["dirs_alive"] = [fd for fd in rec["dirs"] if alive(fd)]

    def assert_private(self, text):
        self.assertNotIn(CANARY, text)
        self.assertNotIn("Errno", text)

    def assert_unchanged(self):
        self.assertEqual(self.store.read_bytes(), self.before)

    # ---- raw descriptor, before the stream owns it -------------------------------------------------------------

    def test_fstat_and_admission_errors_survive_raw_close_eio(self):
        self.prepare()
        with self.faults(raw_close=True, fstat_error=OSError(errno.EACCES, "SYNTHETIC-FSTAT")) as rec:
            with self.assertRaises(ValueError) as ctx:
                kit_assign.read_assignments(self.root)
        self.assertEqual(str(ctx.exception), FIXED)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertEqual((rec["raw_closes"], rec["fdopens"], rec["reads"], rec["dirs_alive"]), (1, 0, 0, []))

        def grow():  # really grows past the cap after the early size check
            with open(self.store, "ab") as handle:
                handle.write(b" " * (kit_assign.MAX_BYTES + 10))

        with self.faults(raw_close=True, before_open=grow) as rec:
            with self.assertRaises(ValueError) as ctx:
                kit_assign.read_assignments(self.root)
        self.assertEqual(str(ctx.exception), "kit/assignments.json unreadable: larger than 1 MiB")
        self.assertEqual((rec["raw_closes"], rec["fdopens"], rec["reads"], rec["dirs_alive"]), (1, 0, 0, []))

        def swap():  # really replaced by a FIFO after the early regular-file check
            self.store.unlink()
            os.mkfifo(self.store)

        self.prepare()
        with self.faults(raw_close=True, before_open=swap) as rec:
            with self.assertRaises(ValueError) as ctx:
                kit_assign.read_assignments(self.root)
        self.assertEqual(str(ctx.exception), "kit/assignments.json unreadable: not a regular file")
        self.assertEqual((rec["raw_closes"], rec["fdopens"], rec["reads"], rec["dirs_alive"]), (1, 0, 0, []))

    def test_constructor_errors_keep_original_class_and_identity(self):
        self.prepare()
        with self.faults(raw_close=True, fdopen_error=OSError(errno.EMFILE, "SYNTHETIC-FDOPEN")) as rec:
            with mock.patch.object(kit_assign.json, "loads", side_effect=AssertionError("parsed")):
                with self.assertRaises(ValueError) as ctx:
                    kit_assign.read_assignments(self.root)
        self.assertEqual(str(ctx.exception), FIXED)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertEqual((rec["raw_closes"], rec["reads"], rec["dirs_alive"]), (1, 0, []))
        for primary in UNEXPECTED:
            with self.faults(raw_close=True, fdopen_error=primary) as rec:
                with mock.patch.object(kit_assign.json, "loads", side_effect=AssertionError("parsed")):
                    with self.assertRaises(type(primary)) as ctx:
                        kit_assign.read_assignments(self.root)
            self.assertIs(ctx.exception, primary)
            self.assertEqual((rec["raw_closes"], rec["reads"], rec["dirs_alive"]), (1, 0, []))

    # ---- transferred stream ------------------------------------------------------------------------------------

    def test_read_error_wins_over_stream_close_eio(self):
        self.prepare()
        with self.faults(stream={"read_error": OSError(errno.EIO, "SYNTHETIC-READ"), "close_error": eio()}) as rec:
            with self.assertRaises(ValueError) as ctx:
                kit_assign.read_assignments(self.root)
        self.assertEqual(str(ctx.exception), FIXED)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertEqual((rec["stream_closes"], rec["raw_closes"], rec["dirs_alive"]), (1, 0, []))
        for primary in UNEXPECTED:
            with self.faults(stream={"read_error": primary, "close_error": eio()}) as rec:
                with self.assertRaises(type(primary)) as ctx:
                    kit_assign.read_assignments(self.root)
            self.assertIs(ctx.exception, primary)
            self.assertEqual((rec["stream_closes"], rec["raw_closes"], rec["dirs_alive"]), (1, 0, []))
        with self.faults(stream={"read_error": RuntimeError("primary"), "close_error": RuntimeError("secondary")}):
            with self.assertRaises(RuntimeError):  # only a secondary OSError is subordinate
                kit_assign.read_assignments(self.root)
        self.assert_unchanged()

    def test_close_only_error_refuses_success_privately(self):
        self.prepare()
        with self.faults(stream={"close_error": eio()}) as rec:
            with self.assertRaises(ValueError) as ctx:
                kit_assign.read_assignments(self.root)
        self.assertEqual(str(ctx.exception), FIXED)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertEqual((rec["reads"], rec["stream_closes"], rec["raw_closes"], rec["dirs_alive"]), (1, 1, 0, []))
        secondary = RuntimeError("fresh cleanup failure")
        with self.faults(stream={"close_error": secondary}):
            with self.assertRaises(RuntimeError) as ctx:
                kit_assign.read_assignments(self.root)
        self.assertIs(ctx.exception, secondary)
        with self.faults(stream={"close_error": eio()}):
            err = self.stopped("kit-status", "--directory", self.root, words=("kit/assignments.json unreadable",))
        self.assertEqual(err, f"Stopped: {FIXED}\n")
        self.assert_private(err)
        self.assertEqual(self.ok("kit-status", "--directory", self.root).count("Prior"), 1)  # healthy retry
        self.assertEqual(kit_assign.read_assignments(self.root), {self.a: "Prior"})

    def test_reused_descriptor_is_never_closed_again(self):
        self.prepare()
        with self.faults(raw_close=True, reuse=True, fstat_error=OSError(errno.EACCES, "x")) as rec:
            with self.assertRaises(ValueError):
                kit_assign.read_assignments(self.root)
        self.assertEqual(rec["closes"].count(rec["leaf"]), 1)  # the one real attempt; the reused number is untouched
        self.assertEqual(rec["sentinel"], rec["leaf"])
        self.assertTrue(rec["sentinel_alive"])
        with self.faults(raw_close=True, reuse=True, fdopen_error=RuntimeError("primary")) as rec:
            with self.assertRaises(RuntimeError):
                kit_assign.read_assignments(self.root)
        self.assertEqual(rec["closes"].count(rec["leaf"]), 1)
        with self.faults(reuse=True, stream={"close_error": eio()}) as rec:
            with self.assertRaises(ValueError):
                kit_assign.read_assignments(self.root)
        self.assertEqual(rec["closes"].count(rec["leaf"]), 0)  # transferred: never os.close()d by the reader
        self.assertEqual(rec["sentinel"], rec["leaf"])
        self.assertTrue(rec["sentinel_alive"])
        with self.faults(reuse=True, stream={"read_error": RuntimeError("primary"), "close_error": eio()}) as rec:
            with self.assertRaises(RuntimeError):
                kit_assign.read_assignments(self.root)
        self.assertEqual(rec["closes"].count(rec["leaf"]), 0)
        self.assertEqual(rec["stream_closes"], 1)

    # ---- real CLI consumers ------------------------------------------------------------------------------------

    def snapshot(self):
        return (existing.tree(self.root), {p.name: p.stat().st_mode & 0o777 for p in self.kit.iterdir()},
                sorted(p.name for p in self.kit.iterdir()),
                {n: (self.kit / n).read_bytes() for n in ("assignments.json", "sent.json", "agencies.json")})

    def test_cli_set_clear_noop_and_views_preserve_store_on_refusal(self):
        self.prepare()
        kinds = {"leaf fstat": {"raw_close": True, "fstat_error": OSError(errno.EACCES, "SYNTHETIC-FSTAT")},
                 "read": {"stream": {"read_error": OSError(errno.EIO, "SYNTHETIC-READ"), "close_error": eio()}}}
        commands = (("kit-assign", "--agency", self.a, "--to", "Next"),
                    ("kit-assign", "--agency", self.a, "--to", "Prior"),  # same owner: a no-op when readable
                    ("kit-assign", "--agency", self.a, "--clear"),
                    ("kit-assign", "--agency", self.b, "--clear"),  # already clear: a no-op when readable
                    ("kit-status",), ("kit-due", *existing.ASOF))
        before = self.snapshot()
        for kind, faults in kinds.items():
            for command in commands:
                with self.subTest(kind=kind, command=command), self.faults(**faults):
                    err = self.stopped(command[0], "--directory", self.root, *command[1:],
                                       words=("kit/assignments.json unreadable",))
                    self.assertEqual(err, f"Stopped: {FIXED}\n")
                    self.assert_private(err)
                self.assertEqual(self.snapshot(), before)
        with self.faults(**kinds["read"]):  # a view that reads the store elsewhere also stays private and read-only
            code, out, err = existing.run("today", "--directory", self.root, *existing.ASOF, "--json")
        self.assert_private(out + err)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(existing.run("kit-assign", "--directory", self.root, "--agency", self.a, "--to", "Next")[0] == 0)

    # ---- unchanged contracts -----------------------------------------------------------------------------------

    def test_healthy_and_single_failure_contracts_are_unchanged(self):
        self.assertEqual(kit_assign.read_assignments(self.root), {})  # absent store
        self.assertEqual(kit_assign.read_assignments(self.tmp / "absent"), {})
        self.prepare()
        self.write_store({self.a: "Alex", self.b: "Zoë"})
        self.assertEqual(kit_assign.read_assignments(self.root), {self.a: "Alex", self.b: "Zoë"})
        for name, content, reason in (
                ("not json", b"{not json", "not JSON, or a key is repeated"),
                ("duplicate", b'{"schema_version":1,"assignments":{},"schema_version":1}',
                 "not JSON, or a key is repeated"),
                ("early cap", b" " * (kit_assign.MAX_BYTES + 1), "larger than 1 MiB"),
                ("schema", b'{"schema_version":2,"assignments":{}}', "expected schema_version 1 and an assignments")):
            with self.subTest(store=name):
                self.store.write_bytes(content)
                with self.assertRaises(ValueError) as ctx:
                    kit_assign.read_assignments(self.root)
                self.assertTrue(str(ctx.exception).startswith(f"kit/assignments.json unreadable: {reason}"),
                                str(ctx.exception))
        self.store.unlink()
        os.mkfifo(self.store)  # no-follow, nonblocking: a FIFO is refused without blocking
        with self.assertRaises(ValueError) as ctx:
            kit_assign.read_assignments(self.root)
        self.assertEqual(str(ctx.exception), "kit/assignments.json unreadable: not a regular file")
        self.store.unlink()
        self.write_store({self.a: "Prior"})
        with self.faults() as rec:  # a healthy read closes the leaf exactly once through the stream, never raw
            self.assertEqual(kit_assign.read_assignments(self.root), {self.a: "Prior"})
        self.assertEqual((rec["raw_closes"], rec["dirs_alive"]), (0, []))
        self.assertFalse(kit_assign.assign(self.root, self.a, to="Prior")["changed"])
        self.assertTrue(kit_assign.assign(self.root, self.b, to="Second")["changed"])
        self.assertEqual(self.stored(), {self.a: "Prior", self.b: "Second"})
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
