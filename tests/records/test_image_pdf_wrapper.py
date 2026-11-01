"""Synthetic D-E wrapper acceptance: no records, tools or services are contacted."""
import hashlib
import io
import unittest
from PIL import Image
from campaign_tool.records import extraction_routes as routes
from contextlib import closing


def image_bytes(form='PNG', changed=False, frames=False):
    out = io.BytesIO()
    with Image.new('RGB', (32, 32), 'white') as image:
        if changed:
            image.putpixel((0, 0), (0, 0, 0))
        if frames:
            with Image.new('RGB', (32, 32), 'black') as second:
                image.save(out, form, save_all=True, append_images=[second])
        else:
            image.save(out, form)
    return out.getvalue()


class WrapperTests(unittest.TestCase):
    def test_deterministic_single_page_parent_binding(self):
        raw = image_bytes()
        self.assertTrue(hasattr(routes, 'image_pdf_wrapper'), 'D-E image wrapper is missing')
        first, record = routes.image_pdf_wrapper(raw)
        from unittest import mock
        with mock.patch('time.time', return_value=1700000000):
            second, repeated = routes.image_pdf_wrapper(raw)
        self.assertEqual(first, second)
        self.assertEqual(record, repeated)
        self.assertEqual(set(record), {'schema', 'parent_sha256', 'derived_sha256',
                                       'method', 'pillow_version', 'frame'})
        self.assertEqual(record['parent_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(record['derived_sha256'], hashlib.sha256(first).hexdigest())
        self.assertEqual((record['schema'], record['method'], record['frame']),
                         ('image-pdf-wrapper-v1', 'pillow-pdf', 1))
        altered, changed = routes.image_pdf_wrapper(image_bytes(changed=True))
        self.assertNotEqual(first, altered)
        self.assertNotEqual(record['derived_sha256'], changed['derived_sha256'])
        from pypdf import PdfReader
        self.assertEqual(len(PdfReader(io.BytesIO(first)).pages), 1)


class ImageStageTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from pathlib import Path
        os.umask(0o077)
        self.tmp = tempfile.TemporaryDirectory(prefix='image-wrapper-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'root'

    def test_png_and_jpeg_extract_on_parent_with_review_hold(self):
        import json
        import sqlite3
        import time
        from pathlib import Path
        from unittest import mock
        from campaign_tool.records import native_extract
        from campaign_tool.records.run import Pipeline
        from tests.records import ocr_fixtures
        from tests.records.test_run_slice import synthetic_email
        inbox = self.base / 'inbox'
        inbox.mkdir(mode=0o700)
        subjects = []
        for form, ext in [('PNG', 'png'), ('JPEG', 'jpg')]:
            raw = image_bytes(form, changed=(form == 'JPEG'))
            subjects.append(hashlib.sha256(raw).hexdigest())
            (inbox / (ext + '.eml')).write_bytes(synthetic_email(
                attachment=raw, filename='image.' + ext,
                message_id=f'<wrapper-{ext}@agency.example.invalid>'))
        Pipeline(self.root).ingest_inbox(inbox)
        runner = ocr_fixtures.FakeOCRRunner(text='Synthetic image OCR\n', confidence='91')
        original = routes.extract
        def injected(*args, **kwargs):
            return original(*args, **dict(kwargs, ocr_runner=runner))
        with mock.patch.object(routes, 'extract', side_effect=injected):
            result = native_extract.run(self.root, run_id='image-ocr', deadline=time.monotonic()+1400,
                document_timeout=1200, ocr={'tools': ocr_fixtures.TOOLS, 'tool_signature': 'stub-v1',
                                            'page_timeout': 180})
        self.assertEqual(result['outcomes'][native_extract.EXTRACTED], 4, result)
        self.assertEqual(result['awaiting_ocr_images'], 0)
        with closing(sqlite3.connect(self.root / 'ledger.sqlite')) as con, con:
            for subject in subjects:
                self.assertEqual(con.execute('SELECT page_no,needs_visual_review FROM page_state '
                    'WHERE original_sha256=?', (subject,)).fetchall(), [(1, 1)])
                texts = [json.loads(Path(path).read_bytes())['text'] for path, in con.execute(
                    'SELECT derived_path FROM units WHERE original_sha256=?', (subject,))]
                self.assertEqual(texts, ['Synthetic image OCR\n'])
                manifest = json.loads(con.execute('SELECT manifest_json FROM extraction_adapter_imports '
                    'WHERE original_sha256=?', (subject,)).fetchone()[0])
                receipt = json.loads(Path(manifest['artifacts']['source_receipt']['path']).read_bytes())
                self.assertEqual(receipt['derived']['parent_sha256'], subject)
                self.assertEqual(receipt['children'], [])
                self.assertEqual(receipt['route']['route'], 'extract:image-pdf-wrapper')
                self.assertEqual(receipt['units'][0]['locator'], {'page': 1})
                self.assertEqual(con.execute('SELECT status FROM stage_state WHERE original_sha256=? '
                    "AND stage='extract'", (subject,)).fetchone()[0], 'done')
        self.assertEqual(runner.calls, ['pdftoppm', 'tesseract']*2)
        import stat
        derivatives = list((self.root/'ocr'/'derived-pdf-v1'/'blobs').iterdir())
        self.assertEqual(len(derivatives), 2)
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o400 for path in derivatives))
        self.assertEqual(len(list((self.root/'ocr').glob('*/page-000001/*/receipt.json'))), 2)

    def extract_image(self, raw=None, form='png', enabled=True):
        from tests.records import ocr_fixtures
        source = self.base / 'input-image'
        source.write_bytes(raw if raw is not None else image_bytes())
        source.chmod(0o400)
        subject = hashlib.sha256(source.read_bytes()).hexdigest()
        out = self.base / 'out'
        ocr = self.base / 'ocr'
        out.mkdir(mode=0o700, exist_ok=True)
        ocr.mkdir(mode=0o700, exist_ok=True)
        options = dict(ocr_output_root=ocr, ocr_tools=ocr_fixtures.TOOLS,
                       tool_signature='stub-v1', ocr_runner=ocr_fixtures.FakeOCRRunner(confidence='91'))
        result = routes.extract(source, subject, out, form, timeout=30, **(options if enabled else {}))
        return source, subject, result

    def test_derivative_and_ocr_are_reused_write_once(self):
        from pathlib import Path
        from tests.records import ocr_fixtures
        source, subject, first = self.extract_image()
        root = Path(first['ocr_output_root'])
        pdf = root/'derived-pdf-v1'/'blobs'/first['derived']['derived_sha256']
        before = (pdf.read_bytes(), pdf.stat().st_mtime_ns)
        runner = ocr_fixtures.FakeOCRRunner(confidence='91')
        second = routes.extract(source, subject, self.base/'out', 'png', timeout=30,
            ocr_output_root=root, ocr_tools=ocr_fixtures.TOOLS, tool_signature='stub-v1', ocr_runner=runner)
        self.assertEqual((pdf.read_bytes(), pdf.stat().st_mtime_ns), before)
        self.assertEqual(second['derived'], first['derived'])
        self.assertEqual(second['units'], first['units'])
        self.assertEqual(runner.calls, [])
        held = routes.extract(source, subject, self.base/'out', 'png', timeout=30,
            document_budget=30, ocr_attempt_id='no-budget', ocr_output_root=root,
            ocr_tools=ocr_fixtures.TOOLS, tool_signature='stub-v1', ocr_runner=runner)
        self.assertEqual(held['status'], 'blocked')
        self.assertEqual(held['issues'][-1]['ocr_pages_pending'], [1])
        self.assertEqual(held['issues'][-1]['ocr_pages_done'], 0)
        self.assertNotIn('ocr_receipts', held)
        self.assertEqual(runner.calls, [])

    def test_unconfigured_and_multiframe_images_remain_counted(self):
        from types import SimpleNamespace
        from unittest import mock
        from campaign_tool.records import native_extract
        for form, raw, enabled in [('png', image_bytes(), False),
                                   ('gif', image_bytes('GIF', frames=True), True),
                                   ('tiff', image_bytes('TIFF', frames=True), True)]:
            with self.subTest(form=form):
                source = self.base / ('held.' + form)
                source.write_bytes(raw)
                subject = hashlib.sha256(raw).hexdigest()
                original = {'storage_path': str(source)}
                pipeline = SimpleNamespace(ocr_tools={'fake': True} if enabled else None,
                    _original=mock.Mock(return_value=original), _form=mock.Mock(return_value=form))
                self.assertEqual(native_extract.classify_form(pipeline, subject, missing=[]),
                                 (form, native_extract.AWAITING_OCR))
                queue, count = native_extract.awaiting_ocr_queue(pipeline,
                    [{'original_sha256': subject, 'form': form, 'outcome': native_extract.AWAITING_OCR}])
                self.assertEqual((queue, count), ([], 1))

    def test_pixel_limit_is_refused_with_fixed_code(self):
        out = io.BytesIO()
        with Image.new('1', (40000001, 1)) as image:
            image.save(out, 'PNG')
        _, _, result = self.extract_image(out.getvalue())
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('image_resource_limit', [issue['code'] for issue in result['issues']])

    def rewrite_bundle(self, result, change):
        import json
        from pathlib import Path
        run = Path(result['run_path'])
        receipt = json.loads((run / 'extraction.json').read_bytes())
        metadata_path = run / 'ocr-derived' / 'digest.json'
        metadata = json.loads(metadata_path.read_bytes())
        change(receipt, metadata)
        metadata_path.write_text(json.dumps(metadata, sort_keys=True)+'\n')
        path = run / 'extraction.json'
        path.write_text(json.dumps(receipt, sort_keys=True)+'\n')
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_derivation_and_receipt_cardinality_tampering_refused(self):
        from campaign_tool.records import extraction_ledger as ledger
        import copy
        source, subject, result = self.extract_image()
        original = dict(sha256=subject, bytes=source.stat().st_size, storage_path=str(source))
        def derived(receipt, metadata):
            receipt['derived']['derived_sha256'] = 'a'*64
            metadata['derived'] = copy.deepcopy(receipt['derived'])
        def parent(receipt, metadata):
            receipt['derived']['parent_sha256'] = 'a'*64
            metadata['derived'] = copy.deepcopy(receipt['derived'])
        def children(receipt, metadata):
            receipt['children'] = [{'sha': receipt['derived']['derived_sha256']}]
            metadata['children'] = copy.deepcopy(receipt['children'])
        def extra(receipt, metadata):
            receipt['ocr_receipts'] *= 2
            metadata['ocr_receipts'] = copy.deepcopy(receipt['ocr_receipts'])
        def missing(receipt, metadata):
            receipt.pop('ocr_receipts')
            metadata.pop('ocr_receipts')
        def page(receipt, metadata):
            receipt['ocr_receipts'][0]['page'] = 2
            metadata['ocr_receipts'] = copy.deepcopy(receipt['ocr_receipts'])
        from pathlib import Path
        run = Path(result['run_path'])
        baseline = {p: p.read_bytes() for p in (run/'extraction.json', run/'ocr-derived'/'digest.json')}
        for change, code in [(derived, 'image_wrapper_derivation_mismatch'),
                             (parent, 'image_wrapper_derivation_mismatch'),
                             (children, 'image_wrapper_derivation_mismatch'),
                             (extra, 'ocr_receipts_binding'), (missing, 'ocr_receipts_binding'),
                             (page, 'ocr_receipts_binding')]:
            with self.subTest(change=change.__name__):
                for path, raw in baseline.items():
                    path.write_bytes(raw)
                path, digest = self.rewrite_bundle(result, change)
                with self.assertRaisesRegex(ledger.ExtractionBindingError, '^'+code+'$'):
                    ledger.validate_bundle(path, digest, source, original)

    def test_ocr_receipt_bound_to_parent_instead_of_derivative_refused(self):
        import json
        from pathlib import Path
        from campaign_tool.records import extraction_ledger as ledger
        from campaign_tool.records.extract import ocr
        source, subject, result = self.extract_image()
        entry = result['ocr_receipts'][0]
        root = Path(result['ocr_output_root'])
        directory = root / result['derived']['derived_sha256'] / 'page-000001' / entry['receipt_id']
        receipt = json.loads((directory/'receipt.json').read_bytes())
        receipt['identity']['source_sha256'] = subject
        receipt['source']['sha256'] = subject
        if 'eligibility' in receipt:
            receipt['eligibility']['source_sha256'] = subject
        receipt['receipt_id'] = ocr._hash(ocr._json(receipt['identity']))
        forged = root / subject / 'page-000001' / receipt['receipt_id']
        forged.mkdir(mode=0o700, parents=True)
        for name in ocr.ARTIFACTS:
            (forged/name).write_bytes((directory/name).read_bytes())
        (forged/'receipt.json').write_bytes(ocr._json(receipt))
        (forged/'manifest.json').write_bytes(ocr._json({'schema_version': ocr.VERSION,
            'receipt_sha256': ocr._hash(ocr._json(receipt)), 'artifact_sha256': receipt['artifact_sha256']}))
        loaded = ocr.load_page_receipt(root, subject, 1, receipt['receipt_id'])
        def rebind(top, metadata):
            top['ocr_receipts'] = [dict(entry, receipt_id=receipt['receipt_id'],
                                       receipt_sha256=loaded['receipt_sha256'])]
            metadata['ocr_receipts'] = top['ocr_receipts']
        path, digest = self.rewrite_bundle(result, rebind)
        original = dict(sha256=subject, bytes=source.stat().st_size, storage_path=str(source))
        with self.assertRaisesRegex(ledger.ExtractionBindingError, '^ocr_receipts_binding$'):
            ledger.validate_bundle(path, digest, source, original)

    def test_existing_image_only_pdf_case_unchanged_with_fake_tools(self):
        from unittest import mock
        from tests.records import test_run_pdf_ocr as pdf_case, ocr_fixtures
        from tests.records.test_native_extract import scanned_pdf
        case = pdf_case.PdfOcrTests('test_image_only_pdf_is_ocr_read_and_disclosed')
        case.setUp()
        self.addCleanup(case.doCleanups)
        original = routes.extract
        runner = ocr_fixtures.FakeOCRRunner(text='Synthetic ALPR policy retained for 45 days.\n', confidence='91')
        def injected(*args, **kwargs):
            return original(*args, **dict(kwargs, ocr_runner=runner))
        with mock.patch.object(pdf_case, 'image_only_pdf', return_value=scanned_pdf()), \
                mock.patch.object(pdf_case, 'local_ocr_tools', return_value=(ocr_fixtures.TOOLS, 'stub-v1')), \
                mock.patch.object(routes, 'extract', side_effect=injected):
            method = pdf_case.PdfOcrTests.test_image_only_pdf_is_ocr_read_and_disclosed
            getattr(method, '__wrapped__', method)(case)
