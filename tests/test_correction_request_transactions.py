"""Separate-process correction transactions against external, fictional campaigns."""
import contextlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from datetime import date
from unittest import mock

from campaign_tool import correction_requests as cr

TODAY = date(2026, 3, 20)
FID = "fnd_0000000000000001"
LOCK = ".correction-requests.lock"
TIMEOUT = 15


def _mutate(root, operation):
    if operation[0] == "add":
        return cr.add(root, FID, "2026-02-02", operation[1], today=TODAY)
    return cr.decide(root, operation[1], operation[2], "2026-02-04", operation[3], today=TODAY)


def _worker(root, operation, pipe, release, pause=False, fault=False):
    """Instrument scheduling only; document reads and successful writes remain real."""
    original_load, original_write = cr._load, cr._write_private
    original_lock = getattr(cr, "_locked", None)
    announced = False

    def announce(stage):
        nonlocal announced
        if not announced:
            pipe.send(("stage", stage))
            announced = True

    @contextlib.contextmanager
    def lock(*args, **kwargs):
        announce("locking")
        assert original_lock is not None
        with original_lock(*args, **kwargs):
            yield

    def load(*args, **kwargs):
        announce("reading")  # old code arrives here without attempting a lock
        pipe.send(("read", None))
        return original_load(*args, **kwargs)

    def write(*args, **kwargs):
        if pause:
            pipe.send(("writing", None))
            if not release.wait(TIMEOUT):
                raise RuntimeError("parent did not release writer")
        if fault:
            raise OSError("synthetic write failure")
        return original_write(*args, **kwargs)

    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cr, "_load", load))
            stack.enter_context(mock.patch.object(cr, "_write_private", write))
            if original_lock is not None:
                stack.enter_context(mock.patch.object(cr, "_locked", lock))
            try:
                pipe.send(("ok", _mutate(root, operation)))
            except (ValueError, OSError) as exc:
                pipe.send(("error", str(exc)))
    finally:
        pipe.close()


class CorrectionTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        self.root.mkdir()
        shutil.copytree(Path(__file__).resolve().parents[1] / "examples/fictional-campaign/content",
                        self.root / "content")
        (self.root / "private").mkdir()
        self.path = self.root / "private" / cr.NAME
        self.marker = self.path.with_name(LOCK)
        self.ctx = multiprocessing.get_context("spawn")
        self.children = []
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for child, pipe, release in self.children:
            if child.is_alive():
                release.set()
            child.join(TIMEOUT)
            if child.is_alive():
                child.terminate()
                child.join(TIMEOUT)
            pipe.close()
            child.close()

    def start(self, operation, pause=False, fault=False):
        parent, child_pipe = self.ctx.Pipe()
        release = self.ctx.Event()
        child = self.ctx.Process(target=_worker, args=(str(self.root), operation, child_pipe, release, pause, fault))
        child.start()
        child_pipe.close()
        handle = (child, parent, release)
        self.children.append(handle)
        return handle

    def receive(self, handle):
        self.assertTrue(handle[1].poll(TIMEOUT), "child outcome missing")
        return handle[1].recv()

    def until(self, handle, kinds):
        while True:
            message = self.receive(handle)
            if message[0] in kinds:
                return message

    def outcome(self, handle):
        result = self.until(handle, {"ok", "error"})
        handle[0].join(TIMEOUT)
        self.assertEqual(handle[0].exitcode, 0)
        return result

    def rows(self):
        document = json.loads(self.path.read_text())
        cr._validate_document(document)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        return document["requests"]

    def seed(self, summary="seed"):
        return _mutate(self.root, ("add", summary))

    def race(self, first, second):
        one = self.start(first, pause=True)
        self.assertEqual(self.until(one, {"writing", "error"})[0], "writing")
        before = self.path.read_bytes() if self.path.exists() else None
        inode = self.marker.stat().st_ino if self.marker.exists() else None
        two = self.start(second, pause=True)
        stage = self.receive(two)
        self.assertEqual(stage[0], "stage")
        # A bounded pipe wait, not a sleep: a contender may announce its lock
        # attempt but cannot reach the real load until writer one commits.
        stale = stage[1] == "reading" or two[1].poll(0.25)
        self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)
        one[2].set()
        result_one = self.outcome(one)
        two[2].set()
        result_two = self.outcome(two)
        self.assertEqual(result_one[0], "ok")
        if inode is not None:
            self.assertEqual(self.marker.stat().st_ino, inode)
        return result_one, result_two, stale

    def test_two_adds_keep_distinct_ids_and_both_summaries(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if self.path.exists():
                    self.path.unlink()
                if existing:
                    self.seed()
                one, two, stale = self.race(("add", "writer one"), ("add", "writer two"))
                self.assertEqual(two[0], "ok")
                self.assertNotEqual(one[1]["id"], two[1]["id"])
                self.assertEqual([r["summary"] for r in self.rows()],
                                 (["seed"] if existing else []) + ["writer one", "writer two"])
                self.assertEqual(two[1]["id"], one[1]["id"] + 1)
                self.assertFalse(stale, "contender read uncommitted state")

    def test_add_and_decide_preserve_each_other_in_both_orders(self):
        for add_first in (True, False):
            with self.subTest(add_first=add_first):
                if self.path.exists():
                    self.path.unlink()
                self.seed()
                operations = [("add", "new request"), ("decide", 1, "declined", "first decision")]
                if not add_first:
                    operations.reverse()
                one, two, stale = self.race(*operations)
                self.assertEqual(two[0], "ok")
                rows = self.rows()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["state"], "declined")
                self.assertEqual(rows[1]["summary"], "new request")
                for outcome in (one, two):
                    self.assertEqual(outcome[1], rows[outcome[1]["id"] - 1])
                self.assertFalse(stale)

    def test_two_decisions_only_first_succeeds(self):
        self.seed()
        one, two, stale = self.race(("decide", 1, "accepted", "corrected"),
                                   ("decide", 1, "declined", "stale decision"))
        self.assertEqual(two[0], "error")
        self.assertIn("already accepted; decisions are final", two[1])
        self.assertEqual(self.rows(), [one[1]])
        self.assertFalse(stale)

    def test_decisions_on_different_requests_both_survive(self):
        self.seed("one")
        self.seed("two")
        one, two, stale = self.race(("decide", 1, "accepted", "corrected"),
                                   ("decide", 2, "declined", "supported"))
        self.assertEqual(two[0], "ok")
        self.assertEqual(self.rows(), [one[1], two[1]])
        self.assertFalse(stale)

    def test_validation_and_write_failure_release_transaction_lock(self):
        self.seed()
        valid = self.path.read_bytes()
        for failure in ("malformed", "final", "write", "write_decide"):
            with self.subTest(failure=failure):
                self.path.write_bytes(valid)
                operation = ("add", "failed")
                if failure == "malformed":
                    self.path.write_text("{bad json")
                elif failure == "final":
                    _mutate(self.root, ("decide", 1, "declined", "final"))
                    operation = ("decide", 1, "accepted", "cannot replace")
                elif failure == "write_decide":
                    operation = ("decide", 1, "declined", "failed")
                before = self.path.read_bytes()
                failed = self.start(operation, fault=failure.startswith("write"))
                self.assertEqual(self.outcome(failed)[0], "error")
                self.assertEqual(self.path.read_bytes(), before)
                inode = self.marker.stat().st_ino
                self.path.write_bytes(valid)
                followup = self.outcome(self.start(("add", "after failure")))
                self.assertEqual(followup[0], "ok")
                self.assertEqual(self.marker.stat().st_ino, inode)
                self.assertEqual(self.rows()[-1], followup[1])

    def test_process_exit_releases_lock_without_cleanup(self):
        self.seed()
        before = self.path.read_bytes()
        child = self.start(("add", "terminated"), pause=True)
        self.assertEqual(self.until(child, {"writing", "error"})[0], "writing")
        inode = self.marker.stat().st_ino
        child[0].terminate()
        child[0].join(TIMEOUT)
        self.assertIsNotNone(child[0].exitcode)
        self.assertNotEqual(child[0].exitcode, 0)
        self.assertEqual(self.path.read_bytes(), before)
        result = self.outcome(self.start(("add", "after exit")))
        self.assertEqual(result[0], "ok")
        self.assertEqual(self.marker.stat().st_ino, inode)
        self.assertEqual([r["summary"] for r in self.rows()], ["seed", "after exit"])

    def test_lock_symlink_and_nonregular_refuse_without_target_change(self):
        self.seed()
        self.marker.unlink()
        target = self.root / "target"
        target.write_bytes(b"unchanged")
        for kind in ("symlink", "dangling", "directory", "fifo"):
            if kind == "fifo" and not hasattr(os, "mkfifo"):
                continue
            with self.subTest(kind=kind):
                if kind in ("symlink", "dangling"):
                    self.marker.symlink_to(target if kind == "symlink" else self.root / "absent")
                elif kind == "directory":
                    self.marker.mkdir()
                else:
                    os.mkfifo(self.marker)
                before = self.path.read_bytes()
                for operation in (("add", "refused"), ("decide", 1, "declined", "refused")):
                    self.assertEqual(self.outcome(self.start(operation))[0], "error")
                    self.assertEqual(self.path.read_bytes(), before)
                    self.assertEqual(target.read_bytes(), b"unchanged")
                    self.assertFalse((self.root / "absent").exists())
                if kind == "directory":
                    self.marker.rmdir()
                else:
                    self.marker.unlink()

    def test_lock_inode_remains_stable_across_json_replacements(self):
        self.seed()
        inode = self.marker.stat().st_ino
        old_log = self.path.open("rb")
        with old_log:
            old_inode = os.fstat(old_log.fileno()).st_ino
            one, two, stale = self.race(("add", "second"), ("add", "third"))
            self.assertEqual(two[0], "ok")
            self.assertNotEqual(self.path.stat().st_ino, old_inode)
        result = self.outcome(self.start(("add", "fourth")))
        self.assertEqual(result[0], "ok")
        self.assertEqual(self.marker.stat().st_ino, inode)
        self.assertEqual([r["id"] for r in self.rows()], [1, 2, 3, 4])
        self.assertFalse(stale)
        self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)
        self.assertEqual(self.marker.read_bytes(), b"")

    def test_read_only_consumers_never_create_or_acquire_mutation_lock(self):
        for present in (False, True):
            with self.subTest(present=present):
                if present:
                    self.seed()
                    self.marker.unlink()
                before = {str(p.relative_to(self.root)): p.read_bytes()
                          for p in self.root.rglob("*") if p.is_file()}
                with mock.patch.object(cr, "_locked", side_effect=AssertionError("reader acquired lock")):
                    cr.list_requests(self.root, TODAY)
                    cr.open_overdue(self.root, TODAY)
                    cr.quarterly_counts(self.root, TODAY)
                    if present:
                        cr._reply_request(self.root, 1)
                    else:
                        with self.assertRaises(ValueError):
                            cr._reply_request(self.root, 1)
                after = {str(p.relative_to(self.root)): p.read_bytes()
                         for p in self.root.rglob("*") if p.is_file()}
                self.assertEqual(after, before)
                self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
