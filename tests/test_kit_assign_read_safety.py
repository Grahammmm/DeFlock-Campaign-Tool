"""RF1255-3: kit/assignments.json is read through one no-follow, nonblocking, regular-file descriptor. Synthetic only.

A leaf replacement is injected immediately before the reader's real os.open, so the production open, fstat and read
are real. The outside file carries a unique canary owner that must never reach a view or a write.
"""
import contextlib
import errno
import json
import os
import signal
import unittest
from unittest import mock

from campaign_tool import kit_assign
from tests import test_kit_assign as existing

NOT_REGULAR = "kit/assignments.json unreadable: not a regular file"
TOO_LARGE = "kit/assignments.json unreadable: larger than 1 MiB"
CHANGED = "kit/assignments.json unreadable: file changed or could not be read"
CANARY = "Outside Canary"
REAL_OPEN, REAL_FSTAT, REAL_FDOPEN = os.open, os.fstat, os.fdopen


class ReadSafetyTests(unittest.TestCase):
    setUp = existing.KitAssignTests.setUp
    ok = existing.KitAssignTests.ok
    put_sent = existing.KitAssignTests.put_sent
    write_store = existing.KitAssignTests.write_store

    def prepare(self):
        self.put_sent({self.a: existing.imported("2026-03-02")})
        self.write_store({self.a: "Local Owner"})
        self.outside = self.tmp / "outside.json"
        self.outside.write_text(json.dumps({"schema_version": 1, "assignments": {self.a: CANARY}}), encoding="utf-8")
        self.outside_bytes = self.outside.read_bytes()
        self.local_bytes = self.store.read_bytes()

    def to_symlink(self, target=None):
        def swap():
            os.rename(self.store, self.tmp / "backup.json")
            self.store.symlink_to(target or self.outside)
        return swap

    def to_fifo(self):
        self.store.unlink()
        os.mkfifo(self.store)

    def to_directory(self):
        self.store.unlink()
        self.store.mkdir()

    @contextlib.contextmanager
    def seam(self, before_open=None, before_fdopen=None, fstat_error=None, fdopen_error=None, read_error=None):
        rec = {"flags": [], "fds": [], "errnos": [], "fdopens": 0}
        store = str(self.store)

        def opened(path, flags, *args, **kwargs):
            if not (isinstance(path, (str, os.PathLike)) and os.fspath(path) == store):
                return REAL_OPEN(path, flags, *args, **kwargs)
            if before_open is not None and not rec["flags"]:
                before_open()
            rec["flags"].append(flags)
            try:
                fd = REAL_OPEN(path, flags, *args, **kwargs)
            except OSError as exc:
                rec["errnos"].append(exc.errno)
                raise
            rec["fds"].append(fd)
            return fd

        def statted(fd, *args, **kwargs):
            if fstat_error is not None and fd in rec["fds"]:
                raise fstat_error
            return REAL_FSTAT(fd, *args, **kwargs)

        def fdopened(fd, *args, **kwargs):
            if fd not in rec["fds"]:
                return REAL_FDOPEN(fd, *args, **kwargs)
            rec["fdopens"] += 1
            if before_fdopen is not None:
                before_fdopen()
            if fdopen_error is not None:
                raise fdopen_error
            handle = REAL_FDOPEN(fd, *args, **kwargs)
            if read_error is None:
                return handle

            class Failing:
                def __enter__(inner):
                    return inner

                def __exit__(inner, *exc):
                    return handle.__exit__(*exc)

                def read(inner, size=-1):
                    raise read_error
            return Failing()

        with mock.patch.object(os, "open", opened), mock.patch.object(os, "fstat", statted), \
                mock.patch.object(os, "fdopen", fdopened), \
                mock.patch.object(kit_assign.json, "loads", wraps=json.loads) as parsed:
            rec["parsed"] = parsed
            yield rec

    def assert_closed(self, rec):
        for fd in rec["fds"]:
            with self.assertRaises(OSError, msg="descriptor leaked"):
                REAL_FSTAT(fd)

    def refuse(self, swap, message, **kw):
        with self.seam(before_open=swap, **kw) as rec:
            with self.assertRaises(ValueError) as caught:
                kit_assign.read_assignments(self.root)
        self.assertEqual(str(caught.exception), message)
        rec["parsed"].assert_not_called()
        self.assertEqual(len(rec["flags"]), 1)
        self.assertTrue(rec["flags"][0] & os.O_NOFOLLOW and rec["flags"][0] & os.O_NONBLOCK)
        self.assert_closed(rec)
        return rec

    def test_leaf_symlink_swap_is_refused_without_target_read(self):
        self.prepare()
        for name in ("existing", "dangling"):
            with self.subTest(outside=name):
                target = self.outside if name == "existing" else self.tmp / "missing.json"
                rec = self.refuse(self.to_symlink(target), NOT_REGULAR)
                self.assertEqual(rec["errnos"], [errno.ELOOP])
                self.assertEqual(rec["fds"], [])  # no descriptor on the outside file ever existed
                self.assertEqual(self.outside.read_bytes(), self.outside_bytes)
                self.store.unlink()
                os.replace(self.tmp / "backup.json", self.store)

    def test_replacement_fifo_and_directory_refuse_without_blocking(self):
        self.prepare()

        def expired(signum, frame):
            raise TimeoutError("open blocked: O_NONBLOCK missing")
        previous = signal.signal(signal.SIGALRM, expired)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        for swap, remove in ((self.to_fifo, self.store.unlink), (self.to_directory, self.store.rmdir)):
            with self.subTest(swap=swap.__name__):
                signal.alarm(10)
                try:
                    rec = self.refuse(swap, NOT_REGULAR)
                finally:
                    signal.alarm(0)
                self.assertEqual(rec["fdopens"], 0)
                remove()
                self.store.write_bytes(self.local_bytes)

    def test_oversize_replacement_is_refused_on_the_descriptor(self):
        self.prepare()

        def to_large():
            self.store.write_bytes(self.local_bytes + b" " * kit_assign.MAX_BYTES)
        rec = self.refuse(to_large, TOO_LARGE)
        self.assertEqual(rec["fdopens"], 0)

    def test_open_descriptor_stays_bound_after_leaf_replacement(self):
        self.prepare()
        with self.seam(before_fdopen=self.to_symlink()) as rec:
            owners = kit_assign.read_assignments(self.root)
        self.assertEqual(owners, {self.a: "Local Owner"})  # the admitted inode, never the outside file
        self.assertTrue(self.store.is_symlink())
        self.assert_closed(rec)

    def test_descriptor_is_closed_on_every_failure(self):
        self.prepare()
        sentinel = REAL_OPEN(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, sentinel)
        cases = (
            ({"fstat_error": OSError(errno.EIO, f"fstat {CANARY}")}, CHANGED),
            ({"fdopen_error": OSError(errno.EMFILE, f"fdopen {CANARY}")}, CHANGED),
            ({"read_error": OSError(errno.EIO, f"read {CANARY}")}, CHANGED),
        )
        for kw, message in cases:
            with self.subTest(kw=list(kw)):
                with self.seam(**kw) as rec:
                    with self.assertRaises(ValueError) as caught:
                        kit_assign.read_assignments(self.root)
                self.assertEqual(str(caught.exception), message)
                self.assertNotIn(CANARY, str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
                self.assert_closed(rec)
        with self.seam(fdopen_error=MemoryError()) as rec:  # programmer/allocation errors propagate, after cleanup
            with self.assertRaises(MemoryError):
                kit_assign.read_assignments(self.root)
        self.assert_closed(rec)
        REAL_FSTAT(sentinel)  # an unrelated descriptor is untouched

    def test_vanished_after_admission_is_not_an_empty_store(self):
        self.prepare()
        with self.seam(before_open=self.store.unlink):
            with self.assertRaisesRegex(ValueError, "^" + CHANGED + "$"):
                kit_assign.read_assignments(self.root)

    def test_set_clear_and_noop_swap_refusal_precedes_any_write(self):
        self.prepare()
        for action in (("--to", "Replacement"), ("--clear",), ("--to", "Local Owner"), ("--to", CANARY)):
            with self.subTest(action=action):
                with self.seam(before_open=self.to_symlink()), \
                        mock.patch("os.replace", side_effect=AssertionError("replacement attempted")) as _, \
                        mock.patch("tempfile.mkstemp", side_effect=AssertionError("temporary file attempted")):
                    code, out, err = existing.run("kit-assign", "--directory", self.root, "--agency", self.a, *action)
                self.assertEqual((code, out), (1, ""), err)
                self.assertIn(NOT_REGULAR, err)
                self.assertNotIn("Recorded", err)
                self.assertNotIn(CANARY, err)
                self.assertEqual(self.outside.read_bytes(), self.outside_bytes)
                self.store.unlink()
                os.replace(self.tmp / "backup.json", self.store)

    def test_views_and_csv_do_not_disclose_substituted_owner(self):
        self.prepare()
        csv_path = self.tmp / "report.csv"
        for arguments in (("kit-status",), ("kit-status", "--owner", CANARY), ("kit-due", *existing.ASOF),
                          ("kit-coverage",), ("kit-coverage", "--csv"), ("kit-status", "--csv", csv_path)):
            with self.subTest(arguments=arguments):
                with self.seam(before_open=self.to_symlink()):
                    code, out, err = existing.run(arguments[0], "--directory", self.root, *arguments[1:])
                self.assertEqual((code, out), (1, ""), err)
                self.assertIn(NOT_REGULAR, err)
                self.assertNotIn(CANARY, err)
                self.assertFalse(csv_path.exists())
                self.store.unlink()
                os.replace(self.tmp / "backup.json", self.store)

    def test_today_swap_yields_one_source_error_and_recovers(self):
        self.prepare()
        for extra in ((), ("--owner", "Local Owner")):
            with self.subTest(extra=extra):
                with self.seam(before_open=self.to_symlink()):
                    code, out, err = existing.run("today", "--directory", self.root, *existing.ASOF, "--json", *extra)
                self.assertEqual((code, err), (0, ""))
                self.assertNotIn(CANARY, out)
                items = json.loads(out)
                self.assertFalse(any(i["kind"].startswith("request_") for i in items))
                bad = [i for i in items if i["kind"] == "unreadable"]
                self.assertEqual(len(bad), 1)
                self.assertIn(NOT_REGULAR, bad[0]["what"])
                self.store.unlink()
                os.replace(self.tmp / "backup.json", self.store)
        self.assertEqual(kit_assign.read_assignments(self.root), {self.a: "Local Owner"})  # healthy recovery

    def test_missing_store_is_empty_and_ordinary_read_succeeds(self):
        self.prepare()
        with self.seam() as rec:
            self.assertEqual(kit_assign.read_assignments(self.root), {self.a: "Local Owner"})
        self.assertEqual(rec["fdopens"], 1)
        self.assert_closed(rec)
        self.store.unlink()
        self.assertEqual(kit_assign.read_assignments(self.root), {})


if __name__ == "__main__":
    unittest.main()
