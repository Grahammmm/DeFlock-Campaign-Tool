"""flock_law_policy gaps STORE --on D: ungoverned spans, closed and unestablished policies per agency + policy_id (RF1220-1)."""
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
HEADER = ["policy_row_id", "agency", "policy_id", "version_label", "doc_sha256", "footer_version_date",
          "adoption_evidence", "adoption_sha256", "governs_from", "governs_to", "added_by", "added_at", "status"]
CITY, SLUG = "Sample City Police Department", "sample-city-police-department"
EVIDENCE = "Synthetic minutes adopt policy"


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


def row(label, start, end, policy="P", agency=CITY, evidence=EVIDENCE):
    doc = hashlib.sha256(label.encode()).hexdigest()
    key = (re.sub(r"[^a-z0-9]+", "-", agency.lower()).strip("-"), policy, label, doc)
    rid = "P-" + hashlib.sha256("\x1f".join(key).encode()).hexdigest()[:12]
    return [rid, agency, policy, label, doc, "2019-12-01", evidence, "f" * 64 if evidence != "unverified" else "",
            start, end, "tester", "2026-10-30", "unreviewed"]


def counts(a=1, p=1, g=0, c=0, u=0):
    return f"agencies={a} policies={p} gaps={g} closed={c} unestablished={u}"


