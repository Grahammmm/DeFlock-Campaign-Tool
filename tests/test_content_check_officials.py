"""content-check validates content/officials.json whether or not statements.json exists (RF0440-4).

Synthetic fictional-example copies only; nothing is fetched.
"""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool import content_check
from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems, run_cli

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
DAY = date(2026, 10, 20)
COUNCIL = "ca-cedar-city-council"


def person(checked_on="2026-09-01", name="Rowan Example"):
    return {"name": name, "role": "Council member", "source_url": "https://cedar.example/council", "checked_on": checked_on}


def roster(*people, body=COUNCIL, version=1):
    return {"schema_version": version, "bodies": [{"agency_id": body, "officials": list(people)}]}


def statement(**extra):
    row = {"agency_id": COUNCIL, "official": "Rowan Example", "said_on": "2026-09-10", "quote": "We will review the contract.",
           "context": "meeting", "source_url": "https://cedar.example/minutes/2026-09-10", "checked_on": "2026-09-12"}
    row.update(extra)
    return row


class OfficialsCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-content-check-officials-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"

    def put(self, name, value):
        text = value if isinstance(value, str) else json.dumps(value)
        (self.content / name).write_text(text, encoding="utf-8")

    def problems(self, day=DAY):
        return content_problems(self.root, today=day)

    def build_error(self, day=DAY):
        with self.assertRaises(ContentError) as caught:
            load_content(self.root, day)
        return str(caught.exception)

    def officials_lines(self, day=DAY):
        return [line for line in self.problems(day) if "officials.json" in line]

    def test_invalid_roster_is_named_without_any_statements_file(self):
        self.assertEqual(self.problems(), [])
        for label, value in (("bad json", "{not json"), ("wrong schema", roster(person(), version=2)),
                             ("unknown body", roster(person(), body="ca-nowhere")), ("scalar", "3"),
                             ("extra key", {**roster(person()), "extra": 1})):
            with self.subTest(label):
                self.put("officials.json", value)
                self.assertFalse((self.content / "statements.json").exists())
                error = self.build_error()
                self.assertIn("officials.json", error)
                self.assertEqual(self.officials_lines(), [error], self.problems())

    def test_roster_diagnostic_is_independent_of_statement_presence(self):
        self.put("officials.json", roster(person(), version=2))
        baseline = self.officials_lines()
        self.assertEqual(len(baseline), 1)
        for rows in ([], [statement()], [statement(official="Nobody Example")], [{"bogus": 1}]):
            with self.subTest(rows=rows):
                self.put("statements.json", rows)
                self.assertEqual(self.officials_lines(), baseline)
        self.put("statements.json", [{"bogus": 1}])
        self.assertTrue(any("statements.json[0]" in line for line in self.problems()))  # independent structural error stays

    def test_strict_roster_day_boundaries_use_the_supplied_day(self):
        for statements in (False, True):
            if statements:
                self.put("statements.json", [])
            for age, ok in ((0, True), (365, True), (366, False), (-1, False)):
                with self.subTest(statements=statements, age=age):
                    self.put("officials.json", roster(person((DAY - timedelta(days=age)).isoformat())))
                    self.assertEqual(self.officials_lines() == [], ok, self.problems())
                    self.assertEqual(len(self.officials_lines()), 0 if ok else 1)

    def test_missing_and_empty_roster_do_not_skip_statement_membership(self):
        self.put("statements.json", [statement()])
        for setup in (lambda: None, lambda: self.put("officials.json", {"schema_version": 1, "bodies": []})):
            setup()
            lines = self.problems()
            self.assertEqual(len([line for line in lines if "statements.json[0].agency_id: no officials listed" in line]), 1, lines)
        self.put("officials.json", roster(person(), version=2))  # refused roster: membership suppressed, one roster line
        lines = self.problems()
        self.assertFalse(any("no officials listed" in line or "not listed" in line for line in lines), lines)
        self.assertEqual(len(self.officials_lines()), 1)

    def test_agency_failure_keeps_dependency_suppression(self):
        self.put("agencies.json", "{not json")
        self.put("officials.json", roster(person(), body="ca-nowhere"))
        reads = []
        real = content_check._read_json
        with mock.patch.object(content_check, "_read_json", side_effect=lambda p, *a, **k: (reads.append(p.name), real(p, *a, **k))[1]):
            lines = self.problems()
        self.assertTrue(lines)  # not clean: agencies refusal is named
        self.assertEqual(self.officials_lines(), [])
        self.assertNotIn("officials.json", reads)

    def test_roster_read_once_and_reused_with_statements(self):
        self.put("officials.json", roster(person()))
        self.put("statements.json", [statement()])
        reads = []
        real = content_check._read_json
        with mock.patch.object(content_check, "_read_json", side_effect=lambda p, *a, **k: (reads.append(p.name), real(p, *a, **k))[1]):
            self.assertEqual(self.problems(), [])
        self.assertEqual(reads.count("officials.json"), 1)
        out = []
        content_problems(self.root, statements_out=out, today=DAY)
        self.assertEqual(len(out), 1)

    def test_symlink_roster_is_named_like_build(self):
        target = self.content / "elsewhere.json"
        target.write_text(json.dumps(roster(person())), encoding="utf-8")
        (self.content / "officials.json").symlink_to(target)
        self.assertEqual(len(self.officials_lines()), 1, self.problems())
        (self.content / "officials.json").unlink()
        (self.content / "officials.json").symlink_to(self.content / "does-not-exist.json")
        self.assertEqual(len(self.officials_lines()), 1, self.problems())

    def test_cli_exit_codes_text_and_json(self):
        self.put("officials.json", roster(person(), version=2))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            run_cli(self.root)
        self.assertEqual(caught.exception.code, 3, out.getvalue())
        self.assertIn("content/: 1 problem(s)", out.getvalue())
        self.assertIn("officials.json.schema_version: must be 1", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            run_cli(self.root, as_json=True)
        document = json.loads(out.getvalue())
        self.assertEqual((caught.exception.code, document["count"]), (3, 1))
        self.assertEqual(document["schema"], "content-check-v1")


if __name__ == "__main__":
    unittest.main()
