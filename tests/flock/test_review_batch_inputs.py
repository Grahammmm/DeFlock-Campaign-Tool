import contextlib, io, unittest
from pathlib import Path

from tests.flock import test_review_batch as base
from tests.flock.test_review import ROOT, tree
from tests.flock.test_review_lock import load

batch = load("_rf1105_batch", ROOT / "tools" / "flock_review_batch.py")
REAL = "out: --out must be a real folder"
# Captured from the same call on main 7b3edae (clean roster, --out a new folder, --run); {TMP} is the test's temp folder.
GOLD_OUT = ("golden ok keys=10\nrun sample-city-police-department rc=3 state=waiting\nrun second-city-police rc=3 state=waiting\n"
            "sample-city-police-department waiting 5/28 next=fact-drafts\nsecond-city-police waiting 5/28 next=fact-drafts\n"
            "campus-police held 2/28 next=inventory\n"
            "agencies=3 done=0 waiting=2 in_flight=0 failed=0 invalid=0 held=1 locked=0 ready=0\n")
GOLD_CSV = ("slug,config,state,stages_done,stages_total,next,waiting_for,input_present,author_family,verifier_family,run_date\n"
            "sample-city-police-department,{TMP}/a0/review.json,waiting,5,28,fact-drafts,fact-drafts,no,,,2026-10-30\n"
            "second-city-police,{TMP}/a1/review.json,waiting,5,28,fact-drafts,fact-drafts,no,,,2026-10-30\n"
            "campus-police,{TMP}/a2/review.json,held,2,28,inventory,,,,,2026-10-30\n")


class ReviewBatchInputsTest(unittest.TestCase):
    setUp, world = base.FlockReviewBatchTest.setUp, base.FlockReviewBatchTest.world

    def main(self, *words, roster=base.ROSTER, out=None):
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown), contextlib.redirect_stderr(io.StringIO()):
            code = batch.main(["status", str(self.list), "--roster", str(roster), "--out", str(out or self.out), *map(str, words)])
        return code, shown.getvalue()

    def roster(self, *rows):  # the fixture header and its first data row, then `rows`
        path = self.tmp / "roster.csv"
        path.write_text("".join(r + "\n" for r in base.ROSTER.read_text(encoding="utf-8").splitlines()[:2] + list(rows)), encoding="utf-8")
        return path

    def refused(self, line, *words, **kw):  # refused with `line`, before any agency moved or any status file was written
        before = [tree(o) for o in self.outs]
        code, shown = self.main("--run", *self.gate, *words, **kw)
        self.assertEqual((code, shown), (1, line + "\n"))
        self.assertEqual([tree(o) for o in self.outs], before)
        self.assertFalse([o for o in self.outs if (o / "REVIEW-STATE.json").exists()])

    def test_repeated_roster_slug_refused_before_run(self):
        self.world()
        # the later row would have held the agency (conflicting in_scope); neither row may silently win
        self.refused("roster row 3: slug already used on row 2",
                     roster=self.roster("Sample City PD,sample-city-police-department,hold,Other,"))
        self.refused("roster row 4: slug already used on row 3",
                     roster=self.roster("Campus,campus-police,hold,x,", "Campus,  campus-police ,yes,x,"))
        self.assertFalse(self.out.exists())

    def test_empty_roster_slug_refused(self):
        self.world()
        self.refused("roster row 3: slug is empty", roster=self.roster("Nameless,,yes,x,"))
        self.refused("roster row 4: slug is empty", roster=self.roster("Campus,campus-police,hold,x,", "Blank,   ,yes,x,"))
        self.assertFalse(self.out.exists())

    def test_out_regular_file_refused_before_run(self):
        self.world()
        self.out.write_text("not a folder\n")
        self.refused(REAL)
        self.assertEqual(self.out.read_text(), "not a folder\n")

    def test_out_symlink_refused(self):
        self.world()
        target = self.tmp / "elsewhere"
        target.mkdir()
        for link in (self.tmp / "link", self.tmp / "dangling"):
            link.symlink_to(target if link.name == "link" else self.tmp / "nowhere")
            self.refused(REAL, out=link)
            self.refused(REAL, out=str(link) + "/")
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((self.tmp / "nowhere").exists())

    def test_out_parent_must_be_a_real_folder(self):
        self.world()
        self.refused(REAL, out=self.tmp / "missing" / "batch")
        (self.tmp / "file").write_text("x")
        self.refused(REAL, out=self.tmp / "file" / "batch")
        (self.tmp / "real").mkdir()
        (self.tmp / "via").symlink_to(self.tmp / "real")
        self.refused(REAL, out=self.tmp / "via" / "batch")
        self.assertFalse((self.tmp / "missing").exists() or (self.tmp / "real" / "batch").exists())

    def test_new_out_with_real_parent_written(self):
        self.world()
        code, shown = self.main()
        self.assertEqual(code, 0, shown)
        self.assertEqual((self.out / "BATCH-STATUS.csv").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.main()[0], 0)  # an existing real folder is fine too

    def test_plan_out_is_usage(self):
        self.world()
        self.out.mkdir()
        for words in (["--out", self.out], ["--out=%s" % self.out], ["--ou", self.out]):
            with contextlib.redirect_stdout(io.StringIO()) as shown, contextlib.redirect_stderr(io.StringIO()) as err, \
                    self.assertRaises(SystemExit) as stop:
                batch.main(["plan", str(self.list), "--roster", str(base.ROSTER), *map(str, self.gate + words)])
            self.assertEqual(stop.exception.code, 2)
            self.assertEqual(shown.getvalue(), "")
            self.assertIn("--out is for status", err.getvalue())
        self.assertEqual(list(self.out.iterdir()), [])

    def test_clean_roster_matches_main(self):
        self.world()
        code, shown = self.main("--run", *self.gate)
        self.assertEqual(code, 0, shown)
        csv_text = (self.out / "BATCH-STATUS.csv").read_text()
        for tmp in (str(self.tmp.resolve()), str(self.tmp)):
            shown, csv_text = shown.replace(tmp, "{TMP}"), csv_text.replace(tmp, "{TMP}")
        self.assertEqual((shown, csv_text), (GOLD_OUT, GOLD_CSV))


if __name__ == "__main__":
    unittest.main()
