"""flock_review facts-import stages, reconciles and records status-blind projection outputs; a human review keeps the DAG valid (DFGL-07e)."""
import contextlib, csv, importlib.util, io, json, os, subprocess, sys, unittest
from pathlib import Path
from unittest.mock import patch

from tests.flock import test_review as base
from tests.flock.test_review import FIX, SLUG, TOOL, WAITS, calls, make_review, mark_all, tool, write_stub

FACTS_TOOL = base.ROOT / "tools" / "flock_facts.py"
DRAFT = "(Path(a[1]) / \"FACTS-v1-record.csv\").write_text((FIX / \"compare-facts.csv\").read_text()%s); sys.exit(0)"
spec = importlib.util.spec_from_file_location("_dfgl07e_review", TOOL)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)
REAL_PLAN, REAL_EXECUTE = review.plan, review.execute


def facts_build(replace=None, extra=None, drop=None):  # a stub FACTS-v1 source: one quote changed, a row added or one dropped
    code = ".replace(%r, %r)" % replace if replace else ""
    if extra:
        code += " + %r" % ("Sample City Police Department,REQ-101,vendor,Added vendor,%s,7,An added quote.\n" % ("b" * 64))
    if drop:
        code = ".replace(%r, '')" % drop
    return DRAFT % code


