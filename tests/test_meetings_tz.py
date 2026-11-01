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
from campaign_tool.meetings import (  # noqa: E402
    LegistarClient, compare_meetings, fixture_opener, relevant_events, resolve_zone,
)

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
ITEM = [{"EventItemTitle": "Item 7: ALPR contract renewal (fictional)"}]


def recorded(events):
    """Recorded Legistar responses: each (event id, date, time) becomes one ALPR-matching event."""
    mapping = {"/events": [{"EventId": i, "EventDate": f"{day}T00:00:00", "EventTime": when,
                            "EventBodyName": "Synthetic Council (fictional)",
                            "EventInSiteURL": f"https://agenda.example.invalid/{i}"} for i, day, when in events]}
    for i, _, _ in events:
        mapping[f"/events/{i}/eventitems"] = ITEM
    return mapping


def starts(events, zone_name=None):
    client = LegistarClient("examplecity", opener=fixture_opener(recorded(events)))
    zone = resolve_zone(zone_name) if zone_name else None
    return {m["event_id"]: (m["starts_at"], m["time_known"])
            for m in relevant_events(client, since=date(2026, 1, 1), zone=zone)}


def doc(meetings):
    return {"schema_version": 1, "client": "examplecity", "meetings": meetings}


def snapshot(folder):
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())} if folder.is_dir() else None


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


class CliZoneTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meetings-tz-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.fixture = self.tmp / "legistar.json"
        self.fixture.write_text(json.dumps(recorded([(1001, "2026-10-14", "6:00 PM"), (1002, "2026-11-03", "6:00 PM")])))
        self.kit = self.root / "kit" / "meetings.json"

    def main(self, *extra):
        argv = ["campaign_tool", "meetings", "--directory", str(self.root), "--client", "examplecity", *extra]
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def offline(self, *extra):
        return self.main("--from-json", str(self.fixture), "--since", "2026-10-01", *extra)

    def test_default_zone_recorded(self):
        code, _, err = self.offline()
        self.assertEqual(code, 0, err)
        document = json.loads(self.kit.read_text())
        self.assertEqual(document["time_zone"], "America/Los_Angeles")
        self.assertEqual([m["starts_at"] for m in document["meetings"]],
                         ["2026-10-14T18:00:00-07:00", "2026-11-03T18:00:00-08:00"])
        self.assertTrue(all(m["time_known"] for m in document["meetings"]))

    def test_tz_option(self):
        code, _, err = self.offline("--tz", "America/New_York")
        self.assertEqual(code, 0, err)
        document = json.loads(self.kit.read_text())
        self.assertEqual(document["time_zone"], "America/New_York")
        self.assertEqual([m["starts_at"] for m in document["meetings"]],
                         ["2026-10-14T18:00:00-04:00", "2026-11-03T18:00:00-05:00"])

    def test_unknown_zone_exits_2_before_any_fetch(self):
        self.kit.parent.mkdir(exist_ok=True)
        self.kit.write_text("previous kit\n")
        never = mock.Mock(side_effect=AssertionError("fetched"))
        for zone in ("Mars/Base", "", "../etc/passwd"):
            for source in (("--online",), ("--from-json", str(self.fixture))):
                with mock.patch("campaign_tool.meetings.default_opener", never), \
                        mock.patch("campaign_tool.meetings.fixture_opener", never):
                    code, out, err = self.main(*source, "--tz", zone)
                self.assertEqual(code, 2, (zone, source))
                self.assertEqual(err, f"meetings: unknown time zone {zone}\n")
                self.assertEqual(out, "")
        never.assert_not_called()
        self.assertEqual(self.kit.read_text(), "previous kit\n")

    def test_upgrade_from_naive_kit_is_not_rescheduled(self):
        self.offline()
        previous = json.loads(self.kit.read_text())
        for meeting in previous["meetings"]:
            meeting["starts_at"] = meeting["starts_at"][:19]  # an old kit: no offset
            meeting.pop("time_known")
        previous.pop("time_zone")
        self.kit.write_text(json.dumps(previous))
        code, out, err = self.offline()
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["changes"]["rescheduled"], 0)
        previous["meetings"][1]["starts_at"] = "2026-11-03T19:00:00"
        self.kit.write_text(json.dumps(previous))
        self.offline()
        self.assertEqual(json.loads(self.kit.read_text())["changes"]["rescheduled"],
                         [{"id": "legistar-examplecity-1002", "from": "2026-11-03T19:00:00",
                           "to": "2026-11-03T18:00:00-08:00"}])


