"""REC-AUTO-2: emailed attachments reach an explicit outcome, and a long scan resumes over bounded runs.

Synthetic fixtures only (nothing here is a copy of a real record). The 25-page scan uses the real
local Tesseract/pdftoppm through the existing per-page OCR route and is skipped where they are not
installed, like ``test_run_pdf_ocr``.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zlib

from campaign_tool.records import arrival
from tests.records.test_native_extract import (HAVE_PARSERS, ExtractStageCase, run_main, scanned_pdf, tiny_docx,
                                               tiny_pdf)

HAVE_OCR = all(shutil.which(tool) for tool in ("tesseract", "pdftoppm"))
PAGES = 25


def multi_page_scan(count=PAGES, dpi=150):
    """A ``count``-page image-only PDF: each page is a bitmap of its own distinct sentence."""
    bitmaps = []
    with tempfile.TemporaryDirectory() as scratch:
        for number in range(1, count + 1):
            source = Path(scratch) / f"p{number}.pdf"
            source.write_bytes(tiny_pdf(f"Synthetic scanned page {number} of {count}. Retention is {number + 10} days."))
            subprocess.run(["pdftoppm", "-gray", "-r", str(dpi), "-singlefile", str(source),
                            str(Path(scratch) / f"b{number}")], check=True, capture_output=True, timeout=60)
            pgm = (Path(scratch) / f"b{number}.pgm").read_bytes()
            header, rest = pgm.split(b"\n", 1)
            assert header == b"P5"
            dims, rest = rest.split(b"\n", 1)
            width, height = (int(v) for v in dims.split())
            _, raw = rest.split(b"\n", 1)
            bitmaps.append((width, height, zlib.compress(raw)))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               ("<< /Type /Pages /Kids [" + " ".join(f"{3 + 3 * i} 0 R" for i in range(count))
                + f"] /Count {count} >>").encode()]
    for index, (width, height, data) in enumerate(bitmaps):
        page_w, page_h = width * 72 / dpi, height * 72 / dpi
        base = 3 + 3 * index
        content = f"q {page_w:.2f} 0 0 {page_h:.2f} 0 0 cm /Im0 Do Q".encode()
        objects += [
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w:.2f} {page_h:.2f}] "
             f"/Resources << /XObject << /Im0 {base + 2} 0 R >> >> /Contents {base + 1} 0 R >>").encode(),
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
            (f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceGray "
             f"/BitsPerComponent 8 /Filter /FlateDecode /Length {len(data)} >>\nstream\n").encode() + data + b"\nendstream"]
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


@unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
class AttachmentArrivalTests(ExtractStageCase):
    POLICY = tiny_pdf("Synthetic policy text.")

    @property
    def attachments(self):
        # The duplicate attachment arrives in a second message: one original, two occurrences.
        return (("policy.pdf", self.POLICY), ("copy-of-policy.pdf", self.POLICY), ("scan.pdf", scanned_pdf()),
                ("schedule.docx", tiny_docx()), ("blob.bin", b"\x00\x01\x02 not a known format"))

    def test_arrival_preserves_and_accounts_for_all_attachment_parts(self):
        runtime = Path(self.temp.name) / "runtime"
        runtime.mkdir(mode=0o700)
        stamp = arrival.stamp(runtime, "inbox-watch", hashlib.sha256(b"mail").hexdigest())
        self.assertEqual(len(arrival.pending(runtime)), 1, stamp)
        code, parsed, raw = self.stage()
        self.assertContract(code, parsed, raw)
        listed = json.loads((self.processing / "runs" / "extract-run-t11k.json").read_text())["documents"]
        manifest = json.loads((self.processing / "runs" / "extract-failures-run-t11k.json").read_text())
        pending = {sha for (sha,) in self.query("SELECT original_sha256 FROM stage_state WHERE stage='extract'")}
        self.assertEqual({d["original_sha256"] for d in listed}, pending, "every preserved original has an outcome")
        for name, payload in self.payloads.items():
            sha = hashlib.sha256(payload).hexdigest()
            self.assertEqual(self.query("SELECT count(*) FROM originals WHERE sha256=?", (sha,)), [(1,)], name)
            self.assertEqual({d["original_sha256"] for d in listed if d["original_sha256"] == sha}, {sha}, name)
        policy = hashlib.sha256(self.POLICY).hexdigest()
        self.assertEqual(self.query("SELECT count(*) FROM occurrences WHERE original_sha256=?", (policy,)), [(2,)])
        self.assertEqual(len([d for d in listed if d["original_sha256"] == policy]), 1)
        self.assertEqual(manifest["extracted"] + len(manifest["documents"]), len(listed))
        # Held attachments are named, never silent.
        by_sha = {d["original_sha256"]: d for d in manifest["documents"]}
        scan = by_sha[hashlib.sha256(self.payloads["scan.pdf"]).hexdigest()]
        blob = by_sha[hashlib.sha256(self.payloads["blob.bin"]).hexdigest()]
        self.assertEqual((scan["outcome"], scan["reason_code"]), ("AWAITING_OCR", "ocr_runtime_unconfigured"),
                         "a native-only target holds the scan by name")
        self.assertEqual((blob["outcome"], blob["reason_code"]), ("DECODER_NEEDED", "decoder_needed"))
        self.assertNotIn(policy, by_sha)
        self.assertEqual(len(arrival.pending(runtime)), 1, "extraction never consumes or edits arrival stamps")


@unittest.skipUnless(HAVE_PARSERS and HAVE_OCR, "tesseract/pdftoppm/pypdf not installed")
class LongScanResumeTests(ExtractStageCase):
    @property
    def attachments(self):
        self.scan = multi_page_scan()
        return (("scan25.pdf", self.scan),)

    def ocr_stage(self, run_id):
        return run_main(["extract", "--project", str(self.project), "--timeout", "120", "--run-id", run_id,
                         "--ocr", "--ocr-page-timeout", "10", "--document-timeout", "25"])

    def receipts(self):
        return len(list((self.processing / "ocr").rglob("receipt.json")))

    def test_25_page_scan_resumes_without_losing_denominator(self):
        sha = hashlib.sha256(self.scan).hexdigest()
        self.assertEqual(self.query("SELECT count(*) FROM originals WHERE sha256=?", (sha,)), [(1,)])
        seen, progress = [], []
        for number in range(1, 40):
            code, parsed, raw = self.ocr_stage(f"run-ocr{number}")
            self.assertContract(code, parsed, raw)
            manifest = json.loads((self.processing / "runs" / f"extract-failures-run-ocr{number}.json").read_text())
            done = self.query("SELECT status FROM stage_state WHERE original_sha256=? AND stage='extract'", (sha,))[0][0]
            progress.append(self.receipts())
            seen.append(manifest)
            if done == "done":
                break
            row = next(d for d in manifest["documents"] if d["original_sha256"] == sha)
            self.assertEqual((row["outcome"], row["disposition"]), ("DEFERRED_RUN_BUDGET", "deferred"), row)
            self.assertGreaterEqual(row["ocr_pages_done"], 1)
            self.assertEqual(len(row["ocr_pages_pending"]) + progress[-1], PAGES, "denominator kept across runs")
        else:
            self.fail("25-page scan did not finish within 39 bounded runs")
        self.assertGreater(len(seen), 1, "a tight budget cannot finish 25 pages in one call")
        self.assertEqual(seen[-1]["documents"], [], "the finished run holds nothing")
        pages = self.query("SELECT page_no,status,needs_visual_review FROM page_state ORDER BY page_no")
        self.assertEqual([p[0] for p in pages], list(range(1, PAGES + 1)), "no page omitted, duplicated or renumbered")
        self.assertEqual({(p[1], p[2]) for p in pages}, {("partial", 1)}, "OCR pages stay unreviewed")
        self.assertEqual(self.receipts(), PAGES)
        for number in (1, 25):
            texts = [json.loads(Path(path).read_bytes())["text"] for (path,) in self.query(
                "SELECT derived_path FROM units WHERE original_sha256=?", (sha,))]
            self.assertTrue(any(str(number) in text for text in texts), number)
        # A completed document is not OCRed again.
        before = self.receipts()
        code, parsed, raw = self.ocr_stage("run-ocr-after")
        self.assertContract(code, parsed, raw)
        self.assertEqual((self.receipts(), parsed["counts"]["input"]), (before, 0))


if __name__ == "__main__":
    unittest.main()
