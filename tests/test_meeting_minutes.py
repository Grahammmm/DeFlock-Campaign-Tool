"""DF2207-05: meetings-outcome --minutes-url URL --minutes-approved-on D confirms a recorded outcome against the body's
approved official minutes; pages, open data and `today` tell the campaign's record from the confirmed one.

Synthetic content only (temp copies of the fictional example, injected ``today``); nothing is fetched.
Run from the repository root: python3 -B -m unittest tests.test_meeting_minutes -v
"""
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from campaign_tool import law, today
from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.meeting_outcome import record_minutes
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "meetings-open-data.schema.json").read_text(encoding="utf-8"))
MID = "cedar-council-2026-10"  # starts_at 2026-10-14T18:00:00-07:00, agency_id ca-cedar-city-council
POLICE = "ca-cedar-police"  # the example agency that has a page: /agencies/cedar.html
TODAY = date(2026, 10, 20)
SOURCE = "https://example.invalid/cedar-council/minutes"
VIDEO = "https://video.example.invalid/cedar-council/2026-10-14.mp4"
MINUTES = "https://example.invalid/cedar-council/approved-minutes-2026-10-14.pdf"
OTHER_MINUTES = "https://example.invalid/cedar-council/approved-minutes-v2.pdf"
BASE = {"result": "approved", "on": "2026-10-14", "source_url": SOURCE, "verified_by": "Synthetic Organizer",
        "verified_at": "2026-10-20", "vote": "4-1", "recording_url": VIDEO, "recording_start": "0:10:00"}
PARTICIPATION = {"in_person": True, "remote": None,
                 "written_comment": {"how": "Email the synthetic clerk", "deadline": "2026-10-13T12:00:00-07:00"},
                 "source_url": SOURCE, "verified_by": "Synthetic Organizer", "verified_at": "2026-10-10"}
LINE = ('<p class="meeting-outcome">Outcome: Approved on <time datetime="2026-10-14">2026-10-14</time> (4-1)'
        f' · <a href="{SOURCE}" rel="noopener noreferrer">Official record</a> · verified by Synthetic Organizer'
        f' · <a href="{VIDEO}" rel="noopener noreferrer">Watch the discussion</a> (from 0:10:00)</p>')
CONFIRMED = (f'<p class="meeting-minutes"><a href="{MINUTES}" rel="noopener noreferrer">'
             'Confirmed in the minutes approved on 2026-10-20</a></p>')
PENDING = '<p class="meeting-minutes">Recorded by the campaign; minutes not yet approved</p>'


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class MinutesBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-minutes-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.meeting = self.meetings / "2026-10-cedar-council.json"
        document = json.loads(self.meeting.read_text(encoding="utf-8"))
        document.update(participation=PARTICIPATION, outcome=dict(BASE))
        self.write(self.meeting, document)

    def write(self, path, document):
        path.write_text(json.dumps(document), encoding="utf-8")

    def doc(self, path=None):
        return json.loads((path or self.meeting).read_text(encoding="utf-8"))

    def add(self, ident, starts_at, outcome=None, **extra):
        record = {"id": ident, "body": f"Synthetic board {ident} (fictional)", "starts_at": starts_at, **extra}
        if outcome is not None:
            record["outcome"] = outcome
        self.write(self.meetings / f"{ident}.json", record)

    def cli(self, *extra, ident=MID):
        return run("meetings-outcome", "--directory", str(self.root), "--id", ident, *extra)

    def refused(self, needle, path=None, ident=MID, url=MINUTES, day="2026-10-20", **kwargs):
        path = path or self.meeting
        before = path.read_bytes()
        with self.assertRaises(ValueError) as caught:
            record_minutes(self.root, ident, url, day, today=kwargs.pop("today", TODAY), **kwargs)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(path.read_bytes(), before)

    def pages(self, today_=TODAY):
        build_site(self.root, today=today_)
        public = self.root / "public"
        return ((public / "meetings.html").read_text(encoding="utf-8").split('id="past-meetings"', 1)[1],
                (public / "meetings" / f"{MID}.html").read_text(encoding="utf-8"))

    def snapshot(self):
        public = self.root / "public"
        return {p.relative_to(public).as_posix(): p.read_bytes() for p in public.rglob("*") if p.is_file()}


