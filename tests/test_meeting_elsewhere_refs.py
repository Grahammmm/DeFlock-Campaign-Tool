"""DF1107-06: a staged kit's cited decisions stay bound to content/elsewhere.json by identity; build and content-check name breakage.

Synthetic only; nothing is fetched or sent.
"""
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.content import ContentError, load_content, load_meeting
from campaign_tool.content_check import content_problems
from campaign_tool.meeting_stage import stage
from tests.test_meeting_stage_elsewhere import EXAMPLE, MID, entry

DAY = date(2026, 10, 1)


class RefTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-elsewhere-refs-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Example City Council", "starts_at": "2026-11-18T18:00:00-08:00"}]}), encoding="utf-8")
        self.file = self.root / "content" / "elsewhere.json"
        self.target = self.root / "content" / "meetings" / f"{MID}.json"
        self.entries = [entry(1), entry(2, kind="declined"), entry(3)]
        self.put(self.entries)
        stage(self.root, MID, elsewhere_indices=[2], today=DAY)
        self.kit = self.target.read_bytes()

    def put(self, entries):
        self.file.write_text(json.dumps(entries), encoding="utf-8")

    def test_reorder_changes_nothing(self):
        self.put(list(reversed(self.entries)))
        load_content(self.root, DAY)
        self.assertEqual(content_problems(self.root, today=DAY), [])
        self.assertEqual(self.target.read_bytes(), self.kit)

    def test_missing_entry_or_file_is_named_by_identity(self):
        self.put([self.entries[0], self.entries[2]])
        want = "meetings/%s.json.cited_elsewhere[0]: content/elsewhere.json has no entry for Springfield 2, Springfield City Council, 2026-03-10 (declined)" % MID
        with self.assertRaises(ContentError) as caught:
            load_content(self.root, DAY)
        self.assertIn(want, str(caught.exception))
        self.assertTrue(any(want in p for p in content_problems(self.root, today=DAY)))
        self.file.unlink()
        with self.assertRaisesRegex(ContentError, "cited_elsewhere.*not found"):
            load_content(self.root, DAY)
        self.assertTrue(any("cited_elsewhere" in p for p in content_problems(self.root, today=DAY)))
        self.put(self.entries)  # re-adding the exact entry repairs it without touching the kit
        load_content(self.root, DAY)
        self.assertEqual(self.target.read_bytes(), self.kit)

    def test_same_names_other_kind_is_a_different_decision(self):
        self.put([dict(self.entries[1], kind="other"), self.entries[0]])
        with self.assertRaisesRegex(ContentError, "no entry for Springfield 2"):
            load_content(self.root, DAY)

    def test_changed_printed_fact_refuses_but_checked_date_and_news_do_not(self):
        for field, value in (("as_stated", "Changed wording."), ("state", "WA"), ("source_url", "https://records.example.test/other")):
            with self.subTest(field=field):
                self.put([self.entries[0], dict(self.entries[1], **{field: value}), self.entries[2]])
                with self.assertRaisesRegex(ContentError, rf"changed in content/elsewhere.json \({field}\); restage"):
                    load_content(self.root, DAY)
        self.put([self.entries[0], dict(self.entries[1], checked_on="2026-09-30", news_url="https://news.example.test/a"), self.entries[2]])
        load_content(self.root, DAY)

    def test_replace_repairs_only_the_target_meeting(self):
        self.put([self.entries[0]])
        with self.assertRaises(ContentError):
            stage(self.root, MID, elsewhere_indices=[1], today=DAY)  # not --replace: the broken staged meeting still blocks
        stage(self.root, MID, replace=True, elsewhere_indices=[1], today=DAY)
        self.assertEqual(self.target.read_bytes() != self.kit, True)
        load_content(self.root, DAY)
        # another broken meeting is never skipped
        other = json.loads(self.target.read_text(encoding="utf-8"))
        other.update(id="other-meeting")
        other["cited_elsewhere"] = [self.entries[2]]
        (self.root / "content" / "meetings" / "other-meeting.json").write_text(json.dumps(other), encoding="utf-8")
        with self.assertRaises(ContentError) as caught:
            stage(self.root, MID, replace=True, elsewhere_indices=[1], today=DAY)
        self.assertIn("other-meeting.json.cited_elsewhere[0]", str(caught.exception))

    def test_snapshot_schema_is_strict(self):
        base = json.loads(self.target.read_text(encoding="utf-8"))
        good = base["cited_elsewhere"][0]
        for bad in ([], [good] * 2, [good, entry(5), entry(6), entry(7)], [dict(good, extra=1)], [{k: v for k, v in good.items() if k != "kind"}],
                    "x"):
            with self.subTest(bad=str(bad)[:40]), self.assertRaises(ContentError):
                load_meeting(dict(base, cited_elsewhere=bad), "meetings/x.json", today=DAY)
        self.assertEqual(load_meeting(base, "meetings/x.json", today=DAY)["cited_elsewhere"], [good])


if __name__ == "__main__":
    unittest.main()
