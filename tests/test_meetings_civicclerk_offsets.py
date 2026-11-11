"""RF0440-5: an explicit negative UTC offset in CivicClerk startDateTime is a known absolute start.

Recorded synthetic JSON only (fictional client examplecounty); no network, no provider, no publication.
"""
import contextlib
import copy
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli  # noqa: E402
from campaign_tool.meetings import (CivicClerkClient, MeetingsError, compare_meetings, fixture_opener,  # noqa: E402
                                    relevant_events, resolve_zone, scan_sources)

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "meetings" / "civicclerk-examplecounty.json"
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SINCE = date(2026, 12, 1)
LA, NY, UTC = "America/Los_Angeles", "America/New_York", "UTC"
NO_CHANGES = {"added": [], "removed": [], "rescheduled": [], "agenda_changed": []}


def recorded(*starts):
    """The synthetic recording; the first event takes starts[0] (and further matching events the rest)."""
    mapping = json.loads(FIXTURE.read_text(encoding="utf-8"))
    events = mapping["/v1/Events"]["value"]
    template = events[0]
    if starts:
        events[:] = [dict(template, id=2001 + i, startDateTime=s) for i, s in enumerate(starts)]
    return mapping


def run(mapping, since=SINCE, zone=LA):
    client = CivicClerkClient("examplecounty", opener=fixture_opener(mapping))
    return relevant_events(client, since=since, zone=resolve_zone(zone))


def start_of(start, zone=LA):
    rows = run(recorded(start), since=date(2026, 1, 1), zone=zone)
    return [(r["starts_at"], r["time_known"]) for r in rows]


def changes_between(old_rows, new_rows, since=SINCE):
    doc = {"schema_version": 1, "client": "examplecounty", "meetings": old_rows}
    return compare_meetings(doc, new_rows, "examplecounty", since)


