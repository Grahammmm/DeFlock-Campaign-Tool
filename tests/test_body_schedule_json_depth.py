"""RF1255-2: decoder recursion in content/body-schedules.json is a fixed refusal, never a crash.

Synthetic campaigns only, no network. Run from the repository root:
python3 -B -m unittest tests.test_body_schedule_json_depth -q
"""
import json
import os
import subprocess
import sys
import unittest
from datetime import date
from unittest import mock

from campaign_tool import body_schedule_cmd as cmd, body_schedules, today
from campaign_tool.body_schedule_cmd import BodyScheduleError
from campaign_tool.body_schedules import read_body_schedules
from campaign_tool.content import MAX_JSON_BYTES, ContentError, load_content
from campaign_tool.content_check import content_problems
from tests.test_body_schedules import REPO_ROOT, TODAY, Base, document, row, snapshot

DEPTH = 100_000
REFUSAL = "body-schedules.json: JSON nesting is too deep"
ENVELOPE = b'{"schema_version":1,"schedules":'


def deep_array():
    return ENVELOPE + b"[" * DEPTH + b"0" + b"]" * DEPTH + b"}"


def deep_object():
    return ENVELOPE + b'{"a":' * DEPTH + b"0" + b"}" * DEPTH + b"}"


def deep_after_valid_row():
    good = json.dumps(row()).encode()
    return ENVELOPE + b"[" + good + b"," + b"[" * DEPTH + b"0" + b"]" * DEPTH + b"]}"


def open_fds():
    return sorted(os.listdir("/proc/self/fd"))


class DepthBase(Base):
    def bad(self, payload=None):
        payload = deep_array() if payload is None else payload
        self.assertLess(len(payload), MAX_JSON_BYTES)
        self.path.write_bytes(payload)

    def repair(self, value=None):
        self.write(document() if value is None else value)