class MinutesCliTests(MinutesBase):
    def test_minutes_only_cli_preserves_outcome_and_meeting(self):
        before = self.doc()
        result = self.cli("--minutes-url", MINUTES, "--minutes-approved-on", "2026-10-20")
        self.assertEqual(result.returncode, 0, result.stderr)
        printed, notice = result.stdout.rsplit("}", 1)
        expected = {**BASE, "minutes_url": MINUTES, "minutes_approved_on": "2026-10-20"}
        self.assertEqual(json.loads(printed + "}"), expected)
        self.assertEqual(notice.strip(), "Recorded only. Nothing was published.")
        self.assertEqual(self.doc(), {**before, "outcome": expected})  # votes, recording, verifier, participation, kit
        self.assertFalse((self.root / "public").exists())
        stored = self.meeting.read_bytes()
        again = self.cli("--minutes-url", OTHER_MINUTES, "--minutes-approved-on", "2026-10-21")
        self.assertEqual(again.returncode, 1)
        self.assertIn("minutes_url", again.stderr)
        self.assertIn("--replace", again.stderr)
        self.assertEqual(self.meeting.read_bytes(), stored)
        replaced = self.cli("--minutes-url", OTHER_MINUTES, "--minutes-approved-on", "2026-10-21", "--replace")
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertNotIn("votes: none", replaced.stdout)
        self.assertTrue(replaced.stdout.rstrip().endswith("Recorded only. Nothing was published."))
        self.assertEqual(self.doc(), {**before, "outcome": {**BASE, "minutes_url": OTHER_MINUTES,
                                                              "minutes_approved_on": "2026-10-21"}})

    def test_minutes_require_existing_outcome_and_complete_pair(self):
        self.add("synthetic-no-outcome", "2026-10-01T18:00:00-07:00")
        self.refused("minutes_url: needs a recorded outcome", path=self.meetings / "synthetic-no-outcome.json",
                     ident="synthetic-no-outcome")
        self.add("synthetic-later", "2026-10-25T18:00:00-07:00")
        self.refused("has not happened yet", path=self.meetings / "synthetic-later.json", ident="synthetic-later")
        self.add("synthetic-cancelled", "2026-10-15T18:00:00-07:00", status="cancelled")
        self.refused("is cancelled", path=self.meetings / "synthetic-cancelled.json", ident="synthetic-cancelled")
        self.refused("no staged meeting synthetic-missing", ident="synthetic-missing")
        before = self.meeting.read_bytes()
        for flags, needle in ((("--minutes-url", MINUTES), "--minutes-approved-on"),
                              (("--minutes-approved-on", "2026-10-20"), "--minutes-url")):
            result = self.cli(*flags)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(needle, result.stderr)
        for mixed in ("--result", "--on", "--source", "--verified-by", "--vote", "--recording-url", "--recording-start"):
            result = self.cli("--minutes-url", MINUTES, "--minutes-approved-on", "2026-10-20", mixed, "x")
            self.assertEqual(result.returncode, 1, mixed)
            self.assertIn(mixed, result.stderr)
        votes = self.cli("--minutes-url", MINUTES, "--minutes-approved-on", "2026-10-20", "--votes", "nope.csv")
        self.assertEqual(votes.returncode, 1)
        self.assertIn("--votes", votes.stderr)
        missing = self.cli("--result", "approved")  # creation mode still needs all four fields
        self.assertEqual(missing.returncode, 1)
        for flag in ("--on", "--source", "--verified-by"):
            self.assertIn(flag, missing.stderr)
        self.assertEqual(self.meeting.read_bytes(), before)

    def test_minutes_refuse_symlinks_and_leave_the_target_alone(self):
        outside = self.tmp / "outside.json"
        shutil.copy(self.meeting, outside)
        outside_bytes = outside.read_bytes()
        self.meeting.unlink()
        self.meeting.symlink_to(outside)
        with self.assertRaises(ValueError) as caught:
            record_minutes(self.root, "2026-10-cedar-council", MINUTES, "2026-10-20", today=TODAY)  # the file stem
        self.assertIn("must not be a symlink", str(caught.exception))
        self.assertEqual(outside.read_bytes(), outside_bytes)
        self.meeting.unlink()
        shutil.copy(outside, self.meeting)
        shutil.move(str(self.meetings), str(self.tmp / "elsewhere"))
        self.meetings.symlink_to(self.tmp / "elsewhere")
        elsewhere = (self.tmp / "elsewhere" / "2026-10-cedar-council.json").read_bytes()
        with self.assertRaises(ValueError):
            record_minutes(self.root, MID, MINUTES, "2026-10-20", today=TODAY)
        self.assertEqual((self.tmp / "elsewhere" / "2026-10-cedar-council.json").read_bytes(), elsewhere)

    def test_legacy_creation_and_replacement_remain_intact(self):
        result = self.cli("--minutes-url", MINUTES, "--minutes-approved-on", "2026-10-20")
        self.assertEqual(result.returncode, 0, result.stderr)
        plain = self.cli("--result", "approved", "--on", "2026-10-14", "--source", SOURCE, "--verified-by", "Synthetic Organizer")
        self.assertEqual(plain.returncode, 1)
        self.assertIn("already has an outcome", plain.stderr)
        replaced = self.cli("--result", "denied", "--on", "2026-10-14", "--source", SOURCE, "--verified-by",
                            "Synthetic Organizer", "--replace")
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertIn("votes: none", replaced.stdout)
        stored = self.doc()["outcome"]
        self.assertNotIn("minutes_url", stored)  # a whole replacement drops the old confirmation
        self.assertNotIn("recording_url", stored)
        list_page, meeting_page = self.pages()
        for page in (list_page, meeting_page):
            self.assertIn(PENDING, page)
            self.assertNotIn("Confirmed in the minutes", page)
        found = [item for item in today.todo(self.root, TODAY + timedelta(days=60)) if item["kind"] == "minutes_pending"]
        self.assertEqual(len(found), 1)  # eligible for the reminder again until re-confirmed


