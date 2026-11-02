"""flock_compare --diff OLDRUN NEWRUN --json: the run diff as one flock-compare-diff-v1 document; refusals and text unchanged (RF0420-8)."""
import json
import unittest

from tests.flock import test_compare_diff as base
from tests.flock.test_compare import run

PUBLIC, edit, write = base.PUBLIC, base.edit, base.write
NONE = {"label_changes": [], "id_sets": [], "texts_changed": []}
# case 1 as text, written out before the --json change: the text renderer must still print exactly this
GOLDEN = ("changed | N1 | rating strong->medium, fact_ids +1 -0, claim changed\n"
          "removed | R1 | retention-changes/record-gap strong\n"
          "added | Z9 | definitions/record-gap weak\n"
          "old=3 new=3 added=1 removed=1 changed=1 unchanged=1 public_ok_affected=- | finding ids only, not a review verdict\n")


class CompareDiffJsonTest(unittest.TestCase):
    def setUp(self):
        base.CompareDiffTest.setUp(self)  # the same synthetic store and two scaffolded runs as test_compare_diff
        self.three = [r for r in self.rows if r[0] in ("finding_id", "N1", "O1", "R1")]
        write(self.old / "FINDINGS.csv", self.three)
        write(self.new / "FINDINGS.csv", self.three)

    def diff(self, *extra, old=None, new=None):
        return run("flock_compare.py", "--diff", str(old or self.old), str(new or self.new), *extra)

    def doc(self, old=None, new=None):
        code, out = self.diff("--json", old=old, new=new)
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(out, json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n")  # one document, nothing else
        return document

    def case1(self):  # N1 re-rated, one more fact, new claim; R1 removed; Z9 added; O1 unchanged
        rows = edit(self.three, "N1", rating="medium", fact_ids="F-090c3d39b556; F-76dbe8a9b4ac",
                    claim="The sample search log still has no search-purpose field.")
        rows = [r for r in rows if r[0] != "R1"] + [["Z9", "definitions", "record-gap", "weak", "The sample record does not define a read.",
                                                      "F-090c3d39b556", "ca-civ-1798.90.52-access-records", "", "", "", "Ask."]]
        write(self.new / "FINDINGS.csv", rows)

    def expected(self, public=None, ok=()):
        def row(kind, fid, labels, **parts):
            return {"kind": kind, "finding_id": fid, "labels": dict(zip(("topic", "claim_class", "rating"), labels)),
                    "old_public_ok": fid in ok, **NONE, **parts}
        return {"schema": "flock-compare-diff-v1", "review_required": True, "note": "finding ids only, not a review verdict",
                "old_count": 3, "new_count": 3, "added": 1, "removed": 1, "changed": 1, "unchanged": 1, "public_ok_affected": public,
                "findings": [row("changed", "N1", ("na-search-purposes", "record-gap", "medium"),
                                 label_changes=[{"field": "rating", "old": "strong", "new": "medium"}],
                                 id_sets=[{"field": "fact_ids", "added": 1, "removed": 0}], texts_changed=["claim"]),
                             row("removed", "R1", ("retention-changes", "record-gap", "strong")),
                             row("added", "Z9", ("definitions", "record-gap", "weak"))]}

    def test_case1_rows_and_totals_match_text(self):
        self.case1()
        document = self.doc()
        self.assertEqual(document, self.expected())
        code, text = self.diff()
        self.assertEqual((code, text), (0, GOLDEN))  # text mode byte-identical to the pre-change output
        totals = dict(kv.split("=") for kv in text.splitlines()[-1].partition(" | ")[0].split())
        self.assertEqual({k: str(document[k if k in ("added", "removed", "changed", "unchanged") else k + "_count"])
                          for k in ("old", "new", "added", "removed", "changed", "unchanged")},
                         {k: v for k, v in totals.items() if k != "public_ok_affected"})
        self.assertEqual((totals["public_ok_affected"], document["public_ok_affected"]), ("-", None))

    def test_old_public_ok_and_affected_count(self):
        self.case1()
        write(self.old / "PUBLIC-OK.csv", [PUBLIC, ["N1", "x"], ["R1", "x"], ["Z9", "x"], ["O1", "x"]])  # Z9 added, O1 unchanged
        write(self.new / "PUBLIC-OK.csv", [PUBLIC, ["Z9", "x"]])  # only the old run's file counts
        self.assertEqual(self.doc(), self.expected(public=2, ok=("N1", "R1")))
        self.assertTrue(self.diff()[1].endswith("public_ok_affected=2 | finding ids only, not a review verdict\n"))
        write(self.old / "PUBLIC-OK.csv", [PUBLIC])  # header only: none affected, but known
        self.assertEqual(self.doc(), self.expected(public=0))

    def test_identical_runs_no_findings(self):
        document = self.doc()
        self.assertEqual((document["findings"], document["unchanged"], document["changed"], document["public_ok_affected"]), ([], 3, 0, None))
        rows = edit(self.three, "N1", claim="  The sample  search log\thas no search-purpose field. ")  # squashed text: unchanged
        write(self.new / "FINDINGS.csv", [rows[0]] + rows[:0:-1], end="\r\n")
        self.assertEqual(self.doc()["findings"], [])

    def test_invalid_labels_never_raw(self):
        write(self.new / "FINDINGS.csv", edit(self.three, "N1", rating="Strongly unlawful", topic="Officer Smith")
              + [["Z9", "Officer Jones", "Bad class", "Super strong", "c", "", "", "", "", "", ""]])
        code, out = self.diff("--json")
        rows = {r["finding_id"]: r for r in json.loads(out)["findings"]}
        self.assertEqual(rows["N1"]["label_changes"], [{"field": "rating", "old": "strong", "new": "invalid"},
                                                       {"field": "topic", "old": "na-search-purposes", "new": "invalid"}])
        self.assertEqual(rows["N1"]["labels"], {"topic": "invalid", "claim_class": "record-gap", "rating": "invalid"})
        self.assertEqual(rows["Z9"]["labels"], dict.fromkeys(("topic", "claim_class", "rating"), "invalid"))
        for raw in ("Strongly", "Officer", "Smith", "Jones", "Bad class", "Super"):
            self.assertNotIn(raw, out)

    def refused(self, reason, old=None, new=None):
        code, out = self.diff("--json", old=old, new=new)
        self.assertEqual((code, out), (1, "diff refused: " + reason + "\n"))
        self.assertNotIn("{", out)
        self.assertEqual(self.diff(old=old, new=new), (code, out))  # the same refusal as text mode

    def test_every_refusal_same_line_no_json(self):
        other = self.store / "agencies" / "other-agency" / "runs" / "2026-10-31"
        store2 = self.tmp / "store2" / "agencies" / base.AGENCY / "runs" / "2026-10-31"
        for folder in (other, store2):
            folder.mkdir(parents=True)
            write(folder / "FINDINGS.csv", self.three)
        self.refused("same run folder twice", self.old, self.old)
        self.refused("different agencies", self.old, other)
        self.refused("different stores", self.old, store2)
        self.refused("not a <store>/agencies/<slug>/runs/<date> folder", self.agency, self.new)
        cases = {"old FINDINGS.csv missing or header wrong": ("old", [["finding_id", "topic"]] + self.three[1:]),
                 "new row 3: wrong column count": ("new", self.three[:2] + [self.three[2][:-1]] + self.three[3:]),
                 "old row 3: finding_id is empty or duplicated": ("old", edit(self.three, "O1", finding_id="N1")),
                 "new row 2: finding_id is empty or duplicated": ("new", edit(self.three, "N1", finding_id=" "))}
        for reason, (side, rows) in cases.items():
            with self.subTest(reason=reason):
                write(self.old / "FINDINGS.csv", rows if side == "old" else self.three)
                write(self.new / "FINDINGS.csv", rows if side == "new" else self.three)
                self.refused(reason)
        write(self.new / "FINDINGS.csv", self.three)
        for rows in ([["finding_id", "topic"], ["N1", "x"]], []):
            write(self.old / "PUBLIC-OK.csv", rows)
            self.refused("old PUBLIC-OK.csv unreadable")
        (self.old / "PUBLIC-OK.csv").unlink()
        (self.new / "FINDINGS.csv").unlink()
        self.refused("new FINDINGS.csv missing or header wrong")
        (self.new / "FINDINGS.csv").write_bytes(b"finding_id\n\xff\n")
        self.assertEqual(self.diff("--json"), (1, "io: UnicodeDecodeError\n"))

    def test_json_without_diff_usage_exit_2(self):
        for extra in (["--json"], ["--json", "--summary", str(self.old)], ["--summary", str(self.old), "--json"],
                      ["--json", "--check", str(self.old)], ["--diff", str(self.old), str(self.new), "--json", "--store", ""],
                      ["scaffold", "--json", "--store", str(self.store), "--agency", base.AGENCY, "--date", "2026-11-01",
                       "--provider", "anthropic", "--model", "m", "--applicability", "x"]):
            with self.subTest(extra=extra):
                self.assertEqual(run("flock_compare.py", *extra), (2, ""))
        self.assertFalse((self.agency / "runs" / "2026-11-01").exists())

    def test_no_text_ids_or_paths_in_json(self):
        rows = edit(self.three, "N1", claim="Planted claim zebra-claim-7731.", quote="Planted quote okapi-quote-4410",
                    next_step="Planted step lemur-step-9902", fact_ids="F-090c3d39b556; F-76dbe8a9b4ac")
        write(self.new / "FINDINGS.csv", edit(rows, "O1", policy_refs="P-3ee5198a9319#435.9(z)", rule_ids=""))
        write(self.old / "PUBLIC-OK.csv", [PUBLIC, ["N1", "x"]])
        code, out = self.diff("--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["findings"]), 2)
        for planted in ("zebra", "okapi", "lemur", "Planted", "F-", "P-", "435.", "ca-civ", str(self.tmp), base.AGENCY, "2026-10-3"):
            self.assertNotIn(planted, out)


if __name__ == "__main__":
    unittest.main()
