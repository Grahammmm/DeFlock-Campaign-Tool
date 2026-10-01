"""Offline representative slice. Synthetic promotion double is NOT WP1 acceptance."""
import hashlib
import json
import multiprocessing
from pathlib import Path
import sqlite3
import unittest
from campaign_tool.records.runner import core
from campaign_tool.records.runner.adapters import LegacyIntakeBackend,OfflineExporter
from campaign_tool.records.runner.contracts import Folder,StageReceipt,IntegrationGap
from tests.records import test_mail_delta as fixtures

class Transcript:
    """Recorded provider boundary; never opens a socket or considers read flags."""
    def __init__(self,scope,uids,receipt,drop_uid=None):
        self.scopes=scope;self.items=uids;self.receipt_factory=receipt;self.drop_uid=drop_uid
    def folders(self):return self.scopes
    def uids(self,scope,after):return [u for u in self.items if u>after]
    def receipt(self,scope,uid):
        if uid==self.drop_uid:raise ConnectionError('fictional credential must not be logged')
        return self.receipt_factory(scope,uid)

class SyntheticPromotingBackend(LegacyIntakeBackend):
    """Test double only: validates bindings and records idempotent acknowledgments."""
    def __init__(self,*args):super().__init__(*args);self.accepted={};self.after_commit_crash=False
    def validate_and_promote(self,receipt,identity):
        receipt.validate();key=(receipt.subject_sha256,receipt.stage)
        if key in self.accepted and self.accepted[key]!=receipt.sha256:raise ValueError('changed receipt')
        self.accepted[key]=receipt.sha256
        if self.after_commit_crash:
            self.after_commit_crash=False;raise KeyboardInterrupt('simulated process crash')

def process_lock_probe(root,queue):
    try:
        with core.writer_lock(root):queue.put('acquired')
    except ValueError:queue.put('blocked')

