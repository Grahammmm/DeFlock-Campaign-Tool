"""T6: Tesseract first, local vision fallback (engine=tesseract|qwen) through routes, ledger and validation.

Everything is synthetic: injected OCR and vision runners, fictional PDFs, no real model.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import extraction_ledger as enrollment
from campaign_tool.records import extraction_routes as routes
from campaign_tool.records import ocr_closure
from campaign_tool.records.extract import vision
from tests.records import ocr_fixtures
from tests.records import test_extraction_validation as fixtures

VISION = vision.adapter(["/synthetic/bin/qwen-vision", "--config", "/synthetic/vision.json"],
                        "qwen", "qwen3.8:27b-thinkingcap-q6", "synthetic-vision-v1")
CLEAN = "Synthetic typed page with ordinary words\n"
HANDWRITTEN_VISION = "Synthetic handwritten note transcribed\n"


class Runner(ocr_fixtures.FakeOCRRunner):
    """FakeOCRRunner plus the vision command: prints a transcription or fails."""

    def __init__(self, text=CLEAN, confidence="95", vision_text=HANDWRITTEN_VISION, vision_rc=0):
        super().__init__(text=text, confidence=confidence)
        self.vision_text, self.vision_rc = vision_text, vision_rc

    def __call__(self, command, **kwargs):
        if Path(command[0]).name == "qwen-vision":
            self.calls.append("qwen-vision")
            self.vision_page = Path(command[-1]).read_bytes()
            stdout = b"" if self.vision_rc else self.vision_text.encode("utf-8")
            return subprocess.CompletedProcess(command, self.vision_rc, stdout, b"")
        return super().__call__(command, **kwargs)


@unittest.skipUnless(sys.platform.startswith("linux"), "native extraction isolation requires Linux")
class VisionFallbackTests(unittest.TestCase):
    def setUp(self):
        fixtures.ExtractionValidationTests.setUpClass()
        self.h = fixtures.ExtractionValidationTests("runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.ocr_root = self.h.root / "page-ocr"
        self.ocr_root.mkdir(mode=0o700)
        self.count = 0

    def bundle(self, runner, *, ocr_vision=VISION, native_text=None):
        """Real routes.extract() of a fictional two-page PDF whose page 2 is image-only."""
        self.count += 1
        try:
            raw = ocr_fixtures.mixed_pdf(native_text or f"Synthetic native text {self.count}")
        except ImportError:
            self.skipTest("pypdf optional parser unavailable")
        source = self.h.root / f"original-{self.count}.pdf"
        source.write_bytes(raw)
        source.chmod(0o400)
        subject = hashlib.sha256(raw).hexdigest()
        with self.h.store.ledger(self.h.database) as con:
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)", (subject, len(raw), None, None, "original",
                        "unresolved", str(source), "receipt_validation_pending", None, "{}"))
            con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)", (f"pdf-source-{self.count}", subject, "local",
                        "synthetic", None, None, "fixture", "{}"))
            for stage in self.h.store.STAGES:
                con.execute("INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)", (subject, stage, "pending", None, "fixture",
                            "2030-01-01T00:00:00+00:00", "intake", None))
            con.commit()
        out = self.h.root / f"pdf-derived-{self.count}"
        out.mkdir(mode=0o700)
        result = routes.extract(source, subject, out, "pdf", timeout=30, ocr_output_root=str(self.ocr_root),
                                ocr_tools=ocr_fixtures.TOOLS, tool_signature="stub-v1", ocr_runner=runner,
                                ocr_vision=ocr_vision)
        self.assertEqual([entry["page"] for entry in result["ocr_receipts"]], [2])
        return source, Path(result["run_path"]) / "extraction.json", result

    def enroll(self, source, receipt):
        return self.h.adapter.enroll(original_path=source, receipt_path=receipt,
                                     receipt_sha256=enrollment.sha(receipt.read_bytes()))

    def page_rows(self, subject):
        with self.h.store.ledger(self.h.database, readonly=True) as con:
            state = con.execute("SELECT page_no,method,status,reason,needs_visual_review FROM page_state "
                                "WHERE original_sha256=? ORDER BY page_no", (subject,)).fetchall()
            history = [json.loads(row[0]) for row in con.execute(
                "SELECT p.page_json FROM extraction_adapter_pages p JOIN extraction_adapter_imports i "
                "ON i.id=p.import_id WHERE i.original_sha256=? ORDER BY p.page_no", (subject,))]
        return [tuple(row) for row in state], history

    # --- acceptance shape: 10 clean scans -> 0 vision calls; 2 handwritten -> vision ---------

    def test_ten_clean_scans_make_no_vision_call_and_record_tesseract(self):
        runner = Runner()
        for _ in range(10):
            source, receipt, result = self.bundle(runner)
            self.assertNotIn("vision_receipts", result)
            self.assertEqual(result["pages"][1]["engine"], "tesseract")
            self.assertEqual(result["units"][1]["data"]["engine"], "tesseract")
            enrolled = self.enroll(source, receipt)
            _, history = self.page_rows(result["original_sha256"])
            self.assertEqual(history[1]["engine"], "tesseract")
            self.assertNotIn("vision_receipt_id", history[1])
            self.assertEqual(enrolled["stage_promotions"], 0)
        self.assertEqual(runner.calls.count("qwen-vision"), 0)
        self.assertEqual(runner.calls.count("tesseract"), 10)

    def test_two_handwritten_pages_go_to_vision_and_ledger_records_qwen(self):
        cases = (Runner(text="", confidence="0"),          # Tesseract found no text
                 Runner(text="~ ,; i1 l\n", confidence="23"))  # low confidence
        for runner in cases:
            source, receipt, result = self.bundle(runner)
            self.assertEqual(runner.calls.count("qwen-vision"), 1)
            self.assertEqual(runner.vision_page, b"synthetic raster")
            [entry] = result["vision_receipts"]
            self.assertEqual((entry["page"], entry["engine"], entry["status"]), (2, "qwen", vision.TEXT_STATE))
            self.assertEqual(entry["base_receipt_id"], result["ocr_receipts"][0]["receipt_id"])
            self.assertEqual(result["units"][1]["text"], HANDWRITTEN_VISION)
            self.assertNotIn("ocr_pages_blocked", [issue["code"] for issue in result["issues"]])
            enrolled = self.enroll(source, receipt)
            self.assertIn("ocr_visual_review_required", enrolled["gaps"])
            state, history = self.page_rows(result["original_sha256"])
            self.assertEqual(state[1], (2, vision.VERSION, "partial", "ocr_visual_review_required", 1))
            self.assertEqual((history[0]["status"], history[1]["engine"], history[1]["vision_receipt_id"]),
                             ("ok", "qwen", entry["receipt_id"]))
            with self.h.store.ledger(self.h.database, readonly=True) as con:
                provenance = [json.loads(r[0]) for r in con.execute(
                    "SELECT provenance_json FROM units WHERE original_sha256=?", (result["original_sha256"],))]
            self.assertTrue(all(p["vision_receipt_ids"] == [entry["receipt_id"]] for p in provenance))

    def test_vision_page_accepted_with_engine_and_visual_hold(self):
        source, receipt, result = self.bundle(Runner(text="", confidence="0"))
        enrolled = self.enroll(source, receipt)
        self.h.install()
        raw, _, coverage = self.h.validator.evidence(enrolled["import_id"])
        envelope = json.loads(raw)
        self.assertEqual(envelope["ocr"]["engines"], {"2": "qwen"})
        self.assertEqual(envelope["ocr"]["vision_receipt_ids"], [result["vision_receipts"][0]["receipt_id"]])
        self.assertTrue(envelope["ocr"]["visual_review_pending"])
        self.assertIn("ocr_vision", envelope["installed_code"])
        self.assertEqual(coverage["denominator"], {"kind": "pages", "total": 2})

    # --- failure dispositions --------------------------------------------------------------

    def test_failed_vision_keeps_low_confidence_tesseract_text(self):
        runner = Runner(text="faint typed words here\n", confidence="40", vision_rc=3)
        source, receipt, result = self.bundle(runner)
        [entry] = result["vision_receipts"]
        self.assertEqual(entry["status"], "blocked")
        self.assertEqual(result["pages"][1]["engine"], "tesseract")
        self.assertEqual(result["pages"][1]["vision_status"], "blocked")
        self.assertEqual(result["units"][1]["text"], "faint typed words here\n")
        enrolled = self.enroll(source, receipt)
        self.h.install()
        envelope = json.loads(self.h.validator.evidence(enrolled["import_id"])[0])
        self.assertEqual(envelope["ocr"]["engines"], {"2": "tesseract"})

    def test_empty_page_with_failed_vision_stays_blocked_and_refuses_acceptance(self):
        source, receipt, result = self.bundle(Runner(text="", confidence="0", vision_text="  \n"))
        self.assertEqual(result["vision_receipts"][0]["status"], "blocked")
        self.assertEqual(result["pages"][1]["status"], "blocked")
        self.assertEqual(result["pages"][1]["reason"], "no_text_after_vision")
        self.assertIn("ocr_pages_blocked", [issue["code"] for issue in result["issues"]])
        enrolled = self.enroll(source, receipt)
        self.h.install()
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, "ocr_page_not_accepted:blocked"):
            self.h.validator.evidence(enrolled["import_id"])

    def test_vision_off_by_default(self):
        runner = Runner(text="", confidence="0")
        _, _, result = self.bundle(runner, ocr_vision=None)
        self.assertEqual(runner.calls.count("qwen-vision"), 0)
        self.assertNotIn("vision_receipts", result)
        self.assertEqual((result["pages"][1]["status"], result["pages"][1]["engine"]), ("blocked", "tesseract"))

    def test_rerun_reuses_vision_receipt_without_calling_the_model(self):
        runner = Runner(text="", confidence="0")
        _, _, first = self.bundle(runner, native_text="Same synthetic page")
        second_runner = Runner(text="", confidence="0", vision_text="different text\n")
        out = self.h.root / "pdf-derived-again"
        out.mkdir(mode=0o700)
        source = self.h.root / f"original-{self.count}.pdf"
        second = routes.extract(source, first["original_sha256"], out, "pdf", timeout=30,
                                ocr_output_root=str(self.ocr_root), ocr_tools=ocr_fixtures.TOOLS,
                                tool_signature="stub-v1", ocr_runner=second_runner, ocr_vision=VISION)
        self.assertEqual(second_runner.calls.count("qwen-vision"), 0)
        self.assertEqual(second["vision_receipts"], first["vision_receipts"])
        self.assertEqual(second["units"][1]["text"], HANDWRITTEN_VISION)

    # --- tamper and binding refusals -------------------------------------------------------

    def vision_dir(self, result):
        entry = result["vision_receipts"][0]
        return (self.ocr_root / "vision" / result["original_sha256"] / f"page-{entry['page']:06d}" /
                entry["receipt_id"])

    def test_tampered_vision_text_refuses_enrollment(self):
        source, receipt, result = self.bundle(Runner(text="", confidence="0"))
        sidecar = self.vision_dir(result) / "sidecar.txt"
        sidecar.chmod(0o600)
        sidecar.write_text("tampered vision text")
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, "vision_receipt_unverified"):
            self.enroll(source, receipt)

    def test_extra_vision_asset_refuses_enrollment(self):
        source, receipt, result = self.bundle(Runner(text="", confidence="0"))
        (self.vision_dir(result) / "notes.txt").write_text("unexpected")
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, "vision_receipt_unverified"):
            self.enroll(source, receipt)

    def test_vision_entry_must_name_the_tesseract_receipt_it_supersedes(self):
        source, receipt, result = self.bundle(Runner(text="", confidence="0"))
        for path in (receipt, receipt.parent / "ocr-derived" / "digest.json"):
            path.chmod(0o600)
            data = json.loads(path.read_text())
            data["vision_receipts"][0]["base_receipt_id"] = "0" * 64
            path.write_text(json.dumps(data))
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, "vision_receipt_unbound"):
            self.enroll(source, receipt)

    def test_vision_receipts_missing_from_metadata_refuse(self):
        source, receipt, result = self.bundle(Runner(text="", confidence="0"))
        digest = receipt.parent / "ocr-derived" / "digest.json"
        digest.chmod(0o600)
        data = json.loads(digest.read_text())
        del data["vision_receipts"]
        digest.write_text(json.dumps(data))
        with self.assertRaisesRegex(enrollment.ExtractionBindingError, "vision_receipts_metadata_mismatch"):
            self.enroll(source, receipt)

    def test_cas_vision_closure_refuses_missing_or_changed_assets(self):
        source, receipt, result = self.bundle(Runner(text="", confidence="0"))
        self.enroll(source, receipt)
        entry = result["vision_receipts"][0]
        artifacts = ocr_closure.vision_closure_artifacts(self.ocr_root, result["original_sha256"], 2, entry["receipt_id"])
        self.assertEqual(set(artifacts), {"vision_receipt:" + entry["receipt_id"]} | {
            "vision_asset:" + entry["receipt_id"] + ":" + name for name in ocr_closure.VISION_NAMES})
        loaded = ocr_closure.load_cas_vision(artifacts, result["original_sha256"], 2, entry["receipt_id"])
        self.assertEqual(loaded["receipt_sha256"], entry["receipt_sha256"])
        for key in artifacts:
            with self.subTest(missing=key), self.assertRaises((ValueError, OSError)):
                ocr_closure.load_cas_vision({k: v for k, v in artifacts.items() if k != key},
                                            result["original_sha256"], 2, entry["receipt_id"])
            with self.subTest(changed=key), self.assertRaises((ValueError, OSError)):
                ocr_closure.load_cas_vision(dict(artifacts, **{key: artifacts[key] + b" "}),
                                            result["original_sha256"], 2, entry["receipt_id"])
        self.assertTrue(ocr_closure.vision_artifact_name("vision_asset:" + "a" * 64 + ":sidecar.txt"))
        self.assertFalse(ocr_closure.vision_artifact_name("vision_asset:" + "a" * 64 + ":confidence.tsv"))


class VisionConfigAndStageWiringTests(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.temp = tempfile.TemporaryDirectory(prefix="vision-config-")
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def config(self, body=None, mode=0o600, name="vision.json"):
        path = self.dir / name
        body = {"schema": vision.CONFIG_SCHEMA, "argv": list(VISION["argv"]), "engine": "qwen",
                "model": VISION["model"], "signature": VISION["signature"]} if body is None else body
        path.write_text(json.dumps(body))
        path.chmod(mode)
        return path

    def test_load_config_accepts_owner_only_exact_schema(self):
        self.assertEqual(vision.load_config(self.config()), VISION)

    def test_load_config_refuses_unsafe_or_malformed_files(self):
        good = self.config()
        link = self.dir / "link.json"
        link.symlink_to(good)
        cases = {"group_readable": self.config(mode=0o640, name="open.json"), "symlink": link,
                 "absent": self.dir / "absent.json",
                 "extra_key": self.config(dict(json.loads(good.read_text()), endpoint="http://x"), name="extra.json"),
                 "relative_argv": self.config(dict(json.loads(good.read_text()), argv=["qwen"]), name="rel.json"),
                 "tesseract_engine": self.config(dict(json.loads(good.read_text()), engine="tesseract"), name="t.json")}
        for label, path in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                vision.load_config(path)

    def stage(self, *extra, tools=("tools", "sig")):
        from campaign_tool.records import native_extract, run_stages
        args = run_stages.parser().parse_args(["extract", "--project", str(self.dir), *extra])
        seen = {}

        def fake_run(processing, **kwargs):
            seen.update(kwargs)
            return "summary"
        with mock.patch.object(run_stages, "paths", return_value=(None, self.dir, None)), \
                mock.patch.object(native_extract, "run", side_effect=fake_run), \
                mock.patch.object(run_stages, "extract_result", return_value=({"status": "ok"}, 0)), \
                mock.patch("campaign_tool.records.run.local_ocr_tools", return_value=tools):
            outcome = run_stages.stage_extract(args)
        return outcome, seen

    def test_extract_stage_is_native_only_by_default(self):
        (result, code), seen = self.stage()
        self.assertEqual(code, 0)
        self.assertIsNone(seen["ocr"])

    def test_extract_stage_passes_ocr_and_vision_from_the_stage_plan(self):
        (_, code), seen = self.stage("--ocr", "--ocr-vision-config", str(self.config()))
        self.assertEqual(code, 0)
        self.assertEqual(seen["ocr"], {"tools": "tools", "tool_signature": "sig", "vision": VISION})
        (_, code), seen = self.stage("--ocr")
        self.assertEqual(seen["ocr"]["vision"], None)

    def test_extract_stage_fails_visibly_on_bad_ocr_configuration(self):
        cases = ((("--ocr",), (None, None), "EXTRACT_OCR_UNAVAILABLE"),
                 (("--ocr", "--ocr-vision-config", str(self.dir / "absent.json")), ("t", "s"),
                  "EXTRACT_OCR_VISION_CONFIG_INVALID"),
                 (("--ocr-vision-config", str(self.config())), ("t", "s"), "STAGE_CONFIG_INVALID"))
        for extra, tools, expected in cases:
            with self.subTest(expected):
                (result, code), seen = self.stage(*extra, tools=tools)
                self.assertEqual((code, result["status"], result["error_code"]), (1, "fail", expected))
                self.assertEqual(seen, {}, "nothing ran")
                self.assertNotIn(self.temp.name, json.dumps(result))

    def test_pipeline_hands_the_vision_adapter_to_extraction(self):
        from campaign_tool.records import run as records_run
        with self.assertRaisesRegex(ValueError, "ocr_vision_requires_ocr_tools"):
            records_run.Pipeline(self.dir / "root", ocr_vision=VISION)
        pipeline = records_run.Pipeline(self.dir / "root", ocr_tools=ocr_fixtures.TOOLS, ocr_tool_signature="stub-v1",
                                        ocr_vision=VISION)
        self.assertEqual(pipeline.config["ocr_vision"], {"engine": "qwen", "model": VISION["model"],
                                                         "signature": VISION["signature"]})
        with mock.patch.object(pipeline, "_original", return_value={"storage_path": "/x"}), \
                mock.patch.object(pipeline, "_form", return_value="pdf"), \
                mock.patch.object(records_run.extraction_routes, "extract", side_effect=RuntimeError("stop")) as call:
            with self.assertRaises(RuntimeError):
                pipeline.stage_extract("a" * 64, None)
        self.assertEqual(call.call_args.kwargs["ocr_vision"], VISION)


from tests.records import test_native_extract as stage_fixtures  # noqa: E402 - after the classes it reuses


class Phase1HoldReleaseUnitTests(unittest.TestCase):
    def test_only_ocr_runtime_holds_are_released(self):
        from campaign_tool.records import native_extract as ne
        released = ({"ocr_runtime_unconfigured"}, {"ocr_runtime_unconfigured", "ocr_needed_or_blank_page"})
        kept = ({"ocr_needed_or_blank_page"}, {"ocr_runtime_unconfigured", "workbook_policy_refused"},
                {"ocr_runtime_unconfigured", "extraction_timeout"}, {"ocr_runtime_unconfigured", "decoder_needed"},
                {"ocr_runtime_unconfigured", "extraction_error"}, {"extraction_check_refused"},
                {"extraction_stage_error"}, {"decoder_unavailable_or_failed"})
        for codes in released:
            self.assertTrue(ne.ocr_released(codes), codes)
        for codes in kept:
            self.assertFalse(ne.ocr_released(codes), codes)


@unittest.skipUnless(stage_fixtures.HAVE_PARSERS and sys.platform.startswith("linux"),
                     "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
class Phase1ToOCRStageTests(stage_fixtures.ExtractStageCase):
    """A PDF held AWAITING_OCR by a native-only (Phase 1) run is extracted once OCR is enabled."""

    attachments = (("policy.pdf", stage_fixtures.tiny_pdf(stage_fixtures.PDF_TEXT)),
                   ("scan.pdf", stage_fixtures.scanned_pdf()),
                   ("broken.pdf", stage_fixtures.corrupt_pdf()))

    def setUp(self):
        super().setUp()
        self.vision_config = Path(self.temp.name) / "vision.json"
        self.vision_config.write_text(json.dumps({"schema": vision.CONFIG_SCHEMA, "argv": list(VISION["argv"]),
                                                  "engine": "qwen", "model": VISION["model"],
                                                  "signature": VISION["signature"]}))
        self.vision_config.chmod(0o600)
        self.scan = hashlib.sha256(self.payloads["scan.pdf"]).hexdigest()
        self.broken = hashlib.sha256(self.payloads["broken.pdf"]).hexdigest()

    def ocr_stage(self, runner, *extra):
        original = routes.extract

        def with_runner(*args, **kwargs):
            kwargs["ocr_runner"] = runner
            return original(*args, **kwargs)
        with mock.patch.object(routes, "extract", side_effect=with_runner), \
                mock.patch("campaign_tool.records.run.local_ocr_tools", return_value=(ocr_fixtures.TOOLS, "stub-v1")):
            return self.stage(*extra)

    def status(self, subject):
        return self.query("SELECT status FROM stage_state WHERE original_sha256=? AND stage='extract'", (subject,))[0][0]

    def alerts(self, subject):
        return sorted((key.rsplit(":", 1)[-1], state) for key, state in self.query(
            "SELECT key,state FROM alerts WHERE key LIKE ?", ("extract:" + subject + ":%",)))

    def test_phase1_awaiting_ocr_pdf_is_extracted_on_the_first_ocr_run_then_idempotent(self):
        runner = Runner(text="~ ,; i1 l\n", confidence="23")  # low confidence -> vision fallback
        # Run 1: native only (Phase 1). The scan is held AWAITING_OCR with its runtime alert.
        code, parsed, raw = self.ocr_stage(runner)
        self.assertContract(code, parsed, raw)
        # Three synthetic emails (each an original) plus policy.pdf extract natively.
        self.assertEqual(self.outcomes(parsed), {"EXTRACTED": 4, "AWAITING_OCR": 1, "CORRUPT_DOCUMENT": 1})
        self.assertEqual(runner.calls, [], "no OCR without --ocr")
        self.assertEqual(self.status(self.scan), "pending")
        [(codes, state)] = self.alerts(self.scan)
        self.assertIn("ocr_runtime_unconfigured", codes.split(","))
        self.assertEqual(state, "open")
        # Run 1b: still native only: the hold is unchanged (flags-off behavior).
        code, parsed, raw = self.ocr_stage(runner)
        self.assertEqual(self.outcomes(parsed), {"AWAITING_OCR": 1, "CORRUPT_DOCUMENT": 1})
        self.assertEqual(runner.calls, [])
        # Run 2: OCR + vision enabled. The scan is released and extracted; the corrupt hold stays.
        code, parsed, raw = self.ocr_stage(runner, "--ocr", "--ocr-vision-config", str(self.vision_config))
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.outcomes(parsed), {"EXTRACTED": 1, "CORRUPT_DOCUMENT": 1})
        self.assertEqual(runner.calls, ["pdftoppm", "tesseract", "qwen-vision"])
        self.assertEqual(self.status(self.scan), "done")
        self.assertEqual(self.alerts(self.scan), [(codes, "resolved")])
        self.assertEqual(self.status(self.broken), "pending")
        self.assertTrue(all(state == "open" for _, state in self.alerts(self.broken)))
        pages = [json.loads(row[0]) for row in self.query(
            "SELECT p.page_json FROM extraction_adapter_pages p JOIN extraction_adapter_imports i "
            "ON i.id=p.import_id WHERE i.original_sha256=? ORDER BY p.page_no", (self.scan,))]
        self.assertEqual([(page["page_no"], page["engine"]) for page in pages], [(1, "qwen")])
        self.assertTrue(pages[0]["vision_receipt_id"])
        imports = self.query("SELECT COUNT(*) FROM extraction_adapter_imports WHERE original_sha256=?", (self.scan,))
        queue = (self.processing / "awaiting-ocr.jsonl").read_text()
        self.assertEqual(queue, "", "nothing left awaiting OCR")
        # Run 3: same flags. Nothing pending for the scan: no tool calls, no second import.
        code, parsed, raw = self.ocr_stage(runner, "--ocr", "--ocr-vision-config", str(self.vision_config))
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.outcomes(parsed), {"CORRUPT_DOCUMENT": 1})
        self.assertEqual(runner.calls, ["pdftoppm", "tesseract", "qwen-vision"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM extraction_adapter_imports WHERE original_sha256=?",
                                    (self.scan,)), imports)
        self.assertEqual(self.alerts(self.scan), [(codes, "resolved")])

    def timed_ocr_stage(self, runner, document_timeout, *extra):
        """OCR stage run with the OCR plans' stage deadline, recording each extract call's timeout.

        Returns the timeouts of the pipeline's own parse calls (the ones that carry the OCR
        configuration) and, separately, of the installed-extraction validator's re-parses.
        """
        original_extract = routes.extract
        parses, reparses = [], []

        def with_runner(*args, **kwargs):
            (parses if kwargs.get("ocr_output_root") is not None else reparses).append(kwargs.get("timeout"))
            kwargs["ocr_runner"] = runner
            return original_extract(*args, **kwargs)
        with mock.patch.object(routes, "extract", side_effect=with_runner), \
                mock.patch("campaign_tool.records.run.local_ocr_tools", return_value=(ocr_fixtures.TOOLS, "stub-v1")):
            code, parsed, raw = self.stage("--max-documents", "20", "--document-timeout", document_timeout,
                                           "--ocr", *extra, timeout="1490")
        return code, parsed, raw, parses, reparses

    def test_document_timeout_bounds_every_ocr_child_on_the_ocr_path(self):
        """T6-plan D4: the live-ocr-vision limit (600 s) reaches the parser, Tesseract and vision."""
        runner = TimedRunner(text="~ ,; i1 l\n", confidence="23")  # low confidence -> vision fallback
        code, parsed, raw, parses, reparses = self.timed_ocr_stage(runner, "600", "--ocr-vision-config",
                                                                   str(self.vision_config))
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.status(self.scan), "done")
        self.assertEqual(runner.calls, ["pdftoppm", "tesseract", "qwen-vision"])
        self.assertEqual(runner.timeouts, [600, 600, 600])
        self.assertEqual(set(parses), {600})
        # The validator re-parses with its own fixed bounds, independent of --document-timeout.
        self.assertLessEqual(set(reparses), {20, 60})
        # FINDING (follow-up card, not fixed here): --document-timeout bounds each child command,
        # not the document. One image-only page already allows parse + pdftoppm + tesseract +
        # vision = 4 x 600 s, each further image-only page adds 3 x 600 s, plus the validator
        # re-parses. Only the stage deadline (--timeout / the host stage timeout) caps a document.

    def test_document_timeout_bounds_tesseract_only_ocr(self):
        """T6-plan D4: the live-ocr (Tesseract only) limit: 300 s, no vision call."""
        runner = TimedRunner()
        code, parsed, raw, parses, reparses = self.timed_ocr_stage(runner, "300")
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.status(self.scan), "done")
        self.assertEqual(runner.calls, ["pdftoppm", "tesseract"])
        self.assertEqual(runner.timeouts, [300, 300])
        self.assertEqual(set(parses), {300})
        self.assertLessEqual(set(reparses), {20, 60})


class TimedRunner(Runner):
    """Runner that also records the timeout each OCR or vision child was given."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.timeouts = []

    def __call__(self, command, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return super().__call__(command, **kwargs)


if __name__ == "__main__":
    unittest.main()
