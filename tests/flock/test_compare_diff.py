"""flock_compare --diff OLDRUN NEWRUN: findings added, removed and changed by id; old public-ok flagged; read-only (RF1150-4)."""
import csv
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest

from tests.flock.test_compare import AGENCY, FIXTURES, TOOLS, run

_spec = importlib.util.spec_from_file_location("_flock_verify_for_diff", TOOLS / "flock_verify.py")
_verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_verify)
PUBLIC = _verify.PUBLIC  # the PUBLIC-OK.csv header flock_verify.finish writes
TAIL = " | finding ids only, not a review verdict"
SAME = "old=7 new=7 added=0 removed=0 changed=0 unchanged=7 public_ok_affected=-" + TAIL + "\n"
FIVE = ["changed | A1 | claim changed", "changed | N1 | rating strong->medium", "changed | O1 | fact_ids +1 -1",
        "removed | R1 | retention-changes/record-gap strong", "added | Z9 | definitions/record-gap weak"]


def write(path, rows, end="\n"):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator=end).writerows(rows)


def sample():
    with open(FIXTURES / "findings-sample.csv", encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def edit(rows, fid, **fields):  # a copy of rows with the finding fid's fields replaced
    out = [list(r) for r in rows]
    for r in out:
        if r[0] == fid:
            for column, value in fields.items():
                r[rows[0].index(column)] = value
    return out


class CompareDiffTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp, self.store = Path(tmp.name), Path(tmp.name) / "store"
        self.assertEqual(run("flock_facts.py", "import", str(FIXTURES / "compare-facts.csv"), "--store", str(self.store),
                             "--added-by", "tester", "--date", "2026-10-30")[0], 0)
        self.agency = self.store / "agencies" / AGENCY
        shutil.copy(FIXTURES / "law-sample.csv", self.store / "LAW.csv")
        shutil.copy(FIXTURES / "policy-sample.csv", self.agency / "POLICY.csv")
        self.old, self.new = self.agency / "runs" / "2026-10-30", self.agency / "runs" / "2026-10-31"
        for day in ("2026-10-30", "2026-10-31"):
            self.assertEqual(run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", AGENCY, "--date", day,
                                 "--provider", "anthropic", "--model", "claude-opus-5-5", "--applicability", "Sample city")[0], 0)
        self.rows = sample()
        write(self.old / "FINDINGS.csv", self.rows)
        write(self.new / "FINDINGS.csv", self.rows)

    def diff(self, old=None, new=None):
        return run("flock_compare.py", "--diff", str(old or self.old), str(new or self.new))

    def five_changes(self):  # re-rate N1, swap O1's fact, edit A1's claim, drop R1, add Z9
        rows = edit(edit(edit(self.rows, "N1", rating="medium"), "O1", fact_ids="F-090c3d39b556"), "A1",
                    claim="The sample department found no responsive audit schedule.")
        rows = [r for r in rows if r[0] != "R1"] + [["Z9", "definitions", "record-gap", "weak", "The sample record does not define a read.",
                                                      "F-090c3d39b556", "ca-civ-1798.90.52-access-records", "", "", "", "Ask."]]
        write(self.new / "FINDINGS.csv", rows)
        return rows

    def refused(self, reason, old=None, new=None):
        self.assertEqual(self.diff(old, new), (1, "diff refused: " + reason + "\n"))

    def test_same_findings_reordered_crlf_and_spacing_unchanged(self):
        self.assertEqual(self.diff(), (0, SAME))
        rows = edit(self.rows, "N1", claim="  The sample  search log\thas no search-purpose field. ", next_step=" Ask how search purpose is recorded.")
        write(self.new / "FINDINGS.csv", [rows[0]] + rows[:0:-1] + [[], ["", " "]], end="\r\n")  # reversed, CRLF, blank rows
        self.assertEqual(self.diff(), (0, SAME))
        write(self.new / "FINDINGS.csv", edit(self.rows, "N1", fact_ids="F-090c3d39b556; F-090c3d39b556 "))  # same id set
        self.assertEqual(self.diff(), (0, SAME))

    def test_each_change_kind_exact_lines(self):
        self.five_changes()
        code, out = self.diff()
        self.assertEqual((code, out.splitlines()), (0, FIVE + ["old=7 new=7 added=1 removed=1 changed=3 unchanged=3 public_ok_affected=-" + TAIL]))
        write(self.new / "FINDINGS.csv", edit(self.rows, "T1", rating="weak", claim_class="duty-concern", topic="audits",
                                              rule_ids="ca-civ-1798.90.52-access-records", policy_refs="", quote="x",
                                              quote_fact_id="F-a71d059d7a51", next_step="Ask again."))
        self.assertEqual(self.diff()[1].splitlines()[0], "changed | T1 | rating strong->weak, claim_class record-gap->duty-concern, "
                         "topic withheld-training->audits, rule_ids +1 -0, policy_refs +0 -1, quote changed, quote_fact_id changed, "
                         "next_step changed")
        write(self.new / "FINDINGS.csv", edit(self.rows, "N1", rating="Strongly unlawful", topic="Officer Smith"))  # unchecked labels
        self.assertEqual(self.diff()[1].splitlines()[0], "changed | N1 | rating strong->invalid, topic na-search-purposes->invalid")

    def test_old_public_ok_flagged(self):
        self.five_changes()
        write(self.old / "PUBLIC-OK.csv", [PUBLIC, ["N1", "x"], ["R1", "x"], ["Z9", "x"], ["O2", "x"]])  # Z9 added, O2 unchanged
        write(self.new / "PUBLIC-OK.csv", [PUBLIC, ["A1", "x"]])  # only the old run's file counts
        flagged = [x + " [old public-ok]" if x.split(" | ")[1] in ("N1", "R1") else x for x in FIVE]
        code, out = self.diff()
        self.assertEqual((code, out.splitlines()), (0, flagged + ["old=7 new=7 added=1 removed=1 changed=3 unchanged=3 public_ok_affected=2" + TAIL]))
        write(self.old / "PUBLIC-OK.csv", [PUBLIC])  # header only: none affected, but known
        self.assertTrue(self.diff()[1].endswith("public_ok_affected=0" + TAIL + "\n"))

    def test_public_ok_unreadable_refused(self):
        bad = {"wrong header": [["finding_id", "topic"], ["N1", "x"]], "empty": []}
        for name, rows in bad.items():
            with self.subTest(name=name):
                write(self.old / "PUBLIC-OK.csv", rows)
                self.refused("old PUBLIC-OK.csv unreadable")
        (self.old / "PUBLIC-OK.csv").write_bytes(b"finding_id,topic\n\xff\xfe\n")
        self.refused("old PUBLIC-OK.csv unreadable")
        (self.old / "PUBLIC-OK.csv").unlink()
        (self.old / "PUBLIC-OK.csv").mkdir()
        self.refused("old PUBLIC-OK.csv unreadable")

    def test_old_run_failing_check_still_diffs(self):
        write(self.old / "FINDINGS.csv", edit(self.rows, "N1", fact_ids="F-000000000000"))  # a fact since corrected away
        self.assertEqual(run("flock_compare.py", "--check", str(self.old))[0], 1)
        code, out = self.diff()
        self.assertEqual((code, out.splitlines()[0]), (0, "changed | N1 | fact_ids +1 -1"))
        self.assertNotIn("F-", out)

    def test_refusals_one_line_exit_1(self):
        other = self.store / "agencies" / "other-agency" / "runs" / "2026-10-31"
        store2 = self.tmp / "store2" / "agencies" / AGENCY / "runs" / "2026-10-31"
        for folder in (other, store2):
            folder.mkdir(parents=True)
            write(folder / "FINDINGS.csv", self.rows)
        self.refused("same run folder twice", self.old, self.old)
        self.refused("same run folder twice", self.old, self.old / ".." / self.old.name)
        self.refused("different agencies", self.old, other)
        self.refused("different stores", self.old, store2)
        for old, new in ((self.agency, self.new), (self.old, self.agency / "runs"), (self.store, self.new)):
            self.refused("not a <store>/agencies/<slug>/runs/<date> folder", old, new)
        cases = {"old FINDINGS.csv missing or header wrong": ("old", [["finding_id", "topic"]] + self.rows[1:]),
                 "new row 3: wrong column count": ("new", self.rows[:2] + [self.rows[2][:-1]] + self.rows[3:]),
                 "old row 4: finding_id is empty or duplicated": ("old", edit(self.rows, "O1", finding_id="N1")),
                 "new row 2: finding_id is empty or duplicated": ("new", edit(self.rows, "N1", finding_id=" "))}
        for reason, (side, rows) in cases.items():
            with self.subTest(reason=reason):
                write(self.old / "FINDINGS.csv", rows if side == "old" else self.rows)
                write(self.new / "FINDINGS.csv", rows if side == "new" else self.rows)
                self.refused(reason)
        (self.new / "FINDINGS.csv").unlink()
        self.refused("new FINDINGS.csv missing or header wrong")

    def test_usage_exit_2(self):
        for extra in (["--diff", str(self.old)], ["--diff", str(self.old), str(self.new), "--check", str(self.old)],
                      ["--diff", str(self.old), str(self.new), "--summary", str(self.old)], ["--check", str(self.old), "--diff"],
                      ["scaffold", "--diff", str(self.old), str(self.new)], ["--diff", str(self.old), str(self.new), "--store", ""]):
            with self.subTest(extra=extra):
                self.assertEqual(run("flock_compare.py", *extra), (2, ""))

    def test_no_text_or_id_leaks(self):
        new = self.five_changes()
        write(self.old / "PUBLIC-OK.csv", [PUBLIC, ["N1"]])
        code, out = self.diff()
        self.assertEqual(code, 0)
        texts = {c for rows in (self.rows, new) for r in rows[1:] for c in r[4:]}  # claim .. next_step, incl. ids and refs
        texts |= {x.strip() for t in set(texts) for x in t.split(";")} | {t.partition("#")[2] for t in texts}
        texts = {t for t in texts if len(t) >= 4}
        self.assertGreater(len(texts), 20)
        self.assertEqual([t for t in sorted(texts) if t in out], [])
        self.assertNotIn("F-", out)
        self.assertNotIn("P-", out)

    def test_writes_nothing(self):
        def snapshot():
            return sorted((str(p.relative_to(self.tmp)), p.lstat().st_size, p.lstat().st_mtime_ns) for p in self.tmp.rglob("*"))
        self.five_changes()
        write(self.old / "PUBLIC-OK.csv", [PUBLIC, ["N1"]])
        before = snapshot()
        self.assertEqual(self.diff()[0], 0)
        self.assertEqual(self.diff(self.old, self.old)[0], 1)
        self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
