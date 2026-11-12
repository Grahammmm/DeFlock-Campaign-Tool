"""DFGL-07f: records analysis-tick advances each agency review DAG to its next model wait (synthetic roots, stub method scripts)."""
import contextlib
import csv
import datetime
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import analysis_bridge as bridge, cli
from tests.flock import test_review as tr
from tests.flock.test_review_verify import RULES, compare_build
from tests.records import test_analysis_bridge as tb

SLUG = "sample-city-police-department"
REPO = tb.REPO
EXAMPLE = REPO / "docs" / "examples" / "needs-model.example.json"
SCHEMA = REPO / "schemas" / "records-needs-model-v1.json"


class AnalysisTickTests(unittest.TestCase):
    def setUp(self):
        self.b = tb.AnalysisBridgeTests("test_dry_run_writes_nothing_and_the_run_store_is_never_modified")
        self.b.setUp()
        self.addCleanup(self.b.doCleanups)
        self.root, self.method = self.b.root, self.b.method
        tr.write_stub(self.method, "compare_build", compare_build())
        tr.write_stub(self.method, "rules_build", RULES)
        self.verbs, self.seen = [], []
        real = bridge._review

        def counting(tool, verb, config):
            self.verbs.append(verb)
            return real(tool, verb, config)
        patcher = patch.object(bridge, "_review", counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sync_agency(self, run=tb.R1, pages=tb.PAGES, request="rq1", at="2026-11-02T18:00:00Z"):
        profile = json.loads(tb.PROFILE.read_text().replace("@ROOT@", str(self.root)).replace("@METHOD@", str(self.method))
                             .replace("@PY@", sys.executable))
        profile.update(records_agency_id=tb.AGY, not_posted_analysis_only_ids=["99999999"])
        profile["review"]["verify_sample"] = 3
        (self.root / "profiles").mkdir(parents=True, exist_ok=True)
        (self.root / "profiles" / (SLUG + ".json")).write_text(json.dumps(profile))
        self.b.record(run, tb.AGY, request, at, digest=self.b.document(pages, tb.AGY)[0])
        self.assertEqual(self.b.receive()[0], 0)
        self.assertEqual(self.b.sync()[0], 0)

    def lane_on(self):
        (self.root / ".analysis" / "LANE-ON").write_text("")

    def tick(self, *args, process=False):
        """-> (exit code, stdout); process=True runs the registered CLI verb from the repo root."""
        if process:
            done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "analysis-tick", "--root", str(self.root), *args],
                                  cwd=REPO, capture_output=True, text=True, check=False)
            code, out = done.returncode, done.stdout
        else:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = bridge.tick_main(["--root", str(self.root), *args])
            out = buffer.getvalue()
        self.seen.append(out)
        return code, out

    def needs(self):
        path = self.root / ".analysis" / "needs-model.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        doc = json.loads(path.read_text())
        self.assertEqual(doc["schema"], "records-needs-model-v1")
        return doc["entries"]

    def config(self):
        return self.root / "agencies" / SLUG / "review-config.json"

    def out(self):
        return Path(json.loads(self.config().read_text())["review"]["out"])

    def mark(self, stage, provider):
        return tr.tool("mark", self.config(), stage, "--provider", provider, "--model", tr.MODELS.get(provider, "model-1"))[0]

    def expect(self, outcome, process=False):
        code, out = self.tick(process=process)
        self.assertEqual(code, 0, out)
        self.assertIn("%s %s" % (SLUG, outcome), out)
        entries = self.needs()
        return entries[0] if entries else None

    def handoff(self):
        """Plays every model card in order; ticks between marks. Ends DONE."""
        self.sync_agency()
        self.lane_on()
        out, rel = self.out(), "agencies/%s/review/" % SLUG
        entry = self.expect("WAITING fact-drafts", process=True)
        self.assertEqual((entry["role"], entry["sources"], entry["author_family"]), ("author", rel + "inputs/SOURCES.csv", None))
        (out / "facts" / "drafts").mkdir(parents=True, exist_ok=True)
        (out / "facts" / "drafts" / "a.yaml").write_text("fact-drafts input\n")
        self.assertEqual(self.mark("fact-drafts", "anthropic"), 0)
        entry = self.expect("WAITING rules-read")
        self.assertEqual((entry["role"], entry["sources"]), ("author", None))
        for name in ("law.spec.csv", "policy.spec.csv", "law.f2map.csv", "policy.txt"):
            (self.root / "inputs").mkdir(exist_ok=True)
            (self.root / "inputs" / name).write_text("x\n")
        self.assertEqual(self.mark("rules-read", "anthropic"), 0)
        self.assertEqual(self.expect("WAITING assess")["wait_files"], [rel + "compare/ASSESSMENTS.yaml"])
        (out / "compare").mkdir(parents=True, exist_ok=True)
        (out / "compare" / "ASSESSMENTS.yaml").write_text("assess input\n")
        self.assertEqual(self.mark("assess", "anthropic"), 0)
        self.assertEqual(self.expect("WAITING f3-findings")["author_family"], "anthropic")
        (out / "compare" / "F3-FINDINGS.yaml").write_text("f3 input\n")
        self.assertEqual(self.mark("f3-findings", "anthropic"), 0)
        entry = self.expect("WAITING verify")
        self.assertEqual((entry["role"], entry["author_family"], entry["verify_family"]), ("verify", "anthropic", None))
        self.assertEqual(entry["wait_files"], [rel + "compare/VERIFY.csv", rel + "compare/VERIFY.md"])
        plan = json.loads((out / "compare" / "VERIFY-PLAN.json").read_text())
        (out / "compare" / "VERIFY.csv").write_text("row_id,result,reason,page_cite\n" + "".join(
            "%s,agree,checked,p.2\n" % i for i in plan["priority_ids"] + plan["sample_ids"]))
        (out / "compare" / "VERIFY.md").write_text("checked\n")
        self.assertEqual(self.mark("verify", "anthropic"), 3)  # same family as the author marks: refused
        self.expect("UNCHANGED WAITING verify")  # nothing a tick can see changed, so it reports the same wait
        self.assertEqual(self.mark("verify", "openai"), 0)
        entry = self.expect("WAITING f4")
        self.assertEqual((entry["role"], entry["verify_family"], entry["author_family"]), ("verify", "openai", "anthropic"))
        run_date = json.loads(self.config().read_text())["review"]["run_date"]
        run = out / "compare" / "store" / "agencies" / SLUG / "runs" / run_date
        self.assertEqual(entry["wait_files"], ["%scompare/store/agencies/%s/runs/%s/VERIFY.csv" % (rel, SLUG, run_date)])
        (run / "VERIFY.csv").write_bytes((tr.FIX / "verify-sample.csv").read_bytes())
        self.assertEqual(self.mark("f4", "openai"), 0)
        self.assertIsNone(self.expect("DONE", process=True))
        return out

    def test_command_is_registered_and_lane_off_writes_nothing(self):
        self.assertIn("analysis-tick", cli.COMMANDS)
        self.sync_agency()
        before = tb.digest_tree(self.root)
        code, out = self.tick(process=True)
        self.assertEqual((code, out.strip()), (0, "ANALYSIS_LANE_OFF"))
        self.assertEqual(tb.digest_tree(self.root), before)

    def test_first_tick_waits_at_fact_drafts_and_a_repeat_is_unchanged_without_run(self):
        self.sync_agency()
        self.lane_on()
        code, out = self.tick(process=True)
        self.assertEqual(code, 0, out)
        self.assertIn("%s WAITING fact-drafts" % SLUG, out)
        self.assertIn("analysis: 0 advanced, 1 waiting for a model, 0 done, 0 errors", out)
        (entry,) = self.needs()
        self.assertEqual(entry["agency"], SLUG)
        self.assertEqual((entry["records_agency_id"], entry["role"], entry["stage"], entry["verify_family"]), (tb.AGY, "author", "fact-drafts", None))
        self.assertEqual((entry["config"], entry["wait_files"]), ("agencies/%s/review-config.json" % SLUG, ["agencies/%s/review/facts/drafts/*.yaml" % SLUG]))
        self.assertEqual(sorted(entry), sorted(json.loads(EXAMPLE.read_text())["entries"][0]))
        self.assertEqual(self.verbs, [])  # the CLI ran in a subprocess; now count in process
        code, out = self.tick()
        self.assertEqual((code, self.verbs), (0, ["status"]))
        self.assertIn("%s UNCHANGED WAITING fact-drafts" % SLUG, out)
        self.assertEqual([e["outcome"] for e in self.ledger()], ["WAITING fact-drafts", "UNCHANGED"])
        self.assertEqual(len(self.needs()), 1)

    def ledger(self):
        lines = (self.root / ".analysis" / "analysis-ledger.jsonl").read_text().splitlines()
        return [e for e in map(json.loads, lines) if e["kind"] == "tick"]

    def test_full_synthetic_handoff_to_done_through_the_cli(self):
        self.handoff()
        self.assertEqual(self.needs(), [])
        self.assertEqual(self.mark("verify", "anthropic"), 3)  # a finished review still refuses a same-family verify
        blob = "\n".join(self.seen) + (self.root / ".analysis" / "analysis-ledger.jsonl").read_text() + (self.root / ".analysis" / "needs-model.json").read_text()
        for page in tb.PAGES:
            self.assertNotIn(page, blob)

    def test_new_text_after_done_reopens_fact_drafts_and_keeps_the_rules_mark(self):
        out = self.handoff()
        state = out / "REVIEW-STATE.json"
        mark = json.loads(state.read_text())["stages"]["rules-read"]["mark"]
        self.sync_agency(tb.R2, ["Other first page", "Other second page"], "rq2", "2026-11-05T09:00:00Z")
        self.assertEqual(self.expect("WAITING fact-drafts")["stage"], "fact-drafts")
        self.assertEqual(json.loads(state.read_text())["stages"]["rules-read"]["mark"], mark)
        rows = {r["id"]: r["state"] for r in json.loads(tr.tool("status", self.config(), "--json")[1])["stages"]}
        self.assertEqual((rows["rules-read"], rows["fact-drafts"]), ("done", "waiting"))

    def test_a_held_lock_is_error_locked_with_exit_zero_and_is_retried(self):
        self.sync_agency()
        self.lane_on()
        lock = self.out() / ".review" / "LOCK"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("1\n")
        code, out = self.tick()
        self.assertEqual((code, self.verbs), (0, ["status"]))
        self.assertIn("%s ERROR LOCKED" % SLUG, out)
        lock.unlink()
        self.assertIn("%s WAITING fact-drafts" % SLUG, self.tick()[1])

    def test_review_state_is_part_of_the_unchanged_key_and_review_survives_the_ticks(self):
        out = self.handoff()
        store = out / "facts" / "store"
        facts = store / "agencies" / SLUG / "FACTS.csv"
        with facts.open(newline="") as handle:
            fact_id = next(csv.DictReader(handle))["fact_id"]
        done = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "flock_facts.py"), "review", "--store", str(store), "--agency", SLUG,
                               "--ids", fact_id, "--set", "checked", "--reviewer", "second-reviewer", "--date", datetime.date.today().isoformat()],
                              capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stdout)
        log = facts.with_name("FACT-REVIEWS.csv")
        reviewed = log.read_bytes()
        self.verbs.clear()
        code, text = self.tick()
        self.assertEqual((code, self.verbs), (0, ["status", "run", "status"]), text)
        self.assertIn("%s DONE" % SLUG, text)
        self.verbs.clear()
        code, text = self.tick()
        self.assertEqual((code, self.verbs), (0, ["status"]), text)
        self.assertIn("%s UNCHANGED DONE" % SLUG, text)
        with facts.open(newline="") as handle:
            self.assertEqual({r["fact_id"]: r["status"] for r in csv.DictReader(handle)}[fact_id], "checked")
        self.assertEqual(log.read_bytes(), reviewed)

    def test_errors_cap_dry_run_and_missing_tool(self):
        self.sync_agency()
        self.lane_on()
        (self.root / "agencies" / "aaa-broken").mkdir()
        (self.root / "agencies" / "aaa-broken" / "review-config.json").write_text("{not json")
        before = tb.digest_tree(self.root)
        code, out = self.tick("--dry-run", "--max-agencies", "2")
        self.assertEqual((code, tb.digest_tree(self.root)), (1, before))
        self.assertIn("aaa-broken ERROR CONFIG", out)
        self.assertIn("%s WOULD_RUN" % SLUG, out)
        code, out = self.tick("--max-agencies", "1")
        self.assertNotIn(SLUG, out)
        self.assertEqual((code, self.verbs.count("run")), (1, 0))
        code, out = self.tick(process=True)
        self.assertEqual(code, 1, out)
        self.assertIn("aaa-broken ERROR CONFIG", out)
        self.assertIn("%s WAITING fact-drafts" % SLUG, out)
        self.assertIn("analysis: 0 advanced, 1 waiting for a model, 0 done, 1 errors", out)
        with patch.object(bridge.ac, "REPO", self.b.tmp / "no-engine"):
            code, out = self.tick()
        self.assertEqual(code, 1)
        self.assertIn("%s ERROR REVIEW_TOOL_MISSING" % SLUG, out)

    def test_a_failed_stage_is_error_failed_and_retried_every_tick(self):
        tr.write_stub(self.method, "inventory", "sys.exit(1)")
        self.sync_agency()
        self.lane_on()
        for _ in range(2):
            self.verbs.clear()
            code, out = self.tick()
            self.assertEqual((code, self.verbs), (1, ["status", "run", "status"]), out)
            self.assertIn("%s ERROR FAILED inventory" % SLUG, out)
        self.assertEqual(self.needs(), [])

    def test_example_is_the_frozen_canonical_bytes_and_matches_the_schema_keys(self):
        text = EXAMPLE.read_text()
        doc = json.loads(text)
        self.assertEqual(text, json.dumps(doc, indent=1, sort_keys=True) + "\n")
        self.assertEqual([(e["agency"], e["stage"], e["role"]) for e in doc["entries"]],
                         [("fictional-valley-pd", "fact-drafts", "author"), ("fictional-hills-so", "verify", "verify")])
        self.assertEqual([e["input_fp"] for e in doc["entries"]], ["a" * 64, "b" * 64])
        required = json.loads(SCHEMA.read_text())["properties"]["entries"]["items"]["required"]
        self.assertTrue(all(sorted(e) == sorted(required) for e in doc["entries"]))


if __name__ == "__main__":
    unittest.main()
