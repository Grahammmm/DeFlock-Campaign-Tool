"""agency-policy-matrix: requirement skeleton with empty verdict columns, on synthetic data."""
import contextlib
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import agency_policies as ap
from campaign_tool.records import agency_policy_matrix as m
from campaign_tool.records import cli, law_package_check

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 64
HEAD = ",".join(ap.HEADER)


def prow(**kw):
    r = {"agency": "example-pd", "policy_id": "Policy 900", "section": "900.5(e)",
         "quote": "audits at the first of each year", "doc_sha256": SHA, "page": "2", "adopted_date": "2024-01-15"}
    r.update(kw)
    return ",".join('"' + r[c] + '"' for c in ap.HEADER)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        pkg = json.loads((ROOT / "jurisdictions/us-ca/package.json").read_text(encoding="utf-8"))
        by = {}
        for r in pkg["rules"]:
            by.setdefault(r["review"], r)
        self.rules = [by["verified"], by["likely"], by["needs_attorney_review"]]
        ids = {r["rule_id"] for r in self.rules}
        pkg["rules"] = self.rules
        scopes = []
        for s in pkg["request_scopes"]:
            s["rule_ids"] = [i for i in s["rule_ids"] if i in ids]
            if s["rule_ids"]:
                scopes.append(s)
        pkg["request_scopes"] = scopes
        self.pkg = self.d / "us-ca"
        self.pkg.mkdir()
        (self.pkg / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        law_package_check.check(self.pkg)  # fixture is a valid package
        self.pol = self.write_pol(HEAD, prow(), prow(section="900.6", quote="compliant"),
                                  prow(agency="other-pd", section="1.1"))

    def write_pol(self, *lines):
        p = self.d / "agency-policies.csv"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def run_cli(self, agency="example-pd", package=None, out=None, policies=None):
        out = out or self.d / "POLICY-MATRIX.tsv"
        argv = ["--policies", str(policies or self.pol), "--agency", agency,
                "--package", str(package or self.pkg), "--out", str(out)]
        so, se = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            rc = m.main(argv)
        return rc, so.getvalue(), se.getvalue(), Path(out)

    def lines(self, out):
        return [x.split("\t") for x in out.read_text(encoding="utf-8").split("\n")[:-1]]

    def test_golden_skeleton_two_policies_three_rules(self):
        rc, so, _, out = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertEqual(so.strip(), "policy-matrix: 3 law rows, 2 policy rows, package status draft")
        rows = self.lines(out)
        self.assertEqual(rows[0], m.HEADER)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(len(r) == 10 for r in rows))
        for r, rule in zip(rows[1:4], self.rules):
            self.assertEqual((r[0], r[2], r[3], r[4], r[6]),
                             (rule["rule_id"], "law", rule["citation"], rule["duty"], rule["review"]))
        self.assertEqual(rows[4][:6], ["POLICY:Policy-900:900.5(e)", rows[4][1], "agency-policy",
                                       "Policy 900 900.5(e), p. 2", "audits at the first of each year", "UNVERIFIED"])
        self.assertEqual(rows[5][0], "POLICY:Policy-900:900.6")
        self.assertEqual(rows[5][4], "compliant")
        self.assertNotIn("1.1", out.read_text(encoding="utf-8").split("Policy-900")[0])

    def test_verdict_evidence_confidence_always_empty(self):
        _, _, _, out = self.run_cli()
        for r in self.lines(out)[1:]:
            self.assertEqual(r[7:], ["", "", ""])

    def test_cite_label_follows_rule_review_flag(self):
        _, _, _, out = self.run_cli()
        rows = self.lines(out)
        got = [(r[5], r[6]) for r in rows[1:4]]
        self.assertEqual(got, [("VERIFIED-IN-PACKAGE", "verified"), ("UNVERIFIED", "likely"),
                               ("UNVERIFIED", "needs_attorney_review")])
        self.assertEqual([(r[5], r[6]) for r in rows[4:]], [("UNVERIFIED", "n/a")] * 2)

    def test_no_stored_policy_is_one_line(self):
        rc, _, _, out = self.run_cli(agency="absent-pd", package=self.d / "missing")
        self.assertEqual(rc, 0)
        self.assertEqual(out.read_text(encoding="utf-8"), "no stored policy: absent-pd\n")
        empty = self.write_pol(HEAD)
        rc, _, _, out2 = self.run_cli(policies=empty, out=self.d / "b.tsv")
        self.assertEqual(rc, 0)
        self.assertEqual(out2.read_text(encoding="utf-8"), "no stored policy: example-pd\n")

    def test_policy_file_problem_refuses_and_writes_nothing(self):
        for name, pol in (("badhead", self.write_pol("agency,x", "a,b")),
                          ("short", self.write_pol(HEAD, "example-pd,only"))):
            out = self.d / (name + ".tsv")
            rc, so, se, _ = self.run_cli(policies=pol, out=out)
            self.assertEqual(rc, 2)
            self.assertIn("refused policy_matrix_policies", se)
            self.assertNotIn("only", se)
            self.assertFalse(out.exists())

    def test_cell_separators_collapsed(self):
        pol = self.write_pol(HEAD, prow(quote="a\tb\nc  d"))
        _, _, _, out = self.run_cli(policies=pol)
        rows = self.lines(out)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(len(r) == 10 for r in rows))
        self.assertEqual(rows[4][4], "a b c d")

    def test_invalid_package_and_agency_refused(self):
        bad = self.d / "bad"
        bad.mkdir()
        (bad / "package.json").write_text("{not json", encoding="utf-8")
        for pk in (self.d / "nope", bad):
            rc, _, se, out = self.run_cli(package=pk)
            self.assertEqual(rc, 2)
            self.assertIn("refused policy_matrix_package", se)
            self.assertFalse(out.exists())
        rc, _, se, out = self.run_cli(agency="a/b")
        self.assertEqual((rc, "refused policy_matrix_agency" in se, out.exists()), (2, True, False))

    def test_review_errors_refuse_without_writing(self):
        package_path = self.pkg / "package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package.update(status="reviewed", reviewed_by=[], reviewed_at=None)
        package_path.write_text(json.dumps(package), encoding="utf-8")
        _, errors = law_package_check.check(self.pkg)
        self.assertIn("REVIEWER_MISSING", errors)
        self.assertIn("REVIEW_DATE_INVALID", errors)
        self.assertTrue(any(error.startswith("RULE_UNVERIFIED ") for error in errors))
        before = (sha(self.pol), sha(package_path))
        rc, so, se, out = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertEqual(so, "")
        self.assertIn("refused policy_matrix_package", se)
        self.assertFalse(out.exists())
        self.assertEqual((sha(self.pol), sha(package_path)), before)

    def test_valid_reviewed_package_is_accepted(self):
        package_path = self.pkg / "package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package.update(status="reviewed", reviewed_by=["Example Reviewer"], reviewed_at="2024-01-01")
        for rule in package["rules"]:
            rule["review"] = "verified"
        package_path.write_text(json.dumps(package), encoding="utf-8")
        _, errors = law_package_check.check(self.pkg)
        self.assertEqual(errors, [])
        rc, so, se, out = self.run_cli()
        self.assertEqual((rc, se), (0, ""))
        self.assertIn("package status reviewed", so)
        self.assertEqual(len(self.lines(out)), 6)

    def test_out_exists_refused_and_inputs_unchanged(self):
        before = (sha(self.pol), sha(self.pkg / "package.json"))
        rc, _, _, out = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual((sha(self.pol), sha(self.pkg / "package.json")), before)
        first = out.read_bytes()
        rc, _, se, _ = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("refused policy_matrix_out_exists", se)
        self.assertEqual(out.read_bytes(), first)

    def test_package_file_path_accepted(self):
        _, _, _, a = self.run_cli(package=self.pkg)
        _, _, _, b = self.run_cli(package=self.pkg / "package.json", out=self.d / "b.tsv")
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_cli_registered(self):
        self.assertEqual(cli.COMMANDS["agency-policy-matrix"], ("agency_policy_matrix", "main"))


if __name__ == "__main__":
    unittest.main()
