"""Adversarial reproductions for campaign_tool/records/ocr_stage.py (2026-10-22).

Findings already in docs/audits/2026-10-22-deflock-opus-audit.md (H2, M9, M10, L3) are not repeated.
Run: ADVERSARIAL_REPRO=1 python3 -B -m unittest discover -s tests/adversarial_repro -t . -v
"""
import argparse
import errno
import http.client
import json
import os
from pathlib import Path
import random
import socket
import threading
import unittest
from unittest import mock

from campaign_tool.records import ocr_stage as stage
from tests.adversarial_repro._support import call, requires_repro
from tests.records.ocr_stage_fixtures import (FakeTools, FakeVision, blank_page, noise_page,
                                              mixed_pdf, ocrmypdf_sidecar, scanned_pdf)
from tests.records.test_ocr_stage import CLEAN, Workspace, fake_ocrmypdf


def reply(text):
    """A well-formed OpenAI-style reply whose content is ``text`` (JSON-escaped by json.dumps)."""
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}]}).encode()


def vision_config(ws, **vision):
    return ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp),
                     vision={"endpoint": "http://127.0.0.1:8003/v1/chat/completions", **vision})


@requires_repro
class PoisonedVisionTextAbortsStage(unittest.TestCase):
    """A1: vision text with a lone surrogate crashes publication and blocks every later document."""

    def test_lone_surrogate_reply_does_not_abort_the_run(self):
        ws = Workspace(self)
        bad = ws.add("a-bad.pdf", scanned_pdf([noise_page(1)]))
        good = ws.add("b-good.pdf", scanned_pdf([noise_page(2)]))
        ws.write_queue()
        tools = FakeTools({bad: [""], good: [CLEAN]})
        # JSON "\ud800" is accepted by json.loads and yields a str that cannot be UTF-8 encoded.
        raw = b'{"choices":[{"message":{"content":"\\ud800 page text that is long enough"}}]}'
        line, code, outcomes = call(self, stage.run, ws.args(vision_config(ws)), tools, FakeVision(raw=raw))
        self.assertNotEqual(line.get("error_code"), "STAGE_INTERNAL_ERROR")
        self.assertIn(good, {p.parent.parent.name for p in Path(ws.out).glob("*/*/result.json")},
                      "the healthy document behind the poisoned one was never processed")

    def test_failed_publish_leaves_no_staging_directory(self):
        ws = Workspace(self)
        bad = ws.add("a-bad.pdf", scanned_pdf([noise_page(1)]))
        ws.write_queue()
        raw = b'{"choices":[{"message":{"content":"\\ud800 page text that is long enough"}}]}'
        with self.assertRaises(Exception):
            stage.run(ws.args(vision_config(ws)), FakeTools({bad: [""]}), FakeVision(raw=raw))
        leftovers = [p.name for p in (Path(ws.out) / ".work").glob("*")]
        self.assertEqual(leftovers, [], "staging directory holding page text was left behind")


@requires_repro
class TransientCountedAsOutput(unittest.TestCase):
    """A2: a vision timeout is published nowhere, yet the stage reports new data to review."""

    def test_unpublished_transient_document_is_not_reported_as_output(self):
        ws = Workspace(self)
        digest = ws.add("scan.pdf", scanned_pdf([noise_page(7)]))
        ws.write_queue()
        tools = FakeTools({digest: [""]})
        vision = FakeVision(error=stage.BackendError("vision_timeout"))
        line, code, outcomes = stage.run(ws.args(vision_config(ws)), tools, vision)
        self.assertEqual(list(Path(ws.out).glob("*/*/result.json")), [], "nothing should be published")
        self.assertEqual(line["counts"]["output"], 0, "an unpublished document was counted as output")
        self.assertNotIn("new_data", line)
        self.assertNotIn("review_required", line)