class GapsTest(unittest.TestCase):
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

    def snapshot(self):
        return sorted((str(p), p.read_bytes() if p.is_file() else b"", p.stat().st_mtime_ns) for p in self.store.rglob("*"))

    def gaps(self, on, *extra, store=None):  # (exit, stdout lines, stderr); the store is never written
        before = self.snapshot()
        code, out, err = run("gaps", str(store or self.store), "--on", on, *extra)
        self.assertEqual(self.snapshot(), before, "store changed")
        return code, out.splitlines(), err

    def test_golden_gap_rows_out_of_order(self):
        v1, v2 = row("v1", "2020-01-01", "2021-06-30"), row("v2", "2021-08-01", "")
        self.agency(SLUG, [v2, v1])
        code, out, err = self.gaps("2024-01-01")
        self.assertEqual((code, out, err), (3, [f"{SLUG} | P | gap 2021-07-01..2021-07-31", counts(g=1)], ""))
        text = "\n".join(out)
        for leak in (v1[4], v2[4], "f" * 64, EVIDENCE, str(self.store), "POLICY.csv"):
            self.assertNotIn(leak, text)

    def test_adjacent_windows_are_not_a_gap(self):
        self.agency(SLUG, [row("v1", "2020-01-01", "2021-06-30"), row("v2", "2021-07-01", "")])
        self.assertEqual(self.gaps("2024-01-01"), (0, [counts()], ""))

    def test_month_and_leap_boundary(self):
        self.agency(SLUG, [row("v1", "2023-01-01", "2024-02-28"), row("v2", "2024-03-01", "")])
        self.assertEqual(self.gaps("2024-06-01")[:2], (3, [f"{SLUG} | P | gap 2024-02-29..2024-02-29", counts(g=1)]))
        self.agency(SLUG, [row("v1", "2022-01-01", "2023-02-28"), row("v2", "2023-03-01", "")])  # not a leap year
        self.assertEqual(self.gaps("2024-06-01")[:2], (0, [counts()]))

    def test_closed_policy_ends_inclusive(self):
        self.agency(SLUG, [row("v1", "2020-01-01", "2023-12-31")])
        closed = f"{SLUG} | P | closed 2023-12-31 (nothing governs on 2024-01-01)"
        self.assertEqual(self.gaps("2024-01-01"), (3, [closed, counts(c=1)], ""))
        self.assertEqual(self.gaps("2023-12-31"), (0, [counts()], ""))
        self.assertEqual(self.gaps("2019-06-01"), (0, [counts()], ""))  # before the first window: no guess

    def test_gap_then_closed_in_date_order_and_on_inside_gap(self):
        self.agency(SLUG, [row("v2", "2020-08-01", "2021-12-31"), row("v1", "2020-01-01", "2020-06-30")])
        gap = f"{SLUG} | P | gap 2020-07-01..2020-07-31"
        self.assertEqual(self.gaps("2022-01-01")[:2], (3, [gap, f"{SLUG} | P | closed 2021-12-31 (nothing governs on 2022-01-01)",
                                                         counts(g=1, c=1)]))
        self.assertEqual(self.gaps("2020-07-15")[:2], (3, [gap, counts(g=1)]))  # a later window exists: not closed

    def test_unverified_only_and_unverified_inside_gap(self):
        self.agency(SLUG, [row("q1", "", "", "Q", evidence="unverified"), row("q2", "", "", "Q", evidence="unverified")])
        self.assertEqual(self.gaps("2024-01-01")[:2], (3, [f"{SLUG} | Q | not established (2 unverified)", counts(u=1)]))
        self.agency(SLUG, [row("v1", "2020-01-01", "2020-12-31"), row("mid", "", "", evidence="unverified"),
                           row("v2", "2021-03-01", "")])
        self.assertEqual(self.gaps("2024-01-01")[:2], (3, [f"{SLUG} | P | gap 2021-01-01..2021-02-28", counts(g=1)]))

    def test_several_agencies_and_policies_sorted(self):
        self.agency("beta-police-department", [
            row("bb", "", "", "B", "Beta Police Department", "unverified"),
            row("ba1", "2020-01-01", "2020-01-31", "A", "Beta Police Department"),
            row("ba2", "2020-03-01", "", "A", "Beta Police Department")])
        self.agency("alpha-sheriff", [row("z", "2020-01-01", "2020-12-31", "Z", "Alpha Sheriff"),
                                      row("ok", "2020-01-01", "", "C", "Alpha Sheriff")])
        self.assertEqual(self.gaps("2024-01-01"), (3, [
            "alpha-sheriff | Z | closed 2020-12-31 (nothing governs on 2024-01-01)",
            "beta-police-department | A | gap 2020-02-01..2020-02-29",
            "beta-police-department | B | not established (1 unverified)",
            counts(a=2, p=4, g=1, c=1, u=1)], ""))

    def test_load_errors_print_nothing_on_stdout(self):
        self.agency(SLUG, [row("v1", "2020-01-01", "2021-06-30"), row("v2", "2021-06-30", "")])
        code, out, err = self.gaps("2024-01-01")
        self.assertEqual((code, out), (1, []))
        self.assertIn(f"{SLUG}: row 3: governs_from overlaps row 2", err)
        bad = row("v1", "2020-01-01", "2021-06-30")
        bad[4] = "not-a-hash"
        self.agency(SLUG, [bad, row("v2", "2021-08-01", "")])
        code, out, err = self.gaps("2024-01-01")
        self.assertEqual((code, out), (1, []))
        self.assertIn(f"{SLUG}: row 2: doc_sha256", err)
        self.agency(SLUG, [row("v1", "2020-01-01", "2020-12-31"), row("v1", "2020-01-01", "2020-12-31")])  # duplicate
        code, out, err = self.gaps("2024-01-01")
        self.assertEqual((code, out), (1, []))
        self.assertIn("duplicates an earlier row", err)

    def test_empty_store_is_the_store_error(self):
        self.assertEqual(self.gaps("2024-01-01"), (1, [], "store: no LAW.csv or agencies/*/POLICY.csv found\n"))

    def test_usage_errors(self):
        self.agency(SLUG, [row("v1", "2020-01-01", "")])
        file = self.store / "agencies" / SLUG / "POLICY.csv"
        for args in (["gaps", str(file), "--on", "2024-01-01"], ["gaps", str(self.store), "--on", "2024-02-30"],
                     ["gaps", str(self.store), "--on", "24-01-01"], ["gaps", str(self.store)],
                     ["gaps", str(self.store), "--at", "2024-01-01"], ["gaps", str(self.store), "--on", "2024-01-01", "x"],
                     ["gaps", str(self.store / "missing"), "--on", "2024-01-01"]):
            before = self.snapshot()
            code, out, err = run(*args)
            self.assertEqual((code, out), (2, ""), args)
            self.assertTrue(err.startswith("usage:"), args)
            self.assertEqual(self.snapshot(), before)

    def test_open_end_next_to_year_9999(self):
        self.agency(SLUG, [row("v1", "2020-01-01", "9999-12-30"), row("v2", "9999-12-31", "")])
        self.assertEqual(self.gaps("2024-01-01"), (0, [counts()], ""))
        self.agency(SLUG, [row("v1", "2020-01-01", "9999-12-30")])
        self.assertEqual(self.gaps("9999-12-31")[:2], (3, [f"{SLUG} | P | closed 9999-12-30 (nothing governs on 9999-12-31)",
                                                          counts(c=1)]))

    def test_help_lists_gaps(self):
        code, out, _ = run("--help")
        self.assertEqual(code, 0)
        self.assertIn("gaps STORE --on YYYY-MM-DD", out)


if __name__ == "__main__":
    unittest.main()
