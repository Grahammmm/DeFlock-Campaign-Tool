"""content/statements.json "What decision makers have said" (DF1107-07), built from a temp copy of the fictional example.

Fictional names and .example hosts only; nothing is fetched.
"""
import contextlib
import io
import json
import os
import shutil
import socket
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems, run_cli
from campaign_tool.legal_claims import site_notes
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BUILD_DAY = date(2026, 10, 20)
COUNCIL, POLICE = "ca-cedar-city-council", "ca-cedar-police"
COUNCIL_PAGE, POLICE_PAGE = "agencies/cedar-city-council.html", "agencies/cedar.html"
ROWAN = "Rowan Example"


def official(name=ROWAN, **extra):
    entry = {"name": name, "role": "Council member", "profile_url": None,
             "source_url": "https://cedar.example/council", "checked_on": "2026-09-01"}
    entry.update(extra)
    return {key: value for key, value in entry.items() if value is not None}


def statement(**extra):
    row = {"agency_id": COUNCIL, "official": ROWAN, "said_on": "2026-09-10", "quote": "We will review the contract.",
           "context": "meeting", "source_url": "https://cedar.example/minutes/2026-09-10", "checked_on": "2026-09-12"}
    row.update(extra)
    return {key: value for key, value in row.items() if value is not None}


class StatementsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-statements-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content, self.public = self.root / "content", self.root / "public"
        self.sources(True)
        self.roster(official(), official("Sage Example"))

    def sources(self, council_page):
        data = json.loads((self.content / "sources.json").read_text(encoding="utf-8"))
        data = [row for row in data if row["agency_id"] != COUNCIL]
        if council_page:
            data.append({"sha256": "d00d" * 16, "title": "Synthetic council roster", "agency_id": COUNCIL,
                         "received_at": "2026-02-01T10:00:00+00:00", "public_url": None,
                         "fragment": "cedar-council-records", "pages": 1})
        (self.content / "sources.json").write_text(json.dumps(data), encoding="utf-8")

    def roster(self, *people, body=COUNCIL):
        (self.content / "officials.json").write_text(json.dumps(
            {"schema_version": 1, "bodies": [{"agency_id": body, "officials": list(people)}]}), encoding="utf-8")

    def statements(self, *rows):
        (self.content / "statements.json").write_text(json.dumps(list(rows), ensure_ascii=False), encoding="utf-8")

    def build(self):
        return build_site(self.root, today=BUILD_DAY)

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def refused(self, *rows, message):
        self.statements(*rows)
        before = self.snapshot() if self.public.exists() else None
        with self.assertRaises(ContentError) as caught:
            self.build()
        self.assertIn(message, str(caught.exception))
        self.assertEqual(self.snapshot() if self.public.exists() else None, before)
        problems = content_problems(self.root, limit=None, today=BUILD_DAY)
        self.assertTrue(any(message in line for line in problems), problems)
        return str(caught.exception)

    # absent file and [] are byte-identical to before; removing the file restores every byte
    def test_no_file_or_empty_list_changes_nothing(self):
        self.build()
        base = self.snapshot()
        self.assertNotIn("statements", load_content(self.root, BUILD_DAY))
        self.assertFalse(any(b"What decision makers have said" in data for data in base.values()))
        self.statements()
        self.build()
        self.assertEqual(self.snapshot(), base)
        self.assertEqual(content_problems(self.root, limit=None, today=BUILD_DAY), [])
        self.statements(statement())
        self.build()
        self.assertNotEqual(self.snapshot(), base)
        (self.content / "statements.json").unlink()
        self.build()
        self.assertEqual(self.snapshot(), base)

    # section content, order (newest first, stable same-day ties), exact quote text, only on the matching body's page
    def test_section_newest_first_with_exact_quote(self):
        quote = '  Keep  this <b>exact</b> & "quoted" text — 100%  '
        self.statements(
            statement(said_on="2026-08-01", quote="Older words."),
            statement(quote=quote, recording_time="1:02:45", context_note="Said before the vote."),
            statement(official="Sage Example", quote="Same day, later row.", context="press_release"),
            statement(said_on="2026-09-01", quote="Even older."))
        self.build()
        page = self.read(COUNCIL_PAGE)
        self.assertEqual(page.count("What decision makers have said"), 1)
        self.assertIn('<p class="statement-quote" style="white-space: pre-wrap">\u201c' + "  Keep  this &lt;b&gt;exact&lt;/b&gt; &amp; "
                      '&quot;quoted&quot; text — 100%  ' + '\u201d</p>', page.replace("&#x27;", "'"))
        self.assertIn("(recording at 1:02:45)", page)
        self.assertIn("Campaign context: Said before the vote.", page)
        self.assertIn('<a href="https://cedar.example/minutes/2026-09-10" rel="noopener noreferrer">Official source</a>', page)
        self.assertIn("checked <time datetime=\"2026-09-12\">2026-09-12</time>", page)
        order = [page.index(text) for text in ("Keep ", "Same day, later row.", "Even older.", "Older words.")]
        self.assertEqual(order, sorted(order))
        self.assertIn("(press release)", page)
        self.assertNotIn("What decision makers have said", self.read(POLICE_PAGE))
        self.assertNotIn("statement-quote", self.read("meetings/cedar-council-2026-10.html"))
        self.build()  # repeat builds are deterministic
        self.assertEqual(self.read(COUNCIL_PAGE), page)

    def test_quote_of_400_is_never_shortened_and_repeats_are_not_merged(self):
        long = "é" * 400
        self.statements(statement(quote=long), statement(quote=long))
        self.build()
        self.assertEqual(self.read(COUNCIL_PAGE).count("\u201c" + long + "\u201d"), 2)

    # a body with a statement but no findings or sources still gets a reachable page and sitemap path
    def test_statement_only_body_gets_agency_page(self):
        self.sources(False)
        self.build()
        self.assertFalse((self.public / COUNCIL_PAGE).exists())
        self.statements(statement())
        self.build()
        self.assertIn("statement-quote", self.read(COUNCIL_PAGE))
        self.assertIn(f'href="/{COUNCIL_PAGE}"', self.read("agencies.html"))

    def test_labels_replace_every_statements_string(self):
        self.statements(statement(recording_time="0:00:05", context_note="Note."))
        keys = ("heading", "line", "quote", "context_meeting", "source", "recording", "checked", "campaign_context")
        labels = {"statements." + key: "X" + key + "X" for key in keys}
        labels["statements.line"] = "XlineX {official} {date} {context}"
        labels["statements.quote"] = "XquoteX{quote}"
        labels["statements.recording"] = "XrecordingX {time}"
        labels["statements.checked"] = "XcheckedX {date}"
        labels["statements.campaign_context"] = "XcampaignX {note}"
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")
        self.build()
        page = self.read(COUNCIL_PAGE)
        for marker in ("XheadingX", "XlineX", "XquoteX", "Xcontext_meetingX", "XsourceX", "XrecordingX", "XcheckedX", "XcampaignX"):
            self.assertIn(marker, page)
        self.assertNotIn("What decision makers have said", page)

    # identity: exact, case-sensitive, within the named body; never a similar official
    def test_unknown_or_misspelled_official_and_body_refused_by_name(self):
        for official_name in ("rowan example", "Rowan  Example", "Rowan Example ", "Nobody Example"):
            message = self.refused(statement(official=official_name), message="statements.json[0].official: not listed")
            self.assertNotIn("Nobody", message)
        self.refused(statement(), statement(agency_id=POLICE), message="statements.json[1].agency_id: no officials listed")
        self.roster(official(), body=COUNCIL)
        self.refused(statement(official="Sage Example"), message="statements.json[0].official")

    def test_same_name_in_two_bodies_resolves_only_within_its_body(self):
        (self.content / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": [
            {"agency_id": COUNCIL, "officials": [official()]},
            {"agency_id": POLICE, "officials": [official("Pat Example")]}]}), encoding="utf-8")
        self.refused(statement(agency_id=POLICE), message="statements.json[0].official: not listed")
        self.statements(statement(agency_id=POLICE, official="Pat Example", quote="Police body quote."))
        self.build()
        self.assertIn("Police body quote.", self.read(POLICE_PAGE))
        self.assertNotIn("Police body quote.", self.read(COUNCIL_PAGE))

    def test_missing_or_removed_roster_refuses_the_rebuild(self):
        self.statements(statement())
        self.build()
        (self.content / "officials.json").unlink()
        with self.assertRaisesRegex(ContentError, r"statements.json\[0\].agency_id: no officials listed"):
            self.build()
        self.roster(official("Sage Example"))
        with self.assertRaisesRegex(ContentError, r"statements.json\[0\].official: not listed"):
            self.build()

    def test_stale_roster_is_reported_once_by_check_and_membership_is_skipped(self):
        self.roster(official(checked_on="2024-01-01"))
        self.statements(statement(official="Nobody Example"), statement(said_on="2026-13-01"))
        problems = content_problems(self.root, limit=None, today=BUILD_DAY)
        self.assertEqual(sum("older than 365 days" in line for line in problems), 1, problems)
        self.assertFalse(any("not listed" in line for line in problems))
        self.assertTrue(any("statements.json[1].said_on" in line for line in problems))

    # dates
    def test_dates(self):
        self.refused(statement(said_on="2026-10-21", checked_on="2026-10-21"), message="statements.json[0].said_on: is in the future")
        self.refused(statement(checked_on="2026-10-21"), message="statements.json[0].checked_on: must not be after the build date")
        self.refused(statement(said_on="2026-09-13"), message="statements.json[0].said_on: must not be after checked_on")
        for bad in ("2026-02-29", "2026-9-10", "20260910", "2026-09-10T00:00", "２０２６-09-10", 20260910):
            self.refused(statement(said_on=bad), message="statements.json[0].said_on: must be a real YYYY-MM-DD date")
        self.statements(statement(said_on="2020-02-29", checked_on="2020-03-01"))  # old statements are fine
        self.build()
        self.statements(statement(said_on="2026-10-20", checked_on="2026-10-20"))
        self.build()

    # quote and note
    def test_quote_bounds_and_note_bounds(self):
        for bad in ("", "   ", "x" * 401, "tab\there", "line\nbreak", "bidi\u202eend", "zero\u200bwidth", 5, None):
            self.refused(statement(quote=bad), message="statements.json[0].quote")
        for bad in ("", "  ", "x" * 201, "a<b", "a\nb", "z\u200bw", 7):
            self.refused(statement(context_note=bad), message="statements.json[0].context_note")
        self.statements(statement(quote="x"), statement(quote="y" * 400, context_note="n" * 200))
        self.build()

    def test_refusal_never_echoes_quote_or_note(self):
        text = self.refused(statement(quote="SECRETQUOTE" * 40), message="statements.json[0].quote")
        self.assertNotIn("SECRETQUOTE", text)
        text = self.refused(statement(context_note="SECRETNOTE<"), message="statements.json[0].context_note")
        self.assertNotIn("SECRETNOTE", text)

    # shape
    def test_shape_context_url_recording_time_and_limits(self):
        self.refused(statement(context="speech"), message="statements.json[0].context: choose one of")
        for bad in ("http://cedar.example/a", "https://user@cedar.example/a", "https://cedar.example/ a", "ftp://x.example/", "https://localhost/",
                    "https://cedar.example/" + "a" * 500, 7):
            self.refused(statement(source_url=bad), message="statements.json[0].source_url")
        for bad in ("", "1:2:3", "01:02:03", "1:60:00", "1:00:60", "1000:00:00", "-1:00:00", "1:02", "١:02:03", 3):
            self.refused(statement(recording_time=bad), message="statements.json[0].recording_time")
        self.refused(statement() | {"context_note": None}, message="context_note: must be omitted")
        self.refused(statement() | {"recording_time": None}, message="recording_time: must be omitted")
        self.refused(statement() | {"surprise": 1}, message="statements.json[0]: unknown field")
        self.refused({k: v for k, v in statement().items() if k != "quote"}, message="statements.json[0].quote: missing")
        self.refused("not an object", message="statements.json[0]: must be an object")
        self.statements(*[statement()] * 201)
        with self.assertRaisesRegex(ContentError, "at most 200 statements"):
            self.build()
        self.statements(*[statement()] * 200)
        self.build()
        for bad in ("null", '{"a": 1}', '"x"'):
            (self.content / "statements.json").write_text(bad, encoding="utf-8")
            with self.assertRaisesRegex(ContentError, "statements.json: must be a list"):
                self.build()
        self.statements(statement(recording_time="0:00:00"), statement(recording_time="999:59:59"))
        self.build()

    # file kinds: refused before output is touched
    def test_unreadable_file_kinds_refused(self):
        path = self.content / "statements.json"
        self.statements(statement())
        self.build()
        base = self.snapshot()
        path.unlink()
        path.write_bytes(b"\xff\xfe not utf8")
        self.assertRaisesRegex(ContentError, "not valid UTF-8 JSON", self.build)
        path.write_text("[" * 100000, encoding="utf-8")
        self.assertRaisesRegex(ContentError, "not valid UTF-8 JSON", self.build)
        path.write_text("[" + " " * (2 * 1024 * 1024) + "]", encoding="utf-8")
        self.assertRaisesRegex(ContentError, "file too large", self.build)
        path.unlink()
        path.mkdir()
        self.assertRaisesRegex(ContentError, "must be a regular file", self.build)
        path.rmdir()
        outside = self.root / "outside.json"
        outside.write_text("[]", encoding="utf-8")
        os.symlink(outside, path)
        self.assertRaisesRegex(ContentError, "symlinks are not read", self.build)
        path.unlink()
        os.symlink(self.root / "missing.json", path)
        self.assertRaisesRegex(ContentError, "symlinks are not read", self.build)
        path.unlink()
        os.mkfifo(path)
        self.assertRaisesRegex(ContentError, "must be a regular file", self.build)
        self.assertTrue(any("statements.json" in line for line in content_problems(self.root, limit=None, today=BUILD_DAY)))
        path.unlink()
        self.assertEqual(self.snapshot(), base)

    # privacy: shared lint, field and category only
    def test_privacy_lint_refuses_without_matched_text(self):
        for field, value in (("quote", "Write me at pat@example.org please"), ("quote", "Call 415-555-0134 today"),
                             ("context_note", "see 7ABC123 plate"), ("source_url", "https://cedar.example/p/415-555-0134")):
            text = self.refused(statement(**{field: value}), message=f"statements.json[0].{field}")
            self.assertIn("privacy lint", text)
            for leaked in ("pat@example.org", "415-555", "7ABC123"):
                self.assertNotIn(leaked, text)

    def test_checker_json_names_the_statements_file(self):
        from campaign_tool.content_check import problem_record
        self.statements(statement(official="Nobody Example"))
        line = content_problems(self.root, limit=None, today=BUILD_DAY)[0]
        self.assertEqual(problem_record(line)["file"], "statements.json[0].official")
        self.roster(official(checked_on="2024-01-01"))
        line = next(l for l in content_problems(self.root, limit=None, today=BUILD_DAY) if "365" in l)
        self.assertEqual(problem_record(line)["file"], "officials.json.bodies[0].officials[0].checked_on")

    def test_no_network_for_valid_links(self):
        self.statements(statement(source_url="https://cedar.example/video?t=1"))
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network used")):
            self.build()
            content_problems(self.root, limit=None, today=BUILD_DAY)

    # legal-claim guard: advisory only, identical on build side and checker side, never echoing the note
    def test_context_note_legal_guard_is_advisory_and_identical(self):
        claim = "This camera network is illegal."
        self.statements(statement(context_note=claim), statement(quote="This is illegal and unlawful.", context_note="Said at item 4."))
        self.build()  # accepted
        notes = [line for line in site_notes(self.root) if "statements.json" in line]
        self.assertEqual(len(notes), 1)
        self.assertIn("statements.json[0].context_note", notes[0])
        self.assertNotIn(claim, notes[0])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            run_cli(self.root, as_json=True)
        self.assertEqual(json.loads(out.getvalue())["count"], 0)
        self.assertEqual([line for line in err.getvalue().splitlines() if "statements.json" in line], notes)
        self.statements(statement(context_note="Attributed: the clerk said it was posted."))
        self.assertEqual([line for line in site_notes(self.root) if "statements.json" in line], [])


if __name__ == "__main__":
    unittest.main()
