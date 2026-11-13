"""LAW-REVIEW-KIT-1: real CLI on shipped data, synthetic reviewed fixtures only."""
import contextlib
import copy
from datetime import date
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from campaign_tool import law
from campaign_tool.records import law_package_check as checker

ROOT = Path(__file__).resolve().parents[2]


def fixture():
    source = {"title": "Synthetic source", "url": "https://example.invalid/law", "accessed": "2026-01-01"}
    rule = {"rule_id": "ca-test-one", "citation": "Synthetic section 1", "actor": "Synthetic actor",
            "activity": "Synthetic activity", "duty": "Synthetic duty", "exceptions": [],
            "remedy": "Synthetic remedy", "effective_from": "2026-01-01", "effective_to": None,
            "sources": [source], "review": "verified"}
    return {"schema_version": 1, "jurisdiction": "us-ca", "status": "reviewed",
            "reviewed_by": ["Synthetic Reviewer"], "reviewed_at": "2026-01-02",
            "records_law": {"name": "Synthetic law", "citation": "Synthetic section",
                            "determination_days": 1, "determination_extension_days": 0,
                            "day_type": "calendar", "fee_basis": "Synthetic fee", "appeal": "Synthetic appeal",
                            "sources": [source]},
            "rules": [rule, dict(rule, rule_id="ca-test-two")], "request_scopes": []}


class LawPackageCheck(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "us-ca"
        self.directory.mkdir()
        self.package = fixture()

    def run_check(self, package=None):
        raw = json.dumps(self.package if package is None else package).encode()
        path = self.directory / "package.json"
        path.write_bytes(raw)
        before = {entry.name: entry.read_bytes() for entry in self.directory.iterdir()}
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = checker.main(["check", str(self.directory)])
        self.assertEqual(before, {entry.name: entry.read_bytes() for entry in self.directory.iterdir()})
        return code, stdout.getvalue(), stderr.getvalue()

    def test_shipped_draft_cli_in_ci(self):
        path = ROOT / "jurisdictions/us-ca/package.json"
        before = path.read_bytes()
        package = json.loads(before)
        self.assertEqual("draft", package["status"])
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "law-package",
                                 "check", "jurisdictions/us-ca"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"draft: 0 of {len(package['rules'])} rules verified", result.stdout)
        self.assertIn("Author labels:", result.stdout)
        self.assertIn("Human reviewer's checklist", result.stdout)
        self.assertEqual(before, path.read_bytes())

    def test_fully_reviewed_ok(self):
        code, out, err = self.run_check()
        self.assertEqual((0, ""), (code, err))
        self.assertIn("\nOK\n", out)
        self.assertIn("does not authenticate", out)
        for rule in self.package["rules"]:
            self.assertIn(rule["rule_id"], out)
        self.assertNotIn("Synthetic duty", out)

    def test_unverified_rule_refused(self):
        for label in ("likely", "needs_attorney_review"):
            with self.subTest(label=label):
                self.package["rules"][1]["review"] = label
                code, out, err = self.run_check()
                self.assertEqual(2, code)
                self.assertIn("RULE_UNVERIFIED ca-test-two", err)
                self.assertNotIn("\nOK\n", out)

    def test_missing_or_nonperson_reviewer(self):
        for names in ([], [""], [" \t"], ["ChatGPT"], ["TBD"], ["123"], ["Synthetic Reviewer", ""]):
            with self.subTest(names=names):
                self.package["reviewed_by"] = names
                code, _, err = self.run_check()
                self.assertEqual(2, code)
                self.assertIn("REVIEWER_MISSING", err)

    def test_duplicate_names_refused(self):
        self.package["reviewed_by"] = ["Synthetic Reviewer", " synthetic reviewer "]
        code, _, err = self.run_check()
        self.assertEqual(2, code)
        self.assertIn("REVIEWER_DUPLICATE", err)

    def test_review_date_required_calendar_and_not_future(self):
        for value in (None, "2026-02-30", "9999-12-31"):
            with self.subTest(value=value):
                self.package["reviewed_at"] = value
                code, _, err = self.run_check()
                self.assertEqual(2, code)
                self.assertIn("REVIEW_DATE_INVALID", err)
        self.package["reviewed_at"] = date.today().isoformat()
        self.assertEqual(0, self.run_check()[0])

    def test_citation_required(self):
        for value in ("", "  "):
            with self.subTest(value=value):
                self.package["rules"][0]["citation"] = value
                code, _, err = self.run_check()
                self.assertEqual(2, code)
                self.assertIn("RULE_CITATION_MISSING ca-test-one", err)

    def test_multiple_review_errors_reported_together(self):
        self.package["reviewed_by"] = []
        self.package["reviewed_at"] = None
        self.package["rules"][1]["review"] = "likely"
        code, out, err = self.run_check()
        self.assertEqual(2, code)
        for message in ("REVIEWER_MISSING", "REVIEW_DATE_INVALID", "RULE_UNVERIFIED ca-test-two"):
            self.assertIn(message, err)
        self.assertIn("Human reviewer's checklist", out)

    def test_empty_reviewed_rules_refused(self):
        self.package["rules"] = []
        code, _, err = self.run_check()
        self.assertEqual(2, code)
        self.assertIn("RULES_MISSING", err)

    def test_draft_does_not_credit_author_labels(self):
        self.package.update(status="draft", reviewed_by=[], reviewed_at=None)
        self.package["rules"][1]["review"] = "likely"
        code, out, err = self.run_check()
        self.assertEqual((0, ""), (code, err))
        self.assertIn("draft: 0 of 2 rules verified", out)
        self.assertIn("Author labels: 1 of 2 verified", out)

    def test_canonical_schema_and_semantics_still_enforced(self):
        invalid = []
        for change in ({"status": "complete"}, {"jurisdiction": "us-ny"}, {"unexpected": True},
                       {"reviewed_by": "Synthetic Reviewer"}, {"reviewed_at": "2026-1-02"}):
            invalid.append(dict(self.package, **change))
        package = copy.deepcopy(self.package)
        package["rules"][0]["sources"] = []
        invalid.append(package)
        package = copy.deepcopy(self.package)
        package["rules"][1]["rule_id"] = package["rules"][0]["rule_id"]
        invalid.append(package)
        for package in invalid:
            with self.subTest(package=package):
                code, _, err = self.run_check(package)
                self.assertEqual(2, code)
                self.assertIn("PACKAGE_INVALID", err)

    def test_missing_malformed_and_duplicate_json_refused(self):
        path = self.directory / "package.json"
        for raw in (None, b"{", b"[]", b'"text"', b'{}', b'\xff',
                    b'{"status":"draft","status":"reviewed"}'):
            with self.subTest(raw=raw):
                if raw is not None:
                    path.write_bytes(raw)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                    code = checker.main(["check", str(self.directory)])
                self.assertEqual(2, code)
                self.assertIn("PACKAGE_INVALID", err.getvalue())

    def test_uses_canonical_schema(self):
        law.validate(self.package, law.load_schema())
        law._semantic_checks(self.package, "us-ca")


if __name__ == "__main__":
    unittest.main()
