import contextlib, importlib.util, io, json, tempfile, unittest
from argparse import Namespace
from pathlib import Path

from tests.flock.test_review import ROOT
from tests.flock.test_review_golden import SAMPLE, golden, golden_review

SLUG = json.loads(SAMPLE.read_text())["slug"]
NOT_ONE = "golden: --expect file is not a schema 1 golden\n"
GATE = "golden gate: Morro Bay golden must pass first"


def agency(other):
    return 'golden: --expect file is for agency %s, not "%s"\n' % (other, SLUG)


class FlockReviewGoldenLeavesTest(unittest.TestCase):
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
        """A golden file: the sample edited in place by `edit`, or raw `text`."""
        self.n += 1
        data = json.loads(SAMPLE.read_text())
        if edit:
            edit(data)
        path = self.tmp / ("golden-%d.json" % self.n)
        path.write_text(text if text is not None else json.dumps(data))
        return path

    def expect(self, edit=None, text=None):
        return golden(self.config, "--expect", self.write(edit, text))

    def test_nested_count_one_line_per_leaf(self):
        bump = lambda d: d["findings_by_kind"]["requirement"].update(met=2)  # noqa: E731
        self.assertEqual(self.expect(bump), (1, "golden: findings_by_kind.requirement.met expected 2 got 1\n"))
        extra = lambda d: d["findings_by_kind"]["claim"].update(upheld=1)  # noqa: E731
        self.assertEqual(self.expect(extra), (1, "golden: findings_by_kind.claim.upheld expected 1 got missing\n"))
        drop = lambda d: d["findings_by_kind"].pop("requirement")  # noqa: E731
        self.assertEqual(self.expect(drop), (1, "golden: findings_by_kind.requirement.cannot tell expected missing got 1\n"
                                                "golden: findings_by_kind.requirement.met expected missing got 1\n"))
        empty = lambda d: d["findings_by_kind"].update(waived={})  # noqa: E731  an empty kind is never silently ok
        self.assertEqual(self.expect(empty), (1, "golden: findings_by_kind.waived expected {} got missing\n"))
        both = lambda d: d.update(f3_findings=8, facts_by_kind={"record": 5})  # noqa: E731  sorted by path, scalars unchanged
        self.assertEqual(self.expect(both), (1, "golden: f3_findings expected 8 got 7\n"
                                                "golden: facts_by_kind.record expected 5 got 6\n"))

    def test_dict_against_non_dict_is_one_whole_key_line(self):
        self.assertEqual(self.expect(lambda d: d.update(facts_by_kind=5)),
                         (1, 'golden: facts_by_kind expected 5 got {"record": 6}\n'))
        self.assertEqual(self.expect(lambda d: d["findings_by_kind"].update(claim=3)),
                         (1, 'golden: findings_by_kind.claim expected 3 got {"contradicted": 1}\n'))
        self.assertEqual(self.expect(lambda d: d["findings_by_kind"].update(claim="x/y")),
                         (1, 'golden: findings_by_kind.claim expected invalid got {"contradicted": 1}\n'))

    def test_another_agency_refused_in_one_line(self):
        other = lambda d: d.update(slug="other-city-police", f3_findings=8)  # noqa: E731  no per-key lines follow
        self.assertEqual(self.expect(other), (1, agency('"other-city-police"')))
        self.assertEqual(self.expect(lambda d: d.update(slug="../x")), (1, agency("invalid")))
        self.assertEqual(self.expect(lambda d: d.pop("slug")), (1, agency("missing")))
        self.assertEqual(self.expect(lambda d: d.update(slug=SLUG.upper())), (1, agency("invalid")))

    def test_non_schema_1_refused_in_one_line(self):
        for schema in (2, 0, True, 1.0, "1", None, [1]):
            with self.subTest(schema=schema):
                self.assertEqual(self.expect(lambda d: d.update(schema=schema, slug="other-city-police")), (1, NOT_ONE))
        self.assertEqual(self.expect(lambda d: d.pop("schema")), (1, NOT_ONE))
        self.assertEqual(self.expect(text="[1]"), (1, "golden: --expect file is not a JSON object\n"))

    def test_unsafe_nested_key_never_echoed(self):
        raw = "a\nb"
        code, out = self.expect(lambda d: d["findings_by_kind"].update({raw: {"met": 1}}))
        self.assertEqual((code, out), (1, "golden: findings_by_kind.<key>.met expected 1 got missing\n"))
        code, out = self.expect(lambda d: d["findings_by_kind"]["claim"].update({"x" * 41: 2, raw: 1}))
        self.assertEqual((code, out.count("\n")), (1, 2), out)
        self.assertEqual(set(out.splitlines()), {"golden: findings_by_kind.claim.<key> expected %d got missing" % n for n in (1, 2)})
        self.assertNotIn(raw, out)
        self.assertNotIn("x" * 41, out)

    def test_match_and_pinned_scalar_lines(self):
        self.assertEqual(self.expect(), (0, "golden ok keys=%d\n" % len(json.loads(SAMPLE.read_text()))))
        self.assertEqual(self.expect(lambda d: d.update(f3_findings=8)), (1, "golden: f3_findings expected 8 got 7\n"))
        self.assertEqual(self.expect(lambda d: d.update(bogus=1)), (1, "golden: bogus expected 1 got missing\n"))

    def test_batch_gate(self):
        spec = importlib.util.spec_from_file_location("_leaves_batch", ROOT / "tools" / "flock_review_batch.py")
        batch = spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module

        def gate(path):
            shown = io.StringIO()
            with contextlib.redirect_stdout(shown):
                try:
                    batch.gate(Namespace(golden_config=str(self.config), golden_expect=str(path)))
                except batch.fr.Fail as exc:
                    return "refused: %s" % exc, shown.getvalue()
            return "passed", shown.getvalue()

        leaf = self.write(lambda d: d["findings_by_kind"]["requirement"].update(met=2))
        self.assertEqual(gate(leaf), ("refused: " + GATE, "golden: findings_by_kind.requirement.met expected 2 got 1\n"))
        self.assertEqual(gate(self.write(lambda d: d.update(slug="other-city-police"))),
                         ("refused: " + GATE, agency('"other-city-police"')))
        self.assertEqual(gate(self.write(lambda d: d.update(schema=2))), ("refused: " + GATE, NOT_ONE))
        self.assertEqual(gate(self.write()), ("passed", "golden ok keys=%d\n" % len(json.loads(SAMPLE.read_text()))))


if __name__ == "__main__":
    unittest.main()
