"""X14 repro copied from adversarial source d0264ab; always enabled."""

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

class LanguageCodes(unittest.TestCase):
    def test_language_grammar_remains_bounded_and_never_becomes_an_option(self):
        for value in ("eng", "eng+spa+chi_sim+srp_latn"):
            backend = stage.OcrmypdfBackend("/usr/bin/ocrmypdf", language=value)
            argv = backend.argv(Path("/source.pdf"), Path("/sidecar.txt"), Path("/out.pdf"))
            self.assertEqual(argv[argv.index("-l") + 1], value)
        for value in ("", "eng\n", "eng+", "+eng", "chi_si", "srp_latnx", "eng__abc",
                      "eng+spa+fra+deu+ita", "--help", "eng -l spa", "eng;true", "../eng"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                stage.OcrmypdfBackend("/usr/bin/ocrmypdf", language=value)

    """A9: tesseract's own codes for Chinese, Serbian (Latin) and similar are refused."""

    def test_underscore_language_codes_are_accepted(self):
        for code in ("chi_sim", "chi_tra", "srp_latn", "eng+chi_sim"):
            with self.subTest(code=code):
                stage.OcrmypdfBackend("/usr/bin/ocrmypdf", language=code)
