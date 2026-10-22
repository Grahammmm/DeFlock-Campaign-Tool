"""X7 repro copied from adversarial source d0264ab; always enabled."""

import hashlib

import io

from pathlib import Path

import subprocess

import tempfile

import unittest

from unittest import mock

from campaign_tool.records.extract import ocr

from tests.adversarial_medium_low._support import call

from tests.records.ocr_stage_fixtures import PNG_STUB, _pdf, _stream, noise_page, scanned_pdf

TOOLS = {"pdftoppm": {"path": "/usr/bin/pdftoppm", "version": "stub-1"},
         "tesseract": {"path": "/usr/bin/tesseract", "version": "stub-1"}}

HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"

def tsv_row(word, conf, number=1):
    return f"5\t1\t1\t1\t1\t{number}\t0\t0\t10\t10\t{conf}\t{word}"

class StubRunner:
    """Stands in for subprocess.run: pdftoppm writes a PNG stub, tesseract writes text + TSV."""

    def __init__(self, text="Synthetic OCR text for the page.\n", tsv=None, tesseract_error=None):
        self.text, self.tsv, self.error = text, tsv, tesseract_error
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        tool = Path(command[0]).name
        if tool == "pdftoppm":
            Path(command[-1] + ".png").write_bytes(PNG_STUB)
        elif tool == "tesseract":
            if self.error is not None:
                raise self.error
            base = Path(command[2])
            base.with_name(base.name + ".txt").write_text(self.text, encoding="utf-8")
            tsv = self.tsv if self.tsv is not None else "\n".join([HEADER, tsv_row("word", 96)]) + "\n"
            base.with_name(base.name + ".tsv").write_text(tsv, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="adv-ocr-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.intake, self.out = self.tmp / "intake", self.tmp / "out"
        (self.intake / "blobs").mkdir(parents=True)

    def blob(self, data):
        sha = hashlib.sha256(data).hexdigest()
        (self.intake / "blobs" / sha).write_bytes(data)
        return sha

    def extract(self, sha, runner=None, **kwargs):
        return ocr.extract_image_only_pages(self.intake, sha, self.out, tool_signature="stub 1", tools=TOOLS,
                                            runner=runner or StubRunner(), **kwargs)

    def attempt_dirs(self):
        return [p.name for p in self.out.glob("*/page-*/.attempt-*")]

class PageSelectionAndResume(Case):
    def test_system_exit_also_cleans_and_propagates(self):
        sha = self.blob(scanned_pdf([noise_page(20)]))
        with self.assertRaises(SystemExit):
            self.extract(sha, StubRunner(tesseract_error=SystemExit(7)))
        self.assertEqual(self.attempt_dirs(), [])

    def test_interrupt_during_attempt_setup_cleans(self):
        sha = self.blob(scanned_pdf([noise_page(21)]))
        real = ocr._managed_directory
        def interrupt(path, *args, **kwargs):
            if Path(path).name.startswith(".attempt-"):
                raise KeyboardInterrupt()
            return real(path, *args, **kwargs)
        with mock.patch.object(ocr, "_managed_directory", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.extract(sha)
        self.assertEqual(self.attempt_dirs(), [])

    def test_successful_publication_survives_cleanup_and_resumes(self):
        sha = self.blob(scanned_pdf([noise_page(22)]))
        first = self.extract(sha)
        runner = StubRunner()
        second = self.extract(sha, runner)
        self.assertEqual(first, second)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.attempt_dirs(), [])

    """Behaviour that currently HOLDS: selector validation, page numbering and idempotent resume."""

    def test_interrupt_during_ocr_leaves_no_attempt_directory(self):
        sha = self.blob(scanned_pdf([noise_page(9)]))
        runner = StubRunner(tesseract_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.extract(sha, runner)
        self.assertEqual(self.attempt_dirs(), [], 'a page raster and OCR text were left in .attempt-*')
