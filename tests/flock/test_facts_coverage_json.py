"""flock_facts coverage --json: one flock-facts-coverage-v1 document; never a hash, value or quote (RF0740-11)."""
import json
import os
import re
import subprocess
import sys
import unittest

from tests.flock import test_facts_coverage as cov
from tests.flock.test_facts import TOOL, run
from tests.flock.test_facts_coverage import AGENCY, HIDDEN, POSTED, SLUG, V1, doc, fact

UNCOVERED, ORPHAN = "d" * 64, "f" * 64
# Golden: tools/flock_facts.py on origin/main 2ab64643 run against fixture() without --json.
MAIN_TEXT = (
    "coverage | - | document 1002 | facts=0 pages=0 unreadable=0\n"
    "coverage | REQ-101 | document 1001 | facts=3 pages=2 unreadable=1\n"
    "documents=3 posted=2 covered=1 uncovered=1 withheld=1 facts_on_withheld=2 orphan_facts=1"
    " | fact rows per document, not a review verdict\n")
SECRETS = (POSTED, HIDDEN, UNCOVERED, ORPHAN, "Vendor Secret", "Quote secret.", "Hidden log", "REQ-SECRET", "99999999",
           "Doc.pdf", "F-")


class FactsCoverageJsonTest(unittest.TestCase):
    # Reuse the RF1140-4 fixture helpers without re-running that module's tests.
    setUp, write, add_facts, inventory = (cov.FactsCoverageTest.setUp, cov.FactsCoverageTest.write,
                                          cov.FactsCoverageTest.add_facts, cov.FactsCoverageTest.inventory)

    def fixture(self):
        self.add_facts(fact(POSTED, 1, value="Vendor Secret", quote="Quote secret."), fact(POSTED, 1, "retention_days", "41"),
                       fact(POSTED, 2, "x", "UNREADABLE", ""), fact(HIDDEN, 3, value="Vendor Secret"), fact(HIDDEN, 4),
                       fact(ORPHAN, 1))
        return self.inventory(doc("REQ-101", "1001", POSTED), doc("", "1002", UNCOVERED),
                              doc("REQ-SECRET", "99999999", HIDDEN, "yes", "Hidden log.xlsx"))

    def raw(self, inv, *extra):
        argv = ["coverage", "--store", str(self.store), "--agency", AGENCY, "--inventory", inv, *extra]
        return subprocess.run([sys.executable, "-B", str(TOOL), *argv], capture_output=True, text=True, check=False)

    def test_json_document_matches_text(self):
        inv = self.fixture()
        done = self.raw(inv, "--json")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        data = json.loads(done.stdout)
        self.assertEqual(data, {
            "schema": "flock-facts-coverage-v1", "agency": SLUG,
            "documents": [{"request": "-", "doc_id": "1002", "facts": 0, "pages": 0, "unreadable": 0},
                          {"request": "REQ-101", "doc_id": "1001", "facts": 3, "pages": 2, "unreadable": 1}],
            "totals": {"documents": 3, "posted": 2, "covered": 1, "uncovered": 1, "withheld": 1, "facts_on_withheld": 2,
                       "orphan_facts": 1},
            "note": "fact rows per document, not a review verdict"})
        text = self.raw(inv).stdout.splitlines()
        self.assertEqual(text[-1], " ".join("%s=%d" % kv for kv in data["totals"].items()) + " | " + data["note"])
        self.assertEqual(text[:-1], ["coverage | %s | document %s | facts=%d pages=%d unreadable=%d" % tuple(d.values())
                                     for d in data["documents"]])

    def test_withheld_absent_and_no_hash_value_or_quote(self):
        done = self.raw(self.fixture(), "--json")
        self.assertEqual(done.returncode, 0)
        self.assertNotIn("99999999", [d["doc_id"] for d in json.loads(done.stdout)["documents"]])
        self.assertIsNone(re.search(r"[0-9a-fA-F]{64}", done.stdout))
        for secret in SECRETS:
            self.assertNotIn(secret, done.stdout)

    def test_all_withheld_gives_empty_documents(self):
        self.add_facts(fact(HIDDEN, 1))
        done = self.raw(self.inventory(doc("R", "1", HIDDEN, "No")), "--json")
        data = json.loads(done.stdout)
        self.assertEqual((done.returncode, data["documents"], data["totals"]["withheld"], data["totals"]["facts_on_withheld"]),
                         (0, [], 1, 1))

    def test_refusals_keep_text_and_print_no_json(self):
        inv = self.inventory(doc("R", "1", POSTED))
        done = self.raw(inv, "--json")
        self.assertEqual((done.returncode, done.stdout), (1, "facts: %s has no FACTS.csv\n" % SLUG))
        (self.store / "agencies" / SLUG).mkdir(parents=True)
        self.write("v1.csv", [V1, fact(POSTED, 1)])
        os.replace(self.dir / "v1.csv", self.store / "agencies" / SLUG / "FACTS.csv")
        done = self.raw(inv, "--json")
        self.assertEqual((done.returncode, done.stdout), (1, "facts: 1 errors (run --check)\n"))
        os.remove(self.store / "agencies" / SLUG / "FACTS.csv")
        self.add_facts(fact(POSTED, 1))
        good = doc("R", "1", POSTED)
        for inv_, expected in [
                (self.inventory(good[:6], head=["source_id", "request", "doc_id", "title", "ext", "sha256"], name="a.csv"),
                 "inventory: header lacks analysis_only"),
                (self.inventory(good, good[:5], name="b.csv"), "inventory: row 3: columns have the wrong count"),
                (str(self.dir / "absent.csv"), "io: FileNotFoundError")]:
            with self.subTest(expected=expected):
                done = self.raw(inv_, "--json")
                self.assertEqual((done.returncode, done.stdout), (1, expected + "\n"))
                self.assertNotIn("{", done.stdout)

    def test_text_without_json_is_byte_identical_to_main(self):
        done = self.raw(self.fixture())
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, MAIN_TEXT, ""))

    def test_json_with_other_commands_is_a_usage_error(self):
        store = str(self.store)
        for argv in (["stale", "--store", store, "--date", "2026-11-01"], ["--check", store],
                     ["import", store, "--store", store, "--added-by", "x"], ["reviews", "--store", store, "--agency", "a"]):
            done = subprocess.run([sys.executable, "-B", str(TOOL), *argv, "--json"], capture_output=True, text=True, check=False)
            self.assertEqual((done.returncode, done.stdout), (2, ""), argv)
            self.assertIn("--json belongs to status or coverage", done.stderr)

    def test_json_coverage_usage_errors_stay_usage_errors(self):
        self.assertEqual(run("coverage", "--store", str(self.store), "--agency", AGENCY, "--json"), (2, ""))


if __name__ == "__main__":
    unittest.main()
