"""Fictional OCR closure/replay; injected OCR tools remain explicitly synthetic."""
import json
import shutil
import sys
import unittest
from unittest.mock import patch
from campaign_tool.records import extraction_ledger as enrollment
from campaign_tool.records import extraction_validation as validation
from campaign_tool.records import ocr_closure
from tests.records import test_extraction_validation as fixtures


@unittest.skipUnless(sys.platform.startswith('linux'), 'native extraction isolation requires Linux')
class ClosedOCRTests(unittest.TestCase):
    def setUp(self):
        fixtures.ExtractionValidationTests.setUpClass()
        self.h = fixtures.ExtractionValidationTests('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        source, receipt, result, root = self.h.ocr_bundle()
        self.h.source, self.h.subject = source, result['original_sha256']
        self.entry = result['ocr_receipts'][0]
        self.root = root
        self.h.enrolled = self.h.adapter.enroll(original_path=source, receipt_path=receipt,
                                               receipt_sha256=result['receipt_sha256'])
        self.h.install()
        with self.h.store.ledger(self.h.database, readonly=True) as con:
            self.manifest = json.loads(con.execute('SELECT manifest_json FROM extraction_adapter_imports').fetchone()[0])
        self.artifacts = {name: enrollment.read_file(ref['path'], validation.MAX_ACCEPT_EVIDENCE)
                          for name, ref in self.manifest['artifacts'].items()}

    def load(self, artifacts=None, subject=None, page=None):
        return ocr_closure.load_cas_page(self.artifacts if artifacts is None else artifacts,
            subject or self.h.subject, self.entry['page'] if page is None else page,
            self.entry['receipt_id'])

    def test_six_asset_closure_and_verifier_code_are_bound(self):
        keys = {'ocr_receipt:' + self.entry['receipt_id']} | {
            'ocr_asset:' + self.entry['receipt_id'] + ':' + name for name in ocr_closure.NAMES}
        self.assertTrue(keys <= set(self.artifacts))
        self.assertEqual(self.manifest['ocr_closure_version'], ocr_closure.VERSION)
        self.assertEqual(self.load()['receipt_sha256'], self.entry['receipt_sha256'])
        self.assertTrue({'ocr_closure','ocr_verifier'} <= set(validation.code_identity()))

    def test_acceptance_evidence_and_test_only_replay_survive_original_ocr_tree_removal(self):
        shutil.rmtree(self.root)
        self.h.runner()
        content, original, coverage = self.h.validator.evidence(self.h.enrolled['import_id'])
        self.assertTrue(json.loads(content)['ocr']['visual_review_pending'])
        self.h.test_runner.set_content(self.h.subject, 'extract', content, author_id='fixture', tier='A')
        claim = self.h.test_runner.claim(self.h.subject, 'extract')
        packet = validation.encoded(self.h.packet('extract',content,coverage))
        first = self.h.test_runner.promote(self.h.subject,'extract',packet,claim_id=claim['claim_id'])
        self.assertTrue(first['test_only'])
        self.assertTrue(self.h.test_runner.promote(self.h.subject,'extract',packet)['reused'])
        self.assertEqual(self.h.stages.counts(self.h.database)['verified_seven_stage_complete'],0)

    def test_version_mismatch_is_held_without_rewriting_history(self):
        # A metadata revision cannot silently reuse an older import identity.
        with self.h.store.ledger(self.h.database, readonly=True) as con:
            prior = list(con.execute("SELECT id FROM extraction_adapter_imports"))
        with patch.object(enrollment, 'VERSION', 'extraction-ledger-v1'):
            with self.assertRaisesRegex(ValueError, 'manifest_binding'):
                self.h.validator.evidence(self.h.enrolled['import_id'])
        self.assertEqual(len(prior), 1)

    def test_each_missing_or_changed_asset_refuses(self):
        keys = [name for name in self.artifacts if name.startswith(('ocr_receipt:','ocr_asset:'))]
        self.assertEqual(len(keys),6)
        for name in keys:
            for mode in ('missing','changed'):
                with self.subTest(name=name,mode=mode):
                    copy = dict(self.artifacts)
                    if mode == 'missing': del copy[name]
                    else: copy[name] += b'fictional corruption'
                    with self.assertRaises((ValueError,OSError)):
                        self.load(copy)

    def test_source_page_and_unknown_namespace_refuse(self):
        with self.assertRaises(ValueError): self.load(subject='a'*64)
        with self.assertRaises(ValueError): self.load(page=1)
        self.assertFalse(ocr_closure.artifact_name('ocr_asset:'+'a'*64+':../receipt.json'))
        self.assertFalse(ocr_closure.artifact_name('ocr_asset:'+'a'*64+':original.pdf'))

    def test_duplicate_or_nonfinite_json_and_limits_refuse(self):
        for body in (b'{"identity":{},"identity":{}}', b'{"identity":NaN}'):
            copy = dict(self.artifacts);copy['ocr_receipt:'+self.entry['receipt_id']]=body
            with self.assertRaises(ValueError):self.load(copy)
        with patch.object(ocr_closure,'MAX_FILE',1), self.assertRaises(ValueError):self.load()
        with patch.object(ocr_closure,'MAX_TOTAL',1), self.assertRaises(ValueError):self.load()
