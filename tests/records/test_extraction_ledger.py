"""Small synthetic WP4 -> actual WP1 overlay tests; no corpus promotions."""
import hashlib
import importlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from campaign_tool.records import extraction_routes as routes
from campaign_tool.records.extraction_ledger import ExtractionLedgerAdapter,ExtractionBindingError,canonical,sha,MAX_UNITS
from tests.records import ocr_fixtures


class ExtractionLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source=os.environ.get('RECORDS_WP1_SOURCE_ROOT')
        if source:
            import campaign_tool.records
            overlay=Path(source)/'campaign_tool'/'records'
            if not (overlay/'ledger'/'store.py').is_file():raise RuntimeError('invalid explicit overlay')
            campaign_tool.records.__path__.append(str(overlay))
        try:cls.store=importlib.import_module('campaign_tool.records.ledger.store')
        except ModuleNotFoundError:raise unittest.SkipTest('explicit WP1 dependency required')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'original.txt';self.source.write_bytes(b'one\ntwo\n');self.source.chmod(0o400)
        self.subject=sha(self.source.read_bytes());self.database=self.root/'ledger'/'canonical.sqlite'
        self.store.initialize(self.database)
        with self.store.ledger(self.database) as con:
            con.execute("INSERT INTO runs VALUES('intake','fixture','2030-01-01T00:00:00+00:00',NULL,'fixture',NULL,NULL,NULL,'captured','{}')")
            con.execute('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)',(self.subject,self.source.stat().st_size,None,None,'original','unresolved',str(self.source),'receipt_validation_pending',None,'{}'))
            con.execute('INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)',('source',self.subject,'local','synthetic',None,None,'fixture','{}'))
            for stage in self.store.STAGES:
                con.execute('INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)',(self.subject,stage,'pending',None,'fixture','2030-01-01T00:00:00+00:00','intake',None))
            con.commit()
        self.adapter=ExtractionLedgerAdapter(database=self.database,evidence_root=self.root/'evidence')
        self.number=0

    def tearDown(self):self.temp.cleanup()

    def write(self,path,raw):
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        path.write_bytes(raw);path.chmod(0o600)

    def bundle(self,*,text='one',parser_version='1',pdf=False,blocked=False):
        self.number+=1;run=self.root/('run-'+str(self.number));run.mkdir(mode=0o700)
        self.write(run/'input',self.source.read_bytes())
        unit={'kind':'pdf_page' if pdf else 'text_line','locator':{'page':1} if pdf else {'line':1},'text':text,'data':{}}
        units=[] if blocked else [unit]
        parser='fixture-parser'
        metadata={'stage':'complete','parser':parser,'parser_version':parser_version,'counts':{'pages_expected':2} if pdf else {'units':len(units)},'children':[]}
        selected=run/'derived'
        if not blocked:
            self.write(selected/'digest.json',json.dumps(metadata,sort_keys=True).encode())
            self.write(selected/'units.jsonl',b''.join((json.dumps(u,sort_keys=True)+'\n').encode() for u in units))
        pages=routes.page_manifest(units,2 if pdf else None,parser,parser_version) if not blocked else []
        result={'schema':'format-extraction-v1','original_sha256':self.subject,'route':routes.route('pdf' if pdf else 'txt'),'parser_adapter':routes.VERSION,'status':'blocked' if blocked else 'partial' if pdf else 'complete','units':units,'pages':pages,'issues':[{'code':'fixture_decoder_blocked'}] if blocked else [],'review_status':'not_reviewed','original_changed':False,'run_path':str(run)}
        if not blocked:result.update(parser=parser,parser_version=parser_version,parser_components={},children=[])
        self.write(run/'extraction.json',(json.dumps(result,sort_keys=True)+'\n').encode())
        return run/'extraction.json'

    def enroll(self,path):
        return self.adapter.enroll(original_path=self.source,receipt_path=path,receipt_sha256=sha(path.read_bytes()))

    def count(self,table):
        with self.store.ledger(self.database,readonly=True) as con:return con.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def rewrite(self,path,mutator):
        data=json.loads(path.read_text());mutator(data);self.write(path,(json.dumps(data,sort_keys=True)+'\n').encode())

    def test_actual_text_route_enrolls_units_pending(self):
        out=self.root/'actual-derived';out.mkdir(mode=0o700)
        result=routes.extract(self.source,self.subject,out,'txt',timeout=20)
        enrolled=self.adapter.enroll(original_path=self.source,receipt_path=Path(result['run_path'])/'extraction.json',receipt_sha256=result['receipt_sha256'])
        self.assertEqual(enrolled['units'],2)
        self.assertEqual(enrolled['stage_promotions'],0)
        self.assertEqual(self.count('units'),2)
        self.assertTrue(all(x['pending']==1 and x['done']==0 for x in self.store.counts(self.database)['stages'].values()))

    def test_receipt_hash_wrong_rejected(self):
        receipt=self.bundle()
        with self.assertRaisesRegex(ExtractionBindingError,'receipt_hash_mismatch'):
            self.adapter.enroll(original_path=self.source,receipt_path=receipt,receipt_sha256=sha(b'wrong'))

    def test_original_bytes_changed(self):
        receipt=self.bundle();self.source.chmod(0o600);self.source.write_bytes(b'changed')
        with self.assertRaises(ExtractionBindingError):self.enroll(receipt)

    def test_pinned_source_changed(self):
        receipt=self.bundle();self.write(receipt.parent/'input',b'changed')
        with self.assertRaises(ExtractionBindingError):self.enroll(receipt)

    def test_units_changed_without_receipt(self):
        receipt=self.bundle();path=receipt.parent/'derived'/'units.jsonl'
        unit=json.loads(path.read_text());unit['text']='changed';self.write(path,(json.dumps(unit)+'\n').encode())
        with self.assertRaisesRegex(ExtractionBindingError,'unit_receipt_mismatch'):self.enroll(receipt)

    def test_parser_metadata_changed(self):
        receipt=self.bundle();path=receipt.parent/'derived'/'digest.json'
        self.rewrite(path,lambda d:d.update(parser_version='other'))
        with self.assertRaisesRegex(ExtractionBindingError,'parser_metadata_mismatch'):self.enroll(receipt)

    def test_unknown_parser_refused(self):
        receipt=self.bundle(parser_version='unknown')
        with self.assertRaisesRegex(ExtractionBindingError,'parser_provenance_unavailable'):self.enroll(receipt)

    def test_page_hash_or_method_tamper_refused(self):
        receipt=self.bundle(pdf=True)
        self.rewrite(receipt,lambda d:d['pages'][0].update(derivative_sha256=sha(b'wrong')))
        with self.assertRaisesRegex(ExtractionBindingError,'page_provenance_mismatch'):self.enroll(receipt)

    def test_missing_page_is_accounted_and_pending(self):
        result=self.enroll(self.bundle(pdf=True))
        self.assertEqual(result['pages'],2)
        self.assertEqual(result['extract_acceptance'],'pending_trusted_validator')
        with self.store.ledger(self.database,readonly=True) as con:
            rows=con.execute('SELECT page_no,status,reason FROM page_state ORDER BY page_no').fetchall()
        self.assertEqual(tuple(rows[1]),(2,'blocked','page_not_extracted'))

    def test_false_complete_pages_rejected(self):
        receipt=self.bundle(pdf=True);self.rewrite(receipt,lambda d:d.update(status='complete'))
        with self.assertRaisesRegex(ExtractionBindingError,'false_complete_pages'):self.enroll(receipt)

    def ocr_bundle(self):
        """Real WP4 extract() of a mixed synthetic PDF with injected per-page OCR."""
        try:raw=ocr_fixtures.mixed_pdf()
        except ImportError:self.skipTest('pypdf optional parser unavailable')
        source=self.root/'original.pdf';source.write_bytes(raw);source.chmod(0o400)
        subject=sha(raw)
        with self.store.ledger(self.database) as con:
            con.execute('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)',(subject,len(raw),None,None,'original','unresolved',str(source),'receipt_validation_pending',None,'{}'))
            con.execute('INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)',('pdf-source',subject,'local','synthetic',None,None,'fixture','{}'))
            for stage in self.store.STAGES:
                con.execute('INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)',(subject,stage,'pending',None,'fixture','2030-01-01T00:00:00+00:00','intake',None))
            con.commit()
        out=self.root/'pdf-derived';out.mkdir(mode=0o700)
        ocr_root=self.root/'page-ocr';ocr_root.mkdir(mode=0o700)
        result=routes.extract(source,subject,out,'pdf',timeout=30,ocr_output_root=str(ocr_root),ocr_tools=ocr_fixtures.TOOLS,tool_signature='stub-v1',ocr_runner=ocr_fixtures.FakeOCRRunner())
        self.assertEqual([x['page'] for x in result['ocr_receipts']],[2])
        return source,Path(result['run_path'])/'extraction.json',result,ocr_root

    def test_ocr_receipt_binding_and_visual_hold(self):
        source,receipt,result,ocr_root=self.ocr_bundle()
        enrolled=self.adapter.enroll(original_path=source,receipt_path=receipt,receipt_sha256=sha(receipt.read_bytes()))
        self.assertIn('ocr_visual_review_required',enrolled['gaps'])
        self.assertIn('visual_review_pending',enrolled['gaps'])
        self.assertNotIn('ocr_runtime_version_unverified',enrolled['gaps'])
        self.assertEqual(enrolled['stage_promotions'],0);self.assertEqual(enrolled['pages'],2)
        entry=result['ocr_receipts'][0]
        with self.store.ledger(self.database,readonly=True) as con:
            rows=con.execute('SELECT page_no,method,status,reason,needs_visual_review FROM page_state WHERE original_sha256=? ORDER BY page_no',(sha(source.read_bytes()),)).fetchall()
            provenance=[json.loads(r[0]) for r in con.execute('SELECT provenance_json FROM units WHERE original_sha256=? ORDER BY legacy_ordinal',(sha(source.read_bytes()),))]
        self.assertEqual(tuple(rows[0])[2],'ok')
        self.assertEqual(tuple(rows[1]),(2,'local-ocr-3','partial','ocr_visual_review_required',1))
        self.assertTrue(all(p['ocr_receipt_ids']==[entry['receipt_id']] and 'ocr_derivative_sha256' not in p for p in provenance))
        sidecar=ocr_root/result['original_sha256']/'page-000002'/entry['receipt_id']/'sidecar.txt'
        sidecar.write_text('tampered OCR text')
        with self.assertRaisesRegex(ExtractionBindingError,'ocr_receipt_unverified'):
            self.adapter.enroll(original_path=source,receipt_path=receipt,receipt_sha256=sha(receipt.read_bytes()))

    def test_ocr_unit_text_must_equal_receipt_sidecar(self):
        source,receipt,result,_=self.ocr_bundle()
        run=receipt.parent
        def change(units):
            units[1]['text']='substituted text'
            return units
        units=change(json.loads(json.dumps(result['units'])))
        self.write(run/'ocr-derived'/'units.jsonl',b''.join((json.dumps(u,ensure_ascii=True,sort_keys=True)+'\n').encode() for u in units))
        def mutate(data):
            data['units']=units
            data['pages'][1]['derivative_sha256']=sha(b'substituted text')
        self.rewrite(receipt,mutate)
        with self.assertRaisesRegex(ExtractionBindingError,'ocr_unit_merge_mismatch'):
            self.adapter.enroll(original_path=source,receipt_path=receipt,receipt_sha256=sha(receipt.read_bytes()))

    def test_ocr_receipt_hash_tamper_rejected(self):
        source,receipt,result,_=self.ocr_bundle()
        def mutate(data):
            data['ocr_receipts'][0]['receipt_sha256']='0'*64
        self.rewrite(receipt,mutate)
        with self.assertRaisesRegex(ExtractionBindingError,'ocr_receipts_metadata_mismatch'):
            self.adapter.enroll(original_path=source,receipt_path=receipt,receipt_sha256=sha(receipt.read_bytes()))
        path=receipt.parent/'ocr-derived'/'digest.json'
        self.rewrite(path,mutate)
        with self.assertRaisesRegex(ExtractionBindingError,'ocr_receipt_hash_mismatch'):
            self.adapter.enroll(original_path=source,receipt_path=receipt,receipt_sha256=sha(receipt.read_bytes()))

    def test_native_page_unit_cannot_be_replaced(self):
        source,receipt,result,_=self.ocr_bundle()
        units=json.loads(json.dumps(result['units']));units[0]['text']='rewritten native text'
        self.write(receipt.parent/'ocr-derived'/'units.jsonl',b''.join((json.dumps(u,ensure_ascii=True,sort_keys=True)+'\n').encode() for u in units))
        def mutate(data):
            data['units']=units
            data['pages'][0]['derivative_sha256']=sha(b'rewritten native text')
        self.rewrite(receipt,mutate)
        with self.assertRaisesRegex(ExtractionBindingError,'ocr_unit_merge_mismatch'):
            self.adapter.enroll(original_path=source,receipt_path=receipt,receipt_sha256=sha(receipt.read_bytes()))

    def test_whole_document_ocr_receipt_rejected(self):
        receipt=self.bundle(pdf=True)
        self.rewrite(receipt,lambda d:d.update(ocr_derivative_sha256=sha(b'legacy'),ocr_derivative_path=str(receipt.parent/'ocr.pdf')))
        with self.assertRaisesRegex(ExtractionBindingError,'whole_document_ocr_unsupported'):self.enroll(receipt)

    def test_duplicate_replay_does_not_duplicate(self):
        receipt=self.bundle();first=self.enroll(receipt)
        events=self.count('stage_events');second=self.enroll(receipt)
        self.assertTrue(second['reused']);self.assertEqual(first['import_id'],second['import_id'])
        self.assertEqual(self.count('units'),1);self.assertEqual(self.count('stage_events'),events)

    def test_parser_version_change_preserves_historical_units(self):
        first_receipt=self.bundle(parser_version='1');first=self.enroll(first_receipt)
        second=self.enroll(self.bundle(text='new interpretation',parser_version='2'))
        self.assertNotEqual(first['import_id'],second['import_id'])
        self.assertEqual(self.count('units'),2)
        replay=self.enroll(first_receipt)
        self.assertEqual(replay['selection'],'historical_candidate')
        self.assertEqual(self.count('extraction_adapter_imports'),2)

    def test_pages_history_survives_changed_version(self):
        first=self.bundle(pdf=True);self.enroll(first)
        self.enroll(self.bundle(pdf=True,text='changed',parser_version='2'))
        self.assertEqual(self.count('extraction_adapter_pages'),4)
        self.assertEqual(self.count('page_state'),2)
        self.assertTrue(self.enroll(first)['reused'])

    def test_changed_canonical_unit_replay_rejected(self):
        receipt=self.bundle();self.enroll(receipt)
        with self.store.ledger(self.database) as con:con.execute("UPDATE units SET text_sha256='changed'");con.commit()
        with self.assertRaisesRegex(ExtractionBindingError,'canonical_unit_changed'):self.enroll(receipt)

    def test_changed_current_page_replay_rejected(self):
        receipt=self.bundle(pdf=True);self.enroll(receipt)
        with self.store.ledger(self.database) as con:con.execute("UPDATE page_state SET reason='changed'");con.commit()
        with self.assertRaisesRegex(ExtractionBindingError,'canonical_current_pages_changed'):self.enroll(receipt)

    def test_changed_bound_artifact_replay_rejected(self):
        receipt=self.bundle();self.enroll(receipt)
        path=receipt.parent/'derived'/'units.jsonl';self.write(path,path.read_bytes()+b'\n')
        with self.assertRaises(ExtractionBindingError):self.enroll(receipt)

    def test_evidence_cas_corruption_rejected(self):
        receipt=self.bundle();self.enroll(receipt)
        with self.store.ledger(self.database,readonly=True) as con:path=Path(con.execute('SELECT derived_path FROM units').fetchone()[0])
        path.chmod(0o600);path.write_bytes(b'corrupt')
        with self.assertRaises(ExtractionBindingError):self.enroll(receipt)

    def test_crash_after_bytes_before_transaction(self):
        receipt=self.bundle()
        def crash(stage):
            if stage=='after_evidence_bytes':raise RuntimeError('synthetic interruption')
        self.adapter.fault=crash
        with self.assertRaises(RuntimeError):self.enroll(receipt)
        self.assertEqual(self.count('units'),0)
        self.adapter.fault=None;self.enroll(receipt)
        self.assertEqual(self.count('units'),1)

    def test_transaction_failure_rolls_back_all_content(self):
        receipt=self.bundle(pdf=True)
        def crash(stage):
            if stage=='before_canonical_commit':raise RuntimeError('synthetic interruption')
        self.adapter.fault=crash
        with self.assertRaises(RuntimeError):self.enroll(receipt)
        for table in ('units','page_state','extraction_adapter_imports','extraction_adapter_pages'):self.assertEqual(self.count(table),0)
        self.assertEqual(self.count('runs'),1)
        self.adapter.fault=None;self.enroll(receipt)
        self.assertEqual(self.count('units'),1)

    def test_crash_after_commit_replay_safe(self):
        receipt=self.bundle()
        def crash(stage):
            if stage=='after_canonical_commit':raise RuntimeError('synthetic interruption')
        self.adapter.fault=crash
        with self.assertRaises(RuntimeError):self.enroll(receipt)
        self.adapter.fault=None;result=self.enroll(receipt)
        self.assertTrue(result['reused']);self.assertEqual(self.count('units'),1)

    def test_active_stage_prevents_selection(self):
        with self.store.ledger(self.database) as con:con.execute("UPDATE stage_state SET status='in_progress' WHERE stage='extract'");con.commit()
        result=self.enroll(self.bundle(pdf=True))
        self.assertEqual(result['selection'],'held_existing_stage')
        self.assertEqual(self.count('page_state'),0)
        self.assertEqual(self.count('units'),1)

    def test_blocked_route_is_explicit_pending_record(self):
        result=self.enroll(self.bundle(blocked=True))
        self.assertEqual(result['units'],0)
        self.assertIn('extraction_route_blocked',result['gaps'])
        self.assertEqual(self.store.counts(self.database)['stages']['extract']['pending'],1)

    def test_receipt_cannot_claim_review(self):
        receipt=self.bundle();self.rewrite(receipt,lambda d:d.update(review_status='fully_reviewed'))
        with self.assertRaisesRegex(ExtractionBindingError,'receipt_schema_or_review_claim'):self.enroll(receipt)

    def test_symlink_derivative_refused(self):
        receipt=self.bundle();path=receipt.parent/'derived'/'units.jsonl';elsewhere=self.root/'elsewhere';path.rename(elsewhere);path.symlink_to(elsewhere)
        with self.assertRaisesRegex(ExtractionBindingError,'symlink_path'):self.enroll(receipt)

    def test_preexisting_unbound_pages_are_preserved(self):
        with self.store.ledger(self.database) as con:
            con.execute('INSERT INTO page_state VALUES(?,?,?,?,?,?,?,?,?)',(self.subject,1,'older','1',None,None,1,'partial','older evidence'))
            con.commit()
        result=self.enroll(self.bundle(pdf=True))
        self.assertEqual(result['selection'],'held_unbound_existing_pages')
        with self.store.ledger(self.database,readonly=True) as con:
            self.assertEqual(con.execute('SELECT reason FROM page_state').fetchone()[0],'older evidence')
        self.assertEqual(self.count('extraction_adapter_pages'),2)

    def test_historical_page_rows_reject_replace(self):
        import sqlite3
        self.enroll(self.bundle(pdf=True))
        with self.store.ledger(self.database) as con:
            row=tuple(con.execute('SELECT * FROM extraction_adapter_pages LIMIT 1').fetchone())
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute('INSERT OR REPLACE INTO extraction_adapter_pages VALUES(?,?,?)',row)

    def test_aggregate_resource_bound(self):
        receipt=self.bundle()
        with patch('campaign_tool.records.extraction_ledger.MAX_TOTAL',1):
            with self.assertRaisesRegex(ExtractionBindingError,'aggregate_evidence_bound'):self.enroll(receipt)
        self.assertEqual(self.count('units'),0)

    def test_duplicate_json_keys_rejected(self):
        receipt=self.bundle()
        raw=receipt.read_bytes().replace(b'{',b'{"schema":"duplicate",',1)
        self.write(receipt,raw)
        with self.assertRaisesRegex(ExtractionBindingError,'duplicate_json_key'):self.enroll(receipt)

    def test_missing_child_bytes_rejected(self):
        receipt=self.bundle()
        self.rewrite(receipt,lambda d:d.update(children=[{'sha':sha(b'child'),'bytes':5,'locator':{'attachment':1}}]))
        with self.assertRaises(FileNotFoundError):self.enroll(receipt)
        self.assertEqual(self.count('units'),0)

    def test_no_blanket_stage_validator(self):
        with self.assertRaisesRegex(ExtractionBindingError,'trusted_extraction_stage_adapter_unconfigured'):
            self.adapter.validate_and_promote({},'fixture-run')


if __name__=='__main__':unittest.main()
