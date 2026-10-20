"""Fault-injection acceptance for the Flock end-to-end harness (T12).

Each test drives the synthetic run-records orchestration through one named
fault, asserts the run-report v1 contents and the synthetic ledger counts, and
the last test proves three consecutive clean runs stay consistent (idempotent
replay). No Docker, Telegram, portal, mail, provider, model or external service
is contacted; OCR runs through a deterministic fake backend.
"""

import os
from pathlib import Path
import tempfile
import unittest

from campaign_tool.records import run_report

from tests.records.flock_e2e_harness import (
    Harness,
    StageOutcome,
    build_run_report,
    corrupt_pdf,
    docker_down,
    duplicate_receipt,
    portal_404,
    telegram_down,
    EXIT_COMPLETED,
    EXIT_COMPLETED_WITH_GAPS,
    EXIT_DOCKER_UNAVAILABLE,
    STAGES,
)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.tmp = tempfile.TemporaryDirectory(prefix="flock-e2e-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        os.chmod(self.base, 0o700)

    def harness(self):
        return Harness(self.base)

    def report_of(self, outcome):
        assert outcome.report is not None, "run must produce a report"
        return outcome.report

    def stage(self, report, name):
        return next(s for s in report["stages"] if s["name"] == name)

    def assert_report_valid_and_ordered(self, report):
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["report_kind"], "flock-records-run-report")
        self.assertEqual([s["name"] for s in report["stages"]], list(STAGES))

    # ------------------------------------------------------------------ #
    # clean run
    # ------------------------------------------------------------------ #
    def test_clean_run_produces_schema_valid_report(self):
        outcome = self.harness().run()
        self.assertEqual(outcome.exit_code, EXIT_COMPLETED)
        self.assertEqual(outcome.outcome, "completed")
        report = self.report_of(outcome)
        self.assert_report_valid_and_ordered(report)
        self.assertEqual(report["overall_status"], "ok")
        self.assertFalse(report["notify_failed"])
        self.assertEqual(report["needs_g"], [])
        for stage in report["stages"]:
            self.assertEqual(stage["status"], "ok")
            self.assertIsNone(stage["error_code"])
        self.assertEqual(report["new_data"]["documents"], 3)
        self.assertEqual(report["new_data"]["pages"], 6)
        self.assertEqual(report["new_data"]["extracted_units"], 6)
        self.assertEqual(report["new_data"]["portal_releases"], 3)
        self.assertEqual(report["new_data"]["request_status_changes"], 3)

    def test_clean_run_report_matches_ledger_counts(self):
        harness = self.harness()
        outcome = harness.run()
        report = self.report_of(outcome)
        self.assertEqual(outcome.ledger.count("runs"), 1)
        self.assertEqual(outcome.ledger.count("documents"), 3)
        self.assertEqual(outcome.ledger.count("adopted"), 3)
        self.assertEqual(outcome.ledger.count("releases"), 3)
        for stage in report["stages"]:
            counts = outcome.ledger.stage_counts(report["run_id"])[stage["name"]]
            self.assertEqual(counts, stage["counts"], stage["name"])
        self.assertEqual(outcome.ledger.count("stage_counts"), len(STAGES))

    # ------------------------------------------------------------------ #
    # docker down
    # ------------------------------------------------------------------ #
    def test_docker_down_fails_cleanly_without_stage_work(self):
        harness = self.harness()
        outcome = harness.run([docker_down()])
        self.assertEqual(outcome.exit_code, EXIT_DOCKER_UNAVAILABLE)
        self.assertEqual(outcome.outcome, "docker_unavailable")
        self.assertIsNone(outcome.report)
        self.assertEqual(outcome.attempts, 3)  # bounded, not infinite
        self.assertEqual(outcome.ledger.count("runs"), 0)
        self.assertEqual(outcome.ledger.count("stage_counts"), 0)
        self.assertEqual(outcome.ledger.count("documents"), 0)
        self.assertEqual(outcome.ledger.count("adopted"), 0)
        self.assertEqual(outcome.ledger.count("releases"), 0)

    # ------------------------------------------------------------------ #
    # telegram down
    # ------------------------------------------------------------------ #
    def test_telegram_down_marks_notify_failed_but_run_completes(self):
        outcome = self.harness().run([telegram_down()])
        self.assertEqual(outcome.exit_code, EXIT_COMPLETED)  # notify is non-fatal
        report = self.report_of(outcome)
        self.assertTrue(report["notify_failed"])
        self.assertEqual(report["overall_status"], "fail")  # roll-up from notify stage
        notify = self.stage(report, "notify")
        self.assertEqual(notify["status"], "fail")
        self.assertEqual(notify["error_code"], "NOTIFY_SEND_FAILED")
        for name in STAGES[:-1]:
            self.assertEqual(self.stage(report, name)["status"], "ok", name)
        self.assertEqual(outcome.ledger.count("runs"), 1)
        self.assertEqual(outcome.ledger.con.execute(
            "SELECT notify_failed FROM runs WHERE run_id=?", (report["run_id"],)).fetchone()[0], 1)

    # ------------------------------------------------------------------ #
    # portal 404
    # ------------------------------------------------------------------ #
    def test_portal_404_marks_portal_warn_and_continues(self):
        outcome = self.harness().run([portal_404()])
        self.assertEqual(outcome.exit_code, EXIT_COMPLETED_WITH_GAPS)
        report = self.report_of(outcome)
        self.assertEqual(report["overall_status"], "warn")
        portal = self.stage(report, "portal")
        self.assertEqual(portal["status"], "warn")
        self.assertEqual(portal["error_code"], "PORTAL_HTTP_404")
        self.assertEqual(portal["counts"], {"input": 3, "output": 2, "failed": 1, "skipped": 0})
        self.assertEqual(report["new_data"]["portal_releases"], 2)
        self.assertEqual(self.stage(report, "extract")["status"], "ok")
        self.assertEqual(self.stage(report, "notify")["status"], "ok")
        self.assertFalse(report["notify_failed"])

    # ------------------------------------------------------------------ #
    # corrupt PDF
    # ------------------------------------------------------------------ #
    def test_corrupt_pdf_marks_extract_warn_and_continues(self):
        outcome = self.harness().run([corrupt_pdf()])
        self.assertEqual(outcome.exit_code, EXIT_COMPLETED_WITH_GAPS)
        report = self.report_of(outcome)
        self.assertEqual(report["overall_status"], "warn")
        extract = self.stage(report, "extract")
        self.assertEqual(extract["status"], "warn")
        self.assertEqual(extract["error_code"], "EXTRACT_CORRUPT_PDF")
        self.assertEqual(extract["counts"], {"input": 3, "output": 2, "failed": 1, "skipped": 0})
        self.assertEqual(self.stage(report, "digest_gates")["status"], "ok")
        self.assertEqual(self.stage(report, "notify")["status"], "ok")
        self.assertEqual(report["new_data"]["pages"], 4)
        self.assertEqual(report["new_data"]["extracted_units"], 4)
        self.assertEqual(report["review_required_count"], 2)

    # ------------------------------------------------------------------ #
    # Phase 1: documents needing OCR are queued, never dropped
    # ------------------------------------------------------------------ #
    def test_awaiting_ocr_document_is_queued_and_counted_not_dropped(self):
        harness = Harness(self.base, scenario={"needs_ocr_documents": ["doc-1"]})
        outcome = harness.run()
        self.assertEqual(outcome.exit_code, EXIT_COMPLETED)  # deferral is not a failure
        report = self.report_of(outcome)
        self.assertEqual(report["overall_status"], "ok")
        extract = self.stage(report, "extract")
        self.assertEqual(extract["status"], "ok")
        self.assertEqual(extract["error_code"], None)
        # The OCR-needing document is queued (skipped), not failed, not dropped.
        self.assertEqual(extract["counts"], {"input": 3, "output": 2, "failed": 0, "skipped": 1})
        # It is still counted in the report and tracked in the ledger.
        self.assertEqual(report["new_data"]["documents"], 3)
        self.assertEqual(outcome.ledger.count("documents"), 3)
        # Deferral is durable: the document is queued awaiting_ocr, not done.
        self.assertEqual(outcome.ledger.document_state("doc-1"), "awaiting_ocr")
        self.assertEqual(outcome.ledger.document_state("doc-2"), "done")
        self.assertEqual(outcome.ledger.document_state("doc-3"), "done")
        self.assertEqual(outcome.ledger.document_state_counts().get("awaiting_ocr"), 1)

    # ------------------------------------------------------------------ #
    # durable deferred/failed work survives a close/reopen (T12f)
    # ------------------------------------------------------------------ #
    def test_reopen_replay_deferred_document_stays_visible(self):
        scenario = {"needs_ocr_documents": ["doc-1"]}
        h1 = Harness(self.base, scenario=scenario)
        r1 = self.report_of(h1.run())
        self.assertEqual(self.stage(r1, "extract")["counts"]["skipped"], 1)
        self.assertEqual(h1.ledger.document_state("doc-1"), "awaiting_ocr")
        h1.ledger.close()  # simulate close/reopen of the ledger

        h2 = Harness(self.base, scenario=scenario)
        r2 = self.report_of(h2.run())
        extract = self.stage(r2, "extract")
        # doc-1 is re-selected (visible), still deferred, never dropped.
        self.assertEqual(extract["counts"], {"input": 1, "output": 0, "failed": 0, "skipped": 1})
        self.assertEqual(extract["status"], "ok")
        self.assertEqual(h2.ledger.document_state("doc-1"), "awaiting_ocr")
        self.assertEqual(h2.ledger.document_state("doc-2"), "done")
        self.assertEqual(h2.ledger.document_state("doc-3"), "done")
        self.assertEqual(h2.ledger.document_state_counts().get("awaiting_ocr"), 1)

    def test_deferred_document_recovers_when_backend_healthy(self):
        h1 = Harness(self.base, scenario={"needs_ocr_documents": ["doc-1"]})
        r1 = self.report_of(h1.run())
        self.assertEqual(self.stage(r1, "extract")["counts"]["skipped"], 1)
        h1.ledger.close()

        # OCR becomes available: reopen with the default healthy backend.
        h2 = Harness(self.base)
        r2 = self.report_of(h2.run())
        extract = self.stage(r2, "extract")
        # doc-1 is recovered and processed exactly once.
        self.assertEqual(extract["counts"], {"input": 1, "output": 1, "failed": 0, "skipped": 0})
        self.assertEqual(h2.ledger.document_state("doc-1"), "done")
        self.assertEqual(r2["new_data"]["pages"], 2)
        self.assertEqual(r2["new_data"]["extracted_units"], 2)
        self.assertEqual(r2["review_required_count"], 1)
        # No double counting: each document is extracted exactly once across runs.
        self.assertEqual(h2.ledger.document_state_counts().get("done"), 3)
        self.assertEqual(h2.ledger.count("documents"), 3)

    def test_corrupt_document_persists_across_reopen(self):
        h1 = Harness(self.base)
        r1 = self.report_of(h1.run([corrupt_pdf()]))
        self.assertEqual(self.stage(r1, "extract")["counts"]["failed"], 1)
        self.assertEqual(h1.ledger.document_state("doc-2"), "failed")
        h1.ledger.close()

        h2 = Harness(self.base)
        r2 = self.report_of(h2.run([corrupt_pdf()]))
        extract = self.stage(r2, "extract")
        # doc-2 is re-selected (visible) and still corrupt; never dropped.
        self.assertEqual(extract["counts"], {"input": 1, "output": 0, "failed": 1, "skipped": 0})
        self.assertEqual(extract["status"], "warn")
        self.assertEqual(r2["overall_status"], "warn")
        self.assertEqual(h2.ledger.document_state("doc-2"), "failed")
        self.assertEqual(h2.ledger.document_state_counts().get("failed"), 1)

    def test_corrupt_document_recovers_when_backend_healthy(self):
        h1 = Harness(self.base)
        r1 = self.report_of(h1.run([corrupt_pdf()]))
        self.assertEqual(self.stage(r1, "extract")["counts"]["failed"], 1)
        h1.ledger.close()

        h2 = Harness(self.base)  # healthy backend
        r2 = self.report_of(h2.run())
        extract = self.stage(r2, "extract")
        # doc-2 is recovered and processed exactly once.
        self.assertEqual(extract["counts"], {"input": 1, "output": 1, "failed": 0, "skipped": 0})
        self.assertEqual(h2.ledger.document_state("doc-2"), "done")
        self.assertEqual(r2["new_data"]["pages"], 2)
        self.assertEqual(r2["new_data"]["extracted_units"], 2)
        self.assertEqual(r2["review_required_count"], 1)
        self.assertEqual(h2.ledger.document_state_counts().get("done"), 3)

    # ------------------------------------------------------------------ #
    # notify receives a non-null, schema-valid pre-delivery payload (T12f)
    # ------------------------------------------------------------------ #
    def test_notify_receives_schema_valid_report_payload(self):
        captured = []

        def spy(report):
            captured.append(report)
            return True

        outcome = self.harness().run(notify=spy)
        self.assertEqual(outcome.exit_code, EXIT_COMPLETED)
        self.assertEqual(len(captured), 1)
        payload = captured[0]
        self.assertIsNotNone(payload)
        self.assertIsInstance(payload, dict)
        # The payload is a sanitized, schema-valid pre-delivery report.
        self.assertIs(run_report.validate_run_report(payload), payload)
        self.assertEqual(payload["run_id"], self.report_of(outcome)["run_id"])
        self.assertFalse(payload["notify_failed"])  # pre-delivery: not yet known

    def test_notify_receives_payload_on_delivery_failure(self):
        captured = []

        def spy(report):
            captured.append(report)
            return False

        outcome = self.harness().run(notify=spy)
        self.assertEqual(outcome.exit_code, EXIT_COMPLETED)  # notify is non-fatal
        self.assertEqual(len(captured), 1)
        self.assertIsNotNone(captured[0])
        self.assertIs(run_report.validate_run_report(captured[0]), captured[0])
        report = self.report_of(outcome)
        self.assertTrue(report["notify_failed"])
        self.assertEqual(self.stage(report, "notify")["error_code"], "NOTIFY_SEND_FAILED")


    # ------------------------------------------------------------------ #
    # duplicate receipt
    # ------------------------------------------------------------------ #
    def test_duplicate_receipt_is_deduplicated(self):
        harness = self.harness()
        outcome = harness.run([duplicate_receipt()])
        self.assertEqual(outcome.exit_code, EXIT_COMPLETED)
        report = self.report_of(outcome)
        adoption = self.stage(report, "adoption")
        self.assertEqual(adoption["counts"], {"input": 4, "output": 3, "failed": 0, "skipped": 1})
        self.assertEqual(report["new_data"]["request_status_changes"], 3)
        self.assertEqual(outcome.ledger.adopted_identities(), {"recv-1", "recv-2", "recv-3"})

    def test_duplicate_receipt_reapply_adopts_nothing_new(self):
        harness = self.harness()
        first = self.report_of(harness.run([duplicate_receipt()]))
        self.assertEqual(first["new_data"]["request_status_changes"], 3)
        second_outcome = harness.run([duplicate_receipt()])
        second = self.report_of(second_outcome)
        adoption = self.stage(second, "adoption")
        self.assertEqual(adoption["counts"]["output"], 0)
        self.assertEqual(adoption["counts"]["skipped"], 4)
        self.assertEqual(second["new_data"]["request_status_changes"], 0)
        self.assertEqual(second_outcome.ledger.count("adopted"), 3)

    # ------------------------------------------------------------------ #
    # three consecutive runs
    # ------------------------------------------------------------------ #
    def test_three_consecutive_runs_pass_and_stay_consistent(self):
        harness = self.harness()
        outcomes = harness.run_consecutive(3)
        self.assertEqual(len(outcomes), 3)
        reports = [self.report_of(outcome) for outcome in outcomes]
        for index, (outcome, report) in enumerate(zip(outcomes, reports)):
            with self.subTest(run=index + 1):
                self.assertEqual(outcome.exit_code, EXIT_COMPLETED)
                self.assertEqual(outcome.outcome, "completed")
                self.assert_report_valid_and_ordered(report)
                self.assertEqual(report["overall_status"], "ok")
                self.assertFalse(report["notify_failed"])
        self.assertEqual(reports[0]["new_data"]["documents"], 3)
        for report in reports[1:]:
            self.assertEqual(report["new_data"], {
                "documents": 0, "pages": 0, "extracted_units": 0,
                "portal_releases": 0, "request_status_changes": 0})
        self.assertEqual(harness.ledger.count("runs"), 3)
        self.assertEqual(harness.ledger.count("documents"), 3)
        self.assertEqual(harness.ledger.count("adopted"), 3)
        self.assertEqual(harness.ledger.count("releases"), 3)

    # ------------------------------------------------------------------ #
    # contract guard: the harness cannot emit a malformed report
    # ------------------------------------------------------------------ #
    def test_build_run_report_rejects_bad_stage_order(self):
        outcomes = [StageOutcome(name=name, status="ok") for name in reversed(STAGES)]
        with self.assertRaises(ValueError):
            build_run_report("run_20260101T000000Z_00000000", "test",
                             "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", outcomes)


if __name__ == "__main__":
    unittest.main()
