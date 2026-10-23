# Claude-authored repros copied from d0264abadb0a3e1b30455855749a182309a5e36a; enabled per admitted brief.
# Sol adaptation: in-memory HTTP transport only; X13 tests excluded.
import json
import http.client
import io
from pathlib import Path
import unittest
from unittest import mock
from campaign_tool.records import ocr_stage as stage
from tests.adversarial_repro._support import call
from tests.records.ocr_stage_fixtures import FakeTools, FakeVision, noise_page, scanned_pdf
from tests.records.test_ocr_stage import CLEAN, Workspace, fake_ocrmypdf

def reply(text):
    """A well-formed OpenAI-style reply whose content is ``text`` (JSON-escaped by json.dumps)."""
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}]}).encode()


def vision_config(ws, **vision):
    return ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp),
                     vision={"endpoint": "http://127.0.0.1:8003/v1/chat/completions", **vision})

class MalformedVisionHttp(unittest.TestCase):
    """A3: HTTP-level garbage from the vision server escapes as a raw http.client exception."""

    def serve_once(self, payload):
        class MemorySocket:
            def makefile(self, *args, **kwargs):
                return io.BytesIO(payload)
        def open_response(*args, **kwargs):
            response = http.client.HTTPResponse(MemorySocket())
            response.begin()
            return response
        opener = mock.Mock()
        opener.open.side_effect = open_response
        patcher = mock.patch.object(stage.urllib.request, "build_opener", return_value=opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        return "http://127.0.0.1:8003/v1/chat/completions"

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


class VisionReplyParsing(unittest.TestCase):
    def test_deeply_nested_json_reply_is_an_invalid_response(self):
        with self.assertRaises(stage.BackendError):
            stage.QwenVisionBackend.parse(b'{"choices":' * 50000)
