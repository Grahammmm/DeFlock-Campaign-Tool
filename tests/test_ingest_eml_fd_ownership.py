"""RF2100-2: `ingest --eml` closes the source descriptor it acquired when stream construction (or a later read step) fails.

Every descriptor here is a genuine one on a synthetic file, directory or FIFO; faults are injected in process only.
Run from the repository root:
python3 -B -W error::ResourceWarning -m unittest tests.test_ingest_eml_fd_ownership -q
"""

import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from campaign_tool import ingest_eml

REPO_ROOT = Path(__file__).resolve().parents[1]
REFUSAL = "--eml must be a regular, non-symlink file"
CAP_REFUSAL = "Alpha limit: 50 MiB per message; nothing was imported"
BODY = b"Subject: Synthetic 2100\r\nFrom: a@example.org\r\nTo: b@example.org\r\n\r\nsynthetic body\r\n"
REAL_OPEN, REAL_CLOSE, REAL_FDOPEN, REAL_FSTAT = os.open, os.close, os.fdopen, os.fstat


def is_open(fd):
    try:
        REAL_FSTAT(fd)
    except OSError as error:
        if error.errno == errno.EBADF:
            return False
        raise
    return True


class Spy:
    """Context manager: records the no-follow descriptor `_read_message` acquires and every close of it. Right
    after the first close it checks the integer is EBADF, then acquires a sentinel that reuses the integer, so
    a second (erroneous) close of the recycled integer would be detected."""

    def __enter__(self):
        self.fd, self.closes, self.ebadf, self.sentinel = None, 0, None, None
        self.patchers = [mock.patch.object(os, "open", self.spy_open), mock.patch.object(os, "close", self.spy_close)]
        for patcher in self.patchers:
            patcher.start()
        return self

    def __exit__(self, *exc):
        for patcher in self.patchers:
            patcher.stop()
        if self.sentinel is not None:
            REAL_CLOSE(self.sentinel)

    def spy_open(self, path, flags, *rest, **kw):
        fd = REAL_OPEN(path, flags, *rest, **kw)
        if self.fd is None and flags & os.O_NOFOLLOW:
            self.fd = fd
        return fd

    def spy_close(self, fd):
        REAL_CLOSE(fd)
        if fd == self.fd:
            self.closes += 1
            if self.closes == 1:
                self.ebadf = not is_open(fd)
                self.sentinel = REAL_OPEN(os.devnull, os.O_RDONLY)

    def check(self, test):
        """The acquired descriptor was closed exactly once and the sentinel that reused its integer survives."""
        test.assertIsNotNone(self.fd, "no descriptor was acquired")
        test.assertEqual(self.closes, 1)
        test.assertTrue(self.ebadf)
        test.assertEqual(self.sentinel, self.fd)
        REAL_FSTAT(self.sentinel)


