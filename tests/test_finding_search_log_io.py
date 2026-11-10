"""RF0725-1: finding-summary I/O failures stay value-free. Synthetic campaign, CSV and names only; offline.

Run from the repository root: python3 -B -m unittest tests.test_finding_search_log_io
"""
import contextlib
import errno
import io
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, finding_search_log, findings, search_log  # noqa: E402
from tests import test_finding_search_log as base  # noqa: E402  (module alias: its TestCase is not re-collected here)

FIXED = "--search-log file could not be read"
CANARY = "PRIVATE-SUMMARY-CANARY"
REAL_FSTAT, REAL_FDOPEN = os.fstat, os.fdopen


class Faults:
    """Faults injected at the low-level seam of the summary file, after the real os.open of that very path."""

    def __init__(self, summary_path, stage, error):
        self.target, self.stage, self.error = os.fspath(summary_path), stage, error
        self.fds, self.reads, self.handles = [], [], []

    def raise_error(self):
        raise self.error

    def open_(self, path, flags, *args, **kwargs):
        fd = self.real_open(path, flags, *args, **kwargs)
        if os.fspath(path) == self.target:
            self.fds.append(fd)
        return fd

    def fstat(self, fd):
        if self.stage == "fstat" and fd in self.fds:
            self.raise_error()
        return REAL_FSTAT(fd)

    def fdopen(self, fd, *args, **kwargs):
        if self.stage == "fdopen" and fd in self.fds:
            self.raise_error()
        handle = REAL_FDOPEN(fd, *args, **kwargs)
        if fd in self.fds:
            handle = FaultyHandle(handle, self)
            self.handles.append(handle)
        return handle

    def closed(self):
        for fd in self.fds:
            try:
                REAL_FSTAT(fd)
            except OSError:
                continue
            return False
        return all(handle.inner.closed for handle in self.handles)

    @contextlib.contextmanager
    def active(self):
        self.real_open = os.open
        with mock.patch.object(os, "open", self.open_), mock.patch.object(os, "fstat", self.fstat), \
                mock.patch.object(os, "fdopen", self.fdopen):
            yield self


class FaultyHandle:
    def __init__(self, inner, faults):
        self.inner, self.faults = inner, faults

    def __enter__(self):
        return self

    def read(self, size=-1):
        self.faults.reads.append(size)
        if self.faults.stage == "read":
            self.faults.raise_error()
        return self.inner.read(size)

    def __exit__(self, *exc_info):
        self.inner.close()  # the real close happens first, as it would before a late close error
        if self.faults.stage == "close":
            self.faults.raise_error()
        return False


def errors():
    return (OSError(errno.EIO, f"{CANARY}-OS-MESSAGE", f"/synthetic/{CANARY}.json", None, f"/synthetic/{CANARY}-2"),
            PermissionError(errno.EACCES, f"{CANARY}-DENIED", f"/synthetic/{CANARY}.json"),
            FileNotFoundError(errno.ENOENT, f"{CANARY}-GONE"),
            OSError(f"{CANARY} message only"),
            TimeoutError(errno.ETIMEDOUT, f"{CANARY}-TIMEOUT"))


STAGES = ("fstat", "fdopen", "read", "close")


