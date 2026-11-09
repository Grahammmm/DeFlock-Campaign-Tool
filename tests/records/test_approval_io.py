"""Approval input admission: FIFOs are refused without blocking, the journal cap binds bytes consumed."""
import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq
from tests.approval_signing import Signer

REPO = Path(__file__).resolve().parents[2]
HAS_FIFO = hasattr(os, "mkfifo")
TIMEOUT = 8
CHILD = """
import sys
from campaign_tool.records import approval_queue as aq
root, action = sys.argv[1], sys.argv[2]
try:
    if action == "list":
        aq.load_queue(root)
    elif action == "add":
        aq.add_draft(root, item_id="%s", kind="reply-extension", agency_id="a", request_id="r",
                     body="b", evidence={"x": 1}, source_run_id="run-1")
    elif action == "append":
        with aq._Store(root, True) as store:
            store.append([{"x": 1}])
    elif action == "received":
        aq.queue_received_items(root, "run-1", sys.argv[3])
except aq.ApprovalQueueError as error:
    print(error.code)
    sys.exit(1)
print("ok")
""" % ("1" * 64)


def run(argv):
    """Run a child; a timeout is a test failure (the child is killed and reaped by subprocess.run)."""
    try:
        return subprocess.run([sys.executable, "-B", *argv], cwd=REPO, capture_output=True, text=True,
                              timeout=TIMEOUT)
    except subprocess.TimeoutExpired as error:
        raise AssertionError(f"child blocked for {TIMEOUT}s: {argv[-3:]}") from error


def child(root, action, *extra):
    return run(["-c", CHILD, str(root), action, *map(str, extra)])


def cli(*argv):
    return run(["-m", "campaign_tool.records", "approvals", *map(str, argv)])


def snapshot(root):
    out = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        data = path.read_bytes() if stat.S_ISREG(info.st_mode) else b""
        out[str(path.relative_to(root))] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), data)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.project("project")

    def project(self, name):
        root = self.base / name
        root.mkdir(mode=0o700)
        return root

    def approvals(self, root=None):
        path = (root or self.root) / "approvals"
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    def draft(self, root=None, body="body", item="1"):
        return aq.add_draft(root or self.root, item_id=item * 64, kind="reply-extension", agency_id="a",
                            request_id="r", body=body, evidence={"x": 1}, source_run_id="run-1")


@unittest.skipUnless(HAS_FIFO, "no mkfifo")
class FifoTests(Base):
    def keep_writer(self, path):
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, fd)

    def fifo(self, path, writer):
        os.mkfifo(path, 0o600)
        if writer:
            self.keep_writer(path)

    def test_fifo_journal_and_lock_refuse_without_waiting(self):
        for name in ("queue.jsonl", ".lock"):
            for writer in (False, True):
                with self.subTest(name=name, writer=writer):
                    root = self.project(f"p-{name[:5].strip('.')}-{writer}")
                    approvals = self.approvals(root)
                    self.fifo(approvals / name, writer)
                    before = snapshot(root)
                    actions = [child(root, "list"), child(root, "add"), child(root, "append"),
                               cli("list", "--project", root), cli("payload", "x", "--project", root),
                               cli("history", "--id", "x", "--project", root)]
                    for proc in actions:
                        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                        self.assertIn("queue_invalid", proc.stdout + proc.stderr)
                        self.assertNotIn("Traceback", proc.stderr)
                        self.assertNotIn(str(root), proc.stdout + proc.stderr)
                    after = snapshot(root)
                    if name == "queue.jsonl":  # an entered writer may keep its normal empty lock; nothing else
                        self.assertEqual(set(after) - set(before), {"approvals/.lock"})
                        after.pop("approvals/.lock")
                    self.assertEqual(before, after)
                    self.assertTrue(stat.S_ISFIFO(os.lstat(approvals / name).st_mode))

    def test_fifo_received_body_and_signature_refuse_before_mutation(self):
        fifo = self.base / "input.fifo"
        os.mkfifo(fifo, 0o600)
        for writer in (False, True):
            if writer:
                self.keep_writer(fifo)
            with self.subTest(writer=writer):
                fresh = self.project(f"fresh-{writer}")  # absent store must stay absent
                proc = child(fresh, "received", fifo)
                self.assertEqual((proc.returncode, proc.stdout.strip()), (1, "source_invalid"), proc.stderr)
                self.assertFalse((fresh / "approvals").exists())
                row = self.draft(self.project(f"has-{writer}"))
                root = self.base / f"has-{writer}"
                before = snapshot(root)
                for argv in (("edit", row["id"], "--actor", "G", "--body-file", fifo),
                             ("approve", row["id"], "--signature", fifo)):
                    proc = cli(*argv, "--project", root)
                    self.assertEqual((proc.returncode, proc.stderr.strip()), (1, "source_invalid"), proc.stdout)
                    self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(snapshot(root), before)