class DepthTests(DepthBase):
    def test_real_deep_array_and_object_are_fixed_content_refusals(self):
        for payload in (deep_array(), deep_object(), deep_after_valid_row()):
            with self.subTest(size=len(payload)):
                self.bad(payload)
                before, fds = snapshot(self.root), open_fds()
                mode = self.path.stat().st_mode
                with self.assertRaises(ContentError) as caught:
                    read_body_schedules(self.root, today=TODAY)
                self.assertNotIsInstance(caught.exception, RecursionError)
                self.assertEqual(str(caught.exception), REFUSAL)
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertNotIn(str(self.root), str(caught.exception))
                self.assertEqual(open_fds(), fds)  # the reader's descriptor is closed
                self.assertEqual(snapshot(self.root), before)
                self.assertEqual(self.path.stat().st_mode, mode)

    def test_maintenance_refuses_before_any_writer_and_recovers(self):
        self.bad()
        before = snapshot(self.root)
        calls = {
            "list": lambda: cmd.list_schedules(self.root, today=TODAY),
            "add": lambda: cmd.add_schedule(self.root, body="Other Council", weekday="monday", ordinals=[1],
                                            local_start_time="18:30", time_zone="America/Los_Angeles",
                                            source_url="https://example.invalid/other", checked_on="2026-10-01",
                                            today=TODAY),
            "recheck": lambda: cmd.recheck_schedule(self.root, body="Synthetic Council", checked_on="2026-10-02",
                                                    today=TODAY),
            "remove": lambda: cmd.remove_schedule(self.root, body="Synthetic Council", today=TODAY),
        }
        with mock.patch.object(cmd, "_write", side_effect=AssertionError("writer reached")) as writer, \
                mock.patch.object(cmd.os, "mkstemp", side_effect=AssertionError("temp created"), create=True):
            for name, call in calls.items():
                with self.subTest(name), self.assertRaises(BodyScheduleError) as caught:
                    call()
                self.assertEqual(str(caught.exception), REFUSAL)
            writer.assert_not_called()
        self.assertEqual(snapshot(self.root), before)
        self.repair()
        self.assertEqual(cmd.list_schedules(self.root, today=TODAY), "No body schedules recorded.")

    def test_real_cli_depth_refusals_keep_existing_exit_contracts(self):
        self.bad()
        before = snapshot(self.root)
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

        def run(*args):
            return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args, "--directory", str(self.root)],
                                  cwd=REPO_ROOT, env=env, text=True, capture_output=True, timeout=120)

        for args in (("body-schedule", "list"), ("body-schedule", "remove", "--body", "Synthetic Council")):
            with self.subTest(args):
                result = run(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn(REFUSAL, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        result = run("content-check", "--json")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema"], "content-check-v1")
        self.assertEqual(report["problems"], [{"file": "body-schedules.json", "message": "JSON nesting is too deep"}])
        self.assertEqual(snapshot(self.root), before)
        self.repair()
        healthy = run("body-schedule", "list")
        self.assertEqual((healthy.returncode, healthy.stdout.strip()), (0, "No body schedules recorded."))

    def test_today_retains_unrelated_work_and_one_schedule_unreadable(self):
        self.stage("synthetic-due", "2026-11-01T18:30:00-08:00")
        self.repair(document(row()))
        healthy = today.todo(self.root, TODAY)
        self.assertTrue(any(i["kind"] == "expected_regular_meeting" for i in healthy))
        self.bad()
        items = today.todo(self.root, TODAY)
        unreadable = [i for i in items if "body-schedules.json" in i["what"] and REFUSAL in i["what"]]
        self.assertEqual(len(unreadable), 1)
        self.assertFalse([i for i in items if i["kind"] in ("expected_regular_meeting", "stale_body_schedule")])
        others = [i for i in items if i not in unreadable]
        self.assertTrue(others, "unrelated work must survive the refused schedule source")
        self.repair(document(row()))
        self.assertTrue(any(i["kind"] == "expected_regular_meeting" for i in today.todo(self.root, TODAY)))

    def test_content_admission_and_check_share_the_refusal(self):
        self.bad()
        with self.assertRaises(ContentError) as caught:
            load_content(self.root, today=TODAY)
        self.assertIn(REFUSAL, str(caught.exception))
        (self.root / "content" / "meetings" / "broken.json").write_text("{not json", encoding="utf-8")
        problems = content_problems(self.root)
        self.assertEqual(len([p for p in problems if REFUSAL in str(p)]), 1)
        self.assertTrue([p for p in problems if "broken.json" in str(p)])
        self.assertFalse([p for p in problems if "RecursionError" in str(p)])


class PolicyTests(DepthBase):
    def test_only_decoder_recursion_is_reclassified(self):
        self.repair()
        with mock.patch.object(body_schedules.json, "loads", side_effect=RecursionError("raw detail")):
            with self.assertRaises(ContentError) as caught:
                read_body_schedules(self.root, today=TODAY)
        self.assertEqual(str(caught.exception), REFUSAL)
        self.assertNotIn("raw detail", str(caught.exception))
        for error in (MemoryError("sentinel"), RuntimeError("sentinel")):
            with mock.patch.object(body_schedules.json, "loads", side_effect=error):
                with self.assertRaises(type(error)) as caught:
                    read_body_schedules(self.root, today=TODAY)
                self.assertIs(caught.exception, error)
        self.path.write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(ContentError, "not valid UTF-8 JSON"):
            read_body_schedules(self.root, today=TODAY)
        self.write("{")
        with self.assertRaisesRegex(ContentError, "not valid UTF-8 JSON"):
            read_body_schedules(self.root, today=TODAY)
        self.write(document(row(weekday="funday")))
        with self.assertRaisesRegex(ContentError, r"schedules\[0\]\.weekday"):
            read_body_schedules(self.root, today=TODAY)

    def test_absent_empty_valid_and_byte_limit_contracts_remain(self):
        self.path.unlink(missing_ok=True)
        self.assertEqual(read_body_schedules(self.root, today=TODAY), [])
        self.assertFalse(self.path.exists())
        self.repair()
        self.assertEqual(read_body_schedules(self.root, today=TODAY), [])
        self.write(document(row(body="Zeta Council"), row(body="Älpha Council")))
        self.assertEqual([r["body"] for r in read_body_schedules(self.root, today=TODAY)],
                         ["Zeta Council", "Älpha Council"])
        self.path.write_bytes(b" " * (MAX_JSON_BYTES + 1))
        with mock.patch.object(body_schedules.json, "loads", side_effect=AssertionError("parsed")):
            with self.assertRaisesRegex(ContentError, "file too large"):
                read_body_schedules(self.root, today=TODAY)


if __name__ == "__main__":
    unittest.main()
