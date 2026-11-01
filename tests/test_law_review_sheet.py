"""Offline tests for ``campaign_tool.law review-sheet`` (NB-DF-15).

Synthetic packages under a temporary ``--base`` plus one read-only run on the
bundled us-ca package. The worksheet is not a legal review and never edits a
package. Run from the repository root:
python3 -B -m unittest tests.test_law_review_sheet -v
"""

import copy
import csv
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law  # noqa: E402

HUMAN = ["reviewer_conclusion", "reviewer_notes", "reviewer_name", "reviewed_on"]
TRICKY = 'Synthetic "quoted", with commas,\nand a second line'


def source(title, accessed):
    return {"title": title, "url": "https://example.invalid/" + title.replace(" ", "-"), "accessed": accessed}


def synthetic_package():
    package = copy.deepcopy(law.load_package("us-ca"))
    template = package["rules"][0]
    rules = []
    for rule_id, sources, exceptions in (
            ("ca-a", [source("a one", "2026-01-01")], ["first, exception", "second"]),
            ("ca-b", [source("b one", "2025-12-31")], []),
            ("ca-c", [source("c new", "2026-01-05"), source("c old", "2025-06-01")], [TRICKY])):
        rule = copy.deepcopy(template)
        rule.update(rule_id=rule_id, citation="Synthetic " + rule_id, actor="Agency " + rule_id,
                    activity=TRICKY, duty="Duty, " + rule_id, exceptions=exceptions, remedy="Remedy",
                    effective_from="2020-01-01", effective_to=None, sources=sources, review="likely")
        rules.append(rule)
    package["rules"] = rules
    for scope in package["request_scopes"]:
        scope["rule_ids"] = ["ca-a"]
    package["records_law"]["sources"] = [source("law now", "2026-01-11"), source("law future", "2026-02-01")]
    return package


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ReviewSheetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-law-sheet-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.base = self.tmp / "base"
        (self.base / "us-ca").mkdir(parents=True)
        self.package_file = self.base / "us-ca" / "package.json"
        self.package_file.write_text(json.dumps(synthetic_package()), encoding="utf-8")
        self.out = self.tmp / "out" / "sheet.csv"
        self.out.parent.mkdir()

    def run_law(self, *arguments, returncode=0, base=True):
        extra = ["--base", str(self.base)] if base else []
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "review-sheet", *arguments,
                                 *extra], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, returncode, result.stderr)
        return result

    def sheet(self, *extra, as_of="2026-01-11"):
        result = self.run_law("us-ca", "--out", str(self.out), "--as-of", as_of, *extra)
        with open(self.out, encoding="utf-8", newline="") as stream:
            return json.loads(result.stdout), list(csv.reader(stream))

    def test_records_law_then_rules_in_order_with_blank_human_columns(self):
        output, table = self.sheet("--stale-days", "10")
        self.assertEqual(table[0], list(law.SHEET_COLUMNS))
        rows = [dict(zip(table[0], row)) for row in table[1:]]
        self.assertEqual([r["rule_id"] for r in rows], ["records_law", "ca-a", "ca-b", "ca-c"])
        self.assertEqual((output["rule_count"], output["rows_written"]), (3, 4))
        for row in rows:
            self.assertEqual([row[c] for c in HUMAN], ["", "", "", ""])
        self.assertEqual(rows[1]["exceptions"], "first, exception | second")
        self.assertEqual(rows[1]["current_review"], "likely")
        self.assertEqual(rows[3]["source_titles"], "c new | c old")
        self.assertEqual(rows[3]["source_urls"], "https://example.invalid/c-new | https://example.invalid/c-old")
        self.assertEqual(rows[0]["source_titles"], "law now | law future")
        self.assertEqual(rows[0]["current_review"], "")
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.out.parent.iterdir()), ["sheet.csv"])

    def test_stale_boundary_named_once_with_rule_id(self):
        output, _ = self.sheet("--stale-days", "10")
        # ca-a is exactly 10 days old (not stale); ca-b is 11 (stale); law sources are new or future.
        self.assertEqual(output["stale_sources"], [
            {"rule_id": "ca-b", "title": "b one", "url": "https://example.invalid/b-one",
             "accessed": "2025-12-31", "age_days": 11},
            {"rule_id": "ca-c", "title": "c old", "url": "https://example.invalid/c-old",
             "accessed": "2025-06-01", "age_days": 224}])
        self.assertEqual(output["stale_days"], 10)

    def test_default_stale_days_is_180(self):
        output, _ = self.sheet()
        self.assertEqual(output["stale_days"], 180)
        self.assertEqual([s["title"] for s in output["stale_sources"]], ["c old"])

    def test_oldest_access_is_the_older_of_two_sources(self):
        _, table = self.sheet()
        row = dict(zip(table[0], table[4]))
        self.assertEqual(row["oldest_access"], "2025-06-01")
        self.assertEqual(row["source_age_days"], str((date(2026, 1, 11) - date(2025, 6, 1)).days))
        law_row = dict(zip(table[0], table[1]))
        self.assertEqual((law_row["oldest_access"], law_row["source_age_days"]), ("2026-01-11", "0"))

    def test_text_cells_round_trip_through_csv_reader(self):
        _, table = self.sheet()
        rows = [dict(zip(table[0], row)) for row in table[1:]]
        self.assertEqual(rows[1]["activity"], TRICKY)
        self.assertEqual(rows[3]["exceptions"], TRICKY)
        self.assertEqual(rows[2]["duty"], "Duty, ca-b")
        self.assertEqual(len(table), 5)

    def test_existing_out_is_refused_and_unchanged(self):
        self.out.write_bytes(b"keep me\n")
        result = self.run_law("us-ca", "--out", str(self.out), "--as-of", "2026-01-11", returncode=1)
        self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.out.read_bytes(), b"keep me\n")
        dangling = self.out.parent / "link.csv"
        dangling.symlink_to(self.tmp / "missing.csv")
        self.run_law("us-ca", "--out", str(dangling), "--as-of", "2026-01-11", returncode=1)
        self.assertFalse((self.tmp / "missing.csv").exists())

    def test_target_appearing_after_the_check_is_not_replaced(self):
        self.out.write_bytes(b"raced\n")
        with mock.patch.object(law.os.path, "lexists", return_value=False):
            with self.assertRaisesRegex(ValueError, "already exists"):
                law.write_review_sheet(self.out, [])
        self.assertEqual(self.out.read_bytes(), b"raced\n")
        self.assertEqual(sorted(p.name for p in self.out.parent.iterdir()), ["sheet.csv"])

    def test_out_inside_repository_is_refused(self):
        for inside in (REPO_ROOT / "nb-df-15-sheet.csv", REPO_ROOT / "docs" / "nb-df-15-sheet.csv"):
            result = self.run_law("us-ca", "--out", str(inside), "--as-of", "2026-01-11", returncode=1)
            self.assertIn("outside the repository", result.stderr)
            self.assertFalse(inside.exists())

    def test_bad_arguments(self):
        for value in ("0", "abc", "3651", "-1", "+5", " 5", "5.0", ""):
            result = self.run_law("us-ca", "--out", str(self.out), "--as-of", "2026-01-11",
                                  "--stale-days", value, returncode=2)
            self.assertIn("--stale-days", result.stderr)
        for value in ("2026-02-30", "2026-1-11", "11/01/2026", "2026-01-11\n"):
            result = self.run_law("us-ca", "--out", str(self.out), "--as-of", value, returncode=1)
            self.assertTrue(result.stderr.startswith("Stopped: --as-of"), result.stderr)
        result = self.run_law("us-zz", "--out", str(self.out), "--as-of", "2026-01-11", returncode=1)
        self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        self.assertFalse(self.out.exists())
        self.assertEqual(json.loads(self.package_file.read_text())["rules"][0]["rule_id"], "ca-a")

    def test_bundled_us_ca_is_read_only_and_stays_draft(self):
        packages = [REPO_ROOT / "jurisdictions" / "us-ca" / "package.json",
                    law.JURISDICTIONS / "us-ca" / "package.json"]
        before = [sha256(p) for p in packages]
        result = self.run_law("us-ca", "--out", str(self.out), "--as-of", "2026-10-31", base=False)
        self.assertEqual([sha256(p) for p in packages], before)
        output, package = json.loads(result.stdout), law.load_package("us-ca")
        self.assertEqual((output["status"], output["reviewed_by"]), (package["status"], package["reviewed_by"]))
        self.assertEqual((package["status"], package["reviewed_by"], package["reviewed_at"]), ("draft", [], None))
        self.assertIn("not a review", output["note"])
        self.assertIn("stays draft until a human reviewer", output["note"])
        self.assertEqual(output["rows_written"], len(package["rules"]) + 1)
        with open(self.out, encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([r["rule_id"] for r in rows], ["records_law"] + [r["rule_id"] for r in package["rules"]])
        self.assertEqual([r["current_review"] for r in rows[1:]], [r["review"] for r in package["rules"]])
        self.assertTrue(all(not r[c] for r in rows for c in HUMAN))


if __name__ == "__main__":
    unittest.main()
