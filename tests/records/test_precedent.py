"""Precedent lookup: private evidence names the agency, the draft phrase never does."""
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import item_tiers as it
from campaign_tool.records import playbook as pb
from campaign_tool.records import precedent as pr

HEADER = ",".join(pr.HEADER) + "\n"
GOOD = HEADER + 'city-alpha,logs,"monthly CSV exports with plate numbers redacted",2026-09-16,"ref 1"\n'


def write(text):
    d = tempfile.mkdtemp()
    p = Path(d) / "precedents.csv"
    p.write_text(text, encoding="utf-8")
    return p


def tiers(text, rating, key="county-sheriff"):
    rep = {"schema": it.SCHEMA_IN, "run_id": "r1", "requests": [
        {"agency_key": key, "request_id": "R-1", "flags": [],
         "asks": [{"ask_id": "a1", "text": text, "rating": rating, "evidence": [], "flags": []}]}]}
    return it.tier_report(rep, pb.load())


class PrecedentTests(unittest.TestCase):
    def rows(self, text=GOOD):
        rows, problems = pr.load(write(text))
        self.assertEqual(problems, [])
        return rows

    def test_denied_log_ask_gets_unnamed_phrase_and_named_private_evidence(self):
        out = pr.precedents(tiers("ALPR search logs for January", "withheld"), self.rows())
        hit = out["requests"][0]["precedents"][0]
        self.assertIn("Another county agency", hit["draft_phrase"])
        self.assertFalse(pr.names_agency(hit["draft_phrase"], ["city-alpha"]))
        self.assertEqual(hit["private_evidence"][0]["agency"], "city-alpha")
        self.assertTrue(out["private"] and not out["publishable"] and out["review_required"])

    def test_own_agency_is_never_its_own_precedent(self):
        out = pr.precedents(tiers("search logs", "withheld", key="city-alpha"), self.rows())
        self.assertEqual(out["requests"][0]["precedents"], [])

    def test_answered_or_non_tier1_asks_get_nothing(self):
        self.assertEqual(pr.precedents(tiers("search logs", "answered"), self.rows())["requests"][0]["precedents"], [])
        self.assertEqual(pr.precedents(tiers("the vendor contract", "withheld"), self.rows())["requests"][0]["precedents"], [])

    def test_form_text_naming_an_agency_is_refused_at_load(self):
        rows, problems = pr.load(write(HEADER + 'city-alpha,logs,"the City Alpha export",2026-09-16,"r"\n'))
        self.assertEqual(rows, [])
        self.assertEqual(problems[0]["field"], "form")

    def test_bad_rows_are_reported_not_used(self):
        text = GOOD + GOOD.splitlines()[1] + "\n" + 'x,money,"f",2026-13-40,"r"\n'
        rows, problems = pr.load(write(text))
        self.assertEqual(len(rows), 1)
        self.assertEqual({p["field"] for p in problems}, {"row", "kind", "released_on"})

    def test_bad_header_and_symlink_refused(self):
        self.assertEqual(pr.load(write("a,b\n1,2\n"))[0], [])
        p = write(GOOD)
        link = p.with_name("link.csv")
        os.symlink(p, link)
        self.assertEqual(pr.load(link)[1][0]["reason"], "not a regular file")

    def test_wrong_report_schema_refused(self):
        with self.assertRaises(pb.PlaybookError):
            pr.precedents({"schema": "nope"}, self.rows())


ROW_A = 'city-alpha,logs,"monthly CSV exports with plate numbers redacted",2026-09-16,"ref 1"'
ROW_B = "town-beta,audits,annual audit summary,2026-08-01,ref 2"
A = {"agency": "city-alpha", "kind": "logs", "form": "monthly CSV exports with plate numbers redacted",
     "released_on": "2026-09-16", "source_ref": "ref 1"}
B = {"agency": "town-beta", "kind": "audits", "form": "annual audit summary", "released_on": "2026-08-01",
     "source_ref": "ref 2"}
