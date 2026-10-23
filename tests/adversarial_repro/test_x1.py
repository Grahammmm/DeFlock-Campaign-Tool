# Claude-authored repros copied from d0264abadb0a3e1b30455855749a182309a5e36a; enabled per admitted brief.
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
