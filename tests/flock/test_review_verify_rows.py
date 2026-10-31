"""RF1105-6: plan refuses FINDINGS rows it would otherwise leave out of both the priority list and the sample."""
import contextlib, importlib.util, io, json, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / rel)
    spec.loader.exec_module(module := importlib.util.module_from_spec(spec))
    return module


VERIFY = load("_rf1105_6_verify", "flock_review_verify.py")
HEAD = "finding_id,kind,verdict,basis\n"
CLEAN = [("A1", "Met"), ("A2", "not met"), ("A3", "CONTRADICTED"), ("A4", "cannot tell"), ("A5", "n/a"), ("A6", " met ")]
SUCCESS = r"\Apriority=\d+ pool=\d+ sample=\d+ findings_sha256=[0-9a-f]{64}\n\Z"


class PlanRowsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.out = self.tmp / "o" / "PLAN.json"
        self.out.parent.mkdir()

    def write(self, rows, text=None):
        path = self.tmp / "FINDINGS.csv"
        path.write_text(text if text is not None else HEAD + "".join("%s,requirement,%s,b\n" % row for row in rows), encoding="utf-8")
        return path

    def run_plan(self, rows=CLEAN, size="2", text=None):
        path = self.write(rows, text)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = VERIFY.main(["plan", str(path), "--seed", "7", "--size", size, "--out", str(self.out)])
        return code, printed.getvalue()

    def refused(self, rows, *lines, text=None):
        code, out = self.run_plan(rows, text=text)
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), list(lines) + ["plan: errors=%d" % len(lines)])
        self.assertEqual(sorted(p.name for p in self.out.parent.iterdir()), [])  # no PLAN.json, no .PLAN.json.*.tmp

    def test_clean_findings_plan_with_na_in_neither_list(self):
        code, out = self.run_plan(size="10")
        self.assertEqual(code, 0)
        self.assertRegex(out, SUCCESS)
        body = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(body["priority_ids"], ["A2", "A3"])
        self.assertEqual(sorted(body["sample_ids"]), ["A1", "A4", "A6"])
        self.assertEqual(body["pool_size"], 3)

    def test_size_zero_is_allowed(self):
        code, out = self.run_plan(size="0")
        self.assertEqual(code, 0)
        self.assertIn("sample=0 ", out)

    def test_empty_verdict_refused(self):
        self.refused([("A1", "met"), ("A2", ""), ("A3", "not met")], "row 3: verdict is unknown")

    def test_unknown_verdicts_refused(self):
        self.refused([("A1", "met."), ("A2", "unknown"), ("A3", "not-met"), ("A4", "met")],
                     "row 2: verdict is unknown", "row 3: verdict is unknown", "row 4: verdict is unknown")

    def test_empty_finding_id_refused(self):
        self.refused([("A1", "met"), ("  ", "contradicted")], "row 3: finding_id is empty")

    def test_duplicate_finding_id_names_second_row(self):
        self.refused([("A1", "met"), ("A2", "met"), ("A1", "contradicted")], "row 4: finding_id is duplicated")

    def test_duplicate_differing_only_by_spaces_refused(self):
        self.refused([("A1", "met"), (" A1 ", "not met")], "row 3: finding_id is duplicated")

    def test_two_problems_both_printed(self):
        self.refused([("A1", "maybe"), ("A2", "met"), ("A2", "met")], "row 2: verdict is unknown", "row 4: finding_id is duplicated")

    def test_one_row_with_two_problems(self):
        self.refused([("", "")], "row 2: verdict is unknown", "row 2: finding_id is empty")

    def test_row_number_counts_blank_lines_like_check(self):
        self.refused([], "row 4: verdict is unknown", text=HEAD + "A1,requirement,met,b\n\nA2,requirement,bad,b\n")

    def test_short_row_without_verdict_refused(self):
        self.refused([], "row 2: verdict is unknown", text=HEAD + "A1,requirement\n")

    def test_negative_size_is_usage_error(self):
        path = self.write(CLEAN)
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as raised:
            VERIFY.main(["plan", str(path), "--seed", "7", "--size", "-1", "--out", str(self.out)])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--size must be 0 or more", err.getvalue())
        self.assertEqual(list(self.out.parent.iterdir()), [])

    def test_existing_plan_for_same_findings_reused(self):
        self.assertEqual(self.run_plan()[0], 0)
        before = self.out.read_bytes()
        self.assertEqual(self.run_plan(), (0, "plan: PLAN.json reused (frozen)\n"))
        self.assertEqual(self.out.read_bytes(), before)
        self.assertEqual([p.name for p in self.out.parent.iterdir()], ["PLAN.json"])

    def test_existing_plan_for_other_findings_still_refused_first(self):
        self.assertEqual(self.run_plan()[0], 0)
        before = self.out.read_bytes()
        self.assertEqual(self.run_plan([("A1", "bad")]), (1, "plan: PLAN.json exists for other findings\n"))
        self.assertEqual(self.out.read_bytes(), before)

    def test_verdict_set_matches_flock_report(self):
        self.assertEqual(VERIFY.VERDICTS, load("_rf1105_6_report", "flock_report.py").VERDICTS)


if __name__ == "__main__":
    unittest.main()