class GrowingJournalTests(Base):
    def setUp(self):
        super().setUp()
        self.journal = self.approvals() / "queue.jsonl"

    def put(self, data):
        self.journal.write_bytes(data)
        os.chmod(self.journal, 0o600)

    def raw(self):
        with aq._Store(self.root, False) as store:
            return store.raw()

    def watch(self, wrapper):
        real, calls = os.read, []

        def spy(fd, n):
            calls.append(n)
            return wrapper(real, fd, n, len(calls))
        return mock.patch.object(os, "read", spy), calls

    def test_growing_journal_consumes_at_most_cap_plus_one(self):
        cap = 16
        self.put(b"x" * 8)

        def grow(real, fd, n, call):
            if call == 1:
                with open(self.journal, "ab") as extra:
                    extra.write(b"y" * 27)
            return real(fd, n)
        patch, calls = self.watch(grow)
        with mock.patch.object(aq, "MAX_JOURNAL", cap), patch, \
                mock.patch.object(aq, "_fold", side_effect=AssertionError("fold must not run")):
            with self.assertRaises(aq.ApprovalQueueError) as caught:
                aq.load_queue(self.root)
        self.assertEqual(caught.exception.code, "queue_invalid")
        self.assertTrue(calls and max(calls) <= cap + 1 and sum(calls) <= cap + 1, calls)
        self.assertEqual(self.journal.stat().st_size, 8 + 27)  # only the test's own append; reader mutated nothing

    def test_cap_boundaries_and_reads(self):
        cap = 16
        with mock.patch.object(aq, "MAX_JOURNAL", cap):
            self.put(b"x" * (cap + 1))  # initial oversize: refused from metadata, never read
            patch, calls = self.watch(lambda real, fd, n, call: real(fd, n))
            with patch, self.assertRaises(aq.ApprovalQueueError) as caught:
                self.raw()
            self.assertEqual((caught.exception.code, calls), ("queue_invalid", []))
            self.put(b"x" * cap)  # exactly the cap then EOF is admissible
            self.assertEqual(self.raw(), b"x" * cap)
            self.put(b"")  # ordinary EOF
            self.assertEqual(self.raw(), b"")
            self.put(b"abcdefghij")  # short reads: each request is bounded by the unconsumed allowance
            patch, calls = self.watch(lambda real, fd, n, call: real(fd, 1))
            with patch:
                self.assertEqual(self.raw(), b"abcdefghij")
            self.assertEqual(calls, [cap + 1 - done for done in range(11)])

    def test_cap_plus_one_after_preflight_is_refused(self):
        cap = 16
        self.put(b"x" * cap)
        real_fstat = os.fstat

        def stale_size(fd):  # the size check passes, then the file is longer by one byte
            info = real_fstat(fd)
            if stat.S_ISREG(info.st_mode):
                with open(self.journal, "ab") as extra:
                    extra.write(b"z")
            return info
        with mock.patch.object(aq, "MAX_JOURNAL", cap), mock.patch.object(os, "fstat", stale_size):
            with self.assertRaises(aq.ApprovalQueueError) as caught:
                self.raw()
        self.assertEqual(caught.exception.code, "queue_invalid")


