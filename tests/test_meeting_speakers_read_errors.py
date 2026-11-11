"""RF2340-2: filesystem failures reading speaker inputs become the caller's safe refusal. Synthetic, offline.

Run from the repository root: python3 -B -m unittest tests.test_meeting_speakers_read_errors -q
"""
import os
import unittest
from unittest import mock

from campaign_tool import meeting_speakers as ms
from tests.test_meeting_speakers import run
from tests.test_meeting_speakers_io import BAD, MID, NOW, IoBase

CANARY = "SYNTHETIC-OS-PAYLOAD-RF2340"


class ReadErrorTests(IoBase):
    def messages(self):
        return {name: (spec[2] if spec[2] else f"content/meetings/{MID}.json is not a regular UTF-8 JSON file up to 4 MiB")
                for name, spec in self.paths.items()}

    def test_each_read_phase_oserror_uses_caller_error(self):
        path, limit, _ = self.paths["store"]
        error = OSError(13, CANARY, str(path), None, "/secret/second")
        real_lstat, real_open, real_fstat, real_close = os.lstat, os.open, os.fstat, os.close
        phases = {
            "lstat": mock.patch.object(ms.os, "lstat", side_effect=error),
            "open": mock.patch.object(ms.os, "open", side_effect=error),
            "fstat": mock.patch.object(ms.os, "fstat", side_effect=error),
            "fdopen": mock.patch.object(ms.os, "fdopen", side_effect=error),
            "read": mock.patch.object(ms.os, "fdopen", side_effect=lambda *a, **k: _Reader(error)),
        }
        for name, patcher in phases.items():
            with self.subTest(name), patcher:
                with self.assertRaises(BAD) as caught:
                    ms._read_regular(path, limit, False, BAD("safe"))
                self.assertEqual(str(caught.exception), "safe")
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertIsNone(caught.exception.__cause__)

        closed = []

        def failing_close(fd):
            closed.append(fd)
            real_close(fd)
            raise error

        with mock.patch.object(ms.os, "close", failing_close):
            with self.assertRaises(BAD) as caught:
                ms._read_regular(path, limit, False, BAD("safe"))
        self.assertEqual(str(caught.exception), "safe")
        self.assertEqual(len(closed), 1)
        self.assertEqual((real_lstat, real_open, real_fstat), (os.lstat, os.open, os.fstat))

    def test_absence_is_only_initial_lstat_absence(self):
        path, limit, _ = self.paths["store"]
        self.assertIsNone(ms._read_regular(path.with_name("none.json"), limit, True, BAD("safe")))
        with self.assertRaises(BAD):
            ms._read_regular(path.with_name("none.json"), limit, False, BAD("safe"))
        real_open = os.open
        for absent_ok in (True, False):
            with self.subTest(absent_ok=absent_ok):
                moved = path.with_name("moved.json")

                def vanish(target, *args, **kwargs):
                    os.rename(path, moved)
                    return real_open(target, *args, **kwargs)

                with mock.patch.object(ms.os, "open", vanish):
                    with self.assertRaises(BAD) as caught:
                        ms._read_regular(path, limit, absent_ok, BAD("safe"))
                self.assertEqual(str(caught.exception), "safe")
                self.assertNotIn(str(path), str(caught.exception))
                os.rename(moved, path)

    def _inject(self, name, error):
        path = self.paths[name][0]
        real_open = os.open

        def hook(target, *args, **kwargs):
            if os.fspath(target) == os.fspath(path):
                raise error
            return real_open(target, *args, **kwargs)

        return mock.patch.object(ms.os, "open", hook)

    def test_all_four_sources_block_actions_before_writes(self):
        messages = self.messages()
        actions = {
            "list": lambda: ms.list_plan(self.root, MID, now=NOW),
            "add": lambda: ms.update_plan(self.root, MID, add="Bea", point="ask:1", now=NOW),
            "repeat-add": lambda: ms.update_plan(self.root, MID, add="Ann", point="ask:1", now=NOW),
            "remove": lambda: ms.update_plan(self.root, MID, remove="Ann", now=NOW),
            "remove-unknown": lambda: ms.update_plan(self.root, MID, remove="Zed", now=NOW),
        }
        store = self.paths["store"][0]
        for source, (path, _, _) in self.paths.items():
            for action, call in actions.items():
                with self.subTest(source=source, action=action):
                    before = store.read_bytes(), store.stat().st_mtime_ns
                    error = PermissionError(13, CANARY, str(path))
                    with self._inject(source, error), mock.patch.object(ms, "_write") as write:
                        with self.assertRaises(BAD) as caught:
                            call()
                    write.assert_not_called()
                    self.assertEqual(str(caught.exception), messages[source])
                    self.assertNotIn(CANARY, str(caught.exception))
                    self.assertEqual((store.read_bytes(), store.stat().st_mtime_ns), before)
            if source != "store":
                with self.subTest(source=source, action="points"), self._inject(source, PermissionError(13, CANARY)):
                    with self.assertRaises(BAD) as caught:
                        ms.meeting_points(self.root, MID, now=NOW)
                    self.assertEqual(str(caught.exception), messages[source])

    def test_absent_plan_gains_nothing_on_refusal(self):
        store = self.paths["store"][0]
        store.unlink()
        before = sorted(self.root.rglob("*"))
        with self._inject("manual", PermissionError(13, CANARY)), self.assertRaises(BAD):
            ms.update_plan(self.root, MID, add="Ann", point="ask:1", now=NOW)
        self.assertEqual(sorted(self.root.rglob("*")), before)

    def test_cli_read_failures_have_single_private_stopped_line(self):
        messages = self.messages()
        for source, (path, _, _) in self.paths.items():
            for error in (PermissionError(13, CANARY, str(path)), FileNotFoundError(2, CANARY, str(path), None, "/x/y")):
                for args in (("--list",), ("--add", "Bea", "--point", "ask:1"), ("--remove", "Ann")):
                    with self.subTest(source=source, error=type(error).__name__, args=args):
                        with self._inject(source, error):
                            code, out, err = run("meetings-speakers", "--directory", self.root, "--id", MID, *args)
                        self.assertEqual(code, 1)
                        self.assertEqual(out, "")
                        self.assertEqual(err, f"Stopped: {messages[source]}\n")

    def test_acquired_descriptor_closed_once_and_runtime_error_propagates(self):
        path, limit, _ = self.paths["store"]
        opened, closed = [], []
        real_open, real_close = os.open, os.close

        def spy_open(*a, **k):
            fd = real_open(*a, **k)
            opened.append(fd)
            return fd

        def spy_close(fd):
            closed.append(fd)
            real_close(fd)

        with mock.patch.object(ms.os, "open", spy_open), mock.patch.object(ms.os, "close", spy_close):
            with mock.patch.object(ms.os, "fdopen", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    ms._read_regular(path, limit, False, BAD("safe"))
            with mock.patch.object(ms.os, "open", side_effect=PermissionError(13, CANARY)):
                with self.assertRaises(BAD):
                    ms._read_regular(path, limit, False, BAD("safe"))
            self.assertTrue(ms._read_regular(path, limit, False, BAD("safe")))
        self.assertEqual(opened, closed)
        self.assertEqual(len(opened), 2)

    def test_meetings_status_io_plan_remains_unknown(self):
        from campaign_tool import meetings_status
        errors = []
        with self._inject("store", PermissionError(13, CANARY, str(self.paths["store"][0]))):
            result = meetings_status._speakers(self.root, MID, True, errors)
        self.assertEqual(result, "unknown")
        self.assertEqual(len(errors), 1)
        self.assertNotIn(CANARY, repr(errors))

    def test_success_unchanged(self):
        self.assertTrue(ms.update_plan(self.root, MID, remove="Ann", now=NOW)["changed"])


class _Reader:
    def __init__(self, error):
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        raise self.error


if __name__ == "__main__":
    unittest.main()
