"""Synthetic tests: meetings-status refuses repeated JSON members like malformed JSON."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool import meetings_status
from campaign_tool.meetings_status import _Bad, _read, inventory


class DuplicateMemberTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-meetings-dup-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def read(self, text):
        path = self.dir / "x.json"
        path.write_text(text, encoding="utf-8")
        return _read(str(path), 4096)

    def test_read_refuses_top_level_repeat(self):
        with self.assertRaises(_Bad):
            self.read('{"a": 1, "a": 2}')

    def test_read_refuses_nested_repeat_in_array_object(self):
        with self.assertRaises(_Bad):
            self.read('{"rows": [{"k": 1, "k": 2}]}')

    def test_read_refuses_escaped_equivalent_repeat(self):
        with self.assertRaises(_Bad):
            self.read('{"a": 1, "\\u0061": 2}')

    def test_read_accepts_same_name_in_sibling_objects_and_case_variants(self):
        self.assertEqual(self.read('[{"a": 1}, {"a": 2}]'), [{"a": 1}, {"a": 2}])
        self.assertEqual(self.read('{"a": 1, "A": 2}'), {"a": 1, "A": 2})

    def test_inventory_reports_repeated_member_meeting_as_unreadable(self):
        meetings = self.dir / "content" / "meetings"
        meetings.mkdir(parents=True)
        good = '{"id": "m1", "body": "B", "starts_at": "2026-11-20T18:00:00-08:00", "agenda_item": "Item 1"%s}'
        (meetings / "m1.json").write_text(good % ', "body": "C"', encoding="utf-8")
        (meetings / "m2.json").write_text("{", encoding="utf-8")
        (meetings / "m3.json").write_text(good.replace("m1", "m3") % "", encoding="utf-8")
        report = inventory(self.dir, "2026-11-09", directory="D", today=date(2026, 11, 9))
        rows = {row["path"]: row for row in report["rows"]}
        self.assertNotEqual(rows["content/meetings/m3.json"]["kind"], "unreadable")  # control
        self.assertEqual(rows["content/meetings/m1.json"]["kind"], "unreadable")
        self.assertEqual(rows["content/meetings/m1.json"]["error"], rows["content/meetings/m2.json"]["error"])
        self.assertIs(meetings_status._Bad, _Bad)


if __name__ == "__main__":
    unittest.main()
