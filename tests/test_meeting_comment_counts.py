"""DF0022-09: an outcome may record how many members of the public spoke and how many written comments were received
(``--spoke`` ``--written`` ``--comment-source``), exactly as the official minutes or record state. The meeting page and
open data carry the totals with their source; there is no for/against split, name or quote.

Synthetic content only (temp copies of the fictional example, injected ``today``); nothing is fetched.
Run from the repository root: python3 -B -m unittest tests.test_meeting_comment_counts -v
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import law
from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.meeting_outcome import MeetingOutcomeError, comment_count, record_comment_counts, record_outcome
from campaign_tool.site import PUBLIC_OUTCOME_FIELDS, Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "meetings-open-data.schema.json").read_text(encoding="utf-8"))
ES = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))
MID = "cedar-council-2026-10"  # starts_at 2026-10-14T18:00:00-07:00
TODAY = date(2026, 10, 20)
SOURCE = "https://example.invalid/cedar-council/2026-10-14-minutes"
MINUTES = "https://example.invalid/cedar-council/approved-minutes"
COUNT_SOURCE = "https://example.invalid/cedar-council/public-comment-summary?item=7&view=full"
BASE = {"result": "approved", "on": "2026-10-14", "source_url": SOURCE, "verified_by": "Synthetic Organizer",
        "verified_at": "2026-10-20"}
CREATE = ("--result", "approved", "--on", "2026-10-14", "--source", SOURCE, "--verified-by", "Synthetic Organizer")
NOTICE = "Recorded only. Nothing was published."
KEYS = ("spoke", "written", "comment_source_url")
RANGE = "must be a whole number from 0 to 999"


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


class Base(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-comment-counts-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "synthetic campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.meeting = self.meetings / "2026-10-cedar-council.json"
        self.original = self.meeting.read_bytes()

    def doc(self):
        return json.loads(self.meeting.read_text(encoding="utf-8"))

    def set_outcome(self, **changes):
        self.meeting.write_text(json.dumps({**json.loads(self.original), "outcome": {**BASE, **changes}}), encoding="utf-8")

    def cli(self, *extra, ident=MID):
        return run("meetings-outcome", "--directory", str(self.root), "--id", ident, *extra)

    def staged(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.meetings.glob("*.json")}

    def public(self):
        public = self.root / "public"
        return {p.relative_to(public).as_posix(): p.read_bytes() for p in public.rglob("*") if p.is_file()}

    def page(self):
        return (self.root / "public" / "meetings" / f"{MID}.html").read_text(encoding="utf-8")

    def attach(self, **kwargs):
        return record_comment_counts(self.root, MID, today=TODAY, **kwargs)

    def refuse(self, needle, call, *args, **kwargs):
        before = self.staged()
        with self.assertRaises(ValueError) as caught:
            call(*args, **kwargs)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(self.staged(), before)  # no partial write, no mtime change
        return str(caught.exception)

    def create(self, **kwargs):
        return record_outcome(self.root, MID, "approved", "2026-10-14", SOURCE, "Synthetic Organizer", today=TODAY, **kwargs)


class BoundsAndTypes(Base):
    def test_counts_bounds_and_types(self):
        for spoke, written in ((0, 0), (1, None), (None, 1), (999, 999), (None, 0), (0, None)):
            with self.subTest(spoke=spoke, written=written):
                self.meeting.write_bytes(self.original)
                outcome = self.create(spoke=spoke, written=written, replace=True)
                expected = {**({"spoke": spoke} if spoke is not None else {}),
                            **({"written": written} if written is not None else {}), "comment_source_url": SOURCE}
                self.assertEqual({k: outcome[k] for k in KEYS if k in outcome}, expected)
                self.assertEqual(self.doc()["outcome"], outcome)
                self.assertTrue(all(type(v) is int for k, v in outcome.items() if k in ("spoke", "written")))
        self.meeting.write_bytes(self.original)  # absent is distinct from zero
        self.assertFalse(set(KEYS) & set(self.create(replace=True)))
        for bad in (-1, 1000, True, False, 1.0, 5.5, "5", [], {}):
            for key in ("spoke", "written"):
                with self.subTest(api=bad, key=key):
                    self.meeting.write_bytes(self.original)
                    self.refuse(f"{key}: {RANGE}", self.create, replace=True, **{key: bad})
        # the same values written by hand (and JSON null) are refused by build and content-check, by field name
        for key in ("spoke", "written"):
            for bad in (None, -1, 1000, True, 2.0, "5"):
                with self.subTest(stored=bad, key=key):
                    self.set_outcome(**{key: bad, "comment_source_url": SOURCE})
                    with self.assertRaisesRegex(ContentError, f"outcome\\.{key}: {RANGE}"):
                        load_content(self.root)
                    self.assertTrue(any(f"outcome.{key}: {RANGE}" in line for line in content_problems(self.root)))
        # CLI: ASCII digits only, 0 to 999, leading zeros read as integers
        self.meeting.write_bytes(self.original)
        for text, number in (("0", 0), ("007", 7), ("999", 999), ("000", 0)):
            self.assertEqual(comment_count(text, "--spoke"), number)
        for flag in ("--spoke", "--written"):
            for text in ("-1", "+1", "1000", "1.0", "1e2", " 1", "1 ", "٣", "５", "", "1_0", "0x1", "three", "0" * 5 + "1000"):
                with self.subTest(flag=flag, text=text):
                    done = self.cli(*CREATE, flag, text, "--replace")
                    self.assertEqual(done.returncode, 1, done.stdout)
                    self.assertIn(f"{flag}: {RANGE}", done.stderr)
                    self.assertEqual(self.meeting.read_bytes(), self.original)
        done = self.cli(*CREATE, "--spoke", "0", "--written", "012", "--replace")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual({k: self.doc()["outcome"][k] for k in KEYS}, {"spoke": 0, "written": 12, "comment_source_url": SOURCE})
        self.assertEqual(done.stdout.rstrip().splitlines()[-1], NOTICE)
        self.assertIn('"spoke": 0', done.stdout)


class SourceAndRefusals(Base):
    def test_source_priority_and_refusals(self):
        # creation: explicit > outcome source
        self.assertEqual(self.create(spoke=3, replace=True)["comment_source_url"], SOURCE)
        self.meeting.write_bytes(self.original)
        self.assertEqual(self.create(spoke=3, comment_source_url=COUNT_SOURCE, replace=True)["comment_source_url"], COUNT_SOURCE)
        # counts-only: explicit > current minutes > outcome source
        self.set_outcome()
        self.assertEqual(self.attach(spoke=1)["comment_source_url"], SOURCE)
        self.set_outcome(minutes_url=MINUTES, minutes_approved_on="2026-10-20")
        self.assertEqual(self.attach(spoke=1)["comment_source_url"], MINUTES)
        self.set_outcome(minutes_url=MINUTES, minutes_approved_on="2026-10-20")
        self.assertEqual(self.attach(spoke=1, comment_source_url=COUNT_SOURCE)["comment_source_url"], COUNT_SOURCE)
        # an obsolete minutes URL dropped by a full --replace is never the source
        self.assertEqual(self.create(written=2, replace=True)["comment_source_url"], SOURCE)
        # unsafe sources
        self.set_outcome()
        for bad in ("http://example.invalid/x", "https://user:pw@example.invalid/x", "https://example.invalid/<b>",
                    'https://example.invalid/"x', "javascript:alert(1)", "example.invalid/x", "",
                    "https://example.invalid/" + "a" * 500):
            with self.subTest(source=bad):
                self.refuse("comment_source_url", self.attach, spoke=1, comment_source_url=bad)
                self.refuse("comment_source_url", self.create, spoke=1, comment_source_url=bad, replace=True)
        # a source alone, by API and by CLI, before any write
        self.refuse("--comment-source needs --spoke or --written", self.create, comment_source_url=COUNT_SOURCE, replace=True)
        self.refuse("--comment-source needs --spoke or --written", self.attach, comment_source_url=COUNT_SOURCE)
        self.refuse("needs --spoke or --written", self.attach)
        for extra in (("--comment-source", COUNT_SOURCE), CREATE + ("--comment-source", COUNT_SOURCE, "--replace")):
            done = self.cli(*extra)
            self.assertEqual(done.returncode, 1)
            self.assertIn("--comment-source needs --spoke or --written", done.stderr)
        self.assertEqual(self.doc()["outcome"], BASE)
        # hand-edited groups
        self.set_outcome(comment_source_url=COUNT_SOURCE)
        with self.assertRaisesRegex(ContentError, r"outcome\.comment_source_url: needs spoke or written"):
            load_content(self.root)
        self.set_outcome(spoke=3)
        with self.assertRaisesRegex(ContentError, r"outcome\.comment_source_url: required with spoke or written"):
            load_content(self.root)
        self.assertTrue(any("comment_source_url: required with spoke or written" in p for p in content_problems(self.root)))
        # counts without an outcome, cancelled, future, unknown and linked meetings
        self.meeting.write_bytes(self.original)
        self.refuse("public comment counts: needs a recorded outcome", self.attach, spoke=1)
        done = self.cli("--spoke", "1")
        self.assertEqual((done.returncode, self.meeting.read_bytes()), (1, self.original))
        self.assertIn("public comment counts: needs a recorded outcome", done.stderr)
        original = json.loads(self.original)
        self.meeting.write_text(json.dumps({**original, "status": "cancelled"}), encoding="utf-8")
        self.refuse("is cancelled", self.attach, spoke=1)
        self.refuse("is cancelled", self.create, spoke=1, replace=True)
        self.meeting.write_text(json.dumps({**original, "starts_at": "2026-12-14T18:00:00-08:00"}), encoding="utf-8")
        self.refuse("has not happened yet", self.attach, spoke=1)
        self.meeting.write_bytes(self.original)
        with self.assertRaisesRegex(MeetingOutcomeError, "no staged meeting no-such-meeting"):
            record_comment_counts(self.root, "no-such-meeting", spoke=1, today=TODAY)
        link = self.meetings / "linked.json"
        link.symlink_to(self.meeting)
        with self.assertRaisesRegex(MeetingOutcomeError, "must not be a symlink"):
            record_comment_counts(self.root, "linked", spoke=1, today=TODAY)
        link.unlink()
        # creation, counts-only and minutes modes are never mixed or guessed
        self.set_outcome(vote="4-1")
        before = self.meeting.read_bytes()
        for extra, needle in (
                (("--spoke", "1", "--vote", "4-1"), "counts-only mode cannot be combined with --vote"),
                (("--written", "1", "--votes", "x.csv"), "counts-only mode cannot be combined with --votes"),
                (("--spoke", "1", "--recording-url", SOURCE, "--recording-start", "0:01:00"),
                 "--recording-url, --recording-start"),
                (("--spoke", "1", "--continued-to", "x", "--continued-to-date", "2026-12-01"),
                 "--continued-to, --continued-to-date"),
                (("--spoke", "1", "--direction", "Return soon", "--return-by", "2026-11-01", "--direction-source", SOURCE),
                 "--direction, --return-by, --direction-source"),
                (("--spoke", "1", "--result", "approved"), "--on, --source, --verified-by: required to record an outcome"),
                (("--written", "1", "--on", "2026-10-14", "--source", SOURCE), "--result, --verified-by: required"),
                (("--spoke", "1", "--minutes-url", MINUTES, "--minutes-approved-on", "2026-10-20"),
                 "minutes mode cannot be combined with --spoke"),
                (("--written", "1", "--comment-source", SOURCE, "--minutes-url", MINUTES, "--minutes-approved-on", "2026-10-20"),
                 "minutes mode cannot be combined with --written, --comment-source"),
                (("--comment-source", SOURCE, "--minutes-url", MINUTES, "--minutes-approved-on", "2026-10-20"),
                 "minutes mode cannot be combined with --comment-source")):
            with self.subTest(extra=extra):
                done = self.cli(*extra)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(needle, done.stderr)
                self.assertEqual(self.meeting.read_bytes(), before)
        self.assertNotIn("Traceback", done.stderr)

    def test_failed_cli_invocations_use_named_errors_not_tracebacks(self):
        self.set_outcome()
        done = self.cli("--spoke", "x")
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("Traceback", done.stderr)


class AttachReplace(Base):
    FULL = {"vote": "4-1", "recording_url": "https://example.invalid/video", "recording_start": "0:12:30",
            "minutes_url": MINUTES, "minutes_approved_on": "2026-10-20", "direction": "Return with a draft usage policy",
            "return_by": "2026-11-01", "direction_source": SOURCE}

    def test_attach_replace_preserves_dependency_outcome(self):
        self.set_outcome(**self.FULL)
        original = json.loads(self.meeting.read_text(encoding="utf-8"))
        done = self.cli("--spoke", "4", "--written", "12")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.rstrip().splitlines()[-1], NOTICE)
        self.assertNotIn("votes: none", done.stdout)
        stored = self.doc()
        self.assertEqual(stored["outcome"], {**original["outcome"], "spoke": 4, "written": 12, "comment_source_url": MINUTES})
        self.assertEqual({k: v for k, v in stored.items() if k != "outcome"}, {k: v for k, v in original.items() if k != "outcome"})
        self.assertEqual(json.loads(done.stdout.rsplit("}", 1)[0] + "}"), stored["outcome"])
        # a duplicate is refused for either member and changes nothing
        before = self.staged()
        for extra in (("--spoke", "5"), ("--written", "1"), ("--spoke", "4", "--written", "12")):
            done = self.cli(*extra)
            self.assertEqual(done.returncode, 1)
            self.assertIn("public comment counts already recorded; pass --replace to overwrite them", done.stderr)
            self.assertNotIn("votes: none", done.stdout)
            self.assertEqual(self.staged(), before)
        # --replace replaces the group: the omitted member is removed and the source is resolved anew
        done = self.cli("--spoke", "9", "--replace")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("votes: none", done.stdout)  # counts-only --replace never reports votes
        outcome = self.doc()["outcome"]
        self.assertEqual({k: outcome[k] for k in KEYS if k in outcome}, {"spoke": 9, "comment_source_url": MINUTES})
        self.assertEqual({k: v for k, v in outcome.items() if k not in KEYS}, original["outcome"])
        done = self.cli("--written", "0", "--comment-source", COUNT_SOURCE, "--replace")
        self.assertEqual(done.returncode, 0, done.stderr)
        outcome = self.doc()["outcome"]
        self.assertEqual({k: outcome[k] for k in KEYS if k in outcome}, {"written": 0, "comment_source_url": COUNT_SOURCE})
        # the minutes-only and direction-only modes keep the counts
        self.assertEqual(self.cli("--minutes-url", "https://example.invalid/amended-minutes", "--minutes-approved-on",
                                  "2026-10-20", "--replace").returncode, 0)
        self.assertEqual(self.cli("--direction", "Return with a map", "--return-by", "2026-11-02", "--replace").returncode, 0)
        outcome = self.doc()["outcome"]
        self.assertEqual((outcome["written"], outcome["comment_source_url"]), (0, COUNT_SOURCE))
        # a full --replace drops omitted counts, as it drops omitted minutes and votes
        self.assertEqual(self.cli(*CREATE, "--replace").returncode, 0)
        self.assertEqual(self.doc()["outcome"], BASE | {"verified_at": self.doc()["outcome"]["verified_at"]})

    def test_counts_only_keeps_continuation_and_validates_the_whole_document(self):
        later = {"id": "cedar-council-2026-11", "body": json.loads(self.original)["body"], "starts_at": "2026-11-18T18:00:00-08:00"}
        (self.meetings / "later.json").write_text(json.dumps(later), encoding="utf-8")
        self.set_outcome(result="continued", continued_to="cedar-council-2026-11")
        outcome = self.attach(written=2)
        self.assertEqual((outcome["continued_to"], outcome["written"]), ("cedar-council-2026-11", 2))
        (self.meetings / "later.json").unlink()  # the reference is now broken: the cross-record check refuses the write
        before = self.staged()
        self.refuse("continued_to", self.attach, written=3, replace=True)
        self.assertEqual(self.staged(), before)

    def test_directory_symlink_is_not_followed(self):
        real = self.meetings.parent / "real-meetings"
        self.meetings.rename(real)
        self.meetings.symlink_to(real)
        with self.assertRaisesRegex(MeetingOutcomeError, "no staged meeting"):
            self.attach(spoke=1)


SCHEMA_OUTCOME = SCHEMA["$defs"]["outcome"]


def strict_outcome_problems(outcome):
    """What the shared (subset) validator does not check: the schema's own maximum, type and anyOf for the counts."""
    problems = []
    for key in ("spoke", "written"):
        rule = SCHEMA_OUTCOME["properties"][key]
        if key in outcome and not (type(outcome[key]) is int and rule["minimum"] <= outcome[key] <= rule["maximum"]):
            problems.append(key)
    if "comment_source_url" in outcome:
        alternatives = SCHEMA_OUTCOME["dependentSchemas"]["comment_source_url"]["anyOf"]
        if not any(all(name in outcome for name in alt["required"]) for alt in alternatives):
            problems.append("comment_source_url")
    return problems