class CompareTests(unittest.TestCase):
    NEW = "2026-10-14T18:00:00-07:00"

    def rescheduled(self, old, new=NEW):
        out = compare_meetings(doc([{"id": "a", "starts_at": old}]), [{"id": "a", "starts_at": new}],
                               "examplecity", date(2026, 10, 1))
        return [r["id"] for r in out["rescheduled"]]

    def test_naive_old_read_as_wall_time(self):
        self.assertEqual(self.rescheduled("2026-10-14T18:00:00"), [])
        self.assertEqual(self.rescheduled("2026-10-14T18:00:00", "2026-10-14T19:00:00-07:00"), ["a"])
        self.assertEqual(self.rescheduled("2026-10-15T18:00:00"), ["a"])

    def test_aware_values_compare_instants(self):
        self.assertEqual(self.rescheduled("2026-10-15T01:00:00+00:00"), [])
        self.assertEqual(self.rescheduled("2026-10-15T01:00:00Z"), [])
        self.assertEqual(self.rescheduled("2026-10-14T18:00:00-08:00"), ["a"])

    def test_unparseable_old_value_is_rescheduled(self):
        self.assertEqual(self.rescheduled("not a time"), ["a"])
        self.assertEqual(self.rescheduled(""), ["a"])


class ContentTests(unittest.TestCase):
    BASE = {"id": "m1", "body": "Synthetic council (fictional)"}

    def test_naive_refused_with_named_field(self):
        for value in ("2026-10-14T18:00:00", "2026-10-14 18:00", "2026-10-14"):
            with self.assertRaises(ContentError) as caught:
                load_meeting(dict(self.BASE, starts_at=value), "meetings/m1.json")
            self.assertEqual(str(caught.exception), "meetings/m1.json.starts_at: needs a UTC offset (e.g. -07:00)")

    def test_offset_or_z_loads(self):
        for value in ("2026-10-14T18:00:00-07:00", "2026-10-15T01:00:00Z", "2026-10-15T01:00:00+00:00"):
            self.assertEqual(load_meeting(dict(self.BASE, starts_at=value), "meetings/m1.json")["starts_at"], value)

    def test_invalid_value_keeps_existing_error(self):
        for value in ("not a time", 1793494646, None):
            with self.assertRaisesRegex(ContentError, r"meetings/m1\.json\.starts_at: (invalid ISO-8601|must be)"):
                load_meeting(dict(self.BASE, starts_at=value), "meetings/m1.json")

    def test_campaign_content_with_naive_meeting_fails_loading(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-meetings-tz-") as tmp:
            root = Path(tmp) / "campaign"
            shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
            self.assertTrue(load_content(root)["meetings"])  # the example already carries -07:00
            (root / "content" / "meetings" / "naive.json").write_text(
                json.dumps(dict(self.BASE, id="naive", starts_at="2026-11-04T18:00:00")))
            with self.assertRaisesRegex(ContentError, r"naive\.json\.starts_at: needs a UTC offset"):
                load_content(root)


class StageTests(unittest.TestCase):
    MID = "legistar-examplecity-1001"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meetings-tz-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content" / "meetings"

    def write_kit(self, starts_at):
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": self.MID, "body": "Synthetic Council (fictional)", "starts_at": starts_at}]}))

    def test_naive_kit_refused_nothing_written(self):
        for value in ("2026-10-14T18:00:00", "2026-10-14"):
            self.write_kit(value)
            before = snapshot(self.content)
            for replace in (False, True):
                with self.assertRaisesRegex(MeetingStageError,
                                            r"^kit/meetings\.json predates time zones; run meetings again$"):
                    stage(self.root, self.MID, replace=replace)
                self.assertEqual(snapshot(self.content), before)

    def test_naive_kit_refused_over_existing_target(self):
        self.write_kit("2026-10-14T18:00:00-07:00")
        stage(self.root, self.MID)
        before = snapshot(self.content)
        self.write_kit("2026-10-14T19:00:00")
        with self.assertRaises(MeetingStageError):
            stage(self.root, self.MID, replace=True)
        self.assertEqual(snapshot(self.content), before)

    def test_offset_and_z_kits_stage(self):
        for value in ("2026-11-03T18:00:00-08:00", "2026-11-04T02:00:00Z"):
            self.write_kit(value)
            stage(self.root, self.MID, replace=True)
            written = json.loads((self.content / f"{self.MID}.json").read_text())
            self.assertEqual(written["starts_at"], value)

    def test_unparseable_kit_time_named_by_content(self):
        self.write_kit("not a time")
        with self.assertRaisesRegex(ContentError, r"starts_at: invalid ISO-8601"):
            stage(self.root, self.MID)


if __name__ == "__main__":
    unittest.main()
