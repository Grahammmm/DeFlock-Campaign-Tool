"""RF1721-3: removing the last decisions-elsewhere entry flushes the owning content/ descriptor after the unlink, and a
flush failure is reported as removed-but-unconfirmed (never as a kept file, never as success).

Offline and synthetic only: temp copies of examples/fictional-campaign; sockets are blocked; nothing is fetched or sent.
"""
import contextlib
import errno
import io
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import cli, elsewhere_cmd
from campaign_tool.elsewhere_cmd import ElsewhereError, add_entry, list_entries, remove_entry

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)
SENTINEL = "ZQ-PRIVATE-9f2.json"
REMOVED_FAILED = ("content/elsewhere.json: removed, but the folder could not be flushed to disk; "
                  "check it with content elsewhere list")
KEPT = "content/elsewhere.json: could not be removed; the previous file was kept"
CHANGED = "content/elsewhere.json: changed while it was being edited; nothing was written"
CHANGED_FOLDER = "content/: changed while it was being edited; nothing was written"
HEALTHY_OUT = ("Elsewhere entry removed: index 1.\n"
               "No staged meetings cite the same place, body and date.\n"
               "content/elsewhere.json removed; no decisions elsewhere remain.\n"
               "Later stored positions shift down by one; run content elsewhere list before choosing --elsewhere again.\n"
               "Content only; nothing was fetched, built, sent or deployed.\n")


def row(n):
    return {"place": f"Springfield {n}", "state": "OR", "body": "Springfield City Council", "decided_on": "2026-03-10",
            "kind": "paused", "as_stated": f"The council voted to pause contract {n}.",
            "source_url": f"https://records.example.test/minutes/{n}", "checked_on": "2026-09-15"}


class Recorder:
    """Forward the real unlink/fsync/close/open and log them; optionally fail or hook only the directory fsync."""

    def __init__(self, fsync_error=None, on_fsync=None):
        self.events, self.dirs, self.fsync_error, self.on_fsync = [], [], fsync_error, on_fsync
        self.real = {name: getattr(os, name) for name in ("unlink", "fsync", "close", "open")}

    def is_dir(self, fd):
        return stat.S_ISDIR(os.fstat(fd).st_mode)

    def unlink(self, path, *args, **kwargs):
        if kwargs.get("dir_fd") is not None and path == "elsewhere.json":
            self.events.append(("unlink", path, kwargs["dir_fd"]))
        return self.real["unlink"](path, *args, **kwargs)

    def fsync(self, fd):
        kind = "dir" if self.is_dir(fd) else "file"
        self.events.append(("fsync", kind, fd))
        if kind == "dir":
            if self.on_fsync:
                self.on_fsync(fd)
            if self.fsync_error is not None:
                raise self.fsync_error
        return self.real["fsync"](fd)

    def open(self, path, flags, *args, **kwargs):
        fd = self.real["open"](path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            self.dirs.append(fd)
        return fd

    def close(self, fd):
        if fd in self.dirs:
            self.dirs.remove(fd)
            self.events.append(("close", "dir", fd))
        return self.real["close"](fd)

    def closes_after_unlink(self):
        """How many times the descriptor used for the unlink was closed after that unlink."""
        unlinks = [i for i, event in enumerate(self.events) if event[0] == "unlink"]
        fd = self.events[unlinks[0]][2]
        return len([e for e in self.events[unlinks[0]:] if e[0] == "close" and e[2] == fd])

    def kinds(self):
        return [event[:2] for event in self.events if event[0] != "close"]

    def dir_fsyncs(self):
        return [event for event in self.events if event[:2] == ("fsync", "dir")]

    @contextlib.contextmanager
    def patched(self):
        with mock.patch.object(os, "unlink", self.unlink), mock.patch.object(os, "fsync", self.fsync), \
                mock.patch.object(os, "close", self.close), mock.patch.object(os, "open", self.open):
            yield self


def open_fds():
    return {int(n) for n in os.listdir("/proc/self/fd")}


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-elsewhere-durable-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.path = self.content / "elsewhere.json"
        self.path.unlink(missing_ok=True)
        self.put([row(1)])
        guard = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network used"))
        guard.start()
        self.addCleanup(guard.stop)

    def put(self, rows):
        self.path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.path, 0o640)

    def others(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in sorted(self.root.rglob("*")) if p.is_file() and p != self.path}

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        args = ["campaign_tool", "content", "elsewhere", argv[0], "--directory", str(self.root), *argv[1:]]
        code = 0
        with mock.patch.object(sys, "argv", args), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()