@requires_repro
class MalformedVisionHttp(unittest.TestCase):
    """A3: HTTP-level garbage from the vision server escapes as a raw http.client exception."""

    def serve_once(self, payload):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        def handle():
            try:
                connection, _ = server.accept()
                connection.settimeout(2)
                try:
                    connection.recv(65536)
                    connection.sendall(payload)
                finally:
                    connection.close()
            except OSError:
                pass

        threading.Thread(target=handle, daemon=True).start()
        return f"http://127.0.0.1:{server.getsockname()[1]}/v1/chat/completions"

    def test_non_http_reply_becomes_a_backend_error(self):
        url = self.serve_once(b"this is not http\r\n\r\n")
        with self.assertRaises(stage.BackendError):
            stage.urllib_transport(url, b"{}", 3)

    def test_truncated_chunked_reply_becomes_a_backend_error(self):
        url = self.serve_once(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nab")
        with self.assertRaises(stage.BackendError):
            stage.urllib_transport(url, b"{}", 3)

    def test_http_exception_does_not_abort_the_whole_run(self):
        ws = Workspace(self)
        digest = ws.add("scan.pdf", scanned_pdf([noise_page(3)]))
        ws.write_queue()
        transport = FakeVision(error=http.client.BadStatusLine("garbage"))
        line, code, outcomes = call(self, stage.run, ws.args(vision_config(ws)), FakeTools({digest: [""]}), transport)
        self.assertNotEqual(line.get("error_code"), "STAGE_INTERNAL_ERROR")


@requires_repro
class VisionReplyParsing(unittest.TestCase):
    def test_deeply_nested_json_reply_is_an_invalid_response(self):
        with self.assertRaises(stage.BackendError):
            stage.QwenVisionBackend.parse(b'{"choices":' * 50000)

    def test_second_think_block_is_not_kept_as_page_text(self):
        text = stage.QwenVisionBackend.parse(
            reply("<think>first thoughts</think>\n<think>second thoughts</think>\nReal page text"))
        self.assertNotIn("thoughts", text)

    def test_think_block_after_a_preamble_is_not_kept_as_page_text(self):
        text = stage.QwenVisionBackend.parse(reply("Let me look.\n<think>reasoning here</think>\nReal page text"))
        self.assertNotIn("reasoning here", text)


@requires_repro
class CacheKeyCompleteness(unittest.TestCase):
    """A5: settings that change the stored text must change the cache key."""

    def signature(self, **vision):
        primary = stage.OcrmypdfBackend("/usr/bin/ocrmypdf")
        fallback = stage.QwenVisionBackend(**vision)
        return stage.OcrStage("/nonexistent", primary, fallback).signature

    def test_max_tokens_is_part_of_the_cache_key(self):
        self.assertNotEqual(self.signature(max_tokens=16), self.signature(max_tokens=4096),
                            "a reply truncated at 16 tokens would be served after raising the limit")

    def test_model_endpoint_dpi_are_part_of_the_cache_key(self):
        base = self.signature()
        self.assertNotEqual(base, self.signature(model="another-model"))
        self.assertNotEqual(base, self.signature(dpi=300))
        self.assertNotEqual(base, self.signature(endpoint="http://127.0.0.1:9000/v1/chat/completions"))


@requires_repro
class QueueValidation(unittest.TestCase):
    SHA = "a" * 64

    def line(self, **over):
        value = {"document_id": "doc-1", "sha256": self.SHA, "path": "/srv/src/a.pdf", **over}
        return json.dumps(value)

    def test_trailing_newline_in_document_id_is_rejected(self):
        with self.assertRaises(stage.BackendError):
            stage.parse_queue_line(self.line(document_id="doc-1\n"), Path("/srv/src"))

    def test_trailing_newline_in_sha256_is_rejected(self):
        with self.assertRaises(stage.BackendError):
            stage.parse_queue_line(self.line(sha256=self.SHA + "\n"), Path("/srv/src"))

    def test_relative_source_root_is_a_clean_error(self):
        try:
            stage.parse_queue_line(self.line(), Path("srv/src"))
        except stage.BackendError:
            return
        except Exception as error:  # noqa: BLE001
            self.fail(f"unhandled {type(error).__name__}: {error}")
        self.fail("a relative source root should not accept an absolute path")

    def test_relative_source_root_does_not_crash_the_run(self):
        ws = Workspace(self)
        ws.add("a.pdf", scanned_pdf([noise_page(1)]))
        ws.write_queue()
        args = ws.args(ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp)))
        args.source_root = os.path.relpath(ws.sources)
        line, code, outcomes = call(self, stage.run, args, FakeTools({}), FakeVision())
        self.assertNotEqual(line.get("error_code"), "STAGE_INTERNAL_ERROR")

    def test_fuzzed_lines_only_raise_backend_error(self):
        rng = random.Random(20261022)
        seeds = [self.line(), self.line(path="/srv/src/sub/b.pdf"), self.line(queue_schema="awaiting-ocr-v1")]
        alphabet = ['"', "\\", "\n", "\x00", "..", "//", "/", "‮", "é", "\ud800", " ", "{", "}", ","]
        for _ in range(2000):
            text = list(rng.choice(seeds))
            for _ in range(rng.randint(1, 4)):
                text.insert(rng.randrange(len(text) + 1), rng.choice(alphabet))
            try:
                stage.parse_queue_line("".join(text), Path("/srv/src"))
            except stage.BackendError:
                pass


