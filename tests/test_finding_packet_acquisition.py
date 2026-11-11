"""RF2210-4: reviewer-packet acquisition helpers release the raw descriptor when a later acquisition step fails.

Synthetic example campaign and synthetic records only. Faults are injected at the real low-level seams (fstat,
fchmod, fdopen) after a real os.open succeeded; closure is judged by a real fstat giving EBADF, never by paths or GC.

Run from the repository root: python3 -B -m unittest tests.test_finding_packet_acquisition -q
"""
import collections
import errno
import os
import socket
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import finding_packet  # noqa: E402
from tests import test_finding_packet as base  # noqa: E402  (module alias: its TestCase is not collected here)

sha, RECORDS, trees = base.sha, base.RECORDS, base.trees
SHAS = sorted(sha(data) for data in RECORDS)


class Tracker:
    """Records every real descriptor opened for a stored object (source) or a new file under ``base`` (sink) and
    fails exactly one seam on the ``nth`` descriptor of ``kind``. On exit, any tracked descriptor still open
    (real fstat succeeds) is listed in ``leaked`` and closed so a failing test cannot cascade."""

    def __init__(self, base_dir, objects, kind=None, nth=1, stage=None, exc=None, close_raises=False):
        self.base_dir, self.objects = str(base_dir), str(objects)
        self.kind, self.nth, self.stage, self.exc, self.close_raises = kind, nth, stage, exc, close_raises
        self.opened, self.counts, self.closes = [], collections.Counter(), collections.Counter()
        self.target, self.fired, self.leaked = None, False, []
        self.real = {name: getattr(os, name) for name in ("open", "fstat", "fchmod", "fdopen", "close")}

    def classify(self, path, flags):
        path = os.fspath(path)
        if os.path.dirname(path) == self.objects:
            return "source"
        if flags & os.O_CREAT and path.startswith(self.base_dir):
            return "sink"
        return None

    def __enter__(self):
        real = self.real

        def opener(path, flags, *args, **kwargs):
            fd = real["open"](path, flags, *args, **kwargs)
            kind = self.classify(path, flags)
            if kind:
                self.counts[kind] += 1
                self.opened.append((kind, fd))
                self.closes[fd] = 0
                if kind == self.kind and self.counts[kind] == self.nth:
                    self.target = fd
            return fd

        def seam(stage):
            def call(fd, *args, **kwargs):
                if stage == self.stage and fd == self.target and not self.fired:
                    self.fired = True
                    raise self.exc
                return real[stage](fd, *args, **kwargs)
            return call

        def closer(fd):
            self.closes[fd] += 1
            real["close"](fd)
            if self.close_raises and fd == self.target:
                raise OSError(errno.EIO, "injected close failure after the descriptor was released")

        self.patches = [mock.patch.object(os, "open", opener), mock.patch.object(os, "close", closer)]
        self.patches += [mock.patch.object(os, name, seam(name)) for name in ("fstat", "fchmod", "fdopen")]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()
        for _, fd in self.opened:
            try:
                self.real["fstat"](fd)
            except OSError as error:
                self.assert_ebadf(error)
            else:
                self.leaked.append(fd)
                self.real["close"](fd)
        return False

    @staticmethod
    def assert_ebadf(error):
        if error.errno != errno.EBADF:
            raise AssertionError(f"expected EBADF, got {error!r}")


def open_fds():
    return set(os.listdir("/proc/self/fd"))


def state(path):
    info = os.lstat(path)
    return (info.st_mode, info.st_mtime_ns, Path(path).read_bytes())