class DagCase(unittest.TestCase):
    drive = base.FlockReviewTest.drive

    def setUp(self):
        base.FlockReviewTest.setUp(self)
        self.config, self.p = make_review(self.tmp)
        self.facts = self.p["FO"] / "store" / "agencies" / SLUG / "FACTS.csv"
        self.log = self.facts.with_name("FACT-REVIEWS.csv")
        self.state = self.p["out"] / "REVIEW-STATE.json"

    def stages(self):
        return json.loads(self.state.read_text())["stages"]

    def marks(self):
        return {s: r.get("mark") for s, r in self.stages().items() if r.get("mark")}

    def rows(self, path):
        return list(csv.DictReader(io.StringIO(path.read_text())))

    def ids(self):
        return [r["fact_id"] for r in self.rows(self.facts)]

    def statuses(self):
        return [r["status"] for r in self.rows(self.facts)]

    def human(self, to, ids, reviewer="ben"):  # the real review CLI, never the DAG
        done = subprocess.run([sys.executable, "-B", str(FACTS_TOOL), "review", "--store", str(self.p["FO"] / "store"), "--agency", SLUG,
                               "--ids", ",".join(ids), "--set", to, "--reviewer", reviewer], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def rerun(self, code=0, config=None):
        got, out = tool("run", config or self.config)
        self.assertEqual(got, code, out)
        return out

    def changed_quote(self, stub):
        write_stub(self.p["method"], "facts_build", stub)
        (self.p["FO"] / "drafts/a.yaml").write_text("fact-drafts revised\n")  # the model card re-writes the draft ...
        self.assertEqual(mark_all(self.config, {"fact-drafts": "anthropic"}), [0])  # ... and marks it

    def rc_stages(self, out):
        return [line.split()[1] for line in out.splitlines() if "rc=" in line]

    def report_review_part(self):
        c = review.context(self.config)
        return review.digest(review.fingerprint_parts(c, review.IDS.index("report"), {d: "x" for d in review.IDS}, review.load_state(c))["review"])


class ReconcileDagTest(DagCase):
    def test_review_keeps_facts_import_and_every_mark_and_only_the_report_part_moves(self):
        self.assertEqual(self.drive(self.config, self.p)[0], 0)
        marks, outputs, before, ids = self.marks(), self.stages()["facts-import"]["outputs"], self.report_review_part(), self.ids()
        self.assertEqual(list(outputs), ["store/agencies/%s/FACTS.csv" % SLUG])
        self.human("checked", ids[:2])
        self.assertNotEqual(self.report_review_part(), before)  # only the report re-renders
        seen, out = len(calls(self.p)), self.rerun()
        self.assertEqual((self.rc_stages(out), out.count(" cached\n"), calls(self.p)[seen:]), ([], 14, []), out)
        self.assertEqual((self.statuses()[:3], self.marks(), self.stages()["facts-import"]["outputs"]), (["checked", "checked", "unreviewed"], marks, outputs))
        self.assertEqual(len(self.log.read_text().splitlines()), 3)
        self.assertFalse(list(self.p["out"].glob(".review/superseded/facts-import-*")))
        self.human("disputed", ids[:1])  # dispute, then reversal: the whole chain stays
        self.human("checked", ids[:1], "cara")
        out = self.rerun()
        self.assertEqual((self.rc_stages(out), self.statuses()[0], self.marks()), ([], "checked", marks), out)
        self.assertEqual([r["to_status"] for r in self.rows(self.log) if r["fact_id"] == ids[0]], ["checked", "disputed", "checked"])

    def test_changed_quote_resets_that_row_and_invalidates_downstream(self):
        self.assertEqual(self.drive(self.config, self.p)[0], 0)
        ids = self.ids()
        self.human("checked", ids[:3])
        self.rerun()
        self.changed_quote(facts_build(("The ALPR vendor", "The ALPR vendr")))
        out = self.rerun(3)
        self.assertEqual(self.rc_stages(out)[:2], ["facts-build", "facts-import"])
        self.assertIn("stage assess WAITING (upstream changed)", out)
        self.assertEqual(self.statuses()[:3], ["unreviewed", "checked", "checked"])
        old = [r for r in self.rows(self.log) if r["fact_id"] == ids[0]]
        self.assertEqual((len(old), len({r["evidence_fp"] for r in self.rows(self.log)})), (1, 3))  # history kept, unbound
        self.assertEqual(mark_all(self.config, {"assess": "anthropic"}), [0])
        self.assertIn("stage f3-findings WAITING (upstream changed)", self.rerun(3))

    def test_added_and_removed_facts(self):
        self.assertEqual(self.drive(self.config, self.p)[0], 0)
        ids = self.ids()
        self.human("checked", ids)
        self.rerun()
        row = "Sample City Police Department,REQ-101,outside_agencies,12,%s,3,Twelve outside agencies have query access.\n" % ("b" * 64)
        self.changed_quote(facts_build(extra=True))
        self.rerun(3)
        self.assertEqual((self.statuses(), len(self.ids())), (["checked"] * 6 + ["unreviewed"], 7))
        self.changed_quote(facts_build(drop=row))
        self.rerun(3)
        self.assertEqual((len(self.ids()), ids[2] in self.ids(), self.statuses()), (5, False, ["checked"] * 5))
        self.assertIn(ids[2], self.log.read_text())  # removed row's history stays, unbound

    def test_inconsistent_review_log_fails_facts_import_and_leaves_the_active_pair(self):
        self.assertEqual(self.drive(self.config, self.p)[0], 0)
        self.human("checked", self.ids()[:1])
        self.log.write_text("fact_id,from_status\nbroken\n")
        self.changed_quote(facts_build(("The ALPR vendor", "The ALPR vendr")))
        pair = (self.facts.read_bytes(), self.log.read_bytes())
        out = self.rerun(1)
        self.assertEqual((self.rc_stages(out)[-1], (self.facts.read_bytes(), self.log.read_bytes())), ("facts-import", pair), out)
        self.assertIn("REVIEW_STATE_INVALID", (self.p["out"] / ".review/logs/facts-import.log").read_text())

    def test_pending_journal_is_stale_and_run_recovers_it_once(self):
        self.assertEqual(self.drive(self.config, self.p)[0], 0)
        fid, marks = self.ids()[0], self.marks()
        facts = importlib.util.module_from_spec(s := importlib.util.spec_from_file_location("_dfgl07e_facts", FACTS_TOOL))
        s.loader.exec_module(facts)
        pid = os.fork()
        if pid == 0:  # a review that dies at its third os.replace/unlink, after the log and before FACTS
            code, n = 99, [0]
            try:
                for name in ("replace", "unlink"):
                    def hook(*a, _real=getattr(os, name), **kw):
                        n[0] += 1
                        if n[0] == 3:
                            os._exit(70)
                        return _real(*a, **kw)
                    setattr(facts.os, name, hook)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    facts.main(["review", "--store", str(self.p["FO"] / "store"), "--agency", SLUG, "--ids", fid, "--set", "checked", "--reviewer", "ben"])
            except BaseException:
                code = 98
            os._exit(code)
        self.assertEqual(os.waitpid(pid, 0)[1] >> 8, 70)
        self.assertTrue(self.facts.with_name(".FACTS-TXN.json").exists())
        code, out = tool("status", self.config, "--json")
        row = next(r for r in json.loads(out)["stages"] if r["id"] == "facts-import")
        self.assertEqual((row["state"], row["reason"]), ("stale", "review transaction pending"))
        out = self.rerun()
        self.assertEqual((self.rc_stages(out), self.statuses()[0], self.marks(), self.facts.with_name(".FACTS-TXN.json").exists()), (["facts-import"], "checked", marks, False), out)
        self.assertEqual([r["fact_id"] for r in self.rows(self.log)].count(fid), 1)


class LegacyStateTest(DagCase):
    def legacy_plan(self, extra=False):
        def plan(c, sid, state):
            if sid != "facts-import":
                return REAL_PLAN(c, sid, state)
            FO, t = c["FO"], lambda *a: ("run", [sys.executable, "-B", str(FACTS_TOOL), *map(str, a)])
            steps = [("aside", FO / "store")] + [t("import", f, "--store", FO / "store", "--kind", f.name[9:-4], "--added-by", c["by"], "--status", "unreviewed",
                                                  "--date", c["date"]) for f in sorted(FO.glob("FACTS-v1-*.csv"))] + [t("--check", FO / "store")]
            return steps + ([("copy", FO / "FACTS-v1-record.csv", FO / "store" / "EXTRA.csv")] if extra else []), None
        return plan

    @staticmethod
    def legacy_execute(c, i, state):  # d84fabd6 recorded every changed file under the stage root as an output (fhash)
        base_, before = review.root(c, i), review.snap(review.root(c, i), c["out"])
        rc, _, log = REAL_EXECUTE(c, i, state)
        if review.IDS[i] != "facts-import":
            return rc, _, log
        return rc, {str(p.relative_to(base_)): review.fhash(p) for p, v in review.snap(base_, c["out"]).items() if before.get(p) != v}, log

    def legacy_tool(self, *args, extra=False):
        out = io.StringIO()
        with patch.object(review, "plan", self.legacy_plan(extra)), patch.object(review, "execute", self.legacy_execute), contextlib.redirect_stdout(out):
            code = review.main([str(a) for a in args])
        return code, out.getvalue()

    def legacy_done(self, extra=False):
        for stage, provider, key, rel in WAITS:
            self.assertEqual(self.legacy_tool("run", self.config, "--until", "counts", extra=extra)[0], 3)
            if key:
                (self.p[key] / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.p[key] / rel).write_text(stage + " input\n")
            self.assertEqual(self.legacy_tool("mark", self.config, stage, "--provider", provider, "--model", base.MODELS[provider], extra=extra)[0], 0)
        self.assertEqual(self.legacy_tool("run", self.config, "--until", "counts", extra=extra)[0], 0)
        self.human("checked", self.ids()[:2])  # recorded by the d84fabd6 plan with the 07a lock present, then reviewed
        rows = list(csv.reader(io.StringIO(self.log.read_text())))
        self.log.write_text("".join(",".join(r[:6]) + "\n" for r in rows))  # the consistent v1 fixture: the d84fabd6 writer's six columns

    def test_legacy_state_keeps_every_mark_because_the_output_maps_are_equal(self):
        self.legacy_done()
        legacy = self.stages()["facts-import"]
        self.assertEqual(list(legacy["outputs"]), ["store/agencies/%s/FACTS.csv" % SLUG])
        self.assertEqual([p for p in self.p["FO"].rglob("*") if "LOCK" in p.name.upper()], [])
        marks = self.marks()
        out = self.rerun()
        self.assertEqual(self.rc_stages(out), ["facts-import"])
        self.assertNotIn("WAITING", out)
        now = self.stages()["facts-import"]
        self.assertEqual((review.digest(now["outputs"]), self.marks(), self.statuses()[:3]), (review.digest(legacy["outputs"]), marks, ["checked", "checked", "unreviewed"]))
        self.assertEqual(self.log.read_text().splitlines()[0], "fact_id,from_status,to_status,reviewer,reviewed_at,note,evidence_fp")
        self.assertNotEqual(now["fp"], legacy["fp"])

    def test_legacy_map_with_an_extra_entry_is_not_blessed(self):
        self.legacy_done(extra=True)
        self.assertIn("store/EXTRA.csv", self.stages()["facts-import"]["outputs"])
        out = self.rerun(3)
        self.assertEqual(self.rc_stages(out), ["facts-import"])
        self.assertIn("stage assess WAITING (upstream changed)", out)
        self.assertNotIn("store/EXTRA.csv", self.stages()["facts-import"]["outputs"])


if __name__ == "__main__":
    unittest.main()
