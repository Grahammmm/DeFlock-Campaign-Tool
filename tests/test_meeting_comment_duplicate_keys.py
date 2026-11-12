"""RF1530-9: a repeated JSON member anywhere in kit/comments-sent.json refuses the whole store. Synthetic only.

Run from the repository root: python3 -B -m unittest tests.test_meeting_comment_duplicate_keys -q
"""
import json
import os
import unittest
from unittest import mock

from campaign_tool import meeting_comments, today
from tests import test_meeting_comment_sent as fixture

FIRST = '{"on":"2026-03-10","channel":"email","recorded_at":"2026-03-11T00:00:00+00:00"}'
ROOT_DUP = '{"schema_version":1,"comments":{"m-a":' + FIRST + '},"comments":{}}'
ROOT_DUP_REVERSED = '{"schema_version":1,"comments":{},"comments":{"m-a":' + FIRST + '}}'
EQUAL_MAPS = '{"schema_version":1,"comments":{"m-a":' + FIRST + '},"comments":{"m-a":' + FIRST + '}}'
MEETING_DUP = ('{"schema_version":1,"comments":{"m-a":' + FIRST
               + ',"m-a":{"on":"2026-03-10","channel":"portal","recorded_at":"x"}}}')
ENTRY_DUP = ('{"schema_version":1,"comments":{"m-a":{"on":"2026-03-10","channel":"email",'
             '"channel":"portal","recorded_at":"x"}}}')
ESCAPED_DUP = ('{"schema_version":1,"comments":{"m-a":{"on":"2026-03-10","channel":"email",'
               '"chann\\u0065l":"portal","recorded_at":"x"}}}')
VERSION_DUP = '{"schema_version":2,"schema_version":1,"comments":{}}'
UNRELATED_DUP = ('{"schema_version":1,"comments":{"m-a":' + FIRST + ',"m-b":{"on":"2026-03-10","channel":"mail",'
                 '"recorded_at":"x","recorded_at":"y"}}}')
UNKNOWN_NESTED_DUP = ('{"schema_version":1,"comments":{"m-a":{"on":"2026-03-10","channel":"email","recorded_at":"x",'
                      '"extra":{"k":1,"k":1}}}}')
ALL_DUPLICATES = (ROOT_DUP, ROOT_DUP_REVERSED, EQUAL_MAPS, MEETING_DUP, ENTRY_DUP, ESCAPED_DUP, VERSION_DUP,
                  UNRELATED_DUP, UNKNOWN_NESTED_DUP)
STOPPED = "kit/comments-sent.json is not readable JSON (ContentError)"


class DuplicateMemberTests(unittest.TestCase):
    _base = fixture.MeetingCommentSentTests
    record, refused = _base.record, _base.refused
    deadlines, snapshot = _base.deadlines, _base.snapshot

    def setUp(self):
        self._base.setUp(self)
        self.sent.parent.mkdir(exist_ok=True)

    def put(self, text):
        self.sent.write_text(text, encoding="utf-8")
        os.chmod(self.sent, 0o600)
        return self.sent.read_bytes(), self.sent.stat().st_ino

    def test_duplicate_comments_cannot_bypass_replace(self):
        for text in (ROOT_DUP, ROOT_DUP_REVERSED, EQUAL_MAPS):
            before, inode = self.put(text)
            tree = self.snapshot()
            with self.assertRaises(meeting_comments.MeetingCommentError) as caught:
                meeting_comments.read(self.root)
            self.assertEqual(str(caught.exception), STOPPED)
            for replace in (False, True):
                with mock.patch.object(meeting_comments, "_letter") as letter, \
                        mock.patch.object(meeting_comments, "_write_json") as writer, \
                        mock.patch.object(meeting_comments, "_kit_folder") as kit:
                    self.refused(on="2026-03-12", channel="mail", fragment=STOPPED, replace=replace)
                for seam in (letter, writer, kit):
                    seam.assert_not_called()
            self.assertEqual(self.sent.read_bytes(), before)
            self.assertEqual(self.sent.stat().st_ino, inode)
            self.assertEqual(self.snapshot(), tree)

    def test_every_depth_refuses_whole_store_value_free(self):
        for text in ALL_DUPLICATES:
            self.put(text)
            with self.assertRaises(meeting_comments.MeetingCommentError) as caught:
                meeting_comments.read(self.root)
            self.assertEqual(str(caught.exception), STOPPED, text)
            self.assertIsNone(caught.exception.__cause__)

    def test_actual_cli_refuses_without_success_or_mutation(self):
        for flags in ((), ("--replace",)):
            before, _ = self.put(ROOT_DUP)
            tree = self.snapshot()
            code, out, err = fixture.run("meetings-comment-sent", "--directory", self.root, "--id", "m-a",
                                         "--on", "2026-03-12", "--channel", "mail", *flags)
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("comments-sent.json", err)
            self.assertNotIn("Recorded", err)
            self.assertNotIn("Traceback", err)
            self.assertEqual(self.sent.read_bytes(), before)
            self.assertEqual(self.snapshot(), tree)

    def test_today_keeps_deadlines_and_pack_falls_back(self):
        self.put(ROOT_DUP)
        items = today.todo(self.root, fixture.AS_OF)
        self.assertEqual(len([i for i in items if i["kind"] == "unreadable"]), 1)
        self.assertEqual(self.deadlines(), ["m-b", "m-a"])
        before = self.sent.read_bytes()
        self.assertIsNone(fixture.build(self.root, "m-a", today=fixture.NOW)["comment_sent"])
        self.assertEqual(self.sent.read_bytes(), before)

    def test_valid_stores_and_retry(self):
        distinct = ('{"schema_version":1,"comments":{"m-a":' + FIRST + ',"m-b":' + FIRST
                    + ',"caf\\u00e9":' + FIRST + ',"cafe\\u0301":' + FIRST + '}}')
        self.put(distinct)
        self.assertEqual(sorted(meeting_comments.read(self.root)), sorted(["m-a", "m-b", "caf\u00e9", "cafe\u0301"]))
        self.put(ROOT_DUP)
        self.refused(on="2026-03-12", fragment=STOPPED)
        self.sent.unlink()
        entry = self.record()
        self.assertEqual(meeting_comments.read(self.root), {"m-a": entry})
        self.assertEqual(self.sent.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.sent.read_text())["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