MALFORMED = [{"line": 0, "field": "file", "reason": "malformed CSV"}]
ROOT = Path(__file__).resolve().parents[2]


def write_bytes(data):
    p = Path(tempfile.mkdtemp()) / "precedents.csv"
    p.write_bytes(data)
    return p


def width(line, n):
    return {"line": line, "field": "row", "reason": "%d columns, expected 5" % n}


class PrecedentCsvAdmissionTests(unittest.TestCase):
    def test_extra_and_missing_columns_are_problems_not_evidence(self):
        text = HEADER + "\n".join([ROW_A, ROW_A[:-1] + 'x",extra', ROW_A + ",", ROW_A.rsplit(",", 1)[0], "   ", ROW_B]) + "\n"
        rows, problems = pr.load(write(text))
        self.assertEqual(rows, [A, B])
        self.assertEqual(problems, [width(3, 6), width(4, 6), width(5, 4), width(6, 1)])

    def test_malformed_csv_discards_preceding_rows(self):
        cases = {
            "unterminated quote": HEADER + ROW_A + "\n" + 'town-beta,audits,"annual,2026-08-01,ref 2\n',
            "junk after closing quote": HEADER + ROW_A + "\n" + 'town-beta,audits,"annual"x,2026-08-01,ref 2\n',
            "malformed header": '"agency,kind,form,released_on,source_ref\n' + ROW_A + "\n",
            "junk in header": 'agency,"kind"x,form,released_on,source_ref\n' + ROW_A + "\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertEqual(pr.load(write(text)), ([], MALFORMED))

    def test_oversized_csv_field_is_controlled_refusal(self):
        limit = csv.field_size_limit()
        payload = "q" * (limit + 1)
        text = HEADER + ROW_A + "\n" + 'town-beta,audits,annual,2026-08-01,"%s"\n' % payload
        self.assertLess(len(text.encode()), pr.MAX_BYTES)
        rows, problems = pr.load(write(text))
        self.assertEqual((rows, problems), ([], MALFORMED))
        self.assertNotIn("qqqq", repr(problems))
        self.assertEqual(csv.field_size_limit(), limit)

    def test_multiline_and_blank_records_keep_physical_lines(self):
        lines = [HEADER.rstrip("\n"), ROW_A[:-1] + 'part', 'two"', "", 'x-y,money,f,2026-01-01,r', "",
                 ROW_A[:-1] + 'part', 'two"']
        for eol in ("\n", "\r\n"):
            with self.subTest(eol=repr(eol)):
                rows, problems = pr.load(write_bytes((eol.join(lines) + eol).encode()))
                self.assertEqual(rows, [dict(A, source_ref="ref 1part" + eol + "two")])
                self.assertEqual(problems, [{"line": 5, "field": "kind", "reason": "not one of logs, audits"},
                                            {"line": 7, "field": "row", "reason": "duplicate"}])

    def test_valid_csv_dialect_and_report_bytes_unchanged(self):
        data = ("\ufeff" + HEADER + 'city-alpha,logs,"exports, monthly",2026-09-16,"ref ""A"" 1"\r\n'
                'town-beta,audits," audit\r\nsummary ",2026-08-01, r2 \r\n').encode()
        p = write_bytes(data)
        rows, problems = pr.load(p)
        self.assertEqual(problems, [])
        self.assertEqual(rows, [
            {"agency": "city-alpha", "kind": "logs", "form": "exports, monthly", "released_on": "2026-09-16",
             "source_ref": 'ref "A" 1'},
            {"agency": "town-beta", "kind": "audits", "form": "audit\r\nsummary", "released_on": "2026-08-01",
             "source_ref": "r2"}])
        self.assertEqual(p.read_bytes(), data)
        self.assertEqual(self.rows_of(GOOD + ROW_B + "\n"), [A, B])

    def rows_of(self, text):
        rows, problems = pr.load(write(text))
        self.assertEqual(problems, [])
        return rows

    def test_later_agency_still_blocks_named_form(self):
        text = HEADER + 'city-alpha,logs,"the Town Beta export",2026-09-16,r\n' + "town-beta,money,x,2026-01-01,r\n"
        rows, problems = pr.load(write(text))
        self.assertEqual(rows, [])
        self.assertEqual(problems, [
            {"line": 2, "field": "form", "reason": "names an agency; the form text goes into follow-ups"},
            {"line": 3, "field": "kind", "reason": "not one of logs, audits"}])


REPORT = {"schema": it.SCHEMA_IN, "run_id": "r1", "requests": [
    {"agency_key": "county-sheriff", "request_id": "R-1", "flags": [],
     "asks": [{"ask_id": "a1", "text": "ALPR search logs for January", "rating": "withheld", "evidence": [], "flags": []}]}]}
PHRASE = "Another county agency has produced an existing log record in this form: monthly CSV exports with plate numbers redacted."
TEXT_OUT = """PRIVATE: reviewer evidence only; not for publication; the draft phrase never names the agency.
county-sheriff R-1: 1 precedent(s)
  ask a1 [withheld] ALPR search logs for January
    draft phrase: %s
    evidence: city-alpha released monthly CSV exports with plate numbers redacted on 2026-09-16 (ref 1)
""" % PHRASE


class PrecedentCliTests(unittest.TestCase):
    def run_cli(self, register, report, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "precedent", str(report),
                               "--register", str(register), *extra],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)

    def test_cli_refuses_invalid_register_before_report_read(self):
        bad = {
            "syntax": (HEADER + ROW_A + "\n" + 'town-beta,audits,"annual,2026-08-01,ref 2\n', "line 0 file: malformed CSV"),
            "width": (HEADER + ROW_A + ',extra\n', "line 2 row: 6 columns, expected 5"),
            "oversize": (HEADER + 'town-beta,audits,a,2026-08-01,"%s"\n' % ("q" * (csv.field_size_limit() + 1)),
                         "line 0 file: malformed CSV"),
            "mixed": (GOOD + ROW_B + ",\n", "line 3 row: 6 columns, expected 5"),
        }
        missing = Path(tempfile.mkdtemp()) / "missing-response-items.json"
        for name, (text, diag) in bad.items():
            register = write(text)
            before = register.read_bytes()
            for extra in ((), ("--json",)):
                with self.subTest(name, json=bool(extra)):
                    run = self.run_cli(register, missing, *extra)
                    self.assertEqual((run.returncode, run.stdout), (2, ""))
                    self.assertEqual(run.stderr, "records precedent: register %s\n" % diag)
                    self.assertEqual(register.read_bytes(), before)

    def test_valid_register_report_output_unchanged(self):
        register = write(GOOD)
        report = Path(tempfile.mkdtemp()) / "response-items.json"
        report.write_text(json.dumps(REPORT), encoding="utf-8")
        before = (register.read_bytes(), report.read_bytes())
        run = self.run_cli(register, report)
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, TEXT_OUT, ""))
        run = self.run_cli(register, report, "--json")
        self.assertEqual((run.returncode, run.stderr), (0, ""))
        expected = {"schema": pr.SCHEMA, "run_id": "r1", "private": True, "publishable": False, "review_required": True,
                    "limits": list(pr.LIMITS), "requests": [{"agency_key": "county-sheriff", "request_id": "R-1", "precedents": [
                        {"ask_id": "a1", "ask_text": "ALPR search logs for January", "ask_state": "withheld",
                         "kind": "logs", "draft_phrase": PHRASE, "private_evidence": [A]}]}]}
        self.assertEqual(run.stdout, json.dumps(expected, indent=2, sort_keys=True) + "\n")
        self.assertEqual((register.read_bytes(), report.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
