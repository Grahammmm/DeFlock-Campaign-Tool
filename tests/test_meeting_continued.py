"""DF0022-07: a continued meeting outcome may state where the item went (``--continued-to MEETING_ID`` or
``--continued-to-date D``); pages link forward and back, open data carries the field and `today` lists stated dates
that passed with no meeting of that body staged. A match is a follow-up cue, never evidence the item was heard.

Synthetic content only (temp copies of the fictional example, injected ``today``); nothing is fetched.
Run from the repository root: python3 -B -m unittest tests.test_meeting_continued -v
"""
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

from campaign_tool import law, today
from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.meeting_outcome import record_minutes, record_outcome
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "meetings-open-data.schema.json").read_text(encoding="utf-8"))
MID = "cedar-council-2026-10"  # starts_at 2026-10-14T18:00:00-07:00, body BODY
BODY = "Cedar City Council (fictional)"
OTHER_BODY = "Cedar Planning Commission (fictional)"
TODAY = date(2026, 10, 20)
SOURCE = "https://example.invalid/cedar-council/2026-10-14-minutes"
MINUTES = "https://example.invalid/cedar-council/approved-minutes"
BASE = {"result": "continued", "on": "2026-10-14", "source_url": SOURCE, "verified_by": "Synthetic Organizer",
        "verified_at": "2026-10-20"}
CREATE = ("--result", "continued", "--on", "2026-10-14", "--source", SOURCE, "--verified-by", "Synthetic Organizer")
NEEDLE_DATE = "continued_to_date: must be a YYYY-MM-DD date after the meeting"


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


