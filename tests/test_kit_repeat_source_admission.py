"""kit-repeat reads its original draft through one no-follow, nonblocking descriptor admitted as a regular file (RF1420-2).

Offline and synthetic: real files, symlinks, FIFOs and directories under a temporary directory. A leaf replacement is
injected at the source acquisition itself (immediately before the real os.open, or before a pathname read on code
that has no descriptor seam), so the production open, fstat, fdopen and read calls are the real ones.
"""
import contextlib
import errno
import hashlib
import json
import os
import signal
import stat
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import kit_repeat, kit_track
from tests import test_kit_repeat as base  # alias only: the old TestCase is not imported by name or rerun here

REAL_OPEN, REAL_FSTAT, REAL_FDOPEN, REAL_CLOSE = os.open, os.fstat, os.fdopen, os.close
REAL_READ_BYTES = Path.read_bytes
TODAY = date(2026, 11, 1)


class _Stream:
    """Forwards to the real stream returned by fdopen and records how many bytes each real read returned."""

    def __init__(self, handle, rec, read_error):
        self.handle, self.rec, self.read_error = handle, rec, read_error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self.handle.__exit__(*exc)

    def read(self, *args):
        if self.read_error is not None:
            raise self.read_error
        data = self.handle.read(*args)
        self.rec["reads"].append(len(data))
        return data


class SourceAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fx = base.KitRepeatTests("test_delayed_send_uses_coverage_not_send_date")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.root, self.tmp, self.kit = self.fx.root, self.fx.tmp, self.fx.kit
        self.fx.sent_main()
        self.source = self.fx.draft()
        self.original = self.source.read_bytes()
        self.outside = self.tmp / "outside.md"

    # --- helpers -----------------------------------------------------------------------------------------------
    def message(self, source=None):
        name = (source or self.source).name
        return f"the original draft kit/requests/{name} must be a regular file (not a symlink)"

    def restore(self, source=None, data=None):
        source = source or self.source
        if source.is_dir() and not source.is_symlink():
            source.rmdir()
        elif os.path.lexists(source):
            source.unlink()
        source.write_bytes(self.original if data is None else data)

    def bind_sent(self, data):
        """Record ``data`` as main's sent draft hash directly (for sources kit-sent itself would not accept)."""
        document = json.loads(self.fx.sent.read_text())
        document["requests"][kit_track._key(self.fx.agency, "main")]["draft_sha256"] = hashlib.sha256(data).hexdigest()
        self.fx.sent.write_text(json.dumps(document))

    def to_symlink(self, target, source=None):
        def swap():
            (source or self.source).unlink()
            (source or self.source).symlink_to(target)
        return swap

    def to_fifo(self):
        self.source.unlink()
        os.mkfifo(self.source)

    def to_directory(self):
        self.source.unlink()
        self.source.mkdir()

    @contextlib.contextmanager
    def seam(self, before_open=None, source=None, device=False, before_fdopen=None, fstat_error=None,
             fdopen_error=None, fdopen_closes=False, read_error=None):
        """Instrument the acquisition of ``source``; ``before_open`` runs once, at the first acquisition attempt."""
        rec = {"flags": [], "fds": [], "errnos": [], "fdopens": 0, "reads": [], "path_reads": [], "closes": [],
               "fstats": 0, "swapped": False}
        target, live = os.fspath(source or self.source), set()

        def fire():
            if before_open is not None and not rec["swapped"]:
                rec["swapped"] = True
                before_open()

        def checked_open(path, flags, *args, **kwargs):
            if not (isinstance(path, (str, os.PathLike)) and os.fspath(path) == target):
                return REAL_OPEN(path, flags, *args, **kwargs)
            fire()
            rec["flags"].append(flags)
            try:
                fd = REAL_OPEN(os.devnull if device else path, flags, *args, **kwargs)
            except OSError as exc:
                rec["errnos"].append(exc.errno)
                raise
            rec["fds"].append(fd)
            live.add(fd)  # the raw source descriptor, until a stream or a close takes it (numbers are reused later)
            return fd

        def checked_fstat(fd, *args, **kwargs):
            if isinstance(fd, int) and fd in live:
                rec["fstats"] += 1
                if fstat_error is not None:
                    raise fstat_error
            return REAL_FSTAT(fd, *args, **kwargs)

        def checked_fdopen(fd, *args, **kwargs):
            if fd not in live:
                return REAL_FDOPEN(fd, *args, **kwargs)
            rec["fdopens"] += 1
            if before_fdopen is not None:
                before_fdopen()
            if fdopen_error is not None:
                if fdopen_closes:  # as CPython's io.open does when FileIO already accepted the descriptor
                    REAL_CLOSE(fd)
                raise fdopen_error
            live.discard(fd)
            return _Stream(REAL_FDOPEN(fd, *args, **kwargs), rec, read_error)

        def checked_close(fd):
            if fd in live:
                live.discard(fd)
                rec["closes"].append(fd)
            return REAL_CLOSE(fd)

        def checked_read_bytes(path):
            if os.fspath(path) == target:
                fire()
                data = REAL_READ_BYTES(path)
                rec["path_reads"].append(len(data))
                return data
            return REAL_READ_BYTES(path)

        with mock.patch.object(os, "open", checked_open), mock.patch.object(os, "fstat", checked_fstat), \
                mock.patch.object(os, "fdopen", checked_fdopen), mock.patch.object(os, "close", checked_close), \
                mock.patch.object(Path, "read_bytes", checked_read_bytes), \
                mock.patch.object(kit_repeat.hashlib, "sha256", wraps=hashlib.sha256) as hashed, \
                mock.patch("socket.socket", side_effect=AssertionError("network used")):
            rec["hashed"] = hashed
            yield rec

    def assert_closed(self, rec):
        for fd in rec["fds"]:
            with self.assertRaises(OSError, msg="descriptor leaked"):
                REAL_FSTAT(fd)

    def assert_flags(self, rec):
        self.assertEqual(len(rec["flags"]), 1, "exactly one source acquisition")
        for flag in (os.O_NOFOLLOW, os.O_NONBLOCK):
            self.assertTrue(rec["flags"][0] & flag)
        self.assertEqual(rec["flags"][0] & os.O_ACCMODE, os.O_RDONLY)

    def no_outputs(self, new="next"):
        self.assertFalse(os.path.lexists(self.fx.draft(new)))
        self.assertEqual(list(self.kit.glob(".repeat-drafts.*")), [])

    def refuse(self, swap, new="next", request="main", source=None, **kw):
        """repeat_request refuses the injected substitution with the regular-file refusal before any payload read."""
        store_before = self.fx.store.read_bytes() if self.fx.store.exists() else None
        with self.seam(before_open=swap, source=source, **kw) as rec:
            with self.assertRaises(ValueError) as caught:
                self.fx.repeat(new, request)
        self.assertTrue(rec["swapped"], "the substitution was injected")
        self.assertEqual(rec["path_reads"], [], "the source was read through its pathname")
        self.assertEqual(str(caught.exception), self.message(source))
        self.assertTrue(caught.exception.__suppress_context__ or caught.exception.__context__ is None)
        self.assertEqual((rec["fdopens"], rec["reads"]), (0, []))  # no payload read was started
        rec["hashed"].assert_not_called()
        self.assert_flags(rec)
        self.assert_closed(rec)
        self.no_outputs(new)
        self.assertEqual(self.fx.store.read_bytes() if self.fx.store.exists() else None, store_before)
        return rec

    def expected_follow_on(self, source_bytes, span="2026-10-01 through 2026-11-01",
                           old="2026-01-01 through 2026-09-30"):
        """The follow-on bytes derived independently of kit_repeat: range sentence and Subject range replaced."""
        lines = source_bytes.decode("utf-8").split("\n")
        hits = [i for i, line in enumerate(lines) if line.startswith(base.LINE)]
        self.assertEqual(len(hits), 1)
        lines[hits[0]] = f"{base.LINE}{span}."
        subjects = [i for i, line in enumerate(lines) if line.startswith("Subject: ")]
        self.assertEqual(len(subjects), 1)
        self.assertTrue(lines[subjects[0]].endswith(", " + old))
        lines[subjects[0]] = lines[subjects[0]][:-len(old)] + span
        return "\n".join(lines).encode("utf-8")

    # --- tests -------------------------------------------------------------------------------------------------
    def test_source_symlink_swap_refuses_before_outside_payload(self):
        for name, content in (("identical", self.original), ("canary", b"outside canary bytes\n"), ("dangling", None)):
            with self.subTest(outside=name):
                if content is not None:
                    self.outside.write_bytes(content)
                elif self.outside.exists():
                    self.outside.unlink()
                rec = self.refuse(self.to_symlink(self.outside))
                self.assertEqual(rec["errnos"], [errno.ELOOP])  # refused by the open itself
                self.assertEqual(rec["fds"], [])  # so no descriptor, and no read, of the outside target existed
                if content is not None:
                    self.assertEqual(self.outside.read_bytes(), content)
                self.restore()
        self.assertEqual(self.fx.repeat("next")["follows"], "main")  # same name, healthy retry

    def test_source_fifo_directory_and_device_swaps_refuse_nonblocking(self):
        def expired(signum, frame):
            raise TimeoutError("source open or read blocked: O_NONBLOCK or fstat admission missing")

        previous = signal.signal(signal.SIGALRM, expired)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        for name, swap, kw in (("fifo", self.to_fifo, {}), ("directory", self.to_directory, {}),
                               ("device", lambda: None, {"device": True})):
            with self.subTest(kind=name):
                signal.alarm(10)
                try:
                    rec = self.refuse(swap, **kw)
                finally:
                    signal.alarm(0)
                self.assertEqual(len(rec["fds"]), 1)  # opened without blocking, then rejected on that descriptor
                self.assertEqual(rec["fstats"], 1)
                self.restore()
        self.assertEqual(self.fx.repeat("next")["follows"], "main")

    def test_source_replacement_after_open_stays_on_admitted_inode(self):
        self.outside.write_bytes(b"outside canary bytes\n")
        with self.seam(before_fdopen=self.to_symlink(self.outside)) as rec:
            out = self.fx.repeat("next")
        self.assertEqual(rec["fdopens"], 1)
        self.assertEqual(rec["reads"], [len(self.original)])  # the admitted inode, read once through its stream
        self.assertEqual(rec["path_reads"], [])
        self.assertTrue(self.source.is_symlink())  # the later pathname change is not reinterpreted or repaired
        self.assertEqual(self.outside.read_bytes(), b"outside canary bytes\n")
        body = self.expected_follow_on(self.original)
        self.assertEqual(self.fx.draft("next").read_bytes(), body)
        self.assertEqual(out["draft_sha256"], hashlib.sha256(body).hexdigest())
        self.assert_closed(rec)
        self.restore()

        def replace_regular(data):
            def swap():
                replacement = self.tmp / "replacement.md"
                replacement.write_bytes(data)
                os.replace(replacement, self.source)
            return swap

        with self.seam(before_open=replace_regular(self.original)) as rec:  # an ordinary regular replacement
            self.assertEqual(self.fx.repeat("again")["follows"], "main")  # passes on its own matching bytes
        self.assertEqual(rec["reads"], [len(self.original)])
        with self.seam(before_open=replace_regular(self.original + b"edited\n")) as rec:
            with self.assertRaisesRegex(ValueError, r"changed since it was recorded as sent; restore it to repeat it$"):
                self.fx.repeat("third")
        self.assert_closed(rec)
        self.no_outputs("third")

    def test_source_descriptor_closes_on_every_exit(self):
        sentinel = REAL_OPEN(os.devnull, os.O_RDONLY)
        self.addCleanup(REAL_CLOSE, sentinel)
        fstat_error = OSError(errno.EIO, "synthetic fstat failure")
        fdopen_error = OSError(errno.EMFILE, "synthetic fdopen failure")
        closed_error = OSError(errno.EINVAL, "synthetic fdopen failure after closing")
        interrupt = KeyboardInterrupt("synthetic interrupt after closing")
        read_error = OSError(errno.EIO, "synthetic read failure")
        edited = self.original + b"edited\n"
        non_utf8 = b"\xff\xfe not UTF-8\n"
        scenarios = (
            ("fstat error", {"fstat_error": fstat_error}, fstat_error, None),
            ("nonregular", {"before_open": self.to_directory}, ValueError, self.message()),
            ("fdopen before ownership", {"fdopen_error": fdopen_error}, fdopen_error, None),
            ("fdopen after closing", {"fdopen_error": closed_error, "fdopen_closes": True}, closed_error, None),
            ("interrupt after closing", {"fdopen_error": interrupt, "fdopen_closes": True}, interrupt, None),
            ("read error", {"read_error": read_error}, read_error, None),
            ("hash mismatch", {"before_open": lambda: self.source.write_bytes(edited)}, ValueError,
             f"kit/requests/{self.source.name} changed since it was recorded as sent; restore it to repeat it"),
        )
        for name, kw, error, text in scenarios * 2:  # repeated failures must not accumulate resources
            with self.subTest(exit=name):
                self.restore()
                with self.seam(**kw) as rec:
                    with self.assertRaises(BaseException) as caught:
                        self.fx.repeat("next")
                if isinstance(error, BaseException):
                    self.assertIs(caught.exception, error)  # the original exception escapes unchanged
                else:
                    self.assertIsInstance(caught.exception, error)
                    self.assertEqual(str(caught.exception), text)
                self.assertEqual(len(rec["fds"]), 1)
                self.assert_closed(rec)
                if rec["fdopens"] and "before ownership" not in name and "closing" not in name:
                    self.assertEqual(rec["closes"], [])  # the stream owned the descriptor; no raw double close
                if "closing" in name:
                    self.assertEqual(rec["closes"], rec["fds"])  # the one tolerated second close attempt
                REAL_FSTAT(sentinel)  # an unrelated descriptor is never closed
                self.no_outputs()
        # a hash-bound source that is not UTF-8 keeps the existing decode refusal after one admitted read
        self.restore(data=non_utf8)
        self.bind_sent(non_utf8)
        with self.seam() as rec:
            with self.assertRaises(ValueError) as caught:
                self.fx.repeat("next")
        self.assertEqual(str(caught.exception), f"kit/requests/{self.source.name} is not UTF-8")
        self.assertEqual(rec["reads"], [len(non_utf8)])
        self.assert_closed(rec)
        self.assertEqual(rec["closes"], [])
        self.no_outputs()
        self.bind_sent(self.original)
        self.restore()
        with self.seam() as rec:  # healthy same-name retry: one read, one descriptor, closed by its stream
            self.assertEqual(self.fx.repeat("next")["follows"], "main")
        self.assertEqual((rec["reads"], rec["closes"]), ([len(self.original)], []))
        self.assert_closed(rec)
        REAL_FSTAT(sentinel)

    def cli_refused(self, arguments, new, source, content):
        """Real kit-repeat with the source leaf swapped to an outside symlink at acquisition; nothing written."""
        self.outside.write_bytes(content)
        after = {}

        def swap():
            self.to_symlink(self.outside, source)()
            after["tree"] = self.fx.snapshot()  # after the deliberate injection, so hook changes are not counted
            after["mtimes"] = {p: p.lstat().st_mtime_ns for p in self.tmp.rglob("*")}

        with self.seam(before_open=swap, source=source) as rec, \
                mock.patch.object(kit_repeat, "_replace_store", side_effect=AssertionError("writer called")):
            code, out, err = base.run("kit-repeat", "--directory", self.root, "--agency", self.fx.agency,
                                      *arguments, "--as", new, "--through", "2026-11-01")
        self.assertTrue(rec["swapped"], err)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "Stopped: " + self.message(source) + "\n")
        self.assertNotIn("Traceback", err)
        self.assertNotIn(self.outside.name, err)
        self.assertNotIn("canary", err)
        self.assertEqual(rec["errnos"], [errno.ELOOP])
        self.assertEqual((rec["fds"], rec["path_reads"], rec["reads"]), ([], [], []))
        self.assertEqual(self.fx.snapshot(), after["tree"])
        self.assertEqual({p: p.lstat().st_mtime_ns for p in self.tmp.rglob("*")}, after["mtimes"])
        self.assertEqual(self.outside.read_bytes(), content)
        self.no_outputs(new)

    def test_real_repeat_cli_refuses_source_swap_without_mutation_and_recovers(self):
        self.assertFalse(self.fx.store.exists())  # absent metadata first
        for content in (self.original, b"outside canary bytes\n"):
            self.cli_refused(("--request", "main"), "next", self.source, content)
            self.restore()
        code, out, err = base.run("kit-repeat", "--directory", self.root, "--agency", self.fx.agency,
                                  "--request", "main", "--as", "next", "--through", "2026-11-01")
        self.assertEqual((code, err), (0, ""), err)
        self.assertTrue(self.fx.draft("next").is_file())
        self.assertEqual(self.fx.send("next", "2026-11-01")[0], 0)  # a named predecessor, existing metadata
        named = self.fx.draft("next")
        named_bytes = named.read_bytes()
        for content in (named_bytes, b"outside canary bytes\n"):
            self.cli_refused(("--request", "next", "--from", "2026-11-01"), "later", named, content)
            self.restore(named, named_bytes)
        code, out, err = base.run("kit-repeat", "--directory", self.root, "--agency", self.fx.agency,
                                  "--request", "next", "--from", "2026-11-01", "--as", "later", "--through", "2026-11-01")
        self.assertEqual((code, err), (0, ""), err)
        self.assertEqual(kit_repeat.read_repeats(self.root)[kit_track._key(self.fx.agency, "later")]["follows"], "next")

    def test_static_source_and_hash_utf8_contracts_unchanged(self):
        for name, make in (("live symlink", lambda: self.source.symlink_to(self.fx.draft(agency=self.fx.other))),
                           ("dangling symlink", lambda: self.source.symlink_to(self.tmp / "missing.md")),
                           ("missing", lambda: None), ("directory", self.source.mkdir)):
            with self.subTest(source=name):
                self.source.unlink()
                make()
                with self.seam() as rec:
                    self.fx.refused(lambda: self.fx.repeat("next"), self.message())
                self.assertEqual((rec["flags"], rec["path_reads"]), ([], []))  # the early refusal is kept
                self.restore()
        self.source.write_bytes(self.original.replace(b"Subject: ", b"Subject: edited "))
        self.fx.refused(lambda: self.fx.repeat("next"), "changed since it was recorded as sent; restore it to repeat it")
        self.restore()
        # existing refusal ordering: an unsent request and a used name are refused before the source is touched
        with self.seam() as rec:
            self.fx.refused(lambda: self.fx.repeat("next", "other"), "is not recorded as sent")
            self.fx.refused(lambda: self.fx.repeat("main"), "--as must be a new request name, not main")
        self.assertEqual(rec["flags"], [])

    def test_regular_source_outputs_and_chain_are_byte_compatible(self):
        text = self.original.decode("utf-8")
        context = text.index("Statutory context") if "Statutory context" in text else len(text)
        unicode_text = text[:context] + "Café records — naïve “quoted” scope ✓\n\n" + text[context:]
        large = unicode_text + "\n" + ("x" * 120 + "\n") * ((kit_track.MAX_BYTES // 121) + 64)
        for request, source_text in (("unicode", unicode_text), ("large", large)):
            with self.subTest(source=request):
                self.restore(data=source_text.encode("utf-8"))
                data = self.source.read_bytes()
                if request == "unicode":
                    code, _, err = self.fx.send("main", "2026-10-15", "--replace")
                    self.assertEqual(code, 0, err)
                else:  # kit-sent caps drafts at 1 MiB, so bind a larger source's hash directly
                    self.assertGreater(len(data), kit_track.MAX_BYTES)  # no new source byte limit in kit-repeat
                    self.bind_sent(data)
                with self.seam() as rec:
                    out = self.fx.repeat(request)
                self.assertEqual(rec["reads"], [len(data)])
                self.assert_flags(rec)
                self.assert_closed(rec)
                body = self.expected_follow_on(data)
                draft = self.fx.draft(request)
                self.assertEqual(draft.read_bytes(), body)
                self.assertEqual(stat.S_IMODE(draft.stat().st_mode), 0o600)
                entry = {"follows": "main", "date_from": "2026-10-01", "date_to": "2026-11-01",
                         "source_sha256": hashlib.sha256(data).hexdigest(),
                         "draft_sha256": hashlib.sha256(body).hexdigest()}
                self.assertEqual(out, {"agency_id": self.fx.agency, "request": request, "follows": "main",
                                       "draft": f"requests/{draft.name}", "date_from": "2026-10-01",
                                       "date_to": "2026-11-01", "draft_sha256": entry["draft_sha256"]})
                stored = json.loads(self.fx.store.read_bytes())["requests"]
                self.assertEqual(stored[kit_track._key(self.fx.agency, request)], entry)
                self.assertEqual(stat.S_IMODE(self.fx.store.stat().st_mode), 0o600)
                self.assertEqual(self.fx.store.read_bytes(), self.fx.pretty(stored))  # unchanged store format
                if request == "unicode":  # the named chain binds through kit-sent and the views
                    self.assertEqual(self.fx.send("unicode", "2026-11-01")[0], 0)
                    sent = kit_track.read_sent(self.root)[kit_track._key(self.fx.agency, "unicode")]
                    self.assertEqual(sent["follows"], "main")
                    self.assertEqual(base.run("kit-coverage", "--directory", self.root)[0], 0)
                    self.assertEqual(base.run("kit-status", "--directory", self.root)[0], 0)


if __name__ == "__main__":
    unittest.main()
