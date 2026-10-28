"""X6 phase 2a: S9-S12, generated PDF objects and deterministic tools only.

These are machine/control-flow assertions, not real OCR qualification or
semantic acceptance. S13-S16 and policy cache migration remain separate.
"""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from pypdf import PdfReader, PdfWriter, PageObject
from pypdf.generic import (ArrayObject, BooleanObject, DecodedStreamObject,
                           DictionaryObject, FloatObject, NameObject, NumberObject)
from campaign_tool.records import extraction_routes as routes, pdf_eligibility as policy
from campaign_tool.records.extract import ocr, vision
from tests.records import test_x6_eligibility as first
from tests.records import test_x6_adversarial as repro


def obj(value):
    if isinstance(value, dict):
        return DictionaryObject({NameObject(k): obj(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return ArrayObject([obj(v) for v in value])
    if isinstance(value, bool):
        return BooleanObject(value)
    if isinstance(value, int):
        return NumberObject(value)
    if isinstance(value, float):
        return FloatObject(value)
    return NameObject(value)


def generated(content=b'', *, page_fields=None, image_fields=None, state=None,
              forms=(), form_matrix=(1, 0, 0, 1, 0, 0), form_box=(0, 0, 100, 100),
              form_group=None, use_form=True):
    """Parsable PDF with a native word and configurable real Form dictionaries.

    Form i invokes i+1; the last draws an image. All forms have their own
    /Resources, /BBox and /Matrix. PDF object graphs, not mocked geometry.
    """
    writer = PdfWriter()
    page = writer.add_page(PdfReader(io.BytesIO(first.fixture())).pages[0])
    resources = page['/Resources']
    image = resources['/XObject']['/Im0']
    if image_fields:
        image.update(obj(image_fields))
    if page_fields:
        page.update(obj(page_fields))
    if state is not None:
        resources[NameObject('/ExtGState')] = obj({'/G0': state})
        content = b'/G0 gs ' + content
    current = resources['/XObject'].raw_get('/Im0')
    current_name = '/Im0'
    for index, stream_content in reversed(list(enumerate(forms))):
        form = DecodedStreamObject()
        form.set_data(stream_content or (current_name + ' Do').encode('ascii'))
        form.update(obj({'/Type': '/XObject', '/Subtype': '/Form',
                         '/BBox': form_box, '/Matrix': form_matrix}))
        form[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): resources['/Font'],
            NameObject('/XObject'): DictionaryObject({NameObject(current_name): current})})
        if form_group:
            form[NameObject('/Group')] = obj(form_group)
        current = writer._add_object(form)
        current_name = '/Fm' + str(index)
    if forms:
        resources['/XObject'][NameObject(current_name)] = current
        if use_form:
            content += (current_name + ' Do ').encode('ascii')
    stream = DecodedStreamObject()
    stream.set_data(content + b' BT /F1 10 Tf 20 20 Td (Approved) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


VISION = vision.adapter(['/synthetic/bin/x6-vision'], 'synthetic', 'stub', 'stub-v1')


class VisionRunner(repro.StubRunner):
    def __init__(self, *args, vision_text='Synthetic visible body\n', vision_rc=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.vision_text, self.vision_rc = vision_text, vision_rc

    def __call__(self, command, **kwargs):
        if Path(command[0]).name == 'x6-vision':
            self.calls.append(command)
            return subprocess.CompletedProcess(command, self.vision_rc,
                self.vision_text.encode(), b'')
        return super().__call__(command, **kwargs)


class Phase2a(unittest.TestCase):
    setUp = first.Contract.setUp
    evaluate = first.Contract.evaluate
    routed = first.Contract.routed
    required = first.Contract.required
    sufficient = first.Contract.sufficient

    def direct(self, raw, runner=None, **options):
        self.serial += 1
        intake, output = self.root / f'intake-{self.serial}', self.root / f'ocr-{self.serial}'
        (intake / 'blobs').mkdir(parents=True)
        sha = hashlib.sha256(raw).hexdigest()
        source = intake / 'blobs' / sha
        source.write_bytes(raw)
        runner = runner or repro.StubRunner(text=first.BODY)
        receipt, = ocr.extract_image_only_pages(intake, sha, output,
            tool_signature='stub 1', tools=repro.TOOLS, runner=runner, **options)
        self.last = intake, sha, output
        self.assertEqual(source.read_bytes(), raw)
        self.assertEqual((receipt['source']['sha256'], receipt['locator'], receipt['page_count']),
                         (sha, {'page': 1}, 1))
        self.assertEqual((receipt['review_status'], receipt['fidelity_status'], receipt['visual_check_status']),
                         ('not_reviewed', 'not_checked', 'not_done'))
        loaded = ocr.load_page_receipt(output, sha, 1, receipt['receipt_id'])
        self.assertEqual(receipt, loaded['receipt'])
        return receipt, loaded, runner

    def blocked(self, raw, reason='page_eligibility_unresolved', runner=None, **options):
        receipt, loaded, runner = self.direct(raw, runner, **options)
        self.assertEqual((receipt['status'], receipt['blocked_reason']), ('blocked', reason))
        self.assertFalse(vision.needs_fallback(receipt))
        intake, sha, output = self.last
        self.assertTrue((output / 'indexes' / 'blocked' / (receipt['receipt_id'] + '.json')).is_file())
        self.assertEqual(receipt['visual_check_required'], False)
        self.assertEqual(receipt['artifact_sha256'], {})
        self.assertIsNone(loaded['sidecar'])
        self.assertEqual(runner.calls, [])
        return receipt

    def test_S9_tiled_and_overlapping_union_not_bounding_rectangle(self):
        # Two partly overlapping tiles and one repeated placement: 200+200-50=350.
        placements = (b'q 20 0 0 10 0 0 cm /Im0 Do Q '
                      b'q 20 0 0 10 10 5 cm /Im0 Do Q '
                      b'q 20 0 0 10 0 0 cm /Im0 Do Q ')
        e = self.required(generated(placements))
        self.assertEqual(e['image_union_area'], 350)
        self.assertEqual(e['image_rectangles_exact'], [['0','0','20','10'], ['10','5','30','15'], ['0','0','20','10']])
        self.assertEqual(len(e['image_placements']), 3)
        self.assertEqual(e['image_placements'][0]['xobject_path'], ['/Im0'])

    def test_S9_nested_form_matrix_bbox_and_caller_clip(self):
        # Outer: (2x+10,2y+20); inner repeats it; leaf unit -> [30,60,34,64].
        raw = generated(b'31 61 2 2 re W n ', forms=(b'', b''),
                        form_matrix=(2,0,0,2,10,20))
        e = self.required(raw)
        self.assertEqual(e['image_rectangles_exact'], [['31','61','33','63']])
        self.assertEqual(e['image_union_area'], 4)
        self.assertEqual(e['image_placements'][0]['xobject_path'], ['/Fm0','/Fm1','/Im0'])

    def test_S9_form_own_bbox_restricts_painting(self):
        raw = generated(forms=(b'20 0 0 20 0 0 cm /Im0 Do',), form_box=(5,5,15,15))
        e = self.required(raw)
        self.assertEqual(e['image_rectangles_exact'], [['5','5','15','15']])
        self.assertEqual(e['image_union_area'], 100)
        self.sufficient(generated(forms=(b'',), form_box=(10,10,20,20)))
        self.sufficient(generated(forms=(b'',), use_form=False))
        self.sufficient(generated(forms=(b'BT /F1 10 Tf 10 10 Td (Native Form word) Tj ET',)))

    def test_S9_rotation_userunit_media_not_crop_and_original_locator(self):
        raw = generated(b'q 10 0 0 20 200 300 cm /Im0 Do Q ',
            page_fields={'/Rotate':90, '/UserUnit':2, '/CropBox':[0,0,100,100]})
        e = self.required(raw)
        self.assertEqual((e['domain'], e['rotation'], e['user_unit']), ([0.,0.,612.,792.],90,2.))
        self.assertEqual(e['image_union_area'], 200)
        self.assertEqual(e['domain_convention'], 'unrotated-MediaBox-pdftoppm-default')
        writer = PdfWriter()
        for _ in range(2):
            writer.add_page(PdfReader(io.BytesIO(raw)).pages[0])
        stream = io.BytesIO(); writer.write(stream)
        multi = stream.getvalue(); sha = hashlib.sha256(multi).hexdigest()
        intake = self.root / 'multi'; (intake / 'blobs').mkdir(parents=True)
        (intake / 'blobs' / sha).write_bytes(multi)
        runner = repro.StubRunner(text=first.BODY)
        receipt, = ocr.extract_image_only_pages(intake, sha, self.root / 'multi-ocr',
            pages=(2,), tools=repro.TOOLS, tool_signature='stub 1', runner=runner)
        self.assertEqual((receipt['locator'],receipt['page_count'],receipt['eligibility']['page']), ({'page':2},2,2))
        self.assertEqual(receipt['eligibility']['source_sha256'], sha)
        self.assertNotIn('-cropbox', runner.calls[0])
        derivative = Path(runner.calls[0][-2])
        self.assertEqual(derivative.name, 'page.pdf')
        final = self.root / 'multi-ocr' / sha / 'page-000002' / receipt['receipt_id'] / 'page.pdf'
        page = PdfReader(final).pages[0]
        self.assertEqual((page.rotation, page.user_unit, list(page.cropbox)), (90,2,[0,0,100,100]))
        self.assertEqual((intake / 'blobs' / sha).read_bytes(), multi)

    def test_S9_quarter_turn_and_reflection_placement(self):
        for cm, rectangle in ((b'0 20 -10 0 30 40 cm', ['20','40','30','60']),
                              (b'-20 0 0 10 30 40 cm', ['10','40','30','50'])):
            with self.subTest(cm=cm):
                e = self.evaluate(generated(b'q ' + cm + b' /Im0 Do Q '))
                self.assertEqual(e['image_rectangles_exact'], [rectangle])
                self.assertEqual(e['image_union_area'], 200)

    def test_carry_gs_and_transparency_group_preserve_native_positive(self):
        for fields, state in (({}, {'/Type':'/ExtGState','/CA':1,'/ca':1,'/BM':'/Normal','/SMask':'/None'}),
                              ({'/Group':{'/S':'/Transparency','/CS':'/DeviceRGB','/I':True,'/K':False}}, None),
                              ({'/Group':{'/S':'/Transparency'}}, {'/ca':0.5})):
            with self.subTest(fields=fields, state=state):
                self.sufficient(generated(page_fields=fields, state=state))
                e = self.required(generated(first.FULL, page_fields=fields, state=state))
                self.assertEqual(e['image_union_area'], 612*792)
        self.sufficient(generated(forms=(b'',), form_group={'/S':'/Transparency'}, form_box=(10,10,20,20)))

    def test_S10_unsupported_clip_mask_state_and_form_fail_closed(self):
        cases = [first.fixture(before=b'0 0 m 10 10 l W n '),
                 generated(first.FULL, image_fields={'/ImageMask':True}),
                 generated(first.FULL, state={'/SMask':{'/S':'/Alpha'}}),
                 generated(state={'/Font':['/F1',12]}),
                 generated(page_fields={'/Group':{'/S':'/Other'}}),
                 generated(forms=(b'',), form_matrix=(1,0.5,0,1,0,0)),
                 generated(b'q 1 0.5 0 1 0 0 cm /Im0 Do Q '),
                 first.fixture(before=b'4 Tr ')]
        for raw in cases:
            with self.subTest(sha=hashlib.sha256(raw).hexdigest()):
                e = self.evaluate(raw)
                self.assertEqual((e['decision'],e['probe_complete'],e['painted_image_present']), ('unresolved',False,None))
                self.blocked(raw)
                result, runner = self.routed(raw, True)
                self.assertNotEqual(result['pages'][0]['status'], 'ok')
                self.assertEqual(result['pages'][0]['reason'], 'page_eligibility_unresolved')
                self.assertEqual(runner.calls, [])

    def test_S10_missing_malformed_nonfinite_geometry_and_text_failure(self):
        for fields in ({'/MediaBox':[0,0,0,20]}, {'/UserUnit':0}, {'/Rotate':45}):
            self.blocked(generated(page_fields=fields))
        for raw in (generated(forms=(b'',), form_box=(0,0,10)),
                    generated(forms=(b'',), form_box=(10,10,0,0)),
                    generated(state={'/ca':2})):
            e = self.evaluate(raw)
            self.assertEqual(e['decision'],'unresolved')
            self.assertIsNone(e['image_union_area'])
            self.blocked(raw)
        writer = PdfWriter()
        missing = writer.add_page(PdfReader(io.BytesIO(generated(forms=(b'',)))).pages[0])
        del missing['/Resources']['/XObject']['/Fm0']['/BBox']
        stream = io.BytesIO(); writer.write(stream)
        self.blocked(stream.getvalue())
        page = PdfReader(io.BytesIO(generated())).pages[0]
        # Nonfinite numbers are invalid PDF lexical tokens, so model the parser's
        # exposed malformed numeric boundary directly, not as a pretend valid PDF.
        with mock.patch.object(type(page), 'mediabox', new_callable=mock.PropertyMock, return_value=[0,0,float('inf'),100]):
            e = policy.probe(page, 'a'*64, 1, 'Approved')
        self.assertEqual((e['decision'],e['probe_complete']), ('unresolved',False))
        with mock.patch.object(PageObject, 'extract_text', side_effect=ValueError('synthetic decoding failure')):
            self.blocked(first.fixture(image=first.FULL), reason='page_text_probe_failed')

    def test_S10_positive_before_unknown_is_lower_bound_not_native_skip(self):
        raw = first.fixture(image=first.SMALL, after=b'0 0 m 10 10 l W n ')
        e = self.evaluate(raw)
        self.assertEqual((e['decision'],e['probe_complete'],e['painted_image_present']), ('ocr_required',False,True))
        self.assertIsNone(e['image_union_area'])
        receipt, loaded, runner = self.direct(raw)
        self.assertEqual(receipt['status'], 'ocr_text_unreviewed')
        self.assertEqual(loaded['sidecar'].decode(), first.BODY)
        self.assertEqual(len(runner.calls), 2)

    def test_S10_global_operation_stream_form_and_image_bounds(self):
        # Each form individually fits; the combined repeated traversal does not.
        raw = generated(forms=(b'q Q q Q /Fm1 Do', b'q Q q Q'))
        page = PdfReader(io.BytesIO(raw)).pages[0]
        outer = page['/Resources']['/XObject']['/Fm0']
        inner = outer['/Resources']['/XObject']['/Fm1']
        # Limit fits every stream individually; only cumulative traversal fails.
        byte_limit = max(len(s.get_data()) for s in (page.get_contents(),outer,inner))
        for constant, limit, reason in (('MAX_OPERATIONS',5,'operation_bound'),
                                       ('MAX_STREAM_BYTES',byte_limit,'stream_bound'),
                                       ('MAX_FORMS',1,'form_bound'),
                                       ('MAX_FORM_DEPTH',1,'form_bound')):
            with self.subTest(constant=constant), mock.patch.object(policy, constant, limit):
                e = self.evaluate(raw)
                self.assertEqual(e['decision'], 'unresolved')
                self.assertIn(reason,e['unavailable'])
                self.assertIsNone(e['image_union_area'])
                self.blocked(raw)
        repeated = generated(b'/Fm0 Do /Fm0 Do ', forms=(b'q Q',), use_form=False)
        with mock.patch.object(policy, 'MAX_FORMS', 1):
            e = self.evaluate(repeated)
            self.assertEqual(e['decision'],'unresolved')
            self.assertIn('form_bound',e['unavailable'])
        with mock.patch.object(policy, 'MAX_IMAGES', 1):
            e = self.evaluate(generated(first.SMALL*2))
            self.assertEqual((e['decision'],e['probe_complete'],e['image_union_area']), ('ocr_required',False,None))
            self.assertIn('image_bound', e['unavailable'])

    def test_S10_form_cycle_is_bounded_without_recursive_text_probe(self):
        page = PdfReader(io.BytesIO(generated(forms=(b'',)))).pages[0]
        form = page['/Resources']['/XObject']['/Fm0']
        form['/Resources']['/XObject'][NameObject('/Self')] = form.indirect_reference
        form.set_data(b'/Self Do')
        e = policy.probe(page, 'a'*64, 1, 'Approved')
        self.assertEqual(e['decision'], 'unresolved')
        self.assertIn('form_cycle',e['unavailable'])

    def test_S11_fidelity_hold_dominates_scan_and_survives_restart(self):
        receipt = self.blocked(first.fixture(image=first.FULL),
            reason='fidelity_hold_requires_visual_comparison', fidelity_holds=(1,))
        intake, sha, output = self.last
        runner = VisionRunner()
        repeated, = ocr.extract_image_only_pages(intake, sha, output, tool_signature='stub 1', tools=repro.TOOLS, runner=runner)
        self.assertEqual(repeated, receipt)
        self.assertEqual(runner.calls, [])
        self.assertFalse(vision.needs_fallback(repeated))

    def test_S11_render_bounds_stop_before_launch(self):
        raw = generated(first.FULL, page_fields={'/UserUnit':10})
        self.assertEqual(self.evaluate(raw)['decision'], 'ocr_required')
        self.blocked(raw, reason='page_raster_bound')

    def test_S11_tesseract_timeout_preserves_exact_reason_and_artifact_prefix(self):
        runner = VisionRunner(tesseract_error=subprocess.TimeoutExpired(['synthetic-tesseract'], 1))
        receipt, loaded, runner = self.direct(first.fixture(image=first.FULL), runner)
        self.assertEqual((receipt['status'],receipt['blocked_reason']), ('blocked','local_ocr_timeout'))
        self.assertEqual(set(receipt['artifact_sha256']), {'page.pdf','page.png'})
        self.assertIsNone(loaded['sidecar'])
        self.assertFalse(vision.needs_fallback(receipt))
        self.assertEqual([Path(c[0]).name for c in runner.calls], ['pdftoppm','tesseract'])

    def test_S12_exact_confidence_states_and_only_existing_vision_eligibility(self):
        cases = [('', 96, 'blocked','no_text_after_ocr',True),
                 (first.BODY, -1, 'blocked','confidence_unavailable',True),
                 (first.BODY, 79.99, 'visual_check_queued',None,True),
                 (first.BODY, 80, 'ocr_text_unreviewed',None,False),
                 (first.BODY, 96, 'ocr_text_unreviewed',None,False)]
        for text, confidence, status, reason, eligible in cases:
            with self.subTest(confidence=confidence,text=bool(text)):
                runner = VisionRunner(text=text, tsv=repro.HEADER+'\n'+repro.tsv_row('word',confidence)+'\n')
                receipt, loaded, _ = self.direct(first.fixture(image=first.FULL), runner)
                self.assertEqual((receipt['status'],receipt['blocked_reason']), (status,reason))
                self.assertEqual(receipt['visual_check_required'], status=='visual_check_queued')
                self.assertEqual(receipt['confidence'], None if confidence<0 else confidence)
                self.assertEqual(set(receipt['artifact_sha256']),set(ocr.ARTIFACTS))
                self.assertEqual(vision.needs_fallback(receipt), eligible)
                intake, sha, output = self.last
                result = routes._page_result(output,sha,receipt,loaded,'initial',VISION,180,runner)
                self.assertEqual([Path(c[0]).name for c in runner.calls].count('x6-vision'),int(eligible))
                if eligible:
                    self.assertEqual(result['summary']['vision']['status'], 'vision_text_unreviewed')
                    self.assertEqual(result['text'],'Synthetic visible body\n')
                    v = vision.load_vision_receipt(output,sha,1,result['summary']['vision']['receipt_id'])['receipt']
                    self.assertEqual((v['review_status'],v['fidelity_status'],v['visual_check_status']), ('not_reviewed','not_checked','not_done'))
                    self.assertTrue(v['visual_check_required'])
                    rows = routes.page_manifest([routes.text_unit(1,result['summary'],result['text'])],1,ocr_receipts={1:result['summary']})
                    self.assertEqual((rows[0]['status'],rows[0]['reason']), ('partial','ocr_visual_review_required'))

    def test_S12_failed_vision_keeps_exact_gap_or_low_confidence_text(self):
        for text, confidence in (('',96), (first.BODY,42)):
            runner = VisionRunner(text=text, tsv=repro.HEADER+'\n'+repro.tsv_row('word',confidence)+'\n', vision_rc=3)
            receipt, loaded, _ = self.direct(first.fixture(image=first.FULL),runner)
            _, sha, output = self.last
            result = routes._page_result(output,sha,receipt,loaded,'initial',VISION,180,runner)
            self.assertEqual(result['summary']['vision']['status'],'blocked')
            self.assertEqual(result['text'], text or None)
            self.assertEqual(result['summary']['status'],receipt['status'])

    def test_S12_no_raster_skip_and_tampered_raster_never_launch_vision(self):
        receipt, _, runner = self.direct(first.fixture(), VisionRunner())
        _, sha, output = self.last
        with self.assertRaisesRegex(ValueError,'vision_fallback_not_eligible'):
            vision.run_fallback(output,sha,1,receipt['receipt_id'],vision_adapter=VISION,runner=runner)
        self.assertEqual(runner.calls,[])
        receipt, _, runner = self.direct(first.fixture(image=first.FULL), VisionRunner(text=''))
        _, sha, output = self.last
        raster = output/sha/'page-000001'/receipt['receipt_id']/'page.png'
        raster.write_bytes(b'synthetic tamper')
        with self.assertRaisesRegex(ValueError,'existing_ocr_artifact_mismatch'):
            vision.run_fallback(output,sha,1,receipt['receipt_id'],vision_adapter=VISION,runner=runner)
        self.assertEqual([Path(c[0]).name for c in runner.calls],['pdftoppm','tesseract'])

    def test_negative_control_disabled_form_traversal_is_detected(self):
        raw = generated(forms=(b'10 0 0 10 0 0 cm /Im0 Do',))
        good = self.evaluate(raw)
        self.assertEqual(good['decision'],'ocr_required')
        original = policy._Traversal.walk
        def omit_nested(traversal, content, resources, matrix, clip, path_names=(), graphics_depth=0):
            if path_names:
                return
            return original(traversal,content,resources,matrix,clip,path_names,graphics_depth)
        # Inert scoped mutation of the deciding gate: all PDF/source bytes and
        # other prerequisites remain valid. The required coverage assertion fails.
        with mock.patch.object(policy._Traversal,'walk',omit_nested):
            with self.assertRaises(AssertionError):
                self.assertEqual(self.evaluate(raw)['decision'],'ocr_required')
        self.assertEqual(self.evaluate(raw),good)


if __name__ == '__main__':
    unittest.main()
