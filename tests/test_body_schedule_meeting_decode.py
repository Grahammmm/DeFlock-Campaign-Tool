"""RF1741-3: decoder resource failures in a staged meeting are contained as controlled schedule-list refusals.

Synthetic campaign only; no network. Run from the repository root:
python3 -B -W error::ResourceWarning -m unittest tests.test_body_schedule_meeting_decode -q
"""
import contextlib
import io
import json
import sys
import unittest
from unittest import mock

from campaign_tool import body_schedule_cmd as cmd, cli
from campaign_tool.content import MAX_JSON_BYTES
from tests.test_body_schedule_meeting_reads import DAY, Base, snapshot

DEEP = "meetings/z.json: z.json: JSON nesting is too deep"
NUMBER = "meetings/z.json: z.json: JSON number could not be decoded"
DIGITS = "9" * (sys.get_int_max_str_digits() + 700 if sys.get_int_max_str_digits() else 10000)
VALID = {"id": "z", "body": "Synthetic Council", "starts_at": "2026-11-18T02:30:00Z"}


def deep_array():
    return ("[" * 20000).encode()


def deep_object():
    return ('{"a":' * 12000).encode()


def valid_with(field):
    return json.dumps({**VALID, "x": "@@"}).replace('"@@"', field).encode()


class DecodeTests(Base):
    def setUp(self):
        super().setUp()
        if sys.get_int_max_str_digits() == 0:
            self.skipTest("integer conversion limit disabled")
        self.stage("a.json")
        self.leaf = self.folder / "z.json"

    def bad_cases(self):
        yield "array", deep_array(), DEEP
        yield "object", deep_object(), DEEP
        yield "ignored-field-depth", valid_with("[" * 20000), DEEP
        yield "root-int", DIGITS.encode(), NUMBER
        yield "neg-int", b"-" + DIGITS.encode(), NUMBER
        yield "ignored-field-int", valid_with(DIGITS), NUMBER
        yield "nested-int", valid_with('{"n": ' + DIGITS + "}"), NUMBER

    def test_genuine_decoder_failures_refuse_whole_listing(self):
        for label, data, message in self.bad_cases():
            with self.subTest(label):
                self.assertLess(len(data), MAX_JSON_BYTES)
                self.leaf.write_bytes(data)
                before = snapshot(self.root)
                self.refused(message)
                with self.assertRaises(cmd.ContentError) as caught:
                    cmd._read_meeting(self.leaf, self.leaf.lstat())
                self.assertEqual(str(caught.exception), message.split(": ", 1)[1])
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(snapshot(self.root), before)

    def test_cli_exits_2_with_no_rows_or_traceback(self):
        for label, data, message in self.bad_cases():
            with self.subTest(label):
                self.leaf.write_bytes(data)
                out, err = io.StringIO(), io.StringIO()
                argv = ["campaign_tool", "body-schedule", "list", "--directory", str(self.root)]
                with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err), mock.patch.object(cmd, "date", wraps=cmd.date) as clock:
                    clock.today.return_value = DAY
                    with self.assertRaises(SystemExit) as caught:
                        cli.main()
                self.assertEqual(caught.exception.code, 2)
                self.assertEqual(out.getvalue(), "")
                self.assertIn(message, err.getvalue())
                for text in ("Traceback", "agenda-watch", "recursion", "digits", "set_int_max"):
                    self.assertNotIn(text, err.getvalue())

    def test_recovers_after_healthy_replacement(self):
        self.leaf.write_bytes(deep_array())
        self.refused(DEEP)
        self.leaf.write_text(json.dumps(VALID), encoding="utf-8")
        self.assertIn("agenda-watch=", self.listing())

    def test_catch_order_and_scope(self):
        self.leaf.write_text("{", encoding="utf-8")
        with self.assertRaises(cmd.BodyScheduleError) as caught:
            self.listing()
        self.assertIn("z.json: not valid UTF-8 JSON (", str(caught.exception))
        self.leaf.write_bytes(b"\xff")
        with self.assertRaises(cmd.BodyScheduleError) as caught:
            self.listing()
        self.assertIn("not valid UTF-8 JSON", str(caught.exception))
        self.leaf.write_text(json.dumps(VALID), encoding="utf-8")
        real = json.loads

        def failing(error):
            def loads(text, *args, **kwargs):
                if '"id": "a"' in text:  # only the staged meeting leaf, not site.json or the schedule store
                    raise error
                return real(text, *args, **kwargs)
            return loads
        with mock.patch.object(cmd.json, "loads", side_effect=failing(ValueError("plain"))):
            self.refused("meetings/a.json: a.json: JSON number could not be decoded")
        for error in (MemoryError("m"), RuntimeError("r")):
            with self.subTest(type(error).__name__), mock.patch.object(cmd.json, "loads", side_effect=failing(error)):
                with self.assertRaises(type(error)) as caught:
                    self.listing()
                self.assertIs(caught.exception, error)
        # an error after decoding (in the meeting validator) is not relabelled as a decoder failure
        with mock.patch.object(cmd, "load_meeting", side_effect=ValueError("late")):
            with self.assertRaises(ValueError) as caught:
                self.listing()
            self.assertNotIsInstance(caught.exception, cmd.ContentError)

    def test_size_refusal_precedes_decoding(self):
        self.leaf.write_bytes(b"[" * (MAX_JSON_BYTES + 1))
        with self.assertRaises(cmd.BodyScheduleError) as caught:
            self.listing()
        self.assertIn("file too large", str(caught.exception))

    def test_descriptor_closed_after_decoder_refusal(self):
        self.leaf.write_bytes(deep_array())
        with self.tracked() as (fds, _):
            with self.assertRaises(cmd.ContentError):
                cmd._read_meeting(self.leaf, self.leaf.lstat())
        self.assertTrue(fds)
        for fd in fds:
            self.closed(fd)


if __name__ == "__main__":
    unittest.main()
