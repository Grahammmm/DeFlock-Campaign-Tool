"""flock_law_policy compare STORE --on D [--types T1,T2] --json: flock-law-policy-compare-v1 (RF1100-12)."""
import json
import unittest

from tests.flock import test_law_policy_compare as base
from tests.flock.test_law_policy_compare import A, B, C, ON, PLANTED, SA, SB, SC, doc, fact, footer, policy, run

CANARY = "CANARY-VALUE-41"
NOTE = "research view, not a legal conclusion"


def counts(a=3, g=3, u=0, t=1, x=0, y=0, d=0, r=0):
    return dict(agencies=a, governed=g, ungoverned=u, types=t, differ=x, agree=y, disputed_skipped=d, unreadable_skipped=r)


def member(agency, *ids):
    return {"agency": agency, "fact_ids": sorted(ids)}


def doc_of(types, ungoverned=(), requested=None, **n):
    return {"schema": "flock-law-policy-compare-v1", "review_required": True, "note": NOTE, "on": ON,
            "types_requested": requested, "ungoverned": list(ungoverned), "types": types, "counts": counts(**n)}


def row(fact_type, classes, undocumented=(), verdict=None):
    return {"fact_type": fact_type, "classes": [{"class": i, "members": m} for i, m in enumerate(classes, 1)],
            "undocumented": list(undocumented), "verdict": verdict}


