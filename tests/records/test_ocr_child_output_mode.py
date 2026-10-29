"""OCR child output under a default ACL: valid page receipts must stay enrollable.

On a records tree with a default ACL the kernel ignores the OCR children's umask, so
pdftoppm's ``page.png`` landed group-accessible (0660) while every engine-written
artifact is 0600. Enrollment reads OCR closure artifacts only when they are owner-only,
so valid OCR'd PDFs were refused as ``extraction_check_refused``. The fake runner below
reproduces that mode directly (synthetic fixtures only; no ACL tooling, no real OCR).
"""
import io
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import ocr_closure
from campaign_tool.records.extract import ocr
from campaign_tool.records.extraction_ledger import ExtractionBindingError, sha
from tests.records import ocr_fixtures
from tests.records import test_extraction_ledger as ledger_fixtures

ACL_MODE = 0o660  # what a 0666 create gets under a default ACL whose mask is rwx


class AclChildRunner(ocr_fixtures.FakeOCRRunner):
    """pdftoppm output lands 0660, as under the records workspace's default ACL."""

    def __call__(self, command, **kwargs):
        result = super().__call__(command, **kwargs)
        if Path(command[0]).name == "pdftoppm":
            os.chmod(command[-1] + ".png", ACL_MODE)
        return result


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class OcrChildOutputModeTests(unittest.TestCase):
    def setUp(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest("pypdf optional parser unavailable")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "blobs").mkdir()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        stream = io.BytesIO()
        writer.write(stream)
        self.raw = stream.getvalue()
        self.sha = hashlib.sha256(self.raw).hexdigest()
        (self.root / "blobs" / self.sha).write_bytes(self.raw)
        self.output = self.root / "private-ocr"

    def call(self, runner):
        return ocr.extract_image_only_pages(self.root, self.sha, self.output, tool_signature="stub-v1",
                                            runner=runner, tools=ocr_fixtures.TOOLS)

    def final(self, receipt):
        return self.output / self.sha / f"page-{receipt['locator']['page']:06d}" / receipt["receipt_id"]

    def closure(self, receipt):
        return ocr_closure.closure_artifacts(self.output, self.sha, receipt["locator"]["page"],
                                             receipt["receipt_id"])

    def test_group_accessible_child_raster_is_published_owner_only(self):
        receipt = self.call(AclChildRunner())[0]
        self.assertEqual(receipt["status"], "visual_check_queued")
        self.assertEqual(set(receipt["artifact_sha256"]), set(ocr.ARTIFACTS))
        final = self.final(receipt)
        for entry in final.iterdir():
            self.assertEqual(mode(entry) & 0o077, 0, entry.name)
        self.assertEqual(mode(final / "page.png"), 0o600)
        # The enrollment closure (strict owner-only reads) now verifies the receipt.
        self.assertIn("ocr_asset:" + receipt["receipt_id"] + ":page.png", self.closure(receipt))

    def test_reused_receipt_left_group_accessible_is_tightened(self):
        first = ocr_fixtures.FakeOCRRunner()
        receipt = self.call(first)[0]
        png = self.final(receipt) / "page.png"
        before = png.read_bytes()
        os.chmod(png, ACL_MODE)  # a receipt published before the fix
        with self.assertRaisesRegex(ExtractionBindingError, "artifact_permission_or_size"):
            self.closure(receipt)  # the closure check itself stays strict
        again = ocr_fixtures.FakeOCRRunner()
        self.assertEqual(self.call(again), [receipt])
        self.assertEqual(again.calls, [], "the published receipt is reused, not re-OCR'd")
        self.assertEqual(mode(png), 0o600)
        self.assertEqual(png.read_bytes(), before)
        self.closure(receipt)

    def test_child_symlink_raster_is_still_a_blocked_page(self):
        class SymlinkRunner(ocr_fixtures.FakeOCRRunner):
            def __call__(self, command, **kwargs):
                if Path(command[0]).name == "pdftoppm":
                    Path(command[-1] + ".png").symlink_to(self.target)
                    return subprocess.CompletedProcess(command, 0, "", "")
                return super().__call__(command, **kwargs)

        target = self.root / "elsewhere.png"
        target.write_bytes(b"synthetic raster")
        os.chmod(target, ACL_MODE)
        runner = SymlinkRunner()
        runner.target = target
        receipt = self.call(runner)[0]
        self.assertEqual(receipt["status"], "blocked")
        self.assertNotIn("page.png", receipt["artifact_sha256"])
        self.assertEqual(mode(target), ACL_MODE, "a symlink target is never chmod-ed")


class OcrChildOutputEnrollmentTests(unittest.TestCase):
    """The reported path: a per-page OCR'd PDF enrolls instead of ocr_receipt_unverified."""

    setUpClass = classmethod(ledger_fixtures.ExtractionLedgerTests.setUpClass.__func__)
    setUp = ledger_fixtures.ExtractionLedgerTests.setUp
    tearDown = ledger_fixtures.ExtractionLedgerTests.tearDown
    ocr_bundle = ledger_fixtures.ExtractionLedgerTests.ocr_bundle

    def test_ocr_pdf_with_acl_mode_raster_enrolls(self):
        with patch.object(ocr_fixtures, "FakeOCRRunner", AclChildRunner):
            source, receipt, result, ocr_root = self.ocr_bundle()
        entry = result["ocr_receipts"][0]
        png = ocr_root / result["original_sha256"] / f"page-{entry['page']:06d}" / entry["receipt_id"] / "page.png"
        self.assertEqual(mode(png), 0o600)
        enrolled = self.adapter.enroll(original_path=source, receipt_path=receipt,
                                       receipt_sha256=sha(receipt.read_bytes()))
        self.assertIn("ocr_visual_review_required", enrolled["gaps"])
        self.assertEqual(enrolled["pages"], 2)

    def test_acl_mode_artifact_without_a_reuse_pass_still_refuses(self):
        source, receipt, result, ocr_root = self.ocr_bundle()
        entry = result["ocr_receipts"][0]
        png = ocr_root / result["original_sha256"] / f"page-{entry['page']:06d}" / entry["receipt_id"] / "page.png"
        os.chmod(png, ACL_MODE)
        with self.assertRaisesRegex(ExtractionBindingError, "ocr_receipt_unverified"):
            self.adapter.enroll(original_path=source, receipt_path=receipt,
                                receipt_sha256=sha(receipt.read_bytes()))


if __name__ == "__main__":
    unittest.main()
