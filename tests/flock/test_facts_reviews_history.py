"""flock_facts reviews: read-only FACT-REVIEWS.csv history after the full check, counts and reversals (RF0420-9)."""
import contextlib
import datetime
import io
import unittest

from tests.flock.test_facts import ROOT, snapshot
from tests.flock import test_facts_review as base
from tests.flock.test_facts_review import ADDED, DOC, HEAD, NOTE, facts

TODAY = datetime.date.today()
D1, D2, D3 = ((TODAY - datetime.timedelta(days=n)).isoformat() for n in (20, 10, 5))
REVIEWERS = ["Marlowe Quist", "Tamsin Vexley"]
SECRETS = ["Quillwort", "REQ-SECRET-7", DOC, NOTE, "Brindlemoss"] + REVIEWERS + ["Marlowe", "Tamsin"]
USAGE = "usage: reviews --store DIR --agency SLUG [--fact ID] [--since YYYY-MM-DD]"


class FactsReviewsHistoryTest(unittest.TestCase):
    """The review test's store (facts a, b added by ana; c by cara), built and logged by the real `review` command."""
    setUp, review = base.FactsReviewTest.setUp, base.FactsReviewTest.review
    log_lines, check = base.FactsReviewTest.log_lines, base.FactsReviewTest.check

    def run_tool(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = facts.main(list(args))
            except SystemExit as exc:
                code = exc.code
        for secret in SECRETS:
            self.assertNotIn(secret, out.getvalue() + err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        return code, out.getvalue().splitlines(), err.getvalue().splitlines()

    def build_log(self):
        """a checked; b checked then disputed; c disputed then checked. Every row carries a reviewer name and the note."""
        one, two = REVIEWERS
        for ids, to, who, day in (([self.a, self.b], "checked", one, D1), ([self.c], "disputed", one, D1),
                                  ([self.b], "disputed", two, D2), ([self.c], "checked", two, D3)):
            code, out, err = self.review(ids, to, who, "--date", day, "--note", NOTE)
            self.assertEqual((code, err), (0, []), out)
        self.assertEqual(len(self.log_lines()), 6)
        self.assertTrue(all(NOTE in line for line in self.log_lines()[1:]))

    def history(self, *extra, store=None, agency="Alpha"):
        """Every reviews call: the whole temp dir (store and all) is byte-identical before and after."""
        before = snapshot(self.dir)
        got = self.run_tool("reviews", "--store", str(store or self.store), "--agency", agency, *extra)
        self.assertEqual(snapshot(self.dir), before, "reviews changed the store")
        return got

    def line(self, fid, change, day):
        return "reviews | alpha | %s | %s | %s" % (fid, change, day)

    def refusal(self, *extra, **kw):
        code, out, err = self.history(*extra, **kw)
        self.assertEqual((code, out), (1, []))
        return err

    def test_history_in_file_order_with_counts_and_reversals(self):
        self.build_log()
        a, b, c = self.a, self.b, self.c
        self.assertEqual(self.history(), (0, [
            self.line(a, "unreviewed -> checked", D1), self.line(b, "unreviewed -> checked", D1),
            self.line(c, "unreviewed -> disputed", D1), self.line(b, "checked -> disputed", D2),
            self.line(c, "disputed -> checked", D3),
            "reviews=5 facts=3 to_checked=3 to_disputed=2 reversals=2"], []))
        self.assertEqual(self.check(), (0, []))

    def test_fact_and_since_filters_alone_and_together(self):
        self.build_log()
        a, b, c = self.a, self.b, self.c
        self.assertEqual(self.history("--fact", b)[1], [self.line(b, "unreviewed -> checked", D1), self.line(
            b, "checked -> disputed", D2), "reviews=2 facts=1 to_checked=1 to_disputed=1 reversals=1"])
        self.assertEqual(self.history("--fact", a)[1], [self.line(a, "unreviewed -> checked", D1),
                                                        "reviews=1 facts=1 to_checked=1 to_disputed=0 reversals=0"])
        self.assertEqual(self.history("--since", D2), (0, [  # inclusive: the D2 row counts, the earlier half of b's flip does not
            self.line(b, "checked -> disputed", D2), self.line(c, "disputed -> checked", D3),
            "reviews=2 facts=2 to_checked=1 to_disputed=1 reversals=0"], []))
        self.assertEqual(self.history("--since", D3, "--fact", c), (0, [
            self.line(c, "disputed -> checked", D3), "reviews=1 facts=1 to_checked=1 to_disputed=0 reversals=0"], []))
        self.assertEqual(self.history("--since", D3, "--fact", b), (0, [
            "reviews=0 facts=0 to_checked=0 to_disputed=0 reversals=0"], []))
        self.assertEqual(self.history("--since", TODAY.isoformat())[1], ["reviews=0 facts=0 to_checked=0 to_disputed=0 reversals=0"])

    def test_unknown_fact_refused_malformed_fact_and_since_are_usage(self):
        self.build_log()
        self.assertEqual(self.refusal("--fact", "F-0123456789ab"), ["--fact: unknown fact id"])
        for extra in (["--fact", "F-XYZ"], ["--fact", ""], ["--fact", self.a.upper()], ["--fact", self.a + "0"],
                      ["--fact", "Quillwort"], ["--since", "2026-1-05"], ["--since", "2026-02-30"], ["--since", ""],
                      ["--since", "yesterday"]):
            with self.subTest(extra=extra):
                code, out, _ = self.history(*extra)
                self.assertEqual((code, out), (2, []))

    def test_no_log_gives_zero_counts_and_creates_nothing(self):
        self.assertEqual(self.history(), (0, ["reviews=0 facts=0 to_checked=0 to_disputed=0 reversals=0"], []))
        self.assertEqual(self.history("--fact", self.a, "--since", D1)[0], 0)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.refusal("--fact", "F-0123456789ab"), ["--fact: unknown fact id"])

    def test_bad_log_refused_with_check_lines(self):
        self.build_log()
        real = self.dir / "real.csv"
        real.write_bytes(self.log.read_bytes())
        self.log.unlink()
        self.log.symlink_to(real)
        self.assertEqual(self.refusal(), ["file 1: FACT-REVIEWS.csv: is a symlink or not a file; refusing"])
        self.log.unlink()
        self.log.write_text("fact_id,to_status\n", encoding="utf-8")
        self.assertEqual(self.refusal("--fact", self.a), ["file 1: reviews row 1: header is not FACT-REVIEWS.csv v1"])
        self.log.write_text("%s\n%s,unreviewed,disputed,%s,%s,%s\n" % (HEAD, self.a, REVIEWERS[0], D1, NOTE), encoding="utf-8")
        self.assertEqual(self.refusal("--since", TODAY.isoformat()), [  # a has FACTS status checked, last logged disputed
            "file 1: reviews: %s: FACTS status checked does not match the review log (disputed)" % self.a])
        self.log.write_text("%s\n%s,unreviewed,checked,Cara,%s,\n" % (HEAD, self.c, D1), encoding="utf-8")
        self.assertEqual(self.refusal()[0], "file 1: reviews row 2: reviewer is invalid")

    def test_bad_facts_or_store_refused(self):
        self.build_log()
        self.assertEqual(self.refusal(store=ROOT / "no-such-reviews-store"), ["store inside repository"])
        self.assertFalse((ROOT / "no-such-reviews-store").exists())
        other = self.dir / "other"
        self.folder.rename(other)
        self.folder.symlink_to(other, target_is_directory=True)
        self.assertEqual(self.refusal(), ["agencies/alpha: is a symlink; refusing"])
        self.folder.unlink()
        other.rename(self.folder)
        self.assertEqual(self.refusal(agency="Beta"), ["agency beta: no FACTS.csv"])
        moved = self.dir / "FACTS-real.csv"
        self.facts.rename(moved)
        self.assertEqual(self.refusal(), ["agency alpha: no FACTS.csv"])
        self.facts.symlink_to(moved)
        self.assertEqual(self.refusal(), ["agencies/alpha/FACTS.csv: is a symlink; refusing"])
        self.facts.unlink()
        moved.write_text(moved.read_text(encoding="utf-8").replace(ADDED, "bad-date", 1), encoding="utf-8")
        moved.rename(self.facts)
        self.assertEqual(self.refusal()[0], "file 1: row 2: added_at is invalid")

    def test_flags_of_other_commands_are_usage_errors(self):
        self.build_log()
        before = snapshot(self.dir)
        store = ["--store", str(self.store)]
        for args in (["status", *store, "--fact", self.a], ["stale", *store, "--date", D1, "--since", D1],
                     ["review", *store, "--agency", "alpha", "--ids", self.a, "--set", "checked", "--reviewer", "dan",
                      "--since", D1], ["--check", str(self.store), "--fact", self.a]):
            with self.subTest(args=args[0]):
                code, out, err = self.run_tool(*args)
                self.assertEqual((code, out, err[-1][-41:]), (2, [], "--fact and --since belong to reviews only"))
        for extra in (["--check", "x"], ["--date", D1], ["--out", "x.csv"], ["--list", "disputed"], ["--added-by", "x"],
                      ["--inventory", "x"], ["--findings", "x"], ["--text", "x"], ["--ids", self.a], ["--set", "checked"],
                      ["--reviewer", "dan"], ["--note", "x"], ["extra"]):
            with self.subTest(extra=extra):
                code, out, _ = self.history(*extra)
                self.assertEqual((code, out), (2, []))
        for args in (["reviews", *store], ["reviews", "--agency", "alpha"], ["reviews", *store, "--agency", "!!"]):
            code, out, err = self.run_tool(*args)
            self.assertEqual((code, out, err[-1][-len(USAGE):]), (2, [], USAGE))
        self.assertEqual(snapshot(self.dir), before)


if __name__ == "__main__":
    unittest.main()
