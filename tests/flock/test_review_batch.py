import csv, json, subprocess, sys, tempfile, unittest
from pathlib import Path

from tests.flock.test_review import FIX, ROOT, SLUG, make_review, tree
from tests.flock.test_review_golden import SAMPLE, golden_review

TOOL, ROSTER = ROOT / "tools" / "flock_review_batch.py", FIX / "review-roster.csv"
HEADER = "slug,config,state,stages_done,stages_total,next,waiting_for,input_present,author_family,verifier_family,run_date".split(",")
SLUGS = (SLUG, "second-city-police", "campus-police")
GATE = "golden gate: Morro Bay golden must pass first"


def batch(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *map(str, args)], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


def titles(out):
    return [line for line in out.splitlines() if line.startswith("[RUN]")]


class FlockReviewBatchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def world(self, stubs=None):
        """Three stub agencies (the 2nd and 3rd only need to reach fact-drafts), the G5 sample golden and a LIST file."""
        made = [make_review(self.tmp / ("a%d" % n), stubs=stubs if n == 0 else None, slug=None if n == 0 else slug)
                for n, slug in enumerate(SLUGS)]
        self.configs, self.outs = [m[0] for m in made], [m[1]["out"] for m in made]
        self.golden = golden_review(self.tmp / "g")
        self.list, self.out = self.tmp / "list.txt", self.tmp / "batch"
        self.list.write_text("# three agencies\n\n" + "\n".join(map(str, self.configs)) + "\n")
        self.gate = ["--golden-config", self.golden, "--golden-expect", SAMPLE]

    def status(self, *extra, roster=ROSTER):
        return batch("status", self.list, "--roster", roster, "--out", self.out, *extra)

    def rows(self):
        with open(self.out / "BATCH-STATUS.csv", newline="") as handle:
            reader = csv.DictReader(handle)
            return reader.fieldnames, list(reader)

    def trees(self):
        return [tree(out) for out in self.outs]

    def test_status_and_run(self):
        self.world()
        before = self.trees()
        code, out = self.status()
        self.assertEqual(code, 0, out)
        self.assertIn("agencies=3 done=0 waiting=0 in_flight=0 failed=0 invalid=0 held=1 locked=0\n", out)
        header, rows = self.rows()
        self.assertEqual((header, [r["state"] for r in rows], [r["slug"] for r in rows]), (HEADER, ["ready", "ready", "held"], list(SLUGS)))
        self.assertEqual((self.out / "BATCH-STATUS.csv").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.trees(), before)
        code, out = self.status("--run", *self.gate)
        self.assertEqual(code, 0, out)
        for slug in SLUGS[:2]:
            self.assertIn("run %s rc=3 state=waiting\n" % slug, out)
        self.assertNotIn("run campus-police", out)
        header, rows = self.rows()
        self.assertEqual((header, len(rows), [r["state"] for r in rows], [r["waiting_for"] for r in rows]),
                         (HEADER, 3, ["waiting", "waiting", "held"], ["fact-drafts", "fact-drafts", ""]))
        self.assertEqual(self.trees()[2], before[2])
        self.assertFalse((self.outs[2] / "REVIEW-STATE.json").exists())

    def test_plan_and_caps(self):
        self.world()
        self.assertEqual(self.status("--run", *self.gate)[0], 0)
        code, out = batch("plan", self.list, "--roster", ROSTER, *self.gate)
        self.assertEqual(code, 0, out)
        self.assertEqual(titles(out), ["[RUN] %s fact-drafts 2026-10-30" % slug for slug in SLUGS[:2]])
        mark = "mark: python3 tools/flock_review.py mark %s fact-drafts --provider <actual runtime> --model <actual runtime>"
        self.assertIn(mark % self.configs[0].resolve(), out)
        self.assertIn("lane: sonnet-coder", out)
        self.assertIn("caps: 40 turns / 2 h; chunk by document; write REMAINING.md on cap", out)
        draft = self.outs[0] / "facts" / "drafts" / "a.yaml"  # written, not marked: a card is working
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text("draft\n")
        self.assertEqual(self.status()[0], 0)
        self.assertEqual([r["state"] for r in self.rows()[1]], ["in-flight", "waiting", "held"])
        code, out = batch("plan", self.list, "--roster", ROSTER, *self.gate)
        self.assertEqual(titles(out), ["[RUN] %s fact-drafts 2026-10-30" % SLUGS[1]])
        self.assertEqual(batch("plan", self.list, "--max", 3, *self.gate)[0], 2)

    def test_fail_closed(self):
        self.world()
        before = self.trees()
        wrong = self.tmp / "wrong.json"
        wrong.write_text(json.dumps(dict(json.loads(SAMPLE.read_text()), f3_findings=8)))
        for args in (("status", self.list, "--run"), ("plan", self.list)):
            code, out = batch(*args, "--golden-config", self.golden, "--golden-expect", wrong)
            self.assertEqual(code, 1, out)
            self.assertIn(GATE, out)
            self.assertNotIn("CARDS", out)
            self.assertNotIn("\nrun ", "\n" + out)
        self.assertEqual(batch("plan", self.list, "--golden-config", self.golden)[0], 1)
        self.assertEqual(batch("status", self.list, "--run", "--golden-config", self.golden)[0], 1)
        self.assertEqual(self.trees(), before)
        twice = self.tmp / "twice.txt"
        twice.write_text("%s\n%s\n" % (self.configs[0], self.configs[0]))
        self.assertEqual(batch("status", twice)[0], 1)
        mismatch = self.tmp / "roster.csv"
        mismatch.write_text(ROSTER.read_text().replace("local ALPR policy applies", "a different applicability", 1))
        self.assertEqual(self.status(roster=mismatch)[0], 0)
        self.assertEqual([r["state"] for r in self.rows()[1]], ["held", "ready", "held"])

    def test_stage_failure_does_not_stop_the_batch(self):
        self.world(stubs={"inventory": "sys.exit(1)"})
        code, out = self.status("--run", *self.gate)
        self.assertEqual(code, 0, out)
        self.assertIn("run %s rc=1 state=failed\n" % SLUG, out)
        rows = self.rows()[1]
        self.assertEqual([(r["state"], r["next"]) for r in rows], [("failed", "inventory"), ("waiting", "fact-drafts"), ("held", "inventory")])


if __name__ == "__main__":
    unittest.main()