class Base(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-continued-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "synthetic campaign"  # a space: the paths in `today` commands are quoted
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.meeting = self.meetings / "2026-10-cedar-council.json"
        self.original = self.meeting.read_bytes()

    def write(self, path, document):
        path.write_text(json.dumps(document), encoding="utf-8")

    def doc(self, path=None):
        return json.loads((path or self.meeting).read_text(encoding="utf-8"))

    def add(self, ident, starts_at, outcome=None, body=BODY, **extra):
        record = {"id": ident, "body": body, "starts_at": starts_at, **extra}
        if outcome is not None:
            record["outcome"] = outcome
        self.write(self.meetings / f"{ident}.json", record)

    def set_outcome(self, **changes):
        self.write(self.meeting, {**json.loads(self.original), "outcome": {**BASE, **changes}})

    def reset(self):
        self.meeting.write_bytes(self.original)

    def cli(self, *extra, ident=MID):
        return run("meetings-outcome", "--directory", str(self.root), "--id", ident, *extra)

    def staged(self):
        return {p.name: p.read_bytes() for p in self.meetings.glob("*.json")}

    def public(self):
        public = self.root / "public"
        return {p.relative_to(public).as_posix(): p.read_bytes() for p in public.rglob("*") if p.is_file()}

    def page(self, ident):
        return (self.root / "public" / "meetings" / f"{ident}.html").read_text(encoding="utf-8")

    def record(self, **kwargs):
        return record_outcome(self.root, MID, "continued", "2026-10-14", SOURCE, "Synthetic Organizer", replace=True,
                              today=TODAY, **kwargs)

    def refuse(self, needle, **kwargs):
        before = self.staged()
        with self.assertRaises(ValueError) as caught:
            self.record(**kwargs)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(self.staged(), before)  # no partial write, nothing written to a target


class ModeTests(Base):
    def test_continuation_cli_and_python_modes(self):
        self.add("later-one", "2026-10-19T18:00:00-07:00")
        target_bytes = (self.meetings / "later-one.json").read_bytes()
        for flags, expected in ((("--continued-to-date", "2026-10-19"), {"continued_to_date": "2026-10-19"}),
                                (("--continued-to", "later-one"), {"continued_to": "later-one"})):
            self.reset()
            done = self.cli(*CREATE, *flags)
            self.assertEqual(done.returncode, 0, done.stderr)
            printed, notice = done.stdout.rsplit("}", 1)
            outcome = json.loads(printed + "}")
            self.assertEqual({k: v for k, v in outcome.items() if k != "verified_at"},
                             {**{k: v for k, v in BASE.items() if k != "verified_at"}, **expected})
            self.assertEqual(notice.strip(), "Recorded only. Nothing was published.")
            self.assertEqual(set(self.doc()["outcome"]) - set(BASE), set(expected))  # exactly the supplied key
            self.assertFalse((self.root / "public").exists())
            self.assertEqual((self.meetings / "later-one.json").read_bytes(), target_bytes)
        refused = (
            (CREATE + ("--continued-to", "later-one", "--continued-to-date", "2026-10-19"),
             "--continued-to and --continued-to-date are mutually exclusive"),
            (("--result", "approved", "--on", "2026-10-14", "--source", SOURCE, "--verified-by", "x",
              "--continued-to", "later-one"), "continued_to: requires result continued"),
            (("--result", "no_action", "--on", "2026-10-14", "--source", SOURCE, "--verified-by", "x",
              "--continued-to-date", "2026-10-19"), "continued_to_date: requires result continued"),
            (CREATE + ("--continued-to", "Bad Slug"), "continued_to"),
            (CREATE + ("--continued-to", "later_one"), "continued_to"),
            (CREATE + ("--continued-to", ""), "continued_to"),
            (CREATE + ("--continued-to", "no-such-meeting"), "continued_to: unknown meeting ID"),
            (CREATE + ("--continued-to-date", "2026-10-14"), NEEDLE_DATE),
            (CREATE + ("--continued-to-date", "2026-13-01"), NEEDLE_DATE),
            (CREATE + ("--continued-to-date", "next week"), NEEDLE_DATE),
            (("--continued-to", "later-one"), "--result"),  # no continuation-only mode: creation flags are still needed
            (("--minutes-url", MINUTES, "--minutes-approved-on", "2026-10-20", "--continued-to", "later-one"),
             "--continued-to"),
            (("--minutes-url", MINUTES, "--minutes-approved-on", "2026-10-20", "--continued-to-date", "2026-10-19"),
             "--continued-to-date"))
        for flags, needle in refused:
            with self.subTest(flags=flags):
                self.reset()
                result = self.cli(*flags)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(needle, result.stderr)
                self.assertEqual(self.meeting.read_bytes(), self.original)
        self.reset()  # a continued result without a destination, and the Python signature, stay as they were
        legacy = self.cli(*CREATE)
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertEqual(set(self.doc()["outcome"]), set(BASE))
        self.reset()  # outcomes made by the CLI carry the real clock's verified_at; the Python calls inject TODAY
        positional = record_outcome(self.root, MID, "continued", "2026-10-14", SOURCE, "Synthetic Organizer", None, True,
                                    TODAY)
        self.assertEqual(set(positional), set(BASE))
        self.assertEqual(self.record(continued_to="later-one")["continued_to"], "later-one")
        self.assertEqual(self.record(continued_to_date="2026-10-19")["continued_to_date"], "2026-10-19")
        self.assertNotIn("continued_to", self.doc()["outcome"])  # a full replacement drops the omitted key
        with self.assertRaises(ValueError) as both:
            self.record(continued_to="later-one", continued_to_date="2026-10-19")
        self.assertIn("mutually exclusive", str(both.exception))
        with self.assertRaises(ValueError) as other:
            record_outcome(self.root, MID, "approved", "2026-10-14", SOURCE, "x", replace=True, today=TODAY,
                           continued_to_date="2026-10-19")
        self.assertIn("continued_to_date: requires result continued", str(other.exception))


class RelationTests(Base):
    def test_target_relation_and_date_boundaries(self):
        self.add("t-cancelled", "2026-10-16T18:00:00-07:00", status="cancelled")
        self.add("t-other-body", "2026-10-16T18:00:00-07:00", body=OTHER_BODY)
        self.add("t-case", "2026-10-16T18:00:00-07:00", body=BODY.upper())
        self.add("t-space", "2026-10-16T18:00:00-07:00", body=BODY + " ")
        self.add("t-same-day", "2026-10-14T20:00:00-07:00")  # a later hour of the same day is not a later date
        self.add("t-earlier", "2026-10-13T18:00:00-07:00")
        self.add("t-offset", "2026-10-15T00:30:00+00:00")  # 2026-10-14 in UTC-7, but the local date shown is 15: no conversion
        for target, needle in (("no-such-meeting", "unknown meeting ID"), (MID, "target must be dated after the meeting"),
                               ("t-cancelled", "target is cancelled"), ("t-other-body", "target must have the same body"),
                               ("t-case", "target must have the same body"), ("t-space", "target must have the same body"),
                               ("t-same-day", "target must be dated after the meeting"),
                               ("t-earlier", "target must be dated after the meeting")):
            with self.subTest(target=target):
                self.refuse(f"meetings/{MID}.outcome.continued_to: {needle}", continued_to=target)
        with self.assertRaises(ContentError) as caught:
            self.record(continued_to="Not A Slug")
        self.assertIn("continued_to", str(caught.exception))
        self.assertEqual(self.record(continued_to="t-offset")["continued_to"], "t-offset")
        for bad in ("2026-10-14", "2026-10-13", "2027-02-29", "2026-11-31", "2026-11-2", "2026-1-15", "\uff12026-11-18",
                    "\u0662\u0660\u0662\u0666-11-18", "", " 2026-11-18", "2026-11-18\n", "2026-11-18T10:00:00", "20261118"):
            with self.subTest(date=bad):
                self.refuse(NEEDLE_DATE, continued_to_date=bad)
        for good in ("2026-10-15", "2028-02-29", "2099-12-31"):  # a future date is fine: no today ceiling
            with self.subTest(date=good):
                self.assertEqual(self.record(continued_to_date=good)["continued_to_date"], good)
        # a stored file is held to the same rules by build and content-check
        for key, needle in (("continued_to", "outcome.continued_to"), ("continued_to_date", "outcome.continued_to_date")):
            for value in (None, True, "", 5, ["x"]):
                with self.subTest(key=key, value=value):
                    self.set_outcome(**{key: value})
                    self.assertTrue(any(needle in problem for problem in content_problems(self.root)), (key, value))
                    with self.assertRaises(ContentError):
                        load_content(self.root)
        for changes, needle in (({"continued_to": "t-offset", "continued_to_date": "2026-10-15"}, "mutually exclusive"),
                                ({"result": "approved", "continued_to": "t-offset"}, "continued_to: requires result continued"),
                                ({"result": "denied", "continued_to_date": "2026-10-15"}, "requires result continued"),
                                ({"continued_to_date": "2026-10-14"}, NEEDLE_DATE)):
            with self.subTest(changes=changes):
                self.set_outcome(**changes)
                self.assertTrue(any(needle in problem for problem in content_problems(self.root)), changes)
                with self.assertRaises(ContentError) as caught:
                    load_content(self.root)
                self.assertIn(needle, str(caught.exception))


class PageTests(Base):
    def setUp(self):
        super().setUp()
        later = {**BASE, "on": "2026-10-16", "verified_at": "2026-10-17", "continued_to_date": "2026-10-19"}
        self.set_outcome(continued_to="later-one")
        self.add("later-one", "2026-10-16T18:00:00-07:00", outcome=later)  # a chain: continued here, then on again
        self.add("twin-a", "2026-10-19T18:00:00-07:00")
        self.add("twin-b", "2026-10-19T09:00:00-07:00")
        self.add("other-body-19", "2026-10-19T18:00:00-07:00", body=OTHER_BODY)
        self.add("cancelled-19", "2026-10-19T18:00:00-07:00", status="cancelled")
        self.add("older-b", "2026-10-02T18:00:00-07:00", outcome={**BASE, "on": "2026-10-02", "continued_to": "later-one"})
        self.add("zero", "2026-10-05T18:00:00-07:00", outcome={**BASE, "on": "2026-10-05", "continued_to_date": "2026-12-02"})
        self.add("single", "2026-10-06T18:00:00-07:00", outcome={**BASE, "on": "2026-10-06", "continued_to_date": "2026-10-08"})
        self.add("mid-eight", "2026-10-08T18:00:00-07:00")
        self.add("same-day-src", "2026-10-20T09:00:00-07:00", outcome={**BASE, "on": "2026-10-20", "continued_to": "after-today"})
        self.add("after-today", "2026-10-25T18:00:00-07:00")
        self.add("plain", "2026-10-03T18:00:00-07:00", outcome={**BASE, "on": "2026-10-03", "result": "approved"})
        build_site(self.root, today=TODAY)

    def link(self, ident, day):
        return f'<a href="/meetings/{ident}.html">{day}</a>'

    def test_forward_backlinks_and_ambiguity(self):
        p = '<p class="meeting-continuation">'
        self.assertIn(f"{p}Continued to {self.link('later-one', '2026-10-16')}</p>", self.page(MID))  # explicit id
        self.assertEqual(self.page(MID).count("meeting-continuation"), 1)  # no recursion into the chain
        later = self.page("later-one")  # several sources, in start order; a date with two staged candidates in start order
        self.assertIn(f"{p}Continued to 2026-10-19 · <a href=\"/meetings/twin-b.html\">Meeting page (twin-b)</a>"
                      f" · <a href=\"/meetings/twin-a.html\">Meeting page (twin-a)</a></p>", later)
        froms = [f"{p}Continued from {self.link('older-b', '2026-10-02')}</p>", f"{p}Continued from {self.link(MID, '2026-10-14')}</p>"]
        for text in froms:
            self.assertIn(text, later)
        self.assertLess(later.index(froms[0]), later.index(froms[1]))
        self.assertEqual(later.count("meeting-continuation"), 3)
        for unheld in ("same-day-src", "after-today"):  # a meeting still happening today shows no outcome, so no link either way
            self.assertNotIn("meeting-continuation", self.page(unheld))
        for twin in ("twin-a", "twin-b"):  # the one source points at both candidates
            self.assertEqual(self.page(twin).count("meeting-continuation"), 1)
            self.assertIn(f"{p}Continued from {self.link('later-one', '2026-10-16')}</p>", self.page(twin))
        for unmatched in ("other-body-19", "cancelled-19", "plain", "zero"):
            self.assertNotIn("Continued from", self.page(unmatched))
        self.assertIn(f"{p}Continued to 2026-12-02</p>", self.page("zero"))  # nothing staged: the plain date
        self.assertIn(f"{p}Continued to {self.link('mid-eight', '2026-10-08')}</p>", self.page("single"))
        self.assertIn(f"{p}Continued from {self.link('single', '2026-10-06')}</p>", self.page("mid-eight"))
        self.assertNotIn("meeting-continuation", self.page("plain"))
        before = self.public()
        build_site(self.root, today=TODAY)
        self.assertEqual(self.public(), before)  # deterministic

    def test_filename_ids_labels_and_escaping(self):
        self.add("filename-only", "2026-10-17T18:00:00-07:00")
        document = self.doc(self.meetings / "filename-only.json")
        del document["id"]
        self.write(self.meetings / "filename-only.json", document)
        self.set_outcome(continued_to="filename-only")
        build_site(self.root, today=TODAY)
        self.assertIn("Continued to " + self.link("filename-only", "2026-10-17"), self.page(MID))
        self.assertIn("Continued from " + self.link(MID, "2026-10-14"), self.page("filename-only"))
        labels = self.root / "content" / "labels.json"
        labels.write_text(json.dumps({"schema_version": 1, "labels": {
            "meeting.continued_to": "Seguir & ver {date}", "meeting.continued_from": "Desde {date} (\"x\")"}}), encoding="utf-8")
        build_site(self.root, today=TODAY)
        self.assertIn("Seguir &amp; ver " + self.link("filename-only", "2026-10-17"), self.page(MID))
        self.assertIn("Desde " + self.link(MID, "2026-10-14") + " (&quot;x&quot;)", self.page("filename-only"))
        shutil.copy(REPO_ROOT / "examples" / "labels" / "es.json", labels)  # the Spanish draft
        build_site(self.root, today=TODAY)
        self.assertIn("Continuado al " + self.link("filename-only", "2026-10-17"), self.page(MID))
        self.assertIn("Continuado desde el " + self.link(MID, "2026-10-14"), self.page("filename-only"))
        labels.write_text(json.dumps({"schema_version": 1, "labels": {"meeting.continued_to": "<b>{date}</b>"}}),
                          encoding="utf-8")  # raw HTML in a label is refused, so none reaches a placeholder
        with self.assertRaises(ContentError):
            build_site(self.root, today=TODAY)


class CrossRecordTests(Base):
    def setUp(self):
        super().setUp()
        self.add("later-one", "2026-10-16T18:00:00-07:00")
        self.set_outcome(continued_to="later-one")

    def broken(self, needle):
        text = f"meetings/{MID}.outcome.continued_to: {needle}"
        self.assertIn(text, content_problems(self.root))
        with self.assertRaises(ContentError) as caught:
            load_content(self.root)
        self.assertEqual(str(caught.exception), text)

    def test_cross_record_validation_and_repair(self):
        build_site(self.root, today=TODAY)
        before = self.public()
        target = self.meetings / "later-one.json"
        good = target.read_bytes()
        for label, edit, needle in (("deleted", lambda: target.unlink(), "unknown meeting ID"),
                                    ("renamed", lambda: self.write(target, {**json.loads(good), "id": "renamed-one"}), "unknown meeting ID"),
                                    ("cancelled", lambda: self.write(target, {**json.loads(good), "status": "cancelled"}), "target is cancelled"),
                                    ("other body", lambda: self.write(target, {**json.loads(good), "body": OTHER_BODY}),
                                     "target must have the same body"),
                                    ("moved earlier", lambda: self.write(target, {**json.loads(good), "starts_at": "2026-10-14T19:00:00-07:00"}),
                                     "target must be dated after the meeting")):
            with self.subTest(label):
                edit()
                self.broken(needle)
                built = run("build", "--directory", str(self.root))
                self.assertEqual(built.returncode, 1, built.stdout)
                self.assertIn(f"meetings/{MID}.outcome.continued_to: {needle}", built.stderr)
                checked = run("content-check", "--directory", str(self.root))
                self.assertEqual(checked.returncode, 3, checked.stdout)
                self.assertIn(f"meetings/{MID}.outcome.continued_to: {needle}", checked.stdout)
                self.assertEqual(self.public(), before)  # the earlier public bytes are untouched
                target.write_bytes(good)
        self.assertEqual(content_problems(self.root), [])
        # a full --replace repairs the source even though its old reference is broken; minutes-only keeps it
        target.unlink()
        stored = self.meeting.read_bytes()
        record_minutes(self.root, MID, MINUTES, "2026-10-20", today=TODAY)
        self.assertEqual(self.doc()["outcome"], {**BASE, "continued_to": "later-one", "minutes_url": MINUTES,
                                                 "minutes_approved_on": "2026-10-20"})  # read, never migrated
        self.assertNotEqual(self.meeting.read_bytes(), stored)
        self.add("other-later", "2026-10-17T18:00:00-07:00")
        self.assertEqual(self.record(continued_to="other-later")["continued_to"], "other-later")
        self.set_outcome(continued_to="later-one")
        self.assertNotIn("continued_to", record_outcome(self.root, MID, "approved", "2026-10-14", SOURCE, "x", replace=True,
                                                        today=TODAY))
        self.assertEqual(content_problems(self.root), [])

    def test_replace_writes_only_the_source_and_no_public_files(self):
        shutil.rmtree(self.root / "public", ignore_errors=True)
        others = {p.name: p.read_bytes() for p in self.meetings.glob("*.json") if p != self.meeting}
        self.record(continued_to="later-one")
        self.assertEqual({p.name: p.read_bytes() for p in self.meetings.glob("*.json") if p != self.meeting}, others)
        self.assertFalse((self.root / "public").exists())

    def test_symlinks_duplicates_and_unreadable_neighbours(self):
        outside = self.tmp / "outside.json"
        shutil.move(str(self.meetings / "later-one.json"), str(outside))
        (self.meetings / "later-one.json").symlink_to(outside)
        outside_bytes = outside.read_bytes()
        self.refuse("must not be a symlink", continued_to="later-one")
        self.assertEqual(outside.read_bytes(), outside_bytes)
        (self.meetings / "later-one.json").unlink()
        self.add("later-one", "2026-10-16T18:00:00-07:00")
        (self.meetings / "unrelated.json").symlink_to(outside)  # an unrelated symlink does not block a valid reference
        (self.meetings / "broken.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.record(continued_to="later-one")["continued_to"], "later-one")
        (self.meetings / "broken-target.json").write_text('{"id": "broken-target", "body": "x"}', encoding="utf-8")
        self.refuse("meeting broken-target cannot be read", continued_to="broken-target")
        self.write(self.meetings / "dup-a.json", {"id": "dup-id", "body": BODY, "starts_at": "2026-10-17T18:00:00-07:00"})
        self.write(self.meetings / "dup-b.json", {"id": "dup-id", "body": BODY, "starts_at": "2026-10-18T18:00:00-07:00"})
        self.refuse("duplicate meeting ID", continued_to="dup-id")
        (self.meetings / "unrelated.json").unlink()
        (self.meetings / "broken.json").unlink()
        (self.meetings / "broken-target.json").unlink()
        with self.assertRaises(ContentError) as caught:
            load_content(self.root)
        self.assertIn("duplicate meeting id", str(caught.exception))


class TodayTests(Base):
    URL = "https://example.invalid/cedar-council/agenda?x=1&y=2"

    def setUp(self):
        super().setUp()
        self.meeting.unlink()
        self.add("src-a", "2026-10-01T18:00:00-07:00", outcome={**BASE, "on": "2026-10-01", "verified_at": "2026-10-01",
                                                               "source_url": self.URL, "continued_to_date": "2026-10-10"})

    def pending(self, as_of, kinds=("continued_pending",)):
        return [item for item in today.todo(self.root, as_of) if item["kind"] in kinds]

    def test_today_continued_boundary_and_isolation(self):
        self.assertEqual(today.KINDS.index("continued_pending"), today.KINDS.index("minutes_pending") + 1)
        self.assertEqual([self.pending(date(2026, 10, d)) for d in (9, 10)], [[], []])  # D-1 and D itself
        item, = self.pending(date(2026, 10, 11))  # D+1
        self.assertEqual(item["due"], "2026-10-10")
        self.assertEqual(item["what"], f"Continued item for {BODY} (src-a): stated date 2026-10-10 has passed with no "
                                       "matching meeting staged; check the official agenda")
        self.assertEqual(item["command"], f"open {shlex.quote(self.URL)} and stage the verified follow-up meeting with "
                                          f"meetings-stage --directory {shlex.quote(str(self.root))} --id MEETING_ID")
        self.assertEqual(set(item), {"rank", "kind", "what", "due", "command"})
        self.assertNotIn("heard", item["what"])
        # a staged meeting of that exact body on the exact date suppresses it; others do not
        as_of = date(2026, 10, 20)
        for ident, starts, extra in (("w-body", "2026-10-10T18:00:00-07:00", {"body": OTHER_BODY}),
                                     ("w-date", "2026-10-11T18:00:00-07:00", {}),
                                     ("w-cancelled", "2026-10-10T18:00:00-07:00", {"status": "cancelled"})):
            self.add(ident, starts, **extra)
            with self.subTest(ident):
                self.assertEqual(len(self.pending(as_of)), 1)
        self.add("exact", "2026-10-10T23:30:00-07:00")
        self.assertEqual(self.pending(as_of), [])
        (self.meetings / "exact.json").unlink()
        # a comment already sent does not stand in for a staged meeting
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "comments-sent.json").write_text(json.dumps({"schema_version": 1, "comments": {
            "src-a": {"on": "2026-10-02", "channel": "email"}}}), encoding="utf-8")
        self.assertEqual(len(self.pending(as_of)), 1)
        # an explicit valid target yields no item, however old; an invalid one is one unreadable item and no guess
        self.add("tgt", "2026-10-05T18:00:00-07:00")
        self.add("src-b", "2026-10-02T18:00:00-07:00", outcome={**BASE, "on": "2026-10-02", "verified_at": "2026-10-02",
                                                               "continued_to": "tgt"})
        self.assertEqual(len(self.pending(as_of)), 1)  # src-b has a valid explicit target: no item of its own
        self.add("src-c", "2026-10-02T19:00:00-07:00", outcome={**BASE, "on": "2026-10-02", "verified_at": "2026-10-02",
                                                               "continued_to": "ghost"})
        items = today.todo(self.root, as_of)
        bad = [i for i in items if i["kind"] == "unreadable" and "src-c.json" in i["what"]]
        self.assertEqual(len(bad), 1)
        self.assertIn("unknown meeting ID", bad[0]["what"])
        self.assertEqual(len([i for i in items if i["kind"] == "continued_pending"]), 1)
        # a malformed neighbour is its own item and hides nothing
        (self.meetings / "bad.json").write_text('{"id": "bad", "body": "x", "starts_at": "soon"}', encoding="utf-8")
        items = today.todo(self.root, as_of)
        self.assertEqual(sum("bad.json" in i["what"] for i in items if i["kind"] == "unreadable"), 1)
        self.assertEqual(len([i for i in items if i["kind"] == "continued_pending"]), 1)
        # duplicate ids: each file is unreadable, the valid neighbour's reminder stays
        self.write(self.meetings / "dup-1.json", {"id": "dup-id", "body": BODY, "starts_at": "2026-10-03T18:00:00-07:00"})
        self.write(self.meetings / "dup-2.json", {"id": "dup-id", "body": BODY, "starts_at": "2026-10-04T18:00:00-07:00"})
        items = today.todo(self.root, as_of)
        self.assertEqual(sorted(i["what"].split(":")[0] for i in items if "dup-" in i["what"]),
                         ["fix content/meetings/dup-1.json", "fix content/meetings/dup-2.json"])
        self.assertEqual(len([i for i in items if i["kind"] == "continued_pending"]), 1)
        # read-only, and the JSON and text forms agree
        files = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        text = today.run_cli(self.root, "2026-10-20")
        listed = json.loads(today.run_cli(self.root, "2026-10-20", as_json=True))
        line = next(i for i in listed if i["kind"] == "continued_pending")
        self.assertIn(f"[continued_pending] {line['what']} — {line['command']}", text)
        self.assertEqual(today.run_cli(self.root, "2026-10-20", as_json=True), json.dumps(listed, indent=2, ensure_ascii=False))
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, files)

    def test_no_continuation_means_no_new_items(self):
        (self.meetings / "src-a.json").write_text(json.dumps({"id": "src-a", "body": BODY, "starts_at": "2026-10-01T18:00:00-07:00"}),
                                                  encoding="utf-8")
        self.write(self.meetings / "dup-1.json", {"id": "same", "body": BODY, "starts_at": "2026-10-03T18:00:00-07:00"})
        self.write(self.meetings / "dup-2.json", {"id": "same", "body": BODY, "starts_at": "2026-10-04T18:00:00-07:00"})
        self.assertEqual([i for i in today.todo(self.root, date(2026, 12, 1)) if "meetings/" in i["what"]], [])


