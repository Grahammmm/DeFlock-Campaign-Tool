"""RF0145-3: ambiguous JSON is refused before validation or any consumer output."""
import contextlib
import html
import io
import json
import os
import shutil
import socket
import stat
import unittest
from unittest import mock

from campaign_tool import content_statements as reader, officials_cmd, recent, statements
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems, problem_record, run_cli
from tests import test_site_statements as site_fixture
from tests import test_meeting_pack_statements as pack_fixture

DAY = site_fixture.BUILD_DAY
ERROR = "statements.json: a JSON object repeats a key"


def duplicate(key="quote", first="First words.", last="Last words.", escaped=None):
    row = site_fixture.statement(recording_time="1:02:45", context_note="Synthetic note.")
    pairs = [(k, v) for k, v in row.items() if k != key]
    fields = [json.dumps(k) + ":" + json.dumps(v) for k, v in pairs]
    fields += [json.dumps(key) + ":" + json.dumps(first),
               (escaped or json.dumps(key)) + ":" + json.dumps(last)]
    return ("[{" + ",".join(fields) + "}]").encode()


def tree(root):
    """Bytes, kinds, modes and membership; do not follow links or open FIFOs."""
    result = {}
    for path in root.rglob("*"):
        info = path.lstat()
        result[path.relative_to(root).as_posix()] = (
            stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
            path.read_bytes() if stat.S_ISREG(info.st_mode) else
            os.readlink(path) if stat.S_ISLNK(info.st_mode) else None)
    return result


