"""flock_compare --summary: ids and counts of what a clean run cited and left uncited; never text (RF1150-1)."""
import csv
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from campaign_tool import law
from tests.flock.test_compare import AGENCY, FIXTURES, run

PACKAGE = {r["rule_id"]: r for r in law.load_package("us-ca")["rules"]}
NAR, VERIFIED = "ca-civ-1798.90.5-definitions", "ca-civ-1798.90.51-security-procedures"
REFUSED = "summary refused: errors=%d (run --check)\n"


def table(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def append(path, *rows):
    with open(path, "a", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)


def split(text):  # ids() as a set
    return {part.strip() for part in text.split(";")} - {""}


def records(path):
    rows = table(path)
    return [dict(zip(rows[0], (c.strip() for c in rec))) for rec in rows[1:] if any(c.strip() for c in rec)]


class CompareSummaryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp, self.store = Path(tmp.name), Path(tmp.name) / "store"
        self.assertEqual(self.facts_import(FIXTURES / "compare-facts.csv"), 0)
        self.agency = self.store / "agencies" / AGENCY
        shutil.copy(FIXTURES / "law-sample.csv", self.store / "LAW.csv")
        shutil.copy(FIXTURES / "policy-sample.csv", self.agency / "POLICY.csv")
        self.run_dir = self.agency / "runs" / "2026-10-30"
        self.assertEqual(run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", AGENCY, "--date",
                             "2026-10-30", "--provider", "anthropic", "--model", "claude-opus-5-5",
                             "--applicability", "Sample city police; local ALPR policy applies")[0], 0)
        meta = json.loads((self.run_dir / "RUN.json").read_text(encoding="utf-8"))  # legacy run: counts, not input freshness
        (self.run_dir / "RUN.json").write_text(json.dumps({k: v for k, v in meta.items() if k != "inputs"}), encoding="utf-8")
        shutil.copy(FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")
        self.findings = self.run_dir / "FINDINGS.csv"

    def facts_import(self, path):
        return run("flock_facts.py", "import", str(path), "--store", str(self.store), "--added-by", "tester",
                   "--date", "2026-10-30")[0]

    def summary(self, *args):
        return run("flock_compare.py", "--summary", str(self.run_dir), *args)

    def law_row(self, rule_id):
        rule = PACKAGE[rule_id]
        append(self.store / "LAW.csv", [rule_id, "Sample section", "Sample duty", "https://leginfo.legislature.ca.gov/sample/9",
                                        "c" * 64, "2026-10-01", rule["effective_from"], "", rule["review"], "tester",
                                        "2026-10-30", "unreviewed"])

    def finding(self, finding_id, rating, rule_id, fact_id="F-090c3d39b556", quote="", quote_fact_id=""):
        append(self.findings, [finding_id, "definitions", "record-gap", rating, "The sample record does not define a read.",
                               fact_id, rule_id, "", quote, quote_fact_id, "Request the definitions page."])

    def expected(self):  # recomputed from the store and run files, independently of the tool
        rows, facts = records(self.findings), records(self.agency / "FACTS.csv")
        rules, policy = {r["rule_id"] for r in records(self.store / "LAW.csv")}, \
            {r["policy_row_id"] for r in records(self.agency / "POLICY.csv")}
        counts = {}
        for r in rows:
            counts.setdefault((r["topic"], r["claim_class"]), []).append(r["rating"])
        out = ["topic | %s | %s | strong=%d medium=%d weak=%d" % (t, c, v.count("strong"), v.count("medium"), v.count("weak"))
               for (t, c), v in sorted(counts.items())]
        cited = set().union(*(split(r["rule_ids"]) for r in rows))
        review = sum(PACKAGE[x]["review"] == "needs_attorney_review" for x in cited)
        out += ["law | rules=%d cited=%d uncited=%d attorney_review_cited=%d" % (len(rules), len(cited), len(rules - cited), review)]
        out += ["law uncited | " + x for x in sorted(rules - cited)]
        refs = {x.split("#")[0] for r in rows for x in split(r["policy_refs"])}
        out += ["policy | rows=%d cited=%d uncited=%d" % (len(policy), len(refs), len(policy - refs))]
        out += ["policy uncited | " + x for x in sorted(policy - refs)]
        ids, used = {f["fact_id"] for f in facts}, set().union(*(split(r["fact_ids"]) for r in rows))
        out += ["facts | rows=%d cited=%d uncited=%d unreadable=%d quoted=%d" % (
            len(ids), len(used), len(ids - used), sum(f["value"] == "UNREADABLE" for f in facts),
            len({r["quote_fact_id"] for r in rows} - {""}))]
        return "\n".join(out + ["findings=%d | counts of what this run cited, not a review verdict" % len(rows)]) + "\n"

    def snapshot(self):
        return sorted((str(p.relative_to(self.tmp)), p.lstat().st_size, p.lstat().st_mtime_ns) for p in self.tmp.rglob("*"))

    def test_golden_counts(self):
        code, out = self.summary()
        self.assertEqual((code, out), (0, self.expected()))
        for line in ("topic | audits | record-gap | strong=1 medium=0 weak=0",
                     "topic | na-search-purposes | duty-concern | strong=0 medium=1 weak=0",
                     "law | rules=3 cited=2 uncited=1 attorney_review_cited=0",
                     "law uncited | ca-oag-2023-dle-06-out-of-state-sharing-guidance",
                     "policy | rows=2 cited=1 uncited=1", "policy uncited | P-2745cbbfbcad",
                     "facts | rows=6 cited=5 uncited=1 unreadable=0 quoted=1"):
            self.assertIn(line + "\n", out)
        self.assertTrue(out.endswith("findings=7 | counts of what this run cited, not a review verdict\n"), out)
        self.assertEqual(out.count("topic | "), 7)
        append(self.findings, [], ["", " "])  # blank rows are skipped as --check skips them
        self.assertEqual(self.summary(), (code, out))

    def test_uncited_rule_and_policy_row(self):
        self.law_row(VERIFIED)
        pid = "P-" + hashlib.sha256("\x1f".join((AGENCY, "436", "test copy", "c" * 64)).encode()).hexdigest()[:12]  # derived id
        append(self.agency / "POLICY.csv", [pid, "Sample City Police Department", "436", "test copy", "c" * 64, "2025-01-01",
                                             "unverified", "", "", "", "tester", "2026-10-30", "unreviewed"])
        self.assertEqual(run("flock_compare.py", "--check", str(self.run_dir))[0], 0)  # LAW and POLICY rows are valid
        code, out = self.summary()
        self.assertEqual((code, out), (0, self.expected()))
        self.assertIn("law | rules=4 cited=2 uncited=2 attorney_review_cited=0\n", out)
        self.assertIn("policy | rows=3 cited=1 uncited=2\n", out)
        self.assertEqual((out.count("law uncited | " + VERIFIED + "\n"), out.count("policy uncited | " + pid + "\n")), (1, 1))

    def test_attorney_review_cited_only_when_a_finding_cites_it(self):
        self.law_row(NAR)  # a needs_attorney_review rule in LAW.csv but uncited
        self.assertIn("law | rules=4 cited=2 uncited=2 attorney_review_cited=0\n", self.summary()[1])
        self.finding("D1", "weak", NAR)
        self.finding("D2", "medium", NAR)  # two findings citing one rule count it once
        code, out = self.summary()
        self.assertEqual((code, out), (0, self.expected()))
        self.assertIn("law | rules=4 cited=3 uncited=1 attorney_review_cited=1\n", out)
        self.assertIn("topic | definitions | record-gap | strong=0 medium=1 weak=1\n", out)
        self.finding("D3", "strong", NAR)  # --check rejects strong on a needs_attorney_review rule
        self.assertEqual(self.summary(), (1, REFUSED % 1))

    def test_facts_unreadable_uncited_and_distinct_counts(self):
        extra = self.tmp / "unreadable.csv"
        shutil.copy(FIXTURES / "compare-facts.csv", extra)
        append(extra, ["Sample City Police Department", "REQ-101", "audit_reports", "UNREADABLE", "b" * 64, "7", ""])
        self.assertEqual(self.facts_import(extra), 0)
        self.finding("D1", "weak", "ca-civ-1798.90.52-access-records", "F-76dbe8a9b4ac",
                     "Twelve outside agencies have query access.", "F-76dbe8a9b4ac")  # same fact cited and quoted again
        code, out = self.summary()
        self.assertEqual((code, out), (0, self.expected()))
        self.assertIn("facts | rows=7 cited=5 uncited=2 unreadable=1 quoted=1\n", out)
        self.assertNotIn("F-", out)  # no fact id is printed

    def test_dirty_run_and_wrong_folder_refused(self):
        rows = table(self.findings)
        rows[1][rows[0].index("fact_ids")] = "F-000000000000"
        with open(self.findings, "w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(rows)
        code, out = run("flock_compare.py", "--check", str(self.run_dir))
        self.assertEqual((code, out.splitlines()[-1]), (1, "findings=7 errors=1"))
        self.assertEqual(self.summary(), (1, REFUSED % 1))
        shutil.copy(FIXTURES / "findings-sample.csv", self.findings)
        (self.agency / "POLICY.csv").unlink()
        code, out = self.summary()  # a store error is refused the same way: one line, no topic line
        self.assertEqual((code, out.startswith("summary refused: errors="), out.count("\n")), (1, True, 1))
        for folder in (self.agency, self.agency / "runs", self.store, Path("")):
            with self.subTest(folder=str(folder)):
                self.assertEqual(run("flock_compare.py", "--summary", str(folder)), (1, REFUSED % 1))

    def test_no_text_leaks(self):
        self.finding("D1", "weak", "ca-civ-1798.90.52-access-records", "F-76dbe8a9b4ac",
                     "Twelve outside agencies have query access.", "F-76dbe8a9b4ac")
        code, out = self.summary()
        self.assertEqual(code, 0)
        texts = [r[k] for r in records(self.findings) for k in ("claim", "quote", "next_step")]
        texts += [r["policy_refs"].partition("#")[2] for r in records(self.findings)]  # policy clause text
        texts += [r[k] for r in records(self.agency / "FACTS.csv") for k in ("value", "verbatim_quote", "source_sha256")]
        texts += [r[k] for r in records(self.agency / "POLICY.csv") for k in ("version_label", "adoption_evidence", "doc_sha256")]
        texts += [r[k] for r in records(self.store / "LAW.csv") for k in ("section", "duty", "official_url", "text_sha256")]
        topics = " ".join(r["topic"] for r in records(self.findings))  # topic ids are printed by design ("withheld")
        texts = {t for t in texts if len(t) >= 5 and t not in topics}
        self.assertGreater(len(texts), 20)
        self.assertEqual([t for t in sorted(texts) if t in out], [])

    def test_usage_combinations_exit_2(self):
        for extra in (["--check", str(self.run_dir)], ["--golden", str(FIXTURES / "morrobay-golden.csv")], ["scaffold"],
                      ["--store", str(self.store)], ["--provider", "anthropic"], ["--model", ""]):
            with self.subTest(extra=extra):
                self.assertEqual(self.summary(*extra), (2, ""))
        self.assertEqual(run("flock_compare.py", "--check", str(self.run_dir), "--summary", str(self.run_dir)), (2, ""))

    def test_writes_nothing(self):
        before = self.snapshot()
        self.assertEqual(self.summary()[0], 0)
        self.assertEqual(self.snapshot(), before)
        self.finding("D3", "strong", NAR)  # refused path: nothing written either
        before_refused = self.snapshot()
        self.assertEqual(self.summary()[0], 1)
        self.assertEqual(self.snapshot(), before_refused)
        self.assertNotEqual(before, before_refused)  # the snapshot does see a change
