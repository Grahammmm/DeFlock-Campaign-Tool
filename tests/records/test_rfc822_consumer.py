"""Synthetic exporter-to-ledger RFC822 occurrence, rollback and replay proofs."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records.intake import folder, mail_delta, eml_export, imap_intake
from campaign_tool.records.intake import rfc822_adapter as adapter
from tests.records.test_rfc822_integration import eml, rfc, multi


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class RFC822ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'mail'
        self.root.mkdir(mode=0o700)
        self.out = self.base / 'intake'
        self.out.mkdir(mode=0o700)
        (self.out / 'blobs').mkdir(mode=0o700)
        self.db = sqlite3.connect(self.out / 'intake.sqlite')
        self.addCleanup(self.db.close)
        self.db.executescript(folder.SCHEMA)
        (self.out / 'intake.sqlite').chmod(0o600)
        self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)',
                        ('baseline',folder.now(),folder.now(),'inventory','complete','{}'))
        self.db.execute("INSERT INTO meta VALUES('inventory_run','baseline')")
        self.db.commit()

    def exported(self, raw=None, uid=5):
        raw = self.nested() if raw is None else raw
        return eml_export.export_message(raw,mail_root=self.root,account='synthetic',
                                         mailbox='INBOX',uidvalidity=1,uid=uid)

    def nested(self):
        leaf=multi([eml(),b'Content-Type: application/pdf\r\nContent-Disposition: attachment; filename=synthetic.pdf\r\n\r\n%PDF-1.4\r\nsynthetic'],boundary=b'leaf')
        return multi([eml(),rfc(b'Subject: synthetic middle\r\n'+multi([eml(),rfc(b'Subject: synthetic leaf\r\n'+leaf)],boundary=b'middle'),named=True)])

    def import_file(self,path):
        return mail_delta.import_receipt(path,self.root,self.out)

    def pristine(self):
        self.assertEqual(self.db.execute('SELECT count(*) FROM docs').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM occurrences').fetchone()[0],0)
        self.assertEqual(list((self.out/'blobs').iterdir()),[])
        self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='inventory_run'").fetchone()[0],'baseline')

    def test_nested_exact_bytes_immediate_parents_formats_and_replay(self):
        path=self.exported();receipt=json.loads(path.read_text())
        self.assertEqual(receipt['schema'],adapter.SCHEMA)
        plan=mail_delta.wire_intake_plan(self.nested())
        result=self.import_file(path)
        self.assertEqual(result['added_occurrences'],len(plan.captures)+1)
        self.assertEqual(result['added_edges'],len(plan.captures))
        for capture in plan.captures:
            self.assertEqual((self.out/'blobs'/capture.sha256).read_bytes(),capture.payload)
            rows=self.db.execute('SELECT parent,locator FROM occurrences WHERE sha=?',(capture.sha256,)).fetchall()
            self.assertIn(capture.parent_sha256,[row[0] for row in rows])
            self.assertTrue(any(json.loads(row[1]).get('mime_chain')==list(capture.mime_chain) for row in rows))
            self.assertEqual(self.db.execute('SELECT child FROM edges WHERE parent=? AND locator=?',
                (capture.parent_sha256,folder.js({'mime':capture.mime}))).fetchone()[0],capture.sha256)
            if capture.kind=='eml':
                self.assertEqual(self.db.execute('SELECT format FROM docs WHERE sha=?',(capture.sha256,)).fetchone()[0],'eml')
        self.assertEqual(self.import_file(path)['status'],'replay')
        forms=eml_export.attachment_forms(path)
        for capture in plan.captures:
            if capture.kind=='eml':self.assertEqual(forms[capture.sha256],'eml')

    def test_same_bytes_distinct_occurrences_and_receipt_order(self):
        raw=multi([eml(),rfc(eml()),rfc(eml())])
        path=self.exported(raw);receipt=json.loads(path.read_text())
        receipt['attachments'].reverse();path.write_text(json.dumps(receipt))
        result=self.import_file(path)
        self.assertEqual(result['added_occurrences'],3)
        self.assertEqual(result['added_edges'],2)
        second=self.exported(raw,uid=6)
        self.assertEqual(self.import_file(second)['added_occurrences'],3)
        self.assertEqual(self.db.execute('SELECT count(*) FROM docs').fetchone()[0],2)

    def test_parent_membership_budget_and_kind_tampering_reject_before_transaction(self):
        path=self.exported();original=json.loads(path.read_text())
        for mutate in [lambda r:r['attachments'][0].update(parent_sha256='0'*64),
                       lambda r:r['attachments'].pop(),
                       lambda r:r['budget'].update(total_bytes=1),
                       lambda r:r['attachments'][0].update(kind='mime')]:
            receipt=json.loads(json.dumps(original));mutate(receipt);path.write_text(json.dumps(receipt))
            with self.assertRaises((mail_delta.Rejected,adapter.RFC822AdapterError)):
                self.import_file(path)
            self.pristine()

    def test_changed_capture_rejects_and_rolls_back_staging(self):
        path=self.exported();receipt=json.loads(path.read_text())
        capture=self.root/receipt['attachments'][0]['path'];capture.write_bytes(b'changed')
        with self.assertRaises(mail_delta.Rejected):self.import_file(path)
        self.pristine()

    def test_sealing_failure_removes_only_new_blobs_and_keeps_baseline(self):
        path=self.exported();seal=mail_delta.seal_tracked;calls=0
        def fail_after_one(*args):
            nonlocal calls
            calls+=1
            if calls==2:raise OSError('synthetic disk failure')
            return seal(*args)
        with patch.object(mail_delta,'seal_tracked',side_effect=fail_after_one):
            with self.assertRaises(mail_delta.Rejected):self.import_file(path)
        self.pristine()
        self.assertEqual(self.import_file(path)['added_occurrences'],4)

    def test_nested_body_ambiguity_is_not_laundered_and_export_has_no_partial_receipt(self):
        raw=multi([eml(),rfc(b'Subject: synthetic inner\r\n'+multi([eml(),eml(b'unmarked record')],boundary=b'inner'))])
        with self.assertRaises(mail_delta.Rejected) as caught:self.exported(raw)
        self.assertEqual(caught.exception.args,('ambiguous_inline_body_part',))
        self.assertEqual(list(self.root.rglob('*')),[])

    def test_global_capture_budget_is_enforced_before_files_are_written(self):
        from campaign_tool.records.intake.rfc822_inventory import RFC822InventoryError
        raw=multi([eml()]+[rfc(eml()) for _ in range(101)])
        with self.assertRaises(RFC822InventoryError) as caught:self.exported(raw)
        self.assertEqual(caught.exception.args,('rfc822_capture_limit',))
        self.assertEqual(list(self.root.rglob('*')),[])

    def test_typed_inventory_codes_are_safe_for_imap_checkpoint_errors(self):
        self.assertEqual(imap_intake._failure_code(adapter.RFC822AdapterError('rfc822_missing_receipt')),'rfc822_missing_receipt')
        self.assertEqual(imap_intake._failure_code(ValueError('rfc822_missing_receipt')),'fetch_or_preserve_failed')

class RFC822CanonicalTests(unittest.TestCase):
    def test_complete_fake_imap_runner_preserves_all_nested_parents_and_checks_checkpoints(self):
        from tests.records.test_imap_intake import IMAPIntakeTests,FakeIMAP
        harness=IMAPIntakeTests();harness.setUp()
        try:
            raw=RFC822ConsumerTests().nested()
            server=FakeIMAP({'INBOX':{'uidvalidity':7,'messages':{1:raw}},
                             'Agencies':{'uidvalidity':3,'messages':{}}})
            report=harness.run_with(server)
            self.assertEqual(report['mailbox']['preserved'],1)
            self.assertEqual(report['mailbox']['failures'],0)
            self.assertEqual(harness.checkpoints()['INBOX'],(7,1))
            rows=harness.ledger('SELECT id,original_sha256,parent_occurrence_id FROM occurrences')
            self.assertEqual(len(rows),4)
            parents={oid:sha for oid,sha,_ in rows}
            plan=mail_delta.wire_intake_plan(raw)
            for capture in plan.captures:
                matching=[r for r in rows if r[1]==capture.sha256]
                self.assertTrue(any(parents.get(r[2])==capture.parent_sha256 for r in matching))
            self.assertEqual(harness.ledger("SELECT count(*) FROM stage_state WHERE stage='preserve' AND status='done'")[0][0],4)
            again=harness.run_with(FakeIMAP(server.folders))
            self.assertEqual(again['mailbox']['preserved'],0)
        finally:harness.doCleanups()

    def test_wire_failure_does_not_advance_folder_but_other_folder_can_progress(self):
        from tests.records.test_imap_intake import IMAPIntakeTests,FakeIMAP
        harness=IMAPIntakeTests();harness.setUp()
        try:
            invalid=multi([eml(),b'Content-Type: message/rfc822\r\nContent-Transfer-Encoding: base64\r\n\r\nU3ludGhldGlj'])
            server=FakeIMAP({'INBOX':{'uidvalidity':7,'messages':{1:invalid}},
                             'Agencies':{'uidvalidity':3,'messages':{2:RFC822ConsumerTests().nested()}}})
            report=harness.run_with(server)
            self.assertEqual(report['mailbox']['preserved'],1)
            self.assertEqual(report['mailbox']['failures'],1)
            self.assertEqual(harness.checkpoints(),{'INBOX':(7,0),'Agencies':(3,2)})
            inbox=next(f for f in report['mailbox']['folders'] if f['folder']=='INBOX')
            self.assertEqual(inbox['failed'],'uid 1: rfc822_wire_rejected')
        finally:harness.doCleanups()