class Instants(unittest.TestCase):
    def test_negative_offset_routes_to_absolute_start_not_unknown_midnight(self):
        self.assertEqual(start_of("2026-12-07T17:00:00-08:00"), [("2026-12-07T17:00:00-08:00", True)])
        self.assertEqual(start_of("2026-12-07T17:00:00-08:00", zone=UTC), [("2026-12-08T01:00:00+00:00", True)])
        client = CivicClerkClient("examplecounty", opener=fixture_opener(recorded("2026-12-07T17:00:00-08:00")))
        event = client.events(since=SINCE)[0]
        self.assertEqual((event["EventStartUtc"], event["EventDate"]), ("2026-12-07T17:00:00-08:00",) * 2)

    def test_equivalent_z_zero_positive_and_negative_spellings_are_identical(self):
        spellings = ["2026-12-08T01:00:00Z", "2026-12-08T01:00:00+00:00", "2026-12-08T06:30:00+05:30",
                     "2026-12-07T17:00:00-08:00"]
        rows = [run(recorded(s))[0] for s in spellings]
        self.assertEqual(rows[0]["starts_at"], "2026-12-07T17:00:00-08:00")  # literal, independent expectation
        self.assertTrue(all(r == rows[0] for r in rows))
        for old in rows:
            for new in rows:
                got = changes_between([old], [new])
                self.assertEqual({k: got[k] for k in NO_CHANGES}, NO_CHANGES)

    def test_negative_nonhour_offsets_dst_and_calendar_crossings(self):
        vectors = [  # (provider start, zone, literal expected starts_at)
            ("2026-07-07T17:00:00-07:00", LA, "2026-07-07T17:00:00-07:00"),  # Pacific summer
            ("2026-12-07T17:00:00-08:00", LA, "2026-12-07T17:00:00-08:00"),  # Pacific winter
            ("2026-07-07T17:00:00-07:00", UTC, "2026-07-08T00:00:00+00:00"),
            ("2026-12-07T20:00:00-03:30", UTC, "2026-12-07T23:30:00+00:00"),
            ("2026-12-07T20:00:00-03:30", LA, "2026-12-07T15:30:00-08:00"),
            ("2026-12-07T23:45:00-00:30", UTC, "2026-12-08T00:15:00+00:00"),
            ("2026-12-07T23:45:00-00:30", LA, "2026-12-07T16:15:00-08:00"),
            ("2026-12-31T20:00:00-05:00", UTC, "2027-01-01T01:00:00+00:00"),  # year crossing
            ("2026-12-31T23:30:00-08:00", NY, "2027-01-01T02:30:00-05:00"),  # year crossing, other zone
            ("2026-12-14T22:00:00-08:00", NY, "2026-12-15T01:00:00-05:00"),  # day crossing
        ]
        for start, zone, expected in vectors:
            with self.subTest(start=start, zone=zone):
                self.assertEqual(start_of(start, zone), [(expected, True)])
                self.assertEqual(datetime.fromisoformat(expected), datetime.fromisoformat(start))  # same instant

    def test_naive_and_date_only_do_not_gain_an_inferred_offset(self):
        for start in ("2026-12-07T17:00:00", "2026-12-07"):
            with self.subTest(start=start):
                self.assertEqual(start_of(start), [("2026-12-07T00:00:00-08:00", False)])
                client = CivicClerkClient("examplecounty", opener=fixture_opener(recorded(start)))
                self.assertIsNone(client.events(since=SINCE)[0]["EventStartUtc"])  # date hyphens never count as an offset
        for start in (None, 5, ["2026-12-07T17:00:00-08:00"]):
            mapping = recorded("2026-12-07T17:00:00-08:00")
            mapping["/v1/Events"]["value"].append(dict(mapping["/v1/Events"]["value"][0], id=2999, startDateTime=start))
            self.assertEqual([r["event_id"] for r in run(mapping)], [2001])
        self.assertEqual(start_of("2026-12-08T01:00:00Z"), [("2026-12-07T17:00:00-08:00", True)])  # Z control
        self.assertEqual(start_of("2026-12-08T06:30:00+05:30"), [("2026-12-07T17:00:00-08:00", True)])  # positive control

    def test_invalid_negative_offset_uses_existing_bad_absolute_start_path(self):
        bad = ["2026-12-07T17:00:00-25:00", "2026-13-07T17:00:00-08:00", "2026-12-32T17:00:00-08:00",
               "2026-12-07T25:00:00-08:00", "2026-12-07T17:00:00-0x:00"]
        mapping = recorded(*bad, "2026-12-07T18:00:00-08:00")
        client = CivicClerkClient("examplecounty", opener=fixture_opener(mapping))
        rows = relevant_events(client, since=SINCE, zone=resolve_zone(LA))
        self.assertEqual([(r["event_id"], r["starts_at"], r["time_known"]) for r in rows],
                         [(2001 + len(bad), "2026-12-07T18:00:00-08:00", True)])  # no fabricated midnight
        for bad_start in ("2026-12-07T17:00:00+25:00", "2026-13-07T17:00:00Z"):  # the positive/Z siblings behave alike
            self.assertEqual(start_of(bad_start), [])

    def test_since_uses_existing_source_calendar_date_and_cap_is_unchanged(self):
        # The cutoff is the provider's written date, not the requested-zone day: -08:00 evening is next day in UTC.
        mapping = recorded("2026-11-30T20:00:00-08:00", "2026-12-01T20:00:00-08:00")
        got = [r["event_id"] for r in run(mapping, zone=UTC)]
        self.assertEqual(got, [2002])  # 11-30 is before since although its UTC day is 12-01
        self.assertEqual(run(mapping, zone=UTC)[0]["starts_at"], "2026-12-02T04:00:00+00:00")
        self.assertEqual(run(mapping, since=date(2026, 12, 2), zone=UTC), [])
        many = recorded()
        many["/v1/Events"] = {"value": [dict(many["/v1/Events"]["value"][0], id=3000 + i, agendaId=900,
                                             startDateTime="2026-12-07T17:00:00-08:00") for i in range(61)]}
        asked = []
        client = CivicClerkClient("examplecounty", opener=lambda u: (asked.append(u), fixture_opener(many)(u))[1])
        self.assertEqual(len(relevant_events(client, since=SINCE)), 60)
        self.assertEqual(sum("/Meetings/" in u for u in asked), 60)
        events_url = next(u for u in asked if "/Events" in u)
        self.assertIn("startDateTime%20ge%202026-12-01", events_url)


