"""Saved MuckRock timestamps are validated whole before their written date is mapped. Offline, synthetic."""
import json
import unittest
from unittest import mock

from campaign_tool import kit_muckrock, kit_track
from tests import test_kit_muckrock as base

FIELD_ERROR = "datetime_updated must be an ISO 8601 date or datetime"
GOOD = ["2026-03-10", "2026-03-10T14:30", "2026-03-10t14:30:00", "2026-03-10 14:30:00Z", "2026-03-10T14:30:00.5",
        "2026-03-10T23:59:59.123456789+05:30", "2026-03-10T00:00:00-08:00", "2024-02-29T01:02:03Z"]
BAD = ["2026-03-10garbage", "2026-03-10T25:99:99Z", "2026-03-10T14:30:00+99:99", "2026-03-10\nPRIVATE-MARKER",
       "2026-03-10T14:30:00+01:99", "2026-03-10T14:30:00\n", " 2026-03-10", "2026-03-10 ", "2026-03-10T", "2026-03-10T14",
       "2026-03-10T14:30:00.", "2026-03-10T14:30.5", "2026-03-10T24:00:00", "2026-03-10T14:60:00", "2026-03-10T14:30:60",
       "2026-03-10T14:30:00z", "2026-03-10T14:30:00\x00", "2026-03-10\u00a014:30", "2026-03-10_14:30", "2026-02-30T10:00:00",
       "2025-02-29", "2026-03-10T14:30:00+0100", "2026-03-10T14:30:00\u0663"]


