"""Synthetic inventory differences, never campaign records."""
import hashlib
import unittest
from campaign_tool.records.ledger.reconcile import reconcile

class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.sha=hashlib.sha256(b'example').hexdigest()
        self.receipt=hashlib.sha256(b'receipt').hexdigest()
        self.row={'identity':{'account':'fixture','folder':'inbox','uidvalidity':1,'uid':2},'sha256':self.sha,'receipt_sha256':self.receipt}
    def run_mail(self,sources,index):return reconcile('mail',sources,index,[self.sha])
    def test_stale_index_has_explanation(self):
        r=self.run_mail([self.row],[])
        self.assertEqual(r['classifications'],{'stale_index':1});self.assertEqual(r['unexplained_rows'],0)
    def test_same_bytes_different_folders_need_both_occurrences(self):
        second=dict(self.row,identity=dict(self.row['identity'],folder='archive'))
        self.assertEqual(self.run_mail([self.row,second],[self.row])['classifications'],{'matched':1,'stale_index':1})
    def test_uidvalidity_reset_not_false_match(self):
        second=dict(self.row,identity=dict(self.row['identity'],uidvalidity=2))
        self.assertEqual(self.run_mail([second],[self.row])['classifications'],{'index_without_bound_receipt':1,'stale_index':1})
    def test_flat_mime_index_not_hierarchical_identity(self):
        row=dict(self.row,kind='attachment',identity=dict(self.row['identity'],part=2))
        self.assertEqual(self.run_mail([row],[])['classifications'],{'incomplete_receipt':1})
    def test_missing_bytes_not_marked_received(self):
        r=reconcile('mail',[self.row],[],[])
        self.assertEqual(r['classifications'],{'unavailable_original':1})
    def test_conflicting_index_hash_visible(self):
        other=dict(self.row,sha256=hashlib.sha256(b'other').hexdigest())
        r=self.run_mail([self.row],[other]);self.assertIn('identity_hash_conflict',r['classifications'])
    def test_portal_notice_alone_not_download(self):
        row={'identity':{'portal_host':'example.invalid','request_id':'R1','item_id':'1'},'sha256':self.sha,'receipt_sha256':self.receipt}
        self.assertEqual(reconcile('portal',[row],[],[])['classifications'],{'receipt-without-bytes':1})
        self.assertEqual(reconcile('portal',[row],[],[self.sha])['classifications'],{'bytes-present-unjoined':1})
        row['binding_evidence']=dict(row)
        self.assertEqual(reconcile('portal',[row],[],[self.sha])['classifications'],{'bytes-present-and-joined':1})
    def test_wrong_portal_item_binding_rejected(self):
        row={'identity':{'portal_host':'example.invalid','request_id':'R1','item_id':'1'},'sha256':self.sha,'receipt_sha256':self.receipt}
        row['binding_evidence']=dict(row,identity=dict(row['identity'],item_id='2'))
        self.assertEqual(reconcile('portal',[row],[],[self.sha])['classifications'],{'bytes-present-unjoined':1})
    def test_digest_coverage_differences_not_accepted(self):
        r=reconcile('digests',[{'sha256':self.sha,'coverage_declared':'full'},{'sha256':self.sha,'coverage_declared':'partial'}],[],[self.sha])
        self.assertEqual(r['classifications'],{'declaration_difference_needs_reconciliation':2});self.assertEqual(r['stage_promotions'],0)
    def test_outside_intake_count(self):
        self.assertEqual(reconcile('digests',[{'sha256':self.sha}],[],[])['classifications'],{'outside_intake':1})
    def test_deterministic_report(self):
        self.assertEqual(self.run_mail([self.row],[self.row]),self.run_mail([self.row],[self.row]))
    def test_invalid_rows_fail_explicitly(self):
        with self.assertRaises(ValueError):self.run_mail([None],[])
    def test_duplicate_indexes_not_silently_collapsed(self):
        self.assertEqual(self.run_mail([self.row],[self.row,self.row])['classifications'],{'duplicate_index_rows':1})

    def test_malformed_portal_hash_is_classified(self):
        r=reconcile('portal',[{'sha256':[], 'identity':{}}],[],[self.sha])
        self.assertEqual(r['classifications'],{'receipt-without-bytes':1})
    def test_nonstring_account_not_an_identity(self):
        row=dict(self.row,identity=dict(self.row['identity'],account=['fixture']))
        self.assertEqual(self.run_mail([row],[])['classifications'],{'incomplete_receipt':1})

    def test_portal_parser_error_cannot_be_joined_success(self):
        row={'identity':{'portal_host':'example.invalid','request_id':'R1','item_id':'1'},'sha256':self.sha,'receipt_sha256':self.receipt,'error':'invalid response'}
        row['binding_evidence']=dict(row)
        self.assertEqual(reconcile('portal',[row],[],[self.sha])['classifications'],{'parser_error':1})
    def test_duplicate_source_receipts_match_index_row_once(self):
        second=dict(self.row,receipt_sha256=hashlib.sha256(b'second receipt').hexdigest())
        r=self.run_mail([self.row,second],[self.row])
        self.assertEqual(r['classifications'],{'duplicate_source_receipt':1,'matched':1})
        self.assertEqual([row['classification'] for row in r['rows']],['matched','duplicate_source_receipt'])
        self.assertEqual(sum(map(len,r['index_match_groups'].values())),1)
    def test_duplicate_match_groups_have_linear_output(self):
        result=self.run_mail([self.row]*128,[self.row]*128)
        self.assertEqual(len(result['index_match_groups']),1)
        self.assertEqual(sum(map(len,result['index_match_groups'].values())),128)
        self.assertEqual(len(result['rows']),128)
        self.assertEqual(result['classifications'],{'duplicate_index_rows':1,'duplicate_source_receipt':127})

    def test_rejected_mail_receipts_do_not_consume_index_coverage(self):
        cases = [(dict(self.row, receipt_sha256=None), 'incomplete_receipt'),
                 (dict(self.row, error='synthetic parser failure'), 'parser_error'),
                 (dict(self.row, scope='excluded', exclusion_reason='synthetic exclusion'), 'excluded_content')]
        for rejected, reason in cases:
            with self.subTest(reason=reason):
                result = self.run_mail([rejected], [self.row])
                self.assertEqual(result['classifications'], {reason:1, 'index_without_bound_receipt':1})
                self.assertEqual(result['index_match_groups'], {})
                self.assertIsNone(result['rows'][0]['index_match_group'])

    def test_rejected_receipt_does_not_reserve_later_valid_pair(self):
        bad = dict(self.row, receipt_sha256=None)
        for sources, expected in (([bad,self.row,self.row], ['incomplete_receipt','matched','duplicate_source_receipt']),
                                  ([self.row,bad,self.row], ['matched','incomplete_receipt','duplicate_source_receipt'])):
            with self.subTest(expected=expected):
                result = self.run_mail(sources, [self.row])
                self.assertEqual([r['classification'] for r in result['rows']], expected)
                self.assertEqual(sum(map(len,result['index_match_groups'].values())), 1)
                self.assertIsNone(next(r for r in result['rows'] if r['classification']=='incomplete_receipt')['index_match_group'])
                self.assertEqual(result['stage_promotions'], 0)

    def test_unavailable_bytes_do_not_bind_an_index_row(self):
        result = reconcile('mail', [self.row], [self.row], [])
        self.assertEqual(result['classifications'], {'unavailable_original':1, 'index_without_bound_receipt':1})
        self.assertEqual(result['index_match_groups'], {})

    def test_unjoined_or_error_portal_source_leaves_index_unbound(self):
        row = {'identity':{'portal_host':'example.invalid','request_id':'R1','item_id':'1'},
               'sha256':self.sha,'receipt_sha256':self.receipt}
        joined = dict(row, binding_evidence=dict(row))
        for source, objects, reason in ((row,[self.sha],'bytes-present-unjoined'),
                                         (joined,[],'receipt-without-bytes'),
                                         (dict(joined,error='synthetic failure'),[self.sha],'parser_error')):
            with self.subTest(reason=reason):
                result = reconcile('portal', [source], [row], objects)
                self.assertEqual(result['classifications'], {reason:1, 'index_without_bound_receipt':1})
                self.assertEqual(result['index_match_groups'], {})
                self.assertIsNone(result['rows'][0]['index_match_group'])
        result = reconcile('portal', [row,joined], [row], [self.sha])
        self.assertEqual(result['classifications'], {'bytes-present-unjoined':1, 'bytes-present-and-joined':1})
        self.assertIsNone(result['rows'][0]['index_match_group'])
        self.assertEqual(sum(map(len,result['index_match_groups'].values())), 1)
