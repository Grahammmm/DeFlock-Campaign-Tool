"""flock_compare --summary: which POLICY rows govern on the run date; findings citing an off-date version (RF1200-2)."""
import csv
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tests.flock.test_compare import AGENCY, FIXTURES, run

OLD, NEW = "P-3ee5198a9319", "P-2745cbbfbcad"  # fixture: OLD unverified (footer 2025-06-03), NEW adopted from 2026-10-13
LAST = "findings=7 | counts of what this run cited, not a review verdict"


def records(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def write(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)


class ComparePolicyDatesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp, self.store = Path(tmp.name), Path(tmp.name) / "store"
        self.assertEqual(run("flock_facts.py", "import", str(FIXTURES / "compare-facts.csv"), "--store", str(self.store),
                             "--added-by", "tester", "--date", "2026-10-30")[0], 0)
        self.agency = self.store / "agencies" / AGENCY
        shutil.copy(FIXTURES / "law-sample.csv", self.store / "LAW.csv")
        self.policy = self.agency / "POLICY.csv"
        shutil.copy(FIXTURES / "policy-sample.csv", self.policy)
        self.run_dir = self.agency / "runs" / "2026-10-30"
        self.assertEqual(run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", AGENCY, "--date",
                             "2026-10-30", "--provider", "anthropic", "--model", "claude-opus-5-5",
                             "--applicability", "Sample city police; local ALPR policy applies")[0], 0)
        self.meta = json.loads((self.run_dir / "RUN.json").read_text(encoding="utf-8"))
        self.meta.pop("inputs")  # legacy run, so POLICY.csv windows can be edited per test
        self.set_meta()
        self.findings = self.run_dir / "FINDINGS.csv"
        shutil.copy(FIXTURES / "findings-sample.csv", self.findings)

    def set_meta(self, **changes):
        (self.run_dir / "RUN.json").write_text(json.dumps({**self.meta, **changes}), encoding="utf-8")

    def window(self, start, end=""):  # NEW's governs_from / governs_to
        rows = records(self.policy)
        rows[2][rows[0].index("governs_from")], rows[2][rows[0].index("governs_to")] = start, end
        write(self.policy, rows)

    def refs(self, **by_finding):
        rows = records(self.findings)
        for rec in rows[1:]:
            rec[rows[0].index("policy_refs")] = by_finding.get(rec[0], rec[rows[0].index("policy_refs")])
        write(self.findings, rows)

    def summary(self):
        code, out = run("flock_compare.py", "--summary", str(self.run_dir))
        return code, [line for line in out.splitlines() if line.startswith(("policy governs", "policy off-date",
                                                                             "policy | off_date"))], out

    def test_golden(self):
        code, lines, out = self.summary()
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["policy governs | %s | not established on 2026-10-30" % OLD,
                                 "policy governs | %s | yes on 2026-10-30" % NEW,  # sorted by footer date, not id
                                 "policy off-date | A1 | " + OLD, "policy off-date | R1 | " + OLD,
                                 "policy off-date | T1 | " + OLD, "policy | off_date_citations=3 findings_affected=3"])
        self.assertEqual(out.splitlines()[-7:], lines + [LAST])  # just before the summary's last line

    def test_window_edges(self):
        self.refs(A1=NEW + "#435.6(c)")
        for start, end, on, state in (("2026-10-13", "2026-10-30", "2026-10-30", "yes"),  # governs_to = run date
                                      ("2026-10-30", "", "2026-10-30", "yes"),  # governs_from = run date
                                      ("2026-10-31", "", "2026-10-30", "no"),  # starts the day after
                                      ("2026-10-13", "2026-10-29", "2026-10-30", "no"),  # expired the day before
                                      ("2026-10-13", "", "2026-10-12", "no"),  # RUN.json date, not the folder name
                                      ("2025-01-01", "", "2026-10-30", "yes")):  # open end, started before
            with self.subTest(start=start, end=end, on=on):
                self.window(start, end)
                self.set_meta(date=on)
                self.assertEqual(run("flock_compare.py", "--check", str(self.run_dir))[0], 0)
                code, lines, _ = self.summary()
                self.assertEqual(code, 0)
                self.assertIn("policy governs | %s | %s on %s" % (NEW, state, on), lines)
                self.assertEqual("policy off-date | A1 | " + NEW in lines, state == "no")
                self.assertEqual(lines[-1], "policy | off_date_citations=%d findings_affected=%d"
                                 % ((3, 3) if state == "no" else (2, 2)))

    def test_clean_run(self):
        self.refs(A1=NEW + "#435.6(c)", R1=NEW + "#435.5", T1=NEW + "#435.4(d)")
        code, lines, _ = self.summary()
        self.assertEqual((code, [x for x in lines if "off-date" in x]), (0, []))
        self.assertEqual(lines[-1], "policy | off_date_citations=0 findings_affected=0")

    def test_two_refs_one_finding_and_two_rows(self):
        self.refs(A1=OLD + "#435.6(c); " + OLD + "#435.5", R1=NEW + "#435.5; " + OLD + "#435.4(d)")
        code, lines, _ = self.summary()
        self.assertEqual(code, 0)
        self.assertEqual(lines[2:], ["policy off-date | A1 | " + OLD, "policy off-date | R1 | " + OLD,
                                     "policy off-date | T1 | " + OLD, "policy | off_date_citations=4 findings_affected=3"])
        pid = "P-" + hashlib.sha256("\x1f".join((AGENCY, "436", "test copy", "c" * 64)).encode()).hexdigest()[:12]
        with open(self.policy, "a", encoding="utf-8", newline="") as handle:  # a second unverified row
            csv.writer(handle, lineterminator="\n").writerow([pid, "Sample City Police Department", "436", "test copy", "c" * 64,
                                                              "2025-01-01", "unverified", "", "", "", "tester", "2026-10-30",
                                                              "unreviewed"])
        self.refs(A1=OLD + "#435.6(c); " + pid + "#436.1")
        code, lines, _ = self.summary()
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "policy governs | %s | not established on 2026-10-30" % pid)
        self.assertEqual(lines[3:6], ["policy off-date | A1 | " + OLD, "policy off-date | A1 | " + pid,
                                      "policy off-date | R1 | " + OLD])
        self.assertEqual(lines[-1], "policy | off_date_citations=4 findings_affected=3")  # A1 2 rows, R1 1, T1 1

    def test_refused_stays_refused(self):
        rows = records(self.findings)
        rows[1][rows[0].index("fact_ids")] = "F-000000000000"
        write(self.findings, rows)
        self.assertEqual(self.summary()[0::2], (1, "summary refused: errors=1 (run --check)\n"))
        shutil.copy(FIXTURES / "findings-sample.csv", self.findings)
        for bad in ("2026-13-01", "", "20261030", None, 20261030):
            with self.subTest(date=bad):
                self.set_meta(date=bad)
                self.assertEqual(run("flock_compare.py", "--check", str(self.run_dir))[0], 0)  # --check does not read date
                self.assertEqual(self.summary()[0::2], (1, "summary refused: RUN.json date\n"))
        self.meta.pop("date")
        self.set_meta()
        self.assertEqual(self.summary()[0::2], (1, "summary refused: RUN.json date\n"))

    def test_no_leak_no_write(self):
        before = sorted((str(p.relative_to(self.tmp)), p.lstat().st_size, p.lstat().st_mtime_ns) for p in self.tmp.rglob("*"))
        code, lines, out = self.summary()
        self.assertEqual((code, len(lines)), (0, 6))
        rows, pol = records(self.findings), records(self.policy)
        texts = [rec[rows[0].index(k)] for rec in rows[1:] for k in ("claim", "quote", "next_step")]
        texts += [rec[rows[0].index("policy_refs")].partition("#")[2] for rec in rows[1:]]  # clause text
        texts += [rec[pol[0].index(k)] for rec in pol[1:] for k in ("version_label", "adoption_evidence", "governs_from")]
        texts = {t for t in texts if t}
        self.assertGreater(len(texts), 15)
        self.assertEqual([t for t in sorted(texts) if t in out], [])
        self.assertEqual(sorted((str(p.relative_to(self.tmp)), p.lstat().st_size, p.lstat().st_mtime_ns)
                                for p in self.tmp.rglob("*")), before)
