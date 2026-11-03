"""flock_compare --summary RUNDIR --json: the run summary as one flock-compare-summary-v1 document; refusals and text unchanged (RF0620-13)."""
import csv
import hashlib
import json
import shutil
import unittest

from tests.flock import test_compare_summary as base
from tests.flock.test_compare import AGENCY, FIXTURES, run

OLD, NEW = "P-3ee5198a9319", "P-2745cbbfbcad"  # fixture: OLD unverified, NEW adopted from 2026-10-13
NOTE = "counts of what this run cited, not a review verdict"
# the fixture run's text summary, captured from main before the --json change: text mode must still print exactly this
GOLDEN = ("topic | audits | record-gap | strong=1 medium=0 weak=0 unreviewed_cited=0\n"
          "topic | na-search-purposes | duty-concern | strong=0 medium=1 weak=0 unreviewed_cited=0\n"
          "topic | na-search-purposes | record-gap | strong=1 medium=0 weak=0 unreviewed_cited=0\n"
          "topic | outside-agency-access | duty-concern | strong=0 medium=0 weak=1 unreviewed_cited=0\n"
          "topic | outside-agency-access | record-gap | strong=1 medium=0 weak=0 unreviewed_cited=0\n"
          "topic | retention-changes | record-gap | strong=1 medium=0 weak=0 unreviewed_cited=0\n"
          "topic | withheld-training | record-gap | strong=1 medium=0 weak=0 unreviewed_cited=0\n"
          "law | rules=3 cited=2 uncited=1 attorney_review_cited=0\n"
          "law uncited | ca-oag-2023-dle-06-out-of-state-sharing-guidance\n"
          "policy | rows=2 cited=1 uncited=1\n"
          "policy uncited | P-2745cbbfbcad\n"
          "facts | rows=6 cited=5 uncited=1 unreadable=0 quoted=1 unreviewed_cited_facts=0 checked_cited=5\n"
          "policy governs | P-3ee5198a9319 | not established on 2026-10-30\n"
          "policy governs | P-2745cbbfbcad | yes on 2026-10-30\n"
          "policy off-date | A1 | P-3ee5198a9319\n"
          "policy off-date | R1 | P-3ee5198a9319\n"
          "policy off-date | T1 | P-3ee5198a9319\n"
          "policy | off_date_citations=3 findings_affected=3\n"
          "findings=7 | " + NOTE + "\n")


def counts(text):
    return {k: int(v) for k, v in (kv.split("=") for kv in text.split())}


def from_text(text):  # the document rebuilt from the text lines alone, independently of the tool
    lines = text.splitlines()
    doc = {"schema": "flock-compare-summary-v1", "review_required": True, "note": NOTE, "topics": [], "law": {"uncited_ids": []},
           "policy": {"uncited_ids": [], "governs": [], "off_date": []}, "facts": {}}
    for head, _, rest in (line.partition(" | ") for line in lines[:-1]):
        parts = rest.split(" | ")
        if head == "topic":
            doc["topics"].append({"topic": parts[0], "claim_class": parts[1], **counts(parts[2])})
        elif head in ("law", "policy", "facts"):
            doc[head].update(counts(rest))
        elif head in ("law uncited", "policy uncited"):
            doc[head.split()[0]]["uncited_ids"].append(rest)
        elif head == "policy governs":
            state, _, doc["date"] = parts[1].rpartition(" on ")
            doc["policy"]["governs"].append({"policy": parts[0], "state": state})
        else:
            doc["policy"]["off_date"].append({"finding_id": parts[0], "policy": parts[1]})
            assert head == "policy off-date", head
    total, _, note = lines[-1].partition(" | ")
    assert note == NOTE, note
    doc["findings"] = counts(total)["findings"]
    return doc


