"""Live apply takes its own host lock and never accepts a replay."""
from contextlib import contextmanager, closing
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from tests.records import test_holds as legacy
from tests.records import test_runtime_fds as transport
from campaign_tool.records import holds
from campaign_tool.records.ledger import store


class SocketRelease(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = self.root / "project"
        self.processing = legacy.make_project(self.project, [(1, True, "extraction_stage_error", 3)], initialized=True)
        (self.processing / "run.lock").touch()
        (self.processing / "runs").mkdir()
        (self.processing / "runs" / f"extract-{legacy.RUN}.json").write_text("{}")
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.lock = self.runtime / "run-records.lock"
        self.lock.touch()
        self.reports = self.root / "reports"
        self.reports.mkdir(mode=0o700)
        self.report = self.reports / "newest.json"
        legacy.write_report(self.report, [("extract", "EXTRACT_ERROR", 1)])
        self.receipts = self.root / "receipts"

    def apply(self, socket, **options):
        args = dict(apply=True, limit=1, receipt=self.receipts, report="newest.json", runtime_socket=socket, now=legacy.NOW)
        return holds.release(self.project, "EXTRACT_ERROR", **{**args, **options})

    def refused(self, path, status, code, **options):
        before = legacy.tree(self.project)
        receipts = legacy.tree(self.receipts) if self.receipts.exists() else None
        with self.assertRaises(holds.Refused) as caught:
            self.apply(path, **options)
        self.assertEqual(caught.exception.args, (status, code))
        self.assertEqual(legacy.tree(self.project), before)
        self.assertEqual(legacy.tree(self.receipts) if self.receipts.exists() else None, receipts)

    def probe(self, held):
        result = subprocess.run([sys.executable, "-B", str(Path(transport.__file__).resolve()), "probe", str(self.lock)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stderr), (3 if held else 0, ""))

    def test_socket_flag_reaches_transport(self):
        status, out, err = legacy.run_main([
            "release", "--project", str(self.project), "--code", "EXTRACT_ERROR",
            "--runtime-socket", str(self.root / "missing" / "session.sock")])
        self.assertEqual((status, out, err), (2, "", "RUNTIME_SOCKET_UNAVAILABLE\n"))

    def test_lock_survives_host_exit_through_receipt_rename(self):
        # Each seam runs the actual operation and an independently opened flock in another process.
        for kill in (False, True):
            with self.subTest(kill=kill):
                with closing(sqlite3.connect(self.processing / "ledger.sqlite")) as db:
                    db.execute("UPDATE alerts SET count=3")
                    db.commit()
                seams = []
                with transport.session(self.root) as (path, host):
                    def check(name):
                        self.probe(True)
                        seams.append(name)
                    real_report, real_candidates = holds._report, holds.candidates
                    real_fsync, real_replace, real_ledger = os.fsync, os.replace, store.ledger
                    def report(*args, **kwargs):
                        check("report")
                        return real_report(*args, **kwargs)
                    def candidates(*args):
                        check("snapshot")
                        return real_candidates(*args)
                    def fsync(*args):
                        check("receipt_fsync")
                        return real_fsync(*args)
                    def replace(*args):
                        check("receipt_rename_before")
                        result = real_replace(*args)
                        check("receipt_rename_after")
                        return result
                    @contextmanager
                    def ledger(*args, **kwargs):
                        with real_ledger(*args, **kwargs) as connection:
                            class Connection:
                                def execute(self, *args):
                                    return connection.execute(*args)
                                def commit(self):
                                    check("ledger_commit_before")
                                    connection.commit()
                                    check("ledger_commit_after")
                            yield Connection()
                    real_receive = holds.receive_runtime_fds
                    @contextmanager
                    def receive(*args, **kwargs):
                        with real_receive(*args, **kwargs) as source:
                            if kill:
                                host.kill()
                                host.wait(timeout=10)
                            yield source
                    with mock.patch.object(holds, "receive_runtime_fds", receive), \
                            mock.patch.object(holds, "_report", report), mock.patch.object(holds, "candidates", candidates), \
                            mock.patch.object(os, "fsync", fsync), mock.patch.object(os, "replace", replace), \
                            mock.patch.object(store, "ledger", ledger):
                        doc = self.apply(path)
                    self.assertEqual(doc["released"], 1)
                    self.assertEqual(doc["runtime_source"], "socket")
                    row = json.loads((self.receipts / doc["receipt_file"]).read_text())
                    self.assertEqual(row["runtime_source"], "socket")
                    self.assertEqual(row["run_report_sha256"], hashlib.sha256(self.report.read_bytes()).hexdigest())
                self.assertEqual(seams, ["report", "snapshot", "receipt_fsync", "ledger_commit_before",
                                         "ledger_commit_after", "receipt_rename_before", "receipt_rename_after"])
                self.probe(False)

    def test_replay_and_later_run_refuse_without_mutation(self):
        with transport.session(self.root) as (path, host):
            host.stdin.write("done\n")
            host.stdin.flush()
            host.wait(timeout=10)
            self.refused(path, 2, "RUNTIME_SOCKET_UNAVAILABLE")
        with transport.session(self.root) as (path, host):
            host.kill()
            host.wait(timeout=10)
            self.refused(path, 2, "RUNTIME_SOCKET_UNAVAILABLE")
        path.unlink(missing_ok=True)
        later = subprocess.Popen([sys.executable, "-B", transport.__file__, "holder", str(self.lock)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(transport.ready(later.stdout), "held")
            with transport.session(self.root) as (path, host):
                self.refused(path, 3, "LEDGER_BUSY", now=legacy.NOW + timedelta(days=2))
        finally:
            later.stdin.close()
            later.wait(timeout=10)
            later.stdout.close()
            later.stderr.close()
        for expected in (1, 0):
            with transport.session(self.root) as (path, host):
                self.assertEqual(self.apply(path)["released"], expected)
        self.assertEqual(len(list(self.receipts.iterdir())), 1)

    def test_relative_report_confinement_and_hash(self):
        for name in ("", ".", "..", "../newest.json", str(self.report), "sub/newest.json", "sub\\newest.json", "bad\0"):
            with self.subTest(name=name), transport.session(self.root) as (path, host):
                self.refused(path, 2, "REPORT_UNREADABLE", report=name)
        for kind in ("symlink", "fifo", "directory", "missing", "malformed", "stale", "future", "not_latest"):
            entry = self.reports / "bad"
            if kind == "symlink":
                entry.symlink_to(self.report)
            elif kind == "fifo":
                os.mkfifo(entry)
            elif kind == "directory":
                entry.mkdir()
            elif kind in ("malformed", "stale", "future", "not_latest"):
                run = {"stale": "run_20261110T150000Z_0a1b2c3d", "future": "run_20261114T150000Z_0a1b2c3d",
                       "not_latest": "run_20261113T140000Z_0a1b2c3d"}.get(kind, "malformed")
                entry.write_text(json.dumps({"run_id": run, "needs_g": []}))
            code = "REPORT_STALE" if kind in ("stale", "future") else "REPORT_NOT_LATEST" if kind == "not_latest" else "REPORT_UNREADABLE"
            with self.subTest(kind=kind), transport.session(self.root) as (path, host):
                self.refused(path, 2, code, report="bad")
            if kind == "directory":
                entry.rmdir()
            else:
                entry.unlink(missing_ok=True)

    def test_invalid_lock_and_stage_or_sqlite_busy(self):
        self.lock.unlink()
        for kind in ("missing", "directory", "symlink", "fifo"):
            if kind == "directory":
                self.lock.mkdir()
            elif kind == "symlink":
                self.lock.symlink_to(self.report)
            elif kind == "fifo":
                os.mkfifo(self.lock)
            with self.subTest(kind=kind), transport.session(self.root) as (path, host):
                self.refused(path, 2, "RUNTIME_LOCK_UNREADABLE")
            if kind == "directory":
                self.lock.rmdir()
            else:
                self.lock.unlink(missing_ok=True)
        self.lock.touch()
        import fcntl
        with open(self.processing / "run.lock", "rb") as stage:
            fcntl.flock(stage, fcntl.LOCK_EX)
            with transport.session(self.root) as (path, host):
                self.refused(path, 3, "LEDGER_BUSY")
        self.receipts.mkdir(mode=0o700)
        wal = self.processing / "ledger.sqlite-wal"
        wal.write_bytes(b"busy")
        with transport.session(self.root) as (path, host):
            self.refused(path, 3, "LEDGER_BUSY")
        wal.unlink()

    def test_receipt_failures_retain_lock_until_unwind(self):
        for seam, code, committed in (("_write_receipt", "RECEIPT_UNSAFE", False),
                                      ("replace", "RECEIPT_NOT_PUBLISHED", True)):
            with self.subTest(seam=seam):
                with closing(sqlite3.connect(self.processing / "ledger.sqlite")) as db:
                    db.execute("UPDATE alerts SET count=3")
                    db.commit()
                def fail(*args):
                    self.probe(True)
                    raise OSError("synthetic receipt failure")
                target = holds if seam == "_write_receipt" else os
                before = legacy.tree(self.project)
                with transport.session(self.root) as (path, host), mock.patch.object(target, seam, fail):
                    with self.assertRaises(holds.Refused) as caught:
                        self.apply(path)
                self.assertEqual(caught.exception.args[0], 1 if committed else 2)
                self.assertTrue(caught.exception.args[1].startswith(code))
                if not committed:
                    self.assertEqual(legacy.tree(self.project), before)
                else:
                    self.assertEqual(len(list(self.receipts.glob(".*.tmp"))), 1)
                self.probe(False)

    def test_attended_cli_apply_end_to_end(self):
        from datetime import datetime, timezone
        run_id = datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ_0a1b2c3d")
        (self.processing / "runs" / f"extract-{run_id}.json").write_text("{}")
        self.report.write_text(json.dumps({"run_id": run_id, "needs_g": []}))
        private = self.root / "private"
        private.mkdir(mode=0o700)
        command = [sys.executable, "-B", str(transport.TOOL), "--runtime", str(self.runtime),
                   "--reports", str(self.reports), "--socket-dir", str(private), "--", sys.executable,
                   "-B", "-m", "campaign_tool.records", "holds", "release", "--project", str(self.project),
                   "--code", "EXTRACT_ERROR", "--apply", "--max", "1", "--receipt", str(self.receipts),
                   "--report", "newest.json", "--runtime-socket", "{socket}", "--json"]
        result = subprocess.run(command, cwd=transport.REPO, capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        doc = json.loads(result.stdout)
        self.assertEqual((doc["released"], doc["runtime_source"]), (1, "socket"))
        self.assertEqual(list(private.iterdir()), [])

    def test_socket_dry_run_and_mixed_api(self):
        before = legacy.tree(self.project)
        with transport.session(self.root) as (path, host):
            doc = self.apply(path, apply=False, report="../ignored", receipt=self.project)
        self.assertEqual((doc["mode"], doc["runtime_source"], doc["runtime_lock"]), ("dry_run", "socket", "not_checked"))
        self.assertEqual(legacy.tree(self.project), before)
        self.assertFalse(self.receipts.exists())
        self.refused(self.root / "missing", 2, "RUNTIME_SOCKET_UNSAFE", runtime_lock=self.lock)
        self.assertEqual(holds.release(self.project, "EXTRACT_ERROR")["runtime_lock"], "not_checked")


if __name__ == "__main__":
    unittest.main()
