"""X11 repro copied from adversarial source d0264ab; always enabled."""

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

class QueueValidation(unittest.TestCase):
    def test_exact_identifier_and_hash_boundaries(self):
        for identifier in ("a", "a" * 128, "sha256:" + "a" * 64):
            with self.subTest(identifier=identifier):
                self.assertEqual(stage.parse_queue_line(self.line(document_id=identifier), Path("/srv/src")).document_id, identifier)
        for field, values in (("document_id", ("", "a" * 129, "doc\r", "doc\x00", "doc\n\n")),
                              ("sha256", ("a" * 63, "a" * 65, "A" * 64, self.SHA + "\r"))):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(stage.BackendError):
                    stage.parse_queue_line(self.line(**{field: value}), Path("/srv/src"))

    SHA = 'a' * 64

    def line(self, **over):
        value = {'document_id': 'doc-1', 'sha256': self.SHA, 'path': '/srv/src/a.pdf', **over}
        return json.dumps(value)

    def test_trailing_newline_in_document_id_is_rejected(self):
        with self.assertRaises(stage.BackendError):
            stage.parse_queue_line(self.line(document_id='doc-1\n'), Path('/srv/src'))

    def test_trailing_newline_in_sha256_is_rejected(self):
        with self.assertRaises(stage.BackendError):
            stage.parse_queue_line(self.line(sha256=self.SHA + '\n'), Path('/srv/src'))
