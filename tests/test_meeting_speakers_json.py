"""RF0015-5: deeply nested speaker-input JSON is refused with the input's own domain error. Synthetic, offline.

Nesting is assembled as bytes (never as recursive Python structures) so the real native decoder fails.
Run from the repository root: python3 -B -m unittest tests.test_meeting_speakers_json -q
"""
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from campaign_tool import meeting_speakers as ms
from campaign_tool import meetings_status
from campaign_tool.meeting_stage import stage
from tests import test_meetings_status as status_tests
from tests.test_meeting_speakers import run
from tests.test_meetings_manual import MID, ManualBase

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
BAD = ms.MeetingSpeakersError
DEPTH = 10000
MARKER = "SECRET-SPEAKER-MARKER"
DEEP = {
    "array": b"[" * DEPTH + f'"{MARKER}"'.encode() + b"]" * DEPTH,
    "object": b'{"a":' * DEPTH + f'"{MARKER}"'.encode() + b"}" * DEPTH,
}
STAGED_MSG = f"content/meetings/{MID}.json is not a regular UTF-8 JSON file up to 4 MiB"
SCAN_MSG = "kit/meetings.json is not a regular UTF-8 JSON file up to 4 MiB"


class DeepBase(ManualBase):
    def setUp(self):
        super().setUp()
        self.add(starts_at="2099-01-20T18:30:00-08:00")
        stage(self.root, MID)
        self.write_scan([{"id": "other-meeting", "status": "scheduled"}])
        self.paths = {
            "staged": (self.root / "content" / "meetings" / f"{MID}.json", STAGED_MSG),
            "scan": (self.scan, SCAN_MSG),
            "manual": (self.manual, ms.BAD_MANUAL),
            "store": (self.root / "kit" / "meetings" / "speakers" / f"{MID}.json", ms.BAD_STORE),
        }

    def plan(self):
        ms.update_plan(self.root, MID, add="Ann", point="ask:1", now=NOW)

    @contextmanager
    def swapped(self, name, payload):
        """Replace one input with the payload; the original bytes (or absence) always come back."""
        path = self.paths[name][0]
        original = path.read_bytes() if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        try:
            yield path
        finally:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)

    def snapshot(self):
        found = {}
        for here, dirs, files in os.walk(self.root):
            for entry in dirs + files:
                path = os.path.join(here, entry)
                info = os.lstat(path)
                found[os.path.relpath(path, self.root)] = (
                    info.st_mode, info.st_mtime_ns, Path(path).read_bytes() if os.path.isfile(path) else None)
        return found

    def actions(self):
        return [lambda: ms.list_plan(self.root, MID, now=NOW),
                lambda: ms.update_plan(self.root, MID, add="Bo", point="ask:1", now=NOW),
                lambda: ms.update_plan(self.root, MID, add="Ann", point="ask:1", now=NOW),  # normally a no-op
                lambda: ms.update_plan(self.root, MID, remove="Ann", now=NOW),
                lambda: ms.update_plan(self.root, MID, remove="Nobody", now=NOW)]  # normally a no-op


class DecoderTests(unittest.TestCase):
    def test_real_decoder_depth_refuses_with_original_context(self):
        recursion_limit = sys.getrecursionlimit()
        for shape, payload in DEEP.items():
            with self.subTest(shape):
                self.assertLess(len(payload), ms.MAX_STORE_BYTES)
                with self.assertRaises(RecursionError):  # the native decoder really fails on these exact bytes
                    json.loads(payload.decode("utf-8"))
                on_bad = BAD("context-specific message")
                with self.assertRaises(BAD) as caught:
                    ms._json(payload, on_bad)
                self.assertIs(caught.exception, on_bad)
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertNotIn(MARKER, str(caught.exception))
        self.assertEqual(sys.getrecursionlimit(), recursion_limit)

    def test_parser_boundary_is_narrow(self):
        on_bad = BAD("bad")
        with mock.patch.object(ms.json, "loads", side_effect=RuntimeError("unrelated")):
            with self.assertRaises(RuntimeError):
                ms._json(b"{}", on_bad)
        with mock.patch.object(ms.json, "loads", side_effect=RecursionError("injected")):
            with self.assertRaises(BAD) as caught:
                ms._json(b"{}", on_bad)
        self.assertIs(caught.exception, on_bad)
        self.assertEqual(ms._json(b'{"a": [1, 2]}', on_bad), {"a": [1, 2]})


