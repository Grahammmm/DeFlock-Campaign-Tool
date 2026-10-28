"""extract: the intake worker runs in a subprocess on synthetic PDF/xlsx/zip bytes."""
import json
import unittest

from runner.client import FakeWorkspace
from runner.handlers import extract
from tests.runner.helpers import context, job, tempdir, tiny_pdf, tiny_xlsx, tiny_zip

try:
    import pypdf  # noqa: F401
    import openpyxl  # noqa: F401
    HAVE_PARSERS = True
except ImportError:
    HAVE_PARSERS = False


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_on(self, data, name, media="application/octet-stream", extra=None):
        ws = FakeWorkspace()
        sha = ws.put_original(data, media)["sha256"]
        ctx = context(self.tmp.name, job("extract", {"sha256": sha, "original_name": name, **(extra or {})}), ws)
        return ws, sha, extract.run(ctx)

    @unittest.skipUnless(HAVE_PARSERS, "pypdf/openpyxl not installed")
    def test_pdf(self):
        ws, sha, result = self.run_on(tiny_pdf(), "policy.pdf", "application/pdf", {"jurisdiction": "us-ca", "correspondence_id": "cor_1"})
        self.assertEqual(result.status, "done", result.error)
        row = result.outputs["extraction"]
        self.assertEqual(row["status"], "complete")
        self.assertEqual(row["page_count"], 1)
        self.assertEqual(row["pages_with_text"], 1)
        self.assertTrue(row["extractor"].startswith("pypdf"))
        text_sha = result.outputs["text_sha256"]
        self.assertIn(text_sha, ws.originals)
        units = [json.loads(l) for l in ws.originals[text_sha].decode().splitlines()]
        self.assertEqual(units[0]["kind"], "pdf_page")
        self.assertIn("Retained for 30 days", units[0]["text"])
        self.assertEqual(units[0]["locator"], {"page": 1})
        followups = result.outputs["followups"]
        self.assertEqual([f["kind"] for f in followups], ["digest"])
        self.assertEqual(followups[0]["inputs"], {"sha256": sha, "text_sha256": text_sha, "original_name": "policy.pdf", "jurisdiction": "us-ca"})

    @unittest.skipUnless(HAVE_PARSERS, "pypdf/openpyxl not installed")
    def test_xlsx(self):
        ws, sha, result = self.run_on(tiny_xlsx(), "search-log.xlsx")
        self.assertEqual(result.status, "done", result.error)
        row = result.outputs["extraction"]
        self.assertEqual(row["status"], "partial")  # workbook caveat issue is always recorded
        self.assertIn("workbook_cached_values_and_objects_not_reviewed", row["notes"])
        self.assertTrue(row["extractor"].startswith("openpyxl"))
        self.assertEqual(row["page_count"], 1)
        units = [json.loads(l) for l in ws.originals[result.outputs["text_sha256"]].decode().splitlines()]
        kinds = [u["kind"] for u in units]
        self.assertEqual(kinds[0], "workbook_sheet")
        self.assertEqual(units[1]["locator"], {"sheet": "log", "row": 1})
        self.assertIn("7ABC123", units[2]["text"])

    def test_zip_children_become_originals_with_receipts(self):
        data = tiny_zip({"inner/notes.txt": b"synthetic inner text", "ignored.py": b"print(1)"})
        ws, sha, result = self.run_on(data, "production.zip", extra={"correspondence_id": "cor_9"})
        self.assertEqual(result.status, "done", result.error)
        children = result.outputs["children"]
        self.assertEqual([c["name"] for c in children], ["inner/notes.txt"])
        self.assertIn("excluded_executable_or_software_source", result.outputs["extraction"]["notes"])
        child_sha = children[0]["sha256"]
        self.assertEqual(ws.originals[child_sha], b"synthetic inner text")
        self.assertEqual(len(ws.receipts), 1)
        receipt = next(iter(ws.receipts.values()))
        self.assertTrue(receipt["source_id"].startswith(sha + ":"))
        self.assertEqual(receipt["correspondence_id"], "cor_9")
        kinds = [(f["kind"], f["inputs"].get("sha256")) for f in result.outputs["followups"]]
        self.assertIn(("extract", child_sha), kinds)

    def test_plain_text_and_unsupported(self):
        ws, sha, result = self.run_on(b"line one\nline two\n", "reply.txt", "text/plain")
        self.assertEqual(result.status, "done")
        self.assertEqual(result.outputs["extraction"]["extractor"], "text")
        self.assertEqual(result.outputs["extraction"]["units"], 2)
        ws, sha, result = self.run_on(b"\x00\x01binary", "blob.qqq")
        self.assertEqual(result.status, "done")
        self.assertEqual(result.outputs["extraction"]["status"], "unsupported")
        self.assertEqual(result.outputs["followups"], [])

    def test_hash_mismatch_is_refused_by_client(self):
        ws = FakeWorkspace()
        ws.originals["a" * 64] = b"wrong bytes"
        ctx = context(self.tmp.name, job("extract", {"sha256": "a" * 64, "original_name": "x.txt"}), ws)
        result = extract.run(ctx)
        # the worker verifies the source hash itself and reports a failed stage
        self.assertEqual(result.status, "failed")
        self.assertIn("extraction_error", result.error)


if __name__ == "__main__":
    unittest.main()
