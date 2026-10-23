"""X12 repro copied from adversarial source d0264ab; always enabled."""

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

class QueueValidation(unittest.TestCase):
    def test_relative_root_is_config_failure_even_for_empty_queue_and_no_output(self):
        ws = Workspace(self)
        ws.write_queue()
        args = ws.args(ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp)))
        args.source_root = "."
        line, code, outcomes = stage.run(args, FakeTools({}), FakeVision())
        self.assertEqual((line["status"], line["error_code"], code, outcomes),
                         ("fail", "OCR_CONFIG_INVALID", 1, []))
        self.assertFalse(Path(ws.out).exists())

    def test_absolute_root_control_still_publishes(self):
        ws = Workspace(self)
        digest = ws.add("a.pdf", scanned_pdf([noise_page(41)]))
        ws.write_queue()
        line, code, outcomes = stage.run(ws.args(ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp))),
                                       FakeTools({digest: [CLEAN]}), FakeVision())
        self.assertEqual((code, outcomes[0].status, line["counts"]["output"]), (0, "ocr_complete", 1))

    SHA = 'a' * 64

    def line(self, **over):
        value = {'document_id': 'doc-1', 'sha256': self.SHA, 'path': '/srv/src/a.pdf', **over}
        return json.dumps(value)

    def test_relative_source_root_is_a_clean_error(self):
        try:
            stage.parse_queue_line(self.line(), Path('srv/src'))
        except stage.BackendError:
            return
        except Exception as error:
            self.fail(f'unhandled {type(error).__name__}: {error}')
        self.fail('a relative source root should not accept an absolute path')

    def test_relative_source_root_does_not_crash_the_run(self):
        ws = Workspace(self)
        ws.add('a.pdf', scanned_pdf([noise_page(1)]))
        ws.write_queue()
        args = ws.args(ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp)))
        args.source_root = os.path.relpath(ws.sources)
        line, code, outcomes = call(self, stage.run, args, FakeTools({}), FakeVision())
        self.assertNotEqual(line.get('error_code'), 'STAGE_INTERNAL_ERROR')
