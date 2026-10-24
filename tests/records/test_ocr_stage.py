"""OCR stage (T6a): OCRmyPDF first, Qwen vision fallback, behind awaiting-ocr-v1.

Fake backends and synthetic scanned PDFs only. The real-tool smoke test runs
only where /usr/bin/ocrmypdf and /usr/bin/pdftoppm exist, and even then the
vision model is faked: no test talks to a real model.
"""
import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from campaign_tool.records import ocr_stage as stage
from tests.records.ocr_stage_fixtures import (FakeTools, FakeVision, blank_page, mixed_pdf, native_text_pdf,
                                              noise_page, ocrmypdf_sidecar, rasterize_gray, scanned_pdf)

CLEAN = "Synthetic records request response page with plenty of clearly legible words."
HANDWRITTEN_GARBAGE = "~ _- |; ., '' :"


class Workspace:
    def __init__(self, test):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr-stage-"))
        test.addCleanup(shutil.rmtree, self.tmp, True)
        self.sources = self.tmp / "sources"
        self.sources.mkdir()
        self.out = self.tmp / "out"
        self.queue = self.tmp / "awaiting-ocr.jsonl"
        self.lines = []

    def add(self, name, data, document_id=None):
        path = self.sources / name
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        self.lines.append(json.dumps({"document_id": document_id or "doc:" + name, "sha256": digest,
                                      "path": str(path)}))
        return digest

    def write_queue(self, extra=()):
        self.queue.write_text("\n".join(list(self.lines) + list(extra)) + "\n", encoding="utf-8")

    def args(self, config=None, max_documents=50):
        return argparse.Namespace(queue=str(self.queue), source_root=str(self.sources), out=str(self.out),
                                  config=config, max_documents=max_documents)

    def config(self, **values):
        path = self.tmp / "ocr-stage-config.json"
        path.write_text(json.dumps(values), encoding="utf-8")
        os.chmod(path, 0o600)
        return str(path)


def fake_ocrmypdf(tmp):
    """An executable placeholder so ``available()`` is true; FakeTools never runs it."""
    path = Path(tmp) / "bin" / "ocrmypdf"
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\nexit 99\n")
    os.chmod(path, 0o700)
    return str(path)


def results(out):
    return {path.parent.parent.name: json.loads(path.read_text()) for path in Path(out).glob("*/*/result.json")}


