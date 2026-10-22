"""X9 repro copied from adversarial source d0264ab; always enabled."""

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

class WorkDirectoryHygiene(unittest.TestCase):
    def test_stale_symlink_is_unlinked_without_touching_target_or_published_cache(self):
        ws = Workspace(self)
        digest = ws.add("scan.pdf", scanned_pdf([noise_page(40)]))
        ws.write_queue()
        config = ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp))
        stage.run(ws.args(config), FakeTools({digest: [CLEAN]}), FakeVision())
        result = next(Path(ws.out).glob("*/*/result.json"))
        before = result.read_bytes()
        external = ws.tmp / "unrelated"
        external.mkdir()
        sentinel = external / "keep.txt"
        sentinel.write_text("synthetic preserved sentinel")
        work = Path(ws.out) / ".work"
        (work / "stale-link").symlink_to(external, target_is_directory=True)
        (work / "stale-file").write_text("synthetic residue")
        line, _, outcomes = stage.run(ws.args(config), FakeTools({}), FakeVision())
        self.assertEqual(list(work.iterdir()), [])
        self.assertEqual(sentinel.read_text(), "synthetic preserved sentinel")
        self.assertEqual(result.read_bytes(), before)
        self.assertEqual(outcomes[0].status, "cached")

    def test_busy_run_cannot_remove_active_work(self):
        ws = Workspace(self)
        ws.write_queue()
        config = ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp))
        owner = stage.build_stage(str(ws.out), stage.load_config(config))
        with owner.locked():
            active = Path(ws.out) / ".work" / "doc-active"
            active.mkdir(parents=True, mode=0o700)
            os.chmod(active.parent, 0o700)
            line, code, _ = stage.run(ws.args(config), FakeTools({}), FakeVision())
            self.assertEqual(line["error_code"], "OCR_STAGE_BUSY")
            self.assertTrue(active.exists())

    """A8: crashes and write failures must neither leak private copies nor take down the queue."""

    def test_stale_document_copies_from_a_killed_run_are_cleared(self):
        ws = Workspace(self)
        digest = ws.add('scan.pdf', scanned_pdf([noise_page(5)]))
        ws.write_queue()
        stale = Path(ws.out) / '.work' / 'doc-killedrun'
        stale.mkdir(parents=True, mode=448)
        (stale / 'source.pdf').write_bytes(b'%PDF-1.4 stale private copy of an original')
        os.chmod(stale.parent, 448)
        stage.run(ws.args(ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp))), FakeTools({digest: [CLEAN]}), FakeVision())
        self.assertFalse(stale.exists(), 'a private copy of an original from a killed run is kept forever')
