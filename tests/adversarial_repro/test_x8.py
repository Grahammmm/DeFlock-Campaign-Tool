"""X8 repro copied from adversarial source d0264ab; always enabled."""

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

class PoisonedVisionTextAbortsStage(unittest.TestCase):
    """A1: vision text with a lone surrogate crashes publication and blocks every later document."""

    def test_failed_publish_leaves_no_staging_directory(self):
        ws = Workspace(self)
        bad = ws.add('a-bad.pdf', scanned_pdf([noise_page(1)]))
        ws.write_queue()
        raw = b'{"choices":[{"message":{"content":"\\ud800 page text that is long enough"}}]}'
        with self.assertRaises(Exception):
            stage.run(ws.args(vision_config(ws)), FakeTools({bad: ['']}), FakeVision(raw=raw))
        leftovers = [p.name for p in (Path(ws.out) / '.work').glob('*')]
        self.assertEqual(leftovers, [], 'staging directory holding page text was left behind')

class WorkDirectoryHygiene(unittest.TestCase):
    def test_manifest_or_rename_failure_is_one_failed_document_then_retry(self):
        for failure in ("manifest", "rename"):
            with self.subTest(failure=failure):
                ws = Workspace(self)
                first = ws.add("a.pdf", scanned_pdf([noise_page(30)]))
                second = ws.add("b.pdf", scanned_pdf([noise_page(31)]))
                ws.write_queue()
                config = ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp))
                real_write, real_rename = stage._write_private, stage._publish_noreplace
                failed = []
                def write(path, data):
                    if failure == "manifest" and path.name == "result.json" and not failed:
                        failed.append(True)
                        raise OSError(errno.ENOSPC, "synthetic full disk")
                    return real_write(path, data)
                def rename(source, target):
                    if failure == "rename" and not failed:
                        failed.append(True)
                        raise OSError(errno.EIO, "synthetic rename failure")
                    return real_rename(source, target)
                with mock.patch.object(stage, "_write_private", write), mock.patch.object(stage, "_publish_noreplace", rename):
                    line, _, outcomes = stage.run(ws.args(config), FakeTools({first: [CLEAN], second: [CLEAN]}), FakeVision())
                self.assertEqual([(o.status, o.code) for o in outcomes],
                                 [("ocr_failed", "publish_failed"), ("ocr_complete", None)])
                self.assertEqual(line["counts"], {"input": 2, "output": 1, "failed": 1, "skipped": 0})
                self.assertEqual(list((Path(ws.out) / ".work").iterdir()), [])
                _, _, retry = stage.run(ws.args(config), FakeTools({first: [CLEAN]}), FakeVision())
                self.assertEqual([o.status for o in retry], ["ocr_complete", "cached"])

    """A8: crashes and write failures must neither leak private copies nor take down the queue."""

    def test_disk_error_on_one_document_does_not_lose_the_others(self):
        ws = Workspace(self)
        first = ws.add('a.pdf', scanned_pdf([noise_page(1)]))
        second = ws.add('b.pdf', scanned_pdf([noise_page(2)]))
        ws.write_queue()
        real, calls = (stage._write_private, [])

        def flaky(path, data):
            calls.append(path)
            if len(calls) == 1:
                raise OSError(errno.ENOSPC, 'No space left on device')
            return real(path, data)
        config = ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp))
        with mock.patch.object(stage, '_write_private', flaky):
            line, code, outcomes = stage.run(ws.args(config), FakeTools({first: [CLEAN], second: [CLEAN]}), FakeVision())
        published = {p.parent.parent.name for p in Path(ws.out).glob('*/*/result.json')}
        self.assertIn(second, published, "one document's disk error stopped the rest of the queue")
        self.assertNotEqual(line.get('error_code'), 'OCR_QUEUE_UNREADABLE', 'a write error was reported as an unreadable queue')
        leftovers = [p.name for p in (Path(ws.out) / '.work').glob('publish-*')]
        self.assertEqual(leftovers, [])
