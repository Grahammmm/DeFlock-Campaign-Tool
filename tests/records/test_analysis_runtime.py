"""DFGL-20s: the rendered run-analysis.sh cycle (stub docker, real host verbs, synthetic roots)."""
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.flock import test_review as tr
from tests.flock.test_review_verify import RULES, compare_build
from tests.records import test_analysis_bridge as tb, test_analysis_packets as tp

REPO = tb.REPO
SAMPLE = "sample-city-police-department"
RUNTIME = REPO / "deploy" / "records-runtime"
STUB = """#!/bin/sh
# stub docker: logs argv, runs the container-side argv locally
printf '%s\\n' "$*" >>"{log}"
case "$1" in
  info) exit 0;;
  inspect) printf 'true\\n'; exit 0;;
  exec) shift; [ "$1" != -i ] || shift; shift 3; exec "$@";;
esac
exit 1
"""


class AnalysisCycleTests(unittest.TestCase):
    def setUp(self):
        self.b = tb.AnalysisBridgeTests("test_dry_run_writes_nothing_and_the_run_store_is_never_modified")
        self.b.setUp()
        self.addCleanup(self.b.doCleanups)
        self.root, self.tmp, self.project = self.b.root, self.b.tmp, self.b.project
        tr.write_stub(self.b.method, "compare_build", compare_build())
        tr.write_stub(self.b.method, "rules_build", RULES)
        self.root.mkdir(mode=0o700)
        self.root.chmod(0o700)
        (self.root / ".analysis").mkdir(mode=0o700)
        self.log = self.tmp / "docker.log"
        self.runtime = self.tmp / "runtime"
        self.runtime.mkdir()
        self.render()

    def render(self, project=None):
        docker = self.tmp / "docker"
        docker.write_text(STUB.format(log=self.log))
        docker.chmod(0o700)
        spec = importlib.util.spec_from_file_location("records_runtime_render", RUNTIME / "render.py")
        render = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(render)
        values = {
            "RELEASE_COMMIT": "b" * 40, "ENGINE_DIR": str(REPO), "DOCKER": str(docker), "TIMEOUT": "/usr/bin/timeout",
            "FLOCK": "/usr/bin/flock", "CONTAINER": "records", "EXPECTED_IMAGE": "sha256:" + "0" * 64,
            "RUN_UID": str(os.getuid()), "RUNTIME_DIR": str(self.runtime), "HOST_PYTHON": sys.executable,
            "NOTIFY_PY": "/srv/engine/notify.py", "NOTIFY_ENV_FILE": "/srv/notify.env", "NOTIFY_LABEL": "Records",
            "DOCKER_WAIT_ATTEMPTS": "12", "DOCKER_WAIT_DELAY": "5", "DOCKER_WAIT_MAX_DELAY": "60",
            "DOCKER_CMD_TIMEOUT": "20", "DOCKER_WAIT_DEADLINE": "600", "NOTIFY_TIMEOUT": "60",
            "RUN_BUDGET_SECONDS": "2640", "DEFAULT_TARGET": "TEST", "SCHEDULED_TARGET": "TEST",
            "RUN_RECORDS_SH": str(self.runtime / "run-records.sh"), "RECORDS_DROP_DIR": "/srv/records-drop",
            "STUB_FIXTURE": "/srv/fixture.json", "MIN_FREE_BYTES": "2147483648", "RECORDS_ROOT": "/srv/records-root",
            "ANALYSIS_ROOT": str(self.root), "CONTAINER_USER": "records", "CONTAINER_ENGINE_DIR": str(REPO),
            "CONTAINER_PYTHON": sys.executable, "CONTAINER_PROJECT_DIR": str(project or self.project)}
        self.renders = getattr(self, "renders", 0) + 1
        out = self.tmp / ("rendered%d" % self.renders)
        render.render(values, [("TEST", str(RUNTIME / "targets" / "stub.json.in"))], out)
        self.script = out / "run-analysis.sh"
        self.assertNotIn("run-records.lock", self.script.read_text())

    def lane_on(self):
        (self.root / ".analysis" / "LANE-ON").write_text("")

    def cycle(self):
        done = subprocess.run([str(self.script)], cwd=REPO, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                              capture_output=True, text=True, timeout=600, check=False)
        for text in (done.stdout, done.stderr):  # metadata only: no record text
            self.assertNotIn(tb.PAGES[0], text)
            self.assertNotIn(tb.PAGES[1], text)
        return done.returncode, done.stdout

    def calls(self, verb=""):
        return [x for x in (self.log.read_text().splitlines() if self.log.exists() else []) if verb in x]

    def profile(self, agency, sample=False):
        """The two-agency profile of the bridge tests; sample=True the single sample-city profile the review stubs drive to a mark."""
        if not sample:
            return self.b.profile(agency, verify_sample=3)
        profile = json.loads(tb.PROFILE.read_text().replace("@ROOT@", str(self.root)).replace("@METHOD@", str(self.b.method))
                             .replace("@PY@", sys.executable))
        profile.update(records_agency_id=agency, not_posted_analysis_only_ids=["99999999"])
        profile["review"]["verify_sample"] = 3
        (self.root / "profiles").mkdir(parents=True, exist_ok=True)
        (self.root / "profiles" / (SAMPLE + ".json")).write_text(json.dumps(profile))

    def cycle_log(self):
        return (self.root / ".analysis" / "cycle.log").read_text()

    def needs(self):
        doc = json.loads((self.root / ".analysis" / "needs-model.json").read_text())
        return doc["entries"]

    def ticks(self):
        return self.b.ledger("tick")

    def mark_fact_drafts(self):
        config = self.root / "agencies" / SAMPLE / "review-config.json"
        out = Path(json.loads(config.read_text())["review"]["out"])
        (out / "facts" / "drafts").mkdir(parents=True, exist_ok=True)
        (out / "facts" / "drafts" / "a.yaml").write_text("fact-drafts input\n")
        self.assertEqual(tr.tool("mark", config, "fact-drafts", "--provider", "anthropic", "--model", "model-1")[0], 0)

    def test_lane_off_is_a_no_op_without_docker_and_the_records_lock_is_never_made(self):
        self.b.record(tb.R1, tb.AGY)
        self.profile(tb.AGY)
        before = tb.digest_tree(self.root)
        code, out = self.cycle()
        self.assertEqual((code, out.strip()), (0, "ANALYSIS_LANE_OFF"))
        self.assertEqual((self.calls(), tb.digest_tree(self.root)), ([], before))
        self.assertEqual(list(self.tmp.rglob("run-records.lock")), [])

    def test_cycle_exports_syncs_ticks_then_resumes_after_a_mark_and_a_second_cycle_changes_nothing(self):
        self.b.record(tb.R1, tb.AGY)
        self.profile(tb.AGY, sample=True)
        self.lane_on()
        self.assertEqual(self.cycle()[0], 0)
        self.assertEqual([e["stage"] for e in self.needs()], ["fact-drafts"])
        self.assertEqual(len(self.b.ledger("sync")), 1)
        self.assertEqual(len(list((self.root / ".analysis" / "incoming-done").glob("*.tar"))), 1)
        lines = self.b.ledger("sync")
        code, out = self.cycle()  # no new run: nothing is exported again, nothing new is synced
        self.assertEqual(code, 0, out)
        self.assertIn("--after %s" % tb.R1, self.calls("analysis-export")[-1])
        self.assertEqual(self.b.ledger("sync"), lines)
        self.assertEqual(self.ticks()[-1]["outcome"], "UNCHANGED")
        self.mark_fact_drafts()  # as a model card would; no other trigger
        self.assertEqual(self.cycle()[0], 0)
        self.assertIn("WAITING rules-read", self.cycle_log())
        self.assertEqual([e["stage"] for e in self.needs()], ["rules-read"])
        self.assertEqual(list(self.tmp.rglob("run-records.lock")), [])

    def test_a_failed_earlier_run_is_synced_after_repair_and_never_exported_twice(self):
        self.b.record(tb.R1, tb.AGX, "rqx")
        self.b.record(tb.R2, tb.AGY, "rqy")
        self.profile(tb.AGY)
        self.lane_on()
        code, out = self.cycle()  # A/X has no profile
        self.assertEqual(code, 21, out)
        self.assertEqual((self.b.outcomes(tb.R1, tb.AGX), self.b.outcomes(tb.R2, tb.AGY)), (["PROFILE_MISSING"], ["SYNCED"]))
        self.assertEqual({e["agency"] for e in self.needs()}, {tb.SLUG[tb.AGY]})
        self.assertEqual(self.cycle()[0], 21)
        self.assertIn("--after %s" % tb.R2, self.calls("analysis-export")[-1])
        self.assertEqual(self.b.sync("--pending")[1].split(), [tb.R1, tb.AGX, "PROFILE_MISSING"])
        self.profile(tb.AGX)
        code, out = self.cycle()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.b.outcomes(tb.R1, tb.AGX)[-1], self.b.outcomes(tb.R2, tb.AGY)), ("SYNCED", ["SYNCED"]))
        self.assertEqual({e["agency"] for e in self.needs()}, {tb.SLUG[tb.AGX], tb.SLUG[tb.AGY]})
        self.assertEqual(len(self.b.csv_rows(self.b.inputs(tb.AGY) / "SOURCES.csv")), 1)
        self.assertEqual(list(self.tmp.rglob("run-records.lock")), [])

    def test_a_staged_packet_is_imported_as_pending_and_moved_to_sent(self):
        packets = tp.AnalysisPacketImportTests("test_command_is_registered")
        packets.setUp()
        self.addCleanup(packets.doCleanups)
        self.render(project=packets.project)
        outbox = self.root / ".analysis" / "packet-outbox"
        outbox.mkdir(mode=0o700)
        (outbox / packets.name).write_bytes(packets.data)
        self.lane_on()
        code, out = self.cycle()
        self.assertIn("import=0", out)
        self.assertEqual(code, 0, out)
        self.assertEqual((list(outbox.glob("*.json")), [p.name for p in (outbox / "sent").iterdir()]), ([], [packets.name]))
        self.assertIn("proposed", self.cycle_log())
        self.assertEqual(len(packets.journal.read_text().splitlines()), 1)

    def test_a_refused_packet_stays_in_the_outbox_and_the_cycle_reports_23(self):
        packets = tp.AnalysisPacketImportTests("test_command_is_registered")
        packets.setUp()
        self.addCleanup(packets.doCleanups)
        self.render(project=packets.project)
        outbox = self.root / ".analysis" / "packet-outbox"
        outbox.mkdir(mode=0o700)
        (outbox / packets.name).write_bytes(packets.data.replace(b'"slug"', b'"slugx"'))
        self.lane_on()
        self.assertEqual(self.cycle()[0], 23)
        self.assertEqual([p.name for p in outbox.glob("*.json")], [packets.name])


if __name__ == "__main__":
    unittest.main()
