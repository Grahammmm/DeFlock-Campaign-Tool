"""Acceptance tests for ``run_report_review.py summary --json`` (run-report-review-v1); synthetic reports only."""
import contextlib
import hashlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import run_report_review as rrr  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "run_report"
EXAMPLE = ROOT / "examples" / "run-report" / "records-run-report-v1.synthetic.json"
BANNED = re.compile(r"\b(late|overdue)\b|violat|unlawful|illegal|non-?complian|breach", re.I)
WORK_STAGES = ["sync", "delta", "adoption", "portal", "extract", "digest_gates", "outputs"]
UNMAPPED = "unmapped -> operator"
# Text-mode golden: int(sha256("<exit>\n<stdout>\n<stderr>")[:15], 16), captured from origin/main 88edf506 before the
# refactor (integers, not hex strings, so the secret scanner does not flag them).
GOLDEN = {
    "fixture baseline-20261023.json": 168867881079330152, "fixture counts-mismatch.json": 973542675544121546,
    "fixture missing-stage.json": 694012944527386070, "fixture notify-failed.json": 842304270098643327,
    "fixture pass.json": 973542675544121546, "fixture t15-cases.json": 168867881079330152,
    "fixture widened-gap.json": 927768289331181097, "fixture baseline-20261023.json --now": 168867881079330152,
    "fixture counts-mismatch.json --now": 503103643245021271, "fixture missing-stage.json --now": 694012944527386070,
    "fixture notify-failed.json --now": 301442045468538229, "fixture pass.json --now": 503103643245021271,
    "fixture t15-cases.json --now": 168867881079330152, "fixture widened-gap.json --now": 240223905700295100,
    "fixtures --dir": 907452926598807666, "example": 990805012492863547, "scheduled": 431513167211531528,
    "stub": 925396520314218158, "stub sync": 915191564615326097, "worded": 155184944986449551,
    "invalid": 249718414660910697, "unknown": 562273950535342838, "notify only": 483308611948801194,
    "unreadable": 1046820183655684569, "age 40h": 131863410784633466, "future": 967928934603896643,
    "calendar-invalid now": 396257828963960833, "shape-invalid now": 1057768397968758950,
    "dupes --dir": 562495717991236580,
}


def example(**top):  # fixture builders copied from tests/test_run_report_review.py
    report = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    report.update(top)
    return report


def stage(report, name):
    return next(s for s in report["stages"] if s["name"] == name)


def set_status(report, name, status, code):
    s = stage(report, name)
    s["status"], s["error_code"] = status, code
    if status == "skipped":
        s["duration_seconds"] = None
    statuses = {x["status"] for x in report["stages"]}
    report["overall_status"] = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "ok"
    return report


def stubbed(stage_name="extract"):
    report = example()
    report["needs_g"].append({"code": "STUB_STAGE_BACKEND", "stage": stage_name, "severity": "info", "count": 1})
    return report


def worded():
    report = example()
    for code in ("APPROVED", "SOURCE_BREACH_FOUND"):
        report["needs_g"].append({"code": code, "stage": "outputs", "severity": "info", "count": 1})
    report["needs_g"].append({"code": "LEGAL_REVIEW_REQUIRED", "stage": "digest_gates", "severity": "blocking", "count": 4})
    report["ocr"] = {"pages_ocr": 6, "pages_conf_ge80": 4, "pages_conf_lt80": 1, "pages_conf_unknown": 1,
                     "pages_reviewed": 4, "awaiting_ocr_images": 0}
    outcomes = stage(report, "extract").setdefault("outcomes", [])
    outcomes[:] = [{"code": "EXTRACTED", "count": 3}, {"code": "VIOLATION_FOUND", "count": 2}]
    return report


class RunReportReviewJsonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, report, name="r.json", directory=None, raw=None):
        directory = directory or self.tmp
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_bytes(raw if raw is not None else json.dumps(report).encode())
        return path

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rrr.main(["summary", *map(str, argv)])
        return code, out.getvalue(), err.getvalue()

    def doc(self, *argv):
        code, out, err = self.run_main(*argv, "--json")
        self.assertEqual(err, "")
        self.assertNotIn(str(self.tmp), out)
        self.assertIsNone(BANNED.search(out), out)
        doc = json.loads(out)
        self.assertEqual(out, json.dumps(doc, indent=1, sort_keys=True) + "\n")
        self.assertEqual((doc["schema"], doc["review_required"], doc["reviewed"]),
                         ("run-report-review-v1", True, "none by this tool"))
        return code, doc

    def golden_cases(self):
        """(name, argv) for every fixture file and the edge inputs below; text output must equal main's."""
        cases = [(f"fixture {p.name}", [p]) for p in sorted(FIXTURES.glob("*.json"))]
        cases += [(f"fixture {p.name} --now", [p, "--now", "2026-12-01T00:00:00Z"]) for p in sorted(FIXTURES.glob("*.json"))]
        cases.append(("fixtures --dir", ["--dir", FIXTURES]))
        reports = {"example": example(), "scheduled": example(mode="scheduled"), "stub": stubbed(),
                   "stub sync": stubbed("sync"), "worded": worded(), "invalid": example(overall_status="ok"),
                   "unknown": set_status(set_status(example(), "extract", "skipped", None), "portal", "fail", "PORTAL_FETCH_BLOCKED"),
                   "notify only": set_status(example(notify_failed=True), "notify", "fail", "NOTIFY_FAILED")}
        for name, report in reports.items():
            cases.append((name, [self.write(report, name.replace(" ", "_") + ".json", self.tmp / "single")]))
        cases.append(("unreadable", [self.write(None, "bad.json", raw=b"{not json")]))
        cases.append(("age 40h", [EXAMPLE, "--now", "2026-01-02T22:07:30Z"]))
        cases.append(("future", [EXAMPLE, "--now", "2026-01-01T06:07:29Z"]))
        cases.append(("calendar-invalid now", [EXAMPLE, "--now", "2026-02-30T00:00:00Z"]))
        cases.append(("shape-invalid now", [EXAMPLE, "--now", "2026-01-02 22:07"]))
        cases.append(("dupes --dir", ["--dir", self.dupes_dir()]))
        return cases

    def dupes_dir(self):
        d = self.tmp / "dupes"
        report = example()
        self.write(report, "a.json", d)
        self.write(report, "b.json", d)  # identical to a.json
        self.write(None, "c.json", d, raw=json.dumps(report, indent=2).encode())  # same run_id, contents differ
        self.write(None, "d.json", d, raw=b"[]")
        return d

    # 7 + exit parity: text stdout/stderr/exit byte-identical to main; --json exits the same, no JSON on refusal
    def test_text_golden_and_exit_parity(self):
        for name, argv in self.golden_cases():
            with self.subTest(name):
                code, out, err = self.run_main(*argv)
                digest = int(hashlib.sha256(f"{code}\n{out}\n{err}".encode()).hexdigest()[:15], 16)
                self.assertEqual(digest, GOLDEN[name], name)
                jcode, jout, jerr = self.run_main(*argv, "--json")
                self.assertEqual(jcode, code)
                if code == 2:
                    self.assertEqual((jout, jerr), ("", err))
                else:
                    self.assertEqual(jerr, "")
                    self.assertEqual(max(r["status"] for r in json.loads(jout)["reports"]), code)

    # 1
    def test_pass_fixture_values_equal_text(self):
        code, doc = self.doc(FIXTURES / "pass.json")
        self.assertEqual(code, 0)
        self.assertEqual(doc["counts"], {"reports": 1, "valid": 1, "invalid": 0, "duplicates": 0})
        self.assertEqual(doc["duplicates"], [])
        (r,) = doc["reports"]
        self.assertEqual((r["run_id"], r["valid"], r["diagnostic"], r["status"]), ("run_20000101T000000Z_00000000", True, None, 0))
        self.assertEqual([w["stage"] for w in r["work"]], WORK_STAGES)
        self.assertEqual([(w["status"], w["output"]) for w in r["work"]], [("ok", 1)] * 4 + [("ok", 0)] + [("ok", 1)] * 2)
        self.assertEqual((r["mode"], r["test_only"], r["stub"]), ("test", True, False))
        self.assertEqual(r["outcomes"], [{"code": "AWAITING_OCR", "count": 1}, {"code": "CORRUPT_DOCUMENT", "count": 22},
                                         {"code": "EXTRACT_NOT_ACCEPTED", "count": 1}, {"code": "PRESERVE_PENDING", "count": 400}])
        self.assertEqual((r["unmapped"], r["notify"], r["rollup"], r["review_required"]), (0, "ok", "ok", 0))
        self.assertEqual((r["ocr"], r["awaiting_ocr"], r["age_hours"]), (None, 1, None))
        self.assertEqual(len(r["owed"]), 8)
        self.assertEqual(r["owed"][0], {"code": "ADOPTION_CODED_GAPS", "count": 116, "severity": "info", "gate": "operator (no gate)"})
        self.assertEqual(r["owed"][2]["gate"], UNMAPPED)
        _, text, _ = self.run_main(FIXTURES / "pass.json")
        self.assertEqual("\n".join(rrr.render(r)) + "\n", text)  # the text is rendered from these same values
        self.assertNotIn("pass.json", json.dumps(doc))

    # 2
    def test_invalid_report_has_diagnostic_and_no_work(self):
        for argv, diag in (([FIXTURES / "missing-stage.json"], "$.stages"),
                           ([self.write(None, raw=b"{not json")], "unreadable JSON"),
                           ([self.write(example(overall_status="ok"), "rollup.json")], "$.overall_status")):
            with self.subTest(diag):
                code, doc = self.doc(*argv)
                _, text, _ = self.run_main(*argv)
                self.assertEqual(code, 1)
                (r,) = doc["reports"]
                self.assertEqual(set(r), {"run_id", "valid", "diagnostic", "status"})
                self.assertEqual((r["valid"], r["diagnostic"], r["status"]), (False, diag, 1))
                self.assertEqual(text.splitlines()[0].split(" | ", 4)[4], diag)
                self.assertEqual(doc["counts"], {"reports": 1, "valid": 0, "invalid": 1, "duplicates": 0})
        self.assertEqual(self.doc(self.write(None, raw=b"{not json"))[1]["reports"][0]["run_id"], "unknown")

    # 3
    def test_dir_lists_both_duplicate_kinds(self):
        code, doc = self.doc("--dir", self.dupes_dir())
        self.assertEqual(code, 1)
        run_id = example()["run_id"]
        self.assertEqual(sorted(d["kind"] for d in doc["duplicates"]), ["contents differ", "contents differ", "identical"])
        self.assertEqual({d["run_id"] for d in doc["duplicates"]}, {run_id})
        self.assertEqual(doc["counts"], {"reports": 3, "valid": 2, "invalid": 1, "duplicates": 3})
        self.assertEqual([(r["run_id"], r["valid"]) for r in doc["reports"]], [("unknown", False), (run_id, True), (run_id, True)])
        self.assertEqual([r["status"] for r in doc["reports"]], [1, 1, 1])  # a differing duplicate contributes exit 1
        _, text, _ = self.run_main("--dir", self.dupes_dir())
        self.assertTrue(text.endswith("review | reports=3 valid=2 invalid=1 duplicates=3 | machine-valid is not reviewed\n"))
        code, doc = self.doc("--dir", FIXTURES)
        self.assertEqual((code, doc["counts"]), (1, {"reports": 7, "valid": 4, "invalid": 3, "duplicates": 4}))
        self.assertEqual([d["kind"] for d in doc["duplicates"]], ["contents differ"] * 4)
        clean = self.tmp / "clean"
        self.write(example(), "a.json", clean)
        self.write(example(), "b.json", clean)
        code, doc = self.doc("--dir", clean)  # identical only: listed once, not repeated in reports, exit 0
        self.assertEqual((code, len(doc["reports"]), doc["duplicates"]), (0, 1, [{"run_id": example()["run_id"], "kind": "identical"}]))

    # 4
    def test_stub_marks_output_and_awaiting_unknown(self):
        code, doc = self.doc(self.write(stubbed()))
        r = doc["reports"][0]
        self.assertEqual((code, r["stub"]), (0, True))
        self.assertEqual(r["work"][4], {"stage": "extract", "status": "ok", "output": None})
        self.assertEqual(r["work"][0]["output"], 3)
        self.assertEqual((r["awaiting_ocr"], r["outcomes"], r["unmapped"]), (None, None, None))
        r = self.doc(self.write(stubbed("sync")))[1]["reports"][0]
        self.assertEqual((r["stub"], r["work"][0]["output"]), (True, None))
        self.assertIsNotNone(r["work"][4]["output"])
        self.assertIsNotNone(r["awaiting_ocr"])
        # not stubbed but skipped/failed: output unknown, never 0; stub stays false
        code, doc = self.doc(self.write(set_status(set_status(example(), "extract", "skipped", None),
                                                   "portal", "fail", "PORTAL_FETCH_BLOCKED")))
        r = doc["reports"][0]
        self.assertEqual((code, r["stub"], r["work"][3], r["work"][4]["output"], r["outcomes"]),
                         (1, False, {"stage": "portal", "status": "fail", "output": None}, None, None))

    # 5
    def test_withheld_and_unmapped_codes(self):
        code, doc = self.doc(self.write(worded()))
        r = doc["reports"][0]
        owed = {o["code"]: o for o in r["owed"]}
        self.assertEqual(owed["WITHHELD_CODE"], {"code": "WITHHELD_CODE", "count": 1, "severity": "info", "gate": UNMAPPED})
        self.assertEqual(owed["APPROVED"]["gate"], UNMAPPED)
        self.assertEqual(owed["LEGAL_REVIEW_REQUIRED"]["gate"], rrr.GATE_3)
        self.assertEqual(owed["GATE_REVIEW_REQUIRED"]["gate"], rrr.GATE_2)
        self.assertEqual(r["outcomes"], [{"code": "EXTRACTED", "count": 3}, {"code": "WITHHELD_CODE", "count": 2}])
        self.assertEqual(r["unmapped"], 1)
        self.assertEqual(r["ocr"], {"pages_ocr": 6, "pages_reviewed": 4, "pages_unreviewed": 2})
        self.assertEqual(code, 0)

    # 6
    def test_now_future_and_invalid(self):
        code, doc = self.doc(EXAMPLE, "--now", "2026-01-01T06:07:29Z")
        self.assertEqual((code, doc["reports"][0]["age_hours"], doc["reports"][0]["status"]), (1, "future", 1))
        code, doc = self.doc(EXAMPLE, "--now", "2026-01-02T22:07:30Z")
        self.assertEqual((code, doc["reports"][0]["age_hours"]), (0, 40))
        for bad in ("2026-02-30T00:00:00Z", "2026-01-02T25:00:00Z", "2026-01-02 22:07"):
            with self.subTest(bad):
                code, out, err = self.run_main(EXAMPLE, "--now", bad, "--json")
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err.startswith("review | refused | --now "), err)
        code, out, err = self.run_main("--json")
        self.assertEqual((code, out, err), (2, "", "review | refused | give exactly one of REPORT.json or --dir\n"))


if __name__ == "__main__":
    unittest.main()
