"""RF1420-1: a decoder integer-conversion refusal in content/body-schedules.json is a fixed domain refusal.

Synthetic campaigns only, no network. Run from the repository root:
python3 -B -m unittest tests.test_body_schedule_numeric_decode -q
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from campaign_tool import body_schedule_cmd as cmd, body_schedules, today
from campaign_tool.body_schedule_cmd import BodyScheduleError
from campaign_tool.body_schedules import read_body_schedules
from campaign_tool.content import MAX_JSON_BYTES, ContentError
from campaign_tool.content_check import content_problems
from tests.test_body_schedules import REPO_ROOT, TODAY, Base, document, row, snapshot

REFUSAL = "body-schedules.json: JSON number could not be decoded"
ENVELOPE = b'{"schema_version":1,"schedules":'
HUGE = b"9" * 10_000  # built as bytes: no huge int-to-str conversion in setup
GOOD = json.dumps(row()).encode()

PAYLOADS = {
    "whole document": HUGE,
    "negative whole document": b"-" + HUGE,
    "schedules member": ENVELOPE + HUGE + b"}",
    "later value after healthy row": ENVELOPE + b"[" + GOOD + b"," + HUGE + b"]}",
    "negative later value": ENVELOPE + b"[" + GOOD + b",-" + HUGE + b"]}",
}
# a row whose ordinals array holds the over-limit token
PAYLOADS["row ordinal"] = (ENVELOPE + b'[{"body":"Synthetic Council","weekday":"tuesday","ordinals":[' + HUGE
                           + b'],"local_start_time":"18:30","time_zone":"America/Los_Angeles",'
                           b'"source_url":"https://example.invalid/calendar","checked_on":"2026-10-01"}]}')


def open_fds():
    return sorted(os.listdir("/proc/self/fd"))


def limit_active():
    return 0 < sys.get_int_max_str_digits() < len(HUGE)


class NumericBase(Base):
    def bad(self, payload=None):
        payload = HUGE if payload is None else payload
        self.assertLess(len(payload), MAX_JSON_BYTES)
        self.path.write_bytes(payload)

    def repair(self, value=None):
        self.write(document() if value is None else value)

    def run_cli(self, *args, limit="4300"):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PYTHONINTMAXSTRDIGITS", None)
        return subprocess.run([sys.executable, "-B", "-X", f"int_max_str_digits={limit}", "-m", "campaign_tool",
                               *args, "--directory", str(self.root)],
                              cwd=REPO_ROOT, env=env, text=True, capture_output=True, timeout=120)


@unittest.skipUnless(limit_active(), "runtime integer conversion limit is not active in this interpreter")
class ReaderTests(NumericBase):
    def test_real_numeric_conversion_refusal_is_fixed_content_error(self):
        for name, payload in PAYLOADS.items():
            with self.subTest(name):
                self.bad(payload)
                before, fds = snapshot(self.root), open_fds()
                with self.assertRaises(ContentError) as caught:
                    read_body_schedules(self.root, today=TODAY)
                self.assertEqual(str(caught.exception), REFUSAL)
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertNotIn("9999", str(caught.exception))
                self.assertNotIn("limit", str(caught.exception))
                self.assertEqual(open_fds(), fds)
                self.assertEqual(snapshot(self.root), before)

    def test_permitted_integers_still_reach_schema_validation(self):
        self.bad(ENVELOPE + b"[" + GOOD + b",12345]}")
        with self.assertRaises(ContentError) as caught:
            read_body_schedules(self.root, today=TODAY)
        self.assertNotEqual(str(caught.exception), REFUSAL)
        self.repair(document(row()))
        self.assertEqual(len(read_body_schedules(self.root, today=TODAY)), 1)

    def test_all_maintenance_operations_refuse_before_writer(self):
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
        with mock.patch.object(cmd, "_write", side_effect=AssertionError("writer reached")) as writer:
            for name, call in calls.items():
                with self.subTest(name), self.assertRaises(BodyScheduleError) as caught:
                    call()
                self.assertEqual(str(caught.exception), REFUSAL)
                self.assertEqual(caught.exception.exit_code, 2)
            writer.assert_not_called()
        self.assertEqual(snapshot(self.root), before)
        self.repair()
        self.assertEqual(cmd.list_schedules(self.root, today=TODAY), "No body schedules recorded.")

    def test_content_and_today_preserve_failure_isolation(self):
        self.stage("synthetic-due", "2026-11-01T18:30:00-08:00")
        self.repair(document(row()))
        self.assertTrue(any(i["kind"] == "expected_regular_meeting" for i in today.todo(self.root, TODAY)))
        self.bad()
        (self.root / "content" / "meetings" / "broken.json").write_text("{not json", encoding="utf-8")
        problems = content_problems(self.root)
        self.assertEqual(len([p for p in problems if REFUSAL in str(p)]), 1)
        self.assertTrue([p for p in problems if "broken.json" in str(p)])
        items = today.todo(self.root, TODAY)
        unreadable = [i for i in items if "body-schedules.json" in i["what"] and REFUSAL in i["what"]]
        self.assertEqual(len(unreadable), 1)
        self.assertFalse([i for i in items if i["kind"] in ("expected_regular_meeting", "stale_body_schedule")])
        self.assertTrue([i for i in items if i not in unreadable])


class ScopeTests(NumericBase):
    def test_decode_exception_order_and_scope_remain_narrow(self):
        self.repair()
        with mock.patch.object(body_schedules.json, "loads", side_effect=ValueError("interpreter wording")):
            with self.assertRaises(ContentError) as caught:
                read_body_schedules(self.root, today=TODAY)
        self.assertEqual(str(caught.exception), REFUSAL)
        self.assertNotIn("interpreter", str(caught.exception))
        for error in (MemoryError("sentinel"), RuntimeError("sentinel")):
            with mock.patch.object(body_schedules.json, "loads", side_effect=error):
                with self.assertRaises(type(error)) as caught:
                    read_body_schedules(self.root, today=TODAY)
                self.assertIs(caught.exception, error)
        with mock.patch.object(body_schedules.json, "loads", side_effect=RecursionError("x")):
            with self.assertRaisesRegex(ContentError, "JSON nesting is too deep"):
                read_body_schedules(self.root, today=TODAY)
        self.path.write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(ContentError, "not valid UTF-8 JSON"):
            read_body_schedules(self.root, today=TODAY)
        self.write("{")
        with self.assertRaisesRegex(ContentError, "not valid UTF-8 JSON"):
            read_body_schedules(self.root, today=TODAY)
        # a ValueError raised after decoding (row validation) is not mislabeled as a JSON number failure
        self.repair(document(row()))
        with mock.patch.object(body_schedules, "load_body_schedules", side_effect=ValueError("later")):
            with self.assertRaises(ValueError) as caught:
                read_body_schedules(self.root, today=TODAY)
        self.assertNotIsInstance(caught.exception, ContentError)

    def test_limits_and_healthy_contracts_are_unchanged(self):
        self.path.unlink(missing_ok=True)
        self.assertEqual(read_body_schedules(self.root, today=TODAY), [])
        self.repair(document(row(body="Zeta Council"), row(body="Älpha Council")))
        self.assertEqual([r["body"] for r in read_body_schedules(self.root, today=TODAY)],
                         ["Zeta Council", "Älpha Council"])
        self.path.write_bytes(b" " * (MAX_JSON_BYTES + 1))
        with mock.patch.object(body_schedules.json, "loads", side_effect=AssertionError("parsed")):
            with self.assertRaisesRegex(ContentError, "file too large"):
                read_body_schedules(self.root, today=TODAY)

    def test_disabled_limit_reaches_ordinary_schema_validation(self):
        self.bad(PAYLOADS["row ordinal"])
        result = self.run_cli("body-schedule", "list", limit="0")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn(REFUSAL, result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class CliTests(NumericBase):
    def test_actual_cli_integer_refusals_keep_domain_exit(self):
        self.bad()
        before = snapshot(self.root)
        for args in (("body-schedule", "list"), ("body-schedule", "remove", "--body", "Synthetic Council"),
                     ("body-schedule", "recheck", "--body", "Synthetic Council", "--checked-on", "2026-10-02")):
            with self.subTest(args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn(REFUSAL, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("set_int_max_str_digits", result.stderr)
        result = self.run_cli("content-check", "--json")
        self.assertEqual(result.returncode, 3, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema"], "content-check-v1")
        self.assertEqual(report["problems"], [{"file": "body-schedules.json",
                                               "message": "JSON number could not be decoded"}])
        self.assertEqual(snapshot(self.root), before)
        self.repair()
        healthy = self.run_cli("body-schedule", "list")
        self.assertEqual((healthy.returncode, healthy.stdout.strip()), (0, "No body schedules recorded."))


if __name__ == "__main__":
    unittest.main()
