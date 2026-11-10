"""Synthetic real-filesystem tests of descriptor-bound action reads (not writer races)."""
import contextlib
import io
import os
import stat
import sys
import unittest
from unittest import mock

from campaign_tool import agency_actions as actions, cli
from tests.test_agency_actions import Base, document, row, TODAY

CHANGED = "content/agency-actions.json: changed while it was being read"


class PathTests(Base):
    @contextlib.contextmanager
    def parent_swap(self, boundary, kind="symlink", outside_bytes=None):
        self.put(document())
        outside = self.tmp / "outside"
        outside.mkdir()
        target = outside / "agency-actions.json"
        target.write_bytes(outside_bytes or actions._dump([row(text="SYNTHETIC-OUTSIDE-MARKER")]))
        original = target.read_bytes(), target.stat().st_mode
        real_open, real_stat = os.open, os.stat
        swapped = False
        opened = []

        def swap():
            nonlocal swapped
            swapped = True
            self.path.parent.rename(self.tmp / "admitted")
            if kind == "directory":
                outside.rename(self.path.parent)
            elif kind != "missing":
                self.path.parent.symlink_to(outside if kind == "symlink" else self.tmp / "absent",
                                            target_is_directory=True)

        def opening(path, flags, *args, **kwargs):
            if path == "content" and boundary == "before_content":
                swap()
            if path == actions.PARTS[1] and boundary == "before_leaf":
                swap()
            fd = real_open(path, flags, *args, **kwargs)
            opened.append(fd)
            info = os.fstat(fd)
            self.assertNotEqual((info.st_dev, info.st_ino),
                                (real_stat(target if kind != "directory" or not swapped else self.path).st_dev,
                                 real_stat(target if kind != "directory" or not swapped else self.path).st_ino),
                                "outside leaf was opened")
            if path == actions.PARTS[1] and boundary == "after_leaf":
                swap()
            return fd

        def inspecting(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == actions.PARTS[1] and boundary == "after_leaf_stat" and not swapped:
                swap()
            return result

        with mock.patch.object(actions, "_open", side_effect=opening), \
                mock.patch.object(actions.os, "stat", side_effect=inspecting), \
                mock.patch.object(actions, "_install", side_effect=AssertionError("writer called")), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network called")):
            yield
        self.assertTrue(swapped)
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)
        saved = self.path if kind == "directory" else target
        self.assertEqual((saved.read_bytes(), saved.stat().st_mode), original)

    def test_parent_swap_after_admission_never_reads_outside(self):
        for payload in (actions._dump([row(text="SYNTHETIC-OUTSIDE-MARKER")]), b"SYNTHETIC-PRIVATE-INVALID-JSON"):
            with self.subTest(payload=payload), self.parent_swap("before_content", outside_bytes=payload):
                with self.assertRaises(actions.ActionError) as caught:
                    actions.read_actions(self.root, self.agencies, today=TODAY)
                self.assertEqual(str(caught.exception), CHANGED)
            self.restore_parent()

    def restore_parent(self):
        if self.path.parent.is_symlink():
            self.path.parent.unlink()
        elif self.path.parent.exists():
            self.path.parent.rename(self.tmp / "outside")
        (self.tmp / "admitted").rename(self.path.parent)
        # Retain the outside sentinel, at a unique name, across subcases.
        if (self.tmp / "outside").exists():
            (self.tmp / "outside").rename(self.tmp / ("saved-" + str(len(list(self.tmp.iterdir())))))

    def test_parent_swaps_at_each_descriptor_boundary(self):
        for boundary in ("before_content", "before_leaf", "after_leaf_stat", "after_leaf"):
            for kind in ("symlink", "dangling", "directory", "missing"):
                with self.subTest(boundary=boundary, kind=kind), self.parent_swap(boundary, kind):
                    with self.assertRaises(actions.ActionError) as caught:
                        actions.read_actions(self.root, self.agencies, today=TODAY)
                    self.assertEqual(str(caught.exception), CHANGED)
                self.restore_parent()

    def test_stable_missing_empty_root_anchor_and_unrelated_entry(self):
        before = self.tree(self.root)
        self.assertIsNone(actions._load(self.root))
        self.assertEqual(actions.action_problems(self.root, self.agencies), [])
        self.assertIsNone(actions._load(self.tmp / "missing-root"))
        self.assertEqual(self.tree(self.root), before)
        self.put(document())
        alias = self.tmp / "root-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(actions.read_actions(alias, self.agencies), [])
        real_open = os.open

        def opening(path, flags, *args, **kwargs):
            if path == actions.PARTS[1]:
                (self.path.parent / "unrelated").write_bytes(b"synthetic")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(actions, "_open", side_effect=opening):
            self.assertEqual(actions.read_actions(self.root, self.agencies), [])
        self.path.parent.rename(self.tmp / "detached")
        self.assertIsNone(actions._load(self.root))
        self.assertFalse(self.path.parent.exists())

    def test_missing_leaf_does_not_hide_parent_change(self):
        real_stat = os.stat

        def inspecting(path, *args, **kwargs):
            if path == actions.PARTS[1]:
                self.path.parent.rename(self.tmp / "detached")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(actions.os, "stat", side_effect=inspecting):
            with self.assertRaises(actions.ActionError) as caught:
                actions._load(self.root)
            self.assertEqual(str(caught.exception), CHANGED)

    def test_leaf_substitution_retains_no_follow_single_link_and_nonblocking_policy(self):
        outside = self.tmp / "outside-leaf"
        outside.write_bytes(actions._dump([row()]))
        for kind in ("symlink", "dangling", "fifo", "directory", "hardlink", "regular"):
            self.put(document())
            real_open = os.open

            def opening(path, flags, *args, **kwargs):
                if path == actions.PARTS[1]:
                    self.assertTrue(flags & os.O_NOFOLLOW)
                    self.assertTrue(flags & os.O_NONBLOCK)
                    self.path.unlink()
                    if kind in ("symlink", "dangling"):
                        self.path.symlink_to(outside if kind == "symlink" else self.tmp / "missing")
                    elif kind == "fifo":
                        os.mkfifo(self.path)
                    elif kind == "directory":
                        self.path.mkdir()
                    elif kind == "hardlink":
                        os.link(outside, self.path)
                    else:
                        self.path.write_bytes(actions._dump([row(text="Replacement café")]))
                return real_open(path, flags, *args, **kwargs)

            with self.subTest(kind=kind), mock.patch.object(actions, "_open", side_effect=opening):
                if kind == "regular":
                    self.assertEqual(actions.read_actions(self.root, self.agencies, today=TODAY),
                                     [row(text="Replacement café")])
                else:
                    with self.assertRaises(actions.ActionError):
                        actions._load(self.root)
            if self.path.is_dir():
                self.path.rmdir()
            else:
                self.path.unlink()
        self.assertEqual(outside.read_bytes(), actions._dump([row()]))

    def test_static_parent_and_leaf_diagnostics(self):
        outside = self.tmp / "static-outside"
        outside.write_bytes(actions._dump([row()]))
        for dangling in (False, True):
            self.path.symlink_to(self.tmp / "absent" if dangling else outside)
            with self.assertRaisesRegex(actions.ActionError, "content/agency-actions.json: is a symbolic link"):
                actions._load(self.root)
            self.path.unlink()
        self.path.parent.rename(self.tmp / "static-content")
        self.path.parent.symlink_to(self.tmp / "static-content", target_is_directory=True)
        with self.assertRaisesRegex(actions.ActionError, "content: is a symbolic link"):
            actions._load(self.root)
        self.path.parent.unlink()
        self.path.parent.write_bytes(b"synthetic not a directory")
        with self.assertRaisesRegex(actions.ActionError, "content: must be a folder"):
            actions._load(self.root)
        self.assertEqual(outside.read_bytes(), actions._dump([row()]))

    def test_parser_refusals_remain_private_and_read_only(self):
        for data, message in ((bytes([255]), "not valid UTF-8 JSON"),
                              (b'{"actions": [], "actions": []}', "repeats a key"),
                              (b'{"schema_version": NaN, "actions": []}', "not valid UTF-8 JSON"),
                              (b"[" * 2000 + b"]" * 2000, "must be an object|not valid UTF-8 JSON")):
            self.path.write_bytes(data)
            before = self.tree(self.root)
            mode = self.path.stat().st_mode
            with self.assertRaisesRegex(actions.ActionError, message):
                actions.read_actions(self.root, self.agencies)
            self.assertEqual(self.tree(self.root), before)
            self.assertEqual(self.path.stat().st_mode, mode)

    def test_actual_byte_cap_growth_and_metadata_shape(self):
        data = actions._dump([row(text="Café")])
        self.path.write_bytes(data)
        self.path.chmod(0o640)
        info = self.path.stat()
        self.assertEqual(actions._load(self.root), (data, actions._identity(info), stat.S_IMODE(info.st_mode)))
        padded = data + b" " * (actions.MAX_BYTES - len(data))
        self.path.write_bytes(padded)
        self.assertEqual(actions.read_actions(self.root, self.agencies, today=TODAY), [row(text="Café")])
        real_fstat, real_fdopen = os.fstat, os.fdopen
        requested = []

        def inspecting(fd):
            result = real_fstat(fd)
            if stat.S_ISREG(result.st_mode):
                with self.path.open("ab") as handle:
                    handle.write(b" ")
            return result

        def opening(*args, **kwargs):
            handle = real_fdopen(*args, **kwargs)
            original = handle.read
            def reading(size):
                requested.append(size)
                return original(size)
            handle.read = reading
            return handle

        with mock.patch.object(actions.os, "fstat", side_effect=inspecting), \
                mock.patch.object(actions.os, "fdopen", side_effect=opening):
            with self.assertRaisesRegex(actions.ActionError, "file too large"):
                actions._load(self.root)
        self.assertEqual(requested, [actions.MAX_BYTES + 1])

    def test_descriptor_cleanup_and_value_free_io_failures(self):
        for failure in (None, "missing", "content_open", "leaf_open", "fstat", "read", "stat", "parse", "oversize"):
            self.put(document())
            if failure == "missing":
                self.path.unlink()
            elif failure == "parse":
                self.path.write_bytes(b"SYNTHETIC-PRIVATE")
            elif failure == "oversize":
                self.path.write_bytes(b" " * (actions.MAX_BYTES + 1))
            real_open, real_fstat, real_fdopen, real_stat = os.open, os.fstat, os.fdopen, os.stat
            opened = []

            def opening(path, flags, *args, **kwargs):
                if (failure == "content_open" and path == "content") or (failure == "leaf_open" and path == actions.PARTS[1]):
                    raise OSError("SYNTHETIC-PRIVATE /outside/path")
                fd = real_open(path, flags, *args, **kwargs)
                opened.append(fd)
                return fd

            def inspecting(fd):
                if failure == "fstat":
                    raise OSError("SYNTHETIC-PRIVATE /outside/path")
                return real_fstat(fd)

            def entry(*args, **kwargs):
                if failure == "stat":
                    raise OSError("SYNTHETIC-PRIVATE /outside/path")
                return real_stat(*args, **kwargs)

            def stream(*args, **kwargs):
                handle = real_fdopen(*args, **kwargs)
                if failure == "read":
                    handle.read = mock.Mock(side_effect=OSError("SYNTHETIC-PRIVATE /outside/path"))
                return handle

            with self.subTest(failure=failure), mock.patch.object(actions, "_open", side_effect=opening), \
                    mock.patch.object(actions.os, "fstat", side_effect=inspecting), \
                    mock.patch.object(actions.os, "stat", side_effect=entry), \
                    mock.patch.object(actions.os, "fdopen", side_effect=stream):
                for _ in range(3):
                    if failure in (None, "missing"):
                        actions.read_actions(self.root, self.agencies)
                    else:
                        with self.assertRaises(actions.ActionError) as caught:
                            actions.read_actions(self.root, self.agencies)
                        self.assertNotIn("SYNTHETIC-PRIVATE", str(caught.exception))
                        self.assertNotIn(str(self.tmp), str(caught.exception))
            for fd in opened:
                with self.assertRaises(OSError):
                    os.fstat(fd)

    def test_problems_and_real_list_cli_propagate_race_refusal(self):
        with self.parent_swap("before_leaf"):
            self.assertEqual(actions.action_problems(self.root, self.agencies), [CHANGED])
        self.restore_parent()
        for flags in ([], ["--json"]):
            out, err = io.StringIO(), io.StringIO()
            with self.parent_swap("before_leaf"), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    mock.patch.object(sys, "argv", ["campaign_tool", "content", "agency-action", "list",
                                                   "--directory", str(self.root), *flags]):
                with self.assertRaises(SystemExit) as caught:
                    cli.main()
                self.assertEqual(caught.exception.code, 2)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(err.getvalue(), "Stopped: content/agency-actions.json: cannot list; fix the file and its agency references.\n")
            self.restore_parent()


if __name__ == "__main__":
    unittest.main()
