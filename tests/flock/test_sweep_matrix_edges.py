"""RF1058-6: flock_sweep matrix refuses an unusable finished verification before it creates or replaces anything.
The damage is applied after the status pass (the assess -> matrix window), so the run was 'finished' when assessed."""
import contextlib, csv, importlib.util, io, os, unittest
from unittest import mock
from tests.flock import test_sweep as base

FIX, AGENCY, SECOND, NAME = base.FIXTURES, base.AGENCY, "second-city-police", "Sample City Police Department"
_spec = importlib.util.spec_from_file_location("_rf1058_sweep", base.TOOLS / "flock_sweep.py")
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)
CELLS = (("audits", "record-gap", "strong"), ("na-search-purposes", "duty-concern", "medium"),
         ("na-search-purposes", "record-gap", "strong"), ("outside-agency-access", "duty-concern", "rejected"),
         ("outside-agency-access", "record-gap", "strong"), ("retention-changes", "record-gap", "medium"),
         ("withheld-training", "record-gap", "strong"))
MATRIX = "topic,claim_class,%s,%s\n" % (AGENCY, SECOND) + "".join("%s,%s,%s,%s\n" % (t, c, r, r) for t, c, r in CELLS)
LINE = "matrix: %s verification unusable (errors=%d unverified=%d bad_rating=%d)"


