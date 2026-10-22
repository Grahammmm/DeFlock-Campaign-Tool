"""M9: tool output must account for exactly the verified PDF's pages."""
import hashlib
import io
from pathlib import Path
import unittest
from pypdf import PdfWriter
from campaign_tool.records import ocr_stage as stage
from tests.records import test_ocr_stage as fixtures


def pdf(pages=3, encrypted=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    if encrypted:
        writer.encrypt('synthetic')
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


class PageCountTests(unittest.TestCase):
    def run_case(self, raw, texts):
        ws = fixtures.Workspace(self)
        digest = ws.add('source.pdf', raw)
        ws.write_queue()
        config = ws.config(ocrmypdf=fixtures.fake_ocrmypdf(ws.tmp))
        tools = fixtures.FakeTools({digest: texts})
        result = stage.run(ws.args(config), runner=tools)
        return ws, tools, result

    def test_short_and_long_sidecars_never_publish_complete_or_call_vision(self):
        for count in (0, 1, 2, 4):
            with self.subTest(sidecar_pages=count):
                ws, tools, (_, _, outcomes) = self.run_case(pdf(), [fixtures.CLEAN] * count)
                self.assertEqual((outcomes[0].status, outcomes[0].code),
                                 ('ocr_failed', 'ocrmypdf_sidecar_inconsistent'))
                self.assertEqual(fixtures.results(ws.out), {})
                self.assertEqual(tools.count('pdftoppm'), 0)

    def test_exact_page_count_retains_blank_and_native_pages_unreviewed(self):
        ws, _, (_, _, outcomes) = self.run_case(pdf(), [fixtures.CLEAN, '', stage.skipped_marker(3)])
        self.assertEqual(outcomes[0].status, 'ocr_partial')
        record = next(iter(fixtures.results(ws.out).values()))
        self.assertEqual(record['page_count'], 3)
        self.assertEqual(record['unresolved_pages'], 1)
        self.assertEqual(record['review_state'], 'not_reviewed')
        self.assertEqual(record['pages'][2]['status'], 'native_text')

    def test_unreadable_empty_and_encrypted_pdf_fail_before_tool(self):
        for raw in (b'%PDF-1.4\nnot a PDF', pdf(0), pdf(encrypted=True)):
            with self.subTest(source=hashlib.sha256(raw).hexdigest()):
                ws, tools, (_, _, outcomes) = self.run_case(raw, [fixtures.CLEAN])
                self.assertEqual((outcomes[0].status, outcomes[0].code),
                                 ('ocr_failed', 'pdf_page_count_unverified'))
                self.assertEqual(tools.calls, [])
                self.assertEqual(fixtures.results(ws.out), {})

    def test_old_sidecar_only_cache_identity_is_not_reused(self):
        from unittest.mock import patch
        ws = fixtures.Workspace(self)
        primary = stage.OcrmypdfBackend(executable=fixtures.fake_ocrmypdf(ws.tmp))
        processor = stage.OcrStage(ws.out, primary)
        old_signature = stage._sha(stage._canonical({
            'schema': stage.SCHEMA, 'sidecar_parser': stage.SIDECAR_PARSER,
            'primary': primary.identity(), 'fallback': None,
            'quality': {'min_chars': stage.Quality().min_chars, 'min_alnum_ratio': stage.Quality().min_alnum_ratio}}))
        self.assertNotEqual(processor.signature, old_signature)
