import csv
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_law_policy.py"
FIXTURES = ROOT / "tests" / "fixtures" / "flock"
HEADER = ["policy_row_id", "agency", "policy_id", "version_label", "doc_sha256", "footer_version_date",
          "adoption_evidence", "adoption_sha256", "governs_from", "governs_to", "added_by", "added_at", "status"]
CITY = "Sample City Police Department"


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


def row(label, start, end, agency=CITY, policy="435", evidence="Council minutes adopt policy"):
    doc = hashlib.sha256(label.encode()).hexdigest()
    key = (re.sub(r"[^a-z0-9]+", "-", agency.lower()).strip("-"), policy, label, doc)
    rid = "P-" + hashlib.sha256("\x1f".join(key).encode()).hexdigest()[:12]
    return [rid, agency, policy, label, doc, "2025-12-01", evidence, "f" * 64 if evidence != "unverified" else "",
            start, end, "tester", "2026-10-30", "unreviewed"]


def msg(n, m):
    return f"row {n}: governs_from overlaps row {m} (same agency and policy_id)"


class PolicyOverlapTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def write(self, rows, name="POLICY.csv"):
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows([HEADER, *rows])
        return str(path)

    def check(self, rows):
        code, out, _ = run("--check", self.write(rows))
        return code, out.splitlines()

    def test_fixture_stays_clean(self):
        self.assertEqual(run("--check", str(FIXTURES / "policy-sample.csv"))[:2], (0, "law=0 policy=2 errors=0\n"))

    def test_open_window_then_later_version_refused_and_versions_on_refuses(self):
        path = self.write([row("v1", "2026-01-01", ""), row("v2", "2026-06-01", "")])
        code, out, _ = run("--check", path)
        self.assertEqual((code, out), (1, f"{msg(3, 2)}\nlaw=0 policy=2 errors=1\n"))
        code, out, err = run("versions", path, "--agency", "sample-city-police-department", "--on", "2026-07-01")
        self.assertEqual((code, out, err), (1, "", msg(3, 2) + "\n"))

    def test_later_row_named_even_when_listed_first(self):
        self.assertEqual(self.check([row("v2", "2026-06-01", ""), row("v1", "2026-01-01", "")]), (1, [msg(2, 3), "law=0 policy=2 errors=1"]))

    def test_same_start_day_names_the_higher_row(self):
        self.assertEqual(self.check([row("v1", "2026-06-01", "2026-06-01"), row("v2", "2026-06-01", "")])[1][0], msg(3, 2))

    def test_touching_boundary_is_inclusive(self):
        self.assertEqual(self.check([row("v1", "2026-01-01", "2026-06-01"), row("v2", "2026-06-01", "")]),
                         (1, [msg(3, 2), "law=0 policy=2 errors=1"]))
        self.assertEqual(self.check([row("v1", "2026-01-01", "2026-05-31"), row("v2", "2026-06-01", "")]),
                         (0, ["law=0 policy=2 errors=0"]))

    def test_closed_window_after_open_window_refused(self):
        self.assertEqual(self.check([row("v1", "2026-01-01", ""), row("v2", "2027-01-01", "2027-02-01")])[1][0], msg(3, 2))

    def test_different_policy_id_or_agency_is_clean(self):
        self.assertEqual(self.check([row("v1", "2026-01-01", ""), row("v2", "2026-06-01", "", policy="436")])[0], 0)
        code, out, _ = run("--check", self.write([row("v1", "2026-01-01", ""), row("v2", "2026-06-01", "", agency="Other City PD")]))
        self.assertEqual((code, out), (0, "law=0 policy=2 errors=0\n"))

    def test_agency_spelling_does_not_split_the_group(self):
        lines = self.check([row("v1", "2026-01-01", ""), row("v2", "2026-06-01", "", agency="SAMPLE CITY police department")])[1]
        self.assertEqual(lines[0], msg(3, 2))

    def test_unverified_row_inside_adopted_window_is_clean(self):
        self.assertEqual(self.check([row("v1", "2026-01-01", ""), row("v0", "", "", evidence="unverified")]),
                         (0, ["law=0 policy=2 errors=0"]))

    def test_row_with_its_own_error_is_left_out_of_the_overlap_pass(self):
        code, lines = self.check([row("v1", "2026-01-01", ""), row("v2", "2026-13-01", "")])
        self.assertEqual((code, lines), (1, ["row 3: governs_from must be YYYY-MM-DD", "law=0 policy=2 errors=1"]))
        bad_id = row("v2", "2026-06-01", "")
        bad_id[0] = "P-000000000000"
        self.assertEqual(self.check([row("v1", "2026-01-01", ""), bad_id])[1],
                         ["row 3: policy_row_id is not the derived id", "law=0 policy=2 errors=1"])

    def test_duplicate_row_reported_once(self):
        code, lines = self.check([row("v1", "2026-01-01", ""), row("v1", "2026-01-01", "")])
        self.assertEqual((code, lines), (1, ["row 3: policy_row_id duplicates an earlier row", "law=0 policy=2 errors=1"]))

    def test_three_versions_inside_one_window_give_two_lines_in_row_order(self):
        rows = [row("c", "2026-08-01", "2026-09-01"), row("a", "2026-01-01", ""), row("b", "2026-03-01", "2026-04-01")]
        self.assertEqual(self.check(rows), (1, [msg(2, 3), msg(4, 3), "law=0 policy=3 errors=2"]))

    def test_store_mode_prefixes_only_the_overlapping_folder(self):
        self.write([row("v1", "2026-01-01", ""), row("v2", "2026-06-01", "")],
                   "store/agencies/sample-city-police-department/POLICY.csv")
        self.write([row("v1", "2026-01-01", "2026-05-31", agency="Other City PD"),
                    row("v2", "2026-06-01", "", agency="Other City PD")], "store/agencies/other-city-pd/POLICY.csv")
        code, out, _ = run("--check", str(self.tmp / "store"))
        self.assertEqual((code, out), (1, f"sample-city-police-department: {msg(3, 2)}\nlaw=0 policy=4 errors=1\n"))
        code, out, err = run("versions", str(self.tmp / "store"), "--agency", "other-city-pd", "--on", "2026-07-01")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("sample-city-police-department: " + msg(3, 2), err)


if __name__ == "__main__":
    unittest.main()