class DuplicateStatementsTests(unittest.TestCase):
    # Borrow setup/helpers, not a TestCase superclass (no inherited test collection).
    sources = site_fixture.StatementsTests.sources
    roster = site_fixture.StatementsTests.roster
    statements = site_fixture.StatementsTests.statements
    build = site_fixture.StatementsTests.build

    def setUp(self):
        site_fixture.StatementsTests.setUp(self)
        self.file = self.content / "statements.json"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network used")).start()

    def refuse(self, raw):
        self.file.write_bytes(raw)
        self.file.chmod(0o640)
        before = tree(self.root)
        for roster in (None, {site_fixture.COUNCIL: [site_fixture.official()]}):
            with self.assertRaises(ContentError) as caught:
                reader.read_statements(self.root, roster, DAY)
            self.assertEqual(str(caught.exception), ERROR)
            problems = ["earlier problem"]
            self.assertIsNone(reader.read_statements(self.root, roster, DAY, problems))
            self.assertEqual(problems, ["earlier problem", ERROR])
        self.assertEqual(tree(self.root), before)

    def test_duplicate_keys_refuse_before_statement_validation(self):
        row = site_fixture.statement(recording_time="1:02:45", context_note="Synthetic note.")
        with mock.patch.object(reader, "check_statements", side_effect=AssertionError("validation reached")):
            for key, value in row.items():
                for first, last in ((value, value), (None, value), (value, None)):
                    with self.subTest(key=key, first=first, last=last):
                        self.refuse(duplicate(key, first, last))
            late = json.dumps([site_fixture.statement()])[:-1].encode() + b"," + duplicate()[1:]
            self.refuse(late)

    def test_escaped_keys_and_overwritten_nested_duplicates_refuse(self):
        cases = [duplicate(escaped='"qu\\u006fte"'),
                 duplicate().replace(b'"First words."', b'{"hidden":1,"hidden":2}'),
                 duplicate().replace(b'"First words."', b'[{"hidden":1,"hidden":2}]'),
                 b'[{"unknown":{"nested":[{"x":1,"x":1}]}}]',
                 b'[{"SENTINEL\\nhttps://synthetic.invalid/p": "PRIVATEVALUE",'
                 b'"SENTINEL\\nhttps://synthetic.invalid/p": "OTHERPRIVATE"}]']
        for raw in cases:
            with self.subTest(raw=raw):
                self.refuse(raw)

    def test_decoded_key_identity_is_literal_not_normalized(self):
        for left, right in (("quote", "Quote"), ("quote", "quote "), ("é", "e\u0301")):
            raw = ("[{" + json.dumps(left) + ":1," + json.dumps(right) + ":2}]").encode()
            self.assertEqual(reader._parse(raw), [{left: 1, right: 2}])
            self.file.write_bytes(raw)
            problems = []
            self.assertIsNone(reader.read_statements(self.root, None, DAY, problems))
            self.assertTrue(any("unknown field" in p for p in problems))
            self.assertNotIn(ERROR, problems)
        raw = json.dumps([site_fixture.statement()], indent=4).replace('"quote"', '"qu\\u006fte"').encode()
        self.assertEqual(reader._parse(raw), [site_fixture.statement()])

    def test_build_and_checker_duplicate_refusal_preserve_public_tree(self):
        for existing in (False, True):
            if existing:
                self.statements(site_fixture.statement())
                self.build()
            for raw in (duplicate(), duplicate(escaped='"qu\\u006fte"'),
                        duplicate().replace(b'"First words."', b'{"hidden":1,"hidden":2}')):
                with self.subTest(existing=existing, raw=raw):
                    self.file.write_bytes(raw)
                    before = tree(self.root)
                    with self.assertRaises(ContentError) as caught:
                        self.build()
                    self.assertEqual(str(caught.exception), ERROR)
                    problems = content_problems(self.root, limit=None, today=DAY)
                    self.assertEqual(problems, [ERROR])
                    self.assertEqual(problem_record(problems[0])["file"], "statements.json")
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                            self.assertRaises(SystemExit) as exited:
                        run_cli(self.root, as_json=True)
                    self.assertEqual(exited.exception.code, 3)
                    self.assertEqual(json.loads(out.getvalue())["count"], 1)
                    self.assertNotIn("Traceback", out.getvalue() + err.getvalue())
                    self.assertEqual(tree(self.root), before)
            if not existing:
                self.assertFalse(self.public.exists())

    def pack_fixture(self):
        fixture = pack_fixture.StatementPackTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        return fixture

    def test_pack_and_official_removal_use_fail_closed_shared_reader(self):
        fixture = self.pack_fixture()
        for existing in (True, False):
            if not existing:
                shutil.rmtree(fixture.folder)
            (fixture.content / "statements.json").write_bytes(duplicate())
            before = tree(fixture.root)
            with self.assertRaisesRegex(ValueError, "a JSON object repeats a key"):
                fixture.pack(replace=True)
            self.assertEqual(tree(fixture.root), before)
            if not existing:
                self.assertFalse(fixture.folder.exists())
        self.file.write_bytes(duplicate())
        before = tree(self.root)
        with self.assertRaisesRegex(ContentError, "a JSON object repeats a key"):
            officials_cmd.remove_official(self.root, agency_id=site_fixture.COUNCIL,
                                          name="Sage Example", today=DAY)
        self.assertEqual(tree(self.root), before)

    def test_recent_reports_unreadable_without_hiding_other_events(self):
        healthy = recent.report(self.root, "2026-01-01", today=DAY)
        self.assertTrue(healthy)
        self.file.write_bytes(duplicate())
        before = tree(self.root)
        events, _, problems = recent.collect(self.root, "2026-01-01", None, today=DAY)
        self.assertTrue(all(event in events for event in healthy))
        self.assertEqual([p for _, p in problems if "content/statements.json" in p],
                         ["content/statements.json unreadable"])
        self.assertNotIn("First words.", str(events) + str(problems))
        self.assertEqual(tree(self.root), before)

    def test_existing_maintenance_refusal_agrees(self):
        quote = self.root / "quote.txt"
        quote.write_text("Synthetic words.")
        for raw in (duplicate(), duplicate(escaped='"qu\\u006fte"'),
                    duplicate().replace(b'"First words."', b'{"hidden":1,"hidden":2}')):
            self.file.write_bytes(raw)
            self.file.chmod(0o640)
            before = tree(self.root)
            calls = [lambda: statements.list_statements(self.root, today=DAY),
                     lambda: statements.remove_statement(self.root, index=1, today=DAY),
                     lambda: statements.add_statement(self.root, agency_id=site_fixture.COUNCIL,
                         official=site_fixture.ROWAN, said_on="2026-09-10", quote_file=quote,
                         context="meeting", source_url="https://cedar.example/minutes",
                         checked_on="2026-09-12", today=DAY)]
            for call in calls:
                with self.assertRaisesRegex(ContentError, "a JSON object repeats a key"):
                    call()
                self.assertEqual(tree(self.root), before)

    def test_repeated_rows_exact_quotes_and_legacy_bytes_remain_compatible(self):
        def legacy(data):
            return json.loads(data.decode("utf-8"))

        rows = [site_fixture.statement(quote='  Keep <b>é & "text"</b>  ',
                    recording_time="1:02:45", context_note="Synthetic note."),
                site_fixture.statement(quote="é" * 400), site_fixture.statement(quote="é" * 400),
                site_fixture.statement(quote="Older.", said_on="2026-08-01")]
        for document in (None, [], rows):
            if document is None:
                self.file.unlink(missing_ok=True)
            else:
                self.statements(*document)
            before = tree(self.content)
            with mock.patch.object(reader, "_parse", side_effect=legacy):
                self.build()
                original = tree(self.public)
            self.build()
            self.assertEqual(tree(self.public), original)
            self.assertEqual(tree(self.content), before)
            loaded = reader.read_statements(self.root, None, DAY)
            self.assertEqual(loaded, document)
            if document:
                page = (self.public / site_fixture.COUNCIL_PAGE).read_text()
                self.assertEqual(page.count("“" + "é" * 400 + "”"), 2)
                self.assertIn(html.escape(rows[0]["quote"], quote=True), page)
                self.assertEqual(reader.newest_first(loaded), rows)
        fixture = self.pack_fixture()
        for document in (None, [], [pack_fixture.statement("Ann Alpha", "2026-03-01", "  Exact <é>  ")] * 2):
            path = fixture.content / "statements.json"
            if document is None:
                path.unlink(missing_ok=True)
            else:
                fixture.put(document)
            with mock.patch.object(reader, "_parse", side_effect=legacy):
                original = fixture.read_pack()
            self.assertEqual(fixture.read_pack(), original)

    def test_existing_reader_refusals_survive(self):
        for raw in (b"\xff", b"{", b"[" * 10000):
            with self.assertRaisesRegex(ContentError, "not valid UTF-8 JSON"):
                reader._parse(raw)
        for raw, message in ((b"null", "must be a list"),
                             (b" " * (reader.MAX_BYTES + 1), "file too large"),
                             (json.dumps([site_fixture.statement(quote="")]).encode(), "quote"),
                             (json.dumps([site_fixture.statement(quote="pat@example.org")]).encode(), "privacy lint")):
            self.file.write_bytes(raw)
            with self.assertRaisesRegex(ContentError, message):
                reader.read_statements(self.root, None, DAY)
        self.file.unlink()
        target = self.root / "elsewhere.json"
        target.write_bytes(b"[]")
        self.file.symlink_to(target)
        with self.assertRaisesRegex(ContentError, "symlinks are not read"):
            reader.read_statements(self.root, None, DAY)
        self.file.unlink()
        os.mkfifo(self.file)
        with self.assertRaisesRegex(ContentError, "must be a regular file"):
            reader.read_statements(self.root, None, DAY)
