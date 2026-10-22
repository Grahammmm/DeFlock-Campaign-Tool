"""X13 repro copied from adversarial source d0264ab; always enabled."""

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

from tests.adversarial_medium_low._support import call

from tests.records.ocr_stage_fixtures import (FakeTools, FakeVision, blank_page, noise_page,
                                              ocrmypdf_sidecar, scanned_pdf)

from tests.records.test_ocr_stage import CLEAN, Workspace, fake_ocrmypdf

def reply(text):
    """A well-formed OpenAI-style reply whose content is ``text`` (JSON-escaped by json.dumps)."""
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}]}).encode()

def vision_config(ws, **vision):
    return ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp),
                     vision={"endpoint": "http://127.0.0.1:8003/v1/chat/completions", **vision})

class VisionReplyParsing(unittest.TestCase):
    def test_unbalanced_nested_or_attributed_tags_are_invalid_not_stored(self):
        for content in ("<think>private reasoning", "page </think>",
                        "<think>outer <think>inner</think> left</think>page",
                        "<think mode='reason'>secret</think>page", "page <THINK>secret"):
            with self.subTest(content=content):
                with self.assertRaises(stage.BackendError) as caught:
                    stage.QwenVisionBackend.parse(reply(content))
                self.assertEqual(caught.exception.code, "vision_response_invalid")

    def test_transcription_without_reasoning_is_preserved_byte_for_byte(self):
        for content in ("", "  Page text\nsecond line  ", "a <thing> example", "日本語のテキスト"):
            with self.subTest(content=content):
                self.assertEqual(stage.QwenVisionBackend.parse(reply(content)), content)

    def test_multiple_case_variant_blocks_leave_only_transcription(self):
        self.assertEqual(stage.QwenVisionBackend.parse(reply(
            "<THINK>first</THINK>\n<think>second</think>\nTranscription")), "Transcription")


    def test_second_think_block_is_not_kept_as_page_text(self):
        text = stage.QwenVisionBackend.parse(reply('<think>first thoughts</think>\n<think>second thoughts</think>\nReal page text'))
        self.assertNotIn('thoughts', text)

    def test_think_block_after_a_preamble_is_not_kept_as_page_text(self):
        text = stage.QwenVisionBackend.parse(reply('Let me look.\n<think>reasoning here</think>\nReal page text'))
        self.assertNotIn('reasoning here', text)
