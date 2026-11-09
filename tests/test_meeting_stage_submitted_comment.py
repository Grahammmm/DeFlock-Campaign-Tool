"""RF0834-3: `meetings-stage --replace` keeps the exact submitted comment (and the verified outcome) it regenerates around.

Synthetic temp copies of the fictional example only; no network, nothing is sent or published.
Run from the repository root: python3 -B -m unittest tests.test_meeting_stage_submitted_comment -v
"""
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import meeting_comment_publish, meeting_comments
from campaign_tool.content import ContentError, load_content
from campaign_tool.meeting_outcome import record_outcome
from campaign_tool.meeting_stage import MeetingStageError, stage
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
MID = "cedar-council-2026-10"
TODAY = date(2026, 10, 20)
SOURCE = "https://example.invalid/cedar-council/minutes"
LETTER = "Dear Council,\r\nPlease publish the policy. Caf\u00e9 \u2014 thanks.\r\n"


def snapshot(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


class RestageSubmittedCommentTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-restage-comment-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.target = self.meetings / f"{MID}.json"
        (self.meetings / "2026-10-cedar-council.json").rename(self.target)  # filename = id, as `meetings-stage` writes
        self.letter = self.tmp / "letter.txt"
        self.letter.write_bytes(LETTER.encode("utf-8"))
        self.write_kit()

    def write_kit(self, body="Cedar City Council (fictional)", status=None):
        entry = {"id": MID, "body": body, "starts_at": "2026-10-14T18:00:00-07:00", "agenda_item": "Item 7: renewals"}
        if status:
            entry["status"] = status
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [entry]}),
                                                         encoding="utf-8")

    def restage(self, **kwargs):
        return stage(self.root, MID, replace=True, today=TODAY, **kwargs)

    def submit(self, channel="email"):
        """The real flow: record the hand-sent letter, then stage its exact hash-bound copy with signer consent."""
        meeting_comments.record(self.root, MID, "2026-10-10", channel, self.letter, replace=True, today=TODAY)
        return meeting_comment_publish.publish(self.root, MID, self.letter, signer_public=True, replace=True,
                                               today=TODAY)[0]

    def stored(self):
        return json.loads(self.target.read_text(encoding="utf-8"))

    def private_bytes(self):
        return (self.root / "kit" / "comments-sent.json").read_bytes()

    def test_real_record_publish_restage_keeps_exact_submission(self):
        self.restage()
        for channel in meeting_comments.CHANNELS:
            comment = self.submit(channel)
            self.write_kit(body=f"Changed generated body for {channel}")
            before_kit = self.stored()["comment_kit_md"]
            private = self.private_bytes()
            report = self.restage()
            after = self.stored()
            self.assertTrue(report["replaced"])
            self.assertEqual(after["submitted_comment"], comment)  # on, channel, text and digest as stored
            self.assertEqual(after["submitted_comment"]["text"].encode("utf-8"), self.letter.read_bytes())  # CRLF, UTF-8
            self.assertEqual(after["submitted_comment"]["sha256"], hashlib.sha256(self.letter.read_bytes()).hexdigest())
            self.assertIn(f"Changed generated body for {channel}", after["comment_kit_md"])  # draft regenerated
            self.assertNotEqual(after["comment_kit_md"], before_kit)
            self.assertEqual(self.private_bytes(), private)  # private receipt untouched
            self.assertFalse((self.root / "public").exists())  # stage never builds
            model = load_content(self.root, today=TODAY)
            loaded = [m for m in model["meetings"] if m["id"] == MID][0]
            self.assertEqual(loaded["submitted_comment"], comment)

    def test_restage_preserves_submission_and_full_outcome(self):
        self.restage()
        comment = self.submit()
        outcome = record_outcome(self.root, MID, "continued", "2026-10-14", SOURCE, "Synthetic Organizer", "4-1",
                                 today=TODAY)
        self.write_kit(body="Regenerated once")
        self.restage()
        first = self.target.read_bytes()
        document = self.stored()
        self.assertEqual(document["submitted_comment"], comment)
        self.assertEqual(document["outcome"], outcome)
        self.restage()  # repeated replacement is stable
        self.assertEqual(self.target.read_bytes(), first)
        public = self.root / "public"
        build_site(self.root, today=TODAY)  # scratch-only build
        page = (public / "meetings" / f"{MID}.html").read_bytes().decode("utf-8")
        self.assertIn('id="submitted-comment"', page)
        self.assertIn("meeting-outcome", page)
        export = json.loads((public / "data" / "meetings.json").read_text(encoding="utf-8"))
        rows = export["meetings"] if isinstance(export, dict) else export
        shown = [row for row in rows if row.get("id") == MID][0]
        self.assertEqual(shown["submitted_comment"], {"on": comment["on"], "channel": comment["channel"]})

    def test_portable_comment_survives_without_private_receipt_or_letter(self):
        self.restage()
        comment = self.submit()
        (self.root / "kit" / "comments-sent.json").unlink()
        self.letter.unlink()
        self.restage()
        self.assertEqual(self.stored()["submitted_comment"], comment)
        self.assertFalse((self.root / "kit" / "comments-sent.json").exists())  # nothing re-approved or recreated

    def test_absent_submission_stays_absent_and_plain_replacement_is_unchanged(self):
        self.target.unlink()
        stage(self.root, MID, today=TODAY)
        first = self.target.read_bytes()
        self.assertNotIn("submitted_comment", self.stored())
        with self.assertRaises(MeetingStageError):
            stage(self.root, MID, today=TODAY)  # no --replace still refuses
        self.assertEqual(self.target.read_bytes(), first)
        self.restage()
        self.assertEqual(self.target.read_bytes(), first)
        self.assertNotIn("submitted_comment", self.stored())

    def test_invalid_preserved_comment_refuses_without_data_loss(self):
        self.restage()
        comment = self.submit()
        good = self.stored()
        bad_comments = (
            {**comment, "sha256": "0" * 64},
            {**comment, "text": comment["text"] + "x"},
            {**comment, "on": "2099-01-01"},
            {**comment, "extra": "key"},
        )
        for bad in bad_comments:
            self.target.write_text(json.dumps({**good, "submitted_comment": bad}), encoding="utf-8")
            before, private = self.target.read_bytes(), self.private_bytes()
            with self.assertRaises(ContentError):
                self.restage()
            self.assertEqual(self.target.read_bytes(), before)  # not dropped to make the document valid
            self.assertEqual(self.private_bytes(), private)
            self.assertFalse((self.root / "public").exists())

    def test_refused_or_failed_restage_keeps_submission(self):
        self.restage()
        comment = self.submit()
        self.write_kit(body="Never written")
        before, private = self.target.read_bytes(), self.private_bytes()
        # a failing atomic write keeps the prior target and leaves no temporary file
        with mock.patch("campaign_tool.kit.os.replace", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                self.restage()
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.stored()["submitted_comment"], comment)
        self.assertEqual([p.name for p in self.meetings.iterdir() if p.name.endswith(".tmp")], [])
        # a duplicate id in another file is refused
        other = self.meetings / "other.json"
        other.write_text(json.dumps({"id": MID, "body": "B", "starts_at": "2026-10-14T18:00:00-07:00"}), encoding="utf-8")
        with self.assertRaisesRegex((MeetingStageError, ContentError), "already used|duplicate meeting id"):
            self.restage()
        other.unlink()
        self.assertEqual(self.target.read_bytes(), before)
        # a cancelled scan entry is refused
        self.write_kit(status="cancelled")
        with self.assertRaisesRegex(MeetingStageError, "cancelled"):
            self.restage()
        self.assertEqual(self.target.read_bytes(), before)
        self.write_kit(body="Never written")
        # a symlinked meetings folder is refused and its external bytes stay put
        outside = self.tmp / "outside-meetings"
        shutil.move(str(self.meetings), outside)
        os.symlink(outside, self.meetings)
        external = snapshot(outside)
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            self.restage()
        self.assertEqual(snapshot(outside), external)
        os.unlink(self.meetings)
        shutil.move(str(outside), self.meetings)
        # a symlinked target is refused and the linked file keeps its bytes
        linked = self.tmp / "linked.json"
        shutil.move(str(self.target), linked)
        os.symlink(linked, self.target)
        with self.assertRaisesRegex((MeetingStageError, ContentError), "symlink"):
            self.restage()
        self.assertEqual(linked.read_bytes(), before)
        self.assertEqual(self.private_bytes(), private)
        self.assertFalse((self.root / "public").exists())


if __name__ == "__main__":
    unittest.main()
