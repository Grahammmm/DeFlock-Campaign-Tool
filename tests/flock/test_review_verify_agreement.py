"""RF1710-8: flock_review_verify.py agreement - results per plan group and verdict, and the escalate= rule (read-only, exit 3)."""
import contextlib, importlib.util, io, json, os, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("_rf1710_8_verify", ROOT / "tools" / "flock_review_verify.py")
spec.loader.exec_module(VERIFY := importlib.util.module_from_spec(spec))

REASON, CITE = "private-reason-sentinel", "private-cite-sentinel"
HEAD = "finding_id,kind,verdict,basis\n"
# Priority P1-P3 (verdict wording varies in case/space), pool S01-S12, one n/a row in neither list.
ROWS = [("P1", "not met"), ("P2", " CONTRADICTED "), ("P3", "Not Met")] + [
    ("S%02d" % n, "met" if n % 2 else "Cannot Tell") for n in range(1, 13)] + [("N1", "n/a")]
TAIL = "agreement | priority_rows=%d sample_rows=%d pool=%d sample_rate=%s escalate=%s | a verify pass is not acceptance"


class AgreementTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp, self.seen = Path(tmp.name), []
        self.findings, self.plan, self.verify = (self.tmp / n for n in ("FINDINGS.csv", "PLAN.json", "VERIFY.csv"))

    def main(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            try: code = VERIFY.main([str(a) for a in argv])
            except SystemExit as exc: code = exc.code
        self.seen += [out.getvalue(), err.getvalue()]
        return code, out.getvalue()

    def make(self, rows=ROWS, size=10):
        self.findings.write_text(HEAD + "".join("%s,requirement,%s,basis text\n" % r for r in rows), encoding="utf-8")
        self.assertEqual(self.main("plan", self.findings, "--seed", 7, "--size", size, "--out", self.plan)[0], 0)
        body = json.loads(self.plan.read_text(encoding="utf-8"))
        return body["priority_ids"], body["sample_ids"]

    def write(self, results, drop=()):  # results: {id: result}; ids not named agree
        pri, smp = (json.loads(self.plan.read_text(encoding="utf-8"))[k] for k in ("priority_ids", "sample_ids"))
        rows = ["%s,%s,%s,%s\n" % (i, results.get(i, "agree"), REASON, CITE) for i in pri + smp if i not in drop]
        self.verify.write_text("row_id,result,reason,page_cite\n" + "".join(rows), encoding="utf-8")

    def agree(self, pct=20, *extra):
        return self.main("agreement", self.plan, self.findings, self.verify, "--max-disagree", pct, *extra)

    def tearDown(self):  # 10: reason and page_cite text never reach stdout or stderr
        self.assertFalse([t for t in self.seen if REASON in t or CITE in t or "basis text" in t])

    def test_all_agree_escalates_nothing_and_verdicts_sum_to_rows(self):
        pri, smp = self.make()
        self.write({})
        code, out = self.agree()
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertEqual(lines[:2], ["agreement | group priority | agree=3 disagree=0 needs_source=0",
                                     "agreement | group sample | agree=10 disagree=0 needs_source=0"])
        verdicts = [line for line in lines if line.startswith("agreement | verdict ")]
        self.assertEqual([v.split(" | ")[1] for v in verdicts], ["verdict met", "verdict not met", "verdict contradicted", "verdict cannot tell"])
        self.assertEqual(sum(int(v.split("| agree=")[1].split()[0]) for v in verdicts), len(pri) + len(smp))
        self.assertIn("agreement | verdict not met | agree=2 disagree=0 needs_source=0", lines)  # "Not Met" lower-cased
        self.assertIn("agreement | verdict contradicted | agree=1 disagree=0 needs_source=0", lines)
        self.assertEqual(lines[-2:], ["agreement | disagree_ids=-", TAIL % (3, 10, 12, "83.3", "none")])

    def test_priority_disagree_escalates_priority_with_its_id(self):
        self.make()
        self.write({"P2": "disagree"})
        code, out = self.agree()
        self.assertEqual(code, 3, out)
        self.assertIn("agreement | group priority | agree=2 disagree=1 needs_source=0", out)
        self.assertIn("agreement | verdict contradicted | agree=0 disagree=1 needs_source=0", out)
        self.assertIn("agreement | disagree_ids=P2\n", out)
        self.assertTrue(out.endswith(TAIL % (3, 10, 12, "83.3", "priority") + "\n"))

    def test_sample_threshold_equal_is_not_over(self):
        _, smp = self.make()
        self.assertEqual(len(smp), 10)
        self.write({smp[0]: "disagree", smp[5]: "disagree"})
        code, out = self.agree(20)
        self.assertEqual(code, 0, out)
        self.assertIn("agreement | group sample | agree=8 disagree=2 needs_source=0", out)
        self.assertIn("escalate=none |", out)
        code, out = self.agree(19)
        self.assertEqual(code, 3, out)
        self.assertIn("escalate=sample |", out)
        self.assertIn("agreement | disagree_ids=-\n", out)  # sample ids are never listed
        self.assertEqual(self.agree(0)[0], 3)  # any sample disagree is over a 0 bar
        self.assertEqual(self.agree(100)[0], 0)

    def test_both_rules_escalate_both(self):
        _, smp = self.make()
        self.write({"P1": "disagree", "P3": "needs-source", **{i: "disagree" for i in smp[:3]}})
        code, out = self.agree(20)
        self.assertEqual(code, 3, out)
        self.assertIn("agreement | disagree_ids=P1,P3\n", out)
        self.assertIn("escalate=both |", out)

    def test_needs_source_is_not_agree_in_both_rules(self):
        _, smp = self.make()
        self.write({"P3": "needs-source"})
        code, out = self.agree()
        self.assertEqual(code, 3, out)
        self.assertIn("agreement | group priority | agree=2 disagree=0 needs_source=1", out)
        self.assertIn("agreement | disagree_ids=P3\n", out)
        self.assertIn("escalate=priority |", out)
        self.write({i: "needs-source" for i in smp[:2]} | {smp[2]: "disagree"})
        code, out = self.agree(20)  # 3 of 10 not agree: 30 > 20
        self.assertEqual(code, 3, out)
        self.assertIn("agreement | group sample | agree=7 disagree=1 needs_source=2", out)
        self.assertIn("escalate=sample |", out)

    def test_empty_pool_or_zero_sample_never_fires_sample_rule(self):
        self.make(rows=[r for r in ROWS if r[0][0] != "S"], size=5)
        self.write({})
        code, out = self.agree(0)
        self.assertEqual(code, 0, out)
        self.assertIn("agreement | group sample | agree=0 disagree=0 needs_source=0", out)
        self.assertTrue(out.endswith(TAIL % (3, 0, 0, "0", "none") + "\n"))
        self.plan.unlink()
        self.make(size=0)
        self.write({})
        code, out = self.agree(0)
        self.assertEqual(code, 0, out)
        self.assertTrue(out.endswith(TAIL % (3, 0, 12, "0", "none") + "\n"))

    def test_changed_findings_fails_check_first(self):
        self.make()
        self.write({"P1": "disagree"})
        self.findings.write_text(self.findings.read_text(encoding="utf-8").replace("S01,requirement,met", "S01,requirement,not met"), encoding="utf-8")
        code, out = self.agree()
        self.assertEqual(code, 1, out)
        self.assertIn("plan: findings changed since plan\n", out)
        self.assertTrue(out.endswith("agreement: check failed; run check first\n"))
        self.assertNotIn("escalate=", out)

    def test_partial_or_invalid_verify_gives_no_partial_agreement(self):
        pri, smp = self.make()
        cases = (("missing", lambda: self.write({}, drop=(smp[3],)), "plan: missing %s" % smp[3]),
                 ("extra", lambda: self.verify.write_text(self.verify.read_text() + "Z9,agree,r,c\n"), "row_id is extra"),
                 ("invalid", lambda: self.write({"P1": "Agree"}), "row 2: result is invalid"),
                 ("duplicate", lambda: self.verify.write_text(self.verify.read_text() + "P1,agree,r,c\n"), "row_id is duplicated"),
                 ("not-posted md", lambda: (self.tmp / "V.md").write_text("see 4242\n"), "not-posted: VERIFY.md"),
                 ("bad plan", lambda: self.plan.write_text("{}\n"), "plan: PLAN.json is not a verify plan"))
        for name, edit, expected in cases:
            with self.subTest(name):
                self.write({})
                (self.tmp / "V.md").write_text("notes\n")
                self.assertEqual(self.agree(20, "--not-posted", "4242", "--md", self.tmp / "V.md")[0], 0)
                edit()
                code, out = self.agree(20, "--not-posted", "4242", "--md", self.tmp / "V.md")
                self.assertEqual(code, 1, out)
                self.assertIn(expected, out)
                self.assertTrue(out.endswith("agreement: check failed; run check first\n"))
                self.assertNotIn("agreement | ", out)
        self.plan.unlink()
        self.make()
        body = json.loads(self.plan.read_text(encoding="utf-8"))
        for bad in (None, "12", 9, True):  # pool_size absent, not an int, or under the sample size
            with self.subTest(pool_size=bad):
                self.plan.write_text(json.dumps({**body, "pool_size": bad} if bad is not None else
                                                {k: v for k, v in body.items() if k != "pool_size"}), encoding="utf-8")
                self.write({})
                self.assertEqual(self.agree(), (1, "agreement: plan pool_size is invalid\n"))

    def test_max_disagree_is_required_and_bounded(self):
        self.make()
        self.write({})
        base = ("agreement", self.plan, self.findings, self.verify)
        for extra in ((), ("--max-disagree", -1), ("--max-disagree", 101), ("--max-disagree", "20.5"), ("--max-disagree", "")):
            with self.subTest(extra=extra):
                self.assertEqual(self.main(*base, *extra), (2, ""))
        self.assertEqual(self.agree(0)[0], 0)
        self.assertEqual(self.agree(100)[0], 0)

    def test_read_only(self):
        self.make()
        self.write({"P1": "disagree"})
        md = self.tmp / "V.md"
        md.write_text("notes\n")
        paths = (self.findings, self.plan, self.verify, md)
        before = [(p.read_bytes(), os.stat(p).st_mtime_ns) for p in paths]
        listing = sorted(os.listdir(self.tmp))
        for pct in (0, 20, 100):
            self.assertEqual(self.agree(pct, "--md", md)[0], 3)
        self.assertEqual([(p.read_bytes(), os.stat(p).st_mtime_ns) for p in paths], before)
        self.assertEqual(sorted(os.listdir(self.tmp)), listing)


if __name__ == "__main__":
    unittest.main()
