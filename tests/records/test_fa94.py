"""FA94: synthetic M-09 writer durability and fixed-code failure contract.

Fixture vocabulary follows portal_reconcile's documented producer schemas and
portal_items' received/version state. No queue instance or live path is used.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tools import portal_reconcile as portal


class WriterCase(unittest.TestCase):
    def setUp(self):
        # Retain synthetic files (including failure residue) for inspection.
        self.root = Path(tempfile.mkdtemp(prefix="fa94-fixture-"))
        host = "records.example.test"
        self.observations = [{"host_class": "nextrequest", "host": host,
                              "url_sha256": "a" * 64, "allowlisted": True,
                              "request_id": "r1", "path": "/documents/i1"}]
        self.queue = [{"host": host, "request_id": "r1", "item_id": "i1",
                       "state": "received", "has_version": True}]
        self.obs = self.root / "observations.json"
        self.que = self.root / "queue.json"
        self.obs.write_text(json.dumps({"schema": portal.OBSERVATION_SCHEMA,
                                        "observations": self.observations}))
        self.que.write_text(json.dumps({"schema": portal.QUEUE_SCHEMA,
                                        "items": self.queue}))
        self.output = self.root / "private-input-marker-report.json"
        (self.root / "cli-receipts.jsonl").touch()
        self.text = portal.render(portal.reconcile(self.observations, self.queue))
        self.events = []

    def invoke(self, output=True):
        argv = ["--observations", str(self.obs), "--queue-export", str(self.que)]
        if output:
            argv += ["--output", str(self.output)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = portal.main(argv)
        result = code, stdout.getvalue(), stderr.getvalue()
        with (self.root / "cli-receipts.jsonl").open("a") as receipt:
            receipt.write(json.dumps({"entry": "tools.portal_reconcile.main", "argv": argv,
                                      "cwd": str(Path.cwd()), "exit": code,
                                      "stdout": result[1], "stderr": result[2]}) + "\n")
        return result

    @contextlib.contextmanager
    def traced(self, fault=None):
        real_open, real_fdopen, real_fsync, real_close = os.open, os.fdopen, os.fsync, os.close
        events = self.events

        def opening(path, flags, *args, **kwargs):
            directory = bool(flags & os.O_DIRECTORY)
            if directory:
                events.append("parent-open")
                if fault == "parent-open":
                    raise OSError("private-input-marker-parent-open")
            return real_open(path, flags, *args, **kwargs)

        def syncing(fd):
            kind = "parent-sync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-sync"
            events.append(kind)
            if kind == fault:
                raise OSError("private-input-marker-" + kind)
            return real_fsync(fd)

        class Handle:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def __enter__(self):
                self.wrapped.__enter__()
                return self

            def __exit__(self, *args):
                try:
                    return self.wrapped.__exit__(*args)
                finally:
                    events.append("file-close")

            def write(self, text):
                events.append("write")
                if fault == "write":
                    self.wrapped.write(text[:7])
                    raise OSError("private-input-marker-write")
                return self.wrapped.write(text)

            def flush(self):
                events.append("flush")
                self.wrapped.flush()
                if fault == "flush":
                    raise OSError("private-input-marker-flush")

            def fileno(self):
                return self.wrapped.fileno()

        def fdopening(fd, *args, **kwargs):
            wrapped = real_fdopen(fd, *args, **kwargs)
            # Input read_json uses rb and must remain a real file object.
            return Handle(wrapped) if args and args[0] == "w" else wrapped

        def closing(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                events.append("parent-close")
            return real_close(fd)

        with mock.patch.object(portal.os, "open", opening), \
                mock.patch.object(portal.os, "fdopen", fdopening), \
                mock.patch.object(portal.os, "fsync", syncing), \
                mock.patch.object(portal.os, "close", closing):
            yield

    def assert_saved(self, expected=None):
        raw = self.output.read_bytes()
        self.assertEqual(raw, (self.text if expected is None else expected).encode("ascii"))
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        return raw

    def failure_contract(self, fault):
        with self.traced(fault):
            result = self.invoke()
        expected = self.text[:7] if fault == "write" else self.text
        saved = self.assert_saved(expected)
        self.assertEqual(result, (2, "", '{"error": "output_not_created"}\n'))
        inode = self.output.stat().st_ino
        retry = self.invoke()
        self.assertEqual(retry, (2, "", '{"error": "output_not_created"}\n'))
        self.assertEqual(self.output.read_bytes(), saved)
        self.assertEqual(self.output.stat().st_ino, inode)
        self.assertNotIn("private-input-marker", "".join(result[1:]))
        self.assertEqual(list(self.root.glob("*report*")), [self.output])


class Behavior(WriterCase):
    def test_write_flush_file_sync_directory_sync_order_and_reload(self):
        with self.traced():
            self.assertEqual(self.invoke(), (0, "", ""))
        self.assert_saved()
        self.assertEqual(self.events, ["write", "flush", "file-sync", "file-close",
                                       "parent-open", "parent-sync", "parent-close"])
        report = json.loads(self.output.read_text())
        self.assertEqual(report["classes"]["fetched"], 1)
        self.assertTrue(report["counts_only"])
        self.assertTrue(report["review_required"])
        for marker in ("example.test", "r1", "i1", "private-input-marker"):
            self.assertNotIn(marker, self.output.read_text())

    def test_symlinked_containing_directory_succeeds_with_order_and_reload(self):
        real = self.root / "real-parent"
        real.mkdir()
        link = self.root / "linked-parent"
        link.symlink_to(real, target_is_directory=True)
        link_inode = link.lstat().st_ino
        sentinel = real / "prior.json"
        sentinel.write_bytes(b"prior-report")
        self.output = link / "report.json"
        with self.traced():
            result = self.invoke()
        self.assertEqual(result, (0, "", ""))
        self.assertEqual(self.events, ["write", "flush", "file-sync", "file-close",
                                       "parent-open", "parent-sync", "parent-close"])
        self.assert_saved()
        self.assertEqual((real / "report.json").read_bytes(), self.text.encode("ascii"))
        self.assertEqual(stat.S_IMODE((real / "report.json").stat().st_mode), 0o600)
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.lstat().st_ino, link_inode)
        self.assertEqual(sentinel.read_bytes(), b"prior-report")

    def test_write_failure_preserves_partial_bytes_and_refuses_retry(self):
        self.failure_contract("write")
        self.assertEqual(self.events, ["write", "file-close"])

    def test_flush_failure_preserves_bytes_and_refuses_retry(self):
        self.failure_contract("flush")
        self.assertEqual(self.events, ["write", "flush", "file-close"])

    def test_file_sync_failure_preserves_bytes_and_refuses_retry(self):
        self.failure_contract("file-sync")
        self.assertEqual(self.events, ["write", "flush", "file-sync", "file-close"])

    def test_parent_open_failure_never_reports_success(self):
        self.failure_contract("parent-open")
        self.assertEqual(self.events, ["write", "flush", "file-sync", "file-close", "parent-open"])

    def test_parent_sync_failure_retains_complete_file_and_refuses_retry(self):
        self.failure_contract("parent-sync")
        self.assertEqual(self.events, ["write", "flush", "file-sync", "file-close",
                                       "parent-open", "parent-sync", "parent-close"])

    def test_each_failure_propagates_oserror_from_real_writer(self):
        for fault in ("write", "flush", "file-sync", "parent-open", "parent-sync"):
            with self.subTest(fault=fault):
                path = self.root / (fault + ".json")
                with self.traced(fault), self.assertRaises(OSError):
                    portal.write_new(path, self.text)
                self.assertTrue(path.is_file())

    def test_existing_file_is_never_overwritten_or_deleted(self):
        self.output.write_bytes(b"prior-report")
        before = self.output.stat()
        with self.traced():
            self.assertEqual(self.invoke(), (2, "", '{"error": "output_not_created"}\n'))
        self.assertEqual(self.events, [])
        self.assertEqual(self.output.read_bytes(), b"prior-report")
        self.assertEqual(self.output.stat().st_ino, before.st_ino)

    def test_symlink_leaf_and_target_are_unchanged(self):
        target = self.root / "target.json"
        target.write_bytes(b"prior-target")
        self.output.symlink_to(target)
        before = self.output.lstat()
        with self.traced():
            self.assertEqual(self.invoke(), (2, "", '{"error": "output_not_created"}\n'))
        self.assertEqual(self.events, [])
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(self.output.lstat().st_ino, before.st_ino)
        self.assertEqual(target.read_bytes(), b"prior-target")

    def test_stdout_does_not_create_or_sync_output(self):
        before = sorted(p.name for p in self.root.iterdir())
        with self.traced():
            self.assertEqual(self.invoke(output=False), (0, self.text, ""))
        self.assertEqual(self.events, [])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)

    def test_missing_parent_is_not_created(self):
        self.output = self.root / "missing" / "report.json"
        self.assertEqual(self.invoke(), (2, "", '{"error": "output_not_created"}\n'))
        self.assertFalse(self.output.parent.exists())

    def test_relative_output_uses_current_directory(self):
        old = Path.cwd()
        try:
            os.chdir(self.root)
            portal.write_new("relative.json", self.text)
        finally:
            os.chdir(old)
        self.assertEqual((self.root / "relative.json").read_text(), self.text)


class GateControl(unittest.TestCase):
    def test_directory_sync_suppression_is_detected_then_restored(self):
        # Run actual Behavior regressions with only directory fsync omitted.
        # File write/flush/fsync and parent open/close remain genuine.
        import types

        def omit_directory(fd):
            self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))

        def suite():
            return unittest.TestSuite([
                Behavior("test_write_flush_file_sync_directory_sync_order_and_reload"),
                Behavior("test_parent_sync_failure_retains_complete_file_and_refuses_retry"),
            ])

        original = portal._sync_parent

        def broken(path):
            # Copy only the private directory helper in memory, replacing its
            # fsync syscall. Other os calls delegate to the traced real ones.
            proxy = types.SimpleNamespace(**vars(os))
            proxy.fsync = omit_directory
            function = types.FunctionType(original.__code__,
                                          dict(original.__globals__, os=proxy),
                                          original.__name__, original.__defaults__)
            return function(path)

        bad_stream = io.StringIO()
        with mock.patch.object(portal, "_sync_parent", broken):
            bad = unittest.TextTestRunner(stream=bad_stream, verbosity=2).run(suite())
        run_root = Path(tempfile.mkdtemp(prefix="fa94-control-"))
        (run_root / "suppressed.stderr.txt").write_text(bad_stream.getvalue())
        self.assertEqual(bad.testsRun, 2)
        self.assertEqual(len(bad.errors), 0, bad_stream.getvalue())
        self.assertEqual(len(bad.failures), 2, bad_stream.getvalue())
        self.assertEqual(len(bad.skipped), 0)
        good_stream = io.StringIO()
        good = unittest.TextTestRunner(stream=good_stream, verbosity=2).run(suite())
        (run_root / "restored.stderr.txt").write_text(good_stream.getvalue())
        self.assertEqual(good.testsRun, 2)
        self.assertEqual(len(good.skipped), 0)
        self.assertTrue(good.wasSuccessful(), good_stream.getvalue())


if __name__ == "__main__":
    unittest.main()
