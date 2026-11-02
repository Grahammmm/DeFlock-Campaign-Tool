"""Offline tests for ``campaign_tool.law apply-sheet`` (RF1520-1).

Synthetic packages under a temporary ``--base``; every worksheet is first written by
``review-sheet`` and then filled in here with synthetic reviewer names. apply-sheet is a
transcription, not a review. Run from the repository root:
python3 -B -m unittest tests.test_law_apply_sheet -v
"""

import contextlib
import csv
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law  # noqa: E402
from tests.test_law_review_sheet import sha256, synthetic_package  # noqa: E402

TODAY = date.today().isoformat()
NAME_A, NAME_B = "Synthetic Reviewer A", "Synthetic Reviewer B"


class ApplySheetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-law-apply-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.base = self.tmp / "base"
        (self.base / "us-ca").mkdir(parents=True)
        self.package_file = self.base / "us-ca" / "package.json"
        self.package_file.write_text(json.dumps(synthetic_package(), indent=2), encoding="utf-8")
        (self.tmp / "sheets").mkdir()
        self.blank = self.tmp / "sheets" / "blank.csv"
        self.run_law("review-sheet", "us-ca", "--out", str(self.blank), "--as-of", "2026-01-11")
        self.out = self.tmp / "out" / "proposed.json"
        self.out.parent.mkdir()

    def run_law(self, command, *arguments, returncode=0, base=True):
        extra = ["--base", str(self.base)] if base else []
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", command, *arguments, *extra],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, returncode, result.stderr)
        return result

    def rows(self, sheet=None):
        with open(sheet or self.blank, encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    def write_sheet(self, rows, name="filled.csv", header=law.SHEET_COLUMNS):
        path = self.tmp / "sheets" / name
        with open(path, "w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows([row.get(column, "") for column in law.SHEET_COLUMNS][:len(header)] for row in rows)
        return path

    def filled(self, **by_rule):
        rows = self.rows()
        for row in rows:
            row.update(by_rule.get(row["rule_id"].replace("-", "_"), {}))
        return rows

    def conclusion(self, label, name=NAME_A, on=TODAY):
        return {"reviewer_conclusion": label, "reviewer_name": name, "reviewed_on": on}

    def apply(self, sheet, returncode=0, base=True):
        result = self.run_law("apply-sheet", "us-ca", "--sheet", str(sheet), "--out", str(self.out),
                              returncode=returncode, base=base)
        return json.loads(result.stdout) if returncode == 0 else result

    def refused(self, sheet, *fragments):
        result = self.apply(sheet, returncode=1)
        self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        for fragment in fragments:
            self.assertIn(fragment, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(list(self.out.parent.iterdir()), [])
        return result

    def test_round_trip_passes_check_law_review(self):
        sheet = self.write_sheet(self.filled(ca_a=self.conclusion("verified"),
                                             ca_b=self.conclusion(" likely ", name=f" {NAME_B} ")))
        output = self.apply(sheet)
        self.assertEqual(output["changed"], [{"rule_id": "ca-a", "from": "likely", "to": "verified"}])
        self.assertEqual((output["confirmed"], output["skipped"], output["text_edited"]), (["ca-b"], 2, []))
        self.assertEqual(output["reviewers_named"], [NAME_A, NAME_B])
        self.assertEqual((output["status"], output["out"]), ("draft", str(self.out)))
        for phrase in ("not a review", "stays draft", "reviewed_by and reviewed_at", "docs/LAW-PACKAGES.md"):
            self.assertIn(phrase, output["note"])
        old, new = json.loads(self.package_file.read_text()), json.loads(self.out.read_text())
        for key in ("status", "reviewed_by", "reviewed_at"):
            self.assertEqual(new[key], old[key])
        self.assertEqual([r["review"] for r in new["rules"]], ["verified", "likely", "likely"])
        old["rules"][0]["review"] = "verified"
        self.assertEqual(new, old)
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o600)
        check = subprocess.run([sys.executable, "-B", "tools/check_law_review.py", str(self.package_file),
                                str(self.out)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertEqual(json.loads(check.stdout)["allowed_changes"], ["$.rules[ca-a].review"])

    def test_blank_sheet_changes_nothing(self):
        output = self.apply(self.blank)
        self.assertEqual((output["changed"], output["confirmed"], output["skipped"]), ([], [], 4))
        self.assertEqual((output["text_edited"], output["reviewers_named"]), ([], []))
        self.assertEqual(json.loads(self.out.read_text()), json.loads(self.package_file.read_text()))

    def test_bad_rows_refused_with_row_number_and_nothing_written(self):
        cases = [
            ({"records_law": self.conclusion("verified")}, "row 1 (records_law): records_law has no review label"),
            ({"ca_a": self.conclusion("Verified")}, "row 2 (ca-a): conclusion must be verified, likely or"),
            ({"ca_b": self.conclusion("yes")}, "row 3 (ca-b): conclusion must be"),
            ({"ca_c": self.conclusion("likely", name="  ")}, "row 4 (ca-c): reviewer_name is blank"),
            ({"ca_a": self.conclusion("likely", on="2026-13-01")}, "row 2 (ca-a): reviewed_on: month"),
            ({"ca_a": self.conclusion("likely", on="")}, "row 2 (ca-a): reviewed_on: expected YYYY-MM-DD"),
            ({"ca_b": self.conclusion("likely", on=(date.today() + timedelta(days=1)).isoformat())},
             "row 3 (ca-b): reviewed_on is after today"),
        ]
        for fill, message in cases:
            with self.subTest(message=message):
                # A valid row before the bad one proves nothing is written when any row is refused.
                fill = {"ca_c": self.conclusion("verified"), **fill} if "ca-c" not in message else fill
                self.refused(self.write_sheet(self.filled(**fill)), message, "nothing written")
        both = self.filled(ca_a=self.conclusion("yes"), ca_c=self.conclusion("maybe"))
        self.refused(self.write_sheet(both), "row 2 (ca-a)", "2 row(s) refused")

    def test_structure_refused_before_any_row_is_applied(self):
        good = self.filled(ca_a=self.conclusion("verified"))
        duplicate = good + [dict(good[2])]
        unknown = [dict(row) for row in good]
        unknown[3]["rule_id"] = "ca-zz"
        renamed = list(law.SHEET_COLUMNS)
        renamed[law.SHEET_COLUMNS.index("reviewer_conclusion")] = "conclusion"
        for sheet, message in (
                (self.write_sheet(duplicate, "dup.csv"), "row 5: duplicate rule_id 'ca-b'"),
                (self.write_sheet(unknown, "unknown.csv"), "row 4: unknown rule_id 'ca-zz'"),
                (self.write_sheet(good, "renamed.csv", renamed),
                 "missing ['reviewer_conclusion'], unexpected ['conclusion']"),
                (self.write_sheet(good, "short-header.csv", law.SHEET_COLUMNS[:-1]),
                 "missing ['reviewed_on'], unexpected []")):
            with self.subTest(message=message):
                self.refused(sheet, message)
        path = self.tmp / "sheets" / "short.csv"
        path.write_text(",".join(law.SHEET_COLUMNS) + "\nrecords_law" + "," * 17 + "\nca-a,x\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "row 2: expected 18 cells"):
            law.read_sheet(path, synthetic_package())

    def test_edited_text_is_listed_not_applied(self):
        rows = self.filled(ca_a={**self.conclusion("needs_attorney_review"), "duty": "Reviewer rewrote this duty"},
                           ca_c={"remedy": "Changed remedy"})
        output = self.apply(self.write_sheet(rows))
        self.assertEqual(output["text_edited"], ["ca-a", "ca-c"])
        self.assertEqual(output["changed"], [{"rule_id": "ca-a", "from": "likely", "to": "needs_attorney_review"}])
        new = json.loads(self.out.read_text())
        self.assertEqual((new["rules"][0]["duty"], new["rules"][2]["remedy"]), ("Duty, ca-a", "Remedy"))

    def test_bom_and_crlf_accepted(self):
        filled = self.write_sheet(self.filled(ca_b=self.conclusion("verified"))).read_bytes()
        lf = filled.replace(b"\r\n", b"\n")
        for name, data in (("bom-crlf.csv", b"\xef\xbb\xbf" + lf.replace(b"\n", b"\r\n")), ("lf.csv", lf)):
            with self.subTest(name=name):
                sheet = self.tmp / "sheets" / name
                sheet.write_bytes(data)
                output = self.apply(sheet)
                self.assertEqual((output["changed"][0]["rule_id"], output["text_edited"]), ("ca-b", []))
                self.out.unlink()

    def test_unreadable_sheets_refused(self):
        rows = self.filled(ca_a=self.conclusion("verified"))
        latin = self.tmp / "sheets" / "latin1.csv"
        latin.write_bytes(self.write_sheet(rows).read_bytes().replace(b"Duty, ca-a", "Duté".encode("latin-1")))
        self.refused(latin, "not UTF-8")
        link = self.tmp / "sheets" / "link.csv"
        link.symlink_to(self.write_sheet(rows))
        self.refused(link, "not a symlink")
        rows[2]["reviewer_notes"] = "x" * (2 * 1024 * 1024)
        self.refused(self.write_sheet(rows, name="big.csv"), "larger than 2097152 bytes")

    def test_out_refusals_leave_no_temporary_file(self):
        sheet = self.write_sheet(self.filled(ca_a=self.conclusion("verified")))
        inside = REPO_ROOT / "rf1520-proposed.json"
        result = self.run_law("apply-sheet", "us-ca", "--sheet", str(sheet), "--out", str(inside), returncode=1)
        self.assertIn("outside the repository", result.stderr)
        self.assertFalse(inside.exists())
        self.out.write_bytes(b"keep me\n")
        self.assertIn("already exists", self.apply(sheet, returncode=1).stderr)
        self.assertEqual(self.out.read_bytes(), b"keep me\n")
        self.out.unlink()
        stderr = io.StringIO()
        with mock.patch.object(law.os, "link", side_effect=FileExistsError), contextlib.redirect_stderr(stderr):
            code = law.main(["apply-sheet", "us-ca", "--sheet", str(sheet), "--out", str(self.out),
                             "--base", str(self.base)])
        self.assertEqual(code, 1)
        self.assertIn("already exists, not overwriting", stderr.getvalue())
        self.assertEqual(list(self.out.parent.iterdir()), [])

    def test_bundled_us_ca_is_read_only(self):
        packages = [REPO_ROOT / "jurisdictions" / "us-ca" / "package.json",
                    law.JURISDICTIONS / "us-ca" / "package.json"]
        before = [sha256(p) for p in packages]
        bundled = self.tmp / "sheets" / "bundled.csv"
        self.run_law("review-sheet", "us-ca", "--out", str(bundled), "--as-of", TODAY, base=False)
        output = self.apply(bundled, base=False)
        self.assertEqual([sha256(p) for p in packages], before)
        self.assertEqual((output["status"], output["changed"]), ("draft", []))
        self.assertEqual(json.loads(self.out.read_text()), law.load_package("us-ca"))


if __name__ == "__main__":
    unittest.main()