class Dates(unittest.TestCase):
    setUp = base.KitMuckrockTests.setUp
    send, filed, saved, imp, steps = (base.KitMuckrockTests.send, base.KitMuckrockTests.filed,
                                      base.KitMuckrockTests.saved, base.KitMuckrockTests.imp,
                                      base.KitMuckrockTests.steps)

    def snapshot(self):
        return {str(p.relative_to(self.tmp)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in sorted(self.tmp.rglob("*")) if p.is_file()}

    def test_date_and_datetime_whole_value_admission(self):
        for value in GOOD:
            self.assertEqual(kit_muckrock._date({"datetime_updated": value}, "datetime_updated"), value[:10], value)
        for value in BAD + [5, ["2026-03-10"], {"a": 1}, True]:
            with self.assertRaises(ValueError) as caught:
                kit_muckrock._date({"datetime_updated": value}, "datetime_updated")
            self.assertEqual(str(caught.exception), FIELD_ERROR)
            self.assertIsNone(caught.exception.__cause__)
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_written_date_is_preserved_without_utc_conversion(self):
        for value in ("2026-03-10T23:30:00-08:00", "2026-03-10T00:30:00+08:00", "0001-01-01T00:00:00+05:00",
                      "9999-12-31T23:59:59-05:00"):
            self.assertEqual(kit_muckrock._date({"datetime_updated": value}, "datetime_updated"), value[:10])

    def test_selected_done_malformed_never_falls_back(self):
        fields = ("datetime_done", "datetime_updated")
        with self.assertRaisesRegex(ValueError, "^datetime_done must be"):
            kit_muckrock._date({"datetime_done": "2026-03-10garbage", "datetime_updated": "2026-03-09"}, *fields)
        for done in (None, ""):
            self.assertEqual(kit_muckrock._date({"datetime_done": done, "datetime_updated": "2026-03-09"}, *fields),
                             "2026-03-09")
        with self.assertRaisesRegex(ValueError, "^datetime_updated must be"):
            kit_muckrock._date({"datetime_updated": "2026-03-09T99:00"}, *fields)
        with self.assertRaisesRegex(ValueError, "^the JSON has no datetime_done or datetime_updated"):
            kit_muckrock._date({}, *fields)
        # a malformed field that is not selected is ignored
        self.assertEqual(kit_muckrock._date({"datetime_done": "2026-03-10", "datetime_updated": "junk"}, *fields),
                         "2026-03-10")
        self.filed()
        for status, field in (("processed", "datetime_done"),):  # an unfinished status never reads done
            path = self.saved(status=status, datetime_updated="2026-03-10", **{field: "junk"})
            self.assertEqual(kit_muckrock.propose(self.root, self.agency, path)["step"]["on"], "2026-03-10")
        path = self.saved(status="done", datetime_done="2026-03-10T25:00", datetime_updated="2026-03-09")
        with self.assertRaisesRegex(ValueError, "^datetime_done must be"):
            kit_muckrock.propose(self.root, self.agency, path)

    def test_waiting_and_manual_statuses_keep_date_independence(self):
        self.filed()
        for status in kit_muckrock.NO_STEP:
            path = self.saved(status=status, datetime_updated="junk", datetime_done="junk")
            result = kit_muckrock.propose(self.root, self.agency, path)
            self.assertIsNone(result["step"])
            self.assertEqual(result["reason"], kit_muckrock.NO_STEP[status])
        with self.assertRaisesRegex(ValueError, "unknown MuckRock status"):
            kit_muckrock.propose(self.root, self.agency, self.saved(status="mystery", datetime_updated="junk"))

    def test_malformed_selected_timestamp_cannot_hide_behind_latest_step(self):
        self.filed()
        good = self.saved(status="processed", datetime_updated="2026-03-10T14:30:00Z")
        self.assertEqual(self.imp(good, "--apply")[0], 0)
        self.assertEqual(self.imp(good)[1].count("already recorded"), 1)
        for bad in ("2026-03-10T25:99:99Z", "2026-03-10garbage", "2026-03-10T14:30:00+99:99"):
            path = self.saved(status="processed", datetime_updated=bad)
            for extra in ((), ("--apply",), ("--json",), ("--apply", "--json")):
                before = self.snapshot()
                with mock.patch.object(kit_track, "record_step") as writer:
                    code, out, err = self.imp(path, *extra)
                writer.assert_not_called()
                self.assertEqual((code, out), (1, ""), (bad, extra))
                self.assertIn(FIELD_ERROR, err)
                self.assertNotIn("already recorded", err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(self.snapshot(), before)
        closed = self.saved(status="done", datetime_done="2026-03-10T99:00", datetime_updated="2026-03-10")
        code, out, err = self.imp(closed, "--apply")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("datetime_done must be", err)

    def test_real_cli_new_step_refusal_preserves_all_persistent_inputs(self):
        self.filed(steps=[("acknowledged", "2026-03-03")])
        for bad in ("2026-03-10T25:99:99Z", "2026-03-10T14:30:00+99:99", "2026-03-10garbage"):
            path = self.saved(status="payment", price="245", datetime_updated=bad)
            before = self.snapshot()
            code, out, err = self.imp(path, "--apply", "--json")
            self.assertEqual((code, out), (1, ""))
            self.assertIn(FIELD_ERROR, err)
            self.assertNotIn(bad, err)
            self.assertEqual(self.snapshot(), before)

    def test_valid_import_after_refusal_and_replay_stay_compatible(self):
        self.filed()
        bad = self.saved(status="processed", datetime_updated="2026-03-10T25:99:99Z")
        self.assertEqual(self.imp(bad, "--apply")[0], 1)
        self.assertEqual(self.steps(), [])
        good = self.saved(status="processed", datetime_updated="2026-03-10T23:30:00-08:00")
        before = self.snapshot()
        code, out, _ = self.imp(good, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out[:out.rindex("}") + 1])["step"]["on"], "2026-03-10")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.imp(good, "--apply")[0], 0)
        self.assertEqual([(s["kind"], s["on"]) for s in self.steps()], [("acknowledged", "2026-03-10")])
        code, out, _ = self.imp(good, "--apply", "--json")
        self.assertEqual((code, json.loads(out[:out.rindex("}") + 1])["reason"]), (0, "already recorded"))
        self.assertEqual(len(self.steps()), 1)


if __name__ == "__main__":
    unittest.main()
