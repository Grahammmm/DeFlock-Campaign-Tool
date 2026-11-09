"""RF1345-4: private manual cancellation admission, synthetic and offline."""
import copy
import json
import os
import stat
import unittest
from datetime import datetime, timezone
from unittest import mock

from campaign_tool import meeting_speakers as ms
from campaign_tool.meeting_stage import stage
from campaign_tool.meetings_manual import cancel_manual
from tests.test_meetings_manual import ManualBase, MID, TODAY, REASON
from tests.test_meeting_speakers import run

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
BAD_MANUAL = "kit/meetings-manual.json is not valid private meeting JSON up to 4 MiB"


class ManualSpeakerTests(ManualBase):
    def setUp(self):
        super().setUp()
        offline = mock.patch("socket.socket.connect", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        self.add(starts_at="2099-01-20T18:30:00-08:00")
        stage(self.root, MID)
        (self.root / "public").mkdir()
        (self.root / "public" / "keep.html").write_text("Synthetic public page")
        (self.root / "kit" / "assignments.json").write_text('{"keep": 1}')

    def snapshot(self):
        result = {}
        for path in [self.root, *self.root.rglob("*")]:
            info = path.lstat()
            data = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            result[str(path.relative_to(self.root))] = (info.st_mode, info.st_mtime_ns, data)
        return result

    def actions(self, now=NOW):
        return [lambda: ms.meeting_points(self.root, MID, now=now),
                lambda: ms.list_plan(self.root, MID, now=now),
                lambda: ms.update_plan(self.root, MID, add="Ann", point="ask:1", now=now),
                lambda: ms.update_plan(self.root, MID, add="Bo", point="ask:1", now=now),
                lambda: ms.update_plan(self.root, MID, remove="Ann", now=now),
                lambda: ms.update_plan(self.root, MID, remove="Nobody", now=now)]

    def all_refused(self, message):
        before = self.snapshot()
        for number, action in enumerate(self.actions()):
            with self.subTest(action=number):
                with self.assertRaises(ms.MeetingSpeakersError) as caught:
                    action()
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(self.snapshot(), before)

    def cancel(self):
        cancel_manual(self.root, MID, on="2026-11-10", reason=REASON, today=TODAY)

    def test_manual_cancel_blocks_every_speaker_entrypoint(self):
        ms.update_plan(self.root, MID, add="Ann", point="ask:1", now=NOW)
        self.cancel()
        staged = self.root / "content" / "meetings" / f"{MID}.json"
        self.assertEqual(json.loads(staged.read_text()).get("status", "scheduled"), "scheduled")
        self.assertFalse(self.scan.exists())
        self.all_refused(ms.CANCELLED)
        # Cancellation still wins when the existing plan binding has gone stale.
        document = json.loads(staged.read_text())
        document["comment_kit_md"] += "\nChanged synthetic kit\n"
        staged.write_text(json.dumps(document))
        self.all_refused(ms.CANCELLED)

    def test_cancelled_manual_without_plan_creates_nothing(self):
        self.cancel()
        self.assertFalse((self.root / "kit" / "meetings").exists())
        self.all_refused(ms.CANCELLED)
        self.assertFalse((self.root / "kit" / "meetings").exists())

    def test_manual_scan_cancellation_parity_and_controls(self):
        scheduled = self.doc()
        expected = ms.list_plan(self.root, MID, now=NOW)
        for manual_status, scan_status in (("cancelled", None), (None, "cancelled"),
                                           ("cancelled", "scheduled"), ("scheduled", "cancelled")):
            with self.subTest(manual=manual_status, scan=scan_status):
                document = copy.deepcopy(scheduled)
                document["meetings"][0]["status"] = manual_status or "scheduled"
                self.manual.write_text(json.dumps(document))
                if manual_status is None:
                    self.manual.unlink()
                if scan_status:
                    self.write_scan([{"id": MID, "status": scan_status}])
                elif self.scan.exists():
                    self.scan.unlink()
                self.all_refused(ms.CANCELLED)
        unrelated = copy.deepcopy(scheduled)
        unrelated["meetings"][0].update(id="manual-unrelated", status="cancelled")
        for document in (scheduled, unrelated, None):
            if document is None:
                self.manual.unlink()
            else:
                self.manual.write_text(json.dumps(document))
            self.write_scan([{"id": "other", "status": "cancelled"}])
            self.assertEqual(ms.list_plan(self.root, MID, now=NOW), expected)
        self.scan.unlink()
        self.assertEqual(ms.list_plan(self.root, MID, now=NOW), expected)

    def test_manual_source_malformed_refuses_without_private_echo(self):
        ms.update_plan(self.root, MID, add="Ann", point="ask:1", now=NOW)
        good = self.doc()
        entry = good["meetings"][0]
        documents = [[], {**good, "schema_version": True}, {**good, "extra": REASON},
                     {**good, "meetings": {}}, {**good, "meetings": [entry, entry]}]
        for changes in ({"status": REASON}, {"history": REASON}, {"body": "<private>"},
                        {"history": [{"action": "cancel", "reason": REASON}]}, {"time_known": False}):
            # A malformed unrelated entry must also refuse.
            documents.append({**good, "meetings": [entry, {**entry, "id": "manual-other", **changes}]})
        raw_values = [b"\xff", b"{", b'{"schema_version":1,"schema_version":1,"meetings":[]}']
        raw_values += [json.dumps(document).encode() for document in documents]
        for raw in raw_values:
            with self.subTest(raw=raw[:80]):
                self.manual.write_bytes(raw)
                self.all_refused(BAD_MANUAL)

    def test_manual_source_paths_refuse_without_following(self):
        good = self.manual.read_bytes()
        outside = self.tmp / "outside.json"
        outside.write_bytes(good)
        frozen = outside.read_bytes(), outside.stat().st_mode, outside.stat().st_mtime_ns
        for kind in ("link", "dangling", "directory", "fifo", "hardlink", "oversized"):
            self.manual.unlink()
            if kind in ("link", "dangling"):
                self.manual.symlink_to(outside if kind == "link" else self.tmp / "absent")
            elif kind == "directory":
                self.manual.mkdir()
            elif kind == "fifo":
                os.mkfifo(self.manual)
            elif kind == "hardlink":
                os.link(outside, self.manual)
            else:
                self.manual.write_bytes(b" " * (ms.MAX_STAGED_BYTES + 1))
            with self.subTest(kind=kind):
                self.all_refused(ms.SYMLINK if kind in ("link", "dangling") else BAD_MANUAL)
                self.assertEqual((outside.read_bytes(), outside.stat().st_mode, outside.stat().st_mtime_ns), frozen)
            if kind == "directory":
                self.manual.rmdir()
            else:
                self.manual.unlink()
            self.manual.write_bytes(good)
        # Ancestor links are refused before any source or plan is read.
        for path in (self.root / "kit", self.root, self.tmp):
            moved = path.with_name(path.name + "-real")
            path.rename(moved)
            path.symlink_to(moved, target_is_directory=True)
            try:
                for action in self.actions():
                    with self.assertRaises(ms.MeetingSpeakersError) as caught:
                        action()
                    self.assertEqual(str(caught.exception), ms.SYMLINK)
            finally:
                path.unlink()
                moved.rename(path)
        self.assertFalse((self.root / "kit" / "meetings").exists())

    def test_manual_cancel_cli_actions_are_stopped(self):
        args = ("meetings-speakers", "--directory", self.root, "--id", MID)
        code, out, err = run(*args, "--add", "Ann", "--point", "ask:1")
        self.assertEqual((code, json.loads(out), err.strip()),
                         (0, {"id": MID, "changed": True, "assignments": 1}, "Recorded only. Nothing was sent."))
        self.cancel()
        before = self.snapshot()
        for action in (("--list",), ("--add", "Ann", "--point", "ask:1"), ("--remove", "Ann")):
            self.assertEqual(run(*args, *action), (1, "", "Stopped: " + ms.CANCELLED + "\n"))
            self.assertEqual(self.snapshot(), before)

    def test_staged_precedence_remains(self):
        self.manual.write_bytes(b"{")
        target = self.root / "content" / "meetings" / f"{MID}.json"
        document = json.loads(target.read_text())
        document["status"] = "cancelled"
        target.write_text(json.dumps(document))
        self.all_refused(ms.CANCELLED)
        target.unlink()
        self.all_refused(ms.NOT_STAGED)

    def test_scheduled_manual_start_instant_and_both_source_validation(self):
        start = datetime.fromisoformat(self.doc()["meetings"][0]["starts_at"])
        before = self.snapshot()
        for action in self.actions(now=start):
            with self.assertRaises(ms.MeetingSpeakersError) as caught:
                action()
            self.assertEqual(str(caught.exception), ms.PAST)
        self.assertEqual(self.snapshot(), before)
        # A valid manual entry cannot hide an invalid present scan.
        self.write_scan([], raw=b"{")
        self.all_refused("kit/meetings.json is not a regular UTF-8 JSON file up to 4 MiB")
        self.write_scan([{"id": MID, "status": "scheduled"}])
        self.manual.write_bytes(b"{")
        self.all_refused(BAD_MANUAL)


if __name__ == "__main__":
    unittest.main()