class NonOwningStream:
    """A real non-owning stream whose read raises a genuine OSError; records that its own exit closed it."""
    exited = False

    def __init__(self, fd, mode, closefd):
        self.inner = REAL_FDOPEN(fd, mode, closefd=closefd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.inner.close()
        NonOwningStream.exited = True

    def read(self, size):
        raise OSError(errno.EIO, "synthetic read fault")


class IngestEmlFdOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-eml-fd-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.inbox, self.work, self.root = self.base / "inbox", self.base / "work", self.base / "campaign"
        self.inbox.mkdir()
        self.work.mkdir()

    def regular(self, data=BODY, name="m.eml"):
        path = self.inbox / name
        path.write_bytes(data)
        return str(path)

    def folder(self, name="directory.eml"):
        path = self.inbox / name
        path.mkdir()
        return str(path)

    def fifo(self):
        path = str(self.inbox / "pipe.eml")
        os.mkfifo(path)
        return path

    def cli(self, *arguments, returncode=0):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                                cwd=self.work, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, returncode, f"stdout={result.stdout!r}; stderr={result.stderr!r}")
        self.assertNotIn("Traceback", result.stderr)
        return result

    def init(self):
        self.cli("init", "--directory", self.root, "--name", "Synthetic Campaign", "--county", "Synthetic County",
                 "--state", "ca")

    def tree(self):
        snapshot = {}
        for path in sorted(self.base.rglob("*")):
            info = path.lstat()
            snapshot[str(path.relative_to(self.base))] = (
                path.read_bytes() if path.is_file() else None, info.st_mode, info.st_mtime_ns)
        return snapshot

    def test_real_directory_constructor_failure_closes_acquired_fd(self):
        folder = self.folder()
        before = self.tree()
        for _ in range(5):  # repeated calls in one process must not accumulate live source descriptors
            with Spy() as spy:
                with self.assertRaises(IsADirectoryError):
                    ingest_eml._read_message(folder)
                spy.check(self)
            self.assertEqual(self.tree(), before)

    def test_stream_constructor_faults_close_raw_fd_without_reclassifying(self):
        path = self.regular()
        before = self.tree()

        def adapter_then_fail(fd, *args, **kw):  # a real non-owning stream, cleaned by the adapter, then a fault
            REAL_FDOPEN(fd, "rb", closefd=False).close()
            raise OSError(errno.EIO, "synthetic constructor fault")

        for label, fault in (("OSError", OSError(errno.EMFILE, "synthetic")), ("MemoryError", MemoryError("synthetic")),
                             ("KeyboardInterrupt", KeyboardInterrupt()), ("adapter", adapter_then_fail)):
            with self.subTest(label), Spy() as spy:
                expected = type(fault) if not callable(fault) else OSError
                with mock.patch.object(os, "fdopen", side_effect=fault), self.assertRaises(expected) as caught:
                    ingest_eml._read_message(path)
                if not callable(fault):
                    self.assertIs(caught.exception, fault)
                spy.check(self)
            self.assertEqual(self.tree(), before)

    def test_stream_lifetime_closes_once_on_success_and_later_faults(self):
        for label, data in (("bytes", BODY), ("empty", b"")):
            with self.subTest(label), Spy() as spy:
                self.assertEqual(ingest_eml._read_message(self.regular(data, f"{label}.eml")), data)
                spy.check(self)
        with self.subTest("fstat"), Spy() as spy:
            with mock.patch.object(os, "fstat", side_effect=OSError(errno.EIO, "synthetic")), \
                    self.assertRaises(OSError):
                ingest_eml._read_message(self.regular(name="f.eml"))
            spy.check(self)
        with self.subTest("read"), Spy() as spy:
            with mock.patch.object(os, "fdopen", NonOwningStream), self.assertRaises(OSError):
                ingest_eml._read_message(self.regular(name="r.eml"))
            spy.check(self)
            self.assertTrue(NonOwningStream.exited)
        with self.subTest("fifo"), Spy() as spy:
            with self.assertRaises(ValueError) as caught:
                ingest_eml._read_message(self.fifo())
            self.assertEqual(str(caught.exception), REFUSAL)
            spy.check(self)
        with self.subTest("cap"), Spy() as spy:
            with mock.patch.object(ingest_eml, "MAX_BYTES", 10), self.assertRaises(ValueError) as caught:
                ingest_eml._read_message(self.regular(b"x" * 100, "big.eml"))
            self.assertEqual(str(caught.exception), CAP_REFUSAL)
            spy.check(self)

    def test_reader_admission_and_cap_contract_stay_unchanged(self):
        target = self.regular()
        link, dangling = self.inbox / "link.eml", self.inbox / "dangling.eml"
        os.symlink(target, link)
        os.symlink(self.inbox / "gone.eml", dangling)
        for label, path in (("missing", str(self.inbox / "nope.eml")), ("symlink", str(link)),
                            ("dangling", str(dangling)), ("fifo", self.fifo())):
            with self.subTest(label), self.assertRaises(ValueError) as caught:
                ingest_eml._read_message(path)
            self.assertEqual(str(caught.exception), REFUSAL)
        with self.assertRaises(OSError):
            ingest_eml._read_message(self.folder())
        ten, eleven = self.regular(b"y" * 10, "ten.eml"), self.regular(b"y" * 11, "eleven.eml")
        with mock.patch.object(ingest_eml, "MAX_BYTES", 10):
            self.assertEqual(ingest_eml._read_message(ten), b"y" * 10)
            with self.assertRaises(ValueError) as caught:
                ingest_eml._read_message(eleven)
        self.assertEqual(str(caught.exception), CAP_REFUSAL)
        self.assertEqual(ingest_eml._read_message(target), BODY)

    def test_constructor_failure_stops_preview_and_apply_before_any_campaign_work(self):
        self.init()
        self.cli("ingest", "--eml", self.regular(name="seed.eml"), "--source-id", "synthetic-seed",
                 "--directory", self.root)
        tripwires = {name: mock.Mock(side_effect=AssertionError(name)) for name in (
            "_require_receipt_name", "_extract", "_ledger_lookup", "database", "_previewer", "_store_bytes",
            "_store_message")}
        folder, healthy = self.folder(), self.regular(name="ok.eml")
        for label, path, fdopen_fault in (("directory", folder, None), ("regular", healthy, OSError("synthetic"))):
            for dry in (True, False):
                with self.subTest((label, dry)), Spy() as spy:
                    before = self.tree()
                    args = types.SimpleNamespace(eml=path, source_id="synthetic-1", dry_run=dry)
                    with mock.patch.multiple(ingest_eml, **tripwires), mock.patch("builtins.print") as printed:
                        if fdopen_fault:
                            with mock.patch.object(os, "fdopen", side_effect=fdopen_fault), \
                                    self.assertRaises(OSError):
                                ingest_eml.ingest_eml(args, self.root)
                        else:
                            with self.assertRaises(OSError):
                                ingest_eml.ingest_eml(args, self.root)
                    printed.assert_not_called()
                    spy.check(self)
                    self.assertEqual(self.tree(), before)

    def test_healthy_retry_and_cli_contract_survive_cleanup(self):
        path = self.inbox / "retry.eml"
        path.mkdir()
        with Spy() as spy:
            with self.assertRaises(IsADirectoryError):
                ingest_eml._read_message(str(path))
            spy.check(self)
        path.rmdir()
        path.write_bytes(BODY)
        with Spy() as spy:
            self.assertEqual(ingest_eml._read_message(str(path)), BODY)
            spy.check(self)
        self.init()
        broken = self.cli("ingest", "--eml", self.folder(), "--source-id", "synthetic-1", "--directory", self.root,
                          returncode=1)
        self.assertEqual(broken.stdout, "")
        self.assertTrue(broken.stderr.startswith("Stopped: "))
        for dry in (False, True):
            ok = self.cli("ingest", "--eml", self.regular(name=f"ok{dry}.eml"), "--source-id", "synthetic-1",
                          "--directory", self.root, *(["--dry-run"] if dry else []))
            self.assertIn('"message"', ok.stdout)
            self.assertNotIn("Synthetic 2100", ok.stdout + ok.stderr)


if __name__ == "__main__":
    unittest.main()
