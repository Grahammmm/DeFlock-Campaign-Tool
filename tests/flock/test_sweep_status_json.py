import csv
import importlib.util
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_sweep as base  # noqa: E402  store/watchlist/roster builders; its TestCase is not re-bound here

TOOLS, FIXTURES, AGENCY, WATCH, ROSTER, run = base.TOOLS, base.FIXTURES, base.AGENCY, base.WATCH, base.ROSTER, base.run
_spec = importlib.util.spec_from_file_location("_json_law_policy", TOOLS / "flock_law_policy.py")
law_policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(law_policy)
STATUS = ["slug", "watch_agency", "stage", "facts", "unreadable", "policy_rows", "run_date", "author_family",
          "verifier_family", "public_ok", "questions", "next"]
COUNTS = ["agencies", "finished", "ready", "blocked", "unmapped", "unwatched"]
DATE, THIRD, CANARY = "2026-11-02", "Third City Police", "CANARY-7731-NOT-PRINTED"
# Text mode on these fixtures as printed by origin/main 88edf506 (store and tools paths replaced).
HEAD = "slug,watch_agency,stage,facts,unreadable,policy_rows,run_date,author_family,verifier_family,public_ok,questions,next\n"
NO_FACTS = "no-facts,0,0,0,,,,,,legwork facts + flock_facts.py import\n"
GOLDEN_THREE = (
    HEAD + "sample-city-police-department,Sample City Police Department,finished,6,0,2,2026-10-30,anthropic,openai,4,2,"
    "F5b drafts; F9 export if public_ok>0\n"
    "second-city-police,Second City Police," + NO_FACTS +
    'third-city-police,Third City Police,ready-compare,6,0,2,,,,,,"python3 {tools}/flock_compare.py scaffold --store {store} '
    '--agency third-city-police --date 2026-11-02 --provider <author family> --model <model> --applicability '
    '""Third city police; local ALPR policy applies""; compare card"\n'
    "agencies=3 finished=1 ready=1 blocked=1 unmapped=0 unwatched=0\n")
GOLDEN_BASE = (
    HEAD + "sample-city-police-department,Sample City Police Department,compare-open,6,0,2,2026-10-30,anthropic,,0,0,"
    "resume compare card (REMAINING.md)\n"
    "second-city-police,Second City Police," + NO_FACTS +
    "campus-police,Campus Police,scope-hold,0,0,0,,,,,,operator: applicability line\n"
    "parks,Parks,out-of-scope,0,0,0,,,,,,-\n"
    "harbor-police,Harbor Police," + NO_FACTS +
    ",Unlisted Agency,unmapped,,,,,,,,,add a ROSTER row\n"
    "orphan-folder,,store-only,,,,,,,,,-\n"
    "unwatched: Harbor Police\n"
    "agencies=7 finished=0 ready=0 blocked=6 unmapped=1 unwatched=1\n")


def parse(out):  # the text form: CSV, optional "unwatched: a; b", summary
    lines = out.splitlines()
    named = lines[-2][len("unwatched: "):].split("; ") if lines[-2].startswith("unwatched: ") else []
    rows = list(csv.DictReader(lines[:len(lines) - 1 - bool(named)]))
    return rows, named, {k: int(v) for k, v in (x.split("=") for x in lines[-1].split())}


