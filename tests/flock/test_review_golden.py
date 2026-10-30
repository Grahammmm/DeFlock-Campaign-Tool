import json, subprocess, sys, tempfile, unittest
from pathlib import Path

from tests.flock.test_review import WAITS, ROOT, FIX, make_review, mark_all, tool, tree

TOOL, SAMPLE = ROOT / "tools" / "flock_review_golden.py", FIX / "review-golden-sample.json"
# compare_build override: a 3-row method-header FINDINGS.csv (2 requirement rows, 1 analysis-only claim row)
OVERRIDE = '''out = Path(json.loads(Path(a[0]).read_text())["compare"]["out"])
out.mkdir(parents=True, exist_ok=True)
header = "finding_id kind req_id source version section obligation topic requirement verdict basis fact_ids record_ids pages other_refs related_req_ids analysis_only next_step".split()
with (out / "FINDINGS.csv").open("w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=header)
    writer.writeheader()
    for fid, kind, verdict, only in (("R001", "requirement", "met", "no"), ("R002", "requirement", "cannot tell", "no"),
                                     ("C001", "claim", "contradicted", "yes")):
        writer.writerow(dict(finding_id=fid, kind=kind, verdict=verdict, analysis_only=only))
sys.exit(0)'''


def golden(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *map(str, args)], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


def golden_review(tmp, until=None):
    """Stub pipeline with the compare_build override; every wait stage before `until` marked, then run. Returns the config path."""
    config, _ = make_review(tmp, stubs={"compare_build": OVERRIDE})
    for stage, provider, key, rel in WAITS:
        if stage == until:
            break
        assert tool("run", config)[0] == 3
        if key:
            path = Path(tmp) / "out" / {"FO": "facts", "CO": "compare"}[key] / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(stage + " input\n")
        assert mark_all(config, {stage: provider}) == [0]
    code, out = tool("run", config)
    assert code == (3 if until else 0), out
    return config


class FlockReviewGoldenTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_match_and_write(self):
        config = golden_review(self.tmp)
        code, out = golden(config, "--expect", SAMPLE)
        self.assertEqual((code, out), (0, "golden ok keys=%d\n" % len(json.loads(SAMPLE.read_text()))))
        target = self.tmp / "counts.json"
        self.assertEqual(golden(config, "--write", target)[0], 0)
        self.assertEqual(target.read_text(), SAMPLE.read_text())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        code, out = golden(config, "--write", target)
        self.assertEqual((code, target.read_text()), (1, SAMPLE.read_text()), out)

    def test_mismatch_and_not_done(self):
        config = golden_review(self.tmp)
        expected = json.loads(SAMPLE.read_text())
        wrong, extra = self.tmp / "wrong.json", self.tmp / "extra.json"
        wrong.write_text(json.dumps(dict(expected, f3_findings=8)))
        extra.write_text(json.dumps(dict(expected, bogus=1)))
        self.assertEqual(golden(config, "--expect", wrong), (1, "golden: f3_findings expected 8 got 7\n"))
        self.assertEqual(golden(config, "--expect", extra), (1, "golden: bogus expected 1 got missing\n"))
        inside = ROOT / "tests" / "fixtures" / "flock" / "zz-golden-refused.json"
        code, out = golden(config, "--write", inside)
        self.assertEqual((code, inside.exists()), (1, False), out)
        self.assertEqual(golden(config)[0], 2)
        half = golden_review(self.tmp / "half", until="assess")
        self.assertEqual(golden(half, "--expect", SAMPLE), (1, "golden: assess not done\n"))

    def test_read_only(self):
        config = golden_review(self.tmp)
        before = tree(self.tmp / "out")
        self.assertEqual(golden(config, "--expect", SAMPLE)[0], 0)
        self.assertEqual(golden(config, "--write", self.tmp / "counts.json")[0], 0)
        self.assertEqual(tree(self.tmp / "out"), before)


if __name__ == "__main__":
    unittest.main()
