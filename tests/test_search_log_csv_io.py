"""RF1155-5: search-log CSV admission, read, final-metadata and close failures stay value-free.

Synthetic campaign and CSV only; offline. Run from the repository root: python3 -B -m unittest tests.test_search_log_csv_io
"""
import contextlib
import csv
import errno
import hashlib
import io
import json
import os
import socket
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, finding_search_log, search_log  # noqa: E402
from tests import test_finding_search_log as finding_base  # noqa: E402  (module aliases: their TestCases are not re-collected)
from tests import test_search_log as base  # noqa: E402

FIXED = "csv could not be read"
CANARY = "SYNTHETIC-CSV-PRIVATE-ERROR"
PATH_CANARY = "PRIVATE-CSV-PATH"
AGENCY = {"agency_id": base.AID, "name": "Synthetic Police Department"}
REAL_OPEN, REAL_FSTAT, REAL_FDOPEN, REAL_CLOSE = os.open, os.fstat, os.fdopen, os.close
STAGES = ("fstat", "fdopen", "construct", "read", "read_after_prefix", "final_fstat", "exit")


def errors():
    return (OSError(errno.EIO, f"{CANARY}-MESSAGE", f"/synthetic/{PATH_CANARY}.csv", None, f"/synthetic/{PATH_CANARY}-2"),
            PermissionError(errno.EACCES, f"{CANARY}-DENIED", f"/synthetic/{PATH_CANARY}.csv"),
            OSError(f"{CANARY} message only"))


def snapshot(base_dir):
    state = {}
    for path in sorted([base_dir, *base_dir.rglob("*")]):
        info = path.lstat()
        data = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        state[str(path.relative_to(base_dir))] = (stat.S_IMODE(info.st_mode), info.st_mtime_ns, data)
    return state


def assert_fixed(test, caught):
    test.assertIs(type(caught.exception), ValueError)
    test.assertEqual(str(caught.exception), FIXED)
    test.assertIsNone(caught.exception.__cause__)
    test.assertTrue(caught.exception.__suppress_context__)


class Handle:
    """The real binary handle of the CSV, with faults at the readinto and context-exit seams."""

    def __init__(self, inner, faults):
        self.inner, self.faults = inner, faults

    def fileno(self):
        return self.inner.fileno()

    def readinto(self, buffer):
        self.faults.reads += 1
        if self.faults.stage == "read" or (self.faults.stage == "read_after_prefix" and self.faults.reads > 1):
            raise self.faults.error
        return self.inner.readinto(buffer)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.inner.close()  # the real close happens first, as it would before a late close error
        if self.faults.stage in ("exit", "exit_always"):
            raise self.faults.error
        return False


class Faults:
    """Faults injected after the real os.open of exactly one path (the CSV under test)."""

    def __init__(self, target, stage, error):
        self.target, self.stage, self.error = os.fspath(target), stage, error
        self.fds, self.live, self.closes, self.fstats, self.reads, self.handles = [], set(), [], {}, 0, []

    def open_(self, path, flags, *args, **kwargs):
        fd = REAL_OPEN(path, flags, *args, **kwargs)
        self.live.discard(fd)  # descriptor numbers are reused once closed
        if os.fspath(path) == self.target:
            self.fds.append(fd)
            self.live.add(fd)
            self.fstats[fd] = 0
        return fd

    def fstat(self, fd):
        if fd in self.live:
            self.fstats[fd] += 1
            if (self.stage == "fstat" and self.fstats[fd] == 1) or (self.stage == "final_fstat" and self.fstats[fd] == 2):
                raise self.error
        return REAL_FSTAT(fd)

    def fdopen(self, fd, *args, **kwargs):
        if fd in self.live and self.stage == "fdopen":
            raise self.error
        handle = REAL_FDOPEN(fd, *args, **kwargs)
        if fd in self.live:
            handle = Handle(handle, self)
            self.handles.append(handle)
        return handle

    def close(self, fd):
        if fd in self.live:
            self.closes.append(fd)
            self.live.discard(fd)
        return REAL_CLOSE(fd)

    def construct(self, *args, **kwargs):
        raise self.error

    def descriptors_closed(self):
        """Every real descriptor was closed: by the application (admission faults) or through its binary handle."""
        if self.handles:
            return all(handle.inner.closed for handle in self.handles)
        return self.closes == self.fds

    @contextlib.contextmanager
    def active(self):
        with contextlib.ExitStack() as stack:
            for name, function in (("open", self.open_), ("fstat", self.fstat), ("fdopen", self.fdopen), ("close", self.close)):
                stack.enter_context(mock.patch.object(os, name, function))
            if self.stage == "construct":
                stack.enter_context(mock.patch.object(search_log.io, "BufferedReader", self.construct))
            yield self