class RenderAndExport(Base):
    def build(self, today=TODAY):
        build_site(self.root, today=today)
        return json.loads((self.root / "public" / "data" / "meetings.json").read_text(encoding="utf-8"))

    def link(self, url):
        return f'<a href="{url.replace("&", "&amp;")}" rel="noopener noreferrer">{url.replace("&", "&amp;")}</a>'

    def test_render_and_export_exact_counts(self):
        for outcome, sentence in (
                ({"spoke": 4, "written": 12}, "4 people spoke and 12 written comments were received, as stated in "),
                ({"spoke": 1}, "1 people spoke, as stated in "),
                ({"written": 0}, "0 written comments were received, as stated in "),
                ({"spoke": 0, "written": 0}, "0 people spoke and 0 written comments were received, as stated in ")):
            with self.subTest(outcome=outcome):
                self.set_outcome(**outcome, comment_source_url=COUNT_SOURCE)
                doc = self.build()
                self.assertIn(f'<p class="meeting-comments">{sentence}{self.link(COUNT_SOURCE)}</p>', self.page())
                self.assertEqual(self.page().count('class="meeting-comments"'), 1)
                public = doc["meetings"][0]["outcome"]
                self.assertEqual({k: public[k] for k in KEYS if k in public}, {**outcome, "comment_source_url": COUNT_SOURCE})
                law.validate(doc, SCHEMA)
                self.assertEqual(strict_outcome_problems(public), [])
                # no missing count is shown as zero; no inference, name, quote or split is rendered or exported
                text = self.page() + json.dumps(doc)
                if "spoke" not in outcome:
                    self.assertNotIn("people spoke", text)
                if "written" not in outcome:
                    self.assertNotIn("written comments were received", text)
                for leaked in ("support", "oppos", "against", "unique"):
                    self.assertNotIn(leaked, self.page().split('class="meeting-comments"')[1].split("</p>")[0])
                self.assertNotIn("verified_by", json.dumps(doc))
        # the sentence comes after the older outcome paragraphs, which are not touched
        page = self.page()
        self.assertLess(page.index("Synthetic Organizer"), page.index('class="meeting-comments"'))
        # spanish draft labels fill the same placeholders with literal integers
        labels = {key: ES["labels"][key] for key in ("meeting.comments_both", "meeting.comments_spoke", "meeting.comments_written")}
        for text in labels.values():
            self.assertTrue(set(re.findall(r"\{(\w+)\}", text)) <= {"spoke", "written", "source"})
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")
        self.set_outcome(spoke=1, written=0, comment_source_url=COUNT_SOURCE)
        self.build()
        self.assertIn("1 personas hablaron y se recibieron 0 comentarios escritos, según consta en " + self.link(COUNT_SOURCE),
                      self.page())
        self.assertNotIn("people spoke", self.page())
        # not held yet, or cancelled: no paragraph, whatever is stored
        self.set_outcome(spoke=1, comment_source_url=COUNT_SOURCE)
        self.build(today=date(2026, 10, 14))
        self.assertNotIn("meeting-comments", self.page())
        self.assertNotIn("outcome", self.build(today=date(2026, 10, 14))["meetings"][0])

    def test_public_schema_and_allowlist(self):
        base = {"result": "approved", "on": "2026-10-14", "source_url": SOURCE}
        self.assertTrue({"spoke", "written", "comment_source_url"} <= set(PUBLIC_OUTCOME_FIELDS))
        self.assertTrue(set(SCHEMA_OUTCOME["properties"]) >= set(KEYS) and SCHEMA_OUTCOME["additionalProperties"] is False)
        self.assertEqual(SCHEMA_OUTCOME["properties"]["spoke"], {"type": "integer", "minimum": 0, "maximum": 999})
        document = {"schema_version": 1, "generated_at": "2026-10-20T00:00:00Z", "meetings": [{
            "id": MID, "body": "Body", "agency_id": None, "starts_at": "2026-10-14T18:00:00-07:00", "status": "scheduled",
            "agenda_url": None, "url": f"/meetings/{MID}.html", "outcome": base}]}
        valid = ({"spoke": 0, "comment_source_url": SOURCE}, {"written": 999, "comment_source_url": SOURCE},
                 {"spoke": 3, "written": 4, "comment_source_url": SOURCE}, {})
        for extra in valid:
            document["meetings"][0]["outcome"] = {**base, **extra}
            law.validate(document, SCHEMA)
            self.assertEqual(strict_outcome_problems(document["meetings"][0]["outcome"]), [])
        invalid = ({"spoke": 1}, {"written": 1}, {"comment_source_url": SOURCE}, {"spoke": "1", "comment_source_url": SOURCE},
                   {"spoke": True, "comment_source_url": SOURCE}, {"spoke": 1.5, "comment_source_url": SOURCE},
                   {"spoke": 1000, "comment_source_url": SOURCE}, {"written": -1, "comment_source_url": SOURCE},
                   {"spoke": 1, "comment_source_url": "http://example.invalid/x"},
                   {"spoke": 1, "comment_source_url": SOURCE, "against": 2}, {"spoke": 1, "comment_source_url": SOURCE, "names": []})
        for extra in invalid:
            with self.subTest(extra=extra):
                document["meetings"][0]["outcome"] = {**base, **extra}
                try:
                    law.validate(document, SCHEMA)
                except ValueError:
                    continue
                self.assertNotEqual(strict_outcome_problems(document["meetings"][0]["outcome"]), [], extra)
        # a key injected into the builder is refused naming meetings.json and nothing is written
        self.set_outcome(spoke=2, comment_source_url=COUNT_SOURCE)
        build_site(self.root, today=TODAY)
        before = self.public()
        real = Site._public_meeting
        for key in ("verified_by", "against", "speakers", "quote"):
            def builder(self_, meeting, key=key):
                value = real(self_, meeting)
                value["outcome"][key] = "x"
                return value
            with self.subTest(key=key), mock.patch.object(Site, "_public_meeting", builder):
                with self.assertRaisesRegex(ContentError, f"^data/meetings\\.json: non-public key {key}$"):
                    build_site(self.root, today=TODAY)
                self.assertEqual(self.public(), before)