class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.delta=fixtures.MailDeltaTests();self.delta.setUp();self.addCleanup(self.delta.doCleanups)
        self.root=self.delta.base/'control';self.root.mkdir(mode=0o700)
        self.profile_path=self.root/'profile.json';self.time=1000
        self.runtime=core.observed_runtime('sha256:'+hashlib.sha256(b'fictional image').hexdigest())
        self.profile={'mode':'fixture','version':1,'account_id':'synthetic-account','folders':['INBOX'],'stages':['catalog'],
                      'runtime':dict(self.runtime),'retry_delay':1}
        self.write_profile()
        self.backend=SyntheticPromotingBackend(self.delta.root,self.delta.out)
        self.scopes=[Folder('INBOX',9)];self.uids=[64]
        self.hook_calls=[]
    def write_profile(self):
        self.profile_path.write_text(json.dumps(self.profile));self.profile_path.chmod(0o600)
    def receipt(self,scope,uid):
        row=dict(self.delta.receipt,folder=scope.name,uidvalidity=scope.uidvalidity,uid=uid)
        p=self.delta.base/(hashlib.sha256(json.dumps([scope.name,scope.uidvalidity,uid]).encode()).hexdigest()+'.json')
        p.write_text(json.dumps(row));p.chmod(0o600);return str(p)
    def hook(self,sha,identity):
        self.hook_calls.append(sha)
        return StageReceipt(sha,'catalog',json.dumps({'subject_sha256':sha,'stage':'catalog','fixture':True}).encode())
    def invoke(self,*,provider=None,backend=None,hooks=None):
        return core.run(self.root,self.profile_path,provider or Transcript(self.scopes,self.uids,self.receipt),
                        backend or self.backend,{'catalog':self.hook} if hooks is None else hooks,
                        runtime_provider=lambda:self.runtime,clock=lambda:self.time)
    def query(self,sql):
        with sqlite3.connect(self.root/'runner.sqlite') as c:return c.execute(sql).fetchall()
    def test_representative_receipt_preservation_to_catalog_hook(self):
        r=self.invoke();self.assertEqual(r['messages_preserved'],1);self.assertEqual(r['stage_promotions'],2)
        self.assertEqual(len(self.hook_calls),2);self.assertFalse(r['pipeline_complete']);self.assertFalse(r['canonical_integration_verified'])
        self.assertEqual(self.delta.db.execute('SELECT count(*) FROM preservations').fetchone()[0],3)
        evidence=json.loads(self.query('SELECT evidence FROM runner_messages')[0][0])
        self.assertEqual(evidence['attachments'][0][0],'1.2')
    def test_repeat_run_no_duplicate_messages_or_catalog(self):
        self.invoke();r=self.invoke();self.assertEqual(r['messages_preserved'],0);self.assertEqual(r['stage_promotions'],0)
        self.assertEqual(self.query('SELECT count(*) FROM runner_messages'),[(1,)])
        self.assertEqual(len(self.hook_calls),2)
    def test_same_bytes_other_folder_new_occurrence_not_original(self):
        self.invoke();before=self.delta.db.execute('SELECT count(*) FROM docs').fetchone()[0]
        self.profile['folders'].append('Archive');self.write_profile();self.scopes.append(Folder('Archive',9))
        r=self.invoke();self.assertEqual(r['messages_preserved'],1)
        self.assertEqual(self.query('SELECT count(*) FROM runner_messages'),[(2,)])
        self.assertEqual(self.delta.db.execute('SELECT count(*) FROM docs').fetchone()[0],before)
    def test_uidvalidity_reset_reenumerates_retains_hashes(self):
        self.invoke();self.scopes=[Folder('INBOX',10)];r=self.invoke()
        self.assertEqual(r['messages_preserved'],1);self.assertEqual(self.query('SELECT count(*) FROM runner_messages'),[(2,)])
        self.assertEqual(self.query('SELECT count(DISTINCT eml_sha) FROM runner_messages'),[(1,)])
        self.assertEqual(self.query("SELECT count(*) FROM runner_events WHERE code='uidvalidity_reset_reenumerate'"),[(1,)])
    def test_connection_drop_keeps_success_and_checkpoint_before_failure(self):
        self.uids=[64,65];provider=Transcript(self.scopes,self.uids,self.receipt,drop_uid=65)
        r=self.invoke(provider=provider);self.assertEqual(r['mail_failures'],1)
        self.assertEqual(self.query('SELECT highest_uid FROM runner_folders'),[(64,)])
        r=self.invoke();self.assertEqual(r['messages_preserved'],1)
        self.assertEqual(self.query('SELECT highest_uid FROM runner_folders'),[(65,)])
        self.assertEqual(self.query('SELECT count(*) FROM runner_messages'),[(2,)])
    def test_unconfigured_and_missing_folders_visible(self):
        self.scopes.append(Folder('Other',7));self.profile['folders'].append('Missing');self.write_profile()
        r=self.invoke();self.assertEqual(r['folder_alerts'],2)
    def test_owner_lock_conflict(self):
        with core.writer_lock(self.root):
            with self.assertRaisesRegex(ValueError,'already_owned'):self.invoke()
    def test_second_process_lock(self):
        ctx=multiprocessing.get_context('spawn');q=ctx.Queue()
        with core.writer_lock(self.root):
            p=ctx.Process(target=process_lock_probe,args=(str(self.root),q));p.start();p.join(5)
            if p.is_alive():p.terminate();p.join();self.fail('synthetic lock probe timeout')
            self.assertEqual(q.get(timeout=1),'blocked')
    def test_runtime_pin_mismatch_refuses_before_journal(self):
        self.profile['runtime']['version']='fictional wrong version';self.write_profile()
        with self.assertRaisesRegex(ValueError,'pin_mismatch'):self.invoke()
        self.assertFalse((self.root/'runner.sqlite').exists())
    def test_private_profile_boundary(self):
        self.profile_path.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'private_control'):self.invoke()
    def test_backend_promotion_gap_explicit(self):
        r=self.invoke(backend=LegacyIntakeBackend(self.delta.root,self.delta.out))
        self.assertEqual(r['stage_promotions'],0);self.assertEqual(r['stage_failures'],2)
        self.assertEqual(len(self.hook_calls),2)
        self.assertEqual(self.query("SELECT DISTINCT error_code FROM runner_work"),[('wp1_promotion_adapter_unavailable',)])
    def test_hook_failure_redacted_and_retry_bounded(self):
        def broken(sha,identity):raise RuntimeError('fictional-private-secret-narrative')
        for _ in range(4):self.invoke(hooks={'catalog':broken});self.time+=2
        self.assertEqual(self.query('SELECT DISTINCT attempts FROM runner_work'),[(3,)])
        self.assertNotIn('fictional-private',str(self.query('SELECT * FROM runner_events')))
    def test_crash_after_backend_commit_retries_idempotently(self):
        self.backend.after_commit_crash=True
        with self.assertRaises(KeyboardInterrupt):self.invoke()
        self.assertEqual(self.query("SELECT count(*) FROM runner_runs WHERE status='running'"),[(1,)])
        self.time+=1;r=self.invoke();self.assertEqual(r['recovered_runs'],1)
        self.assertEqual(self.query("SELECT count(*) FROM runner_work WHERE state='done'"),[(2,)])
        self.assertEqual(len(self.backend.accepted),2)
    def test_expired_lease_recovered_under_lock(self):
        self.backend.after_commit_crash=True
        with self.assertRaises(KeyboardInterrupt):self.invoke()
        self.time+=1000;r=self.invoke();self.assertEqual(r['recovered_runs'],1)
        self.assertEqual(self.query("SELECT count(*) FROM runner_work WHERE state='running'"),[(0,)])
    def test_future_retry_not_immediately_reexecuted(self):
        backend=LegacyIntakeBackend(self.delta.root,self.delta.out);self.invoke(backend=backend)
        r=self.invoke(backend=backend);self.assertEqual(r['stage_failures'],0)
        self.assertEqual(self.query('SELECT DISTINCT attempts FROM runner_work'),[(1,)])
    def test_wrong_stage_receipt_cannot_promote(self):
        def wrong(sha,identity):return StageReceipt(sha,'catalog',b'{"stage":"catalog","subject_sha256":"incorrect"}')
        r=self.invoke(hooks={'catalog':wrong});self.assertEqual(r['stage_promotions'],0)
    def test_message_budget_checkpoint_exact(self):
        self.uids=[64,65,66];self.profile['max_messages']=1;self.write_profile()
        self.invoke();self.assertEqual(self.query('SELECT highest_uid FROM runner_folders'),[(64,)])
        self.invoke();self.assertEqual(self.query('SELECT highest_uid FROM runner_folders'),[(65,)])
    def test_uid_order_invalid_never_skips_gap(self):
        class Bad(Transcript):
            def uids(self,scope,after):return [64,63]
        r=self.invoke(provider=Bad(self.scopes,self.uids,self.receipt))
        self.assertEqual(r['mail_failures'],1);self.assertEqual(self.query('SELECT highest_uid FROM runner_folders'),[(64,)])
    def test_receipt_scope_mismatch_keeps_checkpoint(self):
        provider=Transcript(self.scopes,self.uids,lambda scope,uid:self.receipt(scope,65))
        r=self.invoke(provider=provider);self.assertEqual(r['mail_failures'],1)
        self.assertEqual(self.query('SELECT highest_uid FROM runner_folders'),[(0,)])
    def test_offline_exporter_manifest_contract(self):
        receipt_path=self.delta.root/'receipt.json';receipt_path.write_text(json.dumps(self.delta.receipt))
        manifest=self.delta.root/'manifest.json';manifest.write_text(json.dumps({'version':1,'complete':True,
          'folders':[{'name':'INBOX','uidvalidity':9,'messages':[{'uid':64,'receipt':'receipt.json'}]}]}))
        r=self.invoke(provider=OfflineExporter(self.delta.root,'manifest.json'))
        self.assertEqual(r['messages_preserved'],1)
    def test_read_flags_not_a_checkpoint_input(self):
        # Transcript has only immutable UID identities; no Seen/unread predicate exists.
        self.invoke();self.assertEqual(self.invoke()['messages_preserved'],0)
    def test_missing_hook_reports_integration_gap(self):
        r=self.invoke(hooks={});self.assertEqual(r['stage_promotions'],0);self.assertEqual(r['stage_failures'],2)
    def test_missing_hook_never_exhausts_retries(self):
        self.profile.update(retry_limit=2);self.write_profile()
        for _ in range(5):
            r=self.invoke(hooks={});self.time+=2
            self.assertEqual(r['stage_failures'],2);self.assertEqual(r['status'],'completed_with_gaps')
        self.assertEqual(self.query("SELECT state,attempts,error_code FROM runner_work"),[('pending',0,None)]*2)
        self.assertEqual(self.query("SELECT count(*) FROM runner_events WHERE kind='stage_gap' AND code='stage_hook_unavailable'"),[(10,)])
        r=self.invoke();self.assertEqual(r['stage_promotions'],2)
        self.assertEqual(self.query("SELECT DISTINCT state,attempts FROM runner_work"),[('done',1)])
    def test_hook_reported_gap_keeps_attempts_and_original_error(self):
        self.profile.update(retry_limit=2);self.write_profile()
        def failing(sha,identity):raise ValueError('synthetic blocked item')
        def unavailable(sha,identity):raise IntegrationGap('stage_hook_unavailable')
        self.invoke(hooks={'catalog':failing});self.time+=2
        before=self.query("SELECT subject_sha,state,attempts,error_code FROM runner_work ORDER BY subject_sha")
        self.assertEqual(len(before),2)
        self.assertTrue(all(row[1:3]==('blocked',1) and row[3] and row[3]!='stage_hook_unavailable' for row in before))
        for _ in range(4):
            r=self.invoke(hooks={'catalog':unavailable});self.time+=2;self.assertEqual(r['stage_failures'],2)
        self.assertEqual(self.query("SELECT subject_sha,state,attempts,error_code FROM runner_work ORDER BY subject_sha"),before)
        self.assertEqual(self.invoke()['stage_promotions'],2)

    def test_crash_after_preservation_before_control_checkpoint(self):
        class CrashBackend(SyntheticPromotingBackend):
            crashed=False
            def preserve(self,*args):
                value=super().preserve(*args)
                if not self.crashed:self.crashed=True;raise KeyboardInterrupt('synthetic preservation crash')
                return value
        backend=CrashBackend(self.delta.root,self.delta.out)
        with self.assertRaises(KeyboardInterrupt):self.invoke(backend=backend)
        self.assertEqual(self.query('SELECT highest_uid FROM runner_folders'),[(0,)])
        before=self.delta.db.execute('SELECT count(*) FROM docs').fetchone()[0]
        r=self.invoke(backend=backend);self.assertEqual(r['messages_preserved'],1)
        self.assertEqual(self.delta.db.execute('SELECT count(*) FROM docs').fetchone()[0],before)
        self.assertEqual(self.query('SELECT count(*) FROM runner_messages'),[(1,)])
    def test_enumeration_drop_after_complete_uid(self):
        class DropSearch(Transcript):
            def uids(self,scope,after):
                yield 64
                raise ConnectionError('synthetic search drop')
        self.uids=[64,65];r=self.invoke(provider=DropSearch(self.scopes,self.uids,self.receipt))
        self.assertEqual(r['mail_failures'],1);self.assertEqual(self.query('SELECT highest_uid FROM runner_folders'),[(64,)])
        self.assertEqual(self.invoke()['messages_preserved'],1)
    def test_bounded_queue_does_not_starve_first_stage(self):
        self.profile['stages']=['extract','catalog'];self.profile['max_work']=1;self.write_profile()
        def extract(sha,identity):return StageReceipt(sha,'extract',json.dumps({'subject_sha256':sha,'stage':'extract'}).encode())
        first=self.invoke(hooks={'extract':extract,'catalog':self.hook})
        self.assertEqual(first['stage_promotions'],1)
        self.assertEqual(self.query("SELECT count(*) FROM runner_work WHERE stage='extract' AND state='done'"),[(1,)])
        self.assertGreater(first['unfinished_work'],0)
        second=self.invoke(hooks={'extract':extract,'catalog':self.hook})
        self.assertEqual(second['stage_promotions'],1)
        self.assertEqual(self.query("SELECT count(*) FROM runner_work WHERE stage='catalog' AND state='done'"),[(1,)])
    def test_failed_run_with_abandoned_lease_recovers(self):
        self.backend.after_commit_crash=True
        with self.assertRaises(KeyboardInterrupt):self.invoke()
        with sqlite3.connect(self.root/'runner.sqlite') as c:c.execute("UPDATE runner_runs SET status='failed'")
        self.invoke();self.assertEqual(self.query("SELECT count(*) FROM runner_work WHERE state='running'"),[(0,)])
        self.assertEqual(self.query("SELECT count(*) FROM runner_work WHERE state='done'"),[(2,)])
    def test_profile_stage_change_keeps_old_pending_visible(self):
        self.invoke(backend=LegacyIntakeBackend(self.delta.root,self.delta.out))
        self.profile['stages']=['extract'];self.write_profile();self.time+=2
        self.invoke(hooks={});self.assertEqual(self.query("SELECT count(*) FROM runner_work WHERE stage='catalog' AND state='blocked'"),[(2,)])

if __name__=='__main__':unittest.main()
