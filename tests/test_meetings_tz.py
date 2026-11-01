"""NB-DF-13: meetings starts_at carries the --tz zone's UTC offset; naive times refused in content.

Synthetic fixtures only (example.invalid hosts, fictional example campaign); no network.
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli  # noqa: E402
from campaign_tool.content import ContentError, load_content, load_meeting  # noqa: E402
from campaign_tool.meeting_stage import MeetingStageError, stage  # noqa: E402
from campaign_tool.meetings import LegistarClient, compare_meetings, fixture_opener, relevant_events, resolve_zone  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
MID = "legistar-examplecity-1001"
NEEDS = "needs a UTC offset (e.g. -07:00)"


def recorded(events):
    """Recorded Legistar responses: each (event id, date, time) is one ALPR-matching event."""
    mapping = {"/events": [{"EventId": i, "EventDate": f"{day}T00:00:00", "EventTime": when,
                            "EventBodyName": "Synthetic Council (fictional)"} for i, day, when in events]}
    for i, _, _ in events:
        mapping[f"/events/{i}/eventitems"] = [{"EventItemTitle": "Item 7: ALPR renewal (fictional)"}]
    return mapping


def starts(events, zone=None):
    client = LegistarClient("examplecity", opener=fixture_opener(recorded(events)))
    meetings = relevant_events(client, since=date(2026, 1, 1), zone=resolve_zone(zone) if zone else None)
    return {m["event_id"]: (m["starts_at"], m["time_known"]) for m in meetings}


def snapshot(folder):
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())} if folder.is_dir() else None


class Campaign(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meetings-tz-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.kit = self.root / "kit" / "meetings.json"
        self.content = self.root / "content" / "meetings"


class StartsAtTests(unittest.TestCase):
    def test_pacific_offsets_follow_daylight_saving(self):
        out = starts([(1, "2026-10-14", "6:00 PM"), (2, "2026-11-03", "6:00 PM"), (3, "2026-11-01", "6:00 PM"),
                      (4, "2026-11-01", "1:30 AM"), (5, "2026-03-08", "7:00 PM"), (6, "2026-03-07", "7:00 PM")])
        self.assertEqual(out, {1: ("2026-10-14T18:00:00-07:00", True), 2: ("2026-11-03T18:00:00-08:00", True),
                               3: ("2026-11-01T18:00:00-08:00", True), 4: ("2026-11-01T01:30:00-07:00", True),
                               5: ("2026-03-08T19:00:00-07:00", True), 6: ("2026-03-07T19:00:00-08:00", True)})

    def test_explicit_zone(self):
        out = starts([(1, "2026-10-14", "6:00 PM"), (2, "2026-11-03", "6:00 PM")], "America/New_York")
        self.assertEqual(out, {1: ("2026-10-14T18:00:00-04:00", True), 2: ("2026-11-03T18:00:00-05:00", True)})

    def test_unknown_time_is_local_midnight_flagged(self):
        out = starts([(1, "2026-10-14", None), (2, "2026-10-14", "25:99 PM"), (3, "2026-11-03", ""),
                      (4, "2026-11-03", "18:00"), (5, "2026-11-01", "  ")])
        self.assertEqual(out, {1: ("2026-10-14T00:00:00-07:00", False), 2: ("2026-10-14T00:00:00-07:00", False),
                               3: ("2026-11-03T00:00:00-08:00", False), 4: ("2026-11-03T00:00:00-08:00", False),
                               5: ("2026-11-01T00:00:00-07:00", False)})

    def test_resolve_zone(self):
        self.assertEqual(resolve_zone("America/Los_Angeles").key, "America/Los_Angeles")
        for bad in ("Mars/Base", "", "../../etc/passwd", "/etc/localtime", "america/los angeles", None):
            self.assertIsNone(resolve_zone(bad), bad)


class CliZoneTests(Campaign):
    def main(self, *extra):
        argv = ["campaign_tool", "meetings", "--directory", str(self.root), "--client", "examplecity", *extra]
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def offline(self, *extra):
        fixture = self.tmp / "legistar.json"
        fixture.write_text(json.dumps(recorded([(1001, "2026-10-14", "6:00 PM"), (1002, "2026-11-03", "6:00 PM")])))
        code, out, err = self.main("--from-json", str(fixture), "--since", "2026-10-01", *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out), json.loads(self.kit.read_text())

    def test_default_and_explicit_zone_recorded(self):
        for extra, zone, offsets in (((), "America/Los_Angeles", ("-07:00", "-08:00")),
                                     (("--tz", "America/New_York"), "America/New_York", ("-04:00", "-05:00"))):
            _, document = self.offline(*extra)
            self.assertEqual(document["time_zone"], zone)
            self.assertEqual([m["starts_at"] for m in document["meetings"]],
                             ["2026-10-14T18:00:00" + offsets[0], "2026-11-03T18:00:00" + offsets[1]])
            self.assertTrue(all(m["time_known"] is True for m in document["meetings"]))

    def test_unknown_zone_exits_2_before_any_fetch(self):
        self.kit.parent.mkdir(exist_ok=True)
        self.kit.write_text("previous kit\n")
        never = mock.Mock(side_effect=AssertionError("fetched"))
        for zone in ("Mars/Base", "", "../etc/passwd"):
            for source in (("--online",), ("--from-json", str(self.tmp / "absent.json"))):
                with mock.patch("campaign_tool.meetings.default_opener", never), \
                        mock.patch("campaign_tool.meetings.fixture_opener", never):
                    code, out, err = self.main(*source, "--tz", zone)
                self.assertEqual((code, out, err), (2, "", f"meetings: unknown time zone {zone}\n"))
        never.assert_not_called()
        self.assertEqual(self.kit.read_text(), "previous kit\n")

    def test_upgrade_from_naive_kit_is_not_rescheduled(self):
        _, previous = self.offline()
        for meeting in previous["meetings"]:
            meeting["starts_at"] = meeting["starts_at"][:19]  # an old kit: no offset, no time_known
            meeting.pop("time_known")
        self.kit.write_text(json.dumps(dict(previous, time_zone=None)))
        out, document = self.offline()
        self.assertEqual((out["changes"]["rescheduled"], document["changes"]["rescheduled"]), (0, []))
        previous["meetings"][1]["starts_at"] = "2026-11-03T19:00:00"
        self.kit.write_text(json.dumps(previous))
        _, document = self.offline()
        self.assertEqual(document["changes"]["rescheduled"], [{"id": "legistar-examplecity-1002",
                                                               "from": "2026-11-03T19:00:00",
                                                               "to": "2026-11-03T18:00:00-08:00"}])


class CompareTests(unittest.TestCase):
    def rescheduled(self, old, new="2026-10-14T18:00:00-07:00"):
        out = compare_meetings({"schema_version": 1, "client": "examplecity", "meetings": [{"id": "a", "starts_at": old}]},
                               [{"id": "a", "starts_at": new}], "examplecity", date(2026, 10, 1))
        return [r["id"] for r in out["rescheduled"]]

    def test_naive_old_is_wall_time_aware_compares_instants(self):
        for old, expected in (("2026-10-14T18:00:00", []), ("2026-10-15T18:00:00", ["a"]),
                              ("2026-10-15T01:00:00+00:00", []), ("2026-10-15T01:00:00Z", []),
                              ("2026-10-14T18:00:00-08:00", ["a"]), ("not a time", ["a"]), ("", ["a"])):
            self.assertEqual(self.rescheduled(old), expected, old)
        self.assertEqual(self.rescheduled("2026-10-14T18:00:00", "2026-10-14T19:00:00-07:00"), ["a"])


class ContentTests(Campaign):
    BASE = {"id": "m1", "body": "Synthetic council (fictional)"}

    def test_naive_refused_with_named_field(self):
        for value in ("2026-10-14T18:00:00", "2026-10-14 18:00", "2026-10-14"):
            with self.assertRaises(ContentError) as caught:
                load_meeting(dict(self.BASE, starts_at=value), "meetings/m1.json")
            self.assertEqual(str(caught.exception), f"meetings/m1.json.starts_at: {NEEDS}")
        for value in ("not a time", 1793494646, None):  # unchanged errors for non-ISO values
            with self.assertRaisesRegex(ContentError, r"m1\.json\.starts_at: (invalid ISO-8601|must be)"):
                load_meeting(dict(self.BASE, starts_at=value), "meetings/m1.json")

    def test_offset_or_z_loads(self):
        for value in ("2026-10-14T18:00:00-07:00", "2026-10-15T01:00:00Z", "2026-10-15T01:00:00+00:00"):
            self.assertEqual(load_meeting(dict(self.BASE, starts_at=value), "meetings/m1.json")["starts_at"], value)

    def test_campaign_content_with_naive_meeting_fails_loading(self):
        self.assertTrue(load_content(self.root)["meetings"])  # the example already carries -07:00
        (self.content / "naive.json").write_text(json.dumps(dict(self.BASE, id="naive", starts_at="2026-11-04T18:00:00")))
        with self.assertRaisesRegex(ContentError, r"naive\.json\.starts_at: needs a UTC offset"):
            load_content(self.root)


class StageTests(Campaign):
    def write_kit(self, starts_at):
        self.kit.parent.mkdir(exist_ok=True)
        self.kit.write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Synthetic Council (fictional)", "starts_at": starts_at}]}))

    def test_naive_kit_refused_nothing_written(self):
        for staged_first in (False, True):
            if staged_first:  # an earlier staged copy is never overwritten by a naive kit
                self.write_kit("2026-10-14T18:00:00-07:00")
                stage(self.root, MID)
            before = snapshot(self.content)
            for value in ("2026-10-14T19:00:00", "2026-10-14"):
                self.write_kit(value)
                for replace in (False, True):
                    with self.assertRaisesRegex(MeetingStageError, r"^kit/meetings\.json predates time zones; "
                                                                   r"run meetings again$"):
                        stage(self.root, MID, replace=replace)
                    self.assertEqual(snapshot(self.content), before)

    def test_offset_and_z_kits_stage(self):
        for value in ("2026-11-03T18:00:00-08:00", "2026-11-04T02:00:00Z"):
            self.write_kit(value)
            stage(self.root, MID, replace=True)
            self.assertEqual(json.loads((self.content / f"{MID}.json").read_text())["starts_at"], value)

    def test_unparseable_kit_time_named_by_content(self):
        self.write_kit("not a time")
        with self.assertRaisesRegex(ContentError, r"starts_at: invalid ISO-8601"):
            stage(self.root, MID)


if __name__ == "__main__":
    unittest.main()