class CsvIoTests(unittest.TestCase):
    # fixtures from the existing module, borrowed as plain functions (no subclassing of its TestCase)
    setUp = base.SearchLogTests.setUp
    csv = base.SearchLogTests.csv
    run_cli = base.SearchLogTests.run_cli

    def snapshot(self):
        return snapshot(self.base)

    def assert_fixed(self, caught):
        assert_fixed(self, caught)

    def assert_cli_fixed(self, path, *extra, code=1):
        formatted = mock.Mock(side_effect=AssertionError("formatter reached"))
        with mock.patch.object(search_log, "format_summary", formatted):
            result = self.run_cli("--csv", path, "--agency", base.AID, *extra)
        self.assertEqual(result, (code, "", f"Stopped: {FIXED}\n"))
        formatted.assert_not_called()

    @contextlib.contextmanager
    def offline(self):
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")):
            yield

    def refuse_all(self, path, stage, error, *extra):
        """Direct API plus actual CLI in text and JSON mode, all through one fault; returns the fault state."""
        limit = csv.field_size_limit()
        with Faults(path, stage, error).active() as faults, self.offline():
            with self.assertRaises(ValueError) as caught:
                search_log.summarize_csv(path, AGENCY)
            self.assert_fixed(caught)
            self.assertEqual(csv.field_size_limit(), limit)
            for mode in ((), ("--json",)):
                self.assert_cli_fixed(path, *extra, *mode)
                self.assertEqual(csv.field_size_limit(), limit)
        self.assertTrue(faults.descriptors_closed(), "a descriptor from the real os.open was left open")
        return faults

    # ---- admission, construction, stream, final metadata, exit ---------------------------------------------------

    def test_csv_admission_fstat_and_fdopen_errors_are_private(self):
        path = self.csv(name=f"{PATH_CANARY}.csv")
        for stage in ("fstat", "fdopen"):
            for error in errors():
                with self.subTest(stage=stage, error=repr(error)):
                    with Faults(path, stage, error).active() as faults:
                        with self.assertRaises(ValueError) as caught:
                            search_log.summarize_csv(path, AGENCY)
                    self.assert_fixed(caught)
                    self.assertEqual(len(faults.fds), 1)  # one admission, no retry
                    self.assertEqual(faults.closes, faults.fds)  # the raw descriptor is closed exactly once
                    faults = self.refuse_all(path, stage, error)
                    self.assertEqual(faults.closes, faults.fds)

    def test_csv_final_fstat_failure_refuses_after_complete_read(self):
        path = self.csv(name=f"{PATH_CANARY}.csv")
        for error in errors():
            with self.subTest(error=repr(error)):
                faults = self.refuse_all(path, "final_fstat", error)
                self.assertGreaterEqual(faults.reads, 2)  # the real stream was consumed through EOF before the refusal

    def test_csv_context_exit_failure_cannot_accept_valid_counts(self):
        path = self.csv(name=f"{PATH_CANARY}.csv")
        for error in errors():
            with self.subTest(error=repr(error)):
                faults = self.refuse_all(path, "exit", error)
                self.assertGreaterEqual(faults.reads, 2)
        # dual fault: malformed content plus a closing error; the outward message is value-free (precedence is not promised)
        bad = self.csv(b"\xff\xfe\x00 not utf-8\n", name=f"{PATH_CANARY}-bad.csv")
        with Faults(bad, "exit_always", errors()[0]).active() as faults:
            with self.assertRaises(ValueError) as caught:
                search_log.summarize_csv(bad, AGENCY)
        self.assert_fixed(caught)
        self.assertTrue(faults.descriptors_closed())
        self.refuse_all(bad, "exit_always", errors()[0])

    def test_csv_reader_construction_and_stream_errors_share_fixed_boundary(self):
        path = self.csv(name=f"{PATH_CANARY}.csv")
        for stage in ("construct", "read", "read_after_prefix"):
            for error in errors():
                with self.subTest(stage=stage, error=repr(error)):
                    self.refuse_all(path, stage, error)

    def test_csv_io_failure_restores_field_limit_and_preserves_tree(self):
        path = self.csv(name=f"{PATH_CANARY}.csv")
        (self.root / "content").mkdir()
        (self.root / "content" / "agencies.json").write_text(json.dumps([{
            "agency_id": base.AID, "name": AGENCY["name"], "kind": "police", "profile": {"sharing": {
                "checked_on": "2026-02-01", "source_url": "https://transparency.flocksafety.com/synthetic",
                "recipients": [{"name": "Alpha PD", "state": "CA", "kind": "local"}]}}}]), encoding="utf-8")
        control = self.run_cli("--csv", path, "--agency", base.AID, "--against-sharing", "--json")
        self.assertEqual(control[0], 0, control)
        before = self.snapshot()
        for stage in STAGES:
            with self.subTest(stage=stage):
                self.refuse_all(path, stage, errors()[0], "--against-sharing")
                self.assertEqual(self.snapshot(), before)  # inputs, membership, bytes, modes and mtimes unchanged

    # ---- preserved behaviour -------------------------------------------------------------------------------------

    def test_existing_refusals_interrupts_and_category_boundary_survive(self):
        limit = csv.field_size_limit()
        missing = self.files / "missing.csv"
        link = self.files / "link.csv"
        link.symlink_to(self.csv())
        fifo = self.files / "fifo.csv"
        os.mkfifo(fifo)
        for target, text in ((missing, "csv cannot be opened as a regular file (symlinks are refused)"),
                             (link, "csv cannot be opened as a regular file (symlinks are refused)"),
                             (fifo, "csv must be a regular file"), (self.files, "csv must be a regular file")):
            with self.subTest(target=target.name), self.assertRaises(ValueError) as caught:
                search_log.summarize_csv(target, AGENCY)
            self.assertEqual(str(caught.exception), text)
        for data, text in ((b"", "csv has no header row"), (b"\xff\xfe\x00\n", "csv is not valid UTF-8"),
                           (b'Time,Purpose\n"open', "csv is malformed near line 2")):
            with self.subTest(data=data), self.assertRaises(ValueError) as caught:
                search_log.summarize_csv(self.csv(data), AGENCY)
            self.assertEqual(str(caught.exception), text)
        # KeyboardInterrupt and SystemExit are not relabelled; the descriptor is closed and the limit restored
        for interrupt in (KeyboardInterrupt(), SystemExit(3)):
            with self.subTest(interrupt=type(interrupt).__name__):
                faults = Faults(self.csv(), "read", interrupt)
                with faults.active(), self.assertRaises(type(interrupt)):
                    search_log.summarize_csv(faults.target, AGENCY)
                self.assertTrue(faults.descriptors_closed())
        self.assertEqual(csv.field_size_limit(), limit)
        # a category failure keeps its own diagnostic and the CSV is never admitted
        path, opened = self.csv(), []
        with mock.patch.object(os, "open", lambda p, *a, **k: opened.append(os.fspath(p)) or REAL_OPEN(p, *a, **k)):
            code, out, err = self.run_cli("--csv", path, "--agency", base.AID, "--categories", self.files / "nope.json")
        self.assertEqual((code, out, err), (1, "", "Stopped: --categories file cannot be opened as a regular file (symlinks are refused)\n"))
        self.assertNotIn(str(path), opened)

    def test_valid_summary_text_json_and_replay_remain_identical(self):
        rows = ["2026-01-05,Robbery,Alpha PD,CA", "2026-01-06,investigation,Alpha PD,CA", "2026-02-01,,Beta SO,",
                "2026-02-02," + "x" * 200000 + ",,"]
        data = base.log_bytes(rows, header="Timestamp,Purpose,Organization,State")  # BOM, CRLF, long purpose field
        path = self.csv(data)
        with self.offline():
            summary = search_log.summarize_csv(path, AGENCY)
        self.assertEqual(summary["input"]["sha256"], hashlib.sha256(data).hexdigest())  # the BOM is hashed
        self.assertEqual(summary["rows"]["read"], 4)
        self.assertEqual(summary["rows"]["counted"], 4)
        self.assertEqual(summary["purposes"]["counts"], {"blank_or_na": 1, "immigration_related": 0, "vague": 1, "other": 2})
        self.assertEqual(summary["months"]["rows"], [{"month": "2026-01", "count": 2}, {"month": "2026-02", "count": 2}])
        self.assertEqual(summary["organizations"]["unattributed"], 1)
        self.assertEqual(summary["organizations"]["rows"], [{"name": "Alpha PD", "state": "CA", "count": 2},
                                                           {"name": "Beta SO", "state": None, "count": 1}])
        self.assertEqual(list(summary), ["schema_version", "agency_id", "agency_name", "input", "period", "rows", "months",
                                         "organizations", "purposes"])
        code, out, err = self.run_cli("--csv", path, "--agency", base.AID, "--json")
        self.assertEqual((code, err), (0, ""))
        expected = {**summary, "agency_name": "Synthetic Police Department"}
        self.assertEqual(json.loads(out), expected)
        self.assertEqual(out, json.dumps(expected, ensure_ascii=True, indent=2) + "\n")
        code, out, err = self.run_cli("--csv", path, "--agency", base.AID)
        self.assertEqual((code, err, out), (0, "", search_log.format_summary(expected)))
        header_only = search_log.summarize_csv(self.csv(base.log_bytes([], header="Timestamp,Purpose")), AGENCY)
        self.assertEqual((header_only["rows"]["read"], header_only["period"]["first_date"]), (0, None))


