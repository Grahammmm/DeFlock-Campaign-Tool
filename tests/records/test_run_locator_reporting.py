"""Synthetic extraction/report boundaries; no native workers or ledger writes."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from campaign_tool.records import run


class RunLocatorReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.subject = run.sha(b"synthetic original")
        self.locator = {"page": 2, "sheet": "private-sheet@example.invalid", "member": "/private/synthetic/path"}
        self.result = {"status": "partial", "run_path": str(self.root), "issues": [
            {"code": "page_error", "locator": self.locator,
             "detail": "OSError: /private/synthetic/error-path"}], "pages": []}
        self.pipeline = object.__new__(run.Pipeline)
        self.pipeline.root = SimpleNamespace(sub=lambda name: self.root)
        self.pipeline.ocr_tools = self.pipeline.ocr_tool_signature = self.pipeline.ocr_vision = None
        self.pipeline.extraction_timeout = 1
        self.pipeline._original = Mock(return_value={"storage_path": "/private/synthetic/original"})
        self.pipeline._form = Mock(return_value="pdf")
        self.pipeline._alert = Mock()
        self.pipeline._open_stage_run = Mock(return_value={})
        self.pipeline._reconcile_review_authority = Mock(return_value=True)
        self.pipeline._states = Mock(return_value={"extract": {"status": "pending"}})
        self.pipeline.subjects = Mock(return_value=[self.subject])

    def progress(self):
        with patch.object(run.extraction_routes, "extract", return_value=self.result):
            return self.pipeline.advance_all()

    def test_partial_issue_locator_survives_run_report_without_raw_labels_or_errors(self):
        before = copy.deepcopy(self.result)
        artifact = self.root / "extraction.json"
        artifact.write_bytes(run.encoded(self.result))
        original = artifact.read_bytes()
        progress = self.progress()
        self.assertEqual(progress[0]["stages"], {"extract": "blocked:extraction_partial:page_error"})
        gap = progress[0]["extraction_gaps"][0]
        self.assertEqual(gap, {"original_sha256": self.subject, "extraction_status": "partial",
            "reason": "page_error", "locator": {"page": 2}, "locator_sha256": run.sha(run.encoded(self.locator))})
        serialized = json.dumps(progress)
        for marker in ("private-sheet", "/private/synthetic", "OSError"):
            self.assertNotIn(marker, serialized)
        self.assertEqual(self.result, before)
        self.assertEqual(artifact.read_bytes(), original)
        self.pipeline._alert.assert_called_once_with("extract:" + self.subject + ":page_error", owner="runtime")

    def test_pdf_page_gaps_survive_when_document_issue_has_no_locator(self):
        self.result.update(issues=[{"code": "ocr_runtime_unconfigured"}], pages=[
            {"page_no": 1, "status": "ok", "reason": None},
            {"page_no": 2, "status": "partial", "reason": "ocr_needed_or_blank"},
            {"page_no": 3, "status": "blocked", "reason": "page_not_extracted"}])
        gaps = self.progress()[0]["extraction_gaps"]
        self.assertEqual([(g["reason"], g["locator"]) for g in gaps], [
            ("ocr_runtime_unconfigured", None), ("ocr_needed_or_blank", {"page": 2}),
            ("page_not_extracted", {"page": 3})])

    def test_text_only_locators_remain_distinct_without_disclosing_labels(self):
        self.result["issues"] = [{"code": "zip_member_error", "locator": {"member": name}}
                                 for name in ("private-first", "private-second")]
        gaps = self.progress()[0]["extraction_gaps"]
        self.assertEqual([g["locator"] for g in gaps], [None, None])
        self.assertNotEqual(gaps[0]["locator_sha256"], gaps[1]["locator_sha256"])
        self.assertNotIn("private-", json.dumps(gaps))

    def test_docx_altchunk_coordinate_survives_without_free_form_part(self):
        locator = {"part": "word/document.xml", "altchunk": 2}
        self.result["issues"] = [{"code": "docx_altchunk_content_not_extracted", "locator": locator, "detail": ""}]
        before = copy.deepcopy(self.result)
        gap = self.progress()[0]["extraction_gaps"][0]
        self.assertEqual(gap["locator"], {"altchunk": 2})
        self.assertEqual(gap["locator_sha256"], run.sha(run.encoded(locator)))
        self.assertEqual(gap["reason"], "docx_altchunk_content_not_extracted")
        self.assertEqual(gap["extraction_status"], "partial")
        self.assertNotIn("word/document.xml", json.dumps(gap))
        self.assertEqual(self.result, before)

    def test_blocked_without_issue_stays_blocked_and_actionable(self):
        self.result.update(status="blocked", issues=[])
        progress = self.progress()[0]
        self.assertEqual(progress["stages"]["extract"], "blocked:extraction_blocked:")
        self.assertEqual(progress["extraction_gaps"][0]["reason"], "extraction_not_complete")

    def test_accepted_ocr_partial_retains_visual_gaps_and_acceptance(self):
        self.result.update(ocr_receipts=[{"fixture": True}], receipt_sha256="a" * 64,
                           issues=[{"code": "ocr_visual_review_required", "locator": {"page": 2}}])
        stage = {"enrollment": Mock(), "extraction": Mock()}
        stage["enrollment"].enroll.return_value = {"import_id": "fixture", "units": 1}
        stage["extraction"].accept.return_value = {"status": "done"}
        with patch.object(run.extraction_routes, "extract", return_value=self.result):
            result = self.pipeline.stage_extract(self.subject, stage)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["extraction_gaps"][0]["reason"], "ocr_visual_review_required")

    def test_human_and_json_cli_include_the_same_sanitized_gap(self):
        report = {"run_id": "fixture", "originals": 1, "intake": {"messages": 0, "preserved": [], "failures": []},
            "recovery": {"interrupted_runs": [], "recovered_leases": []}, "counts": {},
            "end_to_end_complete": False, "proposals_awaiting_owner": 0, "exit_code": 3,
            "subjects": self.progress()}
        for options in ([], ["--json"]):
            output = io.StringIO()
            with patch("campaign_tool.records.runtime.require", return_value=True), \
                 patch.object(run, "build_pipeline", return_value=SimpleNamespace(run=lambda *a, **k: report)), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(run.main(["--root", str(self.root), *options]), 3)
            self.assertIn(self.subject, output.getvalue())
            self.assertIn('"page": 2', output.getvalue())
            self.assertNotIn("/private/synthetic", output.getvalue())

    def test_complete_extraction_does_not_gain_a_gap_or_change_acceptance(self):
        self.result.update(status="complete", issues=[], receipt_sha256="a" * 64)
        stage = {"enrollment": Mock(), "extraction": Mock()}
        stage["enrollment"].enroll.return_value = {"import_id": "fixture", "units": 1}
        stage["extraction"].accept.return_value = {"status": "done"}
        with patch.object(run.extraction_routes, "extract", return_value=self.result):
            result = self.pipeline.stage_extract(self.subject, stage)
        self.assertEqual(result["status"], "done")
        self.assertNotIn("extraction_gaps", result)