class SourceTests(DeepBase):
    def test_deep_json_refuses_all_four_source_paths(self):
        self.plan()
        for shape, payload in DEEP.items():
            for name, (path, message) in self.paths.items():
                with self.subTest(shape=shape, source=name), self.swapped(name, payload):
                    with self.assertRaises(BAD) as caught:
                        ms.list_plan(self.root, MID, now=NOW)
                    self.assertEqual(str(caught.exception), message)
                    self.assertNotIn(MARKER, str(caught.exception))
                    if name == "store":
                        with self.assertRaises(BAD) as direct:
                            ms.read_plan(self.root, MID)
                        self.assertEqual(str(direct.exception), ms.BAD_STORE)
                        self.assertTrue(ms.meeting_points(self.root, MID, now=NOW))  # points never read the plan
                    else:
                        with self.assertRaises(BAD):
                            ms.meeting_points(self.root, MID, now=NOW)

    def test_depth_failure_blocks_actions_and_noops_without_writing(self):
        for with_plan in (False, True):
            if with_plan:
                self.plan()
            for name, (path, message) in self.paths.items():
                if name == "store" and not with_plan:
                    continue
                for shape, payload in DEEP.items():
                    for number, action in enumerate(self.actions()):
                        with self.subTest(plan=with_plan, source=name, shape=shape, action=number), \
                                self.swapped(name, payload):
                            before = self.snapshot()
                            with mock.patch.object(ms, "_write") as write:
                                with self.assertRaises(BAD) as caught:
                                    action()
                            write.assert_not_called()
                            self.assertEqual(str(caught.exception), message)
                            self.assertEqual(self.snapshot(), before)

    def test_shallow_errors_and_successful_contracts_unchanged(self):
        self.plan()
        valid = ms.list_plan(self.root, MID, now=NOW)
        self.assertEqual([s for p in valid["points"] for s in p["speakers"]], ["Ann"])
        for name, (path, message) in self.paths.items():
            for label, payload in (("bad utf-8", b"\xff\xfe"), ("truncated", b'{"a": ['), ("empty", b""),
                                   ("duplicate key", b'{"a": 1, "a": 2}')):
                with self.subTest(source=name, case=label), self.swapped(name, payload):
                    with self.assertRaises(BAD) as caught:
                        ms.list_plan(self.root, MID, now=NOW)
                    self.assertEqual(str(caught.exception), message)
        self.assertEqual(ms.list_plan(self.root, MID, now=NOW), valid)
        plan_path = self.paths["store"][0]
        before = plan_path.read_bytes()
        self.assertEqual(ms.update_plan(self.root, MID, add="Ann", point="ask:1", now=NOW)["changed"], False)
        self.assertEqual(plan_path.read_bytes(), before)
        self.assertEqual(ms.update_plan(self.root, MID, add="Bo", point="ask:2", now=NOW)["assignments"], 2)
        self.assertEqual(oct(plan_path.stat().st_mode & 0o777), "0o600")
        plan_path.unlink()  # optional store absence still lists as an empty plan
        self.assertEqual(ms.read_plan(self.root, MID), (None, []))

    def test_injected_recursion_error_is_refused_like_real_depth(self):
        with mock.patch.object(ms.json, "loads", side_effect=RecursionError("injected")):
            with self.assertRaises(BAD) as caught:
                ms.list_plan(self.root, MID, now=NOW)
        self.assertEqual(str(caught.exception), STAGED_MSG)


class CliTests(DeepBase):
    def test_real_cli_depth_refusal_is_single_private_stopped_line(self):
        self.plan()
        for name, (path, message) in self.paths.items():
            for shape, payload in DEEP.items():
                for cli_args in (("--list",), ("--add", "Bo", "--point", "ask:1"), ("--remove", "Ann")):
                    with self.subTest(source=name, shape=shape, cli=cli_args), self.swapped(name, payload):
                        before = self.snapshot()
                        code, out, err = run("meetings-speakers", "--directory", self.root, "--id", MID, *cli_args)
                        self.assertEqual((code, out, err), (1, "", f"Stopped: {message}\n"))
                        for text in (out, err):
                            self.assertNotIn(MARKER, text)
                            self.assertNotIn("Traceback", text)
                            self.assertNotIn("Recorded", text)
                        self.assertEqual(self.snapshot(), before)

    def test_cli_depth_refusal_without_a_plan_creates_nothing(self):
        for name in ("staged", "scan", "manual"):
            message = self.paths[name][1]
            for cli_args in (("--list",), ("--add", "Bo", "--point", "ask:1"), ("--remove", "Ann")):
                with self.subTest(source=name, cli=cli_args), self.swapped(name, DEEP["array"]):
                    code, out, err = run("meetings-speakers", "--directory", self.root, "--id", MID, *cli_args)
                    self.assertEqual((code, out, err), (1, "", f"Stopped: {message}\n"))
                    self.assertFalse((self.root / "kit" / "meetings" / "speakers").exists())


class StatusTests(unittest.TestCase):
    def test_meetings_status_deep_plan_remains_unknown(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meetings-status-deep-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "campaign"
        root.mkdir()
        status_tests.build(root)
        deep = root / "kit" / "meetings" / "speakers" / "i-planned.json"
        deep.write_bytes(DEEP["array"])
        report = meetings_status.inventory(root, status_tests.AS_OF, directory="D", today=status_tests.TODAY)
        rows = {row.get("id", row["path"]): row for row in report["rows"]}
        self.assertEqual(rows["i-planned"]["speakers_plan"], "unknown")
        self.assertEqual(rows["g-done"]["speakers_plan"], "present")  # the healthy neighbour is kept
        unreadable = [r["path"] for r in report["rows"] if r["kind"] == "unreadable"]
        self.assertEqual(unreadable, ["kit/meetings.json[4]", "kit/meetings/speakers/i-planned.json"])
        blob = json.dumps(report) + meetings_status.render_text(report)
        for leak in (MARKER, "Traceback", "RecursionError"):
            self.assertNotIn(leak, blob)


if __name__ == "__main__":
    unittest.main()
