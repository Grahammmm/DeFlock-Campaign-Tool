"""flock_report check --json and region --check --json: one machine document with what the text check says (RF1100-9)."""
import json, os, subprocess, sys, unittest

from tests.flock import test_report as base
from tests.flock import test_report_check as tc
from tests.flock import test_report_region as reg
from tests.flock import test_report_region_check as trc

A, B, NAMES = reg.A, reg.B, reg.NAMES
COUNTS = {"included": 3, "verified_agree": 3, "unchecked": 3, "withheld_analysis_only": 1, "withheld_verify": 1, "sources": 3}
ZERO = dict.fromkeys(COUNTS, 0)
CANARY = "Canary QZXW9 kept in a link target."
FILES = ("REPORT-DRAFT.md", "SOURCES.json")


def check_doc(state, files, errors, counts=COUNTS):
    return {"schema": "flock-report-check-v1", "review_required": True, "state": state, "files": dict(zip(FILES, files)),
            "counts": counts, "errors": errors}


def region_doc(files, agencies=(), counts=None, errors=()):
    return {"schema": "flock-report-region-check-v1", "review_required": True, "files": dict(zip(NAMES, files)),
            "agencies": [{"slug": s, "state": st} for s, st in agencies], "counts": counts or {}, "errors": list(errors)}


