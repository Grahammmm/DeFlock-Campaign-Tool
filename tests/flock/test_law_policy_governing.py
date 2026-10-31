"""flock_law_policy governing STORE --on D: each agency's adopted version on one date + LAW rows in effect (RF1140-8)."""
import csv
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_law_policy.py"
FIXTURES = ROOT / "tests" / "fixtures" / "flock"
HEADER = ["policy_row_id", "agency", "policy_id", "version_label", "doc_sha256", "footer_version_date",
          "adoption_evidence", "adoption_sha256", "governs_from", "governs_to", "added_by", "added_at", "status"]
CITY, SAMPLE = "Sample City Police Department", "sample-city-police-department"
ADOPTED = "P-2745cbbfbcad"  # policy-sample.csv row 3: adopted, governs 2026-10-13..open
TAIL = " | research view, not a legal conclusion"


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


def row(label, start, end, agency=CITY, policy="435", evidence="Synthetic minutes adopt policy", footer="2025-12-01"):
    doc = hashlib.sha256(label.encode()).hexdigest()
    key = (re.sub(r"[^a-z0-9]+", "-", agency.lower()).strip("-"), policy, label, doc)
    rid = "P-" + hashlib.sha256("\x1f".join(key).encode()).hexdigest()[:12]
    return [rid, agency, policy, label, doc, footer, evidence, "f" * 64 if evidence != "unverified" else "",
            start, end, "tester", "2026-10-30", "unreviewed"]


def line(r):
    return f"governing | {r[1].lower().replace(' ', '-')} | {r[2]} | {r[3]} | governs {r[8]}..{r[9] or 'open'} | policy_row {r[0]}"


def counts(a, g, o, u):
    return f"agencies={a} governing={g} other_dates={o} unverified_only={u}{TAIL}"


class GoverningTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.store.mkdir()

    def agency(self, slug, rows):
        path = self.store / "agencies" / slug / "POLICY.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows([HEADER, *rows])

    def fixtures(self, edit=None):
        (self.store / "agencies" / SAMPLE).mkdir(parents=True)
        shutil.copyfile(FIXTURES / "policy-sample.csv", self.store / "agencies" / SAMPLE / "POLICY.csv")
        with open(FIXTURES / "law-sample.csv", newline="") as handle:
            table = list(csv.reader(handle))
        for n, field, value in edit or ():
            table[n][table[0].index(field)] = value
        with open(self.store / "LAW.csv", "w", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(table)

    def governing(self, on):
        got = run("governing", str(self.store), "--on", on)
        self.assertEqual((got[0], got[2]), (0, ""), got)
        return got[1].splitlines()

    def test_golden_fixtures_inside_adopted_window(self):
        self.fixtures()
        self.assertEqual(self.governing("2026-10-20"), [
            f"governing | {SAMPLE} | 435 | 2026-09-22 packet | governs 2026-10-13..open | policy_row {ADOPTED}",
            "law in effect on 2026-10-20: 3 of 3 rows", counts(1, 1, 0, 0)])

    def test_before_adoption_and_unverified_only_agency(self):
        self.fixtures()
        self.agency("beta-police-department", [row("b1", "", "", agency="Beta Police Department", evidence="unverified")])
        self.assertEqual(self.governing("2026-10-12"), [
            "governing | beta-police-department | none established (adoption unverified for all 1 rows)",
            f"governing | {SAMPLE} | none on 2026-10-12 (adopted versions exist for other dates)",
            "law in effect on 2026-10-12: 3 of 3 rows", counts(2, 0, 1, 1)])

    def test_window_edges_inclusive_and_open_end(self):
        closed, later = row("v1", "2026-01-01", "2026-03-31"), row("v1", "2026-05-01", "", policy="436")
        self.agency(SAMPLE, [closed, later])
        for on, want in (("2026-01-01", [closed]), ("2026-03-31", [closed]), ("2031-07-04", [later])):
            self.assertEqual(self.governing(on), [line(r) for r in want] + [f"law in effect on {on}: 0 of 0 rows", counts(1, 1, 0, 0)])
        self.assertEqual(self.governing("2026-04-01")[0], f"governing | {SAMPLE} | none on 2026-04-01 (adopted versions exist for other dates)")
        self.assertEqual(self.governing("2025-12-31")[-1], counts(1, 0, 1, 0))

    def test_two_policies_in_versions_order(self):
        a, b = row("pa", "2026-01-01", "", policy="435", footer="2026-02-01"), row("pb", "2026-01-01", "", policy="436", footer="2025-01-01")
        self.agency(SAMPLE, [a, b])
        self.assertEqual(self.governing("2026-06-01")[:2], [line(b), line(a)])  # footer date order, as versions() lists them
        ids = [x.split(" | ")[0] for x in run("versions", str(self.store), "--agency", SAMPLE, "--on", "2026-06-01")[1].splitlines()]
        self.assertEqual(ids, [b[0], a[0]])
        self.assertEqual(self.governing("2026-06-01")[-1], counts(1, 1, 0, 0))

    def test_law_edges(self):
        self.fixtures(edit=[(1, "effective_to", "2020-01-01")])  # rule 3 effective_from 2023-10-27 is after both dates
        self.assertEqual(self.governing("2020-01-01")[-2], "law in effect on 2020-01-01: 2 of 3 rows")
        self.assertEqual(self.governing("2020-01-02")[-2], "law in effect on 2020-01-02: 1 of 3 rows")

    def test_store_errors_print_nothing_on_stdout(self):
        self.fixtures()
        bad = row("b1", "2026-01-01", "", agency="Beta Police Department")
        bad[4] = "not-hex"
        self.agency("beta-police-department", [bad])
        code, out, err = run("governing", str(self.store), "--on", "2026-10-20")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("beta-police-department: row 2: doc_sha256 must be 64 lowercase hex", err.splitlines())
        self.agency("beta-police-department", [row("b1", "2026-01-01", "", agency="Beta Police Department"),
                                                row("b2", "2026-02-01", "", agency="Beta Police Department")])
        code, out, err = run("governing", str(self.store), "--on", "2026-10-20")
        self.assertEqual((code, out, err), (1, "", "beta-police-department: row 3: governs_from overlaps row 2 (same agency and policy_id)\n"))
        empty = self.store / "empty"
        empty.mkdir()
        self.assertEqual(run("governing", str(empty), "--on", "2026-10-20"), (1, "", "store: no LAW.csv or agencies/*/POLICY.csv found\n"))

    def test_usage_errors(self):
        self.fixtures()
        usage = run("--help")[1]
        self.assertIn("governing STORE --on YYYY-MM-DD", usage)
        s = str(self.store)
        for args in (["governing", s], ["governing", s, "--on"], ["governing", s, "--on", "2026-13-01"],
                     ["governing", s, "--at", "2026-10-20"], ["governing", s + "/LAW.csv", "--on", "2026-10-20"],
                     ["governing", s, "--on", "2026-10-20", "x"], ["governing", s + "/missing", "--on", "2026-10-20"]):
            self.assertEqual(run(*args), (2, "", usage), args)

    def test_no_leak_and_no_write(self):
        self.fixtures()
        def tree():
            return sorted((str(p), p.stat().st_mtime_ns) for p in self.store.rglob("*"))
        before = tree()
        for on in ("2026-10-12", "2026-10-20"):
            out = "\n".join(self.governing(on))
            self.assertNotIn("Council packet minutes", out)
            self.assertNotIn("tester", out)
            self.assertNotIn(str(self.store), out)
            self.assertIsNone(re.search("[0-9a-f]{64}", out))
        self.assertEqual(tree(), before)


if __name__ == "__main__":
    unittest.main()
