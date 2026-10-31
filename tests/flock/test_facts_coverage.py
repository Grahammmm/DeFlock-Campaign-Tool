"""flock_facts coverage: cited facts per posted INVENTORY document; withheld counted, never named (RF1140-4)."""
import csv
import os
from pathlib import Path
import tempfile
import unittest

from tests.flock.test_facts import ROOT, run, snapshot

FACTS = ROOT / "tests" / "fixtures" / "flock" / "compare-facts.csv"
INVENTORY = ROOT / "tests" / "fixtures" / "flock" / "report" / "INVENTORY.csv"
AGENCY, SLUG = "Sample City Police Department", "sample-city-police-department"
HEAD = ["source_id", "request", "doc_id", "title", "ext", "sha256", "analysis_only"]
V1 = ["agency", "request", "fact_type", "value", "source_sha256", "page", "verbatim_quote"]
POSTED, HIDDEN = "b" * 64, "c" * 64


def fact(sha, page, fact_type="vendor", value="Acme", quote="Quote."):
    return [AGENCY, "REQ-101", fact_type, value, sha, str(page), quote]


def doc(request, doc_id, sha, analysis_only="no", title="Doc.pdf"):
    return ["s#%s#%s" % (request, doc_id), request, doc_id, title, "pdf", sha, analysis_only]


class FactsCoverageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = self.dir / "store"

    def write(self, name, rows, encoding="utf-8"):
        path = self.dir / name
        with open(path, "w", encoding=encoding, newline="") as handle:
            csv.writer(handle, lineterminator="\r\n").writerows(rows)
        return str(path)

    def add_facts(self, *rows):
        src = self.write("facts.csv", [V1, *rows])
        code, out = run("import", src, "--store", str(self.store), "--added-by", "t", "--date", "2026-10-31")
        self.assertEqual(code, 0, out)

    def inventory(self, *rows, head=HEAD, encoding="utf-8", name="INVENTORY.csv"):
        return self.write(name, [head, *rows], encoding)

    def coverage(self, inventory, agency=AGENCY):
        return run("coverage", "--store", str(self.store), "--agency", agency, "--inventory", inventory)

    def test_golden_report_fixture(self):
        code, out = run("import", str(FACTS), "--store", str(self.store), "--added-by", "t", "--date", "2026-10-31")
        self.assertEqual(code, 0, out)
        self.add_facts(fact(HIDDEN, 1))
        code, out = self.coverage(str(INVENTORY))
        self.assertEqual((code, out.splitlines()), (0, [
            "coverage | REQ-101 | document 1001 | facts=6 pages=6 unreadable=0",
            "documents=2 posted=1 covered=1 uncovered=0 withheld=1 facts_on_withheld=1 orphan_facts=0"
            " | fact rows per document, not a review verdict"]))
        for secret in ("99999999", HIDDEN, POSTED, "hidden log", "Sample retention", "Vigilant", "F-"):
            self.assertNotIn(secret, out)

    def test_withheld_never_named(self):
        self.add_facts(fact(POSTED, 1), fact(HIDDEN, 2))
        inv = self.inventory(doc("REQ-101", "1001", POSTED), doc("REQ-SECRET", "99999999", HIDDEN, "yes", "Hidden log.xlsx"))
        code, out = self.coverage(inv)
        self.assertEqual(code, 0)
        for secret in ("99999999", HIDDEN, "Hidden log", "REQ-SECRET", "Doc.pdf", POSTED):
            self.assertNotIn(secret, out)
        self.assertIn("withheld=1 facts_on_withheld=1 ", out)

    def test_uncovered_document_is_counted_and_exits_zero(self):
        self.add_facts(fact(POSTED, 1))
        code, out = self.coverage(self.inventory(doc("REQ-101", "1001", POSTED), doc("", "1002", "d" * 64)))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[:2], ["coverage | - | document 1002 | facts=0 pages=0 unreadable=0",
                                                "coverage | REQ-101 | document 1001 | facts=1 pages=1 unreadable=0"])
        self.assertIn("documents=2 posted=2 covered=1 uncovered=1 withheld=0 ", out)

    def test_orphan_pages_and_unreadable(self):
        sha = "e" * 64
        self.add_facts(fact(sha, 3), fact(sha, 3, "retention_days", "365"), fact(sha, 4, "x", "UNREADABLE", ""),
                       fact("f" * 64, 1))
        code, out = self.coverage(self.inventory(doc("REQ-2", "7", sha)))
        self.assertEqual((code, out.splitlines()), (0, [
            "coverage | REQ-2 | document 7 | facts=3 pages=2 unreadable=1",
            "documents=1 posted=1 covered=1 uncovered=0 withheld=0 facts_on_withheld=0 orphan_facts=1"
            " | fact rows per document, not a review verdict"]))

    def test_withheld_fails_closed(self):
        self.add_facts(fact(POSTED, 1))
        for rows in ([doc("R", "1", POSTED, "")], [doc("R", "1", POSTED, "no"), doc("R", "2", POSTED, "yes")],
                     [doc("R", "2", POSTED, "yes"), doc("R", "1", POSTED, "no")],
                     [doc("R", "1", POSTED, "No")], [doc("R", "1", POSTED, " no")], [doc("R", "1", POSTED, "false")]):
            with self.subTest(rows=rows):
                code, out = self.coverage(self.inventory(*rows))
                self.assertEqual(code, 0)
                self.assertNotIn("coverage |", out)
                self.assertIn("posted=0 covered=0 uncovered=0 withheld=1 facts_on_withheld=1 ", out)

    def test_inventory_problems_exit_one(self):
        self.add_facts(fact(POSTED, 1))
        good = doc("R", "1", POSTED)
        cases = [(self.inventory(good[:6], head=HEAD[:6], name="a.csv"), "inventory: header lacks analysis_only"),
                 (self.inventory(good, good[:5], name="b.csv"), "inventory: row 3: columns have the wrong count"),
                 (self.write("c.csv", []), "inventory: header lacks request, doc_id, sha256, analysis_only")]
        for inv, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.coverage(inv), (1, expected + "\n"))
        code, out = self.coverage(self.inventory(good, encoding="utf-8-sig"))
        self.assertEqual(code, 0)
        self.assertIn("coverage | R | document 1 | facts=1 ", out)
        self.assertEqual(self.coverage(str(self.dir / "absent.csv")), (1, "io: FileNotFoundError\n"))

    def test_facts_problems_exit_one(self):
        inv = self.inventory(doc("R", "1", POSTED))
        self.assertEqual(self.coverage(inv), (1, "facts: %s has no FACTS.csv\n" % SLUG))
        (self.store / "agencies" / SLUG).mkdir(parents=True)
        self.write("v1.csv", [V1, fact(POSTED, 1)])
        os.replace(self.dir / "v1.csv", self.store / "agencies" / SLUG / "FACTS.csv")
        self.assertEqual(self.coverage(inv), (1, "facts: 1 errors (run --check)\n"))

    def test_usage_errors_and_agency_slug(self):
        self.add_facts(fact(POSTED, 1))
        inv = self.inventory(doc("R", "1", POSTED))
        base = ["--store", str(self.store), "--agency", AGENCY, "--inventory", inv]
        for argv in (base[:4], ["extra"] + base, base[2:], ["--check", str(self.store)] + base, base[:3] + ["!!"] + base[4:]):
            with self.subTest(argv=argv):
                self.assertEqual(run("coverage", *argv), (2, ""))
        self.assertEqual(self.coverage(inv, "  sample city POLICE department ")[0], 0)

    def test_writes_nothing(self):
        self.add_facts(fact(POSTED, 1), fact("f" * 64, 2))
        inv = self.inventory(doc("R", "1", POSTED), doc("R", "2", HIDDEN, "yes"))
        before, mtime = snapshot(self.dir), os.stat(inv).st_mtime_ns
        self.assertEqual(self.coverage(inv)[0], 0)
        self.assertEqual((snapshot(self.dir), os.stat(inv).st_mtime_ns), (before, mtime))

    def test_help_shows_coverage_form(self):
        code, out = run("--help")
        self.assertEqual(code, 0)
        self.assertIn("coverage --store DIR --agency NAME --inventory CSV", " ".join(out.split()))


if __name__ == "__main__":
    unittest.main()