class FreshReplayTests(unittest.TestCase):
    # the finding suite's fixture: campaign, ingested stored CSV, register and a matching summary file
    setUp = finding_base.FindingSearchLogTests.setUp
    cli = finding_base.FindingSearchLogTests.cli
    ingest = finding_base.FindingSearchLogTests.ingest
    register = finding_base.FindingSearchLogTests.register
    make_summary = finding_base.FindingSearchLogTests.make_summary
    write_summary = finding_base.FindingSearchLogTests.write_summary

    def test_fresh_finding_replay_csv_fault_never_writes_draft(self):
        stored = finding_search_log._verified_object(self.root, finding_base.AID, self.sha)
        self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), self.sha)
        argv = ["campaign_tool", "finding", "new", "--directory", str(self.root), "--agency", finding_base.AID,
                "--search-log", str(self.summary_path)]
        before = snapshot(self.base)
        for stage in ("fstat", "final_fstat", "exit"):
            with self.subTest(stage=stage), Faults(stored, stage, errors()[0]).active() as faults, contextlib.ExitStack() as stack:
                for name in ("_body", "_skeleton", "_write_draft"):  # composition and writing are never reached
                    stack.enter_context(mock.patch.object(finding_search_log, name, side_effect=AssertionError(name)))
                with self.assertRaises(ValueError) as caught:
                    finding_search_log.draft_from_summary(self.root, finding_base.AID, self.summary_path)
                assert_fixed(self, caught)
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as exit_:
                        cli.main()
                self.assertNotEqual(exit_.exception.code, 0)
                self.assertEqual((out.getvalue(), err.getvalue()), ("", f"Stopped: {FIXED}\n"))
            self.assertTrue(faults.descriptors_closed())
            self.assertFalse(self.drafts.exists())
            self.assertEqual(snapshot(self.base), before)


if __name__ == "__main__":
    unittest.main()
