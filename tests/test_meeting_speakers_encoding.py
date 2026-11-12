"""RF1651-3: a staged comment kit holding a decoded lone surrogate is refused by every speaker-planning action.
Synthetic, offline.

Run from the repository root: python3 -B -m unittest tests.test_meeting_speakers_encoding -q
"""
import hashlib
import json
import unittest
from unittest import mock

from campaign_tool import meeting_speakers as ms
from tests.test_meeting_speakers import KIT, NOW, START, Base, run

FIXED = "content/meetings/m-a.json is not valid staged meeting content"
SURROGATES = ("\ud800", "\udbff", "\udc00", "\udfff")


class EncodingBase(Base):
    def stage_raw(self, kit, **extra):
        # json.dumps escapes the surrogate, so the file is valid UTF-8 and only the decoded text is unencodable.
        return self.stage(kit=kit, **extra)

    def apis(self):
        r = self.root
        return {"points": lambda: ms.meeting_points(r, "m-a", now=NOW),
                "list": lambda: ms.list_plan(r, "m-a", now=NOW),
                "add": lambda: ms.update_plan(r, "m-a", add="Ann", point="ask:1", now=NOW),
                "remove": lambda: ms.update_plan(r, "m-a", remove="Ann", now=NOW),
                "unknown_remove": lambda: ms.update_plan(r, "m-a", remove="Nobody", now=NOW)}

    def assert_refused(self, call, label):
        with self.assertRaises(ms.MeetingSpeakersError) as caught:
            call()
        self.assertEqual(str(caught.exception), FIXED, label)
        self.assertTrue(caught.exception.__suppress_context__, label)
        self.assertIsNone(caught.exception.__cause__, label)


class RefusalTests(EncodingBase):
    def test_decoded_surrogates_refuse_all_actions_and_positions(self):
        for char in SURROGATES:
            for where, kit in (("prose", KIT.replace("Hello.", "Hel" + char + "lo.")),
                               ("ask", KIT.replace("- Pause the contract\n", "- Pause" + char + "\n", 1)),
                               ("sources", KIT + "- bad " + char + "\n"),
                               ("first", char + KIT), ("last", KIT + char)):
                self.stage_raw(kit)
                for name, call in self.apis().items():
                    self.assert_refused(call, f"{name} {where} {ord(char):x}")
                self.assertFalse(self.store().exists())

    def test_refuses_before_plan_read_and_write(self):
        self.stage_raw(KIT + "\udc00")
        with mock.patch.object(ms, "read_plan", side_effect=AssertionError("read")), \
                mock.patch.object(ms, "_write", side_effect=AssertionError("write")):
            for name, call in self.apis().items():
                self.assert_refused(call, name)

    def test_populated_plan_and_external_recovery(self):
        self.add("Ann", "ask:1")
        before = self.tree()
        self.stage_raw(KIT.replace("Hello.", "\ud800"))
        bad_tree = self.tree()
        for name, call in self.apis().items():
            self.assert_refused(call, name)
        self.assertEqual(self.tree(), bad_tree)
        self.stage(kit=KIT)
        self.assertEqual(self.tree()["kit/meetings/speakers/m-a.json"], before["kit/meetings/speakers/m-a.json"])
        self.assertEqual(ms.update_plan(self.root, "m-a", remove="Ann", now=NOW)["assignments"], 0)

    def test_cli_has_one_safe_stopped_line(self):
        self.stage(starts_at="2099-01-01T10:00:00-08:00", kit=KIT + "\ud800")
        base = ("meetings-speakers", "--directory", self.root, "--id", "m-a")
        for more in (("--list",), ("--add", "Ann", "--point", "ask:1"), ("--remove", "Ann")):
            code, out, err = run(*base, *more)
            self.assertEqual((code, out), (1, ""), more)
            self.assertEqual(err, f"Stopped: {FIXED}\n", more)
        self.assertFalse(self.store().exists())


class PreservationTests(EncodingBase):
    def test_valid_scalars_pairs_and_literal_escapes_still_work(self):
        for kit in (KIT.replace("Hello.", "Hi \U0001F600 pair"), KIT.replace("Hello.", "caf\u00e9 \u4e2d\u6587 e\u0301"),
                    KIT.replace("Hello.", "literal \\ud800 text")):
            self.stage(kit=kit)
            self.assertEqual(self.add("Ann", "ask:1")["assignments"], 1)
            plan = json.loads(self.store().read_text(encoding="utf-8"))
            self.assertIn(hashlib.sha256(kit.encode("utf-8")).hexdigest(), json.dumps(plan))
            self.assertEqual(ms.update_plan(self.root, "m-a", remove="Ann", now=NOW)["assignments"], 0)

    def test_earlier_gates_keep_precedence(self):
        self.stage(kit=KIT + "\ud800", status="cancelled")
        self.refused(ms.CANCELLED, ms.list_plan, "m-a", now=NOW)
        self.stage(kit=KIT + "\ud800", starts_at="2026-12-01T10:00:00-08:00")
        self.refused(ms.PAST, ms.list_plan, "m-a", now=NOW)

    def test_changed_kit_is_still_stale(self):
        self.add("Ann", "ask:1")
        self.stage(kit=KIT.replace("Hello.", "Changed."))
        with self.assertRaises(ms.MeetingSpeakersError) as caught:
            ms.list_plan(self.root, "m-a", now=NOW)
        self.assertIn("stale", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
