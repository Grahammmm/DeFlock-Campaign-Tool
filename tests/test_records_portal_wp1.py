"""Optional WP1 overlay tests. Point RECORDS_WP1_SOURCE_ROOT at trusted source.

No WP1 code or schema is copied into this branch. Default main lacks that
separately developed dependency, so this module reports an explicit skip there.
"""
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from campaign_tool.records.portal import Queue,fetch_queue
from campaign_tool.records.portal.ledger_adapter import WP1LedgerAdapter
from campaign_tool.records.portal.runner_bridge import PortalRunnerBridge
from campaign_tool.records.portal.notice import inventory_notice
from campaign_tool.records.portal.policy import PortalError
from tests.test_records_portal import HOST,URL,SOURCE,PDF,NOW,approval,FakeTransport,response


class WP1PortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source=os.environ.get('RECORDS_WP1_SOURCE_ROOT')
        if source:
            import campaign_tool.records
            overlay=Path(source)/'campaign_tool'/'records'
            if not (overlay/'ledger'/'store.py').is_file():raise RuntimeError('invalid explicit WP1 overlay')
            campaign_tool.records.__path__.append(str(overlay))
        try:cls.store=importlib.import_module('campaign_tool.records.ledger.store')
        except ModuleNotFoundError:raise unittest.SkipTest('WP1 dependency unavailable; explicit source overlay required')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.queue=Queue(self.root/'queue')
        inventory_notice(self.queue,body='<a href="'+URL+'">synthetic record</a>',request_id='26-001',source_sha256=SOURCE,known_hosts={HOST})
        self.database=self.root/'canonical'/'ledger.sqlite'
        self.store.initialize(self.database)
        self.adapter=WP1LedgerAdapter(queue=self.queue,database=self.database,cas_root=self.root/'cas')

    def tearDown(self):self.queue.close();self.temp.cleanup()

    def fetch(self,**kwargs):
        return fetch_queue(self.queue,apply=True,approval_loader=approval,egress=lambda u:True,
            transport=FakeTransport(response()),ledger=self.adapter,wall=lambda:NOW.timestamp(),**kwargs)

    def receipt(self):
        row=dict(self.queue.db.execute('SELECT * FROM portal_versions ORDER BY rowid LIMIT 1').fetchone())
        row.pop('ledger_state');row['provenance']=json.loads(row.pop('provenance_json'))
        return row,self.queue.objects/row['sha256']

    def count(self,table):
        with self.store.ledger(self.database,readonly=True) as con:
            return con.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def test_notice_bytes_ledger_pending_card_end_to_end(self):
        result=self.fetch()
        self.assertEqual(result['ledger_delivered'],1)
        self.assertEqual(self.count('originals'),1)
        self.assertEqual(self.count('occurrences'),1)
        self.assertEqual(self.count('portal_items'),1)
        self.assertEqual(self.count('runs'),1)
        counts=self.store.counts(self.database)
        self.assertEqual(counts['stage_slots_observed'],7)
        self.assertTrue(all(x['pending']==1 and x['done']==0 for x in counts['stages'].values()))
        cards=self.adapter.pending_card_inputs()
        self.assertEqual(len(cards),1)
        self.assertEqual(Path(cards[0]['storage_path']).read_bytes(),PDF)
        self.assertEqual(cards[0]['status'],'pending')

    def test_replay_does_not_duplicate(self):
        self.fetch();receipt,path=self.receipt()
        self.adapter.record_original(receipt,path)
        self.assertEqual(self.count('originals'),1)
        self.assertEqual(self.count('occurrences'),1)
        self.assertEqual(self.count('runs'),1)
        self.assertEqual(self.count('stage_events'),7)

    def test_crash_after_portal_bytes_retries_to_ledger(self):
        def crash(stage):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.fetch(fault=crash)
        self.assertEqual(self.count('originals'),0)
        self.fetch()
        self.assertEqual(self.count('originals'),1)
        self.assertEqual(self.count('occurrences'),1)

    def test_crash_after_canonical_bytes_before_sql(self):
        def crash(stage):
            if stage=='after_canonical_bytes':raise RuntimeError('fixture crash')
        self.adapter.fault=crash;self.fetch()
        self.assertEqual(self.count('originals'),0)
        self.adapter.fault=None;self.fetch()
        self.assertEqual(self.count('originals'),1)
        self.assertEqual(self.queue.status()['ledger_pending'],0)

    def test_failure_before_commit_is_atomic(self):
        def crash(stage):
            if stage=='before_canonical_commit':raise RuntimeError('fixture crash')
        self.adapter.fault=crash;self.fetch()
        for table in ('originals','occurrences','portal_items','runs','stage_state'):
            self.assertEqual(self.count(table),0)
        self.adapter.fault=None;self.fetch()
        self.assertEqual(self.count('stage_state'),7)

    def test_crash_after_ledger_commit_before_outbox_ack(self):
        def crash(stage):
            if stage=='after_canonical_commit':raise RuntimeError('fixture crash')
        self.adapter.fault=crash;self.fetch()
        self.assertEqual(self.count('originals'),1)
        self.assertEqual(self.queue.status()['ledger_pending'],1)
        self.adapter.fault=None;self.fetch()
        self.assertEqual(self.count('occurrences'),1)
        self.assertEqual(self.queue.status()['ledger_pending'],0)

    def test_corrupt_canonical_cas_refuses_replay(self):
        self.fetch();receipt,path=self.receipt()
        canonical=self.adapter.cas/receipt['sha256'];canonical.chmod(0o600);canonical.write_bytes(b'corrupt')
        with self.assertRaises(PortalError):self.adapter.record_original(receipt,path)

    def test_corrupt_source_cas_refuses_replay(self):
        self.fetch();receipt,path=self.receipt();path.chmod(0o600);path.write_bytes(b'corrupt')
        with self.assertRaises(PortalError):self.adapter.record_original(receipt,path)

    def test_receipt_and_attempt_binding_rejects_mutation(self):
        self.fetch();receipt,path=self.receipt()
        self.queue.db.execute("UPDATE portal_attempts SET result='failed'");self.queue.db.commit()
        with self.assertRaisesRegex(PortalError,'portal_attempt_unbound'):self.adapter.record_original(receipt,path)

    def test_occurrence_mutation_rejected(self):
        self.fetch();receipt,path=self.receipt()
        with self.store.ledger(self.database) as con:
            con.execute("UPDATE occurrences SET source_ref='changed'");con.commit()
        with self.assertRaisesRegex(PortalError,'canonical_occurrence_changed'):self.adapter.record_original(receipt,path)

    def test_changed_bytes_versions_and_old_replay(self):
        self.fetch();old,path=self.receipt()
        self.queue.inventory(HOST,'26-001','42',URL+'-refresh',SOURCE)
        fetch_queue(self.queue,apply=True,approval_loader=approval,egress=lambda u:True,
            transport=FakeTransport(response(PDF+b' changed')),ledger=self.adapter,wall=lambda:NOW.timestamp()+1)
        self.assertEqual(self.count('originals'),2)
        self.assertEqual(self.count('occurrences'),2)
        self.assertEqual(self.count('stage_state'),14)
        self.adapter.record_original(old,path)
        with self.store.ledger(self.database,readonly=True) as con:
            self.assertNotEqual(con.execute('SELECT original_sha256 FROM portal_items').fetchone()[0],old['sha256'])

    def test_no_trusted_stage_adapter_no_promotion(self):
        self.fetch()
        with self.assertRaisesRegex(PortalError,'trusted_portal_stage_adapter_unconfigured'):
            self.adapter.validate_and_promote({},'synthetic-run')
        self.assertEqual(self.store.counts(self.database)['stages']['preserve']['done'],0)

    def test_same_bytes_two_items_keep_distinct_occurrences(self):
        self.fetch()
        self.queue.inventory(HOST,'26-002','43',URL.replace('42','43'),SOURCE)
        self.fetch()
        self.assertEqual(self.count('originals'),1)
        self.assertEqual(self.count('occurrences'),2)
        self.assertEqual(self.count('portal_items'),2)
        self.assertEqual(self.count('stage_state'),7)

    def test_old_replay_detects_corrupt_current_portal_pointer(self):
        self.fetch();old,path=self.receipt()
        self.queue.inventory(HOST,'26-001','42',URL+'-refresh',SOURCE)
        fetch_queue(self.queue,apply=True,approval_loader=approval,egress=lambda u:True,
            transport=FakeTransport(response(PDF+b' changed')),ledger=self.adapter,wall=lambda:NOW.timestamp()+1)
        with self.store.ledger(self.database) as con:
            con.execute('UPDATE portal_items SET original_sha256=?',(old['sha256'],));con.commit()
        with self.assertRaisesRegex(PortalError,'canonical_portal_version_changed'):
            self.adapter.record_original(old,path)

    def test_source_open_failure_closes_temporary_descriptor(self):
        import tempfile as temporary_files
        import hashlib
        allocated=[]
        actual=temporary_files.mkstemp
        def allocate(*args,**kwargs):
            fd,name=actual(*args,**kwargs);allocated.append(fd);return fd,name
        with patch('campaign_tool.records.portal.ledger_adapter.tempfile.mkstemp',side_effect=allocate):
            with self.assertRaises(FileNotFoundError):
                self.adapter._copy_cas(self.root/'absent-source',hashlib.sha256(PDF).hexdigest(),len(PDF))
        self.assertEqual(len(allocated),1)
        with self.assertRaises(OSError):os.fstat(allocated[0])
        self.assertEqual(list(self.adapter.cas.iterdir()),[])

    def test_runner_bridge_returns_pending_input(self):
        bridge=PortalRunnerBridge(queue=self.queue,ledger=self.adapter,approval_loader=approval,
            egress=lambda u:True,transport=FakeTransport(response()))
        outcome=bridge.run(apply=True)
        self.assertEqual(outcome['stage_promotions'],0)
        self.assertEqual(len(outcome['pending_card_inputs']),1)
        self.assertFalse(outcome['pipeline_complete'])


if __name__=='__main__':unittest.main()