class CompareJsonTest(unittest.TestCase):
    setUp, write, agency, one, snapshot = (base.CompareTest.setUp, base.CompareTest.write, base.CompareTest.agency,
                                           base.CompareTest.one, base.CompareTest.snapshot)

    def both(self, *extra, json_at=None):  # (exit, parsed doc, raw stdout, text exit, text lines); store never written
        args = ["compare", str(self.store), "--on", ON, *extra]
        before = self.snapshot()
        tcode, tout, terr = run(*args)
        args.insert(len(args) if json_at is None else json_at, "--json")
        code, out, err = run(*args)
        self.assertEqual(self.snapshot(), before, "store changed")
        self.assertEqual((err, terr), ("", ""))
        self.assertTrue(out.endswith("}\n"), out)
        got = json.loads(out)
        self.assertEqual(out, json.dumps(got, sort_keys=True, indent=1, ensure_ascii=False) + "\n")
        return code, got, out, tcode, tout.splitlines()

    def test_two_classes_differ_members_and_ids_match_text(self):
        ia, ib, ic = self.one(A, "a1", "retention", "30 days"), self.one(B, "b1", "retention", "90 days"), \
            self.one(C, "c1", "retention", " 30 DAYS")
        code, got, _, tcode, lines = self.both()
        self.assertEqual((code, tcode), (3, 3))
        self.assertEqual(got, doc_of([row("retention", [[member(SA, ia), member(SC, ic)], [member(SB, ib)]], verdict="differ")],
                                     x=1))
        self.assertEqual(lines, [f"compare | retention | class 1: {SA} ({ia}), {SC} ({ic}) | class 2: {SB} ({ib}) | differ",
                                 footer(x=1)])  # text form byte-identical to main's

    def test_agree_exit_zero_and_single_member_with_undocumented_is_null(self):
        fa = [fact(A, "a1", "partner", "County"), fact(A, "a1", "partner", "State", page="2"), fact(A, "a1", "retention", "30 days")]
        fb = [fact(B, "b1", "partner", "state"), fact(B, "b1", "partner", "county", page="2")]
        self.agency(A, [policy(A, "a1", "2025-01-01")], fa)
        self.agency(B, [policy(B, "b1", "2025-01-01")], fb)
        ida, idb = [f["fact_id"] for f in fa], [f["fact_id"] for f in fb]
        code, got, _, tcode, lines = self.both()
        self.assertEqual((code, tcode), (0, 0))
        self.assertEqual(got, doc_of([row("partner", [[member(SA, *ida[:2]), member(SB, *idb)]], verdict="agree"),
                                      row("retention", [[member(SA, ida[2])]], undocumented=[SB])], a=2, g=2, t=2, y=1))
        self.assertEqual(lines[1], f"compare | retention | class 1: {SA} ({ida[2]}) | undocumented: {SB}")

    def test_ungoverned_listed_and_disputed_counted_not_listed(self):
        gone = fact(A, "a1", "retention", "90 days", status="disputed")
        keep = fact(A, "a1", "retention", "30 days", page="2")
        self.agency(A, [policy(A, "a1", "2025-01-01")], [gone, keep])
        ib = self.one(B, "b1", "retention", "30 days")
        hidden = fact(C, "c1", "retention", "30 days")
        self.agency(C, [policy(C, "c1", "", evidence="unverified")], [hidden])
        code, got, out, _, lines = self.both()
        self.assertEqual(code, 0)
        self.assertEqual(got, doc_of([row("retention", [[member(SA, keep["fact_id"]), member(SB, ib)]], undocumented=[SC])],
                                     ungoverned=[SC], g=2, u=1, d=1))
        self.assertEqual(lines[0], f"compare | {SC} | no governing adopted policy on {ON}")
        for leak in (gone["fact_id"], hidden["fact_id"]):
            self.assertNotIn(leak, out)

    def test_types_filter_and_json_position(self):
        ir = self.one(A, "a1", "retention", "30 days")
        self.agency(B, [policy(B, "b1", "2025-01-01")], [fact(B, "b1", "sharing", "County")])
        want = doc_of([row("retention", [[member(SA, ir)]], undocumented=[SB]), row("unknown_type", [], undocumented=[SA, SB])],
                      requested=["retention", "unknown_type"], a=2, g=2, t=2)
        for extra, at in ((["--types", "unknown_type , retention"], None), (["--types", "retention,unknown_type"], 2)):
            code, got, _, tcode, _ = self.both(*extra, json_at=at)  # after --types, and before --on
            self.assertEqual((code, tcode, got), (0, 0, want), at)
        self.assertEqual(self.both()[1]["types_requested"], None)
        self.assertEqual([t["fact_type"] for t in self.both()[1]["types"]], ["retention", "sharing"])

    def test_json_usage_errors(self):
        self.one(A, "a1", "retention", "30 days")
        s = str(self.store)
        for args in (["compare", s, "--on", ON, "--json", "--json"], ["compare", s, "--json", "--on", ON, "--json"],
                     ["compare", s, "--json"], ["compare", s, "--on", "--json", ON, "--types"],
                     ["governing", s, "--on", ON, "--json", "--json"], ["gaps", s, "--on", ON, "--json", "--json"],
                     ["changes", s, "--json", "--json"], ["coverage", s, "--json", "--json"], ["review", s, "--json", "--json"],
                     ["stale", s, "--max-age-days", "5", "--json", "--json"], ["versions", s, "--agency", SA, "--json"]):
            code, out, err = run(*args)
            self.assertEqual((code, out), (2, ""), args)
            self.assertTrue(err.startswith("usage:"), args)
        self.assertIn("compare STORE --on YYYY-MM-DD [--types T1,T2] [--json]", run("--help")[1])

    def test_broken_facts_stderr_as_text_exit_one_stdout_empty(self):
        self.one(A, "a1", "retention", "30 days")
        self.one(B, "b1", "retention", "30 days")
        (self.store / "agencies" / SB / "FACTS.csv").write_text("not,a,facts,header\n", encoding="utf-8")
        text = run("compare", str(self.store), "--on", ON)
        got = run("compare", str(self.store), "--on", ON, "--json")
        self.assertEqual(got, text)
        self.assertEqual(got[:2], (1, ""))
        self.assertTrue(got[2].startswith(f"{SB}: FACTS.csv row 1:"), got[2])

    def test_canary_values_quotes_hashes_paths_never_in_json(self):
        self.one(A, "a1", "retention", f"{CANARY} 30 days")
        self.one(B, "b1", "retention", f"{CANARY} 90 days")
        code, got, out, tcode, lines = self.both()
        self.assertEqual((code, tcode, got["counts"]["differ"]), (3, 3, 1))
        for leak in (CANARY, PLANTED, doc("a1"), doc("b1"), "f" * 64, "Synthetic minutes", str(self.store), "days"):
            self.assertNotIn(leak, out)
            self.assertNotIn(leak, "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
