"""flock_facts --check STORE: policy facts cite their agency's POLICY doc_sha256, law facts a LAW text_sha256 (DF1537-14)."""
import csv
import importlib.util
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest

from tests.flock.test_compare import AGENCY, FIXTURES, TOOLS, run

_spec = importlib.util.spec_from_file_location("flock_facts_links_under_test", TOOLS / "flock_facts.py")
facts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(facts)
_spec = importlib.util.spec_from_file_location("flock_law_policy_links_under_test", TOOLS / "flock_law_policy.py")
law_policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(law_policy)

POLICY_DOC, OTHER_DOC, LAW_TEXT, RECORD_DOC = "a" * 64, "b" * 64, "d" * 64, "c" * 64  # synthetic hashes only
QUOTE = "Sentinel quote nobody may print."
OTHER = "zeta-city-police-department"
NOT_POLICY = "row %d: policy fact source is not a POLICY version of this agency"
NOT_LAW = "row %d: law fact source is not a LAW row"
POLICY_BAD = "POLICY.csv has errors (run flock_law_policy.py --check)"
LAW_BAD = "LAW.csv has errors (run flock_law_policy.py --check)"


def fact(agency, kind, source, page):
    row = dict(zip(facts.V1, [agency, "REQ-1" if kind == "record" else "", "retention", "30 days", source, str(page), QUOTE]))
    row.update(kind=kind, status="unreviewed", effective_from="", effective_to="", added_by="tester", added_at="2026-10-31")
    row["fact_id"] = facts.fact_id(row)
    return [row[k] for k in facts.V2]


