"""Sol-authored X2 malformed-reply class and later-document controls."""
import http.client
import json
import unittest
from campaign_tool.records import ocr_stage as stage
from tests.adversarial_repro.test_x2 import vision_config
from tests.adversarial_repro.test_x1 import reply
from tests.records.ocr_stage_fixtures import FakeTools, FakeVision, noise_page, scanned_pdf
from tests.records.test_ocr_stage import CLEAN, Workspace, results

class ParsingIsolation(unittest.TestCase):
    def test_invalid_response_keeps_later_document(self):
        for payload in (b'{"choices":' * 50000, b'not json', b'\xff', b'{}', reply(None)):
            with self.subTest(payload=payload[:30]):
                self.check_continuation(FakeVision(raw=payload))
        for error in (http.client.BadStatusLine('PRIVATE'), http.client.IncompleteRead(b'PRIVATE'), http.client.LineTooLong('PRIVATE')):
            with self.subTest(error=type(error).__name__):
                self.check_continuation(FakeVision(error=error))

    def check_continuation(self, vision):
        ws = Workspace(self)
        bad = ws.add('bad.pdf', scanned_pdf([noise_page(3)]))
        good = ws.add('good.pdf', scanned_pdf([noise_page(4)]))
        ws.write_queue()
        line, code, outcomes = stage.run(ws.args(vision_config(ws)), FakeTools({bad: [''], good: [CLEAN]}), vision)
        self.assertEqual([(o.document_id, o.status) for o in outcomes],
                         [('doc:bad.pdf', 'ocr_partial'), ('doc:good.pdf', 'ocr_complete')])
        published = results(ws.out)
        self.assertEqual(set(published), {bad, good})
        self.assertEqual(published[bad]['pages'][0]['vision_error'], 'vision_response_invalid')
        self.assertEqual(published[good]['pages'][0]['status'], 'ocr_text')
        self.assertEqual(published[bad]['review_state'], 'not_reviewed')
        self.assertNotIn('PRIVATE', json.dumps(line))

    def test_valid_json_and_unicode_still_parse(self):
        self.assertEqual(stage.QwenVisionBackend.parse(reply('Quoted "hello", café 😀')), 'Quoted "hello", café 😀')