class FinalRemovalFlush(Base):
    def test_last_remove_unlinks_then_fsyncs_same_admitted_directory(self):
        with Recorder().patched() as rec:
            done = remove_entry(self.root, 1, today=TODAY)
        self.assertEqual(done, {"index": 1, "citing": [], "last": True})
        self.assertFalse(self.path.exists())
        self.assertEqual(rec.kinds(), [("unlink", "elsewhere.json"), ("fsync", "dir")])
        unlink, flush = rec.events[0], rec.dir_fsyncs()[0]
        self.assertEqual(unlink[2], flush[2])  # the very descriptor used for the unlink
        self.assertEqual(rec.closes_after_unlink(), 1)
        self.assertEqual(rec.events[-1], ("close", "dir", flush[2]))  # closed only after the flush
        # healthy CLI bytes are unchanged
        self.put([row(1)])
        with Recorder().patched() as rec:
            self.assertEqual(self.run_cli("remove", "--index", "1"), (0, HEALTHY_OUT, ""))
        self.assertEqual(len(rec.dir_fsyncs()), 1)

    def test_postcommit_fsync_oserrors_report_removed_without_rollback(self):
        errors = (OSError(errno.EIO, SENTINEL), OSError(errno.ENOSPC, SENTINEL), PermissionError(errno.EACCES, SENTINEL),
                  FileExistsError(errno.EEXIST, SENTINEL))
        for error in errors:
            with self.subTest(error=type(error).__name__, errno=error.errno):
                self.put([row(1)])
                before = self.others()
                names = sorted(os.listdir(self.content))
                with Recorder(fsync_error=error).patched() as rec:
                    with self.assertRaises(ElsewhereError) as caught:
                        remove_entry(self.root, 1, today=TODAY)
                self.assertEqual(str(caught.exception), REMOVED_FAILED)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertIsNone(caught.exception.__cause__)
                self.assertNotIn(SENTINEL, str(caught.exception))
                self.assertEqual([event[:2] for event in rec.events if event[0] == "unlink"], [("unlink", "elsewhere.json")])
                self.assertEqual(len(rec.dir_fsyncs()), 1)  # no retry
                self.assertFalse(self.path.exists())  # no rollback, no recreation, no temporary file
                self.assertEqual(sorted(os.listdir(self.content)), [n for n in names if n != "elsewhere.json"])
                self.assertEqual(self.others(), before)
                self.assertNotIn("kept", str(caught.exception))
                self.assertNotIn("nothing was written", str(caught.exception))

    def test_real_cli_postcommit_failure_has_no_success_stdout(self):
        with Recorder(fsync_error=OSError(errno.EIO, SENTINEL)).patched():
            code, out, err = self.run_cli("remove", "--index", "1")
        self.assertEqual((code, out, err), (1, "", f"Stopped: {REMOVED_FAILED}\n"))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.run_cli("list"), (0, "No decisions elsewhere recorded.\n", ""))
        with Recorder().patched() as rec:  # a second removal uses the existing absent-file refusal, with no flush
            code, out, err = self.run_cli("remove", "--index", "1")
        self.assertEqual((code, out, err), (1, "", "Stopped: content/elsewhere.json: does not exist; there is nothing to remove\n"))
        self.assertEqual(rec.kinds(), [])

    def test_precommit_failures_never_claim_committed_removal(self):
        before_bytes, before_mode = self.path.read_bytes(), self.path.stat().st_mode
        real_verify = elsewhere_cmd._verify_path

        def edit_target(root, expected):
            self.path.write_text(json.dumps([row(2)]), encoding="utf-8")
            return real_verify(root, expected)

        def swap_folder(root, expected):
            self.content.rename(self.root / "content.aside")
            (self.root / "content").mkdir()
            return real_verify(root, expected)

        def drop_folder(root, expected):
            self.content.rename(self.root / "content.aside")
            return real_verify(root, expected)

        def fail_unlink(path, *args, **kwargs):
            raise OSError(errno.EIO, SENTINEL)

        cases = (("changed-target", edit_target, None, CHANGED), ("replaced-folder", swap_folder, None, CHANGED_FOLDER),
                 ("missing-folder", drop_folder, None, CHANGED_FOLDER), ("unlink-fails", None, fail_unlink, KEPT))
        for name, verify_hook, unlink_hook, message in cases:
            with self.subTest(case=name):
                self.put([row(1)])
                before_bytes = self.path.read_bytes()
                stack = contextlib.ExitStack()
                if verify_hook:
                    stack.enter_context(mock.patch.object(elsewhere_cmd, "_verify_path", verify_hook))
                with Recorder().patched() as rec, stack:
                    if unlink_hook:
                        stack.enter_context(mock.patch.object(os, "unlink", unlink_hook))
                    with self.assertRaises(ElsewhereError) as caught:
                        remove_entry(self.root, 1, today=TODAY)
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(rec.dir_fsyncs(), [])
                self.assertNotIn("removed, but", str(caught.exception))
                self.assertNotIn(SENTINEL, str(caught.exception))
                if verify_hook is None:
                    self.assertEqual((self.path.read_bytes(), self.path.stat().st_mode), (before_bytes, before_mode))
                elif name == "changed-target":
                    self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), [row(2)])
                for aside in (self.root / "content.aside",):  # undo the folder swap for the next case
                    if aside.exists():
                        shutil.rmtree(self.content, ignore_errors=True)
                        aside.rename(self.content)

    def test_broken_staged_meeting_refuses_before_any_removal_or_flush(self):
        (self.content / "meetings").mkdir(exist_ok=True)
        (self.content / "meetings" / "broken.json").write_text("{", encoding="utf-8")
        before = self.path.read_bytes()
        with Recorder().patched() as rec:
            with self.assertRaises(ElsewhereError):
                remove_entry(self.root, 1, today=TODAY)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(rec.dir_fsyncs(), [])

    def test_retained_directory_is_flushed_not_reopened_path(self):
        seen = {}

        def swap_then_check(fd):
            seen["admitted"] = os.fstat(fd).st_ino
            self.content.rename(self.root / "content.aside")
            self.content.mkdir()
            (self.content / "elsewhere.json").write_text("decoy", encoding="utf-8")
            seen["replacement"] = self.content.stat().st_ino
            seen["fd_ino"] = os.fstat(fd).st_ino
            seen["aside"] = (self.root / "content.aside").stat().st_ino

        with Recorder(on_fsync=swap_then_check).patched() as rec:
            done = remove_entry(self.root, 1, today=TODAY)
        self.assertTrue(done["last"])
        self.assertEqual(seen["fd_ino"], seen["aside"])
        self.assertNotEqual(seen["fd_ino"], seen["replacement"])
        self.assertEqual((self.content / "elsewhere.json").read_text(encoding="utf-8"), "decoy")
        self.assertFalse((self.root / "content.aside" / "elsewhere.json").exists())
        self.assertEqual(len(rec.dir_fsyncs()), 1)

    def test_folder_descriptor_closes_on_success_and_flush_failure(self):
        probe = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, probe)
        mode = os.fstat(probe).st_mode
        for error in (None, OSError(errno.EIO, "x"), PermissionError(errno.EACCES, "x"), OSError(errno.ENOSPC, "x")):
            with self.subTest(error=error):
                self.put([row(1)])
                fds = open_fds()
                with Recorder(fsync_error=error).patched() as rec:
                    try:
                        remove_entry(self.root, 1, today=TODAY)
                    except ElsewhereError:
                        self.assertIsNotNone(error)
                self.assertEqual(rec.closes_after_unlink(), 1)
                self.assertEqual(open_fds() - fds, set())
                self.assertEqual(os.fstat(probe).st_mode, mode)
        for error in (RuntimeError("boom"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.put([row(1)])
                fds = open_fds()
                with Recorder(fsync_error=error).patched():
                    with self.assertRaises(type(error)) as caught:
                        remove_entry(self.root, 1, today=TODAY)
                self.assertIs(caught.exception, error)
                self.assertEqual(open_fds() - fds, set())


class Unchanged(Base):
    def test_nonfinal_remove_add_and_list_contracts_are_unchanged(self):
        self.put([row(1), row(2)])
        with Recorder().patched() as rec:
            done = remove_entry(self.root, 1, today=TODAY)
        self.assertEqual(done, {"index": 1, "citing": [], "last": False})
        self.assertEqual(rec.kinds(), [("fsync", "file"), ("fsync", "dir")])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), [row(2)])
        self.assertEqual([n for n in os.listdir(self.content) if n.endswith(".tmp")], [])
        with Recorder().patched() as rec:
            self.assertEqual(add_entry(self.root, **row(3), today=TODAY)["index"], 2)
        self.assertEqual(rec.kinds(), [("fsync", "file"), ("fsync", "dir")])
        with Recorder().patched() as rec:
            self.assertEqual(len(list_entries(self.root, today=TODAY) or ()), 2)
        self.assertEqual(rec.kinds(), [])
        with Recorder(fsync_error=OSError(errno.EIO, "x")).patched() as rec:  # replacement keeps its own message
            with self.assertRaisesRegex(ElsewhereError, "written, but the folder could not be flushed to disk"):
                add_entry(self.root, **row(4), today=TODAY)

    def test_final_removal_writes_no_empty_array_and_no_temp_file(self):
        before = self.others()
        with Recorder().patched() as rec:
            remove_entry(self.root, 1, today=TODAY)
        self.assertEqual(sorted(n for n in os.listdir(self.content) if n.startswith(".elsewhere")), [])
        self.assertEqual(self.others(), before)
        self.assertEqual([e[:2] for e in rec.events if e[0] == "fsync"], [("fsync", "dir")])

    def test_documented_refusal_distinguishes_precommit_and_durability_uncertainty(self):
        text = (REPO_ROOT / "docs" / "SITE-CONTENT.md").read_text(encoding="utf-8")
        self.assertIn("#### Maintaining the file: `content elsewhere add|remove|list`", text)
        start = text.index("#### Maintaining the file: `content elsewhere add|remove|list`")
        paragraph = text[start:text.index("```sh", start)]
        for needle in ("**before the change is committed**, leaves the old file byte for byte",
                       "the file was `written` (replacement) or `removed` (last entry)",
                       "could not be flushed to disk", "Run `content elsewhere list`",
                       "nothing is retried, rebuilt, restaged, fetched or published automatically"):
            self.assertIn(needle, paragraph)
        self.assertNotIn("exits 1 and leaves the old file byte for byte", paragraph)


if __name__ == "__main__":
    unittest.main()
