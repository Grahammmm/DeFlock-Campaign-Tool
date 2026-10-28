"""Always-enabled X6 reproductions copied from the pinned adversarial branch.

Only X6 definitions are admitted here; unrelated adversarial findings remain upstream.
"""
import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from campaign_tool.records.extract import ocr
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

def scan_with_text_layer(text, pages=1):
    """Full-page image plus a small real text layer (a stamp), like a stamped scan."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % (4 + 4 * i) for i in range(pages))
               + b"] /Count %d >>" % pages,
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    width, height, gray = noise_page(1)
    import zlib
    for index in range(pages):
        page, content, image = 4 + 4 * index, 5 + 4 * index, 6 + 4 * index
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> "
                       b"/XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>" % (image, content))
        objects.append(_stream(b"", b"q 612 0 0 792 0 0 cm /Im0 Do Q BT /F1 8 Tf 20 20 Td (" + text.encode("ascii") + b") Tj ET"))
        objects.append(_stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
                               b"/BitsPerComponent 8 /Filter /FlateDecode" % (width, height), zlib.compress(gray)))
        objects.append(b"null")
    return _pdf(objects)

class TextLayerHeuristic(Case):
    """B3/B4: any non-blank text layer makes the whole page 'machine text' and OCR is skipped."""

    def test_stamp_on_a_scanned_page_does_not_hide_the_scan_from_ocr(self):
        sha = self.blob(scan_with_text_layer("BATES-000123"))
        runner = StubRunner()
        (receipt,) = self.extract(sha, runner)
        self.assertNotEqual(receipt["status"], "skipped_machine_text",
                            "a full-page scan with only a Bates stamp as text was never OCR'd")

    def test_replacement_characters_are_not_treated_as_text(self):
        from pypdf import PageObject
        sha = self.blob(scanned_pdf([noise_page(5)]))
        for junk in ("���", "\x00\x00", "​​", "﻿"):
            with self.subTest(junk=repr(junk)), mock.patch.object(PageObject, "extract_text", return_value=junk):
                out = self.tmp / ("out-" + str(abs(hash(junk))))
                (receipt,) = ocr.extract_image_only_pages(self.intake, sha, out, tool_signature="stub 1",
                                                          tools=TOOLS, runner=StubRunner())
                self.assertNotEqual(receipt["status"], "skipped_machine_text")
