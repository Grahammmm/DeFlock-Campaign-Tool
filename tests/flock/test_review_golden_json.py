import json, subprocess, sys, tempfile, unittest
from pathlib import Path

from tests.flock.test_review_golden import SAMPLE, TOOL, golden, golden_review

DATA = json.loads(SAMPLE.read_text())
SLUG, KEYS = DATA["slug"], len(DATA)


def doc(match, differences, keys=KEYS):  # keys: the golden's own top-level key count
    return {"schema": "flock-review-golden-check-v1", "review_required": True, "match": match, "keys": keys,
            "differences": differences, "slug": SLUG}


class FlockReviewGoldenJsonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.config = golden_review(Path(tmp.name) / "review")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp, self.n = Path(tmp.name), 0

    def write(self, edit=None, text=None):
        self.n += 1
        data = json.loads(SAMPLE.read_text())
        if edit:
            edit(data)
        path = self.tmp / ("golden-%d.json" % self.n)
        path.write_text(text if text is not None else json.dumps(data))
        return path

    def check(self, edit=None, text=None):
        """(exit, parsed document or None, raw stdout) for --expect --json."""
        code, out = golden(self.config, "--expect", self.write(edit, text), "--json")
        try:
            parsed = json.loads(out)
        except ValueError:
            parsed = None
        return code, parsed, out

    def test_match(self):
        code, parsed, out = self.check()
        self.assertEqual((code, parsed), (0, doc(True, [])))
        self.assertEqual(out, json.dumps(doc(True, []), indent=1, sort_keys=True) + "\n")
        self.assertEqual(golden(self.config, "--expect", SAMPLE), (0, "golden ok keys=%d\n" % KEYS))

    def test_nested_leaf_difference(self):
        code, parsed, _ = self.check(lambda d: d["findings_by_kind"]["requirement"].update(met=2))
        self.assertEqual((code, parsed), (1, doc(False, [{"key": "findings_by_kind.requirement.met", "expected": 2, "got": 1}])))

    def test_missing_invalid_and_sorted(self):
        def edit(d):
            d.update(bogus=1, f3_findings="Free text, not a slug", facts_by_kind={"record": 5})
            d["findings_by_kind"]["claim"] = "x/y"
        code, parsed, out = self.check(edit)
        self.assertEqual((code, parsed), (1, doc(False, [
            {"key": "bogus", "expected": 1, "got": "missing"},
            {"key": "f3_findings", "expected": "invalid", "got": 7},
            {"key": "facts_by_kind.record", "expected": 5, "got": 6},
            {"key": "findings_by_kind.claim", "expected": "invalid", "got": {"contradicted": 1}}], KEYS + 1)))
        self.assertNotIn("Free text", out)
        self.assertNotIn("x/y", out)
        code, parsed, _ = self.check(lambda d: d["findings_by_kind"]["claim"].update(upheld="other-slug"))
        self.assertEqual(parsed["differences"], [{"key": "findings_by_kind.claim.upheld", "expected": "other-slug",
                                                  "got": "missing"}])

    def test_unsafe_key_never_echoed(self):
        raw = "a\nb"
        code, parsed, out = self.check(lambda d: d["findings_by_kind"].update({raw: {"met": 1}}))
        self.assertEqual((code, parsed["differences"]), (1, [{"key": "findings_by_kind.<key>.met", "expected": 1, "got": "missing"}]))
        self.assertNotIn("a\\nb", out)

    def test_json_with_write_is_usage_error(self):
        target = self.tmp / "counts.json"
        done = subprocess.run([sys.executable, "-B", str(TOOL), str(self.config), "--write", str(target), "--json"],
                              capture_output=True, text=True, check=False)
        self.assertEqual((done.returncode, done.stdout, target.exists()), (2, "", False))
        self.assertIn("--json needs --expect", done.stderr)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_refusals_stay_one_text_line(self):
        cases = [(None, "", "golden: --expect file cannot be read as JSON\n"),
                 (None, "[1]", "golden: --expect file is not a JSON object\n"),
                 (lambda d: d.update(schema=2), None, "golden: --expect file is not a schema 1 golden\n"),
                 (lambda d: d.update(slug="other-city-police"), None,
                  'golden: --expect file is for agency "other-city-police", not "%s"\n' % SLUG)]
        for edit, text, line in cases:
            with self.subTest(line=line):
                self.assertEqual(self.check(edit, text), (1, None, line))
        code, out = golden(self.config, "--expect", self.tmp / "absent.json", "--json")
        self.assertEqual((code, out), (1, "golden: --expect file cannot be read as JSON\n"))
        half = golden_review(self.tmp / "half", until="assess")
        self.assertEqual(golden(half, "--expect", SAMPLE, "--json"), (1, "golden: assess not done\n"))

    def test_text_mode_unchanged(self):
        bump = self.write(lambda d: d.update(f3_findings=8, facts_by_kind={"record": 5}))
        self.assertEqual(golden(self.config, "--expect", bump),
                         (1, "golden: f3_findings expected 8 got 7\ngolden: facts_by_kind.record expected 5 got 6\n"))


if __name__ == "__main__":
    unittest.main()
