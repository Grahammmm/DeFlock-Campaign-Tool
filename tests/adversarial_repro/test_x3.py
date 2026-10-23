"""X3 repro copied from adversarial source d0264ab; always enabled."""

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

from tests.adversarial_repro._support import call

from tests.records.ocr_stage_fixtures import (FakeTools, FakeVision, blank_page, noise_page,
                                              ocrmypdf_sidecar, scanned_pdf)

from tests.records.test_ocr_stage import CLEAN, Workspace, fake_ocrmypdf

def reply(text):
    """A well-formed OpenAI-style reply whose content is ``text`` (JSON-escaped by json.dumps)."""
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}]}).encode()

def vision_config(ws, **vision):
    return ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp),
                     vision={"endpoint": "http://127.0.0.1:8003/v1/chat/completions", **vision})

class TransientCountedAsOutput(unittest.TestCase):
    """A2: a vision timeout is published nowhere, yet the stage reports new data to review."""

    def test_mixed_published_and_deferred_counts_only_published_pages_and_retries(self):
        ws = Workspace(self)
        bad = ws.add("a.pdf", scanned_pdf([noise_page(8)]))
        good = ws.add("b.pdf", scanned_pdf([noise_page(9)]))
        ws.write_queue()
        config = vision_config(ws)
        tools = FakeTools({bad: [""], good: [CLEAN]})
        line, code, outcomes = stage.run(ws.args(config), tools,
            FakeVision(error=stage.BackendError("vision_timeout")))
        self.assertEqual(code, 0)
        self.assertEqual([o.status for o in outcomes], ["ocr_deferred", "ocr_complete"])
        self.assertEqual(line["counts"], {"input": 2, "output": 1, "failed": 0, "skipped": 0})
        self.assertEqual(line["new_data"], {"documents": 1, "pages": 1})
        self.assertEqual(line["review_required"], 1)
        self.assertEqual(line["error_code"], "OCR_BACKEND_TRANSIENT")
        self.assertEqual(outcomes[0].unresolved_pages, 1)
        again, _, retried = stage.run(ws.args(config), FakeTools({bad: [CLEAN]}), FakeVision())
        self.assertEqual([o.status for o in retried], ["ocr_complete", "cached"])
        self.assertEqual(again["counts"]["output"], 1)

    def test_published_partial_still_counts_as_review_required(self):
        ws = Workspace(self)
        digest = ws.add("partial.pdf", scanned_pdf([noise_page(10)]))
        ws.write_queue()
        line, _, outcomes = stage.run(ws.args(ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp))),
                                     FakeTools({digest: [""]}), FakeVision())
        self.assertEqual(outcomes[0].status, "ocr_partial")
        self.assertEqual(line["counts"]["output"], 1)
        self.assertEqual(line["review_required"], 1)

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
