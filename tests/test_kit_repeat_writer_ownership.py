"""kit-repeat writers own each raw output descriptor until fdopen succeeds. Offline, synthetic files, real descriptors."""
import contextlib
import errno
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_repeat  # noqa: E402
from tests import test_kit_repeat as repeat_fixture  # noqa: E402  (composed, never subclassed: no duplicate discovery)

REAL = {name: getattr(os, name) for name in ("open", "fdopen", "fchmod", "unlink", "replace", "close", "fstat")}
REAL_MKSTEMP = tempfile.mkstemp
REAL_LEXISTS = os.path.lexists
PREFIX = ".repeat-drafts."


class Interrupt(BaseException):
    """A dedicated control-flow exception that must never be reclassified."""


def is_open(fd):
    try:
        REAL["fstat"](fd)
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return False
        raise
    return True


class FaultyStream:
    """A real binary stream whose write or close raises after the real stream has been closed."""

    def __init__(self, real, write=None, close=None):
        self.real, self.write_fault, self.close_fault = real, write, close

    def __enter__(self):
        return self

    def write(self, data):
        if self.write_fault:
            raise self.write_fault
        return self.real.write(data)

    def __exit__(self, *exc):
        self.real.close()
        if self.close_fault:
            raise self.close_fault
        return False


class Trace:
    """Phase-specific acquisition/transfer seams: forwards the real calls, injects only the named faults, and
    measures real descriptors. Any descriptor still open at exit is recorded as leaked, then closed here."""

    def __init__(self, target, **faults):
        self.target, self.faults = str(target), faults
        self.kind, self.fdopened, self.closes, self.at_unlink, self.replaced = {}, [], [], [], []
        self.leaked = []

    def fault(self, site):
        return self.faults.get(site)

    def __enter__(self):
        self.stack = contextlib.ExitStack()

        def opened(path, *args, **kwargs):
            if str(path) != self.target:
                return REAL["open"](path, *args, **kwargs)
            if self.fault("open"):
                raise self.fault("open")
            fd = REAL["open"](path, *args, **kwargs)
            self.kind[fd] = "draft"
            return fd

        def made(*args, **kwargs):
            if kwargs.get("prefix") != PREFIX:
                return REAL_MKSTEMP(*args, **kwargs)
            if self.fault("mkstemp"):
                raise self.fault("mkstemp")
            fd, name = REAL_MKSTEMP(*args, **kwargs)
            self.kind[fd] = "store"
            return fd, name

        def chmod(fd, mode):
            if self.kind.get(fd) == "store" and self.fault("fchmod"):
                raise self.fault("fchmod")
            return REAL["fchmod"](fd, mode)

        def fdopen(fd, *args, **kwargs):
            kind = self.kind.get(fd)
            if kind and self.fault(kind + "_fdopen"):
                raise self.fault(kind + "_fdopen")
            if kind:
                self.fdopened.append(kind)
            stream = REAL["fdopen"](fd, *args, **kwargs)
            wrap = self.fault(kind + "_stream") if kind else None
            return wrap(stream) if wrap else stream

        def close(fd):
            self.closes.append(fd)
            return REAL["close"](fd)

        def unlink(path, *args, **kwargs):
            self.at_unlink.append([fd for fd in self.kind if is_open(fd)])
            return REAL["unlink"](path, *args, **kwargs)

        def replace(source, destination):
            self.replaced.append(Path(destination).name)
            if self.fault("replace"):
                raise self.fault("replace")
            return REAL["replace"](source, destination)

        for name, fake in (("open", opened), ("fchmod", chmod), ("fdopen", fdopen), ("close", close),
                           ("unlink", unlink), ("replace", replace)):
            self.stack.enter_context(mock.patch.object(os, name, fake))
        self.stack.enter_context(mock.patch.object(tempfile, "mkstemp", made))
        return self

    def __exit__(self, *exc):
        self.stack.close()
        for fd in self.kind:
            if is_open(fd):
                self.leaked.append(self.kind[fd])
                REAL["close"](fd)  # measured above; never leave it open in the test host
        return False


class WriterOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.fx = repeat_fixture.KitRepeatTests("test_delayed_send_uses_coverage_not_send_date")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        offline = mock.patch("socket.socket", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        self.sentinel = REAL["open"](os.devnull, os.O_RDONLY)  # an unrelated descriptor that must survive
        self.addCleanup(lambda: is_open(self.sentinel) and REAL["close"](self.sentinel))
        self.fx.sent_main()

    # --- helpers ---------------------------------------------------------------------------------------------
    def tree(self):
        out = {}
        for path in sorted(self.fx.tmp.rglob("*")):
            info = path.lstat()
            if path.is_symlink() or path.is_dir():
                out[str(path)] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode))
            else:
                out[str(path)] = (stat.S_IMODE(info.st_mode), path.read_bytes(), info.st_mtime_ns)
        return out

    def new_target(self, name="next"):
        return self.fx.draft(name)

    def call(self, name="next", **kw):
        return self.fx.repeat(name, **kw)

    def faulted(self, name, expected, **faults):
        """One repeat attempt with injected faults: the same exception object escapes, nothing leaks or changes."""
        before = self.tree()
        with Trace(self.new_target(name), **faults) as trace:
            with self.assertRaises(type(expected)) as caught:
                self.call(name)
        self.assertIs(caught.exception, expected)
        self.assertEqual(trace.leaked, [], "an acquired output descriptor was left open")
        self.assertTrue(is_open(self.sentinel))
        self.assertEqual(self.tree(), before)
        return trace

    def assert_closed_before_unlink(self, trace):
        self.assertTrue(trace.at_unlink)
        self.assertEqual(trace.at_unlink, [[] for _ in trace.at_unlink], "a pathname was removed while its raw descriptor was open")

    def bound(self):
        self.call("bound")  # a valid earlier follow-on: the store exists and must be preserved

    # --- principal pre-transfer faults -----------------------------------------------------------------------
    def test_draft_fdopen_failure_closes_descriptor_before_target_rollback(self):
        for existing in (False, True):
            with self.subTest(existing_metadata=existing):
                if existing:
                    self.bound()
                error = OSError("synthetic-constructor-failure")
                trace = self.faulted("next", error, draft_fdopen=error)
                self.assert_closed_before_unlink(trace)
                self.assertEqual(len(trace.closes), 1)
                self.assertEqual(trace.fdopened, [])
                self.assertEqual(trace.replaced, [])
                self.assertFalse(os.path.lexists(self.new_target()))

    def test_metadata_fchmod_failure_closes_raw_temporary_before_unlink(self):
        self.bound()
        error = OSError("synthetic-mode-failure")
        trace = self.faulted("next", error, fchmod=error)
        self.assert_closed_before_unlink(trace)
        self.assertEqual(trace.fdopened, ["draft"])  # the metadata fdopen and replace never ran
        self.assertEqual(trace.replaced, [])
        self.assertEqual(len(trace.closes), 1)
        self.assertEqual(sorted(p.name for p in self.fx.kit.glob(PREFIX + "*")), [])

    def test_metadata_fdopen_failure_closes_raw_temporary_and_rolls_back_draft(self):
        self.bound()
        error = OSError("synthetic-constructor-failure")
        trace = self.faulted("next", error, store_fdopen=error)
        self.assert_closed_before_unlink(trace)
        self.assertEqual(trace.fdopened, ["draft"])
        self.assertEqual(trace.replaced, [])
        self.assertEqual(len(trace.closes), 1)
        # _replace_store directly: no draft involved, the prior store is untouched and no temporary remains
        before, store = self.tree(), self.fx.store.read_bytes()
        with Trace(self.new_target(), store_fdopen=error) as direct:
            with self.assertRaises(OSError) as caught:
                kit_repeat._replace_store(self.fx.store, b"{}\n")
        self.assertIs(caught.exception, error)
        self.assertEqual(direct.leaked, [])
        self.assert_closed_before_unlink(direct)
        self.assertEqual(self.fx.store.read_bytes(), store)
        self.assertEqual(self.tree(), before)

    # --- other categories and acquisition failure ------------------------------------------------------------
    def test_pretransfer_non_oserror_unwinds_without_reclassification(self):
        sites = ("draft_fdopen", "fchmod", "store_fdopen")
        for site in sites:
            for make in (MemoryError, Interrupt):
                with self.subTest(site=site, error=make.__name__):
                    error = make("synthetic")
                    trace = self.faulted("next", error, **{site: error})
                    self.assert_closed_before_unlink(trace)
                    self.assertEqual(len(trace.closes), 1)
                    self.assertEqual(trace.replaced, [])

    def test_output_acquisition_failure_never_closes_unowned_descriptor(self):
        self.bound()
        before = self.tree()
        error = OSError(errno.EACCES, "synthetic-open-failure")
        trace = self.faulted("next", error, open=error)
        self.assertEqual(trace.closes, [])
        error = OSError(errno.ENOSPC, "synthetic-mkstemp-failure")
        trace = self.faulted("next", error, mkstemp=error)  # the draft stream closed normally, then rolled back
        self.assertEqual(trace.closes, [])
        self.assertEqual(self.tree(), before)
        # a real collision at exclusive creation keeps its legacy translation and never touches the existing file
        target = self.new_target()
        target.write_text("existing, never replaced\n")
        before = self.tree()
        with Trace(target) as trace, mock.patch.object(kit_repeat.os.path, "lexists", lambda p: False if str(p) == str(target) else REAL_LEXISTS(p)):
            with self.assertRaises(ValueError) as caught:
                self.call()
        self.assertIn("already exists; it is never replaced", str(caught.exception))
        self.assertEqual(trace.closes, [])
        self.assertEqual(trace.leaked, [])
        self.assertEqual(self.tree(), before)
        self.assertTrue(is_open(self.sentinel))

    # --- after a successful transfer --------------------------------------------------------------------------
    def test_successful_transfer_and_posttransfer_failures_close_once(self):
        self.bound()
        with Trace(self.new_target("ok")) as ok:
            out = self.call("ok")
        self.assertEqual((ok.closes, ok.leaked, ok.fdopened), ([], [], ["draft", "store"]))
        source = self.fx.draft().read_text(encoding="utf-8")
        body = source.replace("2026-01-01 through 2026-09-30", "2026-10-01 through 2026-11-01").encode()
        self.assertEqual(self.fx.draft("ok").read_bytes(), body)
        self.assertEqual(out["draft_sha256"], hashlib.sha256(body).hexdigest())
        stored = json.loads(self.fx.store.read_text())["requests"]
        self.assertEqual(sorted(stored), [f"{self.fx.agency}#bound", f"{self.fx.agency}#ok"])
        self.assertEqual(stored[f"{self.fx.agency}#ok"]["draft_sha256"], out["draft_sha256"])
        self.assertEqual(self.fx.store.read_bytes(), (json.dumps({"schema_version": 1, "requests": stored}, indent=2) + "\n").encode())
        for path in (self.fx.draft("ok"), self.fx.store):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for site, faults in (("draft write", {"draft_stream": lambda s: FaultyStream(s, write=OSError("write"))}),
                             ("draft close", {"draft_stream": lambda s: FaultyStream(s, close=OSError("close"))}),
                             ("store write", {"store_stream": lambda s: FaultyStream(s, write=OSError("write"))}),
                             ("store close", {"store_stream": lambda s: FaultyStream(s, close=OSError("close"))})):
            with self.subTest(site):
                before = self.tree()
                with Trace(self.new_target("late"), **faults) as trace:
                    with self.assertRaises(OSError):
                        self.call("late")
                self.assertEqual((trace.closes, trace.leaked), ([], []))  # the stream closed it; no raw second close
                self.assertEqual(self.tree(), before)
        error = OSError("synthetic-replace-failure")  # metadata replace failure: exact prior bytes, temporary removed
        before, store = self.tree(), self.fx.store.read_bytes()
        trace = self.faulted("late", error, replace=error)
        self.assertEqual((trace.closes, trace.replaced), ([], ["repeat-drafts.json"]))
        self.assertEqual(self.fx.store.read_bytes(), store)
        self.assertEqual(self.tree(), before)

    # --- repeated calls and recovery --------------------------------------------------------------------------
    def test_repeated_writer_handoff_failures_do_not_accumulate_and_retry(self):
        self.bound()
        source = self.fx.root / "kit" / "sent.json"
        before_chain = (self.fx.store.read_bytes(), source.read_bytes(), self.fx.draft().read_bytes())
        for site in ("draft_fdopen", "fchmod", "store_fdopen"):
            for attempt in range(5):
                error = OSError(f"synthetic-{site}-{attempt}")
                self.faulted("again", error, **{site: error})
        self.assertEqual((self.fx.store.read_bytes(), source.read_bytes(), self.fx.draft().read_bytes()), before_chain)
        out = self.call("again")  # same request name, fault removed
        self.assertEqual(out["follows"], "main")
        self.assertEqual(self.fx.draft("again").read_bytes(), self.fx.draft("bound").read_bytes())
        chain = kit_repeat.read_repeats(self.fx.root)
        self.assertEqual(sorted(chain), [f"{self.fx.agency}#again", f"{self.fx.agency}#bound"])
        code, _, err = self.fx.send("again", "2026-11-01")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fx.sent_entry("again")["follows"], "main")
        self.assertEqual(repeat_fixture.run("kit-coverage", "--directory", self.fx.root)[0], 0)

    def test_real_repeat_cli_fault_has_no_success_and_recovers(self):
        self.bound()
        arguments = ("kit-repeat", "--directory", self.fx.root, "--agency", self.fx.agency, "--request", "main",
                     "--as", "cli", "--through", "2026-11-01")
        for site in ("draft_fdopen", "fchmod", "store_fdopen"):
            with self.subTest(site):
                before = self.tree()
                with Trace(self.fx.draft("cli"), **{site: OSError("synthetic")}) as trace:
                    code, out, err = repeat_fixture.run(*arguments)
                self.assertEqual(code, 1)
                self.assertNotIn("Unsent draft written", out)
                self.assertNotIn("Traceback", err)
                self.assertEqual(trace.leaked, [])
                self.assertEqual(self.tree(), before)
        code, out, err = repeat_fixture.run(*arguments)
        self.assertEqual(code, 0, err)
        self.assertIn("Unsent draft written", out)
        self.assertEqual(sorted(kit_repeat.read_repeats(self.fx.root)), [f"{self.fx.agency}#bound", f"{self.fx.agency}#cli"])


if __name__ == "__main__":
    unittest.main()
