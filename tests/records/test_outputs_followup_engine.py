"""FLK-REC-1: the scheduled outputs stage runs the draft-only follow-up engine. Synthetic; nothing is sent."""
import json
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq, followup_clock as fc, followup_engine as fe
from campaign_tool.records import run_orchestrator, run_report, run_stages
from campaign_tool.records.outcomes import received_items, response_items, timeline
from campaign_tool.records.outcomes import register as outcome_register
from tests.records.test_case_builder import DAY, ask
from tests.records.test_mail_drafts import Base as MailBase, DOC
from tests.test_records_run_stages import StageTestCase

RUN = "run-1"
FEE_ID = "e" * 64
PRIVATE = "SYNTHETIC-PRIVATE-MARKER"


def request(agency, rid):
    return {"agency_key": agency, "agency": agency.upper(), "request_id": rid, "closed": False,
            "events": [{"type": "sent", "date": "2026-10-01"}, {"type": "production", "date": "2026-11-02"}]}


def rated(agency, rid):
    return {"agency_key": agency, "request_id": rid,
            "asks": [ask(1, "ALPR search logs", "answered"), ask(2, "Vendor agreement", "missing")]}


def item(code):
    return {"code": code, "severity": "action", "count": 1}


def fee_item():
    return {"item_id": FEE_ID, "original_sha256": "f" * 64, "occurrence_ids": [], "agency_id": "beta-pd",
            "request_id": "B-1", "received_at": DAY + "T12:00:00Z", "kind": "fee",
            "kind_basis": {"page": 1, "quote": "Estimated fee is attached."}, "findings": [],
            "deadlines": [], "review_required": True}


