"""RF0111-2: the decisions-elsewhere reader admits the descriptor it actually opened (type, identity, link count)
before any stream or payload read. Offline and synthetic only: temp copies of examples/fictional-campaign.

A deterministic seam runs one filesystem action immediately before the Nth real ``os.open`` of the target, after the
production pathname stat, then forwards to the genuine ``os.open``.
"""
import contextlib
import errno
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import cli, elsewhere_cmd
from campaign_tool.content import MAX_JSON_BYTES
from campaign_tool.elsewhere_cmd import ElsewhereError, add_entry, list_entries, remove_entry

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 11)
SINGLE = "content/elsewhere.json: must be a regular file with a single link; nothing was written"
CHANGED = "content/elsewhere.json: changed while it was being read; nothing was written"
SENTINEL = "ZQ-SUBSTITUTE-PAYLOAD-91"


def row(n, **extra):
    return {"place": f"Springfield {n}", "state": "OR", "body": "Springfield City Council", "decided_on": "2026-03-10",
            "kind": "paused", "as_stated": f"The council voted to pause contract {n}.",
            "source_url": f"https://records.example.test/minutes/{n}", "checked_on": "2026-09-15", **extra}


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-elsewhere-admit-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.path = self.content / "elsewhere.json"
        self.path.unlink(missing_ok=True)
        self.alias = self.base / "alias.json"
        self.addCleanup(mock.patch.stopall)

    def put(self, rows, mode=0o600):
        self.path.write_text(json.dumps(rows), encoding="utf-8")
        os.chmod(self.path, mode)

    def snapshot(self):
        return {str(p.relative_to(self.base)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in sorted(self.base.rglob("*")) if p.is_file()}

    def seam(self, action, *, name="elsewhere.json", nth=1):
        """Run ``action`` once, right before the ``nth`` real open of ``name``; record every fd opened for it."""
        real, state = os.open, {"opens": [], "calls": 0}

        def opening(path, flags, *args, **kwargs):
            if path == name and kwargs.get("dir_fd") is not None and flags & os.O_NONBLOCK:
                state["calls"] += 1
                if state["calls"] == nth:
                    action()
            fd = real(path, flags, *args, **kwargs)
            if path == name and kwargs.get("dir_fd") is not None and flags & os.O_NONBLOCK:
                state["opens"].append(fd)
            return fd

        mock.patch.object(elsewhere_cmd.os, "open", opening).start()
        return state

    def spy(self):
        """Count payload-stream constructions and parses, forwarding to the real ones."""
        real = os.fdopen
        streams, parses = [], []

        def fdopen(fd, mode="r", *args, **kwargs):
            if mode == "rb":
                streams.append(os.fstat(fd).st_ino)  # which inode a payload stream was built over
            return real(fd, mode, *args, **kwargs)

        real_parse = elsewhere_cmd._parse

        def parse(data, label):
            parses.append(label)
            return real_parse(data, label)

        mock.patch.object(elsewhere_cmd.os, "fdopen", fdopen).start()
        mock.patch.object(elsewhere_cmd, "_parse", parse).start()
        return streams, parses

    def link(self):
        os.link(self.path, self.alias)

    def swap(self, payload):
        """Rename the target aside and put a different regular inode (carrying ``payload``) in its place."""
        os.rename(self.path, self.base / "moved-aside.json")
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def assert_closed(self, fds):
        self.assertTrue(fds)
        for fd in fds:
            with self.assertRaises(OSError) as ctx:
                os.fstat(fd)
            self.assertEqual(ctx.exception.errno, errno.EBADF)

    def no_temp(self):
        self.assertEqual([p.name for p in self.content.iterdir() if p.name.endswith(".tmp")], [])

    def verbs(self):
        return {"list": lambda: list_entries(self.root, today=TODAY),
                "add": lambda: add_entry(self.root, **row(9), today=TODAY),
                "remove": lambda: remove_entry(self.root, 1, today=TODAY)}


class LinkArrival(Base):
    def test_same_inode_hardlink_arrival_refuses_before_stream_for_all_verbs(self):
        cases = (("list", [row(1)]), ("add", [row(1)]), ("remove", [row(1), row(2)]), ("remove", [row(1)]))
        for verb, rows in cases:
            with self.subTest(verb=verb, rows=len(rows)):
                self.alias.unlink(missing_ok=True)
                self.put(rows)
                before = self.snapshot()
                streams, parses = self.spy()
                state = self.seam(self.link)
                with self.assertRaises(ElsewhereError) as ctx:
                    self.verbs()[verb]()
                self.assertEqual(str(ctx.exception), SINGLE)
                self.assertEqual(os.stat(self.path).st_nlink, 2)
                self.assertEqual((streams, parses, state["calls"]), ([], [], 1))
                self.assert_closed(state["opens"])
                self.no_temp()
                after = self.snapshot()
                self.assertEqual(after.pop("alias.json"), before["campaign/content/elsewhere.json"])
                self.assertEqual(after, before)
                mock.patch.stopall()

    def test_real_cli_link_arrival_has_one_private_stopped_line(self):
        for verb, extra in (("list", []), ("remove", ["--index", "1"]),
                            ("add", [f"--{k.replace('_', '-')}={v}" for k, v in row(9).items()])):
            with self.subTest(verb=verb):
                self.alias.unlink(missing_ok=True)
                self.put([row(1), row(2)])
                self.seam(self.link)
                out, err = io.StringIO(), io.StringIO()
                argv = ["campaign_tool", "content", "elsewhere", verb, "--directory", str(self.root), *extra]
                with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
                    cli.main()
                self.assertEqual(ctx.exception.code, 1)
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(err.getvalue(), "Stopped: " + SINGLE + "\n")
                self.no_temp()
                mock.patch.stopall()

    def test_precommit_link_arrival_refuses_and_cleans_only_owned_temp(self):
        for verb, rows in (("add", [row(1)]), ("remove", [row(1), row(2)]), ("remove", [row(1)])):
            with self.subTest(verb=verb, rows=len(rows)):
                self.alias.unlink(missing_ok=True)
                self.put(rows)
                stray = self.content / ".elsewhere-other.tmp"
                stray.write_bytes(b"not ours")
                before = self.snapshot()
                streams, _ = self.spy()
                state = self.seam(self.link, nth=2)  # open 1 admits the store; open 2 is the precommit recheck
                with self.assertRaises(ElsewhereError) as ctx:
                    self.verbs()[verb]()
                self.assertEqual(str(ctx.exception), SINGLE)
                # only the admitted store (open 1) was read; the newly linked descriptor never is
                self.assertEqual((streams.count(self.path.stat().st_ino), state["calls"]), (1, 2))
                self.assert_closed(state["opens"])
                after = self.snapshot()
                self.assertEqual(after.pop("alias.json"), before["campaign/content/elsewhere.json"])
                self.assertEqual(after, before)  # target and both names kept; the unrelated temp-named file untouched
                self.assertEqual([p.name for p in self.content.iterdir() if p.name.endswith(".tmp")],
                                 [".elsewhere-other.tmp"])
                stray.unlink()
                mock.patch.stopall()

    def test_link_policy_remains_conditional_for_staged_meetings(self):
        meetings = self.content / "meetings"
        folder = os.open(meetings, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, folder)
        name = "2026-10-cedar-council.json"
        os.link(meetings / name, self.base / "meeting-alias.json")  # static extra link
        loaded = elsewhere_cmd._read(folder, name, "m", single_link=False)
        self.assertEqual(loaded[1][4], 2)
        self.assertEqual(loaded[0], (meetings / name).read_bytes())
        with self.assertRaisesRegex(ElsewhereError, "single link"):
            elsewhere_cmd._read(folder, name, "m", single_link=True)
        os.unlink(self.base / "meeting-alias.json")
        # a link arriving after the pathname stat is admitted only when the policy is off
        state = self.seam(lambda: os.link(meetings / name, self.base / "late.json"), name=name)
        self.assertEqual(elsewhere_cmd._read(folder, name, "m")[1][4], 2)
        os.unlink(self.base / "late.json")
        mock.patch.stopall()
        self.seam(lambda: os.link(meetings / name, self.base / "late.json"), name=name)
        with self.assertRaisesRegex(ElsewhereError, "single link"):
            elsewhere_cmd._read(folder, name, "m", single_link=True)
        self.assertEqual(state["calls"], 1)

    def test_multiply_linked_staged_meeting_still_participates_in_removal_warnings(self):
        self.put([row(1)])
        meetings = self.content / "meetings"
        document = json.loads((meetings / "2026-10-cedar-council.json").read_text(encoding="utf-8"))
        document["cited_elsewhere"] = [row(1)]
        (meetings / "2026-10-cedar-council.json").write_text(json.dumps(document), encoding="utf-8")
        os.link(meetings / "2026-10-cedar-council.json", self.base / "meeting-alias.json")
        done = remove_entry(self.root, 1, today=TODAY)
        self.assertEqual(done["citing"], ["meetings/2026-10-cedar-council.json"])
        self.assertFalse(self.path.exists())


class Substitution(Base):
    def test_different_inode_is_rejected_before_reading_its_payload(self):
        self.put([row(1)])
        streams, parses = self.spy()
        state = self.seam(lambda: self.swap([row(2, as_stated=SENTINEL)]))
        for call in (self.verbs()["list"],):
            with self.assertRaises(ElsewhereError) as ctx:
                call()
        self.assertEqual(str(ctx.exception), CHANGED)
        self.assertEqual((streams, parses, state["calls"]), ([], [], 1))  # no read, no parse, no reopen
        self.assert_closed(state["opens"])
        self.assertIn(SENTINEL, self.path.read_text(encoding="utf-8"))  # the substituted file is left as injected

    def test_different_inode_rejected_for_staged_meeting_read(self):
        meetings = self.content / "meetings"
        folder = os.open(meetings, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, folder)
        name = "2026-10-cedar-council.json"

        def replace():
            os.rename(meetings / name, self.base / "aside.json")
            (meetings / name).write_text(SENTINEL, encoding="utf-8")

        streams, _ = self.spy()
        state = self.seam(replace, name=name)
        with self.assertRaisesRegex(ElsewhereError, "changed while it was being read"):
            elsewhere_cmd._read(folder, name, "content/meetings/x")
        self.assertEqual((streams, state["calls"]), ([], 1))
        self.assert_closed(state["opens"])

    def test_substituted_fifo_directory_and_symlink_fail_without_payload_read(self):
        outside = self.base / "outside.json"
        outside.write_text(json.dumps([row(3)]), encoding="utf-8")
        makers = {"fifo": (lambda: os.mkfifo(self.path), CHANGED),
                  "directory": (lambda: self.path.mkdir(), CHANGED),
                  "symlink": (lambda: self.path.symlink_to(outside), "content/elsewhere.json: cannot be read; nothing was written"),
                  "dangling": (lambda: self.path.symlink_to(self.base / "gone"), "content/elsewhere.json: cannot be read; nothing was written")}
        for kind, (make, message) in makers.items():
            with self.subTest(kind=kind):
                self.put([row(1)])

                def replace():
                    self.path.unlink()
                    make()

                streams, parses = self.spy()
                state = self.seam(replace)
                with self.assertRaises(ElsewhereError) as ctx:
                    list_entries(self.root, today=TODAY)
                self.assertEqual(str(ctx.exception), message)
                self.assertEqual((streams, parses), ([], []))
                mock.patch.stopall()
                if kind in ("fifo", "directory"):  # a symlink refusal fails at open, so no descriptor exists
                    self.assert_closed(state["opens"])
                else:
                    self.assertEqual(state["opens"], [])
                self.assertEqual(json.loads(outside.read_text(encoding="utf-8")), [row(3)])
                if self.path.is_dir() and not self.path.is_symlink():
                    self.path.rmdir()
                else:
                    self.path.unlink()

    def test_static_path_controls_keep_their_original_categories(self):
        self.assertIsNone(list_entries(self.root, today=TODAY))  # missing stays absent
        os.mkfifo(self.path)
        with self.assertRaisesRegex(ElsewhereError, "must be a regular file with a single link"):
            list_entries(self.root, today=TODAY)
        self.path.unlink()
        self.path.symlink_to(self.base / "gone")
        with self.assertRaisesRegex(ElsewhereError, "is a symbolic link"):
            list_entries(self.root, today=TODAY)


class Lifecycle(Base):
    def test_pre_stream_refusals_and_fstat_failures_close_raw_fd_once(self):
        self.put([row(1)])
        sentinel = os.open(self.base / "sentinel", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(lambda: os.path.exists("/proc/self/fd/%d" % sentinel) and os.close(sentinel))
        fds_before = len(os.listdir("/proc/self/fd"))
        for _ in range(5):
            state = self.seam(self.link)
            with self.assertRaises(ElsewhereError):
                list_entries(self.root, today=TODAY)
            self.assert_closed(state["opens"])
            self.alias.unlink()
            mock.patch.stopall()
        self.assertEqual(len(os.listdir("/proc/self/fd")), fds_before)
        os.fstat(sentinel)  # an independently owned descriptor is untouched
        # an fstat OSError is the existing cannot-read category; any other exception propagates after cleanup
        for exc, expected in ((OSError(errno.EIO, "synthetic"), ElsewhereError), (RuntimeError("synthetic"), RuntimeError)):
            state = self.seam(lambda: None)
            real_fstat = os.fstat

            def failing(fd, _real=real_fstat, _exc=exc, _state=state):
                if fd in _state["opens"]:
                    raise _exc
                return _real(fd)

            mock.patch.object(elsewhere_cmd.os, "fstat", failing).start()
            with self.assertRaises(expected) as ctx:
                list_entries(self.root, today=TODAY)
            if expected is ElsewhereError:
                self.assertEqual(str(ctx.exception), "content/elsewhere.json: cannot be read; nothing was written")
            mock.patch.stopall()
            self.assert_closed(state["opens"])
        os.fstat(sentinel)

    def test_admitted_read_failure_closes_fd(self):
        self.put([row(1)])
        state = self.seam(lambda: None)
        real = os.fdopen

        def fdopen(fd, mode="r", *args, **kwargs):
            if mode == "rb":
                raise OSError(errno.EMFILE, "synthetic")
            return real(fd, mode, *args, **kwargs)

        mock.patch.object(elsewhere_cmd.os, "fdopen", fdopen).start()
        with self.assertRaisesRegex(ElsewhereError, "cannot be read"):
            list_entries(self.root, today=TODAY)
        self.assert_closed(state["opens"])


class Unchanged(Base):
    def test_admitted_file_budget_and_ordinary_outputs_stay_unchanged(self):
        folder = os.open(self.content, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, folder)
        target = self.content / "scratch.json"
        # exactly the cap, built from multibyte characters, passes through byte for byte
        unit = "é".encode("utf-8")
        data = unit * (MAX_JSON_BYTES // len(unit)) + b"x" * (MAX_JSON_BYTES % len(unit))
        self.assertEqual(len(data), MAX_JSON_BYTES)
        target.write_bytes(data)
        loaded = elsewhere_cmd._read(folder, "scratch.json", "s", single_link=True)
        self.assertEqual((loaded[0], loaded[1][2]), (data, MAX_JSON_BYTES))
        # one byte over: refused from the pathname size before any open
        target.write_bytes(data + b"x")
        opened = self.seam(lambda: None, name="scratch.json")
        with self.assertRaisesRegex(ElsewhereError, "file too large"):
            elsewhere_cmd._read(folder, "scratch.json", "s")
        self.assertEqual(opened["calls"], 0)
        # growth after the pathname stat is caught by the actual read length, never parsed
        target.write_bytes(data)
        mock.patch.stopall()

        def grow():
            with open(target, "ab") as handle:
                handle.write(b"x")

        state = self.seam(grow, name="scratch.json")
        with self.assertRaisesRegex(ElsewhereError, "file too large"):
            elsewhere_cmd._read(folder, "scratch.json", "s")
        self.assert_closed(state["opens"])
        mock.patch.stopall()
        # missing stays None; ordinary add/list/remove keep their bytes and modes
        self.assertIsNone(elsewhere_cmd._read(folder, "absent.json", "a"))
        self.assertEqual(add_entry(self.root, **row(1), today=TODAY)["index"], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        os.chmod(self.path, 0o600)
        self.assertEqual(add_entry(self.root, **row(2), today=TODAY)["index"], 2)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.read_bytes(),
                         (json.dumps([row(1), row(2)], indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        self.assertEqual([r["index"] for r in list_entries(self.root, today=TODAY)], [1, 2])
        self.assertEqual(remove_entry(self.root, 2, today=TODAY)["last"], False)
        self.assertEqual(remove_entry(self.root, 1, today=TODAY)["last"], True)
        self.assertFalse(self.path.exists())
        self.assertTrue(stat.S_ISREG(target.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