class MinutesValidationTests(MinutesBase):
    def test_minutes_dates_and_urls_are_strict(self):
        for good in ("2026-10-14", "2026-10-20"):  # the meeting's own day and today, both inclusive
            self.assertEqual(record_minutes(self.root, MID, MINUTES, good, replace=True, today=TODAY)["minutes_approved_on"], good)
        bad_days = ("2026-10-13", "2026-10-21", "2026-10-2", "2026-1-20", "2026-10-20T10:00:00", "2026-10-20Z",
                    "2026-02-30", "2026-13-01", "20261020", " 2026-10-20", "2026-10-20\n", "\uff12026-10-20",
                    "\u0662\u0660\u0662\u0666-10-20", "", None, 20261020, ["2026-10-20"])
        for day in bad_days:
            with self.subTest(day=day):
                self.refused("minutes_approved_on", day=day, replace=True)
        long_url = "https://example.invalid/" + "a" * 477
        self.assertEqual(len(long_url), 501)
        bad_urls = ("http://example.invalid/m.pdf", "https://user:pw@example.invalid/m.pdf",  # pragma: allowlist secret
                    "https://example.invalid/m y.pdf", 'https://example.invalid/"onmouseover="x',
                    "https://example.invalid/<script>", "https://example.invalid/m\n.pdf", long_url,
                    "javascript:alert(1)", "", None, 5)
        for url in bad_urls:
            with self.subTest(url=url):
                self.refused("minutes_url", url=url, replace=True)
        self.assertEqual(len(record_minutes(self.root, MID, "https://example.invalid/" + "a" * 476, "2026-10-20",
                                            replace=True, today=TODAY)["minutes_url"]), 500)
        query = MINUTES + "?id=1&x=2"
        record_minutes(self.root, MID, query, "2026-10-20", replace=True, today=TODAY)
        self.assertEqual(self.doc()["outcome"]["minutes_url"], query)  # stored verbatim
        for page in self.pages():
            self.assertIn(f'<a href="{MINUTES}?id=1&amp;x=2" rel="noopener noreferrer">Confirmed in the minutes approved on '
                          "2026-10-20</a>", page)
            self.assertNotIn("<iframe", page)
            self.assertNotIn("<embed", page)

    def test_leap_day_is_a_real_day(self):
        self.add("synthetic-leap", "2028-02-29T10:00:00-08:00",
                 outcome={**BASE, "on": "2028-02-29", "verified_at": "2028-02-29"})
        path = self.meetings / "synthetic-leap.json"
        for day in ("2027-02-29", "2028-02-30", "2028-02-28"):
            self.refused("minutes_approved_on", path=path, ident="synthetic-leap", day=day, today=date(2028, 3, 1))
        result = record_minutes(self.root, "synthetic-leap", MINUTES, "2028-02-29", today=date(2028, 3, 1))
        self.assertEqual(result["minutes_approved_on"], "2028-02-29")

    def test_offset_date_of_the_meeting_is_used(self):
        # 2026-10-14T23:30-07:00 is already 2026-10-15 in UTC, but the meeting's own date is the 14th.
        self.add("synthetic-night", "2026-10-14T23:30:00-07:00",
                 outcome={**BASE, "on": "2026-10-14", "verified_at": "2026-10-14"})
        path = self.meetings / "synthetic-night.json"
        self.refused("minutes_approved_on", path=path, ident="synthetic-night", day="2026-10-13")
        self.assertEqual(record_minutes(self.root, "synthetic-night", MINUTES, "2026-10-14", today=TODAY)["minutes_approved_on"],
                         "2026-10-14")

    def test_stored_minutes_use_same_validation(self):
        pair = {"minutes_url": MINUTES, "minutes_approved_on": "2026-10-20"}
        cases = [({"minutes_url": MINUTES}, "outcome.minutes_approved_on"),
                 ({"minutes_approved_on": "2026-10-20"}, "outcome.minutes_url"),
                 ({**pair, "minutes_url": None}, "outcome.minutes_url"),
                 ({**pair, "minutes_approved_on": None}, "outcome.minutes_approved_on"),
                 ({**pair, "minutes_url": "http://example.invalid/m.pdf"}, "outcome.minutes_url"),
                 ({**pair, "minutes_url": "https://example.invalid/" + "a" * 480}, "outcome.minutes_url: at most 500"),
                 ({**pair, "minutes_approved_on": "2026-10-13"}, "outcome.minutes_approved_on"),
                 ({**pair, "minutes_approved_on": "2026-1-20"}, "outcome.minutes_approved_on"),
                 ({**pair, "minutes_approved_on": "2026-10-20T00:00:00"}, "outcome.minutes_approved_on"),
                 ({**pair, "minutes_approved_on": "2999-01-01"}, "outcome.minutes_approved_on"),
                 ({**pair, "minutes_approved_on": 20261020}, "outcome.minutes_approved_on")]
        legacy_hash = digest(self.meeting)
        build_site(self.root, today=TODAY)  # the legacy file: no minutes keys, nothing rewritten by load or build
        self.assertEqual(content_problems(self.root), [])
        self.assertEqual(load_content(self.root)["meetings"][0]["outcome"].keys(), BASE.keys())
        self.assertEqual(digest(self.meeting), legacy_hash)
        before = self.snapshot()
        for changes, needle in cases:
            document = self.doc()
            document["outcome"] = {**BASE, **changes}
            self.write(self.meeting, document)
            stored = digest(self.meeting)
            with self.subTest(changes=changes):
                self.assertTrue(any(needle in problem for problem in content_problems(self.root)), needle)
                with self.assertRaises(ContentError) as caught:
                    load_content(self.root)
                self.assertIn(needle, str(caught.exception))
                built = run("build", "--directory", str(self.root))
                self.assertEqual(built.returncode, 1, built.stdout)
                self.assertIn(needle, built.stderr)
                self.assertEqual(self.snapshot(), before)  # the earlier public output is untouched
                self.assertEqual(digest(self.meeting), stored)
        document = self.doc()
        document["outcome"] = {**BASE, **pair}
        self.write(self.meeting, document)
        self.assertEqual(content_problems(self.root), [])
        self.assertEqual(load_content(self.root)["meetings"][0]["outcome"]["minutes_url"], MINUTES)