@requires_repro
class WorkDirectoryHygiene(unittest.TestCase):
    """A8: crashes and write failures must neither leak private copies nor take down the queue."""

    def test_stale_document_copies_from_a_killed_run_are_cleared(self):
        ws = Workspace(self)
        digest = ws.add("scan.pdf", scanned_pdf([noise_page(5)]))
        ws.write_queue()
        stale = Path(ws.out) / ".work" / "doc-killedrun"
        stale.mkdir(parents=True, mode=0o700)
        (stale / "source.pdf").write_bytes(b"%PDF-1.4 stale private copy of an original")
        os.chmod(stale.parent, 0o700)
        stage.run(ws.args(ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp))), FakeTools({digest: [CLEAN]}), FakeVision())
        self.assertFalse(stale.exists(), "a private copy of an original from a killed run is kept forever")

    def test_disk_error_on_one_document_does_not_lose_the_others(self):
        ws = Workspace(self)
        first = ws.add("a.pdf", scanned_pdf([noise_page(1)]))
        second = ws.add("b.pdf", scanned_pdf([noise_page(2)]))
        ws.write_queue()
        real, calls = stage._write_private, []

        def flaky(path, data):
            calls.append(path)
            if len(calls) == 1:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, data)

        config = ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp))
        with mock.patch.object(stage, "_write_private", flaky):
            line, code, outcomes = stage.run(ws.args(config), FakeTools({first: [CLEAN], second: [CLEAN]}), FakeVision())
        published = {p.parent.parent.name for p in Path(ws.out).glob("*/*/result.json")}
        self.assertIn(second, published, "one document's disk error stopped the rest of the queue")
        self.assertNotEqual(line.get("error_code"), "OCR_QUEUE_UNREADABLE",
                            "a write error was reported as an unreadable queue")
        leftovers = [p.name for p in (Path(ws.out) / ".work").glob("publish-*")]
        self.assertEqual(leftovers, [])


@requires_repro
class LanguageCodes(unittest.TestCase):
    """A9: tesseract's own codes for Chinese, Serbian (Latin) and similar are refused."""

    def test_underscore_language_codes_are_accepted(self):
        for code in ("chi_sim", "chi_tra", "srp_latn", "eng+chi_sim"):
            with self.subTest(code=code):
                stage.OcrmypdfBackend("/usr/bin/ocrmypdf", language=code)