class Leaks:
    def parsed(self, stdout):  # exactly one document: sorted keys, indent 1, no draft text, canary or absolute path
        data = json.loads(stdout)
        self.assertEqual(stdout, json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        for text in (base.LINE1, "Research draft", base.BASIS, CANARY, "QZXW", str(self.tmp), str(base.ROOT), "Hand-added"):
            self.assertNotIn(text, stdout)
        return data


class FlockReportCheckJsonTest(Leaks, unittest.TestCase):
    setUp, stage = base.FlockReportTest.setUp, base.FlockReportTest.stage
    built, snap, checked = tc.FlockReportCheckTest.built, tc.FlockReportCheckTest.snap, tc.FlockReportCheckTest.checked

    def both(self, config, *extra):  # text and --json from the same tree: same exit, same error lines, same counts and state
        code, text = self.checked(config, *extra)
        jcode, stdout = self.checked(config, *extra, "--json")
        data = self.parsed(stdout)
        self.assertEqual(jcode, code)
        self.assertEqual(data["errors"], text.splitlines()[:-1])
        last = dict(kv.split("=") for kv in text.splitlines()[-1].split())
        self.assertEqual(({k: int(last[k]) for k in COUNTS}, last["state"]), (data["counts"], data["state"]))
        return code, text, data

    def test_current_and_text_golden(self):
        config, p, _ = self.built()
        code, text, data = self.both(config)
        self.assertEqual((code, text), (0, tc.CURRENT))
        self.assertEqual(data, check_doc("current", ("current", "current"), []))

    def test_stale_missing_link_not_a_file(self):
        config, p, report = self.built()
        draft, sources = report / FILES[0], report / FILES[1]
        draft.write_text(draft.read_text() + "- **Hand-added line** [1 p.9]\n")
        code, text, data = self.both(config)
        self.assertEqual(code, 1)
        self.assertEqual(data, check_doc("stale", ("stale", "current"), [tc.DIFFERS[:-1] % FILES[0]]))
        os.unlink(sources)
        code, text, data = self.both(config)
        self.assertEqual(code, 1)
        self.assertEqual(data, check_doc("missing", ("stale", "missing"), [tc.MISSING[:-1] % FILES[1]]))
        config, p, report = self.built()
        draft, sources = report / FILES[0], report / FILES[1]
        target = self.tmp / "target.md"  # identical bytes plus a canary: following the link would print current or the canary
        target.write_text(draft.read_text() + CANARY + "\n")
        os.unlink(draft)
        draft.symlink_to(target)
        code, text, data = self.both(config)
        self.assertEqual(code, 1)
        self.assertEqual(data, check_doc("invalid", ("link", "current"), [tc.LINK[:-1] % FILES[0]]))
        os.unlink(sources)
        os.mkfifo(sources)  # never waits on a fifo
        code, text, data = self.both(config)
        self.assertEqual(data["files"], dict(zip(FILES, ("link", "not-a-file"))))
        self.assertEqual(data["state"], "invalid")

    def test_build_error_files_not_checked(self):
        config, p, _ = self.built()
        plan = json.loads((p["CO"] / "VERIFY-PLAN.json").read_text())
        plan["findings_sha256"] = "0" * 64
        (p["CO"] / "VERIFY-PLAN.json").write_text(json.dumps(plan))
        code, text, data = self.both(config)
        self.assertEqual(code, 1)
        self.assertEqual(data["errors"], ["verify: VERIFY-PLAN findings_sha256 does not match FINDINGS.csv"])
        self.assertEqual((data["state"], data["files"]), ("invalid", dict.fromkeys(FILES, "not-checked")))
        inside = base.ROOT / "tests" / "fixtures" / "flock" / "report" / "out"
        code, text, data = self.both(config, "--out", inside)
        self.assertEqual(data, check_doc("invalid", ("not-checked", "not-checked"), ["out: is inside the repository"], ZERO))
        self.assertFalse(inside.exists())

    def test_build_json_is_usage(self):
        config, p = self.stage()
        before = self.snap()
        done = subprocess.run([sys.executable, "-B", str(base.TOOL), "build", str(config), "--json"],
                              capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertIn("--json works with check only", done.stderr)
        self.assertEqual(self.snap(), before)
        self.assertFalse((p["CO"] / "report").exists())


class FlockReportRegionCheckJsonTest(Leaks, unittest.TestCase):
    setUp, stage = base.FlockReportTest.setUp, base.FlockReportTest.stage
    agency_b, two, run_region = reg.FlockReportRegionTest.agency_b, reg.FlockReportRegionTest.two, reg.FlockReportRegionTest.run_region
    snapshot, check, built = (trc.FlockReportRegionCheckTest.snapshot, trc.FlockReportRegionCheckTest.check,
                              trc.FlockReportRegionCheckTest.built)

    def both(self, ag, matrix, out, golden):  # text equals main's golden; --json has the same exit and no stderr
        code, text, err = self.check(ag, matrix, out)
        self.assertEqual((text, err), (golden, ""))
        jcode, stdout, err = self.check(ag, matrix, out, "--json")
        self.assertEqual((jcode, err), (code, ""))
        return code, self.parsed(stdout)

    def test_current_agencies_sorted(self):
        ag, matrix, out = self.built()
        code, data = self.both(ag, matrix, out, trc.expect("current", "current"))
        self.assertEqual(code, 0)
        self.assertLess(B, A)  # the matrix lists A first; the document sorts by slug
        self.assertEqual(data, region_doc(("current", "current"), ((B, "current"), (A, "current")),
                                          {"current": 2, "stale": 0, "missing": 0, "link": 0, "agencies_current": 2, "excluded": 0}))

    def test_agency_stale_excluded(self):
        ag, matrix, out = self.built()
        draft = ag[A][1]["CO"] / "report" / "REPORT-DRAFT.md"
        draft.write_text(draft.read_text() + "- **Hand-added line** [1 p.9]\n")
        code, data = self.both(ag, matrix, out, trc.expect("stale", "stale", a="stale"))
        self.assertEqual(code, 1)
        self.assertEqual(data, region_doc(("stale", "stale"), ((B, "current"), (A, "stale")),
                                          {"current": 0, "stale": 2, "missing": 0, "link": 0, "agencies_current": 1, "excluded": 1}))

    def test_region_link_target_never_read(self):
        ag, matrix, out = self.built()
        target = self.tmp / "target.md"
        target.write_text((out / NAMES[0]).read_text() + CANARY + "\n")
        os.unlink(out / NAMES[0])
        (out / NAMES[0]).symlink_to(target)
        os.unlink(out / NAMES[1])
        code, data = self.both(ag, matrix, out, trc.expect("link", "missing"))
        self.assertEqual(code, 1)
        self.assertEqual(data["files"], dict(zip(NAMES, ("link", "missing"))))
        self.assertEqual(data["counts"], {"current": 0, "stale": 0, "missing": 1, "link": 1, "agencies_current": 2, "excluded": 0})

    def test_invalid_build(self):
        ag, matrix, out = self.built()
        (matrix / "COMPARE-MATRIX.csv").write_text(reg.MATRIX.replace("topic,claim_class", "claim_class,topic", 1))
        line = "matrix: COMPARE-MATRIX.csv header is not topic, claim_class, then each agency slug once"
        code, data = self.both(ag, matrix, out, line + "\nregion-check: invalid\n")
        self.assertEqual((code, data), (1, region_doc((), errors=[line])))
        (matrix / "COMPARE-MATRIX.csv").write_text(reg.MATRIX)
        swapped = {A: ag[B], B: ag[A]}
        lines = ["agency %s: config: review.slug is not the matrix column" % s for s in (A, B)]
        code, data = self.both(swapped, matrix, out, "\n".join(lines) + "\nregion-check: invalid\n")
        self.assertEqual((code, data), (1, region_doc((), errors=lines)))

    def test_json_without_check_is_usage(self):
        ag, matrix, out = self.two()
        before = self.snapshot()
        for extra in (("--json",), ("--json", "--replace"), ("--check", "--json", "--replace")):
            code, stdout, stderr = self.run_region(ag, matrix, out, *extra)
            self.assertEqual((code, stdout), (2, ""), extra)
            self.assertIn("--json needs --check" if "--check" not in extra else "cannot be combined", stderr)
        self.assertTrue(before == self.snapshot(), "a --json usage error wrote a file")
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
