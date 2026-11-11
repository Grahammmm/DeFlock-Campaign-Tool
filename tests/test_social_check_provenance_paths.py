"""RF0421-3: sidecar provenance keys never select Markdown paths in suggested folders (synthetic only).

Run from the repository root: python3 -B -m unittest tests.test_social_check_provenance_paths -q
"""
import json
import os
import unittest
from unittest import mock

from campaign_tool import social_check as SC
from campaign_tool.social import build_meeting_social, build_social
from tests.test_social_check_folders import NOW, FolderCheckCase

CANARY = "SYNTHETIC-OUTSIDE-CANARY"


class ProvenancePaths(FolderCheckCase):
    def suggested_fixture(self):
        """Edited primary + second build so meetings.suggested has a real draft and sidecar."""
        self.meeting()
        build_social(self.root)
        build_meeting_social(self.root, now=NOW)
        primary = self.social / "meetings" / "m-one.md"
        primary.write_text(primary.read_text(encoding="utf-8") + " (organizer note)", encoding="utf-8")
        self.meeting(starts="2026-11-15T10:00:00-08:00")
        build_meeting_social(self.root, now=NOW)
        folder = self.social / "meetings.suggested"
        self.assertTrue((folder / "m-one.md").is_file())
        return folder

    def add_keys(self, folder, keys):
        sidecar = folder / ".check.json"
        doc = json.loads(sidecar.read_text(encoding="utf-8"))
        entry = doc["drafts"]["m-one"]
        for key in keys:
            doc["drafts"][key] = dict(entry)
        sidecar.write_text(json.dumps(doc), encoding="utf-8")

    def opened_markdown(self):
        opened = []
        real_open = os.open

        def spy(path, *args, **kwargs):
            if str(path).endswith(".md"):
                opened.append(str(path))
            return real_open(path, *args, **kwargs)

        patcher = mock.patch.object(SC.os, "open", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return opened

    def test_suggestion_metadata_nonmembers_never_acquire_markdown(self):
        folder = self.suggested_fixture()
        (self.social / "sibling-canary.md").write_text(CANARY, encoding="utf-8")
        outside = self.root / "outside-canary.md"
        outside.write_text(CANARY, encoding="utf-8")
        (folder / "bridge").symlink_to(self.root)  # linked intermediate directory
        baseline = self.folder("meetings.suggested")
        self.add_keys(folder, ["../sibling-canary", str(self.root / "outside-canary"), "bridge/outside-canary", "not-present"])
        opened = self.opened_markdown()
        report = self.folder("meetings.suggested")
        self.assertEqual(report, baseline)
        self.assertEqual(report["problems"], 0)
        self.assertEqual([p for p in opened if "canary" in p or "bridge" in p or "not-present" in p], [])
        self.assertEqual(len([p for p in opened if p.endswith("meetings.suggested/m-one.md")]), 1)
        self.assertNotIn(CANARY, json.dumps(report))

    def test_local_suggestion_is_read_once_and_rechecked_next_call(self):
        folder = self.suggested_fixture()
        self.add_keys(folder, ["../sibling-canary"])
        opened = self.opened_markdown()
        self.assertEqual(self.states("meetings.suggested"), {"m-one": ("ok", [])})
        self.assertEqual(len([p for p in opened if p.endswith("meetings.suggested/m-one.md")]), 1)
        draft = folder / "m-one.md"
        draft.write_text(draft.read_text(encoding="utf-8") + " edited", encoding="utf-8")
        self.assertEqual(self.states("meetings.suggested")["m-one"][0], "outdated")

    def test_primary_missing_draft_still_contributes_custom_window(self):
        self.meeting()
        build_social(self.root)
        build_meeting_social(self.root, now=NOW)
        sidecar = self.social / "meetings" / ".check.json"
        doc = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(doc["drafts"]["m-one"]["within_days"], 14)
        with_draft = self.folder("meetings")
        (self.social / "meetings" / "m-one.md").unlink()
        missing = self.folder("meetings")
        self.assertEqual([e["state"] for e in with_draft["drafts"]], ["ok"])
        self.assertEqual([e["state"] for e in missing["drafts"]], ["missing"])


if __name__ == "__main__":
    unittest.main()
