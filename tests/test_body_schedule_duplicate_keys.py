"""RF1210-13: repeated JSON object members in body-schedules.json are refused, offline, with a fixed message."""
import contextlib
import io
import json
import sys
import unittest
from datetime import date
from unittest import mock

from campaign_tool import body_schedule_cmd as cmd, cli
from campaign_tool.body_schedules import read_body_schedules
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from tests.test_body_schedule_commands import snapshot
from tests.test_body_schedules import Base, row

DAY = date(2026, 11, 16)
FIXED = "body-schedules.json: duplicate JSON keys"
CANARY = "canary-zq9"
ROW = json.dumps(row(source_url=f"https://example.invalid/{CANARY}"))
HEAD = '{"schema_version":1,"schedules":'


def raw(*parts):
    return "".join(parts)


class DuplicateKeyTests(Base):
    def refused(self, text):
        self.write(text)
        with self.assertRaises(ContentError) as caught:
            read_body_schedules(self.root, today=DAY)
        self.assertEqual(str(caught.exception), FIXED)
        self.assertIsNone(caught.exception.__cause__)

    def test_duplicate_members_refuse_whole_document(self):
        cases = [
            raw('{"schema_version":1,"schema_version":1,"schedules":[]}'),
            raw('{"schema_version":1,"schedules":[],"schedules":[]}'),
            raw(HEAD, '[', ROW, ']', ',"schedules":[]}'),
            raw('{"schedules":[],"schema_version":1 , "schedules" : [ ' + ROW + ' ]}'),
            raw('{"sched\\u0075les":[],"schedules":[],"schema_version":1}'),
        ]
        for key in ("body", "weekday", "ordinals", "local_start_time", "time_zone", "source_url", "checked_on"):
            value = json.dumps(row()[key])
            cases.append(HEAD + "[" + ROW[:-1] + f',"{key}":{value}' + "}]}")  # identical repeat
            cases.append(HEAD + '[{"' + key + '":"x",' + ROW[1:] + "]}")  # conflicting repeat, other order
        cases.append(HEAD + "[" + ROW[:-1] + ',"weekday":"monday"}]}')
        cases.append(HEAD + "[" + ROW[:-1] + ',"week\\u0064ay":"tuesday"}]}')
        for text in cases:
            with self.subTest(text=text[:80]):
                self.refused(text)
                self.assertNotIn(CANARY, FIXED)

    def test_duplicates_cannot_hide_populated_or_invalid_rows(self):
        bad = json.dumps(row(weekday="funday"))
        for text in (
            raw(HEAD, "[", ROW, "]", ',"schedules":[]}'),
            raw('{"schema_version":1,"schedules":[],"schedules":[', ROW, "]}"),
            raw(HEAD, "[", ROW, ",", ROW[:-1], ',"body":"Other","body":"Other"}]}'),
            raw(HEAD, "[", ROW[:-1], ',"weekday":"funday","weekday":"tuesday"}]}'),
            raw(HEAD, "[", ROW[:-1], ',"extra":{"a":1,"a":2}}]}'),
            raw(HEAD, "[", bad, "]", ',"schedules":[', ROW, "]}"),
        ):
            with self.subTest(text=text[:80]):
                self.refused(text)

    def test_real_maintenance_refuses_without_mutation(self):
        for text in (raw(HEAD, "[", ROW, "]", ',"schedules":[]}'), raw(HEAD, "[]", ',"schedules":[', ROW, "]}")):
            self.write(text)
            before = snapshot(self.root)
            for args in (["list"],
                         ["add", "--body", "New Body", "--weekday", "monday", "--ordinals", "1",
                          "--local-start-time", "18:00", "--time-zone", "America/Los_Angeles",
                          "--source-url", "https://example.invalid/x", "--checked-on", "2026-11-09"],
                         ["recheck", "--body", "Synthetic Council", "--checked-on", "2026-11-09"],
                         ["remove", "--body", "Synthetic Council"]):
                out, err = io.StringIO(), io.StringIO()
                argv = ["campaign_tool", "body-schedule", *args, "--directory", str(self.root)]
                with (mock.patch.object(sys, "argv", argv), mock.patch.object(cmd, "date", wraps=date) as clock,
                      contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
                    clock.today.return_value = DAY
                    with self.assertRaises(SystemExit) as exit_info:
                        cli.main()
                with self.subTest(args=args[0]):
                    self.assertEqual(exit_info.exception.code, 2)
                    self.assertEqual(out.getvalue(), "")
                    self.assertEqual(err.getvalue().count(FIXED), 1, err.getvalue())
                    self.assertNotIn("Traceback", err.getvalue())
                    self.assertNotIn(CANARY, err.getvalue())
                    self.assertEqual(snapshot(self.root), before)

    def test_read_only_projections_refuse_ambiguity(self):
        self.write(raw(HEAD, "[", ROW, "]", ',"schedules":[]}'))
        before = snapshot(self.root)
        self.assertTrue(any(FIXED in str(problem) for problem in content_problems(self.root)))
        self.assertEqual(snapshot(self.root), before)

    def test_unique_json_and_other_refusals_stay_compatible(self):
        self.assertEqual(read_body_schedules(self.root, today=DAY) if not self.path.exists() else [], [])
        self.write(raw('{"sch\\u0065dules":[], "schema_version":1}'))
        self.assertEqual(read_body_schedules(self.root, today=DAY), [])
        self.write(raw(HEAD, "[", ROW, "]}"))
        self.assertEqual(len(read_body_schedules(self.root, today=DAY)), 1)
        self.write(raw(HEAD, "[", ROW[:-1].replace('[1, 3]', '[1, 3]'), "}]}"))  # unchanged unique row
        self.assertEqual(read_body_schedules(self.root, today=DAY)[0]["ordinals"], [1, 3])
        self.write(raw(HEAD, "[", ROW, ",", ROW, "]}"))  # distinct-body rule, not a member duplicate
        with self.assertRaises(ContentError) as caught:
            read_body_schedules(self.root, today=DAY)
        self.assertIn("duplicate body", str(caught.exception))
        self.write("{not json")
        with self.assertRaises(ContentError) as caught:
            read_body_schedules(self.root, today=DAY)
        self.assertIn("not valid UTF-8 JSON", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
