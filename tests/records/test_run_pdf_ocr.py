"""PDF originals through the single command: native text pages, and image-only pages via the
real local OCR tools when they are installed (held, visibly, when they are not)."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import zlib

from campaign_tool.records.run import Pipeline, local_ocr_tools
from tests.records.test_run_slice import synthetic_email
from tests.runner.helpers import tiny_pdf

HAVE_OCR = all(shutil.which(tool) for tool in ("tesseract", "pdftoppm"))


def image_only_pdf(text_pdf, dpi=150):
    """Rasterise a text PDF with pdftoppm and wrap the raw RGB bitmap as a one-page image PDF.

    The result has no text layer at all, like a scanned production.
    """
    with tempfile.TemporaryDirectory() as scratch:
        source = Path(scratch) / "source.pdf"
        source.write_bytes(text_pdf)
        subprocess.run(["pdftoppm", "-r", str(dpi), "-singlefile", str(source), str(Path(scratch) / "page")],
                       check=True, capture_output=True, timeout=60)
        ppm = (Path(scratch) / "page.ppm").read_bytes()
    header, rest = ppm.split(b"\n", 1)
    assert header == b"P6"
    dims, rest = rest.split(b"\n", 1)
    width, height = (int(v) for v in dims.split())
    _, raw = rest.split(b"\n", 1)
    data = zlib.compress(raw)
    page_w, page_h = width * 72 / dpi, height * 72 / dpi
    content = f"q {page_w:.2f} 0 0 {page_h:.2f} 0 0 cm /Im0 Do Q".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w:.2f} {page_h:.2f}] /Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>".encode(),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        (f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceRGB "
         f"/BitsPerComponent 8 /Filter /FlateDecode /Length {len(data)} >>\nstream\n").encode() + data + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


class PdfOcrTests(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.tmp = tempfile.TemporaryDirectory(prefix="records-pdf-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        os.chmod(self.base, 0o700)
        self.inbox = self.base / "inbox"
        self.inbox.mkdir(mode=0o700)
        self.root = self.base / "root"

    def ledger(self, sql, values=()):
        con = sqlite3.connect(self.root / "ledger.sqlite")
        try:
            return con.execute(sql, values).fetchall()
        finally:
            con.close()

    def test_native_text_pdf_completes(self):
        (self.inbox / "text.eml").write_bytes(synthetic_email(attachment=tiny_pdf(), filename="policy.pdf",
                                                              message_id="<pdf1@agency.example.invalid>"))
        report = Pipeline(self.root).run(self.inbox)
        self.assertEqual(report["originals"], 2)
        self.assertEqual(report["end_to_end_complete"], 2)
        pages = self.ledger("SELECT page_no,status,needs_visual_review FROM page_state")
        self.assertEqual(pages, [(1, "ok", 1)])

    @unittest.skipUnless(HAVE_OCR, "tesseract and pdftoppm not installed; OCR path exercised in the records-run workflow")
    def test_image_only_pdf_is_ocr_read_and_disclosed(self):
        scan = image_only_pdf(tiny_pdf("Scanned ALPR policy. Detections retained for 45 days. Shared with federal agencies."))
        (self.inbox / "scan.eml").write_bytes(synthetic_email(attachment=scan, filename="scan.pdf",
                                                              message_id="<pdf2@agency.example.invalid>"))
        tools, signature = local_ocr_tools()
        self.assertIsNotNone(tools)
        report = Pipeline(self.root, ocr_tools=tools, ocr_tool_signature=signature).run(self.inbox)
        self.assertEqual(report["originals"], 2)
        self.assertEqual(report["end_to_end_complete"], 2, json.dumps(report["subjects"]))
        pages = self.ledger("SELECT page_no,status,method,needs_visual_review,confidence FROM page_state")
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0][1], "partial")
        self.assertEqual(pages[0][2], "local-ocr-3")
        self.assertEqual(pages[0][3], 1)
        self.assertIsNotNone(pages[0][4])
        public = next(p for p in (self.root / "proposals/public").glob("*.md") if "45 days" in p.read_text()).read_text()
        self.assertIn("read by local OCR", public)
        envelope = json.loads(bytes(self.ledger(
            "SELECT a.payload FROM stage_content c JOIN stage_artifacts a ON a.sha256=c.content_sha256 "
            "WHERE c.stage='extract' AND c.subject_sha256=(SELECT original_sha256 FROM page_state)")[0][0]))
        self.assertEqual(envelope["ocr"]["pages"], [1])
        self.assertTrue(envelope["ocr"]["visual_review_pending"])
        # Replay with OCR configured again: no new receipts, no duplicate page rows.
        receipts = self.ledger("SELECT count(*) FROM receipts")[0][0]
        Pipeline(self.root, ocr_tools=tools, ocr_tool_signature=signature).run(self.inbox)
        self.assertEqual(self.ledger("SELECT count(*) FROM receipts")[0][0], receipts)
        self.assertEqual(len(self.ledger("SELECT page_no FROM page_state")), 1)

    def test_image_only_pdf_without_ocr_is_held_not_faked(self):
        if not shutil.which("pdftoppm"):
            self.skipTest("pdftoppm needed to build the image-only fixture")
        scan = image_only_pdf(tiny_pdf("Scanned policy with no text layer."))
        (self.inbox / "scan.eml").write_bytes(synthetic_email(attachment=scan, filename="scan.pdf",
                                                              message_id="<pdf3@agency.example.invalid>"))
        report = Pipeline(self.root).run(self.inbox)  # OCR not configured
        self.assertEqual(report["originals"], 2)
        self.assertEqual(report["end_to_end_complete"], 1)  # the email completes; the scan is held at extract
        held = [s for s in report["subjects"] if s["stages"].get("extract") not in ("done", None)]
        self.assertEqual(len(held), 1)
        self.assertIn("blocked", held[0]["stages"]["extract"])
        self.assertIn("ocr_runtime_unconfigured", held[0]["stages"]["extract"])
        self.assertEqual(self.ledger("SELECT count(*) FROM proposals")[0][0], 1)
        alerts = self.ledger("SELECT key,count,state FROM alerts")
        self.assertEqual(len(alerts), 1)
        self.assertIn("ocr_runtime_unconfigured", alerts[0][0])
        Pipeline(self.root).run(self.inbox)  # the next run counts the same gap, it does not duplicate it
        self.assertEqual(self.ledger("SELECT count,state FROM alerts"), [(2, "open")])


if __name__ == "__main__":
    unittest.main()
