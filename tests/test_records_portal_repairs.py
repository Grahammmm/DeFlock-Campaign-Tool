"""Independent-finding regressions using synthetic notices and transport only."""
import contextlib
import hashlib
import io
import json
import socket
import unittest
from unittest.mock import patch
from campaign_tool.records.portal import Queue,fetch_queue
from campaign_tool.records.portal.notice import inventory_notice
from campaign_tool.records.portal.policy import PortalError
from campaign_tool.records.portal.__main__ import main
from tests import test_records_portal as fixtures
from tests import test_records_portal_wp1 as canonical


class PortalRepairTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.PortalTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.queue=self.f.queue
        for name in ('socket','getaddrinfo'):
            guard=patch.object(socket,name,side_effect=AssertionError('network forbidden in fixture'))
            guard.start();self.addCleanup(guard.stop)

    def notice(self,body,**changes):
        args=dict(body=body,request_id='26-001',source_sha256=fixtures.SOURCE,known_hosts={fixtures.HOST})
        args.update(changes)
        return inventory_notice(self.queue,**args)

    def inventory(self,url,source=fixtures.SOURCE,**changes):
        return self.queue.inventory(fixtures.HOST,'26-001','42',url,source,**changes)

    def two_versions(self):
        self.f.run_fetch()
        self.queue.inventory(fixtures.HOST,'26-002','43',fixtures.URL.replace('/42/','/43/'),fixtures.SOURCE)
        self.f.run_fetch(fixtures.FakeTransport(fixtures.response(fixtures.PDF+b' independent item')))
        return self.queue.db.execute('SELECT * FROM portal_versions ORDER BY rowid').fetchall()

    def corrupt(self,row):
        path=self.queue.objects/row['sha256'];path.chmod(0o600);path.write_bytes(b'corrupt synthetic bytes')

    def test_old_notice_replay_does_not_refresh(self):
        self.f.run_fetch()
        self.inventory(fixtures.URL+'-new',hashlib.sha256(b'new fixture notice').hexdigest())
        self.f.run_fetch(fixtures.FakeTransport(fixtures.response(fixtures.PDF+b' revision')))
        before=self.f.item()
        self.inventory(fixtures.URL)
        self.assertEqual(self.f.item(),before)
        self.assertEqual(self.queue.status()['versions'],2)
        self.assertEqual(self.queue.db.execute('SELECT count(*) FROM portal_link_revisions').fetchone()[0],2)

    def test_forwarded_old_url_new_source_does_not_reactivate(self):
        self.f.run_fetch()
        self.inventory(fixtures.URL+'-new',hashlib.sha256(b'new URL notice').hexdigest())
        self.f.run_fetch(fixtures.FakeTransport(fixtures.response(fixtures.PDF+b' newer observation')))
        before=self.f.item()
        forwarded=hashlib.sha256(b'forwarded old URL in distinct notice').hexdigest()
        self.inventory(fixtures.URL,forwarded)
        self.assertEqual(self.f.item(),before)
        self.assertEqual(self.queue.status()['versions'],2)
        notices=self.queue.db.execute('SELECT source_sha256 FROM portal_notices WHERE item=?',(self.f.key,)).fetchall()
        self.assertEqual(len(notices),3)
        link=self.queue.db.execute('SELECT url_sha256 FROM portal_notice_links WHERE item=? AND source_sha256=?',(self.f.key,forwarded)).fetchone()
        self.assertEqual(link[0],hashlib.sha256(fixtures.URL.encode()).hexdigest())
        self.assertEqual(self.queue.db.execute('SELECT count(*) FROM portal_link_revisions').fetchone()[0],2)
        self.assertEqual(self.f.run_fetch(fixtures.FakeTransport())['attempted'],0)

    def test_forwarded_known_url_requires_explicit_generation_refresh(self):
        self.inventory(fixtures.URL+'-new',hashlib.sha256(b'new URL observation').hexdigest())
        forwarded=hashlib.sha256(b'forwarded earlier URL').hexdigest()
        self.inventory(fixtures.URL,forwarded)
        self.assertEqual(self.f.item()['generation'],2)
        with self.assertRaisesRegex(PortalError,'refresh_generation_conflict'):
            self.inventory(fixtures.URL,forwarded,refresh=True,expected_generation=1)
        self.inventory(fixtures.URL,forwarded,refresh=True,expected_generation=2)
        self.assertEqual(self.f.item()['generation'],3)
        self.assertEqual(self.f.item()['last_url_private'],fixtures.URL)
        revision=self.queue.db.execute('SELECT reason,source_sha256 FROM portal_link_revisions WHERE generation=3').fetchone()
        self.assertEqual(tuple(revision),('explicit_refresh',forwarded))

    def test_explicit_refresh_preserves_revision_provenance(self):
        self.inventory(fixtures.URL+'-refresh',refresh=True,expected_generation=1)
        revisions=self.queue.db.execute('SELECT * FROM portal_link_revisions ORDER BY generation').fetchall()
        self.assertEqual(len(revisions),2)
        self.assertEqual(revisions[1]['reason'],'explicit_refresh')
        self.assertEqual(revisions[1]['previous_url_sha256'],revisions[0]['url_sha256'])
        self.assertEqual(revisions[1]['source_sha256'],fixtures.SOURCE)
        self.assertNotIn('token=',json.dumps([dict(row) for row in revisions]))

    def test_refresh_requires_generation_and_refuses_stale_generation(self):
        with self.assertRaisesRegex(PortalError,'explicit_refresh_generation'):
            self.inventory(fixtures.URL+'-refresh',refresh=True)
        self.inventory(fixtures.URL+'-refresh',refresh=True,expected_generation=1)
        with self.assertRaisesRegex(PortalError,'refresh_generation_conflict'):
            self.inventory(fixtures.URL+'-again',refresh=True,expected_generation=1)
        self.assertEqual(self.f.item()['generation'],2)

    def test_seen_notice_alternate_link_recorded_not_activated(self):
        self.inventory(fixtures.URL+'-unknown-alternate')
        self.assertEqual(self.f.item()['generation'],1)
        self.assertEqual(self.queue.db.execute('SELECT count(*) FROM portal_notice_links').fetchone()[0],2)

    def test_mixed_help_anchor_and_document_text_detected(self):
        body='<a href="https://portal.example.test/help">help</a><p>'+fixtures.URL.replace('/42/','/43/')+'</p>'
        self.assertEqual(self.notice(body),{'items':1})
        self.assertEqual(len(self.queue.status()['items']),2)

    def test_duplicate_attribute_text_entity_single_generation(self):
        url=fixtures.URL+'&part=1'
        body='<a href="'+url.replace('&','&amp;')+'">'+url.replace('&','&amp;')+'</a>'
        self.assertEqual(self.notice(body,source_sha256=hashlib.sha256(b'entity text fixture').hexdigest()),{'items':1})
        self.assertEqual(self.f.item()['last_url_private'],url)
        self.assertEqual(self.f.item()['generation'],2)

    def test_busy_queue_not_silently_dropped(self):
        with self.queue.lock(),self.assertRaisesRegex(PortalError,'queue_busy'):
            self.notice(fixtures.URL.replace('/42/','/43/'))

    def test_invalid_notice_hash_and_request_raise(self):
        for change in ({'source_sha256':'invalid'},{'request_id':'not valid'}):
            with self.subTest(change=change),self.assertRaises(PortalError):self.notice(fixtures.URL,**change)

    def test_notice_storage_error_propagates(self):
        with patch.object(self.queue,'inventory',side_effect=OSError('synthetic storage failure')):
            with self.assertRaises(OSError):self.notice(fixtures.URL)

    def test_poison_item_does_not_block_unrelated_delivery(self):
        rows=self.two_versions();self.corrupt(rows[0]);ledger=fixtures.MemoryLedger()
        with self.queue.lock():result=self.queue.deliver(ledger,2)
        self.assertEqual((result['attempted'],result['delivered'],result['failed']),(2,1,1))
        self.assertEqual(len(ledger.rows),1)
        self.assertEqual(self.queue.status()['ledger_pending'],1)
        self.assertEqual(self.queue.status()['ledger_failures'][0]['reason'],'object_integrity_failed')

    def test_limit_one_rotates_failed_item(self):
        rows=self.two_versions();self.corrupt(rows[0]);ledger=fixtures.MemoryLedger()
        with self.queue.lock():first=self.queue.deliver(ledger,1)
        with self.queue.lock():second=self.queue.deliver(ledger,1)
        self.assertEqual(first['failed'],1)
        self.assertEqual(second['delivered'],1)
        self.assertEqual(second['attempted'],1)

    def test_delivery_failure_survives_reopen(self):
        rows=self.two_versions();self.corrupt(rows[0])
        with self.queue.lock():self.queue.deliver(fixtures.MemoryLedger(),2)
        other=Queue(self.f.root)
        try:self.assertEqual(other.status()['ledger_failures'],self.queue.status()['ledger_failures'])
        finally:other.close()

    def test_arbitrary_exception_text_not_saved(self):
        self.f.run_fetch()
        ledger=fixtures.MemoryLedger()
        with patch.object(ledger,'record_original',side_effect=RuntimeError(fixtures.URL)):
            with self.queue.lock():result=self.queue.deliver(ledger)
        self.assertEqual(result['failures'][0]['reason'],'ledger_delivery_failed')
        history=[dict(row) for row in self.queue.db.execute('SELECT * FROM portal_delivery_attempts')]
        self.assertNotIn('token=',json.dumps(history))

    def test_recovered_failure_retains_history_clears_active_gap(self):
        self.f.run_fetch();ledger=fixtures.MemoryLedger();ledger.crash=True
        with self.queue.lock():first=self.queue.deliver(ledger)
        with self.queue.lock():second=self.queue.deliver(ledger)
        self.assertEqual((first['failed'],second['delivered']),(1,1))
        self.assertEqual(self.queue.status()['ledger_failures'],[])
        self.assertEqual(self.queue.db.execute('SELECT count(*) FROM portal_delivery_attempts').fetchone()[0],2)
        self.assertEqual(len(ledger.rows),1)

    def test_delivery_limit_refuses_unbounded(self):
        for limit in (0,1001,True):
            with self.assertRaisesRegex(PortalError,'invalid_delivery_limit'):
                self.queue.deliver(fixtures.MemoryLedger(),limit)

    def test_inventory_json_cannot_authorize_refresh(self):
        path=self.f.root/'synthetic-input.json'
        path.write_text(json.dumps([dict(host=fixtures.HOST,request_id='26-001',item_id='42',url=fixtures.URL+'-changed',source_sha256=fixtures.SOURCE,refresh=True,expected_generation=1)]))
        with contextlib.redirect_stdout(io.StringIO()):
            result=main(['--root',str(self.f.root),'inventory','--input',str(path)])
        self.assertEqual(result,2)
        self.assertEqual(self.f.item()['generation'],1)

    def test_fetch_reports_exact_partial_handoff(self):
        rows=self.two_versions();self.corrupt(rows[0])
        result=self.f.run_fetch(fixtures.FakeTransport(),ledger=fixtures.MemoryLedger())
        self.assertEqual(result['ledger_delivered'],1)
        self.assertEqual(result['ledger_pending'],1)
        self.assertEqual(result['ledger_delivery']['failed'],1)
        self.assertEqual(result['ledger_error'],'ledger_delivery_pending')


class PortalCanonicalRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):canonical.WP1PortalTests.setUpClass()

    def setUp(self):
        self.f=canonical.WP1PortalTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        for name in ('socket','getaddrinfo'):
            guard=patch.object(socket,name,side_effect=AssertionError('network forbidden in fixture'))
            guard.start();self.addCleanup(guard.stop)

    def test_old_notice_replay_cannot_rollback_canonical_pointer(self):
        f=self.f;f.fetch()
        f.queue.inventory(fixtures.HOST,'26-001','42',fixtures.URL+'-new',hashlib.sha256(b'newer synthetic notice').hexdigest())
        changed=fixtures.PDF+b' new observation'
        fetch_queue(f.queue,apply=True,approval_loader=fixtures.approval,egress=lambda u:True,
                    transport=fixtures.FakeTransport(fixtures.response(changed)),ledger=f.adapter,wall=lambda:fixtures.NOW.timestamp()+1)
        f.queue.inventory(fixtures.HOST,'26-001','42',fixtures.URL,fixtures.SOURCE)
        result=fetch_queue(f.queue,apply=True,approval_loader=fixtures.approval,egress=lambda u:True,
                          transport=fixtures.FakeTransport(),ledger=f.adapter,wall=lambda:fixtures.NOW.timestamp()+2)
        self.assertEqual(result['attempted'],0)
        with f.store.ledger(f.database,readonly=True) as c:
            current=c.execute('SELECT original_sha256 FROM portal_items').fetchone()[0]
            states=[tuple(row) for row in c.execute('SELECT status,count(*) FROM stage_state GROUP BY status')]
        self.assertEqual(current,hashlib.sha256(changed).hexdigest())
        self.assertEqual(states,[('pending',14)])
        self.assertEqual(f.count('occurrences'),2)

    def test_poison_outbox_other_original_enrolls_pending(self):
        f=self.f
        fetch_queue(f.queue,apply=True,approval_loader=fixtures.approval,egress=lambda u:True,transport=fixtures.FakeTransport(fixtures.response()),wall=lambda:fixtures.NOW.timestamp())
        f.queue.inventory(fixtures.HOST,'26-002','43',fixtures.URL.replace('/42/','/43/'),fixtures.SOURCE)
        fetch_queue(f.queue,apply=True,approval_loader=fixtures.approval,egress=lambda u:True,transport=fixtures.FakeTransport(fixtures.response(fixtures.PDF+b' separate')),wall=lambda:fixtures.NOW.timestamp()+1)
        first=f.queue.db.execute('SELECT * FROM portal_versions ORDER BY rowid LIMIT 1').fetchone()
        bad=f.queue.objects/first['sha256'];bad.chmod(0o600);bad.write_bytes(b'corrupt')
        with f.queue.lock():result=f.queue.deliver(f.adapter)
        self.assertEqual((result['delivered'],result['failed']),(1,1))
        self.assertEqual(f.count('originals'),1)
        self.assertTrue(all(row['pending']==1 and row['done']==0 for row in f.store.counts(f.database)['stages'].values()))
