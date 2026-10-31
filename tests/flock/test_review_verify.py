import csv, json, random, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

from tests.flock.test_report import LINE1
from tests.flock.test_review import FIX, ROOT, WAITS, make_review, mark_all, tool, write_stub

VERIFY = ROOT / "tools" / "flock_review_verify.py"
REASON = "private-reason-text"
HEAD = "row_id,result,reason,page_cite\n"
COUNTS = "included=5 verified_agree=5 unchecked=6 withheld_analysis_only=1 withheld_verify=0 sources=3 errors=0"  # report.log
BAD_LOG = ('if "verify-recount" in a[1]:\n    Path(a[1]).mkdir(parents=True, exist_ok=True)\n'
           '    (Path(a[1]) / "LOG-SUMMARY-by-user.csv").write_text("user,count\\nx,1\\n")\n    sys.exit(0)')
with open(FIX / "review-findings.csv", newline="") as handle:  # stage 28: one REQUIREMENTS row per finding req_id
    RULES = "REQS = %r" % "".join("%s,%s,%s,%s,\n" % (r["source"], r["version"], r["section"], r["req_id"]) for r in csv.DictReader(handle))


def vtool(*args):
    done = subprocess.run([sys.executable, "-B", str(VERIFY), *map(str, args)], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


def compare_build(edit=None):  # copies the two fixtures into CO; edit = (file, old, new) changes one of them
    body = ('out = Path(json.loads(Path(a[0]).read_text())["compare"]["out"]); out.mkdir(parents=True, exist_ok=True)\n'
            'for name, fixture in (("FINDINGS.csv", "review-findings.csv"), ("BUILD-REPORT.txt", "review-build-report.txt")):\n'
            '    text = (FIX / fixture).read_text()\n')
    if edit:
        body += '    text = text.replace(%r, %r) if name == %r else text\n' % (edit[1], edit[2], edit[0])
    return body + '    (out / name).write_text(text)\nsys.exit(0)'


class FlockReviewVerifyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def reach_verify(self, stubs=None):
        stubs = {"compare_build": compare_build(), "rules_build": RULES, **(stubs or {})}
        config, p = make_review(self.tmp, stubs=stubs, review__verify_sample=3, not_posted_analysis_only_ids=["99999999"])
        for stage, _, key, rel in WAITS:
            self.assertEqual(tool("run", config, full=True)[0], 3)
            if key:
                (p[key] / rel).parent.mkdir(parents=True, exist_ok=True)
                (p[key] / rel).write_text(stage + " input\n")
            self.assertEqual(mark_all(config, {stage: "anthropic"}), [0])
        code, out = tool("run", config, full=True)
        return config, p, code, out

    def plan(self, p):
        return json.loads((p["CO"] / "VERIFY-PLAN.json").read_text())

    def write_verify(self, p, ids=None, result="agree", md="notes\n"):
        spec = self.plan(p)
        rows = "".join("%s,%s,%s,p.2\n" % (i, result, REASON) for i in (spec["priority_ids"] + spec["sample_ids"] if ids is None else ids))
        (p["CO"] / "VERIFY.csv").write_text(HEAD + rows)
        (p["CO"] / "VERIFY.md").write_text(md)

    def test_plan_reuses_identical_parameters_without_writing(self):
        plan = self.tmp / "plan.json"
        args = ("plan", FIX / "review-findings.csv", "--seed", 17, "--size", 3, "--out", plan)
        self.assertEqual(vtool(*args)[0], 0)
        # Non-default formatting makes any regeneration visible in the bytes.
        plan.write_text(json.dumps(json.loads(plan.read_text()), indent=4) + "\n")
        frozen, before = plan.read_bytes(), plan.stat()
        code, out = vtool(*args)
        self.assertEqual(code, 0, out)
        self.assertEqual(plan.read_bytes(), frozen)
        after = plan.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertFalse(plan.with_name(plan.name + ".tmp").exists())

    def test_plan_rejects_parameter_changes(self):
        for seed, size in ((18, 3), (17, 4)):
            with self.subTest(seed=seed, size=size):
                plan = self.tmp / ("plan-%s-%s.json" % (seed, size))
                args = ("plan", FIX / "review-findings.csv")
                self.assertEqual(vtool(*args, "--seed", 17, "--size", 3, "--out", plan)[0], 0)
                frozen = plan.read_bytes()
                code, out = vtool(*args, "--seed", seed, "--size", size, "--out", plan)
                self.assertEqual(code, 1, out)
                self.assertIn("parameters", out)
                self.assertEqual(plan.read_bytes(), frozen)

    def test_plan_and_happy_path(self):
        config, p, code, out = self.reach_verify()
        self.assertEqual(code, 3, out)
        self.assertIn("stage verify WAITING (not marked)", out)
        self.assertNotIn("stage report", out)
        self.assertFalse((p["CO"] / "report").exists())
        spec = self.plan(p)
        pool = ["R001", "R004", "R005", "R007", "R008", "R010", "R011", "R012"]
        self.assertEqual((spec["priority_ids"], spec["pool_size"], spec["seed"]), (["R002", "R003", "R009"], 8, 20261030))
        self.assertEqual(spec["sample_ids"], random.Random(20261030).sample(pool, 3))
        self.assertEqual((p["CO"] / "VERIFY-PLAN.json").stat().st_mode & 0o777, 0o600)
        self.write_verify(p)
        self.assertEqual(mark_all(config, {"verify": "openai"}), [0])
        code, out = tool("run", config, full=True)
        self.assertEqual(code, 3, out)
        self.assertIn("stage f4 WAITING (not marked)", out)
        self.assertIn("planned=6 rows=6 agree=6 disagree=0 needs_source=0 missing=0 extra=0 errors=0", (p["CO"] / "VERIFY-CHECK.txt").read_text())
        self.assertEqual(json.loads((p["RUN"] / "VERIFY.json").read_text())["verifier_provider"], "openai")
        shutil.copy(FIX / "verify-sample.csv", p["RUN"] / "VERIFY.csv")
        self.assertEqual(mark_all(config, {"f4": "openai"}), [0])
        code, out = tool("run", config, full=True)
        self.assertEqual(code, 0, out)
        self.assertIn("public_ok=4 questions=2", (p["out"] / ".review/logs/f4-finish.log").read_text())
        self.assertIn("stage report done rc=0", out)
        report = [p["CO"] / "report" / name for name in ("REPORT-DRAFT.md", "SOURCES.json")]
        self.assertEqual([path.stat().st_mode & 0o777 for path in report], [0o600, 0o600])
        self.assertEqual(report[0].read_text().splitlines()[0], LINE1)
        self.assertEqual((p["out"] / ".review/logs/report.log").read_text().splitlines()[-1], COUNTS)
        code, out = tool("run", config, full=True)
        self.assertEqual((code, out.count("rc="), out.count("WAITING")), (0, 0, 0), out)
        self.assertTrue(tool("status", config)[1].endswith("stages=28 done=26 stale=0 waiting=0 pending=0 failed=0 skip=2 next=- locked=no\n"))

    def test_cached_report_invalidates_when_inventory_becomes_analysis_only(self):
        config, p, code, out = self.reach_verify()
        self.assertEqual(code, 3, out)
        self.write_verify(p)
        self.assertEqual(mark_all(config, {"verify": "openai"}), [0])
        self.assertEqual(tool("run", config, full=True)[0], 3)
        shutil.copy(FIX / "verify-sample.csv", p["RUN"] / "VERIFY.csv")
        self.assertEqual(mark_all(config, {"f4": "openai"}), [0])
        code, out = tool("run", config, full=True)
        self.assertEqual(code, 0, out)
        self.assertIn("stage report done rc=0", out)
        self.assertIn("stage report cached", tool("run", config, full=True)[1])
        report = p["CO"] / "report" / "REPORT-DRAFT.md"
        self.assertIn("document 1", report.read_text())
        state_path = p["out"] / "REVIEW-STATE.json"
        before = json.loads(state_path.read_text())["stages"]

        write_stub(p["method"], "inventory", 'ONLY = "yes"')
        code, out = tool("run", config, full=True)
        self.assertEqual(code, 3, out)
        self.assertIn("stage fact-drafts WAITING (upstream changed)", out)
        self.assertEqual(mark_all(config, {"fact-drafts": "anthropic"}), [0])
        for _ in range(2):
            code, out = tool("run", config, full=True)
            self.assertEqual(code, 1, out)
            self.assertIn("stage report failed rc=1", out)
            self.assertIn("cite resolves to an analysis-only record",
                          (p["out"] / ".review/logs/report.log").read_text())
            self.assertFalse(report.exists())
            self.assertFalse((p["CO"] / "report" / "SOURCES.json").exists())
        after = json.loads(state_path.read_text())["stages"]
        for stage in ("facts-import", "compare-build", "verify-plan", "verify", "verify-check"):
            self.assertEqual(before[stage]["outputs"], after[stage]["outputs"], stage)
        self.assertNotEqual(before["report"]["fp"], after["report"]["fp"])
        self.assertIn("28 report run failed", tool("status", config)[1])

    def test_report_fail_closed_and_recovers(self):
        config, p, _, out = self.reach_verify({"inventory": 'ONLY = "yes"'})  # the cited record is analysis-only
        self.write_verify(p)
        self.assertEqual(mark_all(config, {"verify": "openai"}), [0])
        self.assertEqual(tool("run", config, full=True)[0], 3)
        shutil.copy(FIX / "verify-sample.csv", p["RUN"] / "VERIFY.csv")
        self.assertEqual(mark_all(config, {"f4": "openai"}), [0])
        for _ in range(2):  # a failed report stage re-runs, still fails and writes nothing
            code, out = tool("run", config, full=True)
            self.assertEqual(code, 1, out)
            self.assertIn("stage report failed rc=1", out)
            self.assertFalse((p["CO"] / "report" / "REPORT-DRAFT.md").exists())
        self.assertIn("cite resolves to an analysis-only record", (p["out"] / ".review/logs/report.log").read_text())
        lines = tool("status", config)[1].splitlines()
        self.assertEqual({line.split()[3] for line in lines[:27]}, {"done", "skip"})
        self.assertEqual((lines[27], lines[28]), ("28 report run failed",
                                                  "stages=28 done=25 stale=0 waiting=0 pending=0 failed=1 skip=2 next=report locked=no"))
        write_stub(p["method"], "inventory")  # fixed: the record is posted
        code, out = tool("run", config, full=True)
        self.assertEqual(code, 3, out)
        self.assertIn("stage fact-drafts WAITING (upstream changed)", out)
        self.assertEqual(mark_all(config, {"fact-drafts": "anthropic"}), [0])
        code, out = tool("run", config, full=True)
        self.assertEqual(code, 0, out)
        self.assertIn("stage report done rc=0", out)
        self.assertEqual((p["CO"] / "report" / "REPORT-DRAFT.md").read_text().splitlines()[0], LINE1)

    def test_fail_closed(self):
        config, p, code, out = self.reach_verify()
        state = (p["out"] / "REVIEW-STATE.json").read_bytes()
        self.assertEqual([tool("mark", config, "verify", "--provider", x, "--model", "m")[0] for x in ("anthropic", "local")], [3, 3])
        self.assertEqual((p["out"] / "REVIEW-STATE.json").read_bytes(), state)
        plan, md = p["CO"] / "VERIFY-PLAN.json", p["CO"] / "VERIFY.md"
        self.write_verify(p)
        self.assertEqual(mark_all(config, {"verify": "openai"}), [0])
        state = (p["out"] / "REVIEW-STATE.json").read_bytes()
        self.assertEqual(tool("mark", config, "f4", "--provider", "xai", "--model", "m")[0], 3)
        self.assertEqual((p["out"] / "REVIEW-STATE.json").read_bytes(), state)
        spec, ids = self.plan(p), self.plan(p)["priority_ids"] + self.plan(p)["sample_ids"]
        args = (plan, p["CO"] / "FINDINGS.csv", p["CO"] / "VERIFY.csv", "--not-posted", "99999999", "--md", md)
        for ids_in, result, expected in ((ids[:-1], "agree", "missing=1"), (ids + ["R999"], "agree", "extra=1"), (ids, "maybe", "row 2: result")):
            self.write_verify(p, ids_in, result)
            code, out = vtool("check", *args)
            self.assertEqual(code, 1)
            self.assertIn(expected, out)
        self.write_verify(p, md="see 99999999\n")
        code, out = vtool("check", *args)
        self.assertEqual(code, 1)
        self.assertIn("not-posted: VERIFY.md", out)
        self.assertNotIn("99999999", out)
        self.assertEqual(tool("--check", config)[0], 0)
        bad, _ = make_review(self.tmp / "cfg", review__verify_sample=0)
        self.assertIn("config: review.verify_sample", tool("--check", bad)[1])

    def test_fail_closed_upstream(self):
        config, p, _, _ = self.reach_verify()
        self.write_verify(p)
        self.assertEqual(mark_all(config, {"verify": "openai"}), [0])
        old = self.plan(p)["findings_sha256"]
        write_stub(p["method"], "compare_build", compare_build(("FINDINGS.csv", "Request the sample record.", "Request the record.")))
        code, out = tool("run", config, full=True)
        self.assertEqual(code, 3, out)
        self.assertIn("stage verify WAITING (upstream changed)", out)
        self.assertEqual(len(list(p["out"].glob(".review/superseded/verify-plan-*/VERIFY-PLAN.json"))), 1)
        self.assertNotEqual(self.plan(p)["findings_sha256"], old)

    def test_counts_and_recount_fail(self):
        self.tmp = self.tmp / "counts"
        config, p, code, out = self.reach_verify({"compare_build": compare_build(("BUILD-REPORT.txt", "'met': 3", "'met': 4"))})
        self.assertEqual(code, 1, out)
        self.assertIn("stage verify-plan failed rc=1", out)
        self.assertIn("counts: requirement differs", (p["out"] / ".review/logs/verify-plan.log").read_text())
        self.tmp = self.tmp.parent / "recount"
        config, p, code, out = self.reach_verify({"log_summary": BAD_LOG})
        self.assertEqual(code, 1, out)
        self.assertIn("stage verify-recount failed rc=1", out)
        self.assertIn("recount: LOG-SUMMARY-by-user.csv differs", out)

    def test_text_hygiene(self):
        config, p, _, out = self.reach_verify()
        seen = [out]
        self.write_verify(p)
        self.assertEqual(mark_all(config, {"verify": "openai"}), [0])
        seen.append(tool("run", config, full=True)[1])
        self.write_verify(p, result="maybe")
        seen += [vtool("check", p["CO"] / "VERIFY-PLAN.json", p["CO"] / "FINDINGS.csv", p["CO"] / "VERIFY.csv")[1], tool("run", config, full=True)[1]]
        self.assertFalse([text for text in seen if REASON in text])
        self.assertNotIn(REASON, (p["CO"] / "VERIFY-CHECK.txt").read_text())


if __name__ == "__main__":
    unittest.main()
