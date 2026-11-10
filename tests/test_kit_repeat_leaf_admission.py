"""kit/repeat-drafts.json is read through a no-follow, nonblocking descriptor admitted by type and size (RF0825-2).

Offline and synthetic: real files under a temporary directory; a leaf replacement is injected immediately before (or
after) the reader's real os.open, so the production open, fstat and read are real.
"""
import contextlib
import errno
import json
import os
import signal
import stat
import unittest
from unittest import mock

from campaign_tool import kit_repeat, kit_track
from tests import test_kit_repeat as base  # alias only: the old TestCase is not imported by name or rerun here

NOT_REGULAR = "kit/repeat-drafts.json unreadable: not a regular file"
TOO_LARGE = "kit/repeat-drafts.json unreadable: larger than 1 MiB"
REAL_OPEN, REAL_FSTAT, REAL_FDOPEN = os.open, os.fstat, os.fdopen


class LeafAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fx = base.KitRepeatTests("test_delayed_send_uses_coverage_not_send_date")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.root, self.store, self.tmp = self.fx.root, self.fx.store, self.fx.tmp
        self.fx.sent_main()
        self.empty = self.fx.pretty({})
        self.store.write_bytes(self.empty)
        self.outside = self.tmp / "outside.json"

    # --- helpers -----------------------------------------------------------------------------------------------
    def canary_document(self):
        """Real schema-valid metadata for a request that the regular store does not contain."""
        prior = self.store.read_bytes()
        self.fx.repeat("canary")
        document = self.store.read_bytes()
        self.store.write_bytes(prior)
        self.fx.draft("canary").unlink()
        return document

    def to_symlink(self, target):
        def swap():
            self.store.unlink()
            self.store.symlink_to(target)
        return swap

    @contextlib.contextmanager
    def seam(self, before_open=None, before_fdopen=None, fstat_error=None, fdopen_error=None, read_error=None):
        rec = {"flags": [], "fds": [], "errnos": [], "fdopens": 0, "reads": []}
        store = str(self.store)

        def is_store(path):
            return isinstance(path, (str, os.PathLike)) and os.fspath(path) == store

        def checked_open(path, flags, *args, **kwargs):
            if not is_store(path):
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

        def checked_fstat(fd):
            if fstat_error is not None and fd in rec["fds"]:
                raise fstat_error
            return REAL_FSTAT(fd)

        def checked_fdopen(fd, *args, **kwargs):
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

        with mock.patch.object(os, "open", checked_open), mock.patch.object(os, "fstat", checked_fstat), \
                mock.patch.object(os, "fdopen", checked_fdopen), \
                mock.patch.object(kit_repeat.json, "loads", wraps=json.loads) as parsed:
            rec["parsed"] = parsed
            yield rec

    def assert_descriptors_closed(self, rec):
        for fd in rec["fds"]:
            with self.assertRaises(OSError, msg="descriptor leaked"):
                REAL_FSTAT(fd)

    def assert_flags(self, rec):
        self.assertEqual(len(rec["flags"]), 1)
        for flag in (os.O_NOFOLLOW, os.O_NONBLOCK):
            self.assertTrue(rec["flags"][0] & flag)
        self.assertEqual(rec["flags"][0] & os.O_ACCMODE, os.O_RDONLY)

    def refuse(self, swap, message, **kw):
        with self.seam(before_open=swap, **kw) as rec:
            with self.assertRaises(ValueError) as caught:
                kit_repeat.read_repeats(self.root)
        self.assertEqual(str(caught.exception), message)
        self.assertTrue(caught.exception.__suppress_context__ or caught.exception.__context__ is None)
        self.assertEqual(rec["fdopens"], 0)  # no payload read was started
        rec["parsed"].assert_not_called()
        self.assert_flags(rec)
        self.assert_descriptors_closed(rec)
        return rec

    # --- tests -------------------------------------------------------------------------------------------------
    def test_leaf_symlink_swap_never_opens_outside_metadata(self):
        valid = self.canary_document()
        for name, content in (("canary", valid), ("empty", self.empty), ("dangling", None)):
            with self.subTest(outside=name):
                if content is not None:
                    self.outside.write_bytes(content)
                elif self.outside.exists():
                    self.outside.unlink()
                before = self.outside.read_bytes() if content is not None else None
                rec = self.refuse(self.to_symlink(self.outside), NOT_REGULAR)
                self.assertEqual(rec["errnos"], [errno.ELOOP])  # the symlink was refused by open itself
                self.assertEqual(rec["fds"], [])  # so no descriptor, and no read, of the outside file ever existed
                if content is not None:
                    self.assertEqual(self.outside.read_bytes(), before)
                self.store.unlink()
                self.store.write_bytes(self.empty)

    def test_leaf_fifo_and_directory_replacement_refuse_before_payload(self):
        def to_fifo():
            self.store.unlink()
            os.mkfifo(self.store)

        def to_directory():
            self.store.unlink()
            self.store.mkdir()

        def expired(signum, frame):
            raise TimeoutError("open blocked: O_NONBLOCK missing")

        previous = signal.signal(signal.SIGALRM, expired)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        for name, swap, remove in (("fifo", to_fifo, self.store.unlink), ("directory", to_directory, self.store.rmdir)):
            with self.subTest(kind=name):
                signal.alarm(10)
                try:
                    rec = self.refuse(swap, NOT_REGULAR)
                finally:
                    signal.alarm(0)
                self.assertEqual(len(rec["fds"]), 1)  # opened, then rejected by fstat on that descriptor
                remove()
                self.store.write_bytes(self.empty)

    def test_descriptor_size_controls_replacement_and_growth(self):
        def to_large():
            self.store.write_bytes(self.empty + b" " * self.fx.LIMIT)  # same inode, grown after the preflight

        def to_large_regular_file():
            replacement = self.tmp / "large.json"
            replacement.write_bytes(self.empty + b" " * self.fx.LIMIT)
            os.replace(replacement, self.store)

        for swap in (to_large, to_large_regular_file):
            with self.subTest(swap=swap.__name__):
                self.refuse(swap, TOO_LARGE)
                self.store.write_bytes(self.empty)

        def to_exact_limit():
            replacement = self.tmp / "exact.json"
            replacement.write_bytes(self.empty + b" " * (self.fx.LIMIT - len(self.empty)))
            os.replace(replacement, self.store)

        with self.seam(before_open=to_exact_limit) as rec:
            self.assertEqual(kit_repeat.read_repeats(self.root), {})
        self.assertEqual(rec["fdopens"], 1)
        self.assertEqual(self.store.stat().st_size, kit_track.MAX_BYTES)

    def test_growth_after_fstat_keeps_cap_plus_one_guard(self):
        grown = self.empty + b" " * self.fx.LIMIT

        def grow():
            self.store.write_bytes(grown)
        with self.seam(before_fdopen=grow) as rec:  # the same descriptor grows after its size admission
            with self.assertRaisesRegex(ValueError, "unreadable: larger than 1 MiB$"):
                kit_repeat.read_repeats(self.root)
        rec["parsed"].assert_not_called()
        self.assert_descriptors_closed(rec)

    def test_replacement_after_open_cannot_redirect_the_read(self):
        self.outside.write_bytes(self.canary_document())
        before = self.outside.read_bytes()
        with self.seam(before_fdopen=self.to_symlink(self.outside)) as rec:
            self.assertEqual(kit_repeat.read_repeats(self.root), {})  # the originally opened inode, not the canary
        self.assertEqual(rec["fdopens"], 1)
        self.assertTrue(self.store.is_symlink())  # the later pathname change is not reinterpreted or repaired
        self.assertEqual(self.outside.read_bytes(), before)
        self.assert_descriptors_closed(rec)

    def test_owned_descriptor_closed_on_all_admission_exits(self):
        def to_directory():
            self.store.unlink()
            self.store.mkdir()

        def restore():
            if self.store.is_dir() and not self.store.is_symlink():
                self.store.rmdir()
            elif self.store.exists() or self.store.is_symlink():
                self.store.unlink()
            self.store.write_bytes(self.empty)

        scenarios = (
            ("fstat error", {"fstat_error": OSError(errno.EIO, "synthetic fstat failure")}, OSError, None),
            ("nonregular", {"before_open": to_directory}, ValueError, NOT_REGULAR),
            ("oversized", {"before_open": lambda: self.store.write_bytes(self.empty + b" " * self.fx.LIMIT)},
             ValueError, TOO_LARGE),
            ("fdopen failure", {"fdopen_error": OSError(errno.EMFILE, "synthetic fdopen failure")}, OSError, None),
            ("read error", {"read_error": OSError(errno.EIO, "synthetic read failure")}, OSError, None),
            ("parse refusal", {"before_open": lambda: self.store.write_bytes(b"{")}, ValueError, "not JSON"),
            ("success", {}, None, None),
        )
        for name, kw, error, text in scenarios:
            with self.subTest(exit=name):
                restore()
                with self.seam(**kw) as rec:
                    if error is None:
                        self.assertEqual(kit_repeat.read_repeats(self.root), {})
                    else:
                        with self.assertRaises(error) as caught:
                            kit_repeat.read_repeats(self.root)
                        if text:
                            self.assertIn(text, str(caught.exception))
                        elif "error" in name or "failure" in name:
                            self.assertIs(caught.exception, kw.get("fstat_error") or kw.get("fdopen_error")
                                          or kw.get("read_error"))
                self.assertEqual(len(rec["fds"]), 1)
                self.assert_descriptors_closed(rec)
        restore()

    def cli_refused(self, call):
        """Run a real consumer with the leaf swapped to a symlink just before the read; refuse without any write."""
        outside_document = self.canary_document()
        self.outside.write_bytes(outside_document)
        after = {}

        def swap():
            self.to_symlink(self.outside)()
            after["tree"] = self.fx.snapshot()  # after the deliberate injection
            after["mtimes"] = {p: p.lstat().st_mtime_ns for p in self.tmp.rglob("*")}

        with self.seam(before_open=swap) as rec, \
                mock.patch("socket.socket", side_effect=AssertionError("network used")):
            code, out, err = call()
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "Stopped: " + NOT_REGULAR + "\n")
        self.assertNotIn(self.outside.name, err)
        self.assertEqual(rec["errnos"][:1], [errno.ELOOP])
        self.assertEqual(self.fx.snapshot(), after["tree"])
        self.assertEqual({p: p.lstat().st_mtime_ns for p in self.tmp.rglob("*")}, after["mtimes"])
        self.assertEqual(self.outside.read_bytes(), outside_document)
        self.assertEqual(list(self.fx.kit.glob(".repeat-drafts.*")), [])

    def test_leaf_swap_refuses_real_repeat_and_named_sent_without_writes(self):
        self.cli_refused(lambda: base.run("kit-repeat", "--directory", self.root, "--agency", self.fx.agency,
                                          "--request", "main", "--as", "swapped", "--through", "2026-11-01"))
        self.assertFalse(self.fx.draft("swapped").exists())
        self.store.unlink()
        self.store.write_bytes(self.empty)
        self.fx.repeat("bound")
        self.cli_refused(lambda: self.fx.send("bound", "2026-11-01"))
        self.assertNotIn(f"{self.fx.agency}#bound", kit_track.read_sent(self.root))

    def test_leaf_swap_refuses_table_and_csv_coverage(self):
        self.fx.repeat("bound")
        original = self.store.read_bytes()
        for flags in ((), ("--csv",)):
            with self.subTest(flags=flags):
                if self.store.is_symlink():
                    self.store.unlink()
                self.store.write_bytes(original)
                self.cli_refused(lambda: base.run("kit-coverage", "--directory", self.root, *flags))

    def test_static_and_ordinary_metadata_contracts_remain_compatible(self):
        self.store.unlink()
        self.assertEqual(kit_repeat.read_repeats(self.root), {})  # absent
        for target in (self.tmp / "missing", self.fx.draft()):  # static dangling / outside symlinks
            self.store.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                kit_repeat.read_repeats(self.root)
            self.store.unlink()
        self.store.mkdir()  # static directory
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            kit_repeat.read_repeats(self.root)
        self.store.rmdir()
        self.store.write_bytes(self.empty)
        rec = None
        with self.assertRaises(FileNotFoundError):  # disappearing after admission is not a fresh absent store
            with self.seam(before_open=self.store.unlink) as rec:
                kit_repeat.read_repeats(self.root)
        self.assertEqual(rec["fds"] if rec else None, [])
        # an ordinary populated store, and an atomic regular-file replacement that passes on its own bytes
        self.fx.repeat("bound")
        populated = self.store.read_bytes()
        requests = kit_repeat.read_repeats(self.root)
        self.assertEqual(list(requests), [f"{self.fx.agency}#bound"])
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)
        self.assertEqual(self.store.read_bytes(), populated)

        def replace_with_regular_empty():
            replacement = self.tmp / "replacement.json"
            replacement.write_bytes(self.empty)
            os.replace(replacement, self.store)
        with self.seam(before_open=replace_with_regular_empty):
            self.assertEqual(kit_repeat.read_repeats(self.root), {})


if __name__ == "__main__":
    unittest.main()