@requires_repro
class SidecarProperties(unittest.TestCase):
    """Fuzz/round-trip checks that currently HOLD (page order and skip-range expansion)."""

    def test_round_trip_keeps_page_order(self):
        rng = random.Random(7)
        for _ in range(500):
            pages = [None if rng.random() < 0.4 else rng.choice(["text %d" % rng.randrange(99), "", "é ü ß", "日本語のテキスト"])
                     for _ in range(rng.randint(1, 40))]
            expected = [stage.skipped_marker(i) if p is None else p for i, p in enumerate(pages, 1)]
            self.assertEqual(stage.split_sidecar(ocrmypdf_sidecar(pages)), expected)

    def test_garbage_sidecars_only_raise_backend_error(self):
        rng = random.Random(11)
        parts = ["[OCR skipped on page(s) 1]", "[OCR skipped on page(s) 2-9]", "[OCR skipped on page(s) 0]",
                 "[OCR skipped on page(s) 99999999999]", "[OCR skipped on page(s) 3-2]", "text", "", "\f\f", "\x00"]
        for _ in range(2000):
            text = "\f".join(rng.choice(parts) for _ in range(rng.randint(0, 12)))
            try:
                pages = stage.split_sidecar(text)
            except stage.BackendError:
                continue
            for number, page in enumerate(pages, 1):
                if stage.SKIPPED_PAGE.match(page.strip()):
                    self.assertEqual(page.strip(), stage.skipped_marker(number))


@requires_repro
class DocumentFlowProperties(unittest.TestCase):
    """Behaviour that currently HOLDS: citations, duplicates, tool timeouts."""

    def test_vision_is_asked_for_exactly_the_blank_pages_and_text_lands_on_its_page(self):
        ws = Workspace(self)
        # A skip marker only qualifies on a real native page since X6; pages 1 and 3 carry a text layer.
        digest = ws.add("mixed.pdf", mixed_pdf([["Approved"], noise_page(2), ["Approved"], noise_page(4)]))
        ws.write_queue()
        tools = FakeTools({digest: [None, CLEAN, None, ""]})
        vision = FakeVision(text="Vision text for the fourth page, long enough to pass.")
        stage.run(ws.args(vision_config(ws)), tools, vision)
        asked = [argv[argv.index("-f") + 1] for argv in tools.calls if Path(argv[0]).name == "pdftoppm"]
        self.assertEqual(asked, ["4"])
        result = next(Path(ws.out).glob("*/*/result.json"))
        record = json.loads(result.read_text())
        self.assertEqual([row["page"] for row in record["pages"]], [1, 2, 3, 4])
        self.assertEqual((result.parent / "page-0002.txt").read_text(), CLEAN)
        self.assertIn("Vision text", (result.parent / "page-0004.txt").read_text())
        self.assertFalse((result.parent / "page-0001.txt").exists())

    def test_identical_bytes_under_two_ids_run_the_tool_once(self):
        ws = Workspace(self)
        data = scanned_pdf([noise_page(9)])
        digest = ws.add("one.pdf", data, document_id="doc-one")
        ws.add("two.pdf", data, document_id="doc-two")
        ws.write_queue()
        tools = FakeTools({digest: [CLEAN]})
        line, code, outcomes = stage.run(ws.args(ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp))), tools, FakeVision())
        self.assertEqual(tools.count("ocrmypdf"), 1)
        self.assertEqual([o.status for o in outcomes], ["ocr_complete", "cached"])

    def test_one_changed_byte_is_a_different_document(self):
        ws = Workspace(self)
        data = bytearray(scanned_pdf([noise_page(9)]))
        other = bytes(data) + b"\n%synthetic trailing comment\n"
        a = ws.add("a.pdf", bytes(data), document_id="a")
        b = ws.add("b.pdf", other, document_id="b")
        ws.write_queue()
        tools = FakeTools({a: [CLEAN], b: [CLEAN]})
        stage.run(ws.args(ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp))), tools, FakeVision())
        self.assertEqual(tools.count("ocrmypdf"), 2)

    def test_ocr_tool_timeout_is_a_failure_and_is_not_cached(self):
        ws = Workspace(self)
        digest = ws.add("scan.pdf", scanned_pdf([noise_page(4)]))
        ws.write_queue()
        config = ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp))
        line, code, outcomes = stage.run(ws.args(config), FakeTools({digest: [CLEAN]}, raise_timeout=True), FakeVision())
        self.assertEqual(outcomes[0].status, "ocr_failed")
        self.assertEqual(line["status"], "warn")
        self.assertEqual(list(Path(ws.out).glob("*/*/result.json")), [])


if __name__ == "__main__":
    unittest.main()