def write(path, head, rows, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(([head] if head else []) + rows)
    return path


def other_policy(doc):
    row = dict(zip(law_policy.POLICY_HEADER, ["", "Zeta City Police Department", "435", "v1", doc, "2026-01-02",
                                              "unverified", "", "", "", "tester", "2026-10-31", "unreviewed"]))
    row["policy_row_id"] = law_policy.derived_id(row)
    return [row[k] for k in law_policy.POLICY_HEADER]


class FactsLinksTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.home = self.store / "agencies" / AGENCY
        self.home.mkdir(parents=True)
        shutil.copy(FIXTURES / "law-sample.csv", self.store / "LAW.csv")
        shutil.copy(FIXTURES / "policy-sample.csv", self.home / "POLICY.csv")
        write(self.store / "agencies" / OTHER / "POLICY.csv", law_policy.POLICY_HEADER, [other_policy(OTHER_DOC)])

    def facts_file(self, slug, rows):
        agency = "Sample City Police Department" if slug == AGENCY else slug.replace("-", " ").title()
        return write(self.store / "agencies" / slug / "FACTS.csv", facts.V2,
                     [fact(agency, kind, source, n) for n, (kind, source) in enumerate(rows, 1)])

    def check(self, target=None):
        code, out = run("flock_facts.py", "--check", str(target or self.store))
        self.assertNotIn(QUOTE, out)
        self.assertIsNone(re.search(r"[0-9a-f]{64}", out), "a hash was printed")
        return code, out.split("\n")[:-1] if out.strip() else []

    def test_matching_policy_law_and_record_citations_are_clean(self):
        self.facts_file(AGENCY, [("policy", POLICY_DOC), ("policy", "e" * 64), ("law", LAW_TEXT), ("record", RECORD_DOC),
                                 ("public", RECORD_DOC)])
        self.facts_file(OTHER, [("policy", OTHER_DOC), ("law", LAW_TEXT)])
        self.assertEqual(self.check(), (0, []))

    def test_other_agency_document_and_kind_swaps_are_named(self):
        self.facts_file(AGENCY, [("policy", OTHER_DOC), ("policy", LAW_TEXT), ("law", POLICY_DOC), ("policy", POLICY_DOC)])
        self.facts_file(OTHER, [("policy", POLICY_DOC), ("law", OTHER_DOC)])
        self.assertEqual(self.check(), (1, ["file 1: " + NOT_POLICY % 2, "file 1: " + NOT_POLICY % 3,
                                            "file 1: " + NOT_LAW % 4, "file 2: " + NOT_POLICY % 2,
                                            "file 2: " + NOT_LAW % 3]))

    def test_missing_policy_or_law_file_is_named_per_row(self):
        os.unlink(self.store / "LAW.csv")
        os.unlink(self.store / "agencies" / OTHER / "POLICY.csv")
        self.facts_file(AGENCY, [("policy", POLICY_DOC), ("law", LAW_TEXT), ("record", RECORD_DOC)])
        self.facts_file(OTHER, [("policy", OTHER_DOC), ("policy", OTHER_DOC.replace("b", "9", 1)), ("record", OTHER_DOC)])
        self.assertEqual(self.check(), (1, ["file 1: row 3: law fact but store has no LAW.csv",
                                            "file 2: row 2: policy fact but agency has no POLICY.csv",
                                            "file 2: row 3: policy fact but agency has no POLICY.csv"]))

    def test_invalid_policy_named_once_per_agency_and_law_once_per_run(self):
        table = list(csv.reader((self.home / "POLICY.csv").read_text().splitlines()))
        table[2][law_policy.POLICY_HEADER.index("footer_version_date")] = "not-a-date"
        write(self.home / "POLICY.csv", None, table)
        law_row = list(csv.reader((self.store / "LAW.csv").read_text().splitlines()))[1]
        write(self.store / "LAW.csv", None, [["ca-civ-0000-none"] + law_row[1:]], mode="a")
        self.facts_file(AGENCY, [("policy", POLICY_DOC), ("policy", OTHER_DOC), ("law", LAW_TEXT), ("law", POLICY_DOC)])
        self.facts_file(OTHER, [("policy", OTHER_DOC), ("policy", POLICY_DOC), ("law", POLICY_DOC)])
        self.assertEqual(self.check(), (1, ["file 1: " + POLICY_BAD, "file 1: " + LAW_BAD, "file 2: " + NOT_POLICY % 3]))

    def test_linked_or_non_file_policy_and_law_are_refused_as_one_line(self):
        target = self.store.parent / "elsewhere.csv"
        shutil.move(self.home / "POLICY.csv", target)
        os.symlink(target, self.home / "POLICY.csv")
        os.unlink(self.store / "LAW.csv")
        (self.store / "LAW.csv").mkdir()
        self.facts_file(AGENCY, [("policy", POLICY_DOC), ("policy", OTHER_DOC), ("law", LAW_TEXT)])
        self.facts_file(OTHER, [("law", LAW_TEXT)])
        self.assertEqual(self.check(), (1, ["file 1: POLICY.csv is a link or not a file; refusing",
                                            "file 1: LAW.csv is a link or not a file; refusing"]))

    def test_malformed_source_is_reported_once_not_cascaded(self):
        self.facts_file(AGENCY, [("policy", "A" * 64), ("law", "")])
        code, out = self.check()
        self.assertEqual((code, len(out)), (1, 2))
        self.assertTrue(all("source_sha256 must be 64 lowercase hex" in line for line in out))

    def test_file_mode_on_same_rows_has_no_link_errors(self):
        path = self.facts_file(AGENCY, [("policy", OTHER_DOC), ("law", POLICY_DOC)])
        os.unlink(self.store / "LAW.csv")
        self.assertEqual(self.check(path), (0, []))
        self.assertEqual(facts.check_file(path, AGENCY), [])

    def test_compare_check_and_status_keep_their_scope(self):
        self.assertEqual(run("flock_facts.py", "import", str(FIXTURES / "compare-facts.csv"), "--store", str(self.store),
                             "--added-by", "tester", "--date", "2026-10-30")[0], 0)
        write(self.home / "FACTS.csv", None, [fact("Sample City Police Department", "policy", OTHER_DOC, 99)], mode="a")
        shutil.rmtree(self.store / "agencies" / OTHER)
        self.assertEqual(self.check(), (1, ["file 1: " + NOT_POLICY % 8]))
        status = run("flock_facts.py", "status", "--store", str(self.store))
        self.assertEqual(status[0], 0)
        self.assertNotIn("POLICY", status[1])
        self.assertEqual(run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", AGENCY, "--date",
                             "2026-10-30", "--provider", "anthropic", "--model", "claude-opus-5-5", "--applicability",
                             "Sample city police; local ALPR policy applies")[0], 0)
        run_dir = self.home / "runs" / "2026-10-30"
        shutil.copy(FIXTURES / "findings-sample.csv", run_dir / "FINDINGS.csv")
        self.assertEqual(run("flock_compare.py", "--check", str(run_dir)), (0, "findings=7 errors=0\n"))
        os.unlink(self.home / "POLICY.csv")
        self.assertEqual(run("flock_facts.py", "status", "--store", str(self.store)), status)


if __name__ == "__main__":
    unittest.main()
