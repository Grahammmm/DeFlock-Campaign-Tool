"""X5 repro copied from adversarial source d0264ab; always enabled."""

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

class MalformedPdfs(Case):
    def test_one_page_control_has_one_unreviewed_receipt(self):
        sha = self.blob(scanned_pdf([noise_page(19)]))
        (receipt,) = self.extract(sha)
        self.assertEqual(receipt["page_count"], 1)
        self.assertEqual(receipt["review_status"], "not_reviewed")
        self.assertEqual(receipt["status"], "ocr_text_unreviewed")

    def test_zero_pages_is_precise_error_before_output_creation(self):
        sha = self.blob(scanned_pdf([]))
        with self.assertRaisesRegex(ValueError, "^pdf_has_no_pages$"):
            self.extract(sha)
        self.assertFalse(self.out.exists())

    def test_zero_page_pdf_is_not_a_silent_success(self):
        sha = self.blob(scanned_pdf([]))
        try:
            receipts = self.extract(sha)
        except ValueError:
            return
        self.assertNotEqual(receipts, [], "a PDF with no readable pages produced no receipt and no error")

    def test_cli_exit_code_for_zero_page_pdf_is_not_success(self):
        sha = self.blob(scanned_pdf([]))
        argv = ["--intake-root", str(self.intake), "--sha", sha, "--output-root", str(self.out), "--tool-signature", "stub 1"]
        with mock.patch.object(ocr, "dependency_status", return_value={"ready": True, "tools": TOOLS}), \
                mock.patch.object(ocr.subprocess, "run", StubRunner()), mock.patch("builtins.print"):
            code = ocr.main(argv)
        self.assertNotEqual(code, 0)

    def test_truncated_pdf_does_not_silently_lose_pages(self):
        data = scanned_pdf([noise_page(n) for n in range(1, 7)])
        sha = self.blob(data[: int(len(data) * 0.45)])
        try:
            receipts = self.extract(sha)
        except Exception:  # noqa: BLE001 - refusing a damaged PDF is acceptable
            return
        self.assertEqual(len(receipts), 6, f"only {len(receipts)} of 6 pages were reported for a truncated file")

    def test_non_pdf_bytes_are_refused_cleanly(self):
        sha = self.blob(b"%PDF-1.4\n" + bytes(range(256)) * 20)
        with self.assertRaises(Exception):
            self.extract(sha)

    def test_enormous_page_is_blocked_not_rasterised(self):
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200000 200000] >>"]
        sha = self.blob(_pdf(objects))
        runner = StubRunner()
        (receipt,) = self.extract(sha, runner)
        self.assertEqual((receipt["status"], receipt["blocked_reason"]), ("blocked", "page_raster_bound"))
        self.assertEqual([c for c in runner.calls if "pdftoppm" in c[0]], [])