class AcceptanceTest(unittest.TestCase):
    """T6 acceptance shape: 10 clean PDFs -> no Qwen calls; 2 handwritten -> Qwen."""

    def setUp(self):
        self.ws = Workspace(self)
        self.sidecars = {}
        for index in range(10):
            digest = self.ws.add(f"clean-{index}.pdf", scanned_pdf([noise_page(index)]))
            self.sidecars[digest] = [CLEAN]
        self.handwritten = []
        for index in range(2):
            digest = self.ws.add(f"handwritten-{index}.pdf", scanned_pdf([noise_page(100 + index)]))
            self.sidecars[digest] = [HANDWRITTEN_GARBAGE if index else ""]
            self.handwritten.append(digest)
        self.ws.write_queue()
        self.config = self.ws.config(ocrmypdf=fake_ocrmypdf(self.ws.tmp),
                                     vision={"endpoint": "http://127.0.0.1:8003/v1/chat/completions"})

    def test_clean_pages_never_call_vision_and_handwritten_pages_do(self):
        tools, vision = FakeTools(self.sidecars), FakeVision()
        line, code, outcomes = stage.run(self.ws.args(self.config), runner=tools, transport=vision)
        self.assertEqual(code, 0)
        self.assertEqual(tools.count("ocrmypdf"), 12)
        self.assertEqual(len(vision.requests), 2)
        self.assertEqual(tools.count("pdftoppm"), 2)
        self.assertEqual(line["status"], "ok")
        self.assertEqual(line["counts"], {"input": 12, "output": 12, "failed": 0, "skipped": 0})
        self.assertEqual(line["new_data"], {"documents": 12, "pages": 12})
        self.assertEqual(line["review_required"], 12)
        self.assertEqual(line["needs_g"], [{"code": "OCR_VISION_PAGES", "severity": "info", "count": 2}])

        published = results(self.ws.out)
        self.assertEqual(len(published), 12)
        engines = sorted(page["engine"] for record in published.values() for page in record["pages"])
        self.assertEqual(engines, ["qwen"] * 2 + ["tesseract"] * 10)
        for digest in self.handwritten:
            page = published[digest]["pages"][0]
            self.assertEqual((page["engine"], page["method"], page["status"]), ("qwen", "llama.cpp-vision", "ocr_text"))
            self.assertIn(page["tesseract_reason"], ("ocr_empty", "ocr_low_quality"))
            directory = next((self.ws.out / digest).iterdir())
            self.assertEqual((directory / "page-0001.txt").read_text(), FakeVision().text)
        for record in published.values():
            self.assertEqual(record["review_state"], "not_reviewed")
            self.assertNotIn(CLEAN, json.dumps(record))  # result.json never carries text

        endpoint, request = vision.requests[0]
        self.assertEqual(endpoint, "http://127.0.0.1:8003/v1/chat/completions")
        image = request["messages"][1]["content"][1]["image_url"]["url"]
        self.assertTrue(image.startswith("data:image/png;base64,"))
        self.assertEqual(request["temperature"], 0)

    def test_rerun_is_idempotent_and_calls_nothing(self):
        stage.run(self.ws.args(self.config), runner=FakeTools(self.sidecars), transport=FakeVision())
        before = {path: path.stat().st_mtime_ns for path in self.ws.out.glob("*/*/*")}
        tools, vision = FakeTools(self.sidecars), FakeVision()
        line, code, outcomes = stage.run(self.ws.args(self.config), runner=tools, transport=vision)
        self.assertEqual((code, tools.calls, vision.requests), (0, [], []))
        self.assertEqual(line["counts"], {"input": 12, "output": 0, "failed": 0, "skipped": 12})
        self.assertEqual(line["status"], "ok")
        self.assertTrue(all(o.cached for o in outcomes))
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in self.ws.out.glob("*/*/*")})
        self.assertEqual(list((self.ws.out / ".work").iterdir()), [])

    def test_vision_server_down_is_transient_and_retried_next_run(self):
        down = FakeVision(error=stage.BackendError("vision_unavailable"))
        line, code, _ = stage.run(self.ws.args(self.config), runner=FakeTools(self.sidecars), transport=down)
        self.assertEqual((code, line["status"], line["error_code"]), (0, "warn", "OCR_BACKEND_TRANSIENT"))
        self.assertEqual(len(results(self.ws.out)), 10)  # handwritten docs are not cached
        up = FakeVision()
        line, _, _ = stage.run(self.ws.args(self.config), runner=FakeTools(self.sidecars), transport=up)
        self.assertEqual(len(up.requests), 2)
        self.assertEqual(line["counts"], {"input": 12, "output": 2, "failed": 0, "skipped": 10})
        self.assertEqual(line["status"], "ok")

    def test_without_vision_handwritten_pages_stay_unresolved(self):
        config = self.ws.config(ocrmypdf=fake_ocrmypdf(self.ws.tmp))
        tools = FakeTools(self.sidecars)
        line, code, outcomes = stage.run(self.ws.args(config), runner=tools, transport=FakeVision())
        self.assertEqual(tools.count("pdftoppm"), 0)
        self.assertEqual((line["status"], line["error_code"]), ("warn", "OCR_PAGES_UNRESOLVED"))
        self.assertIn({"code": "OCR_PAGES_UNRESOLVED", "severity": "action", "count": 2}, line["needs_g"])
        partial = [o for o in outcomes if o.status == "ocr_partial"]
        self.assertEqual(len(partial), 2)
        record = results(self.ws.out)[self.handwritten[0]]
        self.assertEqual((record["status"], record["pages"][0]["status"]), ("ocr_partial", "ocr_empty"))
        self.assertNotIn("text_file", record["pages"][0])

    def test_max_documents_leaves_backlog(self):
        line, _, outcomes = stage.run(self.ws.args(self.config, max_documents=5),
                                      runner=FakeTools(self.sidecars), transport=FakeVision())
        self.assertEqual(len(outcomes), 5)
        self.assertEqual(line["counts"], {"input": 12, "output": 5, "failed": 0, "skipped": 7})
        self.assertIn({"code": "OCR_BACKLOG", "severity": "info", "count": 7}, line["needs_g"])


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(self)
        self.config = self.ws.config(ocrmypdf=fake_ocrmypdf(self.ws.tmp),
                                     vision={"endpoint": "http://localhost:8003/v1/chat/completions"})

    def test_bad_sources_and_queue_lines_are_counted_not_fatal(self):
        good = scanned_pdf([noise_page(1)])
        self.ws.add("good.pdf", good)
        self.ws.lines.append(json.dumps({"document_id": "doc:mismatch", "sha256": "0" * 64,
                                         "path": str(self.ws.sources / "good.pdf")}))
        self.ws.lines.append(json.dumps({"document_id": "doc:missing", "sha256": "1" * 64,
                                         "path": str(self.ws.sources / "missing.pdf")}))
        os.symlink(self.ws.sources / "good.pdf", self.ws.sources / "link.pdf")
        self.ws.lines.append(json.dumps({"document_id": "doc:link", "sha256": hashlib.sha256(good).hexdigest(),
                                         "path": str(self.ws.sources / "link.pdf")}))
        self.ws.add("notpdf.pdf", b"PK\x03\x04 not a pdf")
        outside = self.ws.tmp / "outside.pdf"
        outside.write_bytes(good)
        self.ws.lines.append(json.dumps({"document_id": "doc:outside", "sha256": hashlib.sha256(good).hexdigest(),
                                         "path": str(outside)}))
        self.ws.write_queue(extra=["not json", json.dumps({"document_id": "../x", "sha256": "a" * 64, "path": "/x"}),
                                   json.dumps({"document_id": "d", "sha256": "a" * 64, "path": "/x", "extra": 1})])
        tools = FakeTools({hashlib.sha256(good).hexdigest(): [CLEAN]})
        line, code, outcomes = stage.run(self.ws.args(self.config), runner=tools, transport=FakeVision())
        self.assertEqual(code, 0)
        codes = sorted(o.code for o in outcomes if o.status == "source_invalid")
        self.assertEqual(codes, ["source_hash_mismatch", "source_missing", "source_not_pdf", "source_symlink"])
        self.assertEqual(tools.count("ocrmypdf"), 1)
        self.assertEqual(line["counts"], {"input": 9, "output": 1, "failed": 8, "skipped": 0})
        self.assertEqual((line["status"], line["error_code"]), ("warn", "OCR_DOCUMENT_FAILED"))
        needs = {item["code"]: item["count"] for item in line["needs_g"]}
        self.assertEqual(needs["OCR_SOURCE_INVALID"], 4)
        self.assertEqual(needs["OCR_QUEUE_ITEM_INVALID"], 4)  # 3 malformed + 1 outside root
        self.assertNotIn(str(self.ws.tmp), json.dumps(line))

    def test_ocrmypdf_exit_codes_and_timeout_map_to_fixed_codes(self):
        self.ws.add("enc.pdf", scanned_pdf([noise_page(2)]))
        self.ws.write_queue()
        _, _, outcomes = stage.run(self.ws.args(self.config), runner=FakeTools(exit_code=8), transport=FakeVision())
        self.assertEqual((outcomes[0].status, outcomes[0].code), ("ocr_failed", "ocrmypdf_encrypted"))
        line, _, outcomes = stage.run(self.ws.args(self.config), runner=FakeTools(raise_timeout=True),
                                      transport=FakeVision())
        self.assertEqual(outcomes[0].code, "ocrmypdf_timeout")
        self.assertIn({"code": "OCR_BACKEND_TRANSIENT", "severity": "action", "count": 1}, line["needs_g"])
        self.assertEqual(results(self.ws.out), {})  # failures are never cached

    def test_native_text_pages_are_left_to_native_extraction(self):
        # A skip marker only qualifies on an actual native-only page (X6).
        digest = self.ws.add("mixed.pdf", mixed_pdf([["Approved"], noise_page(4)]))
        self.ws.write_queue()
        vision = FakeVision()
        stage.run(self.ws.args(self.config), runner=FakeTools({digest: ["[OCR skipped on page(s) 1]", CLEAN]}),
                  transport=vision)
        pages = results(self.ws.out)[digest]["pages"]
        self.assertEqual([(p["status"], p["engine"]) for p in pages], [("native_text", None), ("ocr_text", "tesseract")])
        self.assertEqual(vision.requests, [])

    def test_invalid_vision_reply_is_unresolved_not_transient(self):
        digest = self.ws.add("hw.pdf", scanned_pdf([noise_page(5)]))
        self.ws.write_queue()
        line, _, outcomes = stage.run(self.ws.args(self.config), runner=FakeTools({digest: [""]}),
                                      transport=FakeVision(raw=b"<html>oops</html>"))
        self.assertEqual(outcomes[0].status, "ocr_partial")
        self.assertEqual(results(self.ws.out)[digest]["pages"][0]["vision_error"], "vision_response_invalid")
        self.assertEqual(line["error_code"], "OCR_PAGES_UNRESOLVED")

    def test_missing_ocrmypdf_fails_the_stage(self):
        self.ws.write_queue()
        config = self.ws.config(ocrmypdf=str(self.ws.tmp / "nope" / "ocrmypdf"))
        line, code, _ = stage.run(self.ws.args(config))
        self.assertEqual((code, line["status"], line["error_code"]), (1, "fail", "OCR_BACKEND_UNAVAILABLE"))

    def test_config_must_be_owner_only_and_known_keys(self):
        self.ws.write_queue()
        config = self.ws.config(ocrmypdf=fake_ocrmypdf(self.ws.tmp))
        os.chmod(config, 0o644)
        self.assertEqual(stage.run(self.ws.args(config))[0]["error_code"], "OCR_CONFIG_INVALID")
        config = self.ws.config(ocrmypdf=fake_ocrmypdf(self.ws.tmp), surprise=True)
        self.assertEqual(stage.run(self.ws.args(config))[0]["error_code"], "OCR_CONFIG_INVALID")
        config = self.ws.config(vision={"endpoint": "http://192.0.2.10:8003/v1/chat/completions"})
        self.assertEqual(stage.run(self.ws.args(config))[0]["error_code"], "OCR_CONFIG_INVALID")

    def test_concurrent_run_reports_busy(self):
        self.ws.write_queue()
        built = stage.build_stage(str(self.ws.out), {"ocrmypdf": fake_ocrmypdf(self.ws.tmp)})
        with built.locked():
            line, code, _ = stage.run(self.ws.args(self.config), runner=FakeTools(), transport=FakeVision())
        self.assertEqual((code, line["error_code"]), (0, "OCR_STAGE_BUSY"))