class MinutesPagesTests(MinutesBase):
    def confirm(self, **changes):
        document = self.doc()
        document["outcome"] = {**document["outcome"], "minutes_url": MINUTES, "minutes_approved_on": "2026-10-20", **changes}
        self.write(self.meeting, document)

    @staticmethod
    def card(page, ident):
        return page.split(f'<article id="meeting-{ident}">', 1)[1].split("</article>", 1)[0]

    def test_confirmation_on_meeting_index_and_matching_agency(self):
        self.confirm()
        document = self.doc()
        document["agency_id"] = POLICE
        self.write(self.meeting, document)
        other = {**BASE, "on": "2026-10-02", "verified_at": "2026-10-03"}
        self.add("other-agency", "2026-10-02T18:00:00-07:00", outcome=other, agency_id="ca-cedar-city-council")
        self.add("unassigned", "2026-10-03T18:00:00-07:00", outcome={**other, "on": "2026-10-03", "verified_at": "2026-10-03"})
        self.add("police-empty", "2026-10-01T18:00:00-07:00", agency_id=POLICE)
        self.add("police-cancelled", "2026-10-05T18:00:00-07:00", agency_id=POLICE, status="cancelled")
        self.add("police-upcoming", "2026-11-05T18:00:00-07:00", agency_id=POLICE)
        self.add("police-pending", "2026-09-20T18:00:00-07:00", agency_id=POLICE,
                 outcome={**other, "on": "2026-09-20", "verified_at": "2026-09-21"})
        listing, page = self.pages()
        self.assertIn(LINE + CONFIRMED, page)  # the old paragraph is unchanged and the new one follows it
        self.assertIn(LINE + CONFIRMED, listing)
        self.assertEqual(self.card(listing, "other-agency").count(PENDING), 1)
        self.assertNotIn("meeting-minutes", self.card(listing, "police-empty"))
        self.assertNotIn("meeting-minutes", self.card(listing, "police-cancelled"))
        agency = (self.root / "public" / "agencies" / "cedar.html").read_text(encoding="utf-8")
        past = agency.split("<h3>Past meetings</h3>", 1)[1]
        self.assertIn(LINE + CONFIRMED, past)
        self.assertEqual(self.card(past, "police-pending").count(PENDING), 1)
        self.assertIn("Cancelled:", self.card(past, "police-cancelled"))
        self.assertNotIn("meeting-minutes", self.card(past, "police-empty"))
        self.assertLess(past.index("police-cancelled"), past.index("police-pending"))  # same order as the meetings list
        self.assertNotIn("other-agency", past)
        self.assertNotIn("unassigned", past)
        upcoming = agency.split("<h3>Upcoming meetings</h3>", 1)[1].split("<h3>Past meetings</h3>", 1)[0]
        self.assertIn("police-upcoming", upcoming)
        self.assertNotIn("meeting-minutes", upcoming)
        self.assertNotIn("<iframe", page + listing + agency)
        self.assertNotIn(f'src="{MINUTES}', page + listing + agency)

    def test_agency_without_its_own_past_meetings_has_no_section(self):
        self.confirm()
        self.pages()  # the meeting belongs to the council; the police page has no past meeting
        agency = (self.root / "public" / "agencies" / "cedar.html").read_text(encoding="utf-8")
        self.assertNotIn("Past meetings", agency)
        self.assertNotIn("meeting-minutes", agency)

    def test_no_outcome_shows_neither_paragraph_and_custom_labels_take_the_date(self):
        document = self.doc()
        del document["outcome"]
        self.write(self.meeting, document)
        listing, page = self.pages()
        self.assertNotIn("meeting-minutes", listing + page)
        document["outcome"] = dict(BASE)
        self.write(self.meeting, document)
        self.confirm()
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "meeting.minutes_confirmed": "Confirmado en el acta aprobada el {date}",
            "meeting.minutes_pending": "Registrado por la campa\u00f1a; acta a\u00fan sin aprobar"}}), encoding="utf-8")
        for rendered in self.pages():
            self.assertIn('rel="noopener noreferrer">Confirmado en el acta aprobada el 2026-10-20</a></p>', rendered)
            self.assertNotIn("Confirmed in the minutes", rendered)
        self.add("other-pending", "2026-10-02T18:00:00-07:00", outcome={**BASE, "on": "2026-10-02", "verified_at": "2026-10-03"})
        self.assertIn("Registrado por la campa\u00f1a; acta a\u00fan sin aprobar", self.pages()[0])

    def test_open_data_minutes_pair_allowlist_and_schema(self):
        self.confirm(minutes_url=MINUTES + "?id=1&x=2")
        self.add("legacy-one", "2026-10-02T18:00:00-07:00", outcome={**BASE, "on": "2026-10-02", "verified_at": "2026-10-03"})
        build_site(self.root, today=TODAY)
        text = (self.root / "public" / "data" / "meetings.json").read_text(encoding="utf-8")
        doc = json.loads(text)
        law.validate(doc, SCHEMA)
        meetings = {m["id"]: m for m in doc["meetings"]}
        self.assertEqual(meetings[MID]["outcome"], {"result": "approved", "on": "2026-10-14", "source_url": SOURCE, "vote": "4-1",
                                                    "minutes_url": MINUTES + "?id=1&x=2", "minutes_approved_on": "2026-10-20"})
        self.assertEqual(meetings["legacy-one"]["outcome"], {"result": "approved", "on": "2026-10-02", "source_url": SOURCE,
                                                             "vote": "4-1"})  # absent keys are omitted, not null
        for private in ("verified_by", "verified_at", "Synthetic Organizer", "participation", "confirmed", "recording"):
            self.assertNotIn(private, text)
        build_site(self.root, today=TODAY)
        self.assertEqual((self.root / "public" / "data" / "meetings.json").read_text(encoding="utf-8"), text)
        checked = run("build", "--directory", str(self.root), "--check")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        # the schema: the pair is accepted whole, legacy and empty documents are accepted, anything else is refused
        outcome = {"result": "approved", "on": "2026-10-14", "source_url": SOURCE}
        shell = {"schema_version": 1, "generated_at": "2026-10-20T00:00:00Z"}
        row = {"id": "m", "body": "b", "agency_id": None, "starts_at": "2026-10-14T18:00:00-07:00", "status": "scheduled",
               "agenda_url": None, "url": "/meetings/m.html"}
        accepted = ({**shell, "meetings": []}, {**shell, "meetings": [{**row, "outcome": outcome}]},
                    {**shell, "meetings": [{**row, "outcome": {**outcome, "minutes_url": MINUTES, "minutes_approved_on": "2026-10-20"}}]})
        for document in accepted:
            law.validate(document, SCHEMA)
        refused = ({"minutes_url": MINUTES}, {"minutes_approved_on": "2026-10-20"},
                   {"minutes_url": MINUTES, "minutes_approved_on": "2026-10-20T00:00:00"},
                   {"minutes_url": "http://example.invalid/m", "minutes_approved_on": "2026-10-20"},
                   {"minutes_url": MINUTES, "minutes_approved_on": "2026-10-20", "minutes_confirmed": True},
                   {"minutes_url": MINUTES, "minutes_approved_on": "2026-10-20", "verified_by": "x"})
        for extra in refused:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                law.validate({**shell, "meetings": [{**row, "outcome": {**outcome, **extra}}]}, SCHEMA)


