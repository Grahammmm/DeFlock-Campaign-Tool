"""X6 phase-1 S1-S8: actual generated PDFs, offline deterministic OCR only."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pypdf import PdfReader, PageObject
from campaign_tool.records import extraction_routes as routes, pdf_eligibility as policy
from campaign_tool.records.extract import ocr
from tests.records import test_x6_adversarial as repro
from tests.records.ocr_stage_fixtures import _pdf, _stream, native_text_pdf, noise_page, mixed_pdf
from tests.records.test_ocr_stage import Workspace, fake_ocrmypdf, results
from tests.records.ocr_stage_fixtures import FakeTools, FakeVision
from campaign_tool.records import ocr_stage

BODY = 'The scanned body contains synthetic policy language, not just a Bates stamp.\n'


def fixture(text='Approved', image=None, before=b'', after=b'', invisible=False, unicode_hex=None):
    """One page, known text and image geometry; image is a placement command."""
    font = b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 7 0 R >>'
    mapping = (unicode_hex or '0041').encode('ascii')
    cmap = (b'/CIDInit /ProcSet findresource begin 12 dict begin begincmap '
            b'/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def '
            b'/CMapName /X6 def /CMapType 2 def 1 begincodespacerange <01> <01> endcodespacerange '
            b'1 beginbfchar <01> <' + mapping + b'> endbfchar endcmap CMapName currentdict /CMap defineresource pop end end')
    text_op = b'<01>' if unicode_hex else b'(' + text.encode('ascii') + b')'
    content = before + (image or b'') + b' BT /F1 10 Tf 20 20 Td ' + (b'3 Tr ' if invisible else b'') + text_op + b' Tj ET ' + after
    return _pdf([b'<< /Type /Catalog /Pages 2 0 R >>',
                 b'<< /Type /Pages /Kids [4 0 R] /Count 1 >>', font,
                 b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> /XObject << /Im0 6 0 R >> >> /Contents 5 0 R >>',
                 _stream(b'', content),
                 _stream(b'/Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8', b'\xff'),
                 _stream(b'', cmap)])

FULL = b'q 612 0 0 792 0 0 cm /Im0 Do Q '
SMALL = b'q 20 0 0 10 400 400 cm /Im0 Do Q '


class Contract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='x6-contract-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.serial = 0

    def evaluate(self, raw):
        page = PdfReader(io.BytesIO(raw), strict=False).pages[0]
        return policy.probe(page, hashlib.sha256(raw).hexdigest(), 1, page.extract_text() or '')

    def direct(self, raw, runner=None):
        self.serial += 1
        intake, out = self.root / f'intake-{self.serial}', self.root / f'ocr-{self.serial}'
        (intake / 'blobs').mkdir(parents=True)
        sha = hashlib.sha256(raw).hexdigest()
        (intake / 'blobs' / sha).write_bytes(raw)
        runner = runner or repro.StubRunner(text=BODY)
        receipt, = ocr.extract_image_only_pages(intake, sha, out, tool_signature='stub 1', tools=repro.TOOLS, runner=runner)
        self.assertEqual((receipt['source']['sha256'], receipt['locator'], receipt['page_count']), (sha, {'page':1}, 1))
        self.assertEqual((intake / 'blobs' / sha).read_bytes(), raw)
        self.assertEqual(receipt['review_status'], 'not_reviewed')
        self.assertEqual(receipt['fidelity_status'], 'not_checked')
        return receipt, ocr.load_page_receipt(out, sha, 1, receipt['receipt_id']), runner

    def routed(self, raw, configured=False):
        self.serial += 1
        source = self.root / f'source-{self.serial}.pdf'
        source.write_bytes(raw)
        out = self.root / f'routes-{self.serial}'
        out.mkdir(mode=0o700)
        options = {}
        runner = repro.StubRunner(text=BODY)
        if configured:
            ocr_out = self.root / f'routes-ocr-{self.serial}'
            ocr_out.mkdir(mode=0o700)
            options = dict(ocr_output_root=ocr_out, ocr_tools=repro.TOOLS, tool_signature='stub 1', ocr_runner=runner)
        result = routes.extract(source, hashlib.sha256(raw).hexdigest(), out, 'pdf', **options)
        self.assertEqual(source.read_bytes(), raw)
        self.assertEqual(len(result['pages']), 1)
        self.assertEqual([r['page_no'] for r in result['pages']], [1])
        self.assertEqual(result['review_status'], 'not_reviewed')
        return result, runner

    def required(self, raw):
        e = self.evaluate(raw)
        self.assertEqual((e['decision'], e['painted_image_present']), ('ocr_required', True))
        self.assertIsNone(e['text_union_area'])
        self.assertIsNone(e['uncovered_image_area'])
        r, loaded, runner = self.direct(raw)
        self.assertEqual(r['status'], 'ocr_text_unreviewed')
        self.assertEqual(r['artifact_sha256'].keys(), set(ocr.ARTIFACTS))
        self.assertEqual(loaded['sidecar'].decode(), BODY)
        self.assertEqual([Path(c[0]).name for c in runner.calls], ['pdftoppm', 'tesseract'])
        self.assertEqual(r['eligibility'], e)
        result, routed_runner = self.routed(raw, True)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['pages'][0]['reason'], 'ocr_visual_review_required')
        self.assertEqual(result['units'][0]['text'], BODY)
        native = Path(result['run_path']) / 'derived' / 'units.jsonl'
        self.assertNotEqual(json.loads(native.read_text())['text'], BODY)
        self.assertEqual([Path(c[0]).name for c in routed_runner.calls], ['pdftoppm', 'tesseract'])
        return e

    def sufficient(self, raw):
        e = self.evaluate(raw)
        self.assertEqual((e['decision'], e['painted_image_present'], e['probe_complete']), ('native_sufficient', False, True))
        native = PdfReader(io.BytesIO(raw)).pages[0].extract_text()
        result, runner = self.routed(raw, True)
        self.assertEqual((result['status'], result['pages'][0]['status'], result['units'][0]['text']), ('complete', 'ok', native))
        self.assertEqual(runner.calls, [])
        r, loaded, runner = self.direct(raw)
        self.assertEqual((r['status'], r['artifact_sha256'], r['confidence'], loaded['sidecar'], runner.calls), ('skipped_machine_text', {}, None, None, []))

    def test_S1_bates_scan_upstream_direct_and_merge(self):
        raw = repro.scan_with_text_layer('BATES-000123')
        e = self.required(raw)
        self.assertEqual(e['image_union_area'], 612 * 792)
        self.assertEqual(e['image_rectangles'], [[0.,0.,612.,792.]])
        result, runner = self.routed(raw)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['units'][0]['text'], 'BATES-000123')
        self.assertTrue(result['units'][0]['data']['ocr_needed'])
        self.assertIn('ocr_runtime_unconfigured', [i['code'] for i in result['issues']])
        self.assertEqual(runner.calls, [])

    def test_S2_header_footer_long_header(self):
        for header in ('Header', 'Footer 1', 'Long native header ' * 100):
            with self.subTest(header=header[:20]):
                self.required(fixture(header, FULL))

    def test_S3_small_scanned_table_with_long_paragraph(self):
        e = self.required(fixture('Native paragraph ' * 200, SMALL))
        self.assertEqual(e['image_union_area'], 200)

    def test_S4_sparse_native_word_number_unicode_symbol(self):
        for raw in (native_text_pdf(['Approved']), native_text_pdf(['7']), fixture(unicode_hex='4E2D6587'), fixture(unicode_hex='26032605'), fixture(unicode_hex='00650301')):
            with self.subTest(sha=hashlib.sha256(raw).hexdigest()):
                self.sufficient(raw)

    def test_S5_unused_offpage_and_fully_clipped_image(self):
        for raw in (fixture(), fixture(image=b'q 10 0 0 10 900 900 cm /Im0 Do Q '),
                    fixture(image=b'q 0 0 1 1 re W n 10 0 0 10 50 50 cm /Im0 Do Q ')):
            with self.subTest(sha=hashlib.sha256(raw).hexdigest()):
                self.sufficient(raw)

    def test_S6_junk_actual_pdf_decoding_and_mocked_text(self):
        for code in ('FFFD', '0000', '200B', 'FEFF', '002000200009'):
            raw = fixture(image=FULL, unicode_hex=code)
            self.assertFalse(policy.usable_text(PdfReader(io.BytesIO(raw)).pages[0].extract_text()))
            self.required(raw)
        for text in ('\ufffd','\0','\u200b','\ufeff',' \n\t'):
            with self.subTest(text=repr(text)), mock.patch.object(PageObject, 'extract_text', return_value=text):
                r, loaded, runner = self.direct(fixture(image=FULL))
                self.assertEqual(r['status'], 'ocr_text_unreviewed')
                self.assertEqual(loaded['sidecar'].decode(), BODY)
                self.assertEqual(len(runner.calls), 2)

    def test_S7_invisible_overlay_does_not_exempt_image(self):
        self.required(fixture('Text overlay ' * 400, FULL, invisible=True))
        # Predicate does not consume or trust a producer's invented text bbox.
        page = PdfReader(io.BytesIO(fixture(image=FULL))).pages[0]
        with mock.patch.object(page, 'extract_text', return_value='page-wide text overlay'):
            self.assertEqual(policy.probe(page, 'a'*64, 1, page.extract_text())['decision'], 'ocr_required')

    def test_junk_predicate_without_image_cannot_be_masked_by_image_rule(self):
        for code in ('FFFD', '0000', '200B', 'FEFF', '002000200009'):
            with self.subTest(code=code):
                raw = fixture(unicode_hex=code)
                self.assertEqual(self.evaluate(raw)['decision'], 'ocr_required')
                r, loaded, runner = self.direct(raw)
                self.assertEqual(r['status'], 'ocr_text_unreviewed')
                self.assertEqual(loaded['sidecar'].decode(), BODY)
                self.assertEqual(len(runner.calls), 2)

    def test_overlap_does_not_inflate_measured_union(self):
        e = self.evaluate(fixture(image=SMALL+SMALL))
        self.assertEqual(e['image_union_area'], 200)
        self.assertEqual(e['decision'], 'ocr_required')

    def test_S8_white_background_logo_photo_prior_ocr_are_conservative(self):
        for raw in (fixture('Approved', FULL), fixture('Photo caption', SMALL), repro.scan_with_text_layer('Prior OCR layer with all words')):
            with self.subTest(sha=hashlib.sha256(raw).hexdigest()):
                self.required(raw)

    def test_unknown_clip_is_never_native_sufficient(self):
        raw = fixture(before=b'0 0 m 10 10 l W n ')
        self.assertEqual(self.evaluate(raw)['decision'], 'unresolved')
        r, loaded, runner = self.direct(raw)
        self.assertEqual((r['status'], r['blocked_reason'], runner.calls), ('blocked', 'page_eligibility_unresolved', []))

    def test_queue_skip_requires_positive_native_evidence(self):
        for raw, expected in ((fixture(), 'ocr_complete'), (fixture(image=FULL), 'ocr_failed'),
                              (fixture(before=b'0 0 m 10 10 l W n '), 'ocr_failed')):
            ws = Workspace(self)
            sha = ws.add('input.pdf', raw)
            ws.write_queue()
            config = ws.config(ocrmypdf=fake_ocrmypdf(ws.tmp))
            tools, vision = FakeTools({sha:[None]}), FakeVision()
            _, _, outcomes = ocr_stage.run(ws.args(config), runner=tools, transport=vision)
            self.assertEqual(outcomes[0].status, expected)
            self.assertEqual(vision.requests, [])
            self.assertEqual((ws.sources / 'input.pdf').read_bytes(), raw)
            if expected == 'ocr_complete':
                record = results(ws.out)[sha]
                self.assertEqual(record['pages'][0]['status'], 'native_text')
                self.assertEqual(record['pages'][0]['eligibility']['decision'], 'native_sufficient')
                self.assertEqual(record['review_state'], 'not_reviewed')
            else:
                self.assertEqual(outcomes[0].code, 'page_eligibility_unresolved')
                self.assertEqual(results(ws.out), {})


if __name__ == '__main__':
    unittest.main()