class ExportTests(Base):
    def test_export_schema_and_legacy_bytes(self):
        self.add("later-one", "2026-10-16T18:00:00-07:00")
        self.set_outcome(continued_to="later-one", vote="4-1")
        document = self.doc()
        document["participation"] = {"in_person": True, "remote": None, "written_comment": None, "source_url": SOURCE,
                                     "verified_by": "Synthetic Verifier", "verified_at": "2026-10-10"}
        self.write(self.meeting, document)
        self.add("by-date", "2026-10-02T18:00:00-07:00", outcome={**BASE, "on": "2026-10-02", "continued_to_date": "2026-12-02"})
        self.add("legacy", "2026-10-03T18:00:00-07:00", outcome={**BASE, "on": "2026-10-03"})
        self.add("today-day", "2026-10-20T09:00:00-07:00", outcome={**BASE, "on": "2026-10-20", "continued_to": "after-today"})
        self.add("after-today", "2026-10-25T18:00:00-07:00")
        build_site(self.root, today=TODAY)
        text = (self.root / "public" / "data" / "meetings.json").read_text(encoding="utf-8")
        doc = json.loads(text)
        law.validate(doc, SCHEMA)
        meetings = {m["id"]: m for m in doc["meetings"]}
        self.assertEqual(meetings[MID]["outcome"], {"result": "continued", "on": "2026-10-14", "source_url": SOURCE,
                                                    "vote": "4-1", "continued_to": "later-one"})
        self.assertEqual(meetings["by-date"]["outcome"], {"result": "continued", "on": "2026-10-02", "source_url": SOURCE,
                                                          "continued_to_date": "2026-12-02"})
        self.assertEqual(meetings["legacy"]["outcome"], {"result": "continued", "on": "2026-10-03", "source_url": SOURCE})
        self.assertNotIn("outcome", meetings["today-day"])  # not shown before the day is over, as before
        self.assertNotIn("continued_from", text)  # no derived back-reference
        for private in ("verified_by", "verified_at", "Synthetic Organizer", "Synthetic Verifier", "participation",
                        "Two minutes, one ask", "Cedar Civic Center"):
            self.assertNotIn(private, text)
        shell = {"schema_version": 1, "generated_at": "2026-10-20T00:00:00Z"}
        row = {"id": "m", "body": "b", "agency_id": None, "starts_at": "2026-10-14T18:00:00-07:00", "status": "scheduled",
               "agenda_url": None, "url": "/meetings/m.html"}
        outcome = {"result": "continued", "on": "2026-10-14", "source_url": SOURCE}

        def check(**extra):
            law.validate({**shell, "meetings": [{**row, "outcome": {**outcome, **extra}}]}, SCHEMA)
        check()
        check(continued_to="later-one")
        check(continued_to_date="2026-12-02")
        for extra in ({"continued_to": "later-one", "continued_to_date": "2026-12-02"}, {"result": "approved", "continued_to": "x"},
                      {"result": "no_action", "continued_to_date": "2026-12-02"}, {"continued_to": "Bad Slug"},
                      {"continued_to": 5}, {"continued_to_date": "2026-12-2"}, {"continued_to_date": "2026-12-02T00:00:00"},
                      {"continued_from": "x"}, {"continued_to": "x", "verified_by": "x"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                check(**extra)
        # with no continuation keys the pages and open data carry nothing of this feature, and a rebuild is identical
        for name in ("by-date", "legacy"):
            self.assertNotIn("Continued from", self.page(name))
        self.assertNotIn("meeting-continuation", self.page("legacy"))
        self.assertNotIn("meeting-continuation", (self.root / "public" / "meetings.html").read_text(encoding="utf-8"))
        before = self.public()
        build_site(self.root, today=TODAY)
        self.assertEqual(self.public(), before)


if __name__ == "__main__":
    unittest.main()
