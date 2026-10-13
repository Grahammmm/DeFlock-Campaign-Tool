"""Fictional workbook contracts; temporary synthetic helper is explicitly test-only."""
import copy
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from campaign_tool.records import extraction_ledger as enrollment
from campaign_tool.records import workbook_policy as policy
from campaign_tool.records import extraction_validation as validation
from campaign_tool.records.intake import folder


class WorkbookPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.root.chmod(0o700)
        self.inspection = {'contract':'plain-workbook-native-rows-v3','package_parts':1,
            'scope':'all_native_sheet_rows_only','gaps':[],'visual_review':False,'semantic_acceptance':False}
        self.accounting = {'schema':'workbook-native-accounting-v1','denominator':{
            'declared_sheets':1,'accounted_sheets':1,'serialized_rows':1,'serialized_cells':1,'formula_cells':0},
            'sheets':[{'name':'Synthetic','sheet_id':'1','part':'xl/worksheets/sheet1.xml','state':'visible',
                'serialized_rows':1,'serialized_cells':1,'explicit_blank_cells':0,'formula_cells':0}],
            'gaps':[],'coverage':'all_serialized_native_rows_and_cells','sparse_grid_blanks_materialized':False,
            'formula_execution':False,'visual_review':False,'semantic_acceptance':False}
        values = [{'cell':'A1','value':'Fictional value','type':'s'}]
        self.units = [{'kind':'workbook_sheet','locator':{'sheet':'Synthetic'},'text':'Synthetic','data':{'state':'visible'}},
            {'kind':'workbook_row','locator':{'sheet':'Synthetic','row':1},'text':folder.js(values),'data':{'cells':values}}]

    def raw(self, parts=None):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            for name, value in (parts or {'xl/workbook.xml':b'<synthetic/>'}).items(): z.writestr(name,value)
        return stream.getvalue()

    def load(self):
        # Synthetic helper models the documented interface, never production approval.
        source = ('import json\nINSPECTION=json.loads('+repr(json.dumps(self.inspection))+')\nACCOUNTING=json.loads('
            +repr(json.dumps(self.accounting))+')\ndef inspect(package): return INSPECTION\ndef account(package): return ACCOUNTING\n').encode()
        path=self.root/'synthetic.py';path.write_bytes(source);path.chmod(0o400)
        self.pin=patch.object(policy,'COMPONENT_SHA256',enrollment.sha(source));self.pin.start();self.addCleanup(self.pin.stop)
        self.binding=policy.load_native_workbook_policy(path)
        return self.binding

    def test_plain_row_preserves_exact_text_and_structural_denominator(self):
        result=self.load().qualify(self.raw(),self.units)
        self.assertEqual(result['native_accounting']['denominator']['serialized_rows'],1)
        self.assertTrue(result['visual_review_pending']);self.assertFalse(result['semantic_acceptance'])
        changed=copy.deepcopy(self.units);changed[1]['text']=json.dumps(changed[1]['data']['cells'],separators=(',',':'))
        with self.assertRaisesRegex(ValueError,'row_text'):self.binding.qualify(self.raw(),changed)

    def test_hidden_state_is_retained_with_review_hold(self):
        self.accounting['sheets'][0]['state']='hidden';self.units[0]['data']['state']='hidden'
        self.assertEqual(self.load().qualify(self.raw(),self.units)['native_accounting']['sheets'][0]['state'],'hidden')

    def test_empty_serialized_row_and_empty_sheet_have_exact_counts(self):
        self.accounting['denominator']['serialized_cells']=0;self.accounting['sheets'][0]['serialized_cells']=0
        self.units[1].update(text='[]',data={'cells':[]})
        self.assertEqual(self.load().qualify(self.raw(),self.units)['native_accounting']['denominator']['serialized_rows'],1)
        self.binding._module.ACCOUNTING['denominator']['serialized_rows']=0
        self.binding._module.ACCOUNTING['sheets'][0]['serialized_rows']=0
        self.assertEqual(self.binding.qualify(self.raw(),self.units[:1])['native_accounting']['denominator']['serialized_rows'],0)

    def test_formula_order_and_package_qualifier_holds_cannot_be_waived_by_accounting(self):
        self.load()
        for gap in ('workbook_worksheet_construct_unqualified','workbook_row_order_unqualified','workbook_package_part_unqualified'):
            self.binding._module.INSPECTION['gaps']=[gap]
            with self.assertRaisesRegex(ValueError,'qualification_hold'):self.binding.qualify(self.raw(),self.units)

    def test_accounting_holds_and_non_boolean_flags_refuse(self):
        self.load();self.binding._module.ACCOUNTING['gaps']=['workbook_external_sheet_unresolved']
        with self.assertRaisesRegex(ValueError,'accounting_unqualified'):self.binding.qualify(self.raw(),self.units)
        self.binding._module.ACCOUNTING['gaps']=[]
        for key in ('formula_execution','visual_review','semantic_acceptance'):
            self.binding._module.ACCOUNTING[key]=0
            with self.assertRaisesRegex(ValueError,'accounting_unqualified'):self.binding.qualify(self.raw(),self.units)
            self.binding._module.ACCOUNTING[key]=False

    def test_sparse_parser_placeholders_and_explicit_blanks_refuse(self):
        self.load();extra=copy.deepcopy(self.units);extra.append({'kind':'workbook_row','locator':{'sheet':'Synthetic','row':2},'text':'[]','data':{'cells':[]}})
        with self.assertRaisesRegex(ValueError,'denominator_mismatch'):self.binding.qualify(self.raw(),extra)
        self.binding._module.ACCOUNTING['sheets'][0]['explicit_blank_cells']=1
        with self.assertRaisesRegex(ValueError,'blank_disposition_hold'):self.binding.qualify(self.raw(),self.units)

    def test_zip_preflight_precedes_unbounded_helper_reads(self):
        self.load()
        for parts in ({'../outside.xml':b'<a/>'},{'xl/workbook.xml':b'<!DOCTYPE a><a/>'}):
            with self.assertRaises(ValueError):self.binding.qualify(self.raw(parts),self.units)
        with patch.object(policy,'MAX_TOTAL',1),self.assertRaises(ValueError):self.binding.qualify(self.raw(),self.units)
        with patch.object(policy,'MAX_PART',1),self.assertRaises(ValueError):self.binding.qualify(self.raw(),self.units)

    def test_component_bytes_permissions_and_caller_callbacks_refuse(self):
        self.load();self.binding._path.chmod(0o600)
        with self.assertRaisesRegex(ValueError,'not_readonly'):self.binding.qualify(self.raw(),self.units)
        self.binding._path.write_bytes(b'changed');self.binding._path.chmod(0o400)
        with self.assertRaisesRegex(ValueError,'component_changed'):self.binding.qualify(self.raw(),self.units)
        with self.assertRaisesRegex(ValueError,'trusted_workbook_policy'):validation._InstalledExtractionValidator(self.root/'db',self.root,self.root,object())
        with self.assertRaisesRegex(ValueError,'loader_required'):policy.NativeWorkbookPolicy(object(),self.root,None)