class CompareSummaryJsonTest(unittest.TestCase):
    setUp, facts_import, summary, law_row, expected = (base.CompareSummaryTest.setUp, base.CompareSummaryTest.facts_import,
                                                       base.CompareSummaryTest.summary, base.CompareSummaryTest.law_row,
                                                       base.CompareSummaryTest.expected)

    def doc(self):
        code, out = self.summary("--json")
        self.assertEqual(code, 0, out)
        document = json.loads(out)
        self.assertEqual(out, json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n")  # one document only
        self.assertIs(document["review_required"], True)
        return document

    def text_and_doc(self, recompute=True):  # the text and the JSON of one run state must say the same thing
        code, text = self.summary()
        self.assertEqual((code, text), (0, self.expected() if recompute else text))  # expected() assumes the 2026-10-30 date
        document = self.doc()
        self.assertEqual(document, from_text(text))
        return text, document

    def set_refs(self, **by_finding):
        rows = base.table(self.findings)
        for rec in rows[1:]:
            rec[rows[0].index("policy_refs")] = by_finding.get(rec[0], rec[rows[0].index("policy_refs")])
        self.write(self.findings, rows)

    def write(self, path, rows):
        with open(path, "w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(rows)

    def set_date(self, value, drop=False):
        meta = json.loads((self.run_dir / "RUN.json").read_text(encoding="utf-8"))
        meta = {k: v for k, v in meta.items() if k != "date"} if drop else {**meta, "date": value}
        (self.run_dir / "RUN.json").write_text(json.dumps(meta), encoding="utf-8")

    def test_text_byte_identical_to_main_and_json_matches(self):
        self.assertEqual(self.summary(), (0, GOLDEN))
        text, document = self.text_and_doc()
        self.assertEqual((document["date"], document["findings"], len(document["topics"])), ("2026-10-30", 7, 7))
        self.assertEqual(document["topics"][0], {"topic": "audits", "claim_class": "record-gap", "strong": 1, "medium": 0, "weak": 0,
                                                 "unreviewed_cited": 0})
        self.assertEqual(sorted(document), ["date", "facts", "findings", "law", "note", "policy", "review_required", "schema", "topics"])

    def test_uncited_rules_and_policies_two_each(self):
        self.law_row(base.VERIFIED)
        pid = "P-" + hashlib.sha256("\x1f".join((AGENCY, "436", "test copy", "c" * 64)).encode()).hexdigest()[:12]
        base.append(self.agency / "POLICY.csv", [pid, "Sample City Police Department", "436", "test copy", "c" * 64, "2025-01-01",
                                                  "unverified", "", "", "", "tester", "2026-10-30", "unreviewed"])
        _, document = self.text_and_doc()
        self.assertEqual(document["law"], {"rules": 4, "cited": 2, "uncited": 2, "attorney_review_cited": 0,
                                           "uncited_ids": sorted([base.VERIFIED, "ca-oag-2023-dle-06-out-of-state-sharing-guidance"])})
        self.assertEqual({k: document["policy"][k] for k in ("rows", "cited", "uncited", "uncited_ids")},
                         {"rows": 3, "cited": 1, "uncited": 2, "uncited_ids": sorted([NEW, pid])})
        self.assertEqual(document["policy"]["governs"][0], {"policy": pid, "state": "not established"})  # footer-date order

    def test_off_date_rows_counts_and_distinct_findings(self):
        self.set_refs(A1=OLD + "#435.6(c); " + OLD + "#435.5", R1=NEW + "#435.5")  # A1 twice on one row; R1 on NEW
        _, document = self.text_and_doc(recompute=False)  # expected() prints one off-date line per clause, not per row
        self.assertEqual(document["policy"]["off_date"], [{"finding_id": "A1", "policy": OLD}, {"finding_id": "T1", "policy": OLD}])
        self.assertEqual((document["policy"]["off_date_citations"], document["policy"]["findings_affected"]), (3, 2))
        self.set_date("2026-10-12")  # the day before NEW governs: R1's NEW citation is off-date too
        self.assertEqual(run("flock_compare.py", "--check", str(self.run_dir))[0], 0)
        _, document = self.text_and_doc(recompute=False)
        self.assertIn({"policy": NEW, "state": "no"}, document["policy"]["governs"])
        self.assertEqual(document["policy"]["off_date"][1], {"finding_id": "R1", "policy": NEW})
        self.assertEqual((document["date"], document["policy"]["off_date_citations"], document["policy"]["findings_affected"]),
                         ("2026-10-12", 4, 3))

    def refused(self, line):
        for extra in (("--json",), ()):
            self.assertEqual(self.summary(*extra), (1, line + "\n"))

    def test_check_failure_refused_no_json(self):
        rows = base.table(self.findings)
        rows[1][rows[0].index("fact_ids")] = "F-000000000000"
        self.write(self.findings, rows)
        self.refused("summary refused: errors=1 (run --check)")
        shutil.copy(FIXTURES / "findings-sample.csv", self.findings)
        (self.agency / "POLICY.csv").unlink()  # a store error: refused the same way
        code, out = self.summary("--json")
        self.assertEqual((code, out.startswith("summary refused: errors="), out.count("\n"), "{" in out), (1, True, 1, False))
        self.assertEqual(run("flock_compare.py", "--summary", str(self.agency), "--json"), (1, "summary refused: errors=1 (run --check)\n"))

    def test_bad_date_and_io_refused_no_json(self):
        for bad in ("2026-13-01", "", "20261030", None, 20261030):
            with self.subTest(date=bad):
                self.set_date(bad)
                self.refused("summary refused: RUN.json date")
        self.set_date(None, drop=True)
        self.refused("summary refused: RUN.json date")
        self.findings.write_bytes(b"finding_id\n\xff\n")
        self.refused("io: UnicodeDecodeError")

    def test_planted_text_never_in_json(self):
        extra = self.tmp / "planted.csv"
        shutil.copy(FIXTURES / "compare-facts.csv", extra)
        base.append(extra, ["Sample City Police Department", "REQ-101", "audit_reports", "Value zebra-value-7731", "b" * 64, "8",
                            "The okapi-quote-4410 sentence is here."])
        self.assertEqual(self.facts_import(extra), 0)
        fid = next(r["fact_id"] for r in base.records(self.agency / "FACTS.csv") if "zebra" in r["value"])
        base.append(self.findings, ["M1", "audits", "record-gap", "weak", "Claim lemur-claim-9902.", fid, "", OLD + "#heron-clause-1",
                                    "okapi-quote-4410", fid, "Step ibis-step-5511."])
        self.assertEqual(run("flock_compare.py", "--check", str(self.run_dir))[0], 0)  # clean, so the document is printed
        text, document = self.text_and_doc()
        self.assertEqual((document["facts"]["quoted"], document["findings"]), (2, 8))
        out = self.summary("--json")[1]
        for planted in ("zebra", "okapi", "lemur", "heron", "ibis", "Value", "Claim", "Step", "F-", str(self.tmp), AGENCY):
            self.assertNotIn(planted, out)

    def test_usage_combinations_exit_2(self):
        before = self.summary()
        for argv in (["--summary", str(self.run_dir), "--json", "--check", str(self.run_dir)], ["--json"],
                     ["--json", "--check", str(self.run_dir)], ["--summary", str(self.run_dir), "--json", "--golden", "x.csv"],
                     ["--summary", str(self.run_dir), "--json", "--diff", str(self.run_dir), str(self.run_dir)],
                     ["--summary", str(self.run_dir), "--json", "--store", ""]):
            with self.subTest(argv=argv):
                self.assertEqual(run("flock_compare.py", *argv), (2, ""))
        self.assertEqual(self.summary(), before)


if __name__ == "__main__":
    unittest.main()
