"""H2 vision gates: synthetic provider-schema replies, no real model or records."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from campaign_tool.records import ocr_stage as stage
from campaign_tool.records.extract import vision
from tests.records import test_ocr_stage as stage_fixtures
from tests.records import test_ocr_vision as extraction_fixtures
from tests.records.ocr_stage_fixtures import FakeTools, FakeVision, noise_page, scanned_pdf

CLEAN = stage_fixtures.CLEAN
REFUSAL = "I'm sorry, I can't read this image. Please provide a clearer scan."
BAD_TEXT = (
    ("", "vision_empty"),
    ("  \n\t", "vision_empty"),
    (REFUSAL, "vision_empty"),
    ("I cannot transcribe the image because it is too blurry to read.", "vision_empty"),
    ("I’m sorry, this image is not legible enough to transcribe.", "vision_empty"),
    ("<think>Considering the image with lots of plausible looking reasoning", "vision_response_incomplete"),
    ("<THINK>Reasoning that was never finished", "vision_response_incomplete"),
    ("<think>done</think><think>still thinking", "vision_response_incomplete"),
    ("Some preliminary words <think>unfinished reasoning", "vision_response_incomplete"),
    ("<think>only reasoning</think>", "vision_empty"),
    ("<think>done</think>" + REFUSAL, "vision_empty"),
    (".", "vision_low_quality"),
    ("~ _- |; ., '' :", "vision_low_quality"),
    ("a" * 19, "vision_low_quality"),
    ("a" * 20 + "." * 20, "vision_low_quality"),
)


def reply(text: object = CLEAN, finish: object = "stop", **message):
    # Sanitized OpenAI-compatible choices/message/finish_reason schema used by
    # the existing llama.cpp client. Missing/null variants are tested explicitly.
    return json.dumps({"choices": [{"index": 0, "finish_reason": finish,
                                   "message": {"role": "assistant", "content": text, **message}}]}).encode()


class StageVisionQualityTests(unittest.TestCase):
    def run_stage(self, transport, **config):
        ws = stage_fixtures.Workspace(self)
        digest = ws.add("synthetic.pdf", scanned_pdf([noise_page(901)]))
        ws.write_queue()
        cfg = ws.config(ocrmypdf=stage_fixtures.fake_ocrmypdf(ws.tmp),
                        vision={"endpoint": "http://127.0.0.1:8003/v1/chat/completions"}, **config)
        line, code, outcomes = stage.run(ws.args(cfg), runner=FakeTools({digest: [""]}), transport=transport)
        self.assertEqual(code, 0)
        return ws, digest, cfg, line, outcomes[0], stage_fixtures.results(ws.out)[digest]

    def assert_unresolved(self, record, line, error):
        self.assertEqual((record["status"], record["code"], record["unresolved_pages"]),
                         ("ocr_partial", "ocr_pages_unresolved", 1))
        self.assertEqual(record["review_state"], "not_reviewed")
        page = record["pages"][0]
        self.assertEqual((page["status"], page["engine"], page["vision_error"]),
                         ("ocr_empty", "tesseract", error))
        self.assertNotIn("text_file", page)
        self.assertEqual((line["status"], line["error_code"]), ("warn", "OCR_PAGES_UNRESOLVED"))

    def test_rejection_class_stays_unresolved_without_storing_vision_text(self):
        for text, error in BAD_TEXT:
            with self.subTest(text=text):
                ws, _, _, line, outcome, record = self.run_stage(FakeVision(text=text))
                self.assert_unresolved(record, line, error)
                self.assertEqual((outcome.vision_calls, outcome.unresolved_pages), (1, 1))
                self.assertEqual(list(ws.out.glob("*/*/page-*.txt")), [])

    def test_finish_reason_must_be_explicit_stop_even_for_clean_text(self):
        for finish in (None, "length", "content_filter", "tool_calls", "error", "", 1):
            with self.subTest(finish=finish):
                *_, line, outcome, record = self.run_stage(FakeVision(raw=reply(finish=finish)))
                self.assert_unresolved(record, line, "vision_response_incomplete")
        missing = json.loads(reply())
        del missing["choices"][0]["finish_reason"]
        *_, line, _, record = self.run_stage(FakeVision(raw=json.dumps(missing).encode()))
        self.assert_unresolved(record, line, "vision_response_incomplete")

    def test_structured_refusal_and_null_content_are_empty_not_complete(self):
        for raw in (reply(None, refusal="I cannot comply"), reply(CLEAN, refusal="Refused"), reply(None)):
            with self.subTest(raw=raw):
                *_, line, _, record = self.run_stage(FakeVision(raw=raw))
                self.assert_unresolved(record, line, "vision_empty")

    def test_clean_and_closed_reasoning_positive_controls_remain_unreviewed(self):
        for text in (CLEAN, "<think>inspect the scan</think>\n" + CLEAN, "a" * 20):
            with self.subTest(text=text):
                ws, _, _, line, _, record = self.run_stage(FakeVision(text=text))
                self.assertEqual((record["status"], record["unresolved_pages"]), ("ocr_complete", 0))
                self.assertEqual(record["review_state"], "not_reviewed")
                self.assertEqual(record["pages"][0]["engine"], "qwen")
                expected = "a" * 20 if text == "a" * 20 else CLEAN
                self.assertEqual(next(ws.out.glob("*/*/page-0001.txt")).read_text(), expected)
                self.assertEqual(line["review_required"], 1)

    def test_configured_quality_applies_to_vision_too(self):
        for config in ({"min_chars": 1000}, {"min_alnum_ratio": 1.0}):
            with self.subTest(config=config):
                *_, line, _, record = self.run_stage(FakeVision(text=CLEAN), **config)
                self.assert_unresolved(record, line, "vision_low_quality")

    def test_rejected_cached_result_still_warns_and_never_calls_vision(self):
        ws, digest, cfg, _, _, _ = self.run_stage(FakeVision(text=REFUSAL))
        tools, transport = FakeTools(), FakeVision()
        line, _, outcomes = stage.run(ws.args(cfg), runner=tools, transport=transport)
        self.assertEqual((tools.calls, transport.requests), ([], []))
        self.assertEqual((outcomes[0].status, outcomes[0].unresolved_pages), ("cached", 1))
        self.assert_unresolved(stage_fixtures.results(ws.out)[digest], line, "vision_empty")

    def test_gate_version_changes_cache_identity(self):
        new = stage.build_stage("/nonexistent", {})
        with mock.patch.object(stage, "VISION_OUTPUT_GATE", 0):
            old = stage.build_stage("/nonexistent", {})
        self.assertNotEqual(new.signature, old.signature)

    def test_negative_control_bypassed_refusal_gate_reproduces_unsafe_completion(self):
        # Same refusal and fake tools: breaking only the actual syntax gate must
        # expose the old defect. This is a control, not a production bypass.
        with mock.patch.object(stage, "clean_vision_text", side_effect=lambda text: text):
            *_, record = self.run_stage(FakeVision(text=REFUSAL))
            self.assertEqual(record["status"], "ocr_complete")
        *_, line, _, record = self.run_stage(FakeVision(text=REFUSAL))
        self.assert_unresolved(record, line, "vision_empty")

    def test_negative_control_bypassed_quality_gate_accepts_punctuation(self):
        original = stage.Quality.check
        def broken_quality(quality, text):
            return None if text == "." else original(quality, text)
        with mock.patch.object(stage.Quality, "check", broken_quality):
            *_, record = self.run_stage(FakeVision(text="."))
            self.assertEqual(record["status"], "ocr_complete")
        *_, line, _, record = self.run_stage(FakeVision(text="."))
        self.assert_unresolved(record, line, "vision_low_quality")

    def test_transcribe_adapter_never_prints_rejected_text(self):
        ws = stage_fixtures.Workspace(self)
        png = ws.tmp / "page.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        cfg = ws.config(vision={"endpoint": "http://127.0.0.1:8003/v1/chat/completions"})
        for raw in (reply(REFUSAL), reply("."), reply(finish="length")):
            output = io.StringIO()
            with self.subTest(raw=raw), contextlib.redirect_stdout(output):
                code = stage.transcribe_png_main(["--config", cfg, str(png)], transport=FakeVision(raw=raw))
            self.assertEqual((code, output.getvalue()), (1, ""))

    def test_fresh_process_import_and_disabled_defaults_have_no_external_effects(self):
        probe = Path(__file__).with_name("vision_import_probe.py")
        ws = stage_fixtures.Workspace(self)
        done = subprocess.run([sys.executable, "-B", str(probe)], cwd=ws.tmp,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "IMPORT_DEFAULTS_OFFLINE_OK\n", ""))


class ExtractionVisionQualityTests(unittest.TestCase):
    def setUp(self):
        self.h = extraction_fixtures.VisionFallbackTests("runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_rejected_class_never_becomes_unit_text_or_accepted_evidence(self):
        for text, error in BAD_TEXT:
            with self.subTest(text=text):
                source, receipt, result = self.h.bundle(extraction_fixtures.Runner(
                    text="", confidence="0", vision_text=text))
                entry = result["vision_receipts"][0]
                self.assertEqual(entry["status"], "blocked")
                self.assertEqual((result["pages"][1]["status"], result["pages"][1]["reason"]), ("blocked", error))
                directory = self.h.vision_dir(result)
                self.assertFalse((directory / "sidecar.txt").exists())
                # Native extraction retains the empty page unit for coverage;
                # rejected vision output must never fill that placeholder.
                self.assertEqual([unit["text"] for unit in result["units"] if unit["locator"].get("page") == 2], [""])
                loaded = vision.load_vision_receipt(self.h.ocr_root, result["original_sha256"], 2, entry["receipt_id"])
                self.assertIsNone(loaded["sidecar"])
                self.assertEqual(loaded["receipt"]["review_status"], "not_reviewed")
                enrolled = self.h.enroll(source, receipt)
                self.h.h.install()
                with self.assertRaisesRegex(extraction_fixtures.enrollment.ExtractionBindingError,
                                            "ocr_page_not_accepted:blocked"):
                    self.h.h.validator.evidence(enrolled["import_id"])

    def test_closed_reasoning_is_removed_and_positive_text_stays_unreviewed(self):
        _, _, result = self.h.bundle(extraction_fixtures.Runner(
            text="", confidence="0", vision_text="<think>inspect</think>\n" + CLEAN))
        entry = result["vision_receipts"][0]
        self.assertEqual(entry["status"], vision.TEXT_STATE)
        self.assertEqual(result["units"][1]["text"], CLEAN)
        loaded = vision.load_vision_receipt(self.h.ocr_root, result["original_sha256"], 2, entry["receipt_id"])
        self.assertEqual(loaded["receipt"]["review_status"], "not_reviewed")
        self.assertTrue(loaded["receipt"]["visual_check_required"])

    def test_gate_version_separates_receipt_identity_and_preserves_prior_bytes(self):
        _, _, result = self.h.bundle(extraction_fixtures.Runner(text="", confidence="0"))
        old_dir = self.h.vision_dir(result)
        before = {p.name: p.read_bytes() for p in old_dir.iterdir()}
        entry = result["vision_receipts"][0]
        runner = extraction_fixtures.Runner(text="", confidence="0")
        with mock.patch.object(vision, "VISION_OUTPUT_GATE", 0):
            other = vision.run_fallback(self.h.ocr_root, result["original_sha256"], 2,
                                        entry["base_receipt_id"], vision_adapter=extraction_fixtures.VISION, runner=runner)
        self.assertNotEqual(other["receipt_id"], entry["receipt_id"])
        self.assertEqual(runner.calls, ["qwen-vision"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in old_dir.iterdir()})


if __name__ == "__main__":
    unittest.main()
