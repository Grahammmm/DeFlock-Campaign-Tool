import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("records_extract_summary", REPO / "tools/records_extract_summary.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)
from campaign_tool.records import native_extract as ne  # noqa: E402

A, B, C, D = (hashlib.sha256(b"unique-synthetic-%d" % i).hexdigest() for i in range(4))
SCHEMA = "records-extract-summary-unique-v1"
KEYS = {"schema", "selection", "scope", "files", "observations", "documents", "replaced_observations", "outcomes",
        "unknown_outcomes", "extracted", "failed", "top_refusal_codes"}


def doc(sha, outcome, code=None, **extra):
    item = {"original_sha256": sha, "form": "xlsx", "outcome": outcome, **extra}
    return item if code is None else {**item, "refusal_code": code}


class UniqueBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, documents, run_id="run-1", written_at="2026-10-31T08:00:00Z", schema=tool.SCHEMA):
        data = {"schema": schema, "run_id": run_id, "written_at": written_at, "documents": documents}
        (self.dir / name).write_text(json.dumps(data))
        return str(self.dir / name)

    def run_tool(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def unique(self, *paths):
        code, out, err = self.run_tool("--unique", *paths)
        self.assertEqual(len(out.splitlines()), 1)
        return code, json.loads(out), err


class UniqueCountTest(UniqueBase):
    def test_retry_replaces_old_outcome_and_retains_absent_original(self):
        old = self.write("old.json", [doc(A, ne.AWAITING_OCR, "ocr_needed"), doc(B, ne.TIMEOUT, "extraction_timeout")])
        new = self.write("new.json", [doc(A, ne.EXTRACTED)])
        code, line, err = self.unique(old, new)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(set(line), KEYS)
        self.assertEqual((line["schema"], line["selection"], line["scope"]),
                         (SCHEMA, "last-input-wins", "provided-outcome-files-only"))
        self.assertEqual((line["files"], line["observations"], line["documents"], line["replaced_observations"]),
                         (2, 3, 2, 1))
        self.assertEqual(line["outcomes"], {**dict.fromkeys(ne.OUTCOMES, 0), ne.EXTRACTED: 1, ne.TIMEOUT: 1})
        self.assertEqual((line["extracted"], line["failed"], line["unknown_outcomes"]), (1, 1, 0))
        self.assertEqual(line["top_refusal_codes"], [["extraction_timeout", 1]])  # A's old ocr_needed did not survive
        self.assertEqual(self.run_tool("--total", old, new)[1].splitlines()[-1].count('"documents": 3'), 1)  # unchanged

    def test_input_order_not_run_label_mtime_or_written_at(self):
        old = self.write("old.json", [doc(A, ne.EXTRACTED)], run_id="run-9", written_at="2030-01-01T00:00:00Z")
        new = self.write("new.json", [doc(A, ne.TIMEOUT, "extraction_timeout")], run_id="run-1",
                         written_at="2020-01-01T00:00:00Z")
        os.utime(old, (1, 1))
        os.utime(new, (2_000_000_000, 2_000_000_000))
        forward = self.unique(old, new)[1]
        os.utime(old, (2_000_000_000, 2_000_000_000))
        os.utime(new, (1, 1))
        self.assertEqual(self.unique(old, new)[1], forward)  # mtimes ignored
        self.assertEqual((forward["extracted"], forward["failed"]), (0, 1))  # later failure replaces EXTRACTED
        reverse = self.unique(new, old)[1]
        self.assertEqual((reverse["extracted"], reverse["failed"]), (1, 0))  # reversing reverses the winner

    def test_new_identity_and_same_run_label_are_not_duplicates(self):
        one = self.write("one.json", [doc(A, ne.EXTRACTED), doc(B, ne.EXTRACTED)], run_id="same")
        two = self.write("two.json", [doc(C, ne.AWAITING_OCR)], run_id="same")
        three = self.write("three.json", [doc(D, ne.EXTRACTED)], run_id=7)
        line = self.unique(one, two, three)[1]
        self.assertEqual((line["documents"], line["observations"], line["replaced_observations"]), (4, 4, 0))
        self.assertEqual((line["extracted"], line["outcomes"][ne.AWAITING_OCR]), (3, 1))

    def test_repeated_file_and_empty_later_file(self):
        base = self.write("base.json", [doc(A, ne.EXTRACTED), doc(B, ne.ERROR, "held_code")])
        empty = self.write("empty.json", [])
        once = self.unique(base)[1]
        twice = self.unique(base, base)[1]
        for key in ("documents", "outcomes", "extracted", "failed", "top_refusal_codes"):
            self.assertEqual(twice[key], once[key])
        self.assertEqual((twice["files"], twice["observations"], twice["replaced_observations"]), (2, 4, 2))
        later = self.unique(base, empty)[1]
        self.assertEqual((later["documents"], later["files"], later["observations"], later["replaced_observations"]),
                         (2, 2, 2, 0))
        self.assertEqual(later["outcomes"], once["outcomes"])
        none = self.unique(empty, empty)[1]
        self.assertEqual((none["documents"], none["observations"], none["unknown_outcomes"], none["extracted"],
                          none["failed"], none["top_refusal_codes"], none["scope"]),
                         (0, 0, 0, 0, 0, [], "provided-outcome-files-only"))
        self.assertEqual(none["outcomes"], dict.fromkeys(ne.OUTCOMES, 0))

    def test_unknown_outcome_overwrites_and_can_be_overwritten(self):
        first = self.write("a.json", [doc(A, ne.EXTRACTED), doc(B, "NEW_CODE"), doc(C, ["list"]), doc(D, None)])
        second = self.write("b.json", [doc(A, "FUTURE_CODE"), doc(B, ne.EXTRACTED)])
        third = self.write("c.json", [{"original_sha256": D, "form": "msg"}, doc(C, ne.TIMEOUT)])
        line = self.unique(first, second)[1]
        self.assertEqual((line["unknown_outcomes"], line["extracted"], line["documents"]), (3, 1, 4))
        self.assertEqual(set(line["outcomes"]), set(ne.OUTCOMES))
        self.assertEqual(sum(line["outcomes"].values()) + line["unknown_outcomes"], line["documents"])
        line = self.unique(first, second, third)[1]  # C known again; D still unknown (outcome missing)
        self.assertEqual((line["unknown_outcomes"], line["extracted"], line["failed"]), (2, 1, 1))
        self.assertEqual(sum(line["outcomes"].values()) + line["unknown_outcomes"], line["documents"])

    def test_histogram_uses_only_winners_and_shared_sanitization(self):
        old = self.write("old.json", [doc(A, ne.TIMEOUT, "gone_code"), doc(B, ne.ERROR, "keep_b")])
        new = self.write("new.json", [doc(A, ne.EXTRACTED), doc(C, ne.TIMEOUT, "Bad Code!"),
                                      doc(D, ne.AWAITING_OCR, "keep_b")])
        line = self.unique(old, new)[1]
        codes = dict(line["top_refusal_codes"])
        self.assertNotIn("gone_code", codes)
        self.assertEqual(line["top_refusal_codes"], [["keep_b", 2], [ne.UNLISTED_CODE, 1]])
        self.assertEqual(line["failed"], 2)  # TIMEOUT(C) + ERROR(B); AWAITING_OCR and EXTRACTED are not failed
        many = self.write("many.json", [doc(hashlib.sha256(b"m%d" % i).hexdigest(), ne.WORKBOOK_HELD, c)
                                        for i, c in enumerate(["zz", "bb", "aa", "bb", "zz", "cc", "dd"])])
        top = self.unique(many)[1]
        self.assertEqual(top["top_refusal_codes"], [["bb", 2], ["zz", 2], ["aa", 1]])
        self.assertEqual(top["failed"], 0)  # waiting states are not in the FAILED set

    def test_pure_helper_does_not_mutate_inputs(self):
        data = {"schema": tool.SCHEMA, "documents": [doc(A, ne.EXTRACTED, None, ocr_pages=[1, 2]), doc(B, ne.TIMEOUT, "extraction_timeout")]}
        before = copy.deepcopy(data)
        winners = {}
        self.assertEqual(tool.unique_winners(winners, data, 1), 2)
        self.assertEqual(data, before)
        self.assertEqual(set(winners[A]), {"outcome"})  # nothing but outcome and refusal_code retained
        self.assertEqual(winners[B], {"outcome": ne.TIMEOUT, "refusal_code": "extraction_timeout"})


class UniqueRefusalTest(UniqueBase):
    def test_identity_validation_is_exact_and_atomic(self):
        good = self.write("good.json", [doc(A, ne.EXTRACTED)])
        bad_rows = {"missing": {"outcome": ne.EXTRACTED}, "null": doc(None, ne.EXTRACTED), "number": doc(7, ne.EXTRACTED),
                    "upper": doc(A.upper(), ne.EXTRACTED), "padded": doc(" " + A, ne.EXTRACTED),
                    "newline": doc(A + "\n", ne.EXTRACTED), "short": doc(A[:-1], ne.EXTRACTED),
                    "long": doc(A + "0", ne.EXTRACTED), "nonhex": doc("g" + A[1:], ne.EXTRACTED)}
        for label, row in bad_rows.items():
            with self.subTest(label):
                bad = self.write("bad.json", [doc(B, ne.EXTRACTED), row])
                code, out, err = self.run_tool("--unique", good, bad)
                self.assertEqual((code, err, json.loads(out)),
                                 (1, "", {"schema": SCHEMA, "error": "bad-original-sha256", "input": 2}))
                self.assertEqual(len(out.splitlines()), 1)
        for same in ([doc(A, ne.EXTRACTED), doc(A, ne.EXTRACTED)], [doc(A, ne.EXTRACTED), doc(A, ne.TIMEOUT)]):
            dup = self.write("dup.json", same)
            code, out, _ = self.run_tool("--unique", dup, good)
            self.assertEqual((code, json.loads(out)), (1, {"schema": SCHEMA, "error": "duplicate-original", "input": 1}))

    def test_refused_input_never_emits_stale_success(self):
        good = self.write("good.json", [doc(A, ne.EXTRACTED)])
        (self.dir / "malformed.json").write_text("{not json")
        wrong = self.write("wrong.json", [doc(A, ne.EXTRACTED)], schema="records-extract-outcomes-v2")
        (self.dir / "list.json").write_text("[]")
        baddocs = self.dir / "baddocs.json"
        baddocs.write_text(json.dumps({"schema": tool.SCHEMA, "documents": [1]}))
        nodocs = self.dir / "nodocs.json"
        nodocs.write_text(json.dumps({"schema": tool.SCHEMA}))
        link = self.dir / "link.json"
        link.symlink_to(good)
        fifo = self.dir / "fifo.json"
        os.mkfifo(fifo)
        cases = [("missing.json", "not-a-regular-file"), ("malformed.json", "not-json"), ("wrong.json", "wrong-schema"),
                 ("list.json", "wrong-schema"), ("baddocs.json", "bad-documents"), ("nodocs.json", "bad-documents"),
                 (".", "not-a-regular-file"), ("link.json", "not-a-regular-file"), ("fifo.json", "not-a-regular-file")]
        for name, error in cases:
            with self.subTest(name):
                path = str(self.dir / name)
                code, out, err = self.run_tool("--unique", good, path)
                self.assertEqual((code, json.loads(out), err), (1, {"schema": SCHEMA, "error": error, "input": 2}, ""))
                self.assertEqual(len(out.splitlines()), 1)
                for private in (self.tmp.name, name, A, "Traceback"):
                    self.assertNotIn(private, out + err)
        with mock.patch.object(tool, "MAX_BYTES", 10):
            code, out, err = self.run_tool("--unique", good)
        self.assertEqual((code, json.loads(out), err), (1, {"schema": SCHEMA, "error": "too-large", "input": 1}, ""))

    def test_first_refused_input_wins(self):
        good = self.write("good.json", [doc(A, ne.EXTRACTED)])
        (self.dir / "bad.json").write_text("{")
        code, out, _ = self.run_tool("--unique", good, str(self.dir / "bad.json"), str(self.dir / "missing.json"))
        self.assertEqual((code, json.loads(out)["input"], json.loads(out)["error"]), (1, 2, "not-json"))

    def test_exclusive_mode_usage_before_io(self):
        path = self.write("a.json", [doc(A, ne.EXTRACTED)])
        with mock.patch.object(tool, "load", side_effect=AssertionError("loader must not run")):
            for argv in (["--unique"], ["--unique", "--unique", path], ["--unique", "--total", path],
                         ["--total", path, "--unique"], ["--unique", "--self-test"], ["--self-test", "--unique", path],
                         ["--unique", "-x", path], ["--unique", path, "--uniques"], ["--UNIQUE", path]):
                with self.subTest(argv=argv):
                    code, out, err = self.run_tool(*argv)
                    self.assertEqual((code, out), (2, ""))
                    self.assertTrue(err.startswith("usage: records_extract_summary.py"))
        other = self.write("b.json", [doc(B, ne.TIMEOUT)])
        code, out, _ = self.run_tool(path, "--unique", other)  # the flag may sit between or after the files
        self.assertEqual((code, json.loads(out)["documents"]), (0, 2))
        self.assertEqual(self.run_tool(path, other, "--unique")[1], out)


class UniquePrivacyTest(UniqueBase):
    def test_counts_only_and_no_mutations(self):
        rows = [doc(A, ne.TIMEOUT, "extraction_timeout", ocr_pages=[{"page": 1, "text": "SENTINEL-TEXT"}],
                    source_path="/private/SENTINEL-PATH", detail="SENTINEL-ERROR")]
        old = self.write("sentinel-old.json", rows, run_id="SENTINEL-RUN", written_at="2026-10-31T08:00:00Z")
        new = self.write("sentinel-new.json", [doc(B, ne.EXTRACTED)], run_id="SENTINEL-RUN-2")

        def snapshot():
            paths = sorted(self.dir.iterdir())
            return [(p.name, p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in paths]

        before = snapshot()
        code, out, err = self.run_tool("--unique", old, new)
        self.assertEqual(code, 0)
        self.assertEqual(snapshot(), before)  # no input changed, no output file created
        self.assertEqual(set(json.loads(out)), KEYS)
        for leaked in (A, B, "SENTINEL", "xlsx", "2026-10-31", "sentinel-old", "sentinel-new", self.tmp.name, "run-"):
            self.assertNotIn(leaked, out + err)
        self.assertNotRegex(out, re.compile(r"[0-9a-f]{64}"))
        self.assertNotIn("%", out)

    def test_legacy_modes_remain_unchanged(self):
        path = self.write("legacy.json", [doc(A, ne.EXTRACTED), doc(B, ne.TIMEOUT, "extraction_timeout")], run_id="r1")
        code, out, _ = self.run_tool(path)
        line = json.loads(out)
        self.assertEqual((code, line["schema"], line["file"], line["run_id"], line["documents"]),
                         (0, "records-extract-summary-v1", "legacy.json", "r1", 2))
        code, out, _ = self.run_tool("--total", path, path)
        self.assertEqual((code, json.loads(out.splitlines()[-1])["repeated_run_ids"],
                          json.loads(out.splitlines()[-1])["documents"]), (0, 1, 4))  # observations, not distinct
        self.assertEqual(self.run_tool("--self-test")[1], "extract summary: self-test OK\n")
        # legacy permissive rows (no identity at all) still summarize; only --unique is strict
        loose = self.write("loose.json", [{"outcome": ne.EXTRACTED}, {"outcome": ne.EXTRACTED}])
        self.assertEqual(json.loads(self.run_tool(loose)[1])["documents"], 2)
        self.assertEqual(json.loads(self.run_tool("--unique", loose)[1])["error"], "bad-original-sha256")

    def test_cli_subprocess_smoke(self):
        import subprocess
        import sys
        old = self.write("o.json", [doc(A, ne.AWAITING_OCR), doc(B, ne.TIMEOUT)])
        new = self.write("n.json", [doc(A, ne.EXTRACTED)])
        done = subprocess.run([sys.executable, "-B", str(REPO / "tools/records_extract_summary.py"), "--unique", old, new],
                              capture_output=True, text=True, cwd=self.dir, timeout=60)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        line = json.loads(done.stdout)
        self.assertEqual((line["documents"], line["extracted"], line["replaced_observations"]), (2, 1, 1))


if __name__ == "__main__":
    unittest.main()
