"""DOC-4: run the documented coverage procedure (docs/RECORDS-COVERAGE-REPORT.md) on a synthetic records root."""
import csv
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = (REPO / "docs" / "RECORDS-COVERAGE-REPORT.md").read_text(encoding="utf-8")
STEPS = re.findall(r"```sh\n(.*?)```", DOC, re.S)  # [extract summary, store check, blobs join]
_spec = importlib.util.spec_from_file_location("flock_facts_for_report", REPO / "tools" / "flock_facts.py")
facts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(facts)
SHA = {key: hashlib.sha256(b"coverage-" + key.encode()).hexdigest() for key in "ABC"}
HEX = re.compile(r"[0-9a-f]{64}")
NOT_HELD = {"EXTRACTED", "AWAITING_OCR", "PRESERVE_PENDING", "DEFERRED_RUN_BUDGET"}
FIXTURE = {"EXTRACTED": 3, "PRESERVE_PENDING": 2, "AWAITING_OCR": 1, "CORRUPT_DOCUMENT": 1, "PARSER_MISSING": 2,
           "DEFERRED_RUN_BUDGET": 1, "NEW_CODE": 1}
LABELS = ["(1) live originals with extracted text:", "(2) PRESERVE_PENDING:", "(3) AWAITING_OCR:", "(4) held, by code:",
          "(5) stored facts citing a sha256 absent from live blobs/:"]


def fact(sha, page):
    row = dict(agency="Synthetic City", request="R-1", fact_type="camera_count", value="4", source_sha256=sha,
               page=str(page), verbatim_quote="four cameras", kind="record", effective_from="", effective_to="",
               added_by="tester", added_at="2026-11-01", status="unreviewed")
    return dict(row, fact_id=facts.fact_id(row))


class RecordsCoverageReportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root, self.store = self.base / "root", self.base / "store"
        for folder in (self.root / "runs", self.root / "blobs", self.base / "scratch"):
            folder.mkdir(parents=True)
        for key in "AB":  # C is cited by two facts but never stored
            (self.root / "blobs" / SHA[key]).write_bytes(key.encode())
        self.extract("extract-run-2.json", FIXTURE, when=2_000_000_000)
        self.write_facts([fact(SHA["A"], 1), fact(SHA["B"], 1), fact(SHA["C"], 1), fact(SHA["C"], 2)])

    def extract(self, name, counts, when, raw=None):
        docs = [{"original_sha256": SHA["A"], "form": "xlsx", "outcome": code} for code, n in counts.items() for _ in range(n)]
        path = self.root / "runs" / name
        path.write_bytes(raw if raw is not None else json.dumps({"schema": "records-extract-outcomes-v1",
                                                                 "run_id": "run-2", "documents": docs}).encode())
        os.utime(path, (when, when))

    def write_facts(self, rows, extra=()):
        path = self.store / "agencies" / "synthetic-city" / "FACTS.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as out:
            csv.writer(out).writerows([facts.V2] + [[r[k] for k in facts.V2] for r in rows] + list(extra))

    def sh(self, step):
        script = STEPS[step].replace("python3 -B", shlex.quote(sys.executable) + " -B")
        env = dict(os.environ, ROOT=str(self.root), STORE=str(self.store), TMPDIR=str(self.base / "scratch"))
        done = subprocess.run(["sh"], input=script, cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
        out = done.stdout + done.stderr  # edge 4: the full captured output, both streams
        self.assertIsNone(HEX.search(out))
        self.assertNotIn("/", out)
        self.assertNotIn("xlsx", out)
        self.assertNotIn('"form"', out)
        return done.returncode, done.stdout

    def report(self):
        """Fill the template lines exactly as the doc's reading rules say."""
        rc, out = self.sh(0)
        line = json.loads(out)
        self.assertEqual(rc, 1 if "error" in line else 0)
        if "error" in line:
            first = ["not measured: " + line["error"]] * 4
        else:
            got = line["outcomes"]
            held = ["%s=%d" % (code, n) for code, n in sorted(got.items()) if n > 0 and code not in NOT_HELD]
            held += ["unknown=%d" % line["unknown_outcomes"]] * bool(line["unknown_outcomes"])
            first = [str(got["EXTRACTED"]), str(got["PRESERVE_PENDING"]), str(got["AWAITING_OCR"]), ", ".join(held) or "none"]
        check_rc, check_out = self.sh(1)
        join_rc, join = self.sh(2)
        self.assertEqual(join_rc, 0)
        if check_rc or check_out.strip():
            fifth = "not measured: fact store has errors"
        elif join.strip() == "blobs: not measured":
            fifth = "not measured: no blobs folder"
        else:
            vals = dict(pair.split("=") for pair in join.split())
            fifth = "%s facts (%s distinct sha256)" % (vals["facts_citing_missing"], vals["distinct_missing_sha256"])
        self.assertEqual(list(self.base.joinpath("scratch").iterdir()), [])  # the scratch folder is removed
        return first + [fifth]

    def test_doc_has_three_steps_and_the_template_lines(self):
        self.assertEqual(len(STEPS), 3)
        self.assertTrue(all(label in DOC for label in LABELS))
        self.assertNotIn("tools/", "".join(s for s in STEPS[2:]))  # the join is shell only, no tool

    def test_five_numbers_equal_the_fixture(self):
        self.assertEqual(self.report(), ["3", "2", "1", "CORRUPT_DOCUMENT=1, PARSER_MISSING=2, unknown=1",
                                         "2 facts (1 distinct sha256)"])

    def test_held_lists_only_positive_codes_and_none_when_all_extracted(self):
        self.extract("extract-run-3.json", {"EXTRACTED": 2, "DEFERRED_RUN_BUDGET": 1}, when=2_000_000_100)
        self.assertEqual(self.report()[:4], ["2", "0", "0", "none"])

    def test_empty_blobs_makes_every_cited_fact_missing(self):
        for key in "AB":
            (self.root / "blobs" / SHA[key]).unlink()
        (self.root / "blobs" / ".pending-0").write_bytes(b"x")  # an unfinished write is not a blob
        self.assertEqual(self.report()[4], "4 facts (3 distinct sha256)")

    def test_header_only_facts_gives_zero(self):
        self.write_facts([])
        self.assertEqual(self.report()[4], "0 facts (0 distinct sha256)")

    def test_malformed_newest_extract_is_not_measured_not_zero(self):
        self.extract("extract-run-3.json", {}, when=2_000_000_100, raw=b"{not json")
        self.assertEqual(self.report(), ["not measured: not-json"] * 4 + ["2 facts (1 distinct sha256)"])

    def test_missing_extract_file_is_not_measured(self):
        (self.root / "runs" / "extract-run-2.json").unlink()
        self.assertEqual(self.report()[:4], ["not measured: not-a-regular-file"] * 4)

    def test_store_errors_or_missing_blobs_make_five_not_measured(self):
        self.write_facts([fact(SHA["C"], 1)], extra=[["Synthetic City", "R-1", "x", "4", "XYZ", "1", "q", "F-0",
                                                      "record", "", "", "t", "2026-11-01", "unreviewed"]])
        self.assertEqual(self.report()[4], "not measured: fact store has errors")
        self.write_facts([fact(SHA["C"], 1)])
        (self.root / "blobs").rename(self.base / "elsewhere")
        self.assertEqual(self.report()[4], "not measured: no blobs folder")


if __name__ == "__main__":
    unittest.main()
