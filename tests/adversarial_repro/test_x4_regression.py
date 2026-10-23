"""Sol-authored literal TSV quote and confidence controls for X4."""
import unittest
from campaign_tool.records.extract import ocr
from tests.adversarial_repro.test_x4 import HEADER, tsv_row, StubRunner, Case
from tests.records.ocr_stage_fixtures import noise_page, scanned_pdf

class LiteralTsv(unittest.TestCase):
    def test_quotes_are_literal_for_words_and_confidence(self):
        for word in ('plain', '"open', '"closed"', 'a"b', "'single", '“curly”'):
            with self.subTest(word=word):
                tsv = '\n'.join([HEADER, tsv_row(word, 90), tsv_row('later', 10, 2)])+'\n'
                self.assertEqual(ocr._confidence(tsv), 50.0)
        for confidence in ('"90"', '"90', '-1', '101', 'nan', 'inf', ''):
            with self.subTest(confidence=confidence):
                self.assertEqual(ocr._confidence('\n'.join([HEADER, tsv_row('word', confidence), tsv_row('later', 10, 2)])), 10.0)

    def test_empty_and_structural_rows_do_not_gain_confidence(self):
        self.assertIsNone(ocr._confidence(HEADER+'\n'))
        self.assertIsNone(ocr._confidence('\n'.join([HEADER, tsv_row(' ', 99), tsv_row('structure', -1)])))
        self.assertEqual(ocr._confidence('\n'.join([HEADER, tsv_row('zero', 0), tsv_row('full', 100, 2)])), 50.0)

class QuotedExtraction(Case):
    def test_clean_quoted_text_is_preserved_not_rejected(self):
        text = 'The "quoted" records include café and clear words. '*4
        sha = self.blob(scanned_pdf([noise_page(9)]))
        tsv = '\n'.join([HEADER, tsv_row('"quoted"', 96), tsv_row('next', 94, 2)])+'\n'
        receipt, = self.extract(sha, StubRunner(text=text, tsv=tsv))
        self.assertEqual(receipt['status'], 'ocr_text_unreviewed')
        self.assertEqual(receipt['confidence'], 95.0)
        self.assertTrue(any(p.read_text()==text for p in self.out.rglob('*.txt')))
