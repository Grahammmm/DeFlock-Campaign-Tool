"""Optional explicit trusted WP1 source overlay, never copied into the WP2 tree."""
import json
import os
from pathlib import Path
import unittest
import campaign_tool.records
from tests.records import test_runner as fixtures

def _wp1_available():
    """WP1 is in-tree after the records merge; an explicit overlay remains supported."""
    if os.environ.get('WP1_OVERLAY'):
        return True
    import importlib.util
    try:
        return importlib.util.find_spec('campaign_tool.records.ledger.store') is not None
    except ModuleNotFoundError:
        return False

@unittest.skipUnless(_wp1_available(),'WP1 not in tree and no overlay supplied; integration unavailable')
class WP1OverlayTests(unittest.TestCase):
    def setUp(self):
        if os.environ.get('WP1_OVERLAY'):
            overlay=Path(os.environ['WP1_OVERLAY'])/'campaign_tool'/'records'
            if str(overlay) not in campaign_tool.records.__path__:campaign_tool.records.__path__.append(str(overlay))
        from campaign_tool.records.ledger import store,stages
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        self.fixture=fixtures.RunnerTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.db=self.fixture.delta.base/'canonical'/'ledger.sqlite';store.initialize(self.db)
        def synthetic_factory(database,identity,validator):
            return stages.testing_runner(database,run_id=identity['run_id'],owner='runner-preserver',
                engine_version=identity['runtime']['version'],config_sha256=identity['config_sha256'],
                validators={'preserve':validator},version='fixture-cas-preserver-v1')
        self.backend=CanonicalMailBackend(self.fixture.delta.root,self.fixture.delta.out,self.db,
            stage_runner_factory=synthetic_factory)
        self.store=store;self.stages=stages
    def test_canonical_preservation_and_catalog_hook_not_catalog_acceptance(self):
        result=self.fixture.invoke(backend=self.backend)
        counts=self.stages.query_counts(self.db)
        with self.store.ledger(self.db,readonly=True) as c:
            states=[tuple(r) for r in c.execute("SELECT stage,status,count(*) FROM stage_state GROUP BY stage,status")]
        self.assertIn(('preserve','done',2),states)
        self.assertIn(('catalog','pending',2),states)
        self.assertEqual(len(self.fixture.hook_calls),2);self.assertEqual(result['stage_promotions'],0)
        self.assertEqual(result['stage_failures'],2);self.assertEqual(counts['end_to_end_complete'],0)
    def test_missing_installed_authority_keeps_preservation_pending(self):
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        backend=CanonicalMailBackend(self.fixture.delta.root,self.fixture.delta.out,self.db,installed_preservation=False)
        result=self.fixture.invoke(backend=backend)
        with self.store.ledger(self.db,readonly=True) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM stage_state WHERE stage='preserve' AND status='done'").fetchone()[0],0)
        self.assertEqual(result['messages_preserved'],0)
    def test_relocated_identical_receipt_retains_first_occurrence_locator(self):
        self.fixture.invoke(backend=self.backend)
        import shutil
        scope=self.fixture.scopes[0];first=self.fixture.receipt(scope,64)
        moved=self.fixture.delta.base/'relocated-receipt.json';shutil.copyfile(first,moved);moved.chmod(0o600)
        self.backend.preserve(str(moved),'synthetic-account',scope,64)
        with self.store.ledger(self.db,readonly=True) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM occurrences').fetchone()[0],2)
    def test_replay_does_not_require_pruned_prior_export_receipt(self):
        self.fixture.invoke(backend=self.backend)
        import shutil
        scope=self.fixture.scopes[0];first=Path(self.fixture.receipt(scope,64))
        moved=self.fixture.delta.base/'replayed-receipt.json';shutil.copyfile(first,moved);moved.chmod(0o600)
        first.unlink()  # The superseded export path may be pruned after verification.
        self.backend.preserve(str(moved),'synthetic-account',scope,64)
        with self.store.ledger(self.db,readonly=True) as c:
            paths={json.loads(r[0])['export_receipt_path'] for r in c.execute('SELECT evidence FROM occurrences')}
        self.assertEqual(paths,{str(first.absolute())})
    def test_controlled_export_proof_to_canonical_preservation_and_catalog_gap(self):
        from tests.records.test_export_proof import ExportProofTests
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        sample=ExportProofTests();sample.setUp();self.addCleanup(sample.doCleanups)
        database=sample.fixture.delta.base/'proof-canonical'/'ledger.sqlite';self.store.initialize(database)
        backend=CanonicalMailBackend(sample.root,sample.fixture.delta.out,database)
        result=sample.fixture.invoke(provider=sample.wrapper(),backend=backend)
        self.assertEqual(result['messages_preserved'],1)
        self.assertEqual(result['stage_promotions'],0)
        self.assertEqual(result['stage_failures'],2)
        with self.store.ledger(database,readonly=True) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM stage_state WHERE stage='preserve' AND status='done'").fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM stage_validation_authority WHERE test_only=0').fetchone()[0],2)
        self.assertEqual(self.stages.query_counts(database)['end_to_end_complete'],0)
        self.assertFalse(result['release_ready']);self.assertIn('verified_mail_cutoff',result)
    def test_installed_domain_adapter_accepts_only_actual_preservation(self):
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        backend=CanonicalMailBackend(self.fixture.delta.root,self.fixture.delta.out,self.db)
        result=self.fixture.invoke(backend=backend)
        self.assertEqual(result['messages_preserved'],1)
        with self.store.ledger(self.db,readonly=True) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM stage_state WHERE stage='preserve' AND status='done'").fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM stage_validation_authority WHERE test_only=0').fetchone()[0],2)
        self.assertEqual(self.stages.query_counts(self.db)['end_to_end_complete'],0)
    def test_metadata_path_streams_actual_large_fixture_without_raw_stage_blob(self):
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        from email.message import EmailMessage
        from email import policy
        import copy,hashlib
        row=copy.deepcopy(self.fixture.delta.receipt)
        payload=b'fixture large bytes\n'*300000
        message=EmailMessage();message['Subject']='Synthetic large preservation';message.set_content('Synthetic body')
        attachment=row['attachments'][0];major,minor=attachment['content_type'].split('/',1)
        message.add_attachment(payload,maintype=major,subtype=minor,filename=attachment['original_filename'])
        eml=message.as_bytes(policy=policy.SMTP)
        for item,raw in ((row['original_eml'],eml),(attachment,payload)):
            path=Path(item['path']);path=path if path.is_absolute() else self.fixture.delta.root/path
            path.write_bytes(raw);item['bytes']=len(raw);item['sha256']=hashlib.sha256(raw).hexdigest()
        row['bytes']=len(eml);self.fixture.delta.receipt=row
        backend=CanonicalMailBackend(self.fixture.delta.root,self.fixture.delta.out,self.db)
        result=self.fixture.invoke(backend=backend)
        self.assertGreater(len(payload),4*1024*1024);self.assertEqual(result['messages_preserved'],1)
        with self.store.ledger(self.db,readonly=True) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM stage_state WHERE stage='preserve' AND status='done'").fetchone()[0],2)
            self.assertLess(c.execute('SELECT max(length(payload)) FROM stage_artifacts').fetchone()[0],64*1024)
            self.assertEqual(c.execute('SELECT count(*) FROM stage_validation_authority WHERE test_only=0').fetchone()[0],2)
        self.assertEqual(self.stages.query_counts(self.db)['end_to_end_complete'],0)
    def test_changed_immutable_verification_receipt_rejected_on_replay(self):
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        backend=CanonicalMailBackend(self.fixture.delta.root,self.fixture.delta.out,self.db)
        self.fixture.invoke(backend=backend)
        objects=list((self.fixture.delta.out/'verification-receipts').iterdir())
        self.assertEqual(len(objects),1);objects[0].chmod(0o600);objects[0].write_bytes(b'changed proof')
        scope=self.fixture.scopes[0]
        with self.assertRaises(ValueError):backend.preserve(self.fixture.receipt(scope,64),'synthetic-account',scope,64)
    def test_parent_mail_contract_distinguishes_hash_roles_and_pending_stages(self):
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        import hashlib,json
        backend=CanonicalMailBackend(self.fixture.delta.root,self.fixture.delta.out,self.db)
        result=self.fixture.invoke(backend=backend)
        scope=self.fixture.scopes[0]
        value=backend.preserve(self.fixture.receipt(scope,64),'synthetic-account',scope,64)
        self.assertEqual(result['messages_preserved'],1)
        self.assertEqual(value.documents,tuple(sorted(set(value.documents))))
        self.assertIn(value.eml_sha256,value.documents)
        self.assertNotEqual(value.eml_sha256,value.receipt_sha256)
        self.assertEqual(len(value.attachments),1)
        for locator,subject in value.attachments:
            self.assertRegex(locator,r'^1(?:\.[1-9][0-9]*)+$');self.assertIn(subject,value.documents)
        with self.store.ledger(self.db,readonly=True) as c:
            for subject in value.documents:
                state=c.execute("SELECT status,receipt_sha256 FROM stage_state WHERE original_sha256=? AND stage='preserve'",(subject,)).fetchone()
                self.assertEqual(state['status'],'done')
                head=c.execute("SELECT content_sha256 FROM stage_content WHERE subject_sha256=? AND stage='preserve' ORDER BY revision DESC LIMIT 1",(subject,)).fetchone()
                raw=c.execute('SELECT payload FROM stage_artifacts WHERE sha256=?',(head[0],)).fetchone()[0]
                envelope=json.loads(raw)
                self.assertEqual(hashlib.sha256(raw).hexdigest(),head[0])
                self.assertEqual(envelope['schema'],'preservation-evidence-v1')
                self.assertEqual(envelope['original_sha256'],subject)
                self.assertEqual(envelope['verification_receipt_sha256'],value.receipt_sha256)
                self.assertNotEqual(head[0],subject);self.assertNotEqual(head[0],value.receipt_sha256)
                self.assertEqual(c.execute("SELECT count(*) FROM stage_state WHERE original_sha256=? AND stage!='preserve' AND status='pending'",(subject,)).fetchone()[0],6)
            authority=c.execute('SELECT test_only FROM stage_runner_bindings WHERE run_id=?',(result['run_id'],)).fetchone()
            self.assertEqual(authority[0],0)
        self.assertEqual(result['stage_promotions'],0)
        self.assertEqual(self.stages.query_counts(self.db)['end_to_end_complete'],0)
    def test_mail_projection_uses_actual_eml_headers_and_native_delivery_scope(self):
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        from email.parser import BytesParser
        from email import policy
        import json
        self.fixture.delta.receipt['headers']={'Subject':'fabricated receipt subject','Message-ID':'fabricated receipt ID'}
        backend=CanonicalMailBackend(self.fixture.delta.root,self.fixture.delta.out,self.db)
        result=self.fixture.invoke(backend=backend)
        self.assertEqual(result['messages_preserved'],1)
        sha=self.fixture.delta.receipt['original_eml']['sha256']
        parsed=BytesParser(policy=policy.default).parsebytes((self.fixture.delta.out/'blobs'/sha).read_bytes())
        with self.store.ledger(self.db,readonly=True) as c:
            row=c.execute('SELECT * FROM mail_messages').fetchone();headers=json.loads(row['headers'])
            self.assertEqual(headers['fields'],[[str(k),str(v)] for k,v in parsed.items()])
            self.assertFalse(headers['provider_attested']);self.assertNotIn('fabricated receipt subject',row['headers'])
            self.assertEqual((row['account'],row['folder'],row['uidvalidity'],row['uid']),('synthetic-account','INBOX',9,64))
            self.assertEqual(row['account_provenance_status'],'configured_namespace_unattested')
            self.assertEqual(row['eml_sha256'],sha)
            self.assertEqual(row['message_id'],str(parsed.get('Message-ID')).strip() if parsed.get('Message-ID') else None)
    def test_native_mail_collision_rolls_back_all_new_canonical_rows(self):
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        import json
        backend=CanonicalMailBackend(self.fixture.delta.root,self.fixture.delta.out,self.db)
        self.fixture.invoke(backend=backend)
        with self.store.ledger(self.db) as c:
            prior=c.execute('SELECT id FROM mail_messages').fetchone()[0]
            c.execute("UPDATE mail_messages SET uid=65,message_id='different declared ID' WHERE id=?",(prior,));c.commit()
        self.fixture.uids=[65]
        result=self.fixture.invoke(backend=backend)
        self.assertEqual(result['mail_failures'],1);self.assertEqual(result['messages_preserved'],0)
        self.assertEqual(self.fixture.query('SELECT highest_uid FROM runner_folders'),[(64,)])
        with self.store.ledger(self.db,readonly=True) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM originals').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM occurrences').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM mail_messages').fetchone()[0],1)
            self.assertEqual(c.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0],0)
            self.assertEqual(c.execute("SELECT count(*) FROM runs WHERE status='completed_with_gaps'").fetchone()[0],2)
    def test_repeated_runs_finalize_separately_without_duplicate_mail(self):
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        import json
        backend=CanonicalMailBackend(self.fixture.delta.root,self.fixture.delta.out,self.db)
        first=self.fixture.invoke(backend=backend);second=self.fixture.invoke(backend=backend)
        self.assertNotEqual(first['run_id'],second['run_id']);self.assertEqual(second['messages_preserved'],0)
        with self.store.ledger(self.db,readonly=True) as c:
            rows=c.execute('SELECT * FROM runs ORDER BY started_at').fetchall()
            self.assertEqual(len(rows),2)
            self.assertTrue(all(row['ended_at'] and row['status']=='completed_with_gaps' for row in rows))
            self.assertEqual(c.execute('SELECT count(*) FROM mail_messages').fetchone()[0],1)
            for row in rows:
                body=json.loads(row['summary'])
                self.assertEqual(body['runner_binding']['run_id'],row['run_id'])
                self.assertFalse(body['runner_summary']['pipeline_complete'])
    def test_crash_recovery_finalizes_abandoned_canonical_run(self):
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        class CrashOnce(CanonicalMailBackend):
            crashed=False
            def preserve(inner,*args):
                value=super().preserve(*args)
                if not inner.crashed:inner.crashed=True;raise SystemExit('synthetic process interruption')
                return value
        backend=CrashOnce(self.fixture.delta.root,self.fixture.delta.out,self.db)
        with self.assertRaises(SystemExit):self.fixture.invoke(backend=backend)
        result=self.fixture.invoke(backend=backend)
        self.assertEqual(result['messages_preserved'],1)
        with self.store.ledger(self.db,readonly=True) as c:
            states=[row[0] for row in c.execute('SELECT status FROM runs')]
            self.assertCountEqual(states,['interrupted','completed_with_gaps'])
            self.assertEqual(c.execute('SELECT count(*) FROM mail_messages').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM occurrences').fetchone()[0],2)
    def test_finalization_is_idempotent_and_rejects_changed_binding(self):
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        import copy,json
        backend=CanonicalMailBackend(self.fixture.delta.root,self.fixture.delta.out,self.db)
        result=self.fixture.invoke(backend=backend)
        with self.store.ledger(self.db,readonly=True) as c:
            row=c.execute('SELECT * FROM runs WHERE run_id=?',(result['run_id'],)).fetchone()
            summary=json.loads(row['summary'])['runner_summary'];ended=row['ended_at']
        self.assertEqual(backend.finish_run(backend.run_identity,'completed_with_gaps',summary),'already_finalized')
        changed=copy.deepcopy(backend.run_identity);changed['config_sha256']='f'*64
        with self.assertRaises(ValueError):backend.finish_run(changed,'completed_with_gaps',summary)
        with self.store.ledger(self.db,readonly=True) as c:
            self.assertEqual(c.execute('SELECT ended_at FROM runs WHERE run_id=?',(result['run_id'],)).fetchone()[0],ended)
    def test_mail_identity_mismatch_keeps_projection_and_checkpoint_empty(self):
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        backend=CanonicalMailBackend(self.fixture.delta.root,self.fixture.delta.out,self.db)
        self.fixture.delta.receipt['account_id']='other-synthetic-namespace'
        result=self.fixture.invoke(backend=backend)
        self.assertEqual(result['messages_preserved'],0);self.assertEqual(result['mail_failures'],1)
        self.assertEqual(self.fixture.query('SELECT highest_uid FROM runner_folders'),[(0,)])
        with self.store.ledger(self.db,readonly=True) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM mail_messages').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM originals').fetchone()[0],0)
            self.assertEqual(c.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0],0)
    def test_mid_enrollment_failure_rolls_back_message_occurrence_and_projection(self):
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        from campaign_tool.records.runner.adapters import LegacyIntakeBackend
        import json
        scope=self.fixture.scopes[0];receipt=self.fixture.receipt(scope,64)
        legacy=LegacyIntakeBackend(self.fixture.delta.root,self.fixture.delta.out)
        value=legacy.preserve(receipt,'synthetic-account',scope,64)
        item=self.fixture.delta.receipt['attachments'][0];sha=item['sha256']
        with self.store.ledger(self.db) as c:
            c.execute('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)',(sha,item['bytes']+1,'application/octet-stream','2026-01-01T00:00:00+00:00','original','in_scope',str(self.fixture.delta.out/'blobs'/sha),'captured',None,'{}'));c.commit()
        backend=CanonicalMailBackend(self.fixture.delta.root,self.fixture.delta.out,self.db)
        result=self.fixture.invoke(backend=backend)
        self.assertEqual(result['mail_failures'],1);self.assertEqual(result['messages_preserved'],0)
        self.assertEqual(self.fixture.query('SELECT highest_uid FROM runner_folders'),[(0,)])
        with self.store.ledger(self.db,readonly=True) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM originals').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM originals WHERE sha256=?',(value.eml_sha256,)).fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM occurrences').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM mail_messages').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM stage_state').fetchone()[0],0)
    def test_finalization_rejects_changed_canonical_version_binding(self):
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        import json
        backend=CanonicalMailBackend(self.fixture.delta.root,self.fixture.delta.out,self.db)
        result=self.fixture.invoke(backend=backend)
        with self.store.ledger(self.db) as c:
            row=c.execute('SELECT summary FROM runs WHERE run_id=?',(result['run_id'],)).fetchone()
            summary=json.loads(row[0])['runner_summary']
            c.execute("UPDATE runs SET engine_version='changed synthetic version' WHERE run_id=?",(result['run_id'],));c.commit()
        with self.assertRaises(ValueError):backend.finish_run(backend.run_identity,'completed_with_gaps',summary)
    def test_direct_replay_idempotent_and_cas_corruption_rejected(self):
        self.fixture.invoke(backend=self.backend)
        scope=self.fixture.scopes[0];receipt=self.fixture.receipt(scope,64)
        self.backend.preserve(receipt,'synthetic-account',scope,64)
        with self.store.ledger(self.db,readonly=True) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM occurrences').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM originals').fetchone()[0],2)
        subject=self.fixture.delta.receipt['original_eml']['sha256']
        blob=self.fixture.delta.out/'blobs'/subject;blob.chmod(0o600);blob.write_bytes(b'corrupt synthetic object')
        with self.assertRaises(ValueError):self.backend.preserve(receipt,'synthetic-account',scope,64)

if __name__=='__main__':unittest.main()
