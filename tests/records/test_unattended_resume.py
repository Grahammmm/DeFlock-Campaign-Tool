"""Synthetic fair-resume and mechanical-count regressions; no private inputs."""
import json
import unittest
from unittest.mock import patch

from campaign_tool.records import run, unattended
from campaign_tool.records.run_safety import RunSafetyPolicy
from tests.records import test_run_safety as fixtures


class ResumeTests(fixtures.SafetyFixture, unittest.TestCase):
    def pipeline(self, rows, held=(), limit=200):
        pipeline = unattended.UnattendedPipeline(self.root.path, policy=RunSafetyPolicy(max_originals=limit))
        pipeline.safety = self.guard(policy=pipeline.policy)
        pipeline.ocr_tools = {"synthetic": True}  # No decoder runs in these selection-only probes.
        pipeline._query = lambda sql: rows
        pipeline._open_stage_run = lambda: {}
        def states(subject):
            return {name: {"status": "blocked" if name == "extract" and subject in held else
                           "pending" if name == "extract" else "inapplicable",
                           "receipt_sha256": fixtures.original(90000) if name == "extract" and subject in held else None}
                    for name in run.STAGE_ORDER}
        pipeline._states = states
        pipeline.stage_extract = lambda subject, stage: {"status": "pending", "reason": "review_required"}
        return pipeline

    def rows(self, count):
        return [{"sha256": fixtures.original(n), "first_seen_at": f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}+00:00"}
                for n in range(count)]

    def test_200_unchanged_valid_review_holds_do_not_starve_next_original(self):
        rows = self.rows(201)
        held = {row["sha256"] for row in rows[:200]}
        for iteration in range(2):
            with self.subTest(iteration=iteration), patch.object(run.stages, "counts", return_value={}) as validate:
                pipeline = self.pipeline(rows, held)
                outcomes = pipeline.advance_all()
                self.assertEqual(pipeline.safety.phases["advance"]["attempted"], 1)
                self.assertEqual(pipeline.review_holds_retained, 200)
                self.assertEqual(outcomes[-1]["subject_sha256"], rows[-1]["sha256"])
                self.assertEqual(outcomes[-1]["stages"]["extract"], "pending")
                self.assertEqual(pipeline.originals_deferred, 0)
                validate.assert_called_once_with(self.root.ledger)
                self.assertEqual(pipeline._states(rows[0]["sha256"])["extract"]["status"], "blocked")

    def test_finite_sweep_resumes_pending_holds_without_automatic_clearing(self):
        rows = self.rows(5)
        seen = []
        for expected in (2, 2, 1):
            pipeline = self.pipeline(rows, limit=2)
            outcomes = pipeline.advance_all()
            self.assertEqual(pipeline.safety.phases["advance"]["attempted"], expected)
            seen.extend(item["subject_sha256"] for item in outcomes)
        self.assertEqual(seen, [row["sha256"] for row in rows])
        self.assertEqual(self.pipeline(rows, limit=2).advance_all()[0]["subject_sha256"], rows[0]["sha256"])

    def test_new_arrivals_do_not_preempt_existing_frontier(self):
        rows = self.rows(5)
        first = self.pipeline(rows, limit=2).advance_all()
        newcomer = {"sha256": fixtures.original(10000), "first_seen_at": "2027-01-01T00:00:00+00:00"}
        second = self.pipeline(rows + [newcomer], limit=2).advance_all()
        third = self.pipeline(rows + [newcomer], limit=2).advance_all()
        self.assertEqual([item["subject_sha256"] for item in first + second + third], [row["sha256"] for row in rows])
        self.assertNotIn(newcomer["sha256"], [item["subject_sha256"] for item in third])

    def test_metadata_acknowledgements_are_bounded_and_resume(self):
        rows = self.rows(401)
        held = {row["sha256"] for row in rows}
        with patch.object(run.stages, "counts", return_value={}):
            first = self.pipeline(rows, held)
            self.assertEqual(len(first.advance_all()), 400)
            self.assertEqual(first.originals_deferred, 1)
            second = self.pipeline(rows, held)
            self.assertEqual(len(second.advance_all()), 1)
            self.assertEqual(second.safety.phases["advance"]["attempted"], 0)

    def test_invalid_receipt_binding_is_not_acknowledged(self):
        rows = self.rows(1)
        pipeline = self.pipeline(rows, {rows[0]["sha256"]})
        with patch.object(run.stages, "counts", side_effect=ValueError("synthetic-invalid-binding")):
            with self.assertRaises(ValueError):
                pipeline.advance_all()
        self.assertEqual(pipeline.review_holds_retained, 0)
        self.assertFalse((self.root.sub("reports") / "unattended-advance-resume.json").exists())

    def test_malformed_resume_stops_without_dispatch(self):
        path = self.root.sub("reports") / "unattended-advance-resume.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text('{"schema":"records-advance-resume-v1","cursor":true,"frontier":null}')
        path.chmod(0o600)
        pipeline = self.pipeline(self.rows(1))
        with self.assertRaises(run.RunError):
            pipeline.advance_all()
        self.assertTrue(pipeline.safety.stopped)
        self.assertEqual(pipeline.safety.phases["advance"]["attempted"], 0)

    def test_technical_failure_is_charged_and_no_later_original_attempted(self):
        pipeline = self.pipeline(self.rows(3))
        def fail(*args):
            raise RuntimeError("synthetic-marker-not-for-report")
        pipeline.stage_extract = fail
        outcomes = pipeline.advance_all()
        self.assertEqual(pipeline.safety.phases["advance"]["attempted"], 1)
        self.assertEqual(pipeline.safety.phases["advance"]["failed"], 1)
        self.assertEqual(len(outcomes), 1)
        self.assertNotIn("synthetic-marker", json.dumps(outcomes))

    def test_resume_write_failure_is_not_completed_or_healthy(self):
        pipeline = unattended.UnattendedPipeline(self.root.path)
        write = unattended._private_bytes
        def fail_resume(path, raw):
            if path.name.startswith(".advance-resume-"):
                raise OSError("SYNTHETIC_RESUME_MARKER")
            return write(path, raw)
        with patch.object(unattended, "_private_bytes", side_effect=fail_resume):
            report = pipeline.run()
        self.assertEqual((report["status"], report["exit_code"]), ("failed", 2))
        self.assertEqual(report["safety"]["stop_code"], "unexpected_run_fault")
        self.assertNotIn("SYNTHETIC_RESUME_MARKER", json.dumps(report))
        self.assertTrue(self.guard().stopped)

    def test_empty_explicit_selection_does_not_write_resume_or_attempt(self):
        pipeline = self.pipeline(self.rows(3))
        self.assertEqual(pipeline.advance_all([]), [])
        self.assertEqual(pipeline.safety.phases["advance"]["attempted"], 0)
        self.assertFalse((self.root.sub("reports") / "unattended-advance-resume.json").exists())


