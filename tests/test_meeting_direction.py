"""DF0022-08: an outcome may record a direction the body gave its staff (``--direction`` ``--return-by``), as the official
source states it. The meeting page attributes it, open data carries it and `today` reminds the organizer when the stated
return-by date is near or past with no later outcome of the same body. A later outcome is a workflow cue only.

Synthetic content only (temp copies of the fictional example, injected ``today``); nothing is fetched.
Run from the repository root: python3 -B -m unittest tests.test_meeting_direction -v
"""
import argparse
import contextlib
import io
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import law, legal_claims, meeting_outcome, today
from campaign_tool.content import ContentError, OUTCOME_RESULTS, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.meeting_outcome import MeetingOutcomeError, record_direction, record_outcome
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "meetings-open-data.schema.json").read_text(encoding="utf-8"))
MID = "cedar-council-2026-10"  # starts_at 2026-10-14T18:00:00-07:00
BODY = "Cedar City Council (fictional)"
OTHER_BODY = "Cedar Planning Commission (fictional)"
TODAY = date(2026, 10, 20)
SOURCE = "https://example.invalid/cedar-council/2026-10-14-minutes"
MINUTES = "https://example.invalid/cedar-council/approved-minutes"
DIRECTION_SOURCE = "https://example.invalid/cedar-council/staff-report?item=7&view=full"
TEXT = "Return with a draft usage policy"
BASE = {"result": "approved", "on": "2026-10-14", "source_url": SOURCE, "verified_by": "Synthetic Organizer",
        "verified_at": "2026-10-20"}
CREATE = ("--result", "approved", "--on", "2026-10-14", "--source", SOURCE, "--verified-by", "Synthetic Organizer")
NOTICE = "Recorded only. Nothing was published."
TRIO = ("direction", "return_by", "direction_source")


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