class Persistence(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-civicclerk-offsets-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.recording = self.tmp / "rec.json"

    def main(self, start):
        mapping = recorded(start)
        self.recording.write_text(json.dumps(mapping))
        argv = ["campaign_tool", "meetings", "--directory", str(self.root), "--platform", "civicclerk",
                "--client", "examplecounty", "--from-json", str(self.recording), "--since", "2026-12-01"]
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", argv), mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main() or 0
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 0, err.getvalue())
        doc = json.loads((self.root / "kit" / "meetings.json").read_text())
        return json.loads(out.getvalue()), doc

    def test_cli_equivalent_offset_refresh_has_no_false_reschedule(self):
        report, first = self.main("2026-12-08T01:00:00Z")
        self.assertEqual(report["previous"], "none")
        report, second = self.main("2026-12-07T17:00:00-08:00")
        self.assertEqual(report["previous"], "compared")
        self.assertEqual(report["changes"], {"added": 0, "removed": 0, "rescheduled": 0, "agenda_changed": 0, "past": 0})
        self.assertEqual(second["meetings"], first["meetings"])
        self.assertEqual((second["meetings"][0]["starts_at"], second["meetings"][0]["time_known"]),
                         ("2026-12-07T17:00:00-08:00", True))
        report, third = self.main("2026-12-07T18:00:00-08:00")  # a genuine one-hour shift
        self.assertEqual(report["changes"]["rescheduled"], 1)
        self.assertEqual(third["changes"]["rescheduled"],
                         [{"id": "civicclerk-examplecounty-2001", "from": "2026-12-07T17:00:00-08:00",
                           "to": "2026-12-07T18:00:00-08:00"}])
        self.assertEqual((third["changes"]["added"], third["changes"]["removed"], third["changes"]["agenda_changed"]), ([], [], []))

    def test_old_midnight_row_is_corrected_and_reported_truthfully(self):
        _, old = self.main("2026-12-07T17:00:00")  # unknown local time: midnight, time_known false
        self.assertEqual((old["meetings"][0]["starts_at"], old["meetings"][0]["time_known"]),
                         ("2026-12-07T00:00:00-08:00", False))
        report, new = self.main("2026-12-07T17:00:00-08:00")
        self.assertEqual(report["changes"]["rescheduled"], 1)
        self.assertEqual(new["changes"]["rescheduled"], [{"id": "civicclerk-examplecounty-2001",
                                                          "from": "2026-12-07T00:00:00-08:00",
                                                          "to": "2026-12-07T17:00:00-08:00"}])

    def test_offline_import_preserves_input_trees_and_only_updates_private_kit(self):
        self.main("2026-12-08T01:00:00Z")
        sentinel = self.root / "sentinel.txt"
        sentinel.write_text("unrelated")

        def snapshot():
            return {str(p.relative_to(self.tmp)): (p.read_bytes(), p.stat().st_mode)
                    for p in sorted(self.tmp.rglob("*")) if p.is_file()}
        before = snapshot()
        self.main("2026-12-07T17:00:00-08:00")
        after = snapshot()
        kit = str(Path("campaign") / "kit" / "meetings.json")
        self.assertEqual(set(after), set(before))
        self.assertEqual({k for k in after if after[k] != before[k]}, {kit, "rec.json"})
        self.assertEqual(after[kit][1] & 0o077, 0)  # private kit file


class MultiSource(unittest.TestCase):
    SOURCES = [{"platform": "civicclerk", "client": "alpha", "label": "Alpha Council"},
               {"platform": "civicclerk", "client": "beta", "label": "Beta Board"}]

    def scan(self, start, previous=None, beta_ok=True):
        mapping = recorded(start)

        def opener_for(key):
            if key == "civicclerk:beta" and not beta_ok:
                def broken(url):
                    raise MeetingsError("recorded outage")
                return broken
            return fixture_opener(mapping)
        return scan_sources(self.SOURCES, opener_for, SINCE, resolve_zone(LA), previous=previous)

    def test_multi_source_negative_offset_stays_ok_and_other_failures_stay_isolated(self):
        meetings, report, _ = self.scan("2026-12-08T01:00:00Z")
        previous = {"schema_version": 1, "client": "multi", "clients": ["civicclerk:alpha", "civicclerk:beta"],
                    "meetings": meetings}
        meetings, report, changes = self.scan("2026-12-07T17:00:00-08:00", previous=previous)
        self.assertEqual([(r["client"], r["label"], r["status"]) for r in report],
                         [("civicclerk:alpha", "Alpha Council", "ok"), ("civicclerk:beta", "Beta Board", "ok")])
        self.assertEqual({(m["client"], m["starts_at"], m["time_known"]) for m in meetings},
                         {("civicclerk:alpha", "2026-12-07T17:00:00-08:00", True),
                          ("civicclerk:beta", "2026-12-07T17:00:00-08:00", True)})
        for key in ("civicclerk:alpha", "civicclerk:beta"):
            self.assertEqual({k: changes[key][k] for k in NO_CHANGES}, NO_CHANGES)
        # beta now fails: it keeps its previous rows as stale, alpha still reads the negative offset as known
        previous = dict(previous, meetings=meetings)
        meetings, report, changes = self.scan("2026-12-07T17:00:00-08:00", previous=previous, beta_ok=False)
        self.assertEqual([r["status"] for r in report], ["ok", "failed"])
        self.assertEqual(report[1]["kept_previous_meetings"], 1)
        self.assertEqual([(m["client"], m["starts_at"], m.get("stale", False)) for m in meetings],
                         [("civicclerk:alpha", "2026-12-07T17:00:00-08:00", False),
                          ("civicclerk:beta", "2026-12-07T17:00:00-08:00", True)])
        self.assertEqual(set(changes), {"civicclerk:alpha"})


if __name__ == "__main__":
    unittest.main()
