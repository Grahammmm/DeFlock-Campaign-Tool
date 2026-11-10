"""RF0854-4: repeated iCalendar UIDs resolve to their latest revision before any filtering (synthetic, no network)."""
import contextlib
import io
import itertools
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool import cli  # noqa: E402
from campaign_tool.meetings import MeetingsError, relevant_events, resolve_zone  # noqa: E402
from campaign_tool.meetings_ics import IcsClient  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
FEED = "https://example.gov/calendar/council.ics"
SINCE = date(2026, 10, 1)
UID = "synthetic-secret-revision@example.gov"
CONFLICT = "feed has conflicting revisions for one UID; nothing was written"
INVALID = "feed has an invalid SEQUENCE or DTSTAMP on a repeated UID; nothing was written"


def rev(seq=None, stamp=None, start: "str | None" = "20261111T180000", summary="ALPR policy", status=None, url=None,
        attach=None, uid=UID, extra=None):
    lines = [f"UID:{uid}", *([f"SEQUENCE:{seq}"] if seq is not None else []), *([f"DTSTAMP:{stamp}"] if stamp else []),
             f"DTSTART;TZID=America/Los_Angeles:{start}" if start else "LOCATION:nowhere", f"SUMMARY:{summary}",
             *([f"STATUS:{status}"] if status else []), *([f"URL:{url}"] if url else []),
             *([f"ATTACH:{attach}"] if attach else []), *([extra] if extra else [])]
    return "\r\n".join(lines)


def wrap(*vevents):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(f"BEGIN:VEVENT\r\n{v}\r\nEND:VEVENT\r\n" for v in vevents) + "END:VCALENDAR\r\n"


def read(*vevents, urls=None):
    def opener(url):
        if urls is not None:
            urls.append(url)
        if url != FEED:
            raise AssertionError(f"fetched {url}")
        return wrap(*vevents).encode("utf-8")
    client = IcsClient(FEED, "Example City Council", opener=opener)
    return relevant_events(client, since=SINCE, zone=resolve_zone("America/Los_Angeles")), client


R0 = rev(0, "20261101T120000Z", url="https://example.gov/agenda/0", attach="https://example.gov/files/r0.pdf")
R1 = rev(1, "20261101T120000Z", start="20261112T180000", status="CANCELLED", url="https://example.gov/agenda/1")
R2 = rev(2, "20261101T120000Z", start="20261113T180000", attach="https://example.gov/files/r2.pdf")


