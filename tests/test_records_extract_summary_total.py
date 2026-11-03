import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("records_extract_summary", REPO / "tools/records_extract_summary.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)
from campaign_tool.records import native_extract as ne  # noqa: E402

HASH = "ab" * 32


def doc(outcome, code=None):
    item = {"original_sha256": HASH, "form": "msg", "outcome": outcome}
    return item if code is None else {**item, **ne.refusal_detail(code, "exception", "held_status")}


class ExtractSummaryTotalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, documents, run_id="run-1", schema=tool.SCHEMA):
        data = {"schema": schema, "run_id": run_id, "written_at": "2026-10-31T08:00:00Z", "documents": documents}
        (self.dir / name).write_text(json.dumps(data))
        return str(self.dir / name)

    def run_tool(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(list(argv))
        return code, [json.loads(line) for line in out.getvalue().splitlines() if line[:1] == "{"], out.getvalue()

    def test_fourth_code_in_every_file_is_first_overall(self):
        files = []
        for i, prefix in enumerate(["pa", "pb", "pc"]):
            codes = [prefix + "_one", prefix + "_two", prefix + "_three"] * 2 + ["x_code"]
            files.append(self.write("extract-%d.json" % i, [doc(ne.TIMEOUT, c) for c in codes] + [doc(ne.EXTRACTED)],
                                    run_id="run-%d" % i))
        code, alone, _ = self.run_tool(*files)
        code_total, lines, _ = self.run_tool("--total", *files)
        self.assertEqual((code, code_total), (0, 0))
        self.assertEqual(lines[:3], alone)
        for line in alone:
            self.assertNotIn("x_code", [c for c, _ in line["top_refusal_codes"]])
        expected_outcomes = dict.fromkeys(ne.OUTCOMES, 0)
        expected_outcomes.update({ne.TIMEOUT: 21, ne.EXTRACTED: 3})
        self.assertEqual(lines[3], {
            "schema": "records-extract-summary-total-v1", "files": 3, "summarized_files": 3, "refused_files": {},
            "documents": 24, "outcomes": expected_outcomes, "unknown_outcomes": 0, "extracted": 3, "failed": 21,
            "top_refusal_codes": [["x_code", 3], ["pa_one", 2], ["pa_three", 2]], "repeated_run_ids": 0})
        self.assertEqual(len(lines), 4)

    def test_flag_position_anywhere(self):
        one = self.write("a.json", [doc(ne.EXTRACTED)])
        two = self.write("b.json", [doc(ne.EXTRACTED)], run_id="run-2")
        code, lines, _ = self.run_tool(one, "--total", two)
        self.assertEqual((code, [line["schema"] for line in lines]),
                         (0, ["records-extract-summary-v1"] * 2 + ["records-extract-summary-total-v1"]))
        self.assertEqual(lines[2]["extracted"], 2)

    def test_refused_files_counted_by_code(self):
        good = self.write("good.json", [doc(ne.EXTRACTED), doc(ne.CORRUPT, "decoder_failed")])
        wrong = self.write("wrong.json", [doc(ne.EXTRACTED)], schema="records-extract-outcomes-v2")
        link = self.dir / "link.json"
        link.symlink_to(good)
        code, lines, _ = self.run_tool("--total", good, wrong, str(link))
        self.assertEqual(code, 1)
        summary = lines[-1]
        self.assertEqual(summary["refused_files"], {"wrong-schema": 1, "not-a-regular-file": 1})
        self.assertEqual((summary["files"], summary["summarized_files"], summary["documents"]), (3, 1, 2))
        self.assertEqual((summary["extracted"], summary["failed"]), (1, 1))
        self.assertEqual(summary["top_refusal_codes"], [["decoder_failed", 1]])

    def test_repeated_run_ids(self):
        cases = [(["same", "same"], 1), (["same", "same", "same"], 2), ([None, None], 0),
                 (["same", "other", "same", "other"], 2), ([7, 7], 0)]
        for run_ids, repeated in cases:
            with self.subTest(run_ids=run_ids):
                files = [self.write("copy-%d.json" % i, [doc(ne.EXTRACTED)], run_id=r) for i, r in enumerate(run_ids)]
                code, lines, text = self.run_tool("--total", *files)
                self.assertEqual((code, lines[-1]["repeated_run_ids"]), (0, repeated))
                self.assertEqual(lines[-1]["documents"], len(run_ids))
                self.assertNotIn("same", json.dumps(lines[-1]))

    def test_all_files_refused(self):
        bad = self.write("bad.json", [doc(ne.EXTRACTED)], schema="other")
        missing = str(self.dir / "missing.json")
        code, lines, _ = self.run_tool("--total", bad, missing)
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], {
            "schema": "records-extract-summary-total-v1", "files": 2, "summarized_files": 0,
            "refused_files": {"wrong-schema": 1, "not-a-regular-file": 1}, "documents": 0,
            "outcomes": dict.fromkeys(ne.OUTCOMES, 0), "unknown_outcomes": 0, "extracted": 0, "failed": 0,
            "top_refusal_codes": [], "repeated_run_ids": 0})

    def test_unknown_outcomes_not_keys(self):
        one = self.write("u1.json", [doc("NEW_CODE"), doc(["list"]), doc(ne.EXTRACTED)])
        two = self.write("u2.json", [doc("NEW_CODE"), doc(None)], run_id="run-2")
        code, lines, _ = self.run_tool("--total", one, two)
        summary = lines[-1]
        self.assertEqual((code, summary["unknown_outcomes"], summary["documents"], summary["extracted"]), (0, 4, 5, 1))
        self.assertEqual(set(summary["outcomes"]), set(ne.OUTCOMES))
        self.assertEqual(summary["unknown_outcomes"], lines[0]["unknown_outcomes"] + lines[1]["unknown_outcomes"])

    def test_usage_errors_print_nothing(self):
        path = self.write("a.json", [doc(ne.EXTRACTED)])
        for argv in (["--total"], ["--total", "--total", path], ["--total", "--self-test"], ["--self-test", "--total"],
                     ["--total", "-x", path], ["--total", path, "--totals"], ["--TOTAL", path]):
            with self.subTest(argv=argv):
                code, _, text = self.run_tool(*argv)
                self.assertEqual((code, text), (2, ""))

    def test_no_identities_in_output(self):
        files = [self.write("p%d.json" % i, [doc(ne.ERROR, "held_code"), doc(ne.AWAITING_OCR)]) for i in range(2)]
        code, lines, text = self.run_tool("--total", *files)
        self.assertEqual((code, lines[-1]["repeated_run_ids"]), (0, 1))
        for leaked in (HASH, "msg", "held_status", "written_at", "2026-10-31", self.tmp.name):
            self.assertNotIn(leaked, text)
        self.assertNotIn("run-1", text.splitlines()[-1])

    def test_self_test_still_runs(self):
        self.assertEqual(self.run_tool("--self-test")[2], "extract summary: self-test OK\n")


if __name__ == "__main__":
    unittest.main()