@unittest.skipUnless(sys.platform.startswith('linux'),'native parser isolation requires Linux')
class WorkbookCompositionTests(unittest.TestCase):
    def test_actual_parser_enrollment_and_test_only_acceptance_require_opt_in(self):
        from openpyxl import Workbook
        from tests.records import test_extraction_validation as fixtures
        fixtures.ExtractionValidationTests.setUpClass()
        h=fixtures.ExtractionValidationTests('runTest');h.setUp();self.addCleanup(h.doCleanups)
        book=Workbook();book.active.title='Synthetic';book.active['A1']='Fictional value'
        stream=io.BytesIO();book.save(stream);raw=stream.getvalue()
        source=h.root/'original.xlsx';h.write(source,raw);h.source=source;h.subject=enrollment.sha(raw)
        with h.store.ledger(h.database) as con:
            con.execute('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)',(h.subject,len(raw),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',None,'original','unresolved',str(source),'receipt_validation_pending',None,'{}'))
            con.execute('INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)',('xlsx-source',h.subject,'local','fictional',None,None,'fixture','{}'))
            for stage in h.store.STAGES:con.execute('INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)',(h.subject,stage,'pending',None,'fixture',h.store.now(),'intake',None))
            con.commit()
        out=h.root/'xlsx';out.mkdir(mode=0o700)
        result=fixtures.routes.extract(source,h.subject,out,'xlsx',timeout=20)
        h.enrolled=h.enroll(Path(result['run_path'])/'extraction.json');h.install()
        with self.assertRaisesRegex(ValueError,'extraction_incomplete'):h.validator.evidence(h.enrolled['import_id'])
        f=WorkbookPolicyTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        f.inspection['package_parts']=len(zipfile.ZipFile(io.BytesIO(raw)).infolist());qualified=f.load()
        adapter_id=validation.install_extraction_validator(database=h.database,evidence_root=h.adapter.evidence_root,original_root=h.root,native_workbook_policy=qualified)
        h.validator=h.stages._INSTALLED_VALIDATORS[adapter_id];h.runner()
        content,original,coverage=h.validator.evidence(h.enrolled['import_id'])
        self.assertEqual(coverage,{'denominator':{'kind':'rows','total':1},'covered':1,'scope':'all_rows'})
        self.assertTrue(json.loads(content)['workbook']['visual_review_pending'])
        h.test_runner.set_content(h.subject,'extract',content,author_id='fixture',tier='A')
        claim=h.test_runner.claim(h.subject,'extract')
        packet=validation.encoded(h.packet('extract',content,coverage))
        self.assertTrue(h.test_runner.promote(h.subject,'extract',packet,claim_id=claim['claim_id'])['test_only'])
        self.assertTrue(h.test_runner.promote(h.subject,'extract',packet)['reused'])
        self.assertEqual(h.stages.counts(h.database)['verified_seven_stage_complete'],0)