class RevisionSelectionTests(unittest.TestCase):
    def assertRefused(self, message, *vevents):
        with self.assertRaises(MeetingsError) as caught:
            read(*vevents)
        self.assertEqual(str(caught.exception), message)
        self.assertNotIn("secret", str(caught.exception))

    def test_latest_sequence_wins_in_every_feed_order(self):
        ids = set()
        for order in itertools.permutations([R0, R1]):
            (m,), _ = read(*order)
            ids.add(m["id"])
            self.assertEqual((m["starts_at"], m["status"], m["agenda_url"], m["attachments"]),
                             ("2026-11-12T18:00:00-08:00", "cancelled", "https://example.gov/agenda/1", []))
        for order in itertools.permutations([R0, R1, R2]):
            (m,), _ = read(*order)
            ids.add(m["id"])
            self.assertEqual((m["starts_at"], m["status"], m["agenda_url"], m["attachments"]),
                             ("2026-11-13T18:00:00-08:00", "scheduled", "https://example.gov/files/r2.pdf",
                              [{"name": "Attachment", "url": "https://example.gov/files/r2.pdf"}]))
        self.assertEqual(len(ids), 1)

    def test_sequence_precedes_dtstamp_and_missing_sequence_is_zero(self):
        cases = [  # (loser, winner)
            (rev(0, "20261105T000000Z"), rev(1, "20260101T000000Z", start="20261112T180000")),
            (rev(1, "20261101T000000Z"), rev(1, "20261102T000000Z", start="20261112T180000")),
            (rev(0, "20261101T000000Z"), rev(None, "20261102T000000Z", start="20261112T180000")),
            (rev(None, "20261109T000000Z"), rev(1, "20261101T000000Z", start="20261112T180000")),
            (rev(3), rev(3, "19700101T000000Z", start="20261112T180000")),
        ]
        for loser, winner in cases:
            for order in ((loser, winner), (winner, loser), (winner, loser, winner)):
                found, _ = read(*order)
                self.assertEqual([m["starts_at"] for m in found], ["2026-11-12T18:00:00-08:00"], (loser, winner))

    def test_equal_rank_conflicts_refuse(self):
        base = rev(1, "20261101T120000Z")
        for other in (rev(1, "20261101T120000Z", status="CANCELLED"), rev(1, "20261101T120000Z", start="20261112T180000"),
                      rev(1, "20261101T120000Z", summary="ALPR contract"),
                      rev(1, "20261101T120000Z", url="https://example.gov/agenda/9")):
            for order in ((base, other), (other, base), (rev(0, "20261201T000000Z"), other, base)):
                self.assertRefused(CONFLICT, *order)
        for order in ((rev(), rev(status="CANCELLED")), (rev(status="CANCELLED"), rev())):
            self.assertRefused(CONFLICT, *order)

    def test_revision_metadata_is_strict_and_diagnostics_are_bounded(self):
        good = rev(1, "20261101T120000Z")
        bad = [rev("-1"), rev("+2"), rev("1.0"), rev("0x1"), rev("2147483648"), rev("9" * 5000), rev("\u0661"),
               rev(1, extra="SEQUENCE:1"), rev(1, "20261101T120000"), rev(1, "20261301T120000Z"),
               rev(1, "20261101T250000Z"), rev(1, "2026-11-01T12:00:00Z"), rev(1, "20261101T120000z"),
               rev(1, "20261101T120000Z", extra="DTSTAMP:20261101T120000Z")]
        for other in bad:
            for order in ((good, other), (other, good)):
                self.assertRefused(INVALID, *order)
        (m,), _ = read(rev("0002147483647", "20261101T120000Z", start="20261112T180000"), good)
        self.assertEqual(m["starts_at"], "2026-11-12T18:00:00-08:00")
        for other in bad:  # a unique UID keeps the reader's existing lenient behaviour
            self.assertEqual(len(read(other)[0]), 1, other)

    def test_latest_filter_failure_never_resurrects_stale_revision(self):
        old = rev(0, "20261101T120000Z")
        newer = {"budget hearing": (rev(1, summary="Budget hearing"), None),
                 "moved before since": (rev(1, start="20260901T180000"), None),
                 "unplaceable": (rev(1, start=None), "bad_start"),
                 "floating": (rev(1, extra="DTSTART:20261112T180000").replace(
                     "DTSTART;TZID=America/Los_Angeles:20261111T180000\r\n", ""), "floating_time"),
                 "rrule": (rev(1, extra="RRULE:FREQ=WEEKLY"), "recurring")}
        for name, (latest, reason) in newer.items():
            for order in ((old, latest), (latest, old)):
                found, client = read(*order)
                self.assertEqual(found, [], name)
                expected = dict.fromkeys(client.skipped, 0)
                if reason:
                    expected[reason] = 1
                self.assertEqual(client.skipped, expected, name)
        override = rev(1, extra="RECURRENCE-ID;TZID=America/Los_Angeles:20261111T180000")
        for order in ((old, override), (override, old)):  # an override is skipped alone; the one-off is not hidden by it
            found, client = read(*order)
            self.assertEqual((len(found), client.skipped["recurring"]), (1, 1))
            self.assertEqual(found[0]["status"], "scheduled")

    def test_recurrence_id_sibling_never_hides_or_ranks_a_revision(self):
        a = rev(0, extra="RECURRENCE-ID;TZID=America/Los_Angeles:20261111T180000")
        b = rev(2, status="CANCELLED")
        for order in ((a, b), (b, a)):
            found, client = read(*order)
            self.assertEqual([m["status"] for m in found], ["cancelled"])
            self.assertEqual(client.skipped["recurring"], 1)
        # a plain revision outranked by another plain one is still dropped; the override changes nothing
        for order in itertools.permutations((a, rev(0), rev(1, status="CANCELLED"))):
            found, client = read(*order)
            self.assertEqual(([m["status"] for m in found], client.skipped["recurring"]), (["cancelled"], 1))
        # an RRULE master with overrides is not a conflict, and is skipped as recurring
        master = rev(1, extra="RRULE:FREQ=WEEKLY")
        found, client = read(master, a, rev(0, extra="RECURRENCE-ID;TZID=America/Los_Angeles:20261118T180000"))
        self.assertEqual((found, client.skipped["recurring"]), ([], 3))

    def test_raw_event_bound_and_single_get_remain_in_force(self):
        urls = []
        events = [rev(0, "20261101T120000Z", url="https://example.gov/agenda/0")] * 1999 + [R1]
        (m,), _ = read(*events, urls=urls)
        self.assertEqual((m["status"], urls), ("cancelled", [FEED]))
        with self.assertRaises(MeetingsError):
            read(*events, R1)
        unique = [rev(uid="u0@example.gov"), rev(uid="r0@example.gov", extra="RDATE:20261201T180000Z")]
        found, client = read(*unique, rev(uid=""), rev(uid="x" * 201))
        self.assertEqual((len(found), client.skipped),
                         (1, {"floating_time": 0, "unknown_zone": 0, "recurring": 1, "uid_too_long": 1, "no_uid": 1,
                              "bad_start": 0}))


class RevisionCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "c"
        shutil.copytree(EXAMPLE, self.root)
        self.kit = self.root / "kit" / "meetings.json"

    def scan(self, *vevents, name="feed.ics"):
        path = self.tmp / name
        path.write_bytes(wrap(*vevents).encode("utf-8"))
        argv = ["campaign_tool", "meetings", "--directory", str(self.root), "--platform", "ics", "--since", "2026-10-01",
                "--feed-url", FEED, "--body", "Example City Council", "--from-ics", str(path)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "argv", argv):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_cli_reports_real_cancellation_and_move_after_revision_selection(self):
        other = rev(0, "20261101T120000Z", uid="moved@example.gov", start="20261120T180000")
        self.assertEqual(self.scan(R0, other)[0], 0)
        first = json.loads(self.kit.read_text(encoding="utf-8"))["meetings"]
        moved = rev(1, "20261102T120000Z", uid="moved@example.gov", start="20261121T180000")
        code, out, _ = self.scan(R1, other, moved, R0, name="after.ics")
        self.assertEqual((code, json.loads(out)["cancelled_meetings"]), (0, 1))
        doc = json.loads(self.kit.read_text(encoding="utf-8"))
        ids = {m["event_id"]: m["id"] for m in first}
        self.assertEqual(doc["changes"]["cancelled"], [ids[UID]])
        self.assertEqual({r["id"]: (r["from"], r["to"]) for r in doc["changes"]["rescheduled"]},
                         {ids[UID]: ("2026-11-11T18:00:00-08:00", "2026-11-12T18:00:00-08:00"),
                          ids["moved@example.gov"]: ("2026-11-20T18:00:00-08:00", "2026-11-21T18:00:00-08:00")})
        self.assertEqual({m["id"]: (m["source"], m["body"]) for m in doc["meetings"]},
                         {i: ("ics", "Example City Council") for i in ids.values()})

    def test_cli_conflict_refuses_without_overwriting_prior_kit(self):
        self.assertEqual(self.scan(R0)[0], 0)
        before = self.kit.read_bytes()
        for order in ((R1, rev(1, "20261101T120000Z", start="20261112T180000")), (rev(1, "20261101T120000Z"), R1),
                      (rev("-1"), R0)):
            code, out, err = self.scan(*order, name="conflict.ics")
            self.assertEqual((code, out, self.kit.read_bytes()), (1, "", before))
            self.assertNotIn("secret", err)


if __name__ == "__main__":
    unittest.main()