class SummaryIoTests(unittest.TestCase):
    # fixtures from the existing module, borrowed as plain functions (no subclassing of its TestCase)
    setUp = base.FindingSearchLogTests.setUp
    cli = base.FindingSearchLogTests.cli
    ingest = base.FindingSearchLogTests.ingest
    register = base.FindingSearchLogTests.register
    make_summary = base.FindingSearchLogTests.make_summary
    write_summary = base.FindingSearchLogTests.write_summary
    summary = base.FindingSearchLogTests.summary
    draft = base.FindingSearchLogTests.draft

    def tree(self):
        """Content, mode and mtime of every path under the campaign, excluding the summary's own atime."""
        state = {}
        for path in sorted([self.root, *self.root.rglob("*")]):
            info = path.lstat()
            data = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            state[str(path.relative_to(self.base))] = (stat.S_IMODE(info.st_mode), info.st_mtime_ns, data)
        return state

    def run_cli(self):
        argv = ["campaign_tool", "finding", "new", "--directory", str(self.root), "--agency", base.AID,
                "--search-log", str(self.summary_path)]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                cli.main()
        return caught.exception.code, out.getvalue(), err.getvalue()

    def downstream(self):
        """Patches proving nothing after the reader ran: verification, replay, composition and writing."""
        return [mock.patch.object(finding_search_log, name, side_effect=AssertionError(name))
                for name in ("_structure", "_verified_object", "_body", "_skeleton", "_write_draft")] + [
                    mock.patch.object(search_log, "summarize_csv", side_effect=AssertionError("summarize_csv"))]

    @contextlib.contextmanager
    def no_downstream(self):
        with contextlib.ExitStack() as stack:
            for patch in self.downstream():
                stack.enter_context(patch)
            yield

    def assert_fixed(self, caught):
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(str(caught.exception), FIXED)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertIsNone(caught.exception.__cause__)

    # ---- reader / composer --------------------------------------------------------------------------------------

    def test_summary_admission_fstat_and_fdopen_errors_are_fixed_and_close_fd(self):
        for stage in ("fstat", "fdopen"):
            for error in errors():
                with self.subTest(stage=stage, error=repr(error)), Faults(self.summary_path, stage, error).active() as faults:
                    with self.no_downstream(), self.assertRaises(ValueError) as caught:
                        self.draft()
                    self.assert_fixed(caught)
                    self.assertEqual(len(faults.fds), 1)
                    self.assertTrue(faults.closed(), "the descriptor from the real os.open was left open")
                    with self.assertRaises(ValueError) as direct:
                        finding_search_log._load_summary(self.summary_path)
                    self.assert_fixed(direct)
                self.assertFalse(self.drafts.exists())

    def test_summary_read_and_context_exit_errors_are_fixed(self):
        for stage in ("read", "close"):
            for error in errors():
                with self.subTest(stage=stage, error=repr(error)), Faults(self.summary_path, stage, error).active() as faults:
                    with self.no_downstream(), self.assertRaises(ValueError) as caught:
                        self.draft()
                    self.assert_fixed(caught)
                    self.assertTrue(faults.closed())
                    self.assertEqual(faults.reads, [finding_search_log.MAX_SUMMARY_BYTES + 1])
                self.assertFalse(self.drafts.exists())

    def test_no_replay_or_writer_after_close_failure(self):
        # a fully valid summary is read completely, yet a context-exit failure refuses before replay and drafting
        self.assertEqual(self.summary()["agency_id"], base.AID)
        with Faults(self.summary_path, "close", OSError(errno.EIO, f"{CANARY}-CLOSE")).active() as faults:
            with self.no_downstream(), self.assertRaises(ValueError) as caught:
                self.draft()
        self.assert_fixed(caught)
        self.assertEqual(len(faults.reads), 1)
        self.assertFalse(self.drafts.exists())

    # ---- actual CLI -----------------------------------------------------------------------------------------------

    def test_actual_cli_summary_io_refusals_have_no_values_or_success(self):
        for stage in STAGES:
            with self.subTest(stage=stage), Faults(self.summary_path, stage,
                                                   OSError(errno.EIO, f"{CANARY}-OS-MESSAGE", f"/synthetic/{CANARY}.json")).active():
                with self.no_downstream():
                    code, out, err = self.run_cli()
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertEqual(err, f"Stopped: {FIXED}\n")
            for text in (CANARY, str(self.summary_path), self.summary_path.name, str(self.root), "Traceback", "Fix:",
                         "Nothing was published"):
                self.assertNotIn(text, out + err)
            self.assertFalse(self.drafts.exists())

    # ---- preservation -------------------------------------------------------------------------------------------

    def test_refusal_preserves_absent_and_existing_draft_outputs(self):
        for populated in (False, True):
            if populated:
                self.drafts.mkdir(mode=0o750)
                (self.drafts / "keep.json").write_text("{}")
                os.chmod(self.drafts / "keep.json", 0o640)
                os.chmod(self.drafts, 0o750)
            before = self.tree()
            for stage in STAGES:
                with self.subTest(populated=populated, stage=stage), \
                        Faults(self.summary_path, stage, OSError(errno.EIO, CANARY)).active():
                    with self.no_downstream(), mock.patch.object(os, "mkdir", side_effect=AssertionError("mkdir")), \
                            mock.patch.object(os, "chmod", side_effect=AssertionError("chmod")), \
                            mock.patch("socket.socket", side_effect=AssertionError("network")):
                        with self.assertRaises(ValueError):
                            self.draft()
                        code, _, _ = self.run_cli()
                    self.assertEqual(code, 2)
                    self.assertEqual(self.tree(), before)
            self.assertEqual(self.drafts.exists(), populated)

    # ---- existing behaviour kept ------------------------------------------------------------------------------------

    def test_existing_admission_parse_and_type_refusals_are_not_normalized(self):
        missing = self.base / "absent.json"
        with self.assertRaisesRegex(ValueError, "cannot be opened as a regular file"):
            self.draft(missing)
        link = self.base / "link.json"
        link.symlink_to(self.summary_path)
        with self.assertRaisesRegex(ValueError, "cannot be opened as a regular file"):
            self.draft(link)
        with self.assertRaisesRegex(ValueError, "must be a regular file"):
            self.draft(self.base)
        fifo = self.base / "fifo.json"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "must be a regular file"):
            self.draft(fifo)
        big = self.base / "big.json"
        big.write_bytes(b" " * (finding_search_log.MAX_SUMMARY_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "is larger than 4 MiB"):
            self.draft(big)
        for name, data, pattern in (
                ("utf8.json", b"\xff\xfe", "not valid UTF-8 JSON"),
                ("syntax.json", b"{", "not valid UTF-8 JSON"),
                ("dup.json", b'{"a": 1, "a": 2}', "not valid UTF-8 JSON"),
                ("nan.json", b'{"a": NaN}', "not valid UTF-8 JSON"),
                ("deep.json", b"[" * 100000 + b"]" * 100000, "not valid UTF-8 JSON"),
                ("list.json", b"[]", "must hold one kit-search-log --json object")):
            path = self.base / name
            path.write_bytes(data)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, pattern):
                self.draft(path)
        wrong = dict(self.summary(), schema_version=True)
        with self.assertRaisesRegex(ValueError, "not a version 1"):
            self.draft(self.write_summary(wrong, "typed.json"))
        self.assertFalse(self.drafts.exists())

    def test_actual_length_guard_still_refuses_a_file_that_grew_after_fstat(self):
        class Growing:
            def __enter__(self):
                return self

            def read(self, size=-1):
                return b" " * size

            def __exit__(self, *exc_info):
                return False

        with mock.patch.object(search_log, "_open_regular", return_value=(Growing(), None)):
            with self.assertRaisesRegex(ValueError, "is larger than 4 MiB"):
                finding_search_log._load_summary(self.summary_path)

    def test_process_control_and_programming_errors_propagate(self):
        for error in (KeyboardInterrupt(), RuntimeError("programming"), AttributeError("programming")):
            with self.subTest(error=type(error).__name__), Faults(self.summary_path, "read", error).active():
                with self.assertRaises(type(error)):
                    finding_search_log._load_summary(self.summary_path)

    def test_valid_summary_and_normal_draft_outputs_remain_compatible(self):
        result = self.draft()
        path = Path(result["written"])
        draft = findings.load_draft(path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(draft["sources"][0]["sha256"], self.sha)
        self.assertEqual(draft["agency_id"], base.AID)
        for line in ("| Total searches counted | 20 |", "| Rows read | 23 |", "| Org01 | CA | 5 |"):
            self.assertIn(line, draft["body_md"])
        # a reordered, escaped, differently-labelled but equivalent summary still drafts the same body
        data = self.summary()
        data["agency_name"] = "Renamed Display Agency"
        reordered = dict(reversed(list(data.items())))
        second = self.draft(self.write_summary(reordered, "reordered.json"))
        self.assertEqual(findings.load_draft(Path(second["written"]))["body_md"], draft["body_md"])
        # the fault-free CLI control succeeds
        out = self.cli("finding", "new", "--directory", self.root, "--agency", base.AID, "--search-log",
                       self.summary_path).stdout.splitlines()
        self.assertEqual(out[1], f"cited: Search log {self.sha[:12]}")
        self.assertIn("Nothing was published.", out[2])
        self.assertTrue(Path(json.loads(out[0])["written"]).is_file())


if __name__ == "__main__":
    unittest.main()
