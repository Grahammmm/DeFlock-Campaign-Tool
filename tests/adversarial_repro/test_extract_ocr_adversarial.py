"""Adversarial reproductions for campaign_tool/records/extract/ocr.py (per-page Tesseract receipts).

Synthetic PDFs and a stub runner only: no real OCR tool is executed.
"""
import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from campaign_tool.records.extract import ocr
from tests.adversarial_repro._support import call, requires_repro
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


@requires_repro
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


@requires_repro
class MalformedPdfs(Case):
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


@requires_repro
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


@requires_repro
class PageSelectionAndResume(Case):
    """Behaviour that currently HOLDS: selector validation, page numbering and idempotent resume."""

    def test_selectors_are_validated(self):
        sha = self.blob(scanned_pdf([noise_page(n) for n in range(3)]))
        for pages in ((0,), (4,), (-1,), (1, 1), (True,), (1.0,), ("1",)):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                self.extract(sha, pages=pages)

    def test_selected_pages_get_matching_page_numbers(self):
        sha = self.blob(scanned_pdf([noise_page(n) for n in range(5)]))
        receipts = self.extract(sha, pages=(5, 2))
        self.assertEqual([(r["locator"]["page"], r["page_count"]) for r in receipts], [(5, 5), (2, 5)])
        self.assertEqual(sorted(p.name for p in (self.out / sha).glob("page-*")), ["page-000002", "page-000005"])

    def test_second_run_reuses_receipts_without_calling_tools(self):
        sha = self.blob(scanned_pdf([noise_page(n) for n in range(2)]))
        first = self.extract(sha)
        runner = StubRunner()
        second = self.extract(sha, runner)
        self.assertEqual([r["receipt_id"] for r in first], [r["receipt_id"] for r in second])
        self.assertEqual(runner.calls, [])

    def test_tool_timeout_is_a_blocked_receipt_not_text(self):
        sha = self.blob(scanned_pdf([noise_page(8)]))
        runner = StubRunner(tesseract_error=subprocess.TimeoutExpired(["tesseract"], 1))
        (receipt,) = self.extract(sha, runner)
        self.assertEqual((receipt["status"], receipt["blocked_reason"]), ("blocked", "local_ocr_timeout"))
        self.assertEqual(self.attempt_dirs(), [])

    def test_interrupt_during_ocr_leaves_no_attempt_directory(self):
        sha = self.blob(scanned_pdf([noise_page(9)]))
        runner = StubRunner(tesseract_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.extract(sha, runner)
        self.assertEqual(self.attempt_dirs(), [], "a page raster and OCR text were left in .attempt-*")


if __name__ == "__main__":
    unittest.main()