class PacketAcquisitionTests(unittest.TestCase):
    setUp = base.FindingPacketTests.setUp  # synthetic init/ingest/library/draft prerequisites, borrowed not subclassed
    cli = base.FindingPacketTests.cli
    packet = base.FindingPacketTests.packet
    cli_in_process = base.FindingPacketTests.cli_in_process

    def tracker(self, **kwargs):
        return Tracker(self.base, self.objects, **kwargs)

    def fault(self, exc_type=OSError):
        return exc_type(errno.EIO, "injected") if exc_type is OSError else exc_type("injected")

    def source(self, index=0):
        return self.objects / sha(RECORDS[index])

    def test_object_fstat_and_fdopen_failure_close_real_owned_fd(self):
        before, fds = state(self.source()), open_fds()
        for stage in ("fstat", "fdopen"):
            for _ in range(3):  # repeated failures do not accumulate descriptors
                exc = self.fault()
                with self.tracker(kind="source", stage=stage, exc=exc) as tracked:
                    with self.assertRaises(OSError) as caught:
                        finding_packet._open_object(self.objects, sha(RECORDS[0]))
                self.assertIs(caught.exception, exc)
                self.assertTrue(tracked.fired)
                self.assertEqual((len(tracked.opened), tracked.leaked), (1, []), stage)
                self.assertEqual(tracked.closes[tracked.target], 1, stage)
        self.assertEqual(open_fds(), fds)
        self.assertEqual(state(self.source()), before)

    def test_nonregular_and_initial_open_failures_preserve_existing_contract(self):
        objects = self.base / "synthetic-objects"
        objects.mkdir()
        directory, fifo, link, missing = (sha(name)[:64] for name in (b"dir", b"fifo", b"link", b"missing"))
        (objects / directory).mkdir()
        os.mkfifo(objects / fifo)
        os.symlink(self.source(), objects / link)
        sentinel = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(lambda: os.path.exists(f"/proc/self/fd/{sentinel}") and os.close(sentinel))
        for name, closes in ((directory, 1), (fifo, 1), (link, 0), (missing, 0)):
            with Tracker(self.base, objects) as tracked:
                with self.assertRaises(ValueError) as caught:
                    finding_packet._open_object(objects, name)
            self.assertEqual(str(caught.exception), f"{name[:finding_packet.PREFIX]} object_missing")
            self.assertEqual(tracked.leaked, [])
            self.assertEqual(sum(tracked.closes.values()), closes, name)
            os.fstat(sentinel)  # an unrelated descriptor was never closed, here or by a second close

    def test_member_fchmod_and_fdopen_failure_close_before_unwind(self):
        for stage in ("fchmod", "fdopen"):
            for data in (None, b"synthetic member\n"):
                path = self.base / f"member-{stage}-{data is None}"
                exc = self.fault()
                with self.tracker(kind="sink", stage=stage, exc=exc) as tracked:
                    with self.assertRaises(OSError) as caught:
                        finding_packet._create(path, data)
                self.assertIs(caught.exception, exc)
                self.assertEqual((len(tracked.opened), tracked.leaked), (1, []), stage)
                self.assertEqual(tracked.closes[tracked.target], 1, stage)
                self.assertEqual(path.read_bytes(), b"")  # nothing written; the entry is left to the caller's cleanup
                path.unlink()
        taken = self.base / "taken"
        taken.write_bytes(b"preexisting")
        with self.tracker() as tracked:
            with self.assertRaises(FileExistsError):
                finding_packet._create(taken, b"replacement")
        self.assertEqual((taken.read_bytes(), tracked.opened, tracked.leaked, sum(tracked.closes.values())),
                         (b"preexisting", [], [], 0))

    def test_successful_stream_transfer_remains_open_until_its_owner_closes(self):
        with self.tracker() as tracked:
            with finding_packet._open_object(self.objects, sha(RECORDS[0])) as handle:
                fd = handle.fileno()
                self.assertEqual(handle.read(), RECORDS[0])
                self.assertEqual(tracked.closes[fd], 0)
            self.assertTrue(handle.closed)
            self.assertEqual(tracked.closes[fd], 0)  # the handle, not a raw close, released it
        self.assertEqual(tracked.leaked, [])
        old = os.umask(0o277)
        try:
            path = self.base / "stream-member"
            with self.tracker() as tracked:
                handle = finding_packet._create(path)
                fd = handle.fileno()
                self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o600)
                handle.write(b"first ")
                handle.write(b"second")
                self.assertFalse(handle.closed)
                self.assertEqual(tracked.closes[fd], 0)
                handle.close()
                data_path = self.base / "bytes-member"
                self.assertIsNone(finding_packet._create(data_path, b"exact bytes"))
        finally:
            os.umask(old)
        self.assertEqual((tracked.leaked, sum(tracked.closes.values())), ([], 0))
        self.assertEqual((path.read_bytes(), data_path.read_bytes()), (b"first second", b"exact bytes"))
        self.assertEqual([stat.S_IMODE(os.lstat(p).st_mode) for p in (path, data_path)], [0o600, 0o600])

    def test_cleanup_preserves_original_exception_and_process_control(self):
        cases = (("source", "fstat"), ("source", "fdopen"), ("sink", "fchmod"), ("sink", "fdopen"))
        for exc_type in (OSError, RuntimeError, KeyboardInterrupt):
            for kind, stage in cases:
                exc = self.fault(exc_type)
                path = self.base / f"pc-{exc_type.__name__}-{stage}-{kind}"
                with self.tracker(kind=kind, stage=stage, exc=exc) as tracked:
                    with self.assertRaises(exc_type) as caught:
                        if kind == "source":
                            finding_packet._open_object(self.objects, sha(RECORDS[0]))
                        else:
                            finding_packet._create(path)
                self.assertIs(caught.exception, exc)
                self.assertEqual((tracked.leaked, tracked.closes[tracked.target]), ([], 1), (exc_type, stage))
        for kind, stage in cases:  # a close that releases the descriptor and then raises must not hide the failure
            exc = self.fault()
            path = self.base / f"close-{stage}-{kind}"
            with self.tracker(kind=kind, stage=stage, exc=exc, close_raises=True) as tracked:
                with self.assertRaises(OSError) as caught:
                    if kind == "source":
                        finding_packet._open_object(self.objects, sha(RECORDS[1]))
                    else:
                        finding_packet._create(path)
            self.assertIs(caught.exception, exc)
            self.assertEqual((tracked.leaked, tracked.closes[tracked.target]), ([], 1), (stage, kind))

    def untouched(self):
        return trees(self.root), sorted(os.listdir(self.base)), state(self.draft)

    def test_full_builder_preflight_and_copy_source_acquisition_fail_cleanly(self):
        before, fds = self.untouched(), open_fds()
        for nth in (1, 2, 3, 4):  # preflight 1 and 2, then copy 1 and 2 (copy 2 follows a completed member)
            for stage in ("fstat", "fdopen"):
                exc = self.fault()
                with self.tracker(kind="source", nth=nth, stage=stage, exc=exc) as tracked:
                    with self.assertRaises(OSError) as caught:
                        finding_packet.build_packet(self.root, self.draft, self.out)
                self.assertIs(caught.exception, exc)
                self.assertTrue(tracked.fired, (nth, stage))
                self.assertEqual(tracked.leaked, [], (nth, stage))
                self.assertEqual(tracked.counts["source"], nth)
                self.assertEqual(tracked.counts["sink"], {1: 0, 2: 0, 3: 6, 4: 7}[nth])  # copy source opens after its sinks
                self.assertEqual(self.untouched(), before, (nth, stage))  # no target, no owned staging, inputs intact
                self.assertFalse(os.path.lexists(self.out))
        self.assertEqual(open_fds(), fds)

    def test_full_builder_member_acquisition_failure_closes_source_and_sink(self):
        before, fds = self.untouched(), open_fds()
        # sink order: six metadata members, record 1, record 2, MANIFEST.sha256
        for nth in (1, 5, 7, 8, 9):
            for stage in ("fchmod", "fdopen"):
                exc = self.fault()
                with self.tracker(kind="sink", nth=nth, stage=stage, exc=exc) as tracked:
                    with self.assertRaises(OSError) as caught:
                        finding_packet.build_packet(self.root, self.draft, self.out)
                self.assertIs(caught.exception, exc)
                self.assertTrue(tracked.fired, (nth, stage))
                self.assertEqual(tracked.leaked, [], (nth, stage))
                self.assertEqual(tracked.counts["sink"], nth)
                if nth in (7, 8):  # a record sink fails after its own source was opened
                    self.assertEqual(tracked.counts["source"], 2 + (nth - 6))
                self.assertEqual(self.untouched(), before, (nth, stage))
                self.assertFalse(os.path.lexists(self.out))
        self.assertEqual(open_fds(), fds)

    def verify_packet(self, out):
        self.assertEqual(sorted(os.listdir(out)), base.FILES)
        self.assertEqual(sorted(os.listdir(out / "records")), SHAS)
        lines = (out / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 8)  # draft, preview, receipts, reviews, status, README, 2 records
        for line in lines:
            digest, name = line.split("  ", 1)
            self.assertEqual(sha((out / name).read_bytes()), digest, name)
        for data in RECORDS:
            self.assertEqual((out / "records" / sha(data)).read_bytes(), data)
        self.assertEqual((out / "draft.json").read_bytes(), self.draft.read_bytes())
        for path in [out, *out.rglob("*")]:
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700 if path.is_dir() else 0o600, path)

    def test_actual_cli_failure_has_no_success_and_followup_packet_is_compatible(self):
        fds = open_fds()
        first = self.base / "packet-healthy-1"
        result = finding_packet.build_packet(self.root, self.draft, first)
        self.assertEqual((result["written"], result["files"], result["records"]), (str(first), 9, 2))
        self.verify_packet(first)
        before = self.untouched()
        exc = self.fault()
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network used")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("network used")):
            with self.tracker(kind="sink", nth=7, stage="fdopen", exc=exc) as tracked:
                stdout, stderr = self.cli_in_process(2)
        self.assertTrue(tracked.fired)
        self.assertEqual(tracked.leaked, [])
        self.assertEqual(stdout, "")
        self.assertTrue(stderr.startswith("Stopped: "), stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("zipped, uploaded or sent", stdout + stderr)
        self.assertEqual(self.untouched(), before)
        self.assertFalse(os.path.lexists(self.out))
        self.packet()  # real CLI after the failure (exit 0 asserted): no stale fault state
        self.verify_packet(self.out)
        self.assertEqual((self.out / "draft.json").read_bytes(), (first / "draft.json").read_bytes())
        self.assertEqual(open_fds(), fds)


if __name__ == "__main__":
    unittest.main()
