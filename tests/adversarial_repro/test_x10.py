"""X10 repro copied from adversarial source d0264ab; always enabled."""

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

class CacheKeyCompleteness(unittest.TestCase):
    """A5: settings that change the stored text must change the cache key."""

    def test_prompt_and_parser_changes_invalidate_but_timeout_does_not(self):
        base = self.signature()
        self.assertEqual(base, self.signature(timeout=1))
        self.assertEqual(base, self.signature())
        for setting in ("VISION_PROMPT", "VISION_USER_PROMPT", "VISION_PARSER"):
            with self.subTest(setting=setting), mock.patch.object(stage, setting,
                    99 if setting == "VISION_PARSER" else "Different transcription instruction", create=True):
                self.assertNotEqual(base, self.signature())

    def test_raising_token_budget_does_not_reuse_existing_low_budget_text(self):
        ws = Workspace(self)
        digest = ws.add("scan.pdf", scanned_pdf([noise_page(11)]))
        ws.write_queue()
        small, large = vision_config(ws, max_tokens=16), None
        # Workspace.config writes one fixed fixture path; run before rewriting it.
        stage.run(ws.args(small), FakeTools({digest: [""]}), FakeVision(text="short text"))
        large = vision_config(ws, max_tokens=4096)
        tools = FakeTools({digest: [""]})
        _, _, outcomes = stage.run(ws.args(large), tools, FakeVision(text=CLEAN))
        self.assertEqual(outcomes[0].status, "ocr_complete")
        self.assertEqual(tools.count("ocrmypdf"), 1)
        self.assertEqual(len(list(Path(ws.out).glob("*/*/result.json"))), 2)

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