class LegacyBytes(Base):
    def test_legacy_bytes_and_check_parity(self):
        # no count group: the stored outcome, the page, every public file and the loaded outcome keys are unchanged
        self.set_outcome(**AttachReplace.FULL)
        build_site(self.root, today=TODAY)
        first = self.public()
        self.assertFalse(set(KEYS) & set(self.doc()["outcome"]))
        self.assertNotIn("meeting-comments", self.page())
        self.assertNotIn("spoke", first["data/meetings.json"].decode())
        self.assertEqual(list(load_content(self.root, today=TODAY)["meetings"][0]["outcome"]),
                         [k for k in ("result", "on", "source_url", "verified_by", "verified_at", "vote", "recording_url",
                                      "recording_start", "minutes_url", "minutes_approved_on", "direction", "return_by",
                                      "direction_source")])
        stored = self.meeting.read_bytes()
        shutil.rmtree(self.root / "public")
        build_site(self.root, today=TODAY)
        self.assertEqual(self.public(), first)
        self.assertEqual(self.meeting.read_bytes(), stored)
        # the creation path without counts writes the same outcome as before
        self.meeting.write_bytes(self.original)
        outcome = self.create(replace=True, direction="Return with a draft usage policy", return_by="2026-11-01")
        self.assertEqual(set(outcome), set(BASE) | {"direction", "return_by", "direction_source"})
        # malformed hand edits are refused by build and by content-check (text and JSON), nothing is written
        for edit in ({"spoke": None, "comment_source_url": SOURCE}, {"written": 1000, "comment_source_url": SOURCE},
                     {"spoke": 1}, {"comment_source_url": SOURCE}, {"spoke": 1.0, "comment_source_url": SOURCE},
                     {"written": 2, "comment_source_url": "http://example.invalid/x"}, {"spoke": 1, "extra": 5,
                                                                                       "comment_source_url": SOURCE}):
            with self.subTest(edit=edit):
                self.set_outcome(**edit)
                shutil.rmtree(self.root / "public", ignore_errors=True)
                with self.assertRaises(ContentError):
                    build_site(self.root, today=TODAY)
                self.assertFalse((self.root / "public").exists())
                self.assertTrue(content_problems(self.root))
        self.set_outcome(written=1000, comment_source_url=SOURCE)
        done = run("content-check", "--json", "--directory", str(self.root))
        self.assertEqual(done.returncode, 3, done.stdout)
        lines = [f'{p["file"]}: {p["message"]}' if p["file"] else p["message"] for p in json.loads(done.stdout)["problems"]]
        self.assertTrue(any(line.endswith(f"outcome.written: {RANGE}") for line in lines), lines)
        done = run("content-check", "--directory", str(self.root))
        self.assertEqual(done.returncode, 3, done.stdout)
        self.assertIn(f"outcome.written: {RANGE}", done.stdout)
        # a fully valid group passes the check
        self.set_outcome(spoke=2, written=3, comment_source_url=SOURCE)
        self.assertEqual(content_problems(self.root), [])
        self.assertEqual(run("content-check", "--directory", str(self.root)).returncode, 0)


if __name__ == "__main__":
    unittest.main()