class OutputsEngineTests(StageTestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        MailBase.build_ledger(self)
        with closing(sqlite3.connect(self.portal / "portal.sqlite")) as pdb, pdb:
            pdb.execute("CREATE TABLE portal_items(id TEXT)")
        (self.processing / "runs").mkdir(mode=0o700)
        (self.processing / "runs" / "extract-run-1.json").write_text(json.dumps(
            {"schema": "records-extract-outcomes-v1", "run_id": RUN, "documents": []}))
        self.agencies = [("alpha-pd", "OLD-1"), ("beta-pd", "B-1"), ("gamma-pd", "G-1")]

    def settings(self, alpha="draft", gamma="off"):
        path = self.project / fc.SETTINGS_FILE
        path.write_text(json.dumps({"schema": fc.SETTINGS_SCHEMA, "default": {"autonomy": "off"},
                                    "agencies": {"alpha-pd": {"autonomy": alpha}, "gamma-pd": {"autonomy": gamma}}}))
        path.chmod(0o600)

    def sources(self):
        alpha = {"item_id": received_items.item_id(DOC), "agency_id": "alpha-pd", "request_id": "OLD-1",
                 "received_at": DAY + "T12:00:00Z", "original_sha256": DOC, "occurrence_ids": [],
                 "kind": "response", "kind_basis": {"page": 1, "quote": "Partial production enclosed."},
                 "findings": [], "deadlines": [], "review_required": True}
        return [alpha, fee_item()]

    def outcomes(self):
        target = self.project / "run-outputs" / RUN / "outcomes"
        target.mkdir(parents=True, mode=0o700, exist_ok=True)
        return target

    def write_received(self, project, run_id):
        doc = {"schema": received_items.SCHEMA, "run_id": run_id, "review_required": True, "legal_conclusion": False,
               "law_package_status": "draft", "disclaimer": "d", "comparison_sha256": None,
               "items": self.sources(), "diagnostics": []}
        (self.outcomes() / "received-items.json").write_text(json.dumps(doc))

    def write_ratings(self, project, run_id):
        doc = {"schema": response_items.SCHEMA, "run_id": run_id,
               "requests": [rated(a, r) for a, r in self.agencies]}
        (self.outcomes() / "response-items.json").write_text(json.dumps(doc))

    def write_timeline(self, project, run_id):
        doc = {"schema": timeline.SCHEMA, "run_id": run_id, "as_of": DAY,
               "requests": [request(a, r) for a, r in self.agencies]}
        (self.outcomes() / "timeline.json").write_text(json.dumps(doc))

    def write_register(self, project, run_id):
        doc = {"schema": "records-request-register-v1", "as_of": DAY,
               "requests": [request(a, r) for a, r in self.agencies]}
        (self.project / "run-outputs" / RUN / fc.REGISTER_NAME).write_text(json.dumps(doc))

    def stage_outputs(self, timeline_writer=None):
        with mock.patch.object(received_items, "write_run_received_items", self.write_received), \
                mock.patch.object(response_items, "write_run_response_items", self.write_ratings), \
                mock.patch.object(timeline, "write_run_timeline", timeline_writer or self.write_timeline), \
                mock.patch.object(outcome_register, "write_for_run", self.write_register):
            return self.stage("outputs")

    def rows(self, kind=None):
        return [r for r in aq.load_queue(self.project) if kind is None or r["kind"] == kind]

    def test_partial_production_queues_one_engine_row_and_rerun_adds_none(self):
        self.settings()
        code, parsed, _ = self.stage_outputs()
        self.assertEqual((code, parsed["status"]), (0, "ok"))
        # A clean run's result is unchanged: no needs_g key and no extra field.
        self.assertEqual(parsed, {"stage_result_version": 1, "status": "ok", "error_code": None,
                                  "counts": {"input": 1, "output": 1, "failed": 0, "skipped": 0}})
        engine = self.rows(fe.KIND)
        self.assertEqual([(r["agency_id"], r["request_id"], r["status"]) for r in engine],
                         [("alpha-pd", "OLD-1", "pending")])
        self.assertEqual(engine[0]["evidence"]["asks_for"], [2])
        self.assertIn("Vendor agreement", engine[0]["body"])
        self.assertEqual(engine[0]["evidence"]["pattern"], "partial_production")
        # The fee estimate keeps its received-items draft; the engine adds nothing for it.
        self.assertEqual([r["agency_id"] for r in self.rows("fee-reply")], ["beta-pd"])
        self.assertEqual([r["agency_id"] for r in self.rows(fe.KIND)], ["alpha-pd"])
        before = aq.load_queue(self.project)
        code, parsed, _ = self.stage_outputs()
        self.assertEqual((code, parsed["status"]), (0, "ok"))
        self.assertEqual(aq.load_queue(self.project), before)

    def test_fee_reply_route_is_received_items_not_engine(self):
        self.settings()
        self.stage_outputs()
        routes = {r["agency_key"]: r["handled_by"] for r in fe.followup_route.route_run(self.project, RUN)}
        self.assertEqual(routes["beta-pd"], "received_items")
        self.assertEqual(routes["alpha-pd"], "engine")

    def test_autonomy_off_agency_gets_no_engine_row(self):
        self.settings(alpha="off", gamma="off")
        code, parsed, _ = self.stage_outputs()
        self.assertEqual((code, parsed["status"]), (0, "ok"))
        self.assertEqual(self.rows(fe.KIND), [])
        self.assertEqual(len(self.rows("fee-reply")), 1)

    def test_missing_settings_gets_no_engine_row_and_creates_none(self):
        code, parsed, _ = self.stage_outputs()
        self.assertEqual((code, parsed["status"]), (0, "ok"))
        self.assertEqual(self.rows(fe.KIND), [])
        self.assertFalse((self.project / fc.SETTINGS_FILE).exists())

    def test_engine_exception_warns_content_free_and_keeps_earlier_outputs(self):
        self.settings()
        with mock.patch.object(fe, "run", side_effect=RuntimeError(PRIVATE)):
            code, parsed, raw = self.stage_outputs()
        self.assertEqual((code, parsed["status"], parsed["error_code"]), (0, "warn", "OUTPUTS_FOLLOWUP_ENGINE_UNAVAILABLE"))
        self.assertNotIn(PRIVATE, raw)
        self.assertEqual(parsed["needs_g"], [item("OUTPUTS_FOLLOWUP_ENGINE_UNAVAILABLE")])  # one failure, one item
        self.assertEqual(self.rows(fe.KIND), [])
        self.assertEqual(len(self.rows("fee-reply")), 1)  # received-item drafts stay
        outputs = self.project / "run-outputs"
        self.assertTrue((outputs / "run-1.json").is_file())
        self.assertTrue((outputs / RUN / "outcomes" / "received-items.json").is_file())
        self.assertTrue((outputs / RUN / "agency-factsheets.json").is_file())
        self.assertTrue((outputs / RUN / "outcomes" / "analysis-ready.json").is_file())  # later writer still ran
        self.assertTrue((outputs / RUN / fc.REGISTER_NAME).is_file())

    def test_earlier_warning_wins_and_timeout_propagates(self):
        self.settings()
        with mock.patch.object(fe, "run", side_effect=RuntimeError(PRIVATE)), \
                mock.patch.object(received_items, "write_run_received_items", self.write_received), \
                mock.patch.object(response_items, "write_run_response_items", self.write_ratings), \
                mock.patch.object(timeline, "write_run_timeline", self.write_timeline), \
                mock.patch.object(outcome_register, "write_for_run", side_effect=RuntimeError(PRIVATE)):
            _, parsed, raw = self.stage("outputs")
        self.assertEqual(parsed["error_code"], "OUTPUTS_REGISTER_UNAVAILABLE")
        self.assertNotIn(PRIVATE, raw)
        self.assertEqual([i["code"] for i in parsed["needs_g"]],
                         ["OUTPUTS_REGISTER_UNAVAILABLE", "OUTPUTS_FOLLOWUP_ENGINE_UNAVAILABLE"])
        with mock.patch.object(fe, "run", side_effect=run_stages.StageTimeout()):
            code, parsed, _ = self.stage_outputs()
        self.assertEqual((code, parsed["error_code"]), (1, "STAGE_CONTAINER_TIMEOUT"))
        self.assertNotIn("needs_g", parsed)  # a timeout carries no additive warning items

    def failing_outputs(self):
        """One run where the queue, follow-up clock and timeline sub-steps fail; the private text is a canary."""
        def broken_timeline(project, run_id):
            self.write_timeline(project, run_id)  # later readers still find their input
            raise RuntimeError(PRIVATE)
        with mock.patch.object(aq, "queue_received_items", side_effect=RuntimeError(PRIVATE)), \
                mock.patch.object(fc, "queue_followups", side_effect=RuntimeError(PRIVATE)):
            return self.stage_outputs(timeline_writer=broken_timeline)

    def test_outputs_warning_items_round_trip_in_order(self):
        # Contract: every failed outputs sub-step is one report needs_g item in sub-step order while the stage
        # error_code stays the first warning. Fails if a later failure is dropped, reordered or leaks exception text.
        self.settings()
        code, parsed, raw = self.failing_outputs()
        self.assertEqual((code, parsed["status"], parsed["error_code"]), (0, "warn", "OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE"))
        expected = ["OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE", "OUTPUTS_FOLLOWUP_CLOCK_UNAVAILABLE", "OUTPUTS_TIMELINE_UNAVAILABLE"]
        self.assertEqual(parsed["needs_g"], [item(c) for c in expected])
        self.assertNotIn(PRIVATE, raw)

        def fake_run_stage(argv, timeout, schema, work_dir, env):
            if "outputs" in argv:
                return "warn", dict(parsed["counts"]), parsed["error_code"], parsed, 1.0
            ok = run_stages.result("ok", {})
            return "ok", dict(ok["counts"]), None, ok, 1.0

        stages = [{"name": name, "argv": [sys.executable, "-B", "-m", "campaign_tool.records.run_orchestrator",
                                          "stub-stage", "--stage", name, "--fixture", "/unused"],
                   "timeout_seconds": 60, "on_fail": "stop"} for name in run_orchestrator.WORK_STAGES]
        config = run_orchestrator.validate_target_config(
            {"target_config_version": 1, "target": "TEST", "mode": "test", "stages": stages})
        with tempfile.TemporaryDirectory() as work, \
                mock.patch.object(run_orchestrator, "run_stage", side_effect=fake_run_stage):
            report_path = Path(work) / "report.json"
            run_orchestrator.execute(config, "run_20261019T120000Z_0123abcd", "test", run_orchestrator.utc_now(),
                                     time.time() + 600, report_path, work)
            text = report_path.read_text(encoding="utf-8")
        report = json.loads(text)
        run_report.validate_run_report(report)
        outputs = next(s for s in report["stages"] if s["name"] == "outputs")
        self.assertEqual(outputs["error_code"], "OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE")
        self.assertEqual([(i["stage"], i["code"], i["severity"], i["count"]) for i in report["needs_g"]],
                         [("outputs", c, "action", 1) for c in expected])
        self.assertNotIn(PRIVATE, text)

    def test_single_failure_gives_one_item(self):
        self.settings()
        with mock.patch.object(aq, "queue_received_items", side_effect=RuntimeError(PRIVATE)):
            _, parsed, _ = self.stage_outputs()
        self.assertEqual((parsed["error_code"], parsed["needs_g"]),
                         ("OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE", [item("OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE")]))

    def test_engine_runs_once_with_apply_after_queue_and_its_three_inputs(self):
        self.settings()
        events = []

        def tracked(label, real):
            def wrapper(*args, **kwargs):
                events.append((label, args, kwargs))
                return real(*args, **kwargs)
            return wrapper
        with mock.patch.object(received_items, "write_run_received_items", tracked("received", self.write_received)), \
                mock.patch.object(response_items, "write_run_response_items", tracked("ratings", self.write_ratings)), \
                mock.patch.object(timeline, "write_run_timeline", tracked("timeline", self.write_timeline)), \
                mock.patch.object(outcome_register, "write_for_run", self.write_register), \
                mock.patch.object(aq, "queue_received_items", tracked("queue", aq.queue_received_items)), \
                mock.patch.object(fe, "run", tracked("engine", fe.run)):
            code, parsed, _ = self.stage("outputs")
        self.assertEqual((code, parsed["status"]), (0, "ok"))
        names = [e[0] for e in events]
        self.assertEqual(names.count("engine"), 1)
        self.assertEqual(names[-1], "engine")
        for earlier in ("queue", "ratings", "timeline"):
            self.assertLess(names.index(earlier), names.index("engine"))
        _, args, kwargs = events[-1]
        self.assertEqual((args[1], kwargs), (RUN, {"apply": True}))
