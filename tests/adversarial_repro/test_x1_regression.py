"""Sol-authored X1 controls; synthetic inputs and fake backends only."""
import json
import unittest
from unittest import mock
from campaign_tool.records import ocr_stage as stage
from tests.adversarial_repro.test_x1 import reply, vision_config
from tests.records.ocr_stage_fixtures import FakeTools, FakeVision, noise_page, scanned_pdf
from tests.records.test_ocr_stage import CLEAN, Workspace, results

class Utf8AndIsolation(unittest.TestCase):
    def test_utf8_validation_rejects_surrogates_but_preserves_unicode(self):
        for text in ('\ud800', '\udfff', 'valid\ud800tail'):
            with self.subTest(text=repr(text)):
                with self.assertRaises(stage.BackendError) as raised:
                    stage.QwenVisionBackend.parse(reply(text))
                self.assertEqual(raised.exception.code, 'vision_response_invalid')
        for text in ('café 日本語 😀 "quoted"', '', CLEAN):
            self.assertEqual(stage.QwenVisionBackend.parse(reply(text)), text)

    def test_unexpected_document_error_keeps_later_document_and_hides_message(self):
        for error in (ValueError('PRIVATE marker'), RuntimeError('PRIVATE marker'), UnicodeError('PRIVATE marker')):
            with self.subTest(error=type(error).__name__):
                ws = Workspace(self)
                bad = ws.add('bad.pdf', scanned_pdf([noise_page(1)]))
                good = ws.add('good.pdf', scanned_pdf([noise_page(2)]))
                ws.write_queue()
                original = stage.OcrStage._process
                def process(candidate, item, fd, workdir):
                    if item.sha256 == bad:
                        raise error
                    return original(candidate, item, fd, workdir)
                with mock.patch.object(stage.OcrStage, '_process', process):
                    line, code, outcomes = stage.run(ws.args(vision_config(ws)), FakeTools({good: [CLEAN]}), FakeVision())
                self.assertEqual([(o.document_id, o.status, o.code, o.attempted) for o in outcomes],
                                 [('doc:bad.pdf', 'ocr_failed', 'document_internal_error', True),
                                  ('doc:good.pdf', 'ocr_complete', None, True)])
                self.assertEqual(set(results(ws.out)), {good})
                self.assertNotIn('PRIVATE', json.dumps(line))
                self.assertEqual(list((ws.out / '.work').iterdir()), [])

    def test_control_flow_baseexception_is_not_swallowed(self):
        ws = Workspace(self)
        ws.add('one.pdf', scanned_pdf([noise_page(1)]))
        ws.write_queue()
        with mock.patch.object(stage.OcrStage, 'process', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                stage.run(ws.args(vision_config(ws)), FakeTools(), FakeVision())
