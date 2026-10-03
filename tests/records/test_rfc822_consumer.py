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

    def test_scalar_line_endings_keep_legacy_export_import_and_replay_identity(self):
        for uid, eol in enumerate((b"\r\n", b"\n"), 10):
            with self.subTest(eol=eol):
                payload = eol.join((b"line one", b"line two", b""))
                attachment = eol.join((b"Content-Type: text/plain",
                    b"Content-Disposition: attachment; filename=synthetic.txt",
                    b"Content-Transfer-Encoding: 7bit", b"", payload))
                raw = multi([eml(eol=eol), attachment], eol=eol)
                path = self.exported(raw, uid=uid)
                receipt = json.loads(path.read_text())
                self.assertNotIn("schema", receipt)
                self.assertNotIn("scalar_payload_policy", receipt)
                item, = receipt["attachments"]
                expected = payload.replace(b"\r\n", b"\n")
                self.assertEqual((self.root / item["path"]).read_bytes(), expected)
                self.assertEqual(item["sha256"], digest(expected))
                self.import_file(path)
                self.assertEqual((self.out / "blobs" / digest(expected)).read_bytes(), expected)
                self.assertEqual(self.import_file(path)["added_occurrences"], 0)
                second = self.exported(raw, uid=uid + 100)
                self.import_file(second)
                self.assertEqual(json.loads(second.read_text())["attachments"], receipt["attachments"])

    def test_legacy_original_later_forwarded_preserves_both_scalar_derivations(self):
        payload = b"line one\r\nline two\r\n"
        attachment = (b"Content-Type: text/plain\r\n"
            b"Content-Disposition: attachment; filename=synthetic.txt\r\n"
            b"Content-Transfer-Encoding: 7bit\r\n\r\n" + payload)
        original = b"Subject: synthetic prior original\r\n" + multi([eml(), attachment], boundary=b"prior")
        self.import_file(self.exported(original, uid=1))
        old_edge = self.db.execute("SELECT locator,child FROM edges WHERE parent=?",
            (digest(original),)).fetchone()
        forwarded = self.exported(multi([eml(), rfc(original)]), uid=2)
        self.import_file(forwarded)
        edges = self.db.execute("SELECT locator,child FROM edges WHERE parent=?",
            (digest(original),)).fetchall()
        self.assertIn(old_edge, edges)
        self.assertEqual({child for _, child in edges},
            {digest(payload), digest(payload.replace(b"\r\n", b"\n"))})
        self.assertEqual(self.import_file(forwarded)["added_occurrences"], 0)
        self.assertEqual((self.out / "blobs" / digest(payload)).read_bytes(), payload)
        self.db.row_factory = sqlite3.Row
        folder.reconcile_edges(self.db, digest(original), {
            "children_inventory_complete": True,
            "children": [{"locator": json.loads(old_edge[0]), "sha": old_edge[1],
                          "name": "synthetic.txt"}]}, {"excluded_path_fragments": []}, "synthetic-v1")
        self.assertEqual(self.db.execute("SELECT count(*) FROM edges WHERE parent=?",
            (digest(original),)).fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM edge_history").fetchone()[0], 0)
        # Explicit exclusions still retire both policies while preserving edge history.
        folder.reconcile_edges(self.db, digest(original), {
            "children_inventory_complete": True, "children": []},
            {"excluded_path_fragments": ["synthetic.txt"]}, "synthetic-v2")
        self.assertEqual(self.db.execute("SELECT count(*) FROM edges WHERE parent=?",
            (digest(original),)).fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM edge_history").fetchone()[0], 2)
        self.db.row_factory = None

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
                (capture.parent_sha256,folder.js({'mime':capture.mime,'wire_schema':adapter.SCHEMA}))).fetchone()[0],capture.sha256)
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


