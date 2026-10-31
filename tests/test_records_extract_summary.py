import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("records_extract_summary", REPO / "tools/records_extract_summary.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)
from campaign_tool.records import native_extract as ne  # noqa: E402

SHAS = [hashlib.sha256(b"synthetic-%d" % i).hexdigest() for i in range(10)]


def doc(i, outcome, **detail):
    return {"original_sha256": SHAS[i], "form": "xlsx", "outcome": outcome, **detail}


class ExtractSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, data, raw=None):
        (self.dir / name).write_bytes(raw if raw is not None else json.dumps(data, sort_keys=True).encode())
        return str(self.dir / name)

    def outcomes_file(self, name, documents, schema=tool.SCHEMA):
        return self.write(name, {"schema": schema, "run_id": "run-7", "written_at": "2026-10-31T08:00:00Z",
                                 "documents": documents})

    def run_tool(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tool.main(list(argv))
        return code, [json.loads(line) for line in out.getvalue().splitlines() if line[:1] == "{"], out.getvalue()

    def test_counts_and_no_identities(self):
        docs = [doc(0, ne.EXTRACTED), doc(1, ne.EXTRACTED), doc(2, ne.EXTRACTED),
                doc(3, ne.AWAITING_OCR, ocr_pages_done=[1], ocr_pages_pending=[2, 3]),
                doc(4, ne.CORRUPT, **ne.refusal_detail("decoder_unavailable_or_failed", "exception")),
                doc(5, ne.TIMEOUT, **ne.refusal_detail("extraction_timeout", "exception")),
                doc(6, ne.NOT_ACCEPTED, **ne.refusal_detail("extraction_timeout", "accept_status", "held_status"))]
        code, lines, text = self.run_tool(self.outcomes_file("extract-run-7.json", docs))
        self.assertEqual(code, 0)
        line = lines[0]
        expected = dict.fromkeys(ne.OUTCOMES, 0)
        expected.update({ne.EXTRACTED: 3, ne.AWAITING_OCR: 1, ne.CORRUPT: 1, ne.TIMEOUT: 1, ne.NOT_ACCEPTED: 1})
        self.assertEqual(line, {"schema": "records-extract-summary-v1", "file": "extract-run-7.json", "run_id": "run-7",
                                "documents": 7, "outcomes": expected, "unknown_outcomes": 0, "extracted": 3, "failed": 3,
                                "top_refusal_codes": [["extraction_timeout", 2], ["decoder_unavailable_or_failed", 1]]})
        self.assertEqual(len(line["outcomes"]), 12)
        self.assertEqual(sum(line["outcomes"].values()), 7)
        for leaked in SHAS[:7] + ["xlsx", "held_status", "ocr_pages", "written_at", "2026-10-31", self.tmp.name]:
            self.assertNotIn(leaked, text)

    def test_unknown_outcome_top3_and_unlisted(self):
        docs = [doc(0, "NEW_CODE"), doc(1, ["list"])] + [doc(2 + i, ne.ERROR, refusal_code=c)
                                                         for i, c in enumerate(["e_code", "d_code", "b_code", "c_code"])]
        docs += [doc(6, ne.ERROR, refusal_code="Bad Code!\n"), doc(7, ne.ERROR, refusal_code=None)]
        code, lines, text = self.run_tool(self.outcomes_file("extract-u.json", docs))
        line = lines[0]
        self.assertEqual((code, line["unknown_outcomes"], line["documents"]), (0, 2, 8))
        self.assertNotIn("NEW_CODE", line["outcomes"])
        self.assertNotIn("Bad Code", text)
        self.assertEqual(line["top_refusal_codes"], [[ne.UNLISTED_CODE, 2], ["b_code", 1], ["c_code", 1]])

    def test_refusals_mixed_and_read_only(self):
        good = self.outcomes_file("extract-good.json", [doc(0, ne.EXTRACTED)])
        link = self.dir / "extract-link.json"
        link.symlink_to(good)
        files = [good, str(link), self.write("a.json", None, raw=b"{not json"), self.write("b.json", None, raw=b"\xff"),
                 self.outcomes_file("c.json", [], schema="records-extract-outcomes-v2"), self.write("d.json", [1]),
                 self.outcomes_file("e.json", {"x": 1}), self.outcomes_file("f.json", [doc(0, ne.EXTRACTED), "s"]),
                 str(self.dir), str(self.dir / "missing.json")]
        before = {p.name: (p.lstat().st_mode, p.lstat().st_mtime_ns) for p in self.dir.iterdir()}
        code, lines, text = self.run_tool(*files)
        self.assertEqual(code, 1)
        self.assertEqual(lines[0]["documents"], 1)
        self.assertEqual([line.get("error") for line in lines[1:]],
                         ["not-a-regular-file", "not-json", "not-json", "wrong-schema", "wrong-schema",
                          "bad-documents", "bad-documents", "not-a-regular-file", "not-a-regular-file"])
        self.assertNotIn(self.tmp.name, text)
        self.assertNotIn("Traceback", text)
        self.assertEqual(before, {p.name: (p.lstat().st_mode, p.lstat().st_mtime_ns) for p in self.dir.iterdir()})

    def test_too_large(self):
        path = self.outcomes_file("extract-big.json", [doc(0, ne.EXTRACTED)])
        with mock.patch.object(tool, "MAX_BYTES", 16):
            code, lines, _ = self.run_tool(path)
        self.assertEqual((code, lines), (1, [{"file": "extract-big.json", "error": "too-large"}]))

    def test_empty_run_usage_and_self_test(self):
        code, lines, _ = self.run_tool(self.outcomes_file("extract-empty.json", []))
        line = lines[0]
        self.assertEqual((code, line["documents"], line["extracted"], line["failed"], line["unknown_outcomes"]), (0, 0, 0, 0, 0))
        self.assertEqual((set(line["outcomes"].values()), line["top_refusal_codes"]), ({0}, []))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_tool()[0], 2)
        self.assertEqual(self.run_tool("--self-test")[2], "extract summary: self-test OK\n")


if __name__ == "__main__":
    unittest.main()
