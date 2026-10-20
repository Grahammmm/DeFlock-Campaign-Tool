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

from campaign_tool.records import ocr_stage as stage
from tests.records.ocr_stage_fixtures import (FakeTools, FakeVision, blank_page, native_text_pdf,
                                              noise_page, rasterize_gray, scanned_pdf)

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
        digest = self.ws.add("mixed.pdf", scanned_pdf([noise_page(3), noise_page(4)]))
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


if __name__ == "__main__":
    unittest.main()