class MechanicalCountTests(fixtures.SafetyFixture, unittest.TestCase):
    def test_unattended_mixed_email_and_image_keeps_integer_count_and_gaps(self):
        inbox = fixtures.PipelineSafetyTests.inbox(self, attachment_name="scan.png")
        pipeline = unattended.UnattendedPipeline(self.root.path)
        report = pipeline.run(inbox)
        self.assertIs(type(report["end_to_end_complete"]), int)
        self.assertEqual(report["end_to_end_complete"], 1)
        self.assertIs(report["substantive_complete"], False)
        self.assertEqual(report["substantive_review_status"], "queued")
        self.assertEqual(report["status"], "completed_with_gaps")
        self.assertEqual(report["exit_code"], 3)
        self.assertEqual(report["safety"]["phases"]["advance"]["review_required"], 1)
        self.assertEqual(run.stages.counts(self.root.ledger)["candidate_seven_stage_complete"], 1)

    def test_empty_free_run_is_operationally_complete_not_substantively_certified(self):
        report = unattended.UnattendedPipeline(self.root.path).run()
        self.assertEqual((report["status"], report["exit_code"]), ("completed", 0))
        self.assertIs(type(report["end_to_end_complete"]), int)
        self.assertEqual(report["end_to_end_complete"], 0)
        self.assertIs(report["substantive_complete"], False)
        self.assertEqual(report["substantive_review_status"], "not_applicable")

    def test_free_nominal_progress_preserves_nested_analysis_without_provider_access(self):
        inbox = fixtures.PipelineSafetyTests.inbox(self)
        def no_provider(*args, **kwargs):
            raise AssertionError("provider access forbidden in free operational mode")
        pipeline = run.Pipeline(self.root.path, opener=no_provider)
        unchecked = pipeline._run_locked_unchecked
        analysis = {"mode": "detector_only", "substantive_complete": False}
        def with_analysis(*args, **kwargs):
            report = unchecked(*args, **kwargs)
            report["analysis"] = analysis
            return report
        with patch.object(pipeline, "_run_locked_unchecked", side_effect=with_analysis):
            report = pipeline.run(inbox)
        self.assertEqual((report["status"], report["exit_code"]), ("completed", 0))
        self.assertIs(type(report["end_to_end_complete"]), int)
        self.assertEqual(report["end_to_end_complete"], 2)
        self.assertIs(report["substantive_complete"], False)
        self.assertEqual(report["substantive_review_status"], "queued")
        self.assertIs(report["analysis"], analysis)
        self.assertIsNone(report["model_id"])
        self.assertIsNone(report["challenge_model_id"])


if __name__ == "__main__":
    unittest.main()