class RFC822RecordsRunnerTests(unittest.TestCase):
    def test_records_runner_accepts_verified_wire_locators_and_canonical_parent_edges(self):
        from tests.records.test_runner_wp1_overlay import WP1OverlayTests
        from tests.records.test_runner import Transcript
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        harness=WP1OverlayTests();harness.setUp()
        try:
            fixture=harness.fixture
            path=eml_export.export_message(RFC822ConsumerTests().nested(),mail_root=fixture.delta.root,
                account='synthetic-account',mailbox='INBOX',uidvalidity=9,uid=64)
            backend=CanonicalMailBackend(fixture.delta.root,fixture.delta.out,harness.db)
            provider=Transcript(fixture.scopes,[64],lambda scope,uid:str(path))
            result=fixture.invoke(provider=provider,backend=backend)
            self.assertEqual(result['messages_preserved'],1)
            self.assertEqual(result['mail_failures'],0)
            self.assertEqual(fixture.query('SELECT highest_uid FROM runner_folders'),[(64,)])
            facts=json.loads(fixture.query('SELECT evidence FROM runner_messages')[0][0])
            self.assertEqual(len(facts['attachment_parents']),3)
            with harness.store.ledger(harness.db,readonly=True) as con:
                rows=con.execute('SELECT id,original_sha256,parent_occurrence_id FROM occurrences').fetchall()
                self.assertEqual(len(rows),4)
                parents={row['id']:row['original_sha256'] for row in rows}
                for capture in mail_delta.wire_intake_plan(RFC822ConsumerTests().nested()).captures:
                    self.assertTrue(any(row['original_sha256']==capture.sha256 and
                        parents.get(row['parent_occurrence_id'])==capture.parent_sha256 for row in rows))
                self.assertEqual(con.execute("SELECT count(*) FROM stage_state WHERE stage='preserve' AND status='done'").fetchone()[0],4)
            self.assertEqual(fixture.invoke(provider=provider,backend=backend)['messages_preserved'],0)
        finally:harness.doCleanups()

    def test_wire_parent_shape_cannot_launder_legacy_or_forged_locators(self):
        from dataclasses import replace
        from campaign_tool.records.runner.contracts import Preserved
        from campaign_tool.records.runner.core import attachment_identity_ok
        h='a'*64
        valid=Preserved(h,h,h,(h,),(("rfc822:1.2",h),("mime:1.2/1.2",h)),
                         (("rfc822:1.2",None),("mime:1.2/1.2","rfc822:1.2")),("rfc822:1.2",))
        self.assertTrue(attachment_identity_ok(valid))
        for invalid in [replace(valid,attachment_parents=()),
                        replace(valid,eml_parts=()),
                        replace(valid,attachment_parents=(("rfc822:1.2",None),("mime:1.2/1.2",None))),
                        replace(valid,attachment_parents=(("rfc822:1.2",None),("mime:1.2/1.2","rfc822:1.9"))),
                        replace(valid,attachments=(("arbitrary:secret",h),)),
                        replace(valid,attachment_parents=valid.attachment_parents+valid.attachment_parents)]:
            self.assertFalse(attachment_identity_ok(invalid))

    def test_runtime_fingerprint_changes_with_each_wire_dependency(self):
        from campaign_tool.records.runner.core import observed_runtime
        from campaign_tool.records.intake import rfc822_adapter,rfc822_inventory,wire_rfc822
        image='sha256:'+'a'*64
        baseline=observed_runtime(image)
        with tempfile.TemporaryDirectory() as temporary:
            for module in [rfc822_adapter,rfc822_inventory,wire_rfc822]:
                path=Path(temporary)/Path(module.__file__).name
                path.write_bytes(Path(module.__file__).read_bytes()+b'\n# synthetic source change\n')
                with patch.object(module,'__file__',str(path)):
                    changed=observed_runtime(image)
                self.assertNotEqual(changed['code_sha256'],baseline['code_sha256'])
                self.assertEqual(changed['image_verification'],'caller_declared_not_host_verified')