class SweepMatrixEdges(unittest.TestCase):
    setUp, finish, sweep = base.FlockSweepTest.setUp, base.FlockSweepTest.finish, base.FlockSweepTest.sweep

    def build(self):  # both roster agencies finished: the sample fixtures re-keyed to Second City Police
        self.finish()
        ids = {}
        for name, key, idf in (("compare-facts.csv", "c", sweep.facts_tool.fact_id), ("policy-sample.csv", "p", sweep.law_policy.derived_id)):
            with open(FIX / name, newline="") as handle:
                rows = list(csv.DictReader(handle))
            for r in rows:
                ids[r.get("policy_row_id") or idf(r)] = idf(dict(r, agency="Second City Police"))
            setattr(self, key, (FIX / name).read_text().replace(NAME, "Second City Police"))
        for old, new in ids.items():
            self.p = self.p.replace(old, new)
        (self.tmp / "f2.csv").write_text(self.c)
        self.assertEqual(base.run("flock_facts.py", "import", str(self.tmp / "f2.csv"), "--store", str(self.store),
                                  "--added-by", "tester", "--date", "2026-10-30")[0], 0)
        (self.store / "agencies" / SECOND / "POLICY.csv").write_text(self.p)
        self.assertEqual(base.run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", SECOND, "--date",
                                  "2026-10-30", "--provider", "anthropic", "--model", "m", "--applicability", "Second city")[0], 0)
        run2 = self.run2 = self.store / "agencies" / SECOND / "runs" / "2026-10-30"
        def swap(text):
            for old, new in ids.items():
                text = text.replace(old, new)
            return text
        (run2 / "FINDINGS.csv").write_text(swap((FIX / "findings-sample.csv").read_text()))
        self.assertEqual(base.run("flock_verify.py", "start", str(run2), "--provider", "openai", "--model", "m")[0], 0)
        (run2 / "VERIFY.csv").write_text(swap((FIX / "verify-sample.csv").read_text()))
        self.assertEqual(base.run("flock_verify.py", "finish", str(run2))[0], 0)

    def matrix(self, *damage, roster=base.ROSTER, out=None):
        real, out, buf = sweep.status_rows, out or self.tmp / "m", io.StringIO()
        def rows(*args):  # status pass first, then the run is damaged before matrix() reads it
            found = real(*args)
            for run, old, new in damage:
                (run / "VERIFY.csv").write_text((run / "VERIFY.csv").read_text().replace(old, new))
            return found
        with mock.patch.object(sweep, "status_rows", rows), contextlib.redirect_stdout(buf):
            code = sweep.main(["matrix", "--watchlist", base.WATCH, "--roster", roster, "--store", str(self.store), "--out", str(out)])
        return code, buf.getvalue()

    def refused(self, expected, *damage, **kw):
        code, out = self.matrix(*damage, **kw)
        self.assertEqual((code, out), (1, "\n".join(expected) + "\n"))
        self.assertFalse((self.tmp / "m").exists())  # --out not created, nothing written

    def test_happy_path_bytes_unchanged(self):
        self.build()
        self.assertEqual(self.matrix(), (0, "matrix rows=7 agencies=2\n"))
        self.assertEqual((self.tmp / "m" / "COMPARE-MATRIX.csv").read_text(), MATRIX)
        self.assertEqual((self.tmp / "m" / "COMPARE.md").read_text().splitlines()[-1], "- not finished: campus-police;parks")
        self.assertEqual(self.sweep("matrix", "--out", str(self.tmp / "cli")), (0, "matrix rows=7 agencies=2\n"))
        self.assertEqual((self.tmp / "cli" / "COMPARE-MATRIX.csv").read_bytes(), MATRIX.encode())

    def test_header_wrong_refuses_and_good_agency_not_written(self):
        self.build()
        self.refused([LINE % (AGENCY, 1, 7, 0)], (self.run_dir, "finding_id,verdict", "finding,verdict"))

    def test_finding_without_verification_row(self):
        self.build()
        t1 = "\nT1,confirm,F-a71d059d7a51,6,strong,,Page 6 states that the training materials were withheld."
        self.refused([LINE % (AGENCY, 1, 1, 0)], (self.run_dir, t1, ""))

    def test_rating_outside_order_but_reject_without_rating_ok(self):
        self.build()  # O2 is a reject row with an empty new_rating: not counted
        self.refused([LINE % (AGENCY, 1, 0, 1)], (self.run_dir, ",2,strong,,Page 2", ",2,excellent,,Page 2"))

    def test_two_bad_agencies_roster_order(self):
        self.build()
        lines = (FIX / "roster-sample.csv").read_text().splitlines(True)
        (self.tmp / "r.csv").write_text("".join([lines[0], lines[2], lines[1]] + lines[3:]))
        damage = ((self.run_dir, "finding_id,verdict", "x,verdict"), (self.run2, ",2,strong,,Page 2", ",2,excellent,,Page 2"))
        self.refused([LINE % (SECOND, 1, 0, 1), LINE % (AGENCY, 1, 7, 0)], *damage, roster=str(self.tmp / "r.csv"))

    def test_existing_outputs_kept_on_refusal(self):
        self.build()
        out = self.tmp / "keep"
        out.mkdir()
        (out / "COMPARE.md").write_text("old matrix\n")
        os.utime(out / "COMPARE.md", ns=(1, 1_000_000_000))
        code, _ = self.matrix((self.run_dir, "finding_id,verdict", "x,verdict"), out=out)
        self.assertEqual((code, sorted(p.name for p in out.iterdir())), (1, ["COMPARE.md"]))
        self.assertEqual(((out / "COMPARE.md").read_bytes(), (out / "COMPARE.md").stat().st_mtime_ns), (b"old matrix\n", 1_000_000_000))

    def test_no_identifier_quote_or_path_leaks(self):
        self.build()
        for name in ("FINDINGS.csv", "VERIFY.csv"):
            (self.run2 / name).write_text((self.run2 / name).read_text().replace("A1,", "F-SECRET-1,"))
        self.assertEqual(self.sweep("status")[1].count(",finished,"), 2)
        code, out = self.matrix((self.run2, ",4,strong,,Page 4", ",4,excellent,,Page 4"))
        self.assertEqual((code, out.count("\n"), out.startswith(LINE.split("%")[0] + SECOND)), (1, 1, True), out)
        for leak in ("F-SECRET-1", "excellent", "Page ", str(self.store), str(self.tmp)):
            self.assertNotIn(leak, out)

    def test_not_finished_agency_ignored(self):
        self.build()  # a broken VERIFY.csv at status time makes the run verify-open, so the matrix skips it
        (self.run2 / "VERIFY.csv").write_text("finding,verdict\n")
        self.assertEqual(self.sweep("matrix", "--out", str(self.tmp / "m")), (0, "matrix rows=7 agencies=1\n"))
        self.assertEqual((self.tmp / "m" / "COMPARE.md").read_text().splitlines()[-1],
                         "- not finished: %s;campus-police;parks" % SECOND)


if __name__ == "__main__":
    unittest.main()
