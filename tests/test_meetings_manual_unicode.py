"""RF1201-2: surrogate code points in manual meeting text refuse at admission (synthetic, offline)."""
import contextlib
import io
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

from campaign_tool import cli
from campaign_tool import meeting_speakers as ms
from campaign_tool import meetings_manual as mm
from campaign_tool.meeting_stage import MeetingStageError, stage
from campaign_tool.meetings_manual import ManualMeetingError, cancel_manual, read_manual, read_meetings
from tests.test_meetings_manual import BASE, MID, REASON, TODAY, ManualBase

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
FUTURE = "2099-01-20T18:30:00-08:00"
CANARY = "Private-canary-"
FIXED = "must be plain text without angle brackets or control characters"
SURROGATES = ("\ud800", "\udbff", "\udc00", "\udfff", "\ud83d\ude00")  # last: adjacent high+low code points


def placed(code_point, where):
    return {"begin": code_point + "tail", "middle": "head" + code_point + "tail", "end": CANARY + code_point}[where]


def run_cli(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class UnicodeBase(ManualBase):
    def snapshot(self):
        result = {}
        for path in [self.root, *sorted(self.root.rglob("*"))]:
            info = path.lstat()
            data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
            result[str(path.relative_to(self.root))] = (info.st_mode, info.st_mtime_ns, data)
        return result

    def tripwire(self):
        patcher = mock.patch.object(mm, "_save", side_effect=AssertionError("writer reached"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def store(self, mutate):
        """Rewrite the saved store as ASCII-escaped JSON after mutating the decoded document."""
        document = self.doc()
        mutate(document)
        self.manual.write_text(json.dumps(document, indent=2, ensure_ascii=True) + "\n", encoding="ascii")

    def assert_refused(self, call):
        with self.assertRaises(ManualMeetingError) as caught:
            call()
        self.assertNotIsInstance(caught.exception, UnicodeError)
        return str(caught.exception)


class DirectAdmissionTests(UnicodeBase):
    def test_direct_manual_text_surrogates_refuse_before_save(self):
        self.add(starts_at=FUTURE)
        self.tripwire()
        before = self.snapshot()
        for code_point in SURROGATES:
            for where in ("begin", "middle", "end"):
                text = placed(code_point, where)
                for field in ("body", "agenda_item"):
                    new = self.assert_refused(lambda: self.add("manual-new", starts_at=FUTURE, **{field: text}))
                    self.assertTrue(new.endswith(FIXED), new)
                    self.assert_refused(lambda: self.replace(starts_at=FUTURE, **{field: text}))
                self.assert_refused(lambda: self.replace(starts_at=FUTURE, reason=text))
                self.assert_refused(lambda: cancel_manual(self.root, MID, on="2026-11-10", reason=text, today=TODAY))
                for message in (new,):
                    self.assertNotIn(CANARY, message)
        self.assertEqual(self.snapshot(), before)

    def test_unicode_scalar_lifecycle_control_is_byte_compatible(self):
        pair = json.loads('"\\ud83d\\ude00"')  # a proper JSON escaped pair decodes to one supplementary scalar
        self.assertEqual(len(pair), 1)
        text = "Caf\u00e9 \u6c34 e\u0301 \\ud800 " + pair
        self.add(body=text, agenda_item="Item \\u00e9 " + pair, starts_at=FUTURE)
        entry = read_manual(self.root)[0]
        self.assertEqual((entry["body"], entry["agenda_item"]), (text, "Item \\u00e9 " + pair))
        self.replace(body=text, reason="R\u00e9sum\u00e9 \\ud800 " + pair, starts_at=FUTURE)
        cancel_manual(self.root, MID, on="2026-11-10", reason="Notice " + pair, today=TODAY)
        self.replace(body=text, reason="Back \u6c34", starts_at=FUTURE)
        document = self.doc()
        entry = document["meetings"][0]
        self.assertEqual(entry["status"], "scheduled")
        self.assertEqual([h["action"] for h in entry["history"]], ["replace", "cancel", "replace"])
        self.assertEqual(entry["history"][0]["reason"], "R\u00e9sum\u00e9 \\ud800 " + pair)
        self.assertEqual(self.manual.read_text(encoding="utf-8"),
                         json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False) + "\n")
        self.assertEqual(mm._manual_document(document), read_manual(self.root))
        stage(self.root, MID)
        self.assertIsInstance(ms.list_plan(self.root, MID, now=NOW), dict)


class StoredTextTests(UnicodeBase):
    def make(self):
        self.add(starts_at=FUTURE)
        self.add("manual-sibling-a", starts_at=FUTURE, body="Sibling A")
        self.replace(starts_at=FUTURE, reason="Earlier fix")
        self.add("manual-sibling-z", starts_at=FUTURE, body="Sibling Z")

    def slots(self):
        def entry(d):
            return next(e for e in d["meetings"] if e["id"] == MID)

        return {
            "body": lambda d: entry(d).__setitem__("body", placed("\ud800", "end")),
            "agenda_item": lambda d: entry(d).__setitem__("agenda_item", placed("\udfff", "middle")),
            "history reason": lambda d: entry(d)["history"][0].__setitem__("reason", placed("\udc00", "end")),
            "before body": lambda d: entry(d)["history"][0]["before"].__setitem__("body", placed("\udbff", "begin")),
            "before agenda_item": lambda d: entry(d)["history"][0]["before"].__setitem__("agenda_item", placed("\ud800", "end")),
            "unrelated sibling": lambda d: d["meetings"][0].__setitem__("body", placed("\ud800", "end")),
        }

    def test_stored_surrogates_refuse_at_every_text_slot(self):
        for name, mutate in self.slots().items():
            with self.subTest(slot=name):
                self.make()
                self.store(mutate)
                self.assertIn("\\ud", self.manual.read_text(encoding="ascii").lower())
                before = self.snapshot()
                document = json.loads(self.manual.read_text(encoding="utf-8"))
                self.assert_refused(lambda: mm._manual_document(document))
                self.assert_refused(lambda: read_manual(self.root))
                self.assert_refused(lambda: read_meetings(self.root, allow_empty=True))
                self.assertEqual(self.snapshot(), before)
                self.manual.unlink()
                (self.root / "content" / "meetings" / f"{MID}.json").unlink(missing_ok=True)

    def test_history_reason_keeps_existing_wrapper(self):
        self.make()
        self.store(self.slots()["history reason"])
        self.assertIn("has an invalid reason", self.assert_refused(lambda: read_manual(self.root)))


class LifecycleAndCliTests(UnicodeBase):
    def entry_args(self, **over):
        values = {**BASE, "starts_at": FUTURE, "checked_on": "2026-01-01", **over}
        return ["--body", values["body"], "--agency-id", values["agency_id"], "--starts-at", values["starts_at"],
                "--agenda-url", values["agenda_url"], "--item-title", values["agenda_item"],
                "--source-url", values["source_url"], "--checked-on", values["checked_on"]]

    def check_cli(self, args):
        before = self.snapshot()
        code, out, err = run_cli("meetings-add", "--directory", self.root, *args)
        self.assertEqual((code, out), (1, ""), err)
        self.assertIn("Stopped:", err)
        for leak in (CANARY, "UnicodeEncodeError", "codec", "Traceback", "Recorded only"):
            self.assertNotIn(leak, err)
        self.assertEqual(self.snapshot(), before)

    def test_lifecycle_and_cli_preserve_tree_on_bad_text(self):
        bad = CANARY + "\ud800"
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "keep.txt").write_text("unrelated")
        # absent store
        self.check_cli(["--id", MID, *self.entry_args(body=bad)])
        self.check_cli(["--id", MID, *self.entry_args(agenda_item=bad)])
        self.assertFalse(self.manual.exists())
        # existing store
        code, _, err = run_cli("meetings-add", "--directory", self.root, "--id", MID, *self.entry_args())
        self.assertEqual(code, 0, err)
        stage(self.root, MID)
        self.check_cli(["--id", MID, *self.entry_args(body=bad), "--replace", "--on", "2026-11-10", "--reason", REASON])
        self.check_cli(["--id", MID, *self.entry_args(), "--replace", "--on", "2026-11-10", "--reason", bad])
        self.check_cli(["--id", MID, "--cancel", "--on", "2026-11-10", "--reason", bad])
        self.check_cli(["--id", "manual-two", *self.entry_args(agenda_item=bad)])


class StageAndSpeakerTests(UnicodeBase):
    def setUp(self):
        super().setUp()
        self.add(starts_at=FUTURE)
        self.replace(starts_at=FUTURE, reason="Earlier fix")
        stage(self.root, MID)
        self.store(lambda d: d["meetings"][0]["history"][0].__setitem__("reason", CANARY + "\ud800"))  # unrelated slot

    def test_stage_and_speaker_reject_bad_manual_text(self):
        before = self.snapshot()
        with self.assertRaises(MeetingStageError):
            stage(self.root, MID, replace=True)
        with self.assertRaises(MeetingStageError):
            stage(self.root, MID)
        self.assertEqual(self.snapshot(), before)
        actions = [lambda: ms.meeting_points(self.root, MID, now=NOW),
                   lambda: ms.list_plan(self.root, MID, now=NOW),
                   lambda: ms.update_plan(self.root, MID, add="Ann", point="ask:1", now=NOW),
                   lambda: ms.update_plan(self.root, MID, remove="Ann", now=NOW)]
        for action in actions:
            with self.assertRaises(ms.MeetingSpeakersError) as caught:
                action()
            self.assertEqual(str(caught.exception), ms.BAD_MANUAL)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