class DescriptorTests(Base):
    def tracked(self):
        real_open = os.open
        opened = []

        def spy(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd
        return mock.patch.object(os, "open", spy), opened

    def assert_closed(self, opened):
        for fd in opened:
            with self.assertRaises(OSError, msg=f"descriptor {fd} left open"):
                os.fstat(fd)

    def expect(self, call, code, *args):
        patch, opened = self.tracked()
        with patch:
            with self.assertRaises(aq.ApprovalQueueError) as caught:
                call(*args)
        self.assertEqual(caught.exception.code, code)
        self.assert_closed(opened)

    def read_queue(self):
        self.expect(aq.load_queue, "queue_invalid", self.root)

    def test_descriptor_admission_closes_on_errors(self):
        approvals = self.approvals()
        journal = approvals / "queue.jsonl"
        elsewhere = self.base / "elsewhere"
        elsewhere.write_text("x", encoding="utf-8")
        os.chmod(elsewhere, 0o600)
        os.symlink(elsewhere, journal)
        self.read_queue()  # symlink
        journal.unlink()
        os.symlink(self.base / "missing", journal)
        self.read_queue()  # dangling link
        journal.unlink()
        journal.mkdir()
        self.read_queue()  # directory
        journal.rmdir()
        if HAS_FIFO:
            os.mkfifo(journal, 0o600)
            self.read_queue()
            journal.unlink()
        journal.write_bytes(b"")
        os.chmod(journal, 0o600)
        os.link(journal, self.base / "second-name")
        self.read_queue()  # hard link
        (self.base / "second-name").unlink()
        os.chmod(journal, 0o644)
        self.read_queue()  # unsafe mode
        os.chmod(journal, 0o600)
        self.assertEqual(aq.load_queue(self.root), [])

        real_fstat = os.fstat

        def failing_fstat(fd):
            if not stat.S_ISDIR(real_fstat(fd).st_mode):
                raise OSError(errno.EIO, "injected")
            return real_fstat(fd)
        with mock.patch.object(os, "fstat", failing_fstat):
            self.read_queue()
        with mock.patch.object(os, "read", side_effect=OSError(errno.EIO, "injected")):
            self.expect(aq.load_queue, "queue_io", self.root)
        before = snapshot(self.root)
        self.assertEqual(snapshot(self.root), before)

    def test_source_inputs_refuse_with_stable_codes_and_close(self):
        source = self.base / "source.json"
        source.write_bytes(b"{}")
        os.chmod(source, 0o644)  # source files keep their existing, looser permission policy
        self.assertEqual(aq._read_source(source), b"{}")
        self.assertEqual(aq._file(source), "{}")
        bad = self.base / "bad.txt"
        bad.write_bytes(b"\xff\xfe")
        os.symlink(source, self.base / "link")
        os.symlink(self.base / "missing", self.base / "dangling")
        (self.base / "dir").mkdir()
        targets = [bad, self.base / "link", self.base / "dangling", self.base / "dir"]
        if HAS_FIFO:
            os.mkfifo(self.base / "fifo", 0o600)
            targets.append(self.base / "fifo")
        def failing_fstat(fd):
            raise OSError(errno.EIO, "injected")
        for reader in (aq._read_source, aq._file):
            for target in targets[(1 if reader is aq._read_source else 0):]:
                with self.subTest(reader=reader.__name__, target=target.name):
                    self.expect(reader, "source_invalid", target)
            with self.subTest(reader=reader.__name__, fault="fstat"), mock.patch.object(os, "fstat", failing_fstat):
                self.expect(reader, "source_invalid", source)
            with self.subTest(reader=reader.__name__, fault="read"), \
                    mock.patch.object(os, "read", side_effect=OSError(errno.EIO, "injected")):
                self.expect(reader, "source_invalid", source)
        proc = cli("edit", "x", "--actor", "G", "--body-file", bad, "--project", self.root)
        self.assertEqual((proc.returncode, proc.stderr.strip()), (1, "source_invalid"))


class LifecycleTests(Base):
    def test_regular_file_approval_lifecycle_and_campaign_isolation(self):
        other = self.project("other")
        absent = self.project("absent")
        one, two = self.draft(self.root, "alpha"), self.draft(other, "alpha")  # same item and request ids
        self.assertEqual(one["id"], two["id"])
        spare = self.draft(self.root, "beta", item="2")
        b_before = snapshot(other)
        signer = Signer()
        self.addCleanup(signer.close)
        edited = aq.edit(self.root, one["id"], "G", "alpha edited")
        self.assertEqual((edited["status"], edited["revision"]), ("pending", 1))
        with signer.trust():
            payload_a, payload_b = aq.approval_payload(self.root, one["id"]), aq.approval_payload(other, one["id"])
            self.assertNotEqual(payload_a, payload_b)  # bound to project, body and revision
            signature = signer.sign(payload_a)
            with self.assertRaises(aq.ApprovalQueueError) as caught:  # a signature for one campaign is no use in another
                aq.approve(other, one["id"], {"kind": "ssh", "signature": signature})
            self.assertEqual(caught.exception.code, "approval_required")
            approved = aq.approve(self.root, one["id"], {"kind": "ssh", "signature": signature})
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(aq.reject(self.root, spare["id"], "G", "dup")["status"], "rejected")
        self.assertEqual(snapshot(other), b_before)  # nothing leaked into the other campaign
        self.assertEqual(aq.load_queue(other)[0]["body"], "alpha")
        history = aq.history(self.root, one["id"])
        self.assertEqual((history["original"]["body"], history["current"]["body"]), ("alpha", "alpha edited"))
        self.assertEqual(aq.reject(other, two["id"], "G", "no")["status"], "rejected")
        self.assertEqual(aq.load_queue(absent), [])  # read-only on an absent store writes nothing
        self.assertFalse((absent / "approvals").exists())
        events = [json.loads(line)["event"] for line in (self.root / "approvals" / "queue.jsonl").read_text().splitlines()]
        self.assertEqual(events, ["draft", "draft", "edited", "approve", "reject"])


if __name__ == "__main__":
    unittest.main()
