"""RF1810-2: `meetings-stage --participation FILE` refuses a repeated JSON object member name.

Synthetic data only; no network. Run: python3 -B -m unittest tests.test_meeting_participation_duplicate_members
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from campaign_tool.meeting_stage import MeetingStageError, _read_participation, stage
from tests.test_meeting_participation_read_safety import (EXAMPLE, FULL, MID, REFUSAL, STARTS, TODAY,
                                                          snapshot)


class DuplicateMemberTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-participation-dup-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()

    def put(self, data):
        path = self.tmp / "participation.json"
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def refusal(self, data):
        with self.assertRaises(MeetingStageError) as caught:
            _read_participation(str(self.put(data)))
        return str(caught.exception)

    def test_participation_top_level_repeat_is_refused(self):
        self.assertEqual(self.refusal('{"a": 1, "a": 2}'), REFUSAL)

    def test_participation_nested_and_escaped_repeat_are_refused(self):
        self.assertEqual(self.refusal('{"x": {"k": 1, "k": 2}}'), REFUSAL)
        self.assertEqual(self.refusal('{"a": 1, "\\u0061": 2}'), REFUSAL)

    def test_refusal_names_no_member_or_path(self):
        text = self.refusal('{"SECRET-MEMBER-NAME": 1, "SECRET-MEMBER-NAME": 2}')
        self.assertNotIn("SECRET-MEMBER-NAME", text)
        self.assertNotIn(str(self.tmp), text)

    def test_participation_valid_siblings_and_case_variants_unchanged(self):
        path = self.put('{"a": [{"k": 1}, {"k": 2}], "A": 3}')
        self.assertEqual(_read_participation(str(path)), {"a": [{"k": 1}, {"k": 2}], "A": 3})

    def test_stage_with_repeated_member_participation_writes_nothing(self):
        root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
        (root / "kit").mkdir(exist_ok=True)
        (root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Example City Council", "starts_at": STARTS, "agenda_item": "Item 7: ALPR",
             "agenda_url": "https://example.invalid/agenda"}]}), encoding="utf-8")
        body = json.dumps(FULL)
        repeated = self.put(body[:-1] + ', "in_person": false}')
        before = snapshot(root)
        with self.assertRaises(MeetingStageError) as caught:
            stage(root, MID, participation=str(repeated), today=TODAY)
        self.assertEqual(str(caught.exception), REFUSAL)
        self.assertEqual(snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