class Base(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-direction-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "synthetic campaign"  # a space: the paths in `today` commands are quoted
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.meeting = self.meetings / "2026-10-cedar-council.json"
        self.original = self.meeting.read_bytes()

    def write(self, path, document):
        path.write_text(json.dumps(document), encoding="utf-8")

    def doc(self):
        return json.loads(self.meeting.read_text(encoding="utf-8"))

    def add(self, ident, starts_at, outcome=None, body=BODY, **extra):
        record = {"id": ident, "body": body, "starts_at": starts_at, **extra}
        if outcome is not None:
            record["outcome"] = outcome
        self.write(self.meetings / f"{ident}.json", record)

    def set_outcome(self, **changes):
        self.write(self.meeting, {**json.loads(self.original), "outcome": {**BASE, **changes}})

    def cli(self, *extra, ident=MID):
        return run("meetings-outcome", "--directory", str(self.root), "--id", ident, *extra)

    def staged(self):
        return {p.name: p.read_bytes() for p in self.meetings.glob("*.json")}

    def public(self):
        public = self.root / "public"
        return {p.relative_to(public).as_posix(): p.read_bytes() for p in public.rglob("*") if p.is_file()}

    def page(self, ident=MID):
        return (self.root / "public" / "meetings" / f"{ident}.html").read_text(encoding="utf-8")

    def direct(self, text=TEXT, day="2026-11-01", **kwargs):
        return record_direction(self.root, MID, text, day, today=TODAY, **kwargs)

    def refuse(self, needle, *args, **kwargs):
        before = self.staged()
        with self.assertRaises(ValueError) as caught:
            self.direct(*args, **kwargs)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(self.staged(), before)  # no partial write


class ModeTests(Base):
    def test_creation_and_direction_only_modes(self):
        flags = ("--direction", TEXT, "--return-by", "2026-11-01")
        for result in OUTCOME_RESULTS:  # any result may carry a direction; the source defaults to the outcome source
            self.meeting.write_bytes(self.original)
            extra = ("--continued-to-date", "2026-12-01") if result == "continued" else ()
            done = self.cli(*CREATE[:1], result, *CREATE[2:], *flags, *extra, "--replace")
            self.assertEqual(done.returncode, 0, done.stderr)
            outcome = self.doc()["outcome"]
            self.assertEqual({k: outcome[k] for k in TRIO}, {"direction": TEXT, "return_by": "2026-11-01",
                                                             "direction_source": SOURCE})
            self.assertEqual(done.stdout.rstrip().splitlines()[-1], NOTICE)
        self.meeting.write_bytes(self.original)
        for extra, needle in (
                (CREATE + ("--direction", TEXT), "return_by: needed together with direction"),
                (CREATE + ("--return-by", "2026-11-01"), "direction: needed together with return_by"),
                (CREATE + ("--direction-source", SOURCE), "direction_source: needs direction and return_by"),
                (("--result", "approved") + flags, "--on, --source, --verified-by: required to record an outcome"),
                (("--minutes-url", MINUTES, "--minutes-approved-on", "2026-10-20") + flags,
                 "minutes mode cannot be combined with direction flags"),
                (("--minutes-url", MINUTES, "--minutes-approved-on", "2026-10-20", "--direction-source", SOURCE),
                 "minutes mode cannot be combined with direction flags"),
                (("--direction-source", SOURCE), "direction_source: needs direction and return_by"),
                (("--direction", TEXT), "return_by: needed together with direction"),
                (("--return-by", "2026-11-01"), "direction: needed together with return_by")):
            with self.subTest(extra=extra):
                done = self.cli(*extra)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(needle, done.stderr)
                self.assertEqual(self.meeting.read_bytes(), self.original)
        done = self.cli(*flags)  # direction-only on a meeting with no outcome: no implicit creation
        self.assertEqual(done.returncode, 1)
        self.assertIn("direction: needs a recorded outcome", done.stderr)
        self.assertEqual(self.meeting.read_bytes(), self.original)
        # direction-only on a recorded outcome prints the whole outcome, the notice and no `votes: none`
        self.set_outcome(vote="4-1")
        done = self.cli(*flags, "--direction-source", DIRECTION_SOURCE)
        self.assertEqual(done.returncode, 0, done.stderr)
        printed, notice = done.stdout.rsplit("}", 1)
        self.assertEqual(json.loads(printed + "}"), {**BASE, "vote": "4-1", "direction": TEXT, "return_by": "2026-11-01",
                                                     "direction_source": DIRECTION_SOURCE})
        self.assertEqual(notice.strip(), NOTICE)
        self.assertNotIn("votes: none", done.stdout)
        done = self.cli(*flags, "--replace")  # replacing keeps the update-only output too
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("votes: none", done.stdout)
        stored = self.meeting.read_bytes()
        for flag, value in (("--vote", "1-0"), ("--votes", "x.csv"), ("--recording-url", SOURCE),
                            ("--recording-start", "0:01:00"), ("--continued-to", "x"), ("--continued-to-date", "2026-12-01")):
            with self.subTest(flag=flag):
                done = self.cli(*flags, "--replace", flag, value)
                self.assertEqual(done.returncode, 1)
                self.assertIn(f"direction-only mode cannot be combined with {flag}", done.stderr)
                self.assertEqual(self.meeting.read_bytes(), stored)
        self.assertEqual(run("meetings-outcome", "--id", MID, *flags).returncode, 2)  # --directory stays required
        self.assertEqual(run("meetings-outcome", "--directory", str(self.root), *flags).returncode, 2)  # so does --id
        self.assertFalse((self.root / "public").exists())

    def test_unavailable_meetings_refuse_by_name(self):
        flags = ("--direction", TEXT, "--return-by", "2026-11-01")
        self.add("cancelled-one", "2026-10-02T18:00:00-07:00", status="cancelled")
        self.add("future-one", "2099-10-27T18:00:00-07:00")
        self.add("held-one", "2026-10-03T18:00:00-07:00", outcome={**BASE, "on": "2026-10-03"})
        for ident, needle in (("cancelled-one", "cancelled"), ("future-one", "has not happened yet"),
                              ("ghost", "no staged meeting ghost")):
            with self.subTest(ident):
                before = self.staged()
                for extra in (flags, CREATE + flags):  # direction-only and creation modes
                    done = self.cli(*extra, ident=ident)
                    self.assertEqual(done.returncode, 1)
                    self.assertIn(needle, done.stderr)
                    self.assertEqual(self.staged(), before)
        done = self.cli(*flags, ident="held-one")  # a held meeting with an outcome still works
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_replace_semantics_full_and_minutes(self):
        flags = ("--direction", TEXT, "--return-by", "2026-11-01")
        self.assertEqual(self.cli(*CREATE, *flags).returncode, 0)
        done = self.cli(*CREATE)  # a second full record needs --replace, as before
        self.assertIn("pass --replace", done.stderr)
        self.assertEqual(self.cli(*CREATE, "--replace").returncode, 0)  # whole replacement drops the omitted direction
        self.assertFalse(set(TRIO) & set(self.doc()["outcome"]))
        self.assertEqual(self.cli(*flags).returncode, 0)
        done = self.cli("--minutes-url", MINUTES, "--minutes-approved-on", "2026-10-20", "--replace")  # minutes keep it
        self.assertEqual(done.returncode, 0, done.stderr)
        outcome = self.doc()["outcome"]
        self.assertEqual((outcome["direction"], outcome["minutes_url"]), (TEXT, MINUTES))


class ValidationTests(Base):
    def test_direction_validation_and_source_default(self):
        self.set_outcome(minutes_url=MINUTES, minutes_approved_on="2026-10-20")
        for text in ("x", "y" * 200, 'Say "hello" & more', "Ünïcode ✓ text", "two  inner  spaces",
                     "Return with a 'draft' policy; & more"):
            with self.subTest(text=text):
                outcome = self.direct(text, replace=True)
                self.assertEqual(outcome["direction"], text)  # supplied valid text is kept exactly
        for text, why in (("", "empty"), ("   ", "spaces"), (" lead", "leading"), ("trail ", "trailing"),
                          ("y" * 201, "201"), ("a\nb", "newline"), ("a\tb", "tab"), ("a\rb", "cr"), ("a\x01b", "control"),
                          ("a<b", "angle"), ("a>b", "angle"), ("a\u202eb", "bidi"), ("\u00a0x", "nbsp edge"), (True, "bool"),
                          (5, "number"), (["x"], "list")):
            with self.subTest(why=why):
                self.refuse("direction: must be 1 to 200 characters of plain text on one line", text, replace=True)
        for day, ok in (("2028-02-29", True), ("2026-10-14", True), ("2026-10-15", True), ("2099-12-31", True),
                        ("2026-10-13", False), ("2000-01-01", False), ("2027-02-29", False), ("2026-02-30", False),
                        ("2026-10-5", False), ("2026-1-05", False), ("20261105", False), ("2026-11-05T00:00:00", False),
                        ("2026-11-05 ", False), ("２０２６-11-05", False), ("", False), (None, False), (True, False)):
            with self.subTest(day=day):
                if ok:
                    self.assertEqual(self.direct(day=day, replace=True)["return_by"], day)
                elif day is None:
                    self.refuse("return_by: needed together with direction", day=None, replace=True)
                else:
                    self.refuse("return_by: must be a YYYY-MM-DD date on or after the meeting", day=day, replace=True)
        # the source defaults to the outcome's own source, never the minutes; a stated one is kept unchanged
        self.assertEqual(self.direct(replace=True)["direction_source"], SOURCE)
        self.assertEqual(self.direct(replace=True, direction_source=DIRECTION_SOURCE)["direction_source"], DIRECTION_SOURCE)
        for url, needle in (("http://example.invalid/x", "must be an https:// URL"),
                            ("https://user:pw@example.invalid/x", "must be an https:// URL"),
                            ("https://example.invalid/x y", "must be an https:// URL"),
                            ("https://example.invalid/<b>", "must be an https:// URL"),
                            ("", "must be an https:// URL"), ("https://example.invalid/" + "a" * 500, "at most 500 characters")):
            with self.subTest(url=url):
                self.refuse(f"direction_source: {needle}", direction_source=url, replace=True)
        self.assertEqual(len(self.direct(direction_source="https://example.invalid/" + "a" * 476, replace=True)["direction_source"]), 500)
        self.assertEqual(content_problems(self.root), [])


class PreserveTests(Base):
    def test_partial_update_preserves_all_dependency_fields(self):
        self.add("later-one", "2026-10-16T18:00:00-07:00")
        rich = {**BASE, "vote": "4-1", "votes": [{"name": "Synthetic Member", "vote": "yes"}],
                "recording_url": "https://example.invalid/video", "recording_start": "0:12:30",
                "minutes_url": MINUTES, "minutes_approved_on": "2026-10-20", "result": "continued",
                "continued_to": "later-one"}
        raw = {**json.loads(self.original), "outcome": rich, "participation": {
            "in_person": True, "remote": None, "written_comment": None, "source_url": SOURCE,
            "verified_by": "Synthetic Verifier", "verified_at": "2026-10-10"}}
        self.write(self.meeting, raw)
        first = self.direct(day="2026-11-01")
        self.assertEqual(first, {**rich, "direction": TEXT, "return_by": "2026-11-01", "direction_source": SOURCE})
        self.assertEqual(self.doc(), {**raw, "outcome": first})  # every unrelated raw key survives
        stored = self.meeting.read_bytes()
        self.refuse("direction already recorded; pass --replace to overwrite it")
        self.assertEqual(self.meeting.read_bytes(), stored)
        second = self.direct("A different direction", "2026-12-01", direction_source=DIRECTION_SOURCE, replace=True)
        self.assertEqual({k: v for k, v in second.items() if k not in TRIO}, rich)  # only the trio changed
        self.assertEqual((second["direction"], second["return_by"], second["direction_source"]),
                         ("A different direction", "2026-12-01", DIRECTION_SOURCE))
        # a dangling continuation in the prospective document blocks the write
        self.add("later-one", "2026-10-16T18:00:00-07:00", status="cancelled")
        before = self.staged()
        self.refuse("target is cancelled", replace=True)
        self.assertEqual(self.staged(), before)

    def test_failed_atomic_write_and_symlinks_leave_originals(self):
        self.set_outcome()
        before = self.meeting.read_bytes()
        with mock.patch.object(meeting_outcome, "_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.direct()
        self.assertEqual(self.meeting.read_bytes(), before)
        outside = self.tmp / "outside.json"
        outside.write_bytes(before)
        self.meeting.unlink()
        self.meeting.symlink_to(outside)
        for ident in ("2026-10-cedar-council", MID):
            with self.assertRaises(MeetingOutcomeError):
                record_direction(self.root, ident, TEXT, "2026-11-01", today=TODAY)
        self.assertEqual(outside.read_bytes(), before)  # the link target outside the tree is never written
        self.assertTrue(self.meeting.is_symlink())
        with self.assertRaises(MeetingOutcomeError):
            record_outcome(self.root, "2026-10-cedar-council", "approved", "2026-10-14", SOURCE, "x", replace=True,
                           today=TODAY, direction=TEXT, return_by="2026-11-01")
        self.assertEqual(outside.read_bytes(), before)


class GuardTests(Base):
    RULE = "review-required: meetings/%s.outcome.direction: legal-claim wording (violation-verb)"

    def test_guard_alerts_without_refusal_or_text_leak(self):
        flags = ("--direction", "Staff violated the records law and must report", "--return-by", "2026-11-01")
        self.set_outcome()
        for extra in (flags, CREATE + flags + ("--replace",)):  # direction-only and creation
            done = self.cli(*extra)
            self.assertEqual(done.returncode, 0, done.stderr)  # alert only: the result and exit code are unchanged
            self.assertEqual(done.stderr.strip(), self.RULE % MID)
            self.assertNotIn("records law", done.stderr)  # names the rule and place, never the text
            self.assertEqual(json.loads(done.stdout.rsplit("}", 1)[0] + "}")["direction"], flags[1])  # stdout parses
        for text in ("Return with a draft usage policy", "According to the clerk, the policy requires a report",
                     "Report whether the agency violated the policy", 'Review the "violated the law" wording'):
            with self.subTest(text=text):
                done = self.cli("--direction", text, "--return-by", "2026-11-01", "--replace")
                self.assertEqual((done.returncode, done.stderr), (0, ""))
        # a guard fault is itself an alert, never a failure or a text dump
        with mock.patch.object(legal_claims, "_sentence_claims", side_effect=RuntimeError("boom")):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                meeting_outcome.run_outcome_command(self.root, self.args("--direction", "Anything here", "--return-by",
                                                                         "2026-11-01", "--replace"))
        self.assertEqual(stderr.getvalue().strip(), self.RULE.replace("violation-verb", "guard-error") % MID)

    def args(self, *extra):
        names = ("result", "on", "source", "verified_by", "minutes_url", "minutes_approved_on", "vote", "votes",
                 "recording_url", "recording_start", "continued_to", "continued_to_date", "direction", "return_by",
                 "direction_source")
        parsed = {name: None for name in names}
        parsed.update(id=MID, replace=False)
        for flag, value in zip(extra[::2] if extra else (), extra[1::2]):
            parsed[flag.lstrip("-").replace("-", "_")] = value
        parsed["replace"] = "--replace" in extra
        return argparse.Namespace(**parsed)

    def test_build_scans_stored_raw_directions(self):
        self.set_outcome(direction="Staff violated the records law", return_by="2026-11-01", direction_source=SOURCE)
        neutral = legal_claims.site_notes(self.root)
        self.assertEqual(neutral, [self.RULE % MID])
        built = run("build", "--directory", str(self.root))
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertIn(self.RULE % MID, built.stdout.splitlines())  # beside the other build notes
        self.assertNotIn("records law", built.stdout + built.stderr)
        self.set_outcome(direction="Return with a draft usage policy", return_by="2026-11-01", direction_source=SOURCE)
        self.assertEqual(legal_claims.site_notes(self.root), [])
        # the attributed page wording cannot mask an assertion: the raw text is what is scanned
        self.set_outcome(direction="Staff violated SB 34, according to the clerk", return_by="2026-11-01", direction_source=SOURCE)
        self.assertEqual(legal_claims.site_notes(self.root), [self.RULE % MID])
        # finding notes are unchanged; a second meeting adds its own note
        self.add("second", "2026-10-02T18:00:00-07:00", outcome={**BASE, "on": "2026-10-02", "direction": "It broke the law",
                                                                 "return_by": "2026-11-01", "direction_source": SOURCE})
        notes = legal_claims.site_notes(self.root)
        self.assertEqual(sorted(notes), sorted([self.RULE % MID, "review-required: meetings/second.outcome.direction: "
                                                "legal-claim wording (broke-the-law)"]))


class PublicTests(Base):
    EXPECTED = ('<p class="meeting-direction">Staff were directed to: Return with a &quot;draft&quot; &amp; usage policy '
                '(by 2026-11-01, as stated in <a href="https://example.invalid/cedar-council/staff-report?item=7&amp;view=full" '
                'rel="noopener noreferrer">https://example.invalid/cedar-council/staff-report?item=7&amp;view=full</a>)</p>')

    def test_page_schema_allowlists_and_legacy_bytes(self):
        self.set_outcome(minutes_url=MINUTES, minutes_approved_on="2026-10-20")
        build_site(self.root, today=TODAY)
        legacy, legacy_page = self.public(), self.page()
        self.assertNotIn("meeting-direction", legacy_page)
        self.assertNotIn("Staff were directed", "".join(v.decode() for k, v in legacy.items() if k.endswith(".html")))
        self.direct('Return with a "draft" & usage policy', direction_source=DIRECTION_SOURCE, replace=True)
        build_site(self.root, today=TODAY)
        page = self.page()
        self.assertEqual(page.count("meeting-direction"), 1)
        self.assertIn(self.EXPECTED, page)
        self.assertLess(page.index("meeting-minutes"), page.index("meeting-direction"))  # after the outcome content
        self.assertEqual([p for p, data in self.public().items() if b"meeting-direction" in data], ["meetings/%s.html" % MID])
        for other in ("meetings.html", "index.html"):
            self.assertNotIn("Staff were directed", (self.root / "public" / other).read_text(encoding="utf-8"))
        doc = json.loads((self.root / "public" / "data" / "meetings.json").read_text(encoding="utf-8"))
        law.validate(doc, SCHEMA)
        outcome = next(m for m in doc["meetings"] if m["id"] == MID)["outcome"]
        self.assertEqual({k: outcome[k] for k in TRIO}, {"direction": 'Return with a "draft" & usage policy',
                                                         "return_by": "2026-11-01", "direction_source": DIRECTION_SOURCE})
        text = (self.root / "public" / "data" / "meetings.json").read_text(encoding="utf-8")
        for private in ("verified_by", "verified_at", "Synthetic Organizer", "overdue", "reminder", "fulfil", "completed"):
            self.assertNotIn(private, text)
        # Spanish draft with its placeholders, and a campaign label that drops one placeholder still builds
        shutil.copy(REPO_ROOT / "examples" / "labels" / "es.json", self.root / "content" / "labels.json")
        build_site(self.root, today=TODAY)
        self.assertIn("Se indicó al personal que: Return with a &quot;draft&quot; &amp; usage policy (para el 2026-11-01, "
                      "según consta en <a href=", self.page())
        (self.root / "content" / "labels.json").unlink()
        # schema: all or none, bounded text and https source, no extra key
        shell = {"schema_version": 1, "generated_at": "2026-10-20T00:00:00Z"}
        row = {"id": "m", "body": "b", "agency_id": None, "starts_at": "2026-10-14T18:00:00-07:00", "status": "scheduled",
               "agenda_url": None, "url": "/meetings/m.html"}
        base = {"result": "approved", "on": "2026-10-14", "source_url": SOURCE}
        trio = {"direction": "x", "return_by": "2026-11-01", "direction_source": SOURCE}

        def check(**extra):
            law.validate({**shell, "meetings": [{**row, "outcome": {**base, **extra}}]}, SCHEMA)
        check()
        check(**trio)
        props = SCHEMA["$defs"]["outcome"]["properties"]  # the bounds are declared (the local validator skips maxLength)
        self.assertEqual((props["direction"]["maxLength"], props["direction_source"]["maxLength"]), (200, 500))
        for extra in ({"direction": "x"}, {"return_by": "2026-11-01"}, {"direction_source": SOURCE},
                      {"direction": "x", "return_by": "2026-11-01"}, {**trio, "direction": ""},
                      {**trio, "return_by": "2026-11-1"}, {**trio, "direction_source": "http://example.invalid/x"},
                      {**trio, "direction_done": True},
                      {**trio, "overdue": True}, {**trio, "verified_by": "x"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                check(**extra)
        # removing the direction gives back the old bytes
        self.set_outcome(minutes_url=MINUTES, minutes_approved_on="2026-10-20")
        build_site(self.root, today=TODAY)
        self.assertEqual(self.public(), legacy)

    def test_cancelled_and_future_meetings_export_and_show_nothing(self):
        self.add("future-one", "2026-10-27T18:00:00-07:00", outcome=None)
        self.set_outcome(direction=TEXT, return_by="2026-11-01", direction_source=SOURCE)
        build_site(self.root, today=date(2026, 10, 14))  # the meeting day itself is not over yet
        self.assertNotIn("meeting-direction", self.page())
        text = (self.root / "public" / "data" / "meetings.json").read_text(encoding="utf-8")
        self.assertNotIn(TEXT, text)


class TodayTests(Base):
    def pending(self, as_of=TODAY):
        return [i for i in today.todo(self.root, as_of) if i["kind"] == "direction_pending"]

    def stated(self, day, **extra):
        self.set_outcome(direction=TEXT, return_by=day, direction_source=DIRECTION_SOURCE, **extra)

    def test_today_fourteen_day_boundary_and_suppression(self):
        self.assertEqual(today.KINDS.index("direction_pending"), today.KINDS.index("continued_pending") + 1)
        for day, shown in (("2026-11-04", False), ("2026-11-03", True), ("2026-10-20", True), ("2026-10-19", True),
                           ("2026-10-14", True)):  # +15 / +14 / +0 / -1 / the meeting day, long past
            self.stated(day)
            with self.subTest(day=day):
                self.assertEqual(len(self.pending()), 1 if shown else 0)
        self.stated("2026-10-19")
        item, = self.pending()
        self.assertEqual(item["due"], "2026-10-19")
        self.assertEqual(item["what"], f"Staff direction for {BODY} ({MID}), return-by 2026-10-19: "
                                       "check the official record for a report back")
        self.assertEqual(item["command"], f"open {shlex.quote(DIRECTION_SOURCE)} and check for a report back; record a "
                         f"verified later outcome with meetings-outcome --directory {shlex.quote(str(self.root))} "
                         "--id MEETING_ID --result RESULT --on YYYY-MM-DD --source OFFICIAL_URL --verified-by NAME")
        self.assertEqual(set(item), {"rank", "kind", "what", "due", "command"})
        for word in ("overdue", "failed", "late", "violat"):
            self.assertNotIn(word, item["what"])
        # only a later, same-body, non-cancelled meeting with a valid outcome on or before the day asked suppresses it
        later = {**BASE, "on": "2026-10-16"}
        for ident, starts, outcome, extra in (
                ("w-body", "2026-10-16T18:00:00-07:00", later, {"body": OTHER_BODY}),
                ("w-same-day", "2026-10-14T20:00:00-07:00", {**BASE, "on": "2026-10-14"}, {}),
                ("w-earlier", "2026-10-02T18:00:00-07:00", {**BASE, "on": "2026-10-02"}, {}),
                ("w-unrecorded", "2026-10-16T18:00:00-07:00", None, {}),
                ("w-cancelled", "2026-10-16T18:00:00-07:00", None, {"status": "cancelled"}),
                ("w-alias", "2026-10-16T18:00:00-07:00", later, {"body": BODY + " ", "agency_id": "x"}),
                ("w-malformed", "2026-10-16T18:00:00-07:00", {**later, "result": "bogus"}, {})):
            self.add(ident, starts, outcome, **({"body": BODY} | extra))
            with self.subTest(ident):
                self.assertEqual(len(self.pending()), 1)
        self.add("w-ok", "2026-10-16T18:00:00-07:00", later)
        self.assertEqual(self.pending(), [])
        # a comment already sent does not stand in for a report back
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "comments-sent.json").write_text(json.dumps({"schema_version": 1, "comments": {
            MID: {"on": "2026-10-15", "channel": "email"}, "w-ok": {"on": "2026-10-15", "channel": "email"}}}), encoding="utf-8")
        self.assertEqual(self.pending(), [])  # still suppressed by the outcome alone
        (self.meetings / "w-ok.json").unlink()
        self.assertEqual(len(self.pending()), 1)

    def test_rank_order_json_text_and_read_only(self):
        self.stated("2026-10-25")
        self.add("src-a", "2026-10-01T18:00:00-07:00", outcome={**BASE, "result": "continued", "on": "2026-10-01",
                                                                 "continued_to_date": "2026-10-10"})
        self.add("dir-b", "2026-10-02T18:00:00-07:00", body=OTHER_BODY, outcome={
            **BASE, "on": "2026-10-02", "direction": "Report on costs", "return_by": "2026-10-22", "direction_source": SOURCE})
        files = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        items = today.todo(self.root, TODAY)
        kinds = [i["kind"] for i in items]
        self.assertEqual(kinds.index("continued_pending") + 1, kinds.index("direction_pending"))
        pending = [i for i in items if i["kind"] == "direction_pending"]
        self.assertEqual([i["due"] for i in pending], ["2026-10-22", "2026-10-25"])  # by return-by date
        text = today.run_cli(self.root, "2026-10-20")
        listed = json.loads(today.run_cli(self.root, "2026-10-20", as_json=True))
        line = next(i for i in listed if i["kind"] == "direction_pending")
        self.assertIn(f"[direction_pending] {line['what']} — {line['command']}", text)
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, files)

    def test_readers_validate_stored_group_without_writes(self):
        self.stated("2026-10-19")
        (self.meetings / "bad.json").write_text('{"id": "bad", "body": "x", "starts_at": "soon"}', encoding="utf-8")
        items = today.todo(self.root, TODAY)  # a broken neighbour is its own item and hides no reminder
        self.assertEqual(sum("bad.json" in i["what"] for i in items if i["kind"] == "unreadable"), 1)
        self.assertEqual(len([i for i in items if i["kind"] == "direction_pending"]), 1)
        (self.meetings / "bad.json").unlink()
        build_site(self.root, today=TODAY)
        before = self.public()
        good = json.loads(self.original)
        for label, outcome, needle in (
                ("partial", {**BASE, "direction": TEXT}, "outcome.return_by: needed together with direction"),
                ("partial source", {**BASE, "direction_source": SOURCE}, "outcome.direction: needed together with direction_source"),
                ("null", {**BASE, "direction": None, "return_by": "2026-11-01", "direction_source": SOURCE},
                 "outcome.direction: must be 1 to 200 characters of plain text on one line"),
                ("empty", {**BASE, "direction": "", "return_by": "2026-11-01", "direction_source": SOURCE},
                 "outcome.direction: must be 1 to 200 characters of plain text on one line"),
                ("markup", {**BASE, "direction": "<b>x</b>", "return_by": "2026-11-01", "direction_source": SOURCE},
                 "outcome.direction: must be 1 to 200 characters"),
                ("date", {**BASE, "direction": TEXT, "return_by": "2026-10-13", "direction_source": SOURCE},
                 "outcome.return_by: must be a YYYY-MM-DD date on or after the meeting"),
                ("source", {**BASE, "direction": TEXT, "return_by": "2026-11-01", "direction_source": "http://x.invalid/a"},
                 "outcome.direction_source: must be an https:// URL"),
                ("null source", {**BASE, "direction": TEXT, "return_by": "2026-11-01", "direction_source": None},
                 "outcome.direction_source: must be an https:// URL")):
            with self.subTest(label):
                self.write(self.meeting, {**good, "outcome": outcome})
                with self.assertRaises(ContentError) as caught:
                    load_content(self.root)
                self.assertIn(needle, str(caught.exception))
                self.assertTrue(any(needle in problem for problem in content_problems(self.root)))
                built = run("build", "--directory", str(self.root))
                self.assertEqual(built.returncode, 1)
                self.assertIn(needle, built.stderr)
                self.assertEqual(self.public(), before)  # the earlier public bytes are untouched
                self.assertEqual([i["kind"] for i in today.todo(self.root, TODAY)].count("unreadable") >= 1, True)


if __name__ == "__main__":
    unittest.main()
