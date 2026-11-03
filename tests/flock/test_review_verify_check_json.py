"""RF1050-12: flock_review_verify.py check --json - one flock-review-verify-check-v1 document; text and exit 0/1 unchanged."""
import json, re, unittest

import tests.flock.test_review_verify_agreement as base  # module import, so its tests are not collected twice
from tests.flock.test_review_verify import VERIFY as TOOL, vtool

KEYS = {"schema", "review_required", "clean", "counts", "problems"}
COUNT_KEYS = ["planned", "rows", "agree", "disagree", "needs_source", "missing", "extra", "errors"]
LINE = re.compile(r"^planned=(\d+) rows=(\d+) agree=(\d+) disagree=(\d+) needs_source=(\d+) missing=(\d+) extra=(\d+) errors=(\d+)$")


class CheckJsonTest(unittest.TestCase):
    setUp, tearDown, main, make, write = (base.AgreementTest.setUp, base.AgreementTest.tearDown, base.AgreementTest.main,
                                          base.AgreementTest.make, base.AgreementTest.write)

    def check(self, *extra):
        return self.main("check", self.plan, self.findings, self.verify, *extra)

    def both(self, *extra):  # (text code, text out, json code, parsed document); the JSON is exactly sorted, indent 1
        code, out = self.check(*extra)
        jcode, raw = self.check(*extra, "--json")
        doc = json.loads(raw)
        self.assertEqual(raw, json.dumps(doc, sort_keys=True, indent=1) + "\n")
        self.assertEqual(set(doc), KEYS)
        self.assertEqual((doc["schema"], doc["review_required"]), ("flock-review-verify-check-v1", True))
        self.assertEqual(list(doc["counts"]), sorted(COUNT_KEYS))
        lines = out.splitlines()
        self.assertEqual(dict(zip(COUNT_KEYS, map(int, LINE.match(lines[-1]).groups()))), doc["counts"])  # same numbers
        self.assertEqual(doc["problems"], lines[:-1])  # the same error lines, in order
        self.assertEqual((jcode, doc["clean"]), (code, code == 0))
        for canary in (base.REASON, base.CITE, "basis text"):
            self.assertNotIn(canary, raw)
        return code, out, jcode, doc

    def test_clean_pass(self):
        pri, smp = self.make()
        self.write({"S01": "disagree"} if "S01" in smp else {smp[0]: "needs-source"})
        code, out, jcode, doc = self.both()
        n = len(pri) + len(smp)
        self.assertEqual((code, jcode, doc["clean"], doc["problems"]), (0, 0, True, []))
        self.assertEqual(doc["counts"]["planned"], n)
        self.assertEqual(doc["counts"]["rows"], n)
        self.assertEqual(doc["counts"]["agree"] + doc["counts"]["disagree"] + doc["counts"]["needs_source"], n)
        self.assertEqual(doc["counts"]["agree"], n - 1)
        self.assertEqual(out, "planned=%d rows=%d agree=%d disagree=%d needs_source=%d missing=0 extra=0 errors=0\n"
                         % (n, n, n - 1, doc["counts"]["disagree"], doc["counts"]["needs_source"]))

    def test_missing_rows_and_one_extra_row(self):
        pri, smp = self.make()
        self.write({}, drop=pri[:1] + smp[:1])
        with self.verify.open("a", encoding="utf-8") as handle:
            handle.write("X9,agree,%s,%s\n" % (base.REASON, base.CITE))
        code, out, jcode, doc = self.both()
        n = len(pri) + len(smp)
        self.assertEqual((code, jcode, doc["clean"]), (1, 1, False))
        self.assertEqual({k: doc["counts"][k] for k in ("planned", "rows", "missing", "extra", "errors")},
                         {"planned": n, "rows": n - 1, "missing": 2, "extra": 1, "errors": 3})
        self.assertEqual(doc["problems"], ["row %d: row_id is extra (not in the plan)" % n,
                                           "plan: missing %s" % pri[0], "plan: missing %s" % smp[0]])

    def test_duplicate_and_invalid_rows_are_problems(self):
        pri, smp = self.make()
        self.write({pri[0]: "maybe"})
        with self.verify.open("a", encoding="utf-8") as handle:
            handle.write("%s,agree,,\n" % smp[0])
        code, out, jcode, doc = self.both()
        self.assertEqual((jcode, doc["clean"], doc["counts"]["errors"]), (1, False, 4))
        self.assertIn("row 2: result is invalid", doc["problems"])
        self.assertIn("row %d: row_id is duplicated" % (len(pri) + len(smp) + 2), doc["problems"])

    def test_not_posted_id_in_md_is_a_problem_but_not_echoed(self):
        self.make()
        self.write({})
        md = self.tmp / "VERIFY.md"
        md.write_text("notes about 99999999\n", encoding="utf-8")
        code, out, jcode, doc = self.both("--md", md, "--not-posted", "99999999")
        self.assertEqual((code, jcode, doc["clean"]), (1, 1, False))
        self.assertEqual(doc["problems"], ["not-posted: VERIFY.md contains a configured not-posted id"])
        self.assertEqual(doc["counts"]["errors"], 1)
        self.assertNotIn("99999999", json.dumps(doc))
        self.assertNotIn("99999999", out)

    def test_missing_md_is_a_problem(self):
        self.make()
        self.write({})
        code, out, jcode, doc = self.both("--md", self.tmp / "absent.md")
        self.assertEqual((jcode, doc["problems"]), (1, ["VERIFY.md: file is missing"]))

    def test_changed_findings_is_a_problem(self):
        self.make()
        self.write({})
        with self.findings.open("a", encoding="utf-8") as handle:
            handle.write("Z1,requirement,met,basis text\n")
        code, out, jcode, doc = self.both()
        self.assertEqual((jcode, doc["problems"]), (1, ["plan: findings changed since plan"]))

    def test_plan_error_and_io_print_one_line_not_json(self):
        self.make()
        self.write({})
        self.plan.write_text('{"schema": 2}\n', encoding="utf-8")
        for extra in ((), ("--json",)):
            self.assertEqual(self.check(*extra), (1, "plan: PLAN.json is not a verify plan\n"))
        self.plan.unlink()
        self.make()
        self.verify.unlink()
        for extra in ((), ("--json",)):
            self.assertEqual(self.check(*extra), (1, "io: FileNotFoundError\n"))

    def test_json_is_check_only(self):
        self.make()
        self.write({})
        for argv in (("agreement", self.plan, self.findings, self.verify, "--max-disagree", 20, "--json"),
                     ("counts", self.findings, self.findings, "--json"), ("plan", self.findings, "--seed", 1, "--size", 1,
                                                                         "--out", self.tmp / "P2.json", "--json")):
            with self.subTest(argv=argv[0]):
                self.assertEqual(self.main(*argv)[0], 2)
        self.assertFalse((self.tmp / "P2.json").exists())

    def test_cli_text_and_json_and_usage_line(self):
        self.make()
        self.write({})
        code, out = vtool("check", self.plan, self.findings, self.verify)
        self.assertEqual((code, out), self.check())
        jcode, raw = vtool("check", self.plan, self.findings, self.verify, "--json")
        self.assertEqual((jcode, json.loads(raw)["clean"]), (0, True))
        self.assertIn("check PLAN.json FINDINGS.csv VERIFY.csv [--not-posted ID ...] [--md VERIFY.md] [--json]\n",
                      TOOL.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