class MinutesTodayTests(MinutesBase):
    def setUp(self):
        super().setUp()
        self.meeting.unlink()
        self.start = date(2026, 3, 1)

    def put(self, ident, starts="2026-03-01", on="2026-03-02", verified="2026-03-02", **extra):
        outcome = {"result": "approved", "on": on, "source_url": SOURCE, "verified_by": "Synthetic Organizer",
                   "verified_at": verified, **extra}
        self.add(ident, starts + "T18:00:00-08:00", outcome=outcome)

    def pending(self, as_of):
        return [item for item in today.todo(self.root, as_of) if item["kind"] == "minutes_pending"]

    def test_today_minutes_age_boundary_and_isolation(self):
        self.put("age-a")
        on = date(2026, 3, 2)
        for days, expected in ((44, 0), (45, 0), (46, 1)):
            with self.subTest(days=days):
                self.assertEqual(len(self.pending(on + timedelta(days=days))), expected)
        as_of = on + timedelta(days=46)
        item, = self.pending(as_of)
        self.assertEqual(item["due"], "2026-03-02")
        self.assertIn("age-a", item["what"])
        self.assertIn("Synthetic board age-a (fictional)", item["what"])
        self.assertIn("2026-03-02", item["what"])
        self.assertIn("check whether", item["what"])
        self.assertNotIn("overdue", item["what"].lower())
        self.assertNotIn("late", item["what"].lower().split())
        self.assertEqual(item["command"].split()[:1], ["meetings-outcome"])
        self.assertIn("--directory " + shlex.quote(str(self.root)), item["command"])
        self.assertTrue(item["command"].endswith("--id age-a --minutes-url OFFICIAL_MINUTES_URL --minutes-approved-on YYYY-MM-DD"))
        self.assertEqual(set(item), {"rank", "kind", "what", "due", "command"})
        self.assertNotIn("--online", item["command"])
        # an old meeting with a young outcome is absent; an old outcome with a freshly checked verifier is present
        self.put("old-meeting-young-outcome", starts="2026-01-01", on=(as_of - timedelta(days=10)).isoformat(),
                 verified=(as_of - timedelta(days=10)).isoformat())
        self.put("old-outcome-fresh-check", on="2026-03-02", verified=(as_of - timedelta(days=1)).isoformat())
        self.put("confirmed", minutes_url=MINUTES, minutes_approved_on="2026-03-10")
        self.put("also-old", on="2026-03-01", verified="2026-03-01")
        found = self.pending(as_of)
        self.assertEqual([entry["command"].split("--id ")[1].split()[0] for entry in found],
                         ["also-old", "age-a", "old-outcome-fresh-check"])  # by outcome date, then meeting id
        self.assertEqual([entry["due"] for entry in found], ["2026-03-01", "2026-03-02", "2026-03-02"])

    def test_today_ranks_after_contracts_before_stale_kits_and_ignores_comments_sent(self):
        self.put("age-a")
        self.assertLess(today.KINDS.index("contract_ending"), today.KINDS.index("minutes_pending"))
        self.assertLess(today.KINDS.index("minutes_pending"), today.KINDS.index("stale_comment_kit"))
        as_of = date(2026, 6, 1)
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "comments-sent.json").write_text(json.dumps({"schema_version": 1, "comments": {
            "age-a": {"on": "2026-03-02", "channel": "email"}}}), encoding="utf-8")
        self.assertEqual(len(self.pending(as_of)), 1)  # a submitted comment affects deadlines only

    def test_today_json_text_parity_and_no_writes(self):
        self.put("age-a")
        as_of = "2026-06-01"
        files = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        text = today.run_cli(self.root, as_of)
        items = json.loads(today.run_cli(self.root, as_of, as_json=True))
        line = next(entry for entry in items if entry["kind"] == "minutes_pending")
        self.assertIn(f"[minutes_pending] {line['what']} — {line['command']}", text)
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, files)

    def test_one_malformed_meeting_never_hides_another(self):
        self.put("age-a")
        (self.meetings / "broken.json").write_text('{"id": "broken", "body": "x", "starts_at": "soon"}', encoding="utf-8")
        bad = {"result": "approved", "on": "2026-03-02", "source_url": SOURCE, "verified_by": "x", "verified_at": "2026-03-02",
               "minutes_url": MINUTES}
        self.add("half-pair", "2026-03-01T18:00:00-08:00", outcome=bad)
        items = today.todo(self.root, date(2026, 6, 1))
        self.assertEqual([i["kind"] for i in items if i["kind"] == "minutes_pending"], ["minutes_pending"])
        unreadable = [i["what"] for i in items if i["kind"] == "unreadable"]
        self.assertEqual(sum("broken.json" in w for w in unreadable), 1)
        self.assertEqual(sum("half-pair.json" in w and "minutes_approved_on" in w for w in unreadable), 1)


if __name__ == "__main__":
    unittest.main()
