"""flock_compare review state (DF1537-02): --check refuses a finding citing a disputed fact; --summary counts findings
citing unreviewed facts per topic. Synthetic store and run under a temp dir; never value or quote text in output."""
import csv
from pathlib import Path
import shutil
import tempfile
import unittest

from tests.flock.test_compare import AGENCY, FIXTURES, run

SEARCH, OUTSIDE, RETENTION, TRAINING = "F-090c3d39b556", "F-76dbe8a9b4ac", "F-53baa6f20643", "F-a71d059d7a51"
CLEAN = (0, "findings=7 errors=0\n")
DISPUTED = "fact_ids cites a disputed fact (review it again or drop it)"
QUOTE = "quote_fact_id is a disputed fact"
SENTINEL_VALUE, SENTINEL_QUOTE = "SENTINEL-VALUE-1537", "SENTINEL-QUOTE-1537 is on page nine."


def table(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def write(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)


class CompareReviewStateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp, self.store = Path(tmp.name), Path(tmp.name) / "store"
        extra = self.tmp / "facts.csv"  # the compare fixture plus one uncited sentinel fact
        write(extra, table(FIXTURES / "compare-facts.csv") + [["Sample City Police Department", "REQ-101", "vendor_contract",
                                                                SENTINEL_VALUE, "b" * 64, "9", SENTINEL_QUOTE]])
        self.assertEqual(run("flock_facts.py", "import", str(extra), "--store", str(self.store), "--added-by", "tester",
                             "--date", "2026-10-30")[0], 0)
        self.agency = self.store / "agencies" / AGENCY
        shutil.copy(FIXTURES / "law-sample.csv", self.store / "LAW.csv")
        shutil.copy(FIXTURES / "policy-sample.csv", self.agency / "POLICY.csv")
        self.run_dir, self.findings = self.agency / "runs" / "2026-10-30", self.agency / "runs" / "2026-10-30" / "FINDINGS.csv"
        facts = table(self.agency / "FACTS.csv")
        self.sentinel = next(r[facts[0].index("fact_id")] for r in facts[1:] if r[3] == SENTINEL_VALUE)
        self.build()

    def build(self, statuses=None, rows=(), edits=None):  # set FACTS statuses, re-scaffold (fresh inputs), write findings
        facts = table(self.agency / "FACTS.csv")
        for rec in facts[1:]:
            rec[facts[0].index("status")] = (statuses or {}).get(rec[facts[0].index("fact_id")], "checked")
        write(self.agency / "FACTS.csv", facts)
        if self.findings.is_file():
            write(self.findings, table(self.findings)[:1])  # header-only: re-scaffold is allowed
        self.assertEqual(run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", AGENCY, "--date",
                             "2026-10-30", "--provider", "anthropic", "--model", "claude-opus-5-5",
                             "--applicability", "Sample city police; local ALPR policy applies")[0], 0)
        findings = table(FIXTURES / "findings-sample.csv")
        for (n, column), value in (edits or {}).items():
            findings[n - 1][findings[0].index(column)] = value
        write(self.findings, findings + list(rows))

    def check(self, *extra):
        return run("flock_compare.py", "--check", str(self.run_dir), *extra)

    def summary(self):
        return run("flock_compare.py", "--summary", str(self.run_dir))

    def assert_refused(self, out, expected, errors):
        self.assertEqual(sorted(line for line in out.splitlines() if line.startswith("row ")), sorted(expected))
        self.assertTrue(out.endswith("findings=7 errors=%d\n" % errors), out)
        self.assertEqual(self.summary(), (1, "summary refused: errors=%d (run --check)\n" % errors))

    def test_disputed_cited_fact_refused_once_per_row(self):  # edge 1
        self.assertEqual(self.check(), CLEAN)
        self.build({RETENTION: "disputed"})
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assert_refused(out, ["row 7: " + DISPUTED], 1)
        self.build({SEARCH: "disputed"})  # one fact cited by two rows: one error on each row
        self.assert_refused(self.check()[1], ["row 2: " + DISPUTED, "row 3: " + DISPUTED], 2)
        self.build({RETENTION: "disputed", TRAINING: "disputed"}, edits={(7, "fact_ids"): RETENTION + "; " + TRAINING})
        self.assert_refused(self.check()[1], ["row 7: " + DISPUTED, "row 8: " + DISPUTED], 2)  # two ids, one error
        self.build({RETENTION: "checked"})  # reviewed again: clean
        self.assertEqual(self.check(), CLEAN)

    def test_disputed_quote_fact_both_errors_once(self):  # edge 2
        self.build({OUTSIDE: "disputed"})
        self.assert_refused(self.check()[1], ["row 4: " + DISPUTED, "row 5: " + DISPUTED, "row 5: " + QUOTE], 3)
        self.build({self.sentinel: "disputed"}, edits={(7, "quote_fact_id"): self.sentinel})  # quote_fact_id only, no quote
        self.assert_refused(self.check()[1], ["row 7: " + QUOTE], 1)

    def test_status_wording_cannot_bypass(self):  # a near-miss status is a store error, never a silent pass
        for status in ("Disputed", "disputed ", "DISPUTED"):
            with self.subTest(status=status):
                self.build({RETENTION: status})
                code, out = self.check()
                self.assertEqual((code, "store: " in out, out.count("row 7: " + DISPUTED)), (1, True, 0))
                self.assertTrue(self.summary()[1].startswith("summary refused: errors="))

    def test_disputed_uncited_and_unreviewed_are_not_errors(self):  # edges 3 and 5
        self.build({self.sentinel: "disputed"})
        self.assertEqual(self.check(), CLEAN)
        code, out = self.summary()
        topics = [line for line in out.splitlines() if line.startswith("topic | ")]
        self.assertEqual((code, len(topics)), (0, 7))
        self.assertTrue(all(line.endswith(" unreviewed_cited=0") for line in topics), out)
        self.assertIn("facts | rows=7 cited=5 uncited=2 unreadable=0 quoted=1 unreviewed_cited_facts=0 checked_cited=5\n", out)

    def test_unreviewed_counted_per_topic(self):  # edge 4: unreviewed is a count, never a gate
        r2 = ["R2", "retention-changes", "record-gap", "weak", "The sample contract record does not state a period.",
              self.sentinel, "", "P-3ee5198a9319#435.5", "", "", "Request the contract retention terms."]
        self.build({self.sentinel: "unreviewed"}, rows=[r2])
        self.assertEqual(self.check(), (0, "findings=8 errors=0\n"))
        code, out = self.summary()
        topics = [line for line in out.splitlines() if line.startswith("topic | ")]
        self.assertEqual(code, 0)
        self.assertIn("topic | retention-changes | record-gap | strong=1 medium=0 weak=1 unreviewed_cited=1", topics)
        self.assertEqual(sum(line.endswith(" unreviewed_cited=0") for line in topics), len(topics) - 1)
        self.assertIn("facts | rows=7 cited=6 uncited=1 unreadable=0 quoted=1 unreviewed_cited_facts=1 checked_cited=5\n", out)
        self.build({SEARCH: "unreviewed", OUTSIDE: "unreviewed"}, edits={(2, "fact_ids"): SEARCH + ";" + OUTSIDE})
        code, out = self.summary()  # two unreviewed ids in one finding count it once; one fact in two topics counts in each
        self.assertEqual(code, 0)
        for line in ("topic | na-search-purposes | record-gap | strong=1 medium=0 weak=0 unreviewed_cited=1",
                     "topic | na-search-purposes | duty-concern | strong=0 medium=1 weak=0 unreviewed_cited=1",
                     "topic | outside-agency-access | record-gap | strong=1 medium=0 weak=0 unreviewed_cited=1",
                     "topic | outside-agency-access | duty-concern | strong=0 medium=0 weak=1 unreviewed_cited=1",
                     "topic | audits | record-gap | strong=1 medium=0 weak=0 unreviewed_cited=0",
                     "facts | rows=7 cited=5 uncited=2 unreadable=0 quoted=1 unreviewed_cited_facts=2 checked_cited=3"):
            self.assertIn(line + "\n", out)

    def test_golden_unaffected(self):  # edge 6
        golden = str(FIXTURES / "morrobay-golden.csv")
        self.assertEqual(self.check("--golden", golden), CLEAN)
        self.build({OUTSIDE: "unreviewed"})
        self.assertEqual(self.check("--golden", golden), CLEAN)
        self.build({RETENTION: "disputed"})
        code, out = self.check("--golden", golden)
        self.assertEqual((code, [x for x in out.splitlines() if x.startswith(("row", "golden"))]), (1, ["row 7: " + DISPUTED]))

    def test_no_value_or_quote_text(self):  # edge 7: sentinel value and quote never reach stdout
        quoted = ["S1", "contract-terms", "record-gap", "weak", "The sample contract page states a vendor term.", self.sentinel,
                  "", "P-3ee5198a9319#435.5", SENTINEL_QUOTE, self.sentinel, "Request the full contract."]
        outputs = []
        for status in ("disputed", "unreviewed"):
            self.build({self.sentinel: status}, rows=[quoted])
            outputs += [self.check()[1], self.summary()[1]]
        self.assertIn("row 9: " + QUOTE, outputs[0])
        self.assertIn("unreviewed_cited_facts=1", outputs[3])
        for out in outputs:
            self.assertEqual([t for t in (SENTINEL_VALUE, SENTINEL_QUOTE, "SENTINEL", self.sentinel) if t in out], [])


if __name__ == "__main__":
    unittest.main()
