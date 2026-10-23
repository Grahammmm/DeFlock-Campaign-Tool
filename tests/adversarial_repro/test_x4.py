# Claude-authored repros copied from d0264abadb0a3e1b30455855749a182309a5e36a; enabled per admitted brief.
import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from campaign_tool.records.extract import ocr
from tests.adversarial_repro._support import call
from tests.records.ocr_stage_fixtures import PNG_STUB, noise_page, scanned_pdf
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


class ConfidenceParsing(Case):
    """B1: Tesseract TSV is parsed as CSV, so a word that starts with a double quote swallows the rows after it."""

    def rows(self):
        good = [tsv_row("good", 95, n) for n in range(10)]
        quoted = [tsv_row('"x', 90, 11)]
        junk = [tsv_row("junk", 10, 20 + n) for n in range(40)]
        return good + quoted + junk

    def test_quoted_word_does_not_hide_later_low_confidence_rows(self):
        rows = self.rows()
        tsv = "\n".join([HEADER] + rows) + "\n"
        expected = round(sum(float(r.split("\t")[10]) for r in rows) / len(rows), 2)
        self.assertEqual(ocr._confidence(tsv), expected)

    def test_garbage_page_with_one_quote_is_queued_for_visual_check(self):
        sha = self.blob(scanned_pdf([noise_page(2)]))
        tsv = "\n".join([HEADER] + self.rows()) + "\n"
        (receipt,) = self.extract(sha, StubRunner(text="junk " * 50, tsv=tsv))
        self.assertEqual(receipt["status"], "visual_check_queued",
                         "a mostly low-confidence page was accepted as ocr_text_unreviewed")

    def test_very_large_quoted_field_blocks_only_that_page(self):
        sha = self.blob(scanned_pdf([noise_page(3), noise_page(4)]))
        big = "\n".join([HEADER, tsv_row('"x', 90)] + [tsv_row("w" * 60, 50, n) for n in range(2500)]) + "\n"
        self.assertGreater(len(big), 131072)
        runner = StubRunner(tsv=big)
        receipts = call(self, self.extract, sha, runner)
        self.assertEqual(len(receipts), 2, "the second page was never attempted")
        self.assertEqual(self.attempt_dirs(), [], "a private page raster was left in an .attempt directory")