class FlockSweepStatusJsonTest(unittest.TestCase):
    setUp, finish = base.FlockSweepTest.setUp, base.FlockSweepTest.finish

    def sweep(self, command, *args, watch=WATCH, roster=ROSTER):
        return base.FlockSweepTest.sweep(self, command, "--date", DATE, *args, watch=watch, roster=roster)

    def three(self):  # one finished, one no-facts, one ready-compare (canary in a FACTS value and a POLICY row)
        self.finish()
        facts = (FIXTURES / "compare-facts.csv").read_text().replace("Sample City Police Department", THIRD)
        (self.tmp / "facts3.csv").write_text(facts.replace("Vigilant Solutions", CANARY))
        code, _ = run("flock_facts.py", "import", str(self.tmp / "facts3.csv"), "--store", str(self.store),
                      "--added-by", "tester", "--date", "2026-10-30")
        self.assertEqual(code, 0)
        with open(FIXTURES / "policy-sample.csv", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:  # policy_row_id is derived from the agency, so recompute it with F2's own rule
            row.update(agency=THIRD, adoption_evidence=CANARY if row["adoption_sha256"] else row["adoption_evidence"])
            row["policy_row_id"] = law_policy.derived_id(row)
        with open(self.store / "agencies" / "third-city-police" / "POLICY.csv", "w", newline="") as handle:
            writer = csv.DictWriter(handle, list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        roster, watch = self.tmp / "roster3.csv", self.tmp / "watch3.csv"
        keep = lambda path: [x for x in Path(path).read_text().splitlines(True) if x.startswith(("Sample", "Second"))]
        roster.write_text("".join(Path(ROSTER).read_text().splitlines(True)[:1] + keep(ROSTER)) +
                          THIRD + ",third-city-police,yes,Third city police; local ALPR policy applies,\n")
        watch.write_text("".join(Path(WATCH).read_text().splitlines(True)[:1] + keep(WATCH)) +
                         THIRD + ",agenda,https://www.third.example/a,,third.example,\n")
        return dict(watch=str(watch), roster=str(roster))

    def extended(self):  # base fixture + an unwatched in_scope=yes roster row + a store-only agency folder
        (self.store / "agencies" / "orphan-folder").mkdir()
        roster = self.tmp / "roster-x.csv"
        roster.write_text(Path(ROSTER).read_text() + "Harbor Police,harbor-police,yes,Harbor police; local ALPR policy applies,\n")
        return dict(roster=str(roster))

    def both(self, *args, **files):  # (text code, text out, json code, json doc); the store is never written
        before = sorted((str(p), p.stat().st_mtime_ns) for p in self.store.rglob("*"))
        code, out = self.sweep("status", *args, **files)
        jcode, jout = self.sweep("status", "--json", *args, **files)
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in self.store.rglob("*")), before)
        return code, out, jcode, json.loads(jout)

    def agree(self, out, doc):  # same rows, same order, same counts, every STATUS key and nothing else
        rows, named, counts = parse(out)
        self.assertEqual(sorted(doc), ["agencies", "counts", "date", "errors", "review_required", "schema", "unwatched"])
        self.assertEqual([list(r) for r in doc["agencies"]], [sorted(STATUS)] * len(rows))  # keys sorted, exactly STATUS
        self.assertEqual([{k: str(v) for k, v in r.items()} for r in doc["agencies"]], rows)
        self.assertEqual((doc["unwatched"], doc["counts"], list(counts)), (named, counts, COUNTS))
        self.assertEqual((doc["schema"], doc["review_required"], doc["date"], doc["errors"]),
                         ("flock-sweep-status-v1", True, DATE, []))
        for r in doc["agencies"]:
            self.assertTrue(all(isinstance(r[k], int) or r[k] == "" for k in ("facts", "policy_rows", "public_ok")), r)

    def golden(self, out):
        return out.replace(str(self.store.resolve()), "{store}").replace(str(TOOLS), "{tools}")

    def test_three_agencies_rows_order_counts(self):
        code, out, jcode, doc = self.both(**self.three())
        self.assertEqual((code, jcode), (0, 0), out)
        self.agree(out, doc)
        self.assertEqual([(r["slug"], r["stage"]) for r in doc["agencies"]],
                         [(AGENCY, "finished"), ("second-city-police", "no-facts"), ("third-city-police", "ready-compare")])
        self.assertEqual((doc["agencies"][0]["facts"], doc["agencies"][0]["public_ok"], doc["agencies"][1]["run_date"]), (6, 4, ""))
        self.assertEqual(doc["counts"], dict(agencies=3, finished=1, ready=1, blocked=1, unmapped=0, unwatched=0))
        self.assertEqual(self.golden(out), GOLDEN_THREE)

    def test_unwatched_unmapped_store_only(self):
        code, out, jcode, doc = self.both(**self.extended())
        self.assertEqual((code, jcode), (0, 0), out)
        self.agree(out, doc)
        stages = {r["slug"] or r["watch_agency"]: r["stage"] for r in doc["agencies"]}
        self.assertEqual((stages["harbor-police"], stages["Unlisted Agency"], stages["orphan-folder"]),
                         ("no-facts", "unmapped", "store-only"))
        self.assertEqual((doc["unwatched"], doc["counts"]["unwatched"], doc["counts"]["unmapped"]), (["Harbor Police"], 1, 1))
        self.assertEqual(self.golden(out), GOLDEN_BASE)

    def test_out_stays_csv_and_stdout_is_one_document(self):
        files = self.extended()
        text_csv, json_csv = self.tmp / "text-STATUS.csv", self.tmp / "json-STATUS.csv"
        code, out = self.sweep("status", "--out", str(text_csv), **files)
        jcode, jout = self.sweep("status", "--out", str(json_csv), "--json", **files)
        self.assertEqual((code, jcode), (0, 0), jout)
        self.assertEqual(json_csv.read_bytes(), text_csv.read_bytes())
        self.assertNotIn(b"unwatched", json_csv.read_bytes())
        doc, end = json.JSONDecoder().raw_decode(jout)
        self.assertEqual((jout[end:], jout.lstrip()[:1]), ("\n", "{"))
        self.agree(out, doc)

    def test_errors_match_text_lines(self):
        text = Path(ROSTER).read_text().replace("Parks,parks,no,", "Parks,parks,maybe,")
        (self.tmp / "bad.csv").write_text(text)
        cases = ((dict(roster=str(self.tmp / "bad.csv")), "roster row 5: in_scope is not yes, no or hold"),
                 (dict(watch=str(self.tmp)), "file unreadable (IsADirectoryError)"))
        for files, first in cases:
            with self.subTest(first=first):
                out_file = self.tmp / "never.csv"
                code, out, jcode, doc = self.both("--out", str(out_file), **files)
                self.assertEqual((code, jcode, out.splitlines()[0]), (1, 1, first), out)
                self.assertEqual(doc, {"schema": "flock-sweep-status-v1", "review_required": True, "agencies": [],
                                       "counts": None, "unwatched": [], "errors": out.splitlines()})
                self.assertFalse(out_file.exists())

    def test_json_with_other_commands_is_usage_error(self):
        self.finish()
        for args in (("plan",), ("matrix", "--out", str(self.tmp / "m")), ("funnel", "--out", str(self.tmp / "f"))):
            with self.subTest(command=args[0]):
                before = sorted((str(p), p.stat().st_mtime_ns) for p in self.tmp.rglob("*"))
                self.assertEqual(self.sweep(*args, "--json"), (2, ""))
                self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in self.tmp.rglob("*")), before)
                self.assertEqual(self.sweep(*args)[0], 0)  # the same arguments without --json are valid
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["f", "m", "store"])

    def test_canary_never_in_json(self):
        files = self.three()
        self.assertIn(CANARY, (self.store / "agencies" / "third-city-police" / "FACTS.csv").read_text())
        self.assertIn(CANARY, (self.store / "agencies" / "third-city-police" / "POLICY.csv").read_text())
        code, out, jcode, doc = self.both(**files)
        self.assertEqual((jcode, CANARY in json.dumps(doc), CANARY in out), (0, False, False))


if __name__ == "__main__":
    unittest.main()