class SidecarTest(unittest.TestCase):
    """OCRmyPDF 14.0.1 sidecar shapes (t_4913f5d3 F1-F8): one result per PDF page."""

    def test_real_sidecar_shapes_split_into_one_string_per_page(self):
        m = stage.skipped_marker
        cases = {
            "F1 two scanned pages": ("page one\n\fpage two\n", ["page one\n", "page two\n"]),
            "F2 two native pages": ("[OCR skipped on page(s) 1-2]", [m(1), m(2)]),
            "F3 scan then native": ("page one\n\f[OCR skipped on page(s) 2]", ["page one\n", m(2)]),
            "F4 one blank page": ("", [""]),
            "F6 scan then native range": ("page one\n\f[OCR skipped on page(s) 2-3]", ["page one\n", m(2), m(3)]),
            "F7 two blank pages": ("\f", ["", ""]),
            "F8 text then blank back page": ("page one\n\f", ["page one\n", ""]),
            "range at start": ("[OCR skipped on page(s) 1-2]\fpage three", [m(1), m(2), "page three"]),
            "range in middle": ("p1\f[OCR skipped on page(s) 2-4]\fp5", ["p1", m(2), m(3), m(4), "p5"]),
            "three blank pages": ("\f\f", ["", "", ""]),
        }
        for name, (sidecar, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(stage.split_sidecar(sidecar), expected)

    def test_fixture_sidecar_matches_ocrmypdf_merge_sidecars(self):
        self.assertEqual(ocrmypdf_sidecar([None, None]), "[OCR skipped on page(s) 1-2]")
        self.assertEqual(ocrmypdf_sidecar(["a", None, None, "", None]),
                         "a\f[OCR skipped on page(s) 2-3]\f\f[OCR skipped on page(s) 5]")
        self.assertEqual(ocrmypdf_sidecar(["a", ""]), "a\f")
        self.assertEqual(stage.split_sidecar(ocrmypdf_sidecar(["a", None, None, "", None])),
                         ["a", stage.skipped_marker(2), stage.skipped_marker(3), "", stage.skipped_marker(5)])

    def test_markers_that_do_not_continue_the_page_count_fail_closed(self):
        for sidecar in ("[OCR skipped on page(s) 2]",          # names the wrong page
                        "p1\f[OCR skipped on page(s) 3-4]",     # skips page 2
                        "[OCR skipped on page(s) 2-1]",         # reversed range
                        "[OCR skipped on page(s) 1, 2]",        # marker-shaped, not a 14.x form
                        "[OCR skipped on page(s) 0]",
                        "[OCR skipped on page(s) 1-999999999]",  # beyond MAX_PAGES
                        "\f" * stage.MAX_PAGES):
            with self.subTest(sidecar[:40]), self.assertRaises(stage.BackendError) as caught:
                stage.split_sidecar(sidecar)
            self.assertEqual(caught.exception.code, "ocrmypdf_sidecar_inconsistent")

    def test_sidecar_parser_version_is_part_of_the_cache_signature(self):
        built = stage.build_stage("/nonexistent", {})
        with mock.patch.object(stage, "SIDECAR_PARSER", 1):
            old = stage.build_stage("/nonexistent", {})
        self.assertNotEqual(built.signature, old.signature)


class PageAttributionTest(unittest.TestCase):
    """Native ranges and blank pages keep every later page on its own number."""

    def setUp(self):
        self.ws = Workspace(self)
        self.config = self.ws.config(ocrmypdf=fake_ocrmypdf(self.ws.tmp),
                                     vision={"endpoint": "http://127.0.0.1:8003/v1/chat/completions"})

    def run_pages(self, sidecar_pages, page_count=None):
        page_count = len(sidecar_pages) if page_count is None else page_count
        digest = self.ws.add("doc.pdf", mixed_pdf([
            ["Approved"] if i < len(sidecar_pages) and sidecar_pages[i] is None else noise_page(40 + i)
            for i in range(page_count)]))
        self.ws.write_queue()
        tools, vision = FakeTools({digest: sidecar_pages}), FakeVision()
        line, code, outcomes = stage.run(self.ws.args(self.config), runner=tools, transport=vision)
        self.assertEqual(code, 0)
        rendered = [int(argv[argv.index("-f") + 1]) for argv in tools.calls if Path(argv[0]).name == "pdftoppm"]
        for argv in tools.calls:
            if Path(argv[0]).name == "pdftoppm":
                self.assertEqual(argv[argv.index("-f") + 1], argv[argv.index("-l") + 1])
        self.assertEqual(len(vision.requests), len(rendered))
        return digest, results(self.ws.out).get(digest), rendered, outcomes[0]

    def rows(self, record):
        self.assertEqual(record["page_count"], len(record["pages"]))
        return [(p["page"], p["status"], p["engine"]) for p in record["pages"]]

    def text_files(self, digest):
        return sorted(path.name for path in next((self.ws.out / digest).iterdir()).glob("page-*.txt"))

    def test_native_range_at_start_keeps_the_scanned_page_on_page_three(self):
        digest, record, rendered, _ = self.run_pages([None, None, ""])
        self.assertEqual(self.rows(record), [(1, "native_text", None), (2, "native_text", None),
                                             (3, "ocr_text", "qwen")])
        self.assertEqual(rendered, [3])  # the Qwen fallback renders page 3, not native page 2
        self.assertEqual(self.text_files(digest), ["page-0003.txt"])

    def test_native_range_in_the_middle(self):
        digest, record, rendered, _ = self.run_pages([CLEAN, None, None, HANDWRITTEN_GARBAGE])
        self.assertEqual(self.rows(record), [(1, "ocr_text", "tesseract"), (2, "native_text", None),
                                             (3, "native_text", None), (4, "ocr_text", "qwen")])
        self.assertEqual(rendered, [4])
        self.assertEqual(self.text_files(digest), ["page-0001.txt", "page-0004.txt"])

    def test_native_range_at_the_end_is_not_dropped(self):
        _, record, rendered, _ = self.run_pages(["", None, None])
        self.assertEqual(self.rows(record), [(1, "ocr_text", "qwen"), (2, "native_text", None),
                                             (3, "native_text", None)])
        self.assertEqual(rendered, [1])

    def test_fully_native_document_has_a_row_per_page(self):
        _, record, rendered, outcome = self.run_pages([None, None])
        self.assertEqual(self.rows(record), [(1, "native_text", None), (2, "native_text", None)])
        self.assertEqual((rendered, outcome.status), ([], "ocr_complete"))

    def test_trailing_blank_page_is_kept_and_sent_to_vision(self):
        digest, record, rendered, _ = self.run_pages([CLEAN, ""])
        self.assertEqual(self.rows(record), [(1, "ocr_text", "tesseract"), (2, "ocr_text", "qwen")])
        self.assertEqual(record["pages"][1]["tesseract_reason"], "ocr_empty")
        self.assertEqual(rendered, [2])
        self.assertEqual(self.text_files(digest), ["page-0001.txt", "page-0002.txt"])

    def test_two_blank_pages_are_two_pages(self):
        _, record, rendered, _ = self.run_pages(["", ""])
        self.assertEqual(self.rows(record), [(1, "ocr_text", "qwen"), (2, "ocr_text", "qwen")])
        self.assertEqual(rendered, [1, 2])

    def test_trailing_blank_page_without_vision_stays_an_unresolved_page(self):
        config = self.ws.config(ocrmypdf=fake_ocrmypdf(self.ws.tmp))
        digest = self.ws.add("doc.pdf", scanned_pdf([noise_page(60), noise_page(61)]))
        self.ws.write_queue()
        line, _, outcomes = stage.run(self.ws.args(config), runner=FakeTools({digest: [CLEAN, ""]}),
                                      transport=FakeVision())
        record = results(self.ws.out)[digest]
        self.assertEqual(self.rows(record), [(1, "ocr_text", "tesseract"), (2, "ocr_empty", "tesseract")])
        self.assertEqual((outcomes[0].status, outcomes[0].unresolved_pages), ("ocr_partial", 1))
        self.assertEqual(line["error_code"], "OCR_PAGES_UNRESOLVED")

    def test_inconsistent_sidecar_fails_the_document_before_any_vision_call(self):
        digest = self.ws.add("doc.pdf", scanned_pdf([noise_page(70), noise_page(71)]))
        self.ws.write_queue()
        tools, vision = FakeTools({digest: "p1\f[OCR skipped on page(s) 3]"}), FakeVision()
        line, _, outcomes = stage.run(self.ws.args(self.config), runner=tools, transport=vision)
        self.assertEqual((outcomes[0].status, outcomes[0].code), ("ocr_failed", "ocrmypdf_sidecar_inconsistent"))
        self.assertEqual((vision.requests, tools.count("pdftoppm"), results(self.ws.out)), ([], 0, {}))
        self.assertEqual(line["error_code"], "OCR_DOCUMENT_FAILED")

    def test_backend_returning_an_unexpanded_range_fails_closed(self):
        class Unexpanded:
            engine, method = "tesseract", "ocrmypdf"

            def identity(self):
                return {"engine": self.engine}

            def pages(self, source, workdir):
                return ["[OCR skipped on page(s) 1-2]", ""]

        digest = self.ws.add("doc.pdf", scanned_pdf([noise_page(80), noise_page(81), noise_page(82)]))
        self.ws.write_queue()
        calls = []

        class Fallback:
            engine, method = "qwen", "llama.cpp-vision"

            def identity(self):
                return {"engine": self.engine}

            def page_text(self, source, page, workdir):
                calls.append(page)
                return "text"

        built = stage.OcrStage(self.ws.out, Unexpanded(), Fallback())
        item = stage.parse_queue_line(self.ws.lines[0], self.ws.sources)
        outcome = built.process(item)
        self.assertEqual((outcome.status, outcome.code), ("ocr_failed", "ocrmypdf_sidecar_inconsistent"))
        self.assertEqual(calls, [])
        self.assertFalse((self.ws.out / digest).exists())


class BackendUnitTest(unittest.TestCase):
    def test_image_tool_paths_are_the_pinned_debian_packages(self):
        self.assertEqual(stage.DEFAULT_OCRMYPDF, "/usr/bin/ocrmypdf")
        self.assertEqual(stage.DEFAULT_PDFTOPPM, "/usr/bin/pdftoppm")
        backend = stage.OcrmypdfBackend()
        argv = backend.argv(Path("/w/source.pdf"), Path("/w/sidecar.txt"), Path("/w/ocr.pdf"))
        self.assertEqual(argv[0], "/usr/bin/ocrmypdf")
        for flag in ("--skip-text", "--sidecar", "--output-type"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[-2:], ["/w/source.pdf", "/w/ocr.pdf"])

    def test_run_group_kills_the_whole_tool_session_on_timeout(self):
        import subprocess
        import time
        tmp = Path(tempfile.mkdtemp(prefix="ocr-group-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        pidfile = tmp / "grandchild.pid"
        script = tmp / "tool.sh"
        script.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pidfile}\nwait\n")
        os.chmod(script, 0o700)
        with self.assertRaises(subprocess.TimeoutExpired):
            stage.run_group([str(script)], timeout=1, stdout=subprocess.DEVNULL)
        grandchild = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{grandchild}/stat").read_text().split(")")[-1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                break
            if state == "Z":
                break
            time.sleep(0.05)
        else:
            self.fail("grandchild OCR process outlived the stage")

    def test_vision_endpoint_must_be_loopback(self):
        for good in ("http://127.0.0.1:8003/v1/chat/completions", "http://[::1]:8003/x", "http://localhost:1/x"):
            stage.loopback_endpoint(good)
        for bad in ("https://127.0.0.1:8003/x", "http://10.0.0.5:8003/x", "http://127.0.0.1/x",
                    "http://u:p@127.0.0.1:8003/x", "http://127.0.0.1:8003/x?k=v", "http://example.org:8003/x"):
            with self.assertRaises(ValueError, msg=bad):
                stage.loopback_endpoint(bad)

    def test_quality_triage(self):
        quality = stage.Quality()
        self.assertIsNone(quality.check(CLEAN))
        self.assertEqual(quality.check(" \n\t"), "ocr_empty")
        self.assertEqual(quality.check(HANDWRITTEN_GARBAGE), "ocr_low_quality")
        self.assertEqual(quality.check("ab cd"), "ocr_low_quality")

    def test_reply_parsing_drops_inline_reasoning(self):
        raw = json.dumps({"choices": [{"message": {"content": "<think>hmm</think>\nLine one\nLine two"}}]})
        self.assertEqual(stage.QwenVisionBackend.parse(raw.encode()), "Line one\nLine two")
        with self.assertRaises(stage.BackendError):
            stage.QwenVisionBackend.parse(b'{"choices": []}')

    def test_real_urllib_transport_against_a_fake_llama_server(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                body = json.dumps({"choices": [{"message": {"content": "served text"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        backend = stage.QwenVisionBackend(endpoint=endpoint)
        reply = stage.urllib_transport(endpoint, backend.request_body(b"\x89PNG\r\n\x1a\nxx"), 10)
        self.assertEqual(backend.parse(reply), "served text")
        self.assertEqual(seen[0][0], "/v1/chat/completions")
        self.assertEqual(seen[0][1]["model"], stage.DEFAULT_VISION_MODEL)
        server.shutdown()
        with self.assertRaises(stage.BackendError) as caught:
            stage.urllib_transport("http://127.0.0.1:9/x", b"{}", 2)
        self.assertEqual(caught.exception.code, "vision_unavailable")


class TranscribePngAdapterTest(unittest.TestCase):
    """The WP4 ``extract.vision`` command adapter is the same Qwen client."""

    def setUp(self):
        replies = self.replies = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                replies.append(request)
                body = json.dumps({"choices": [{"message": {"content": "<think>x</think>Adapter text"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.ws = Workspace(self)
        self.png = self.ws.tmp / "page.png"
        self.png.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")

    def argv(self, config):
        import sys
        return [sys.executable, "-B", "-m", "campaign_tool.records.ocr_stage", "transcribe-png", "--config", config]

    def test_adapter_prints_only_the_transcription(self):
        import subprocess
        from campaign_tool.records.extract import vision
        config = self.ws.config(vision={"endpoint": f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions"})
        argv = self.argv(config)
        vision.adapter(argv, "qwen", stage.DEFAULT_VISION_MODEL, "ocr-stage-v1")  # accepted by WP4's validator
        root = str(Path(stage.__file__).resolve().parents[2])
        done = subprocess.run(argv + [str(self.png)], capture_output=True, timeout=60, cwd=root)
        self.assertEqual((done.returncode, done.stdout), (0, b"Adapter text"))
        self.assertTrue(self.replies[0]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png"))

    def test_adapter_failures_exit_nonzero_with_empty_stdout(self):
        import contextlib
        import io
        config = self.ws.config(vision={"endpoint": "http://127.0.0.1:9/v1/chat/completions"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(stage.main(["transcribe-png", "--config", config, str(self.png)]), 1)
            not_png = self.ws.tmp / "x.png"
            not_png.write_bytes(b"GIF89a")
            self.assertEqual(stage.main(["transcribe-png", "--config", config, str(not_png)]), 1)
            no_vision = self.ws.config()  # same path, rewritten without a vision block
            self.assertEqual(stage.main(["transcribe-png", "--config", no_vision, str(self.png)]), 2)
        self.assertEqual(out.getvalue(), "")


class CacheAndContainmentTest(unittest.TestCase):
    """Sol review of #152 at 4e1118d: capped drain, cached warnings, ancestor symlinks, damaged cache."""

    def setUp(self):
        self.ws = Workspace(self)
        self.config = self.ws.config(ocrmypdf=fake_ocrmypdf(self.ws.tmp))
        self.sidecars = {}

    def add(self, name, text=CLEAN, seed=0):
        data = scanned_pdf([noise_page(seed)])
        digest = self.ws.add(name, data)
        self.sidecars[digest] = [text]
        return digest

    def run_stage(self, max_documents=50):
        tools = FakeTools(self.sidecars)
        line, code, outcomes = stage.run(self.ws.args(self.config, max_documents=max_documents),
                                         runner=tools, transport=FakeVision())
        return line, code, outcomes, tools

    # 1. cache hits must not consume --max-documents
    def test_capped_runs_drain_a_stable_queue_without_reprocessing(self):
        for index in range(3):
            self.add(f"doc-{index}.pdf", seed=index)
        self.ws.write_queue()
        backlog = []
        for run in range(3):
            line, _, outcomes, tools = self.run_stage(max_documents=1)
            self.assertEqual(tools.count("ocrmypdf"), 1, f"run {run}")
            self.assertEqual(line["counts"], {"input": 3, "output": 1, "failed": 0, "skipped": 2})
            self.assertEqual(sum(o.cached for o in outcomes), run)
            backlog.append({i["code"]: i["count"] for i in line.get("needs_g", [])}.get("OCR_BACKLOG", 0))
        self.assertEqual(backlog, [2, 1, 0])
        self.assertEqual(len(results(self.ws.out)), 3)
        line, _, outcomes, tools = self.run_stage(max_documents=1)
        self.assertEqual((tools.calls, line["status"]), ([], "ok"))
        self.assertEqual(line["counts"], {"input": 3, "output": 0, "failed": 0, "skipped": 3})
        self.assertTrue(all(o.cached for o in outcomes))

    def test_failed_attempts_count_against_the_cap(self):
        self.add("a.pdf", seed=1)
        self.add("b.pdf", seed=2)
        self.ws.write_queue()
        tools = FakeTools(exit_code=8)
        line, _, outcomes = stage.run(self.ws.args(self.config, max_documents=1), runner=tools,
                                      transport=FakeVision())
        self.assertEqual((tools.count("ocrmypdf"), len(outcomes)), (1, 1))
        self.assertIn({"code": "OCR_BACKLOG", "severity": "info", "count": 1}, line["needs_g"])

    # 2. cached partial documents keep warning
    def test_cached_unresolved_pages_keep_warning_without_new_output(self):
        self.add("blank.pdf", text="", seed=3)
        self.add("clean.pdf", seed=4)
        self.ws.write_queue()
        first, _, _, _ = self.run_stage()
        self.assertEqual((first["status"], first["error_code"]), ("warn", "OCR_PAGES_UNRESOLVED"))
        for _ in range(2):
            again, _, outcomes, tools = self.run_stage()
            self.assertEqual(tools.calls, [])
            self.assertEqual((again["status"], again["error_code"]), ("warn", "OCR_PAGES_UNRESOLVED"))
            self.assertIn({"code": "OCR_PAGES_UNRESOLVED", "severity": "action", "count": 1}, again["needs_g"])
            self.assertEqual(again["counts"], {"input": 2, "output": 0, "failed": 0, "skipped": 2})
            self.assertNotIn("new_data", again)
            self.assertNotIn("review_required", again)
            partial = [o for o in outcomes if o.unresolved_pages]
            self.assertEqual([(o.status, o.code) for o in partial], [("cached", "ocr_pages_unresolved")])

    # 3. containment through every path component
    def test_symlinked_ancestor_directory_cannot_escape_the_source_root(self):
        outside = self.ws.tmp / "outside"
        outside.mkdir()
        data = scanned_pdf([noise_page(5)])
        (outside / "doc.pdf").write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        self.sidecars[digest] = [CLEAN]
        os.symlink(outside, self.ws.sources / "linked_dir")
        self.ws.lines.append(json.dumps({"document_id": "doc:escape", "sha256": digest,
                                         "path": str(self.ws.sources / "linked_dir" / "doc.pdf")}))
        self.ws.write_queue()
        line, _, outcomes, tools = self.run_stage()
        self.assertEqual([(o.status, o.code) for o in outcomes], [("source_invalid", "source_symlink")])
        self.assertEqual(tools.calls, [])  # external bytes never reach a backend
        self.assertEqual(results(self.ws.out), {})
        self.assertEqual((line["status"], line["error_code"]), ("warn", "OCR_DOCUMENT_FAILED"))

    def test_cached_source_check_also_refuses_a_swapped_in_ancestor_symlink(self):
        sub = self.ws.sources / "sub"
        sub.mkdir()
        data = scanned_pdf([noise_page(6)])
        (sub / "doc.pdf").write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        self.sidecars[digest] = [CLEAN]
        self.ws.lines.append(json.dumps({"document_id": "doc:sub", "sha256": digest, "path": str(sub / "doc.pdf")}))
        self.ws.write_queue()
        self.assertEqual(self.run_stage()[2][0].status, "ocr_complete")
        moved = self.ws.tmp / "moved"
        os.rename(sub, moved)
        os.symlink(moved, sub)
        _, _, outcomes, tools = self.run_stage()
        self.assertEqual([(o.status, o.code) for o in outcomes], [("source_invalid", "source_symlink")])
        self.assertEqual(tools.calls, [])

    def test_unnormalized_paths_and_dot_dot_are_rejected(self):
        self.add("doc.pdf", seed=7)
        digest = next(iter(self.sidecars))
        for path in (str(self.ws.sources / "x" / ".." / "doc.pdf"), str(self.ws.sources) + "/./doc.pdf",
                     str(self.ws.sources) + "//doc.pdf", str(self.ws.sources / "doc.pdf") + "/"):
            self.ws.lines.append(json.dumps({"document_id": "doc:odd", "sha256": digest, "path": path}))
        self.ws.write_queue()
        line, _, outcomes, _ = self.run_stage()
        self.assertEqual(len(outcomes), 1)
        self.assertIn({"code": "OCR_QUEUE_ITEM_INVALID", "severity": "action", "count": 4}, line["needs_g"])

    def test_source_swapped_after_publication_is_not_a_cache_hit(self):
        self.add("doc.pdf", seed=8)
        self.ws.write_queue()
        self.run_stage()
        (self.ws.sources / "doc.pdf").write_bytes(b"%PDF-1.4\nreplaced\n")
        _, _, outcomes, tools = self.run_stage()
        self.assertEqual([(o.status, o.code) for o in outcomes], [("source_invalid", "source_hash_mismatch")])
        self.assertEqual(tools.calls, [])

    # 4. cache hits are verified
    def published_dir(self, digest):
        return next((self.ws.out / digest).iterdir())

    def assert_damaged(self, digest, other):
        line, code, outcomes, tools = self.run_stage()
        by_id = {o.document_id: o for o in outcomes}
        self.assertEqual((by_id["doc:target.pdf"].status, by_id["doc:target.pdf"].code),
                         ("cache_invalid", "cache_damaged"))
        self.assertTrue(by_id["doc:target.pdf"].cached is False)
        self.assertEqual(by_id["doc:other.pdf"].status, "ocr_complete")  # the queue continues
        self.assertEqual(tools.count("ocrmypdf"), 1)  # only `other`; the damaged doc is not redone
        self.assertEqual(code, 0)
        self.assertEqual((line["status"], line["error_code"]), ("warn", "OCR_DOCUMENT_FAILED"))
        self.assertIn({"code": "OCR_CACHE_DAMAGED", "severity": "action", "count": 1}, line["needs_g"])
        self.assertTrue(self.published_dir(digest).exists())  # left in place for the operator

    def damage_case(self, damage):
        digest = self.add("target.pdf", seed=9)
        self.ws.write_queue()
        self.run_stage()
        directory = self.published_dir(digest)
        damage(directory)
        self.add("other.pdf", seed=10)
        self.ws.write_queue()
        self.assert_damaged(digest, "other")

    def test_missing_page_text_is_damage(self):
        self.damage_case(lambda d: (d / "page-0001.txt").unlink())

    def test_tampered_page_text_is_damage(self):
        def tamper(directory):
            path = directory / "page-0001.txt"
            text = path.read_text()
            path.write_text(text[:-1] + ("X" if text[-1] != "X" else "Y"))  # same length, new hash
        self.damage_case(tamper)

    def test_symlinked_page_text_is_damage(self):
        def link(directory):
            elsewhere = self.ws.tmp / "elsewhere.txt"
            os.rename(directory / "page-0001.txt", elsewhere)
            os.symlink(elsewhere, directory / "page-0001.txt")
        self.damage_case(link)

    def test_symlinked_result_json_is_damage(self):
        def link(directory):
            elsewhere = self.ws.tmp / "elsewhere.json"
            os.rename(directory / "result.json", elsewhere)
            os.symlink(elsewhere, directory / "result.json")
        self.damage_case(link)

    def test_missing_result_json_is_damage(self):
        self.damage_case(lambda d: (d / "result.json").unlink())

    def test_malformed_manifests_are_damage_not_a_crash(self):
        bad_records = [b"not json", b"[]", b'{"schema": "ocr-stage-v1"}']

        def rewrite(record):
            def damage(directory):
                path = directory / "result.json"
                original = json.loads(path.read_text())
                path.write_bytes(record(original))
            return damage

        mutations = [lambda r, bad=bad: bad for bad in bad_records] + [
            lambda r: json.dumps(dict(r, pages=[1])).encode(),
            lambda r: json.dumps(dict(r, pages=[dict(r["pages"][0], page="1")])).encode(),
            lambda r: json.dumps(dict(r, unresolved_pages=0, status="ocr_complete",
                                      pages=[dict(r["pages"][0], text_file="../../x")])).encode(),
            lambda r: json.dumps(dict(r, unresolved_pages=3)).encode(),
            lambda r: json.dumps(dict(r, source_sha256="0" * 64)).encode(),
            lambda r: json.dumps(dict(r, pages=[dict(r["pages"][0], engine="other")])).encode(),
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                self.ws = Workspace(self)
                self.config = self.ws.config(ocrmypdf=fake_ocrmypdf(self.ws.tmp))
                self.sidecars = {}
                self.damage_case(rewrite(mutation))

    def test_group_or_world_readable_cache_entries_are_damage(self):
        self.damage_case(lambda d: os.chmod(d / "page-0001.txt", 0o644))

    def test_symlinked_result_directory_is_damage(self):
        def link(directory):
            elsewhere = self.ws.tmp / "elsewhere-dir"
            os.rename(directory, elsewhere)
            os.symlink(elsewhere, directory)
        digest = self.add("target.pdf", seed=11)
        self.ws.write_queue()
        self.run_stage()
        directory = self.published_dir(digest)
        link(directory)
        self.add("other.pdf", seed=12)
        self.ws.write_queue()
        self.assert_damaged(digest, "other")


@unittest.skipUnless(os.access(stage.DEFAULT_OCRMYPDF, os.X_OK) and os.access(stage.DEFAULT_PDFTOPPM, os.X_OK),
                     "real ocrmypdf/pdftoppm not installed")
class RealToolSmokeTest(unittest.TestCase):
    """Real OCRmyPDF + pdftoppm on synthetic scans; the vision model is still faked."""

    def test_scanned_text_page_reads_with_tesseract_and_blank_page_goes_to_vision(self):
        ws = Workspace(self)
        typed = ws.tmp / "typed.pdf"
        typed.write_bytes(native_text_pdf(["SYNTHETIC PUBLIC RECORDS RESPONSE", "Camera retention policy 30 days"]))
        width, height, gray = rasterize_gray(stage.DEFAULT_PDFTOPPM, typed, ws.tmp)
        scan = ws.add("scan.pdf", scanned_pdf([(width, height, gray)]))
        blank = ws.add("blank.pdf", scanned_pdf([blank_page(400, 520)]))
        born_digital = ws.add("typed.pdf", typed.read_bytes())
        ws.write_queue()
        config = ws.config(vision={"endpoint": "http://127.0.0.1:8003/v1/chat/completions"})
        vision = FakeVision()
        line, code, outcomes = stage.run(ws.args(config), transport=vision)
        self.assertEqual(code, 0, line)
        published = results(ws.out)
        page = published[scan]["pages"][0]
        self.assertEqual((page["engine"], page["status"]), ("tesseract", "ocr_text"), page)
        text = next((ws.out / scan).iterdir()).joinpath("page-0001.txt").read_text().upper()
        self.assertIn("RETENTION", text)
        self.assertEqual(published[blank]["pages"][0]["engine"], "qwen")
        # A born-digital page is skipped by --skip-text and left to native extraction.
        self.assertEqual(published[born_digital]["pages"][0]["status"], "native_text")
        self.assertEqual(len(vision.requests), 1)
        image = vision.requests[0][1]["messages"][1]["content"][1]["image_url"]["url"]
        self.assertTrue(image.startswith("data:image/png;base64,iVBOR"))  # a real PNG render

    def test_native_ranges_and_blank_pages_keep_one_row_per_real_page(self):
        """t_4913f5d3 F2/F6/F7/F8 shapes plus native-before-scan, through real OCRmyPDF."""
        ws = Workspace(self)
        typed = ws.tmp / "typed.pdf"
        typed.write_bytes(native_text_pdf(["SYNTHETIC PUBLIC RECORDS RESPONSE", "Camera retention policy 30 days"]))
        scan = rasterize_gray(stage.DEFAULT_PDFTOPPM, typed, ws.tmp)
        native = ["SYNTHETIC BORN DIGITAL PAGE", "Native text layer"]
        blank = blank_page(400, 520)
        docs = {
            "f2": ([native, native], ["native_text", "native_text"], []),
            "f6": ([scan, native, native], ["ocr_text", "native_text", "native_text"], []),
            "f7": ([blank, blank], ["ocr_text", "ocr_text"], [1, 2]),
            "f8": ([scan, blank], ["ocr_text", "ocr_text"], [2]),
            "native-first": ([native, native, scan, blank], ["native_text", "native_text", "ocr_text", "ocr_text"], [4]),
        }
        digests = {name: ws.add(name + ".pdf", mixed_pdf(pages)) for name, (pages, _, _) in docs.items()}
        ws.write_queue()
        calls = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return stage.run_group(argv, **kwargs)

        config = ws.config(vision={"endpoint": "http://127.0.0.1:8003/v1/chat/completions"})
        vision = FakeVision()
        line, code, outcomes = stage.run(ws.args(config), runner=runner, transport=vision)
        self.assertEqual(code, 0, line)
        published = results(ws.out)
        for name, (pages, statuses, vision_pages) in docs.items():
            with self.subTest(name):
                record = published[digests[name]]
                self.assertEqual(record["page_count"], len(pages))
                self.assertEqual([p["page"] for p in record["pages"]], list(range(1, len(pages) + 1)))
                self.assertEqual([p["status"] for p in record["pages"]], statuses)
                self.assertEqual([p["page"] for p in record["pages"] if p["engine"] == "qwen"], vision_pages)
        record = published[digests["native-first"]]
        self.assertEqual(record["pages"][2]["engine"], "tesseract")
        text = next((ws.out / digests["native-first"]).iterdir()).joinpath("page-0003.txt").read_text().upper()
        self.assertIn("RETENTION", text)  # the scanned page's text lands on page 3, not page 2
        rendered = sorted(int(argv[argv.index("-f") + 1]) for argv in calls if Path(argv[0]).name == "pdftoppm")
        self.assertEqual(rendered, sorted(sum((v for _, _, v in docs.values()), [])))
        self.assertEqual(len(vision.requests), len(rendered))

    def test_cli_prints_one_stage_result_line_and_a_private_report(self):
        import contextlib
        import io
        ws = Workspace(self)
        ws.add("blank.pdf", scanned_pdf([blank_page(200, 260)]))
        ws.write_queue()
        report = ws.tmp / "report.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = stage.main(["--queue", str(ws.queue), "--source-root", str(ws.sources),
                               "--out", str(ws.out), "--report", str(report)])
        lines = stdout.getvalue().splitlines()
        self.assertEqual((code, len(lines)), (0, 1))
        line = json.loads(lines[0])
        self.assertEqual(set(line) - {"needs_g", "new_data", "review_required"},
                         {"stage_result_version", "status", "counts", "error_code"})
        self.assertEqual((line["status"], line["error_code"]), ("warn", "OCR_PAGES_UNRESOLVED"))
        self.assertNotIn(str(ws.tmp), lines[0])
        self.assertEqual(json.loads(report.read_text())[0]["status"], "ocr_partial")
        self.assertEqual(report.stat().st_mode & 0o077, 0)


class HostGatewayTest(unittest.TestCase):
    """T6-NET A: ``vision.host_gateway`` admits one private IPv4 beside loopback (owner-only config)."""

    GATEWAY = "10.0.2.2"
    ENDPOINT = "http://10.0.2.2:8003/v1/chat/completions"

    def setUp(self):
        self.ws = Workspace(self)
        self.ocrmypdf = str(fake_ocrmypdf(self.ws.tmp))

    def config(self, **vision):
        return self.ws.config(ocrmypdf=self.ocrmypdf, vision=vision)

    def test_gateway_endpoint_accepted_and_identity_carries_it(self):
        self.assertEqual(stage.loopback_endpoint(self.ENDPOINT, frozenset({self.GATEWAY})), self.ENDPOINT)
        backend = stage.QwenVisionBackend(self.ENDPOINT, host_gateway=self.GATEWAY)
        self.assertEqual(backend.identity()["endpoint"], self.ENDPOINT)
        config = stage.load_config(self.config(endpoint=self.ENDPOINT, host_gateway=self.GATEWAY))
        self.assertEqual(stage.vision_backend(config).identity()["endpoint"], self.ENDPOINT)

    def test_default_stays_loopback_only(self):
        self.assertEqual(stage.LOOPBACK_HOSTS, frozenset({"127.0.0.1", "::1", "localhost"}))
        self.assertEqual(stage.DEFAULT_VISION_ENDPOINT, "http://127.0.0.1:8003/v1/chat/completions")
        with self.assertRaises(ValueError):
            stage.loopback_endpoint(self.ENDPOINT)
        with self.assertRaises(ValueError):
            stage.QwenVisionBackend(self.ENDPOINT)
        ws_config = self.config(endpoint=self.ENDPOINT)
        self.ws.write_queue()
        self.assertEqual(stage.run(self.ws.args(ws_config))[0]["error_code"], "OCR_CONFIG_INVALID")

    def test_endpoint_host_must_equal_the_gateway(self):
        with self.assertRaises(ValueError):
            stage.loopback_endpoint("http://10.0.2.3:8003/v1/chat/completions", frozenset({self.GATEWAY}))
        config = self.config(endpoint="http://10.0.2.3:8003/v1/chat/completions", host_gateway=self.GATEWAY)
        self.ws.write_queue()
        self.assertEqual(stage.run(self.ws.args(config))[0]["error_code"], "OCR_CONFIG_INVALID")

    def test_endpoint_shape_checks_still_apply_to_the_gateway(self):
        hosts = frozenset({self.GATEWAY})
        for endpoint in ("https://10.0.2.2:8003/v1/chat/completions",
                         "http://10.0.2.2/v1/chat/completions",
                         "http://{}@10.0.2.2:8003/v1/chat/completions".format("user:pw"),  # userinfo built at runtime
                         "http://10.0.2.2:8003/v1/chat/completions?x=1",
                         "http://10.0.2.2:8003/v1/chat/completions#f"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                stage.loopback_endpoint(endpoint, hosts)
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                stage.QwenVisionBackend(endpoint, host_gateway=self.GATEWAY)

    def test_gateway_values_other_than_one_private_ipv4_rejected(self):
        for value in ("gateway.example", "localhost", "127.0.0.1", "8.8.8.8", "169.254.1.1",
                      "224.0.0.1", "0.0.0.0", "240.0.0.1", "255.255.255.255", "::1", "fe80::1",
                      "fd00::2", "10.0.2.2/32", "010.0.2.2", " 10.0.2.2", "", 167772674,
                      ["10.0.2.2"], None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    stage.checked_host_gateway(value)
                config = self.config(endpoint=self.ENDPOINT, host_gateway=value)
                with self.assertRaises(ValueError):
                    stage.load_config(config)
                self.ws.write_queue()
                self.assertEqual(stage.run(self.ws.args(config))[0]["error_code"], "OCR_CONFIG_INVALID")
        for value in ("10.0.2.2", "172.17.0.1", "192.168.1.1"):
            with self.subTest(value=value):
                self.assertEqual(stage.checked_host_gateway(value), value)

    def test_gateway_is_used_only_by_the_transport_with_the_configured_endpoint(self):
        vision = FakeVision()
        config = stage.load_config(self.config(endpoint=self.ENDPOINT, host_gateway=self.GATEWAY))
        backend = stage.vision_backend(config, transport=vision)
        backend.transport(backend.endpoint, backend.request_body(b"\x89PNG\r\n\x1a\nsynthetic"), backend.timeout)
        self.assertEqual([endpoint for endpoint, _ in vision.requests], [self.ENDPOINT])

    def test_transcribe_png_honours_the_gateway(self):
        import contextlib
        import io
        png = self.ws.tmp / "page.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        vision = FakeVision(text="Gateway text")
        config = self.config(endpoint=self.ENDPOINT, host_gateway=self.GATEWAY)
        out = io.BytesIO()
        with contextlib.redirect_stdout(io.TextIOWrapper(out, encoding="utf-8")) as wrapper:
            code = stage.transcribe_png_main(["--config", config, str(png)], transport=vision)
            wrapper.flush()
        self.assertEqual((code, out.getvalue()), (0, b"Gateway text"))
        self.assertEqual(vision.requests[0][0], self.ENDPOINT)
        without = self.config(endpoint=self.ENDPOINT)
        self.assertEqual(stage.transcribe_png_main(["--config", without, str(png)], transport=vision), 1)
        self.assertEqual(len(vision.requests), 1)


if __name__ == "__main__":
    unittest.main()
