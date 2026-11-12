"""FACT-REVIEWS.csv v2 binds each review row to the reviewed evidence (fact_id + evidence_fp) (DFGL-07b)."""
import csv
import unittest

from tests.flock.test_facts import snapshot
from tests.flock.test_facts_review import DAY, FactsReviewTest, facts

V1 = ",".join(facts.REVIEW_HEAD)


class ReviewLogV2Test(unittest.TestCase):
    setUp, run_tool, review, check, log_lines = (getattr(FactsReviewTest, n) for n in ("setUp", "run_tool", "review", "check", "log_lines"))

    def fp(self, i):
        return facts.evidence_fp(next(r for r in self.rows if r["fact_id"] == i))

    def rewrite(self, **changes):  # the fixture writer: the first row changes in place (same fact_id), as a re-import would
        self.rows[0] = dict(self.rows[0], **changes)
        facts.write_atomic(self.facts, self.rows)

    def log_rows(self):
        return list(csv.DictReader(self.log.read_text(encoding="utf-8").splitlines()))

    def errs(self):
        return facts.check_reviews(self.log, self.facts), facts.overlay_errors(self.facts, self.log)

    def test_first_review_writes_v2_with_the_rows_fp_and_check_passes(self):
        self.assertEqual(self.review([self.a])[0], 0)
        self.assertEqual(self.log_lines()[0], V1 + ",evidence_fp")
        self.assertEqual(self.log_rows()[0]["evidence_fp"], self.fp(self.a))
        self.assertEqual((self.check(), self.errs()), ((0, []), ([], [])))

    def test_consistent_v1_log_upgraded_with_the_new_row_in_one_write(self):
        self.review([self.a])
        self.log.write_text("%s\n%s,unreviewed,checked,ben,%s,\n" % (V1, self.a, DAY), encoding="utf-8")
        self.assertEqual(self.review([self.b], "checked", "cara")[0], 0)
        rows = self.log_rows()
        self.assertEqual([(r["fact_id"], r["evidence_fp"]) for r in rows], [(self.a, self.fp(self.a)), (self.b, self.fp(self.b))])
        self.assertEqual((rows[0]["reviewer"], rows[0]["reviewed_at"]), ("ben", DAY))
        self.assertEqual((self.check(), self.errs()), ((0, []), ([], [])))

    def test_inconsistent_v1_log_is_refused_and_unchanged(self):
        self.review([self.a])
        self.log.write_text("%s\n%s,checked,disputed,ben,%s,\n" % (V1, self.a, DAY), encoding="utf-8")
        before = snapshot(self.dir)
        self.assertEqual(self.review([self.b])[0], 1)
        self.assertEqual(snapshot(self.dir), before)
        self.assertEqual(self.log.read_text(encoding="utf-8").splitlines()[0], V1)

    def test_changed_quote_unbinds_history_and_a_raw_checked_edit_is_not_a_review(self):
        self.review([self.a])
        old_fp = self.fp(self.a)
        self.rewrite(verbatim_quote="a different quote", status="unreviewed")
        self.assertNotEqual(self.fp(self.a), old_fp)
        self.assertEqual(self.errs(), ([], []))  # old row is unbound history; the row is unreviewed
        self.rewrite(status="checked")
        check, overlay = self.errs()
        self.assertEqual(check, [])
        self.assertEqual(overlay, ["%s: status checked without a bound review" % self.a])
        self.rewrite(status="unreviewed")
        self.assertEqual(self.review([self.a], "disputed", "cara")[0], 0)  # a new review binds to the new fp, old row kept
        self.assertEqual([r["evidence_fp"] for r in self.log_rows()], [old_fp, self.fp(self.a)])
        self.assertEqual(self.errs(), ([], []))

    def test_reverting_the_evidence_regains_the_bound_decision(self):
        self.review([self.a])
        quote = self.rows[0]["verbatim_quote"]
        self.rewrite(verbatim_quote="changed", status="unreviewed")
        self.rewrite(verbatim_quote=quote, status="checked")
        self.assertEqual(self.errs(), ([], []))

    def test_reversal_chain_by_second_reviewers_and_added_by_refused(self):
        for to, who in (("checked", "ben"), ("disputed", "cara"), ("checked", "dan")):
            self.assertEqual(self.review([self.a], to, who)[0], 0)
        self.assertEqual(self.errs(), ([], []))
        self.assertEqual(self.log_rows()[-1]["to_status"], "checked")
        code, out, _ = self.run_tool("reviews", "--store", str(self.store), "--agency", "Alpha")
        self.assertEqual((code, out[-1]), (0, "reviews=3 facts=1 to_checked=2 to_disputed=1 reversals=1"))
        self.assertEqual(self.review([self.a], "disputed", "ANA")[0], 1)

    def test_removed_row_history_is_unbound_and_not_an_error(self):
        self.review([self.a, self.b])
        facts.write_atomic(self.facts, [r for _, r in facts.load(self.facts)[1] if r["fact_id"] != self.a])
        self.assertEqual(self.errs(), ([], []))
        self.assertEqual(self.check(), (0, []))

    def test_unbound_history_with_a_bad_chain_is_still_format_checked(self):
        self.review([self.a])
        facts.write_atomic(self.facts, [r for _, r in facts.load(self.facts)[1] if r["fact_id"] != self.a])
        self.log.write_text(self.log.read_text(encoding="utf-8") + "%s,checked,checked,ben,%s,,E-zz\n" % (self.a, DAY), encoding="utf-8")
        self.assertEqual(len(facts.check_reviews(self.log, self.facts)), 2)


if __name__ == "__main__":
    unittest.main()
