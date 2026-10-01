"""Synthetic recovery/outbox acceptance; no network, host services or publication."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from campaign_tool.records.recovery import backup as b,publication as p


class RecoveryFixtures(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ('socket','getaddrinfo'):
            guard=patch.object(socket,name,side_effect=AssertionError('network forbidden'))
            guard.start();self.addCleanup(guard.stop)
        self.source=self.root/'source';self.source.mkdir(mode=0o700)
        self.entries=[]
        for category,name,raw in [('originals','original.txt',b'synthetic agency record'),('rules','rule.json',b'{"synthetic":true}'),('receipts','review.json',b'{"review":"synthetic fixture, not substantive review"}')]:
            file=self.source/name;file.write_bytes(raw)
            self.entries.append(dict(category=category,name=name,source=file))
        self.database=self.source/'ledger.sqlite'
        with sqlite3.connect(self.database) as c:
            c.execute('CREATE TABLE originals(sha256 TEXT, status TEXT)')
            c.execute('INSERT INTO originals VALUES(?,?)',(hashlib.sha256(b'synthetic agency record').hexdigest(),'pending'))
        self.entries.append(dict(category='ledger',name='ledger.sqlite',source=self.database))
    def backup(self):
        result=b.backup(self.root/'backup',self.entries)
        return result
    def restore(self,result):
        return b.restore(self.root/'backup',self.root/'restored',manifest_sha256=result['manifest_sha256'])


class BackupTests(RecoveryFixtures):
    def test_exact_selected_backup_restore(self):
        result=self.backup();receipt=self.restore(result)
        self.assertEqual(result['files'],4)
        self.assertEqual(receipt['hash_reconciliation'],'exact')
        self.assertEqual((self.root/'restored/originals/original.txt').read_bytes(),b'synthetic agency record')
        with sqlite3.connect(self.root/'restored/ledger/ledger.sqlite') as c:
            self.assertEqual(c.execute('SELECT status FROM originals').fetchone()[0],'pending')
        self.assertEqual(receipt['sends'],0);self.assertEqual(receipt['deployments'],0)
        self.assertEqual(receipt['scope'],'explicit_selected_files')

    def test_wal_transaction_included(self):
        live=sqlite3.connect(self.database)
        try:
            live.execute('PRAGMA journal_mode=WAL')
            live.execute('INSERT INTO originals VALUES(?,?)',('synthetic-wal-row','pending'));live.commit()
            result=self.backup();self.restore(result)
            with sqlite3.connect(self.root/'restored/ledger/ledger.sqlite') as c:
                self.assertEqual(c.execute('SELECT count(*) FROM originals').fetchone()[0],2)
        finally:live.close()

    def test_missing_file_no_complete_restore_receipt(self):
        result=self.backup();(self.root/'backup/rules/rule.json').unlink()
        with self.assertRaises(OSError):self.restore(result)
        self.assertFalse((self.root/'restored/RESTORE-RECEIPT.json').exists())

    def test_tampered_file_fails_exact_hash(self):
        result=self.backup();path=self.root/'backup/originals/original.txt';path.chmod(0o600);path.write_bytes(b'tampered')
        with self.assertRaisesRegex(b.RecoveryError,'hash_or_size'):self.restore(result)
        self.assertFalse((self.root/'restored/RESTORE-RECEIPT.json').exists())

    def test_tampered_manifest_fails_trusted_anchor(self):
        result=self.backup();path=self.root/'backup/manifest.json';path.chmod(0o600);path.write_bytes(b'{}')
        with self.assertRaisesRegex(b.RecoveryError,'manifest_hash'):self.restore(result)
        self.assertFalse((self.root/'restored').exists())

    def test_source_symlink_rejected(self):
        link=self.source/'link';link.symlink_to(self.source/'original.txt')
        self.entries[0]['source']=link
        with self.assertRaises(OSError):self.backup()
        self.assertFalse((self.root/'backup/manifest.json').exists())

    def test_ancestor_symlink_rejected(self):
        link=self.root/'linked';link.symlink_to(self.source,target_is_directory=True)
        self.entries[0]['source']=link/'original.txt'
        with self.assertRaises(OSError):self.backup()

    def test_restore_member_symlink_rejected(self):
        result=self.backup();path=self.root/'backup/receipts/review.json';path.unlink();path.symlink_to(self.source/'review.json')
        with self.assertRaises(OSError):self.restore(result)

    def test_existing_destination_never_overwritten(self):
        result=self.backup();self.restore(result)
        with self.assertRaises(FileExistsError):self.restore(result)
        self.assertEqual((self.root/'restored/originals/original.txt').read_bytes(),b'synthetic agency record')

    def test_isolation_and_traversal(self):
        result=self.backup()
        with self.assertRaisesRegex(b.RecoveryError,'isolated'):
            b.restore(self.root/'backup',self.root/'backup/nested',manifest_sha256=result['manifest_sha256'])
        self.entries[0]['name']='../escape'
        with self.assertRaisesRegex(b.RecoveryError,'entry_identity'):b.backup(self.root/'other',self.entries)

    def test_snapshot_failure_leaves_no_complete_manifest(self):
        with patch.object(b.sqlite3,'connect',side_effect=sqlite3.OperationalError('synthetic interrupted backup')):
            with self.assertRaises(sqlite3.Error):self.backup()
        self.assertFalse((self.root/'backup/manifest.json').exists())

    def test_source_change_during_copy_fails(self):
        actual=b.os.read;changed=False
        source=self.source/'original.txt'
        def mutating(fd,size):
            nonlocal changed
            raw=actual(fd,size)
            if raw and not changed:
                changed=True;source.write_bytes(b'changed synthetic record')
            return raw
        with patch.object(b.os,'read',side_effect=mutating):
            with self.assertRaisesRegex(b.RecoveryError,'source_changed'):
                b.copy_exact(source,self.root/'copied.txt')

    def test_bounds_are_explicit(self):
        with patch.object(b,'MAX_FILE',3):
            with self.assertRaisesRegex(b.RecoveryError,'bound'):self.backup()
        self.assertFalse((self.root/'backup/manifest.json').exists())


class ExplicitSyntheticReviewStub:
    """Unit-test seam only. Actual review behavior is in the WP8 composition suite."""
    test_only=True
    @contextmanager
    def assess(self,job,*,owner,test_only):
        if test_only is not True:raise p.ReviewGateError('synthetic_stub_not_production')
        yield {'test_only':True,'fixture_only':True}


class TransientLockReviewStub(ExplicitSyntheticReviewStub):
    """Simulates a WP8 review lock held by another process."""
    locked=True
    @contextmanager
    def assess(self,job,*,owner,test_only):
        if self.locked:raise p.ReviewGateError('wp8_review_lock_unavailable')
        with super().assess(job,owner=owner,test_only=test_only) as result:yield result


class PublicationTests(RecoveryFixtures):
    def setUp(self):
        super().setUp()
        self.authority=p.SyntheticApprovalAuthority()
        self.withdrawal=p.SyntheticRollbackAuthority()
        self.runtime=p.testing_runtime(self.authority,review_verifier=ExplicitSyntheticReviewStub(),rollback_authority=self.withdrawal)
        self.box=p.PublicationOutbox(self.root/'outbox');self.addCleanup(self.box.close)
        self.content=b'# Synthetic factual record update\nNo real agency claim.\n'
        self.review=b'{"test_only":true,"independent_check":"synthetic","privacy":"synthetic"}'
    def stage(self,name='sample',content=None,supersedes=None):
        content=self.content if content is None else content
        approval=self.authority.approve(name,content,self.review,supersedes=supersedes)
        return self.box.stage(self.runtime,proposal_id=name,content=content,review_bundle=self.review,approval=approval,supersedes=supersedes)
    def deploy(self,name='sample'):
        self.box.execute(self.runtime,name,'prepare')
        return self.box.execute(self.runtime,name,'deploy')

    def rollback(self,name='sample'):
        target=self.box.rollback_target(self.runtime,name)
        return self.box.execute(self.runtime,name,'rollback',rollback_authorization=self.withdrawal.approve(target))

    def test_exact_approved_content_prepare_deploy_rollback(self):
        self.stage();deployed=self.deploy();rollback=self.rollback()
        self.assertEqual(rollback['rollback_ref'],deployed['deployed_version'])
        self.assertIsNone(self.runtime.adapter.active_version)
        self.assertTrue(rollback['test_only']);self.assertFalse(rollback['production'])
        self.assertEqual((self.root/'outbox/site/sample.md').read_bytes(),self.content)
        self.assertEqual(sorted(path.name for path in (self.root/'outbox/site').iterdir()),['sample.md'])

    def test_transiently_blocked_stage_can_prepare_after_lock_release(self):
        gate=TransientLockReviewStub()
        self.runtime=p.testing_runtime(self.authority,review_verifier=gate,rollback_authority=self.withdrawal)
        staged=self.stage()
        self.assertEqual((staged['state'],staged['blocked_reason']),('blocked','wp8_review_lock_unavailable'))
        self.assertEqual(self.stage()['state'],'blocked')  # replay returns the stored row
        held=self.box.execute(self.runtime,'sample','prepare')
        self.assertEqual((held['state'],held['reason']),('blocked','wp8_review_lock_unavailable'))
        gate.locked=False
        prepared=self.box.execute(self.runtime,'sample','prepare')
        self.assertEqual(prepared['action'],'prepare')
        self.assertEqual(self.box.status()[0]['state'],'prepared')
        self.assertEqual(self.box.execute(self.runtime,'sample','deploy')['action'],'deploy')

    def test_changed_content_or_bundle_not_authorized(self):
        approval=self.authority.approve('sample',self.content,self.review)
        for name,content,review in [('sample',self.content+b' changed',self.review),('other',self.content,self.review+b' changed')]:
            result=self.box.stage(self.runtime,proposal_id=name,content=content,review_bundle=review,approval=approval)
            self.assertEqual(result['blocked_reason'],'owner_approval_invalid')
        self.assertEqual(self.runtime.adapter.calls,[])

    def test_approval_cannot_supply_verifier_callback(self):
        approval=self.authority.approve('sample',self.content,self.review);approval['verifier']='always_true'
        result=self.box.stage(self.runtime,proposal_id='sample',content=self.content,review_bundle=self.review,approval=approval)
        self.assertEqual(result['state'],'blocked')

    def test_wrong_owner_or_unsigned_approval_blocked(self):
        other=p.SyntheticApprovalAuthority(owner='other-owner')
        approval=other.approve('sample',self.content,self.review)
        result=self.box.stage(self.runtime,proposal_id='sample',content=self.content,review_bundle=self.review,approval=approval)
        self.assertEqual(result['blocked_reason'],'owner_approval_invalid')

    def test_default_missing_review_gate_blocks_synthetic_too(self):
        runtime=p.testing_runtime(self.authority)
        approval=self.authority.approve('sample',self.content,self.review)
        result=self.box.stage(runtime,proposal_id='sample',content=self.content,review_bundle=self.review,approval=approval)
        self.assertEqual(result['blocked_reason'],'review_gate_unconfigured')
        self.assertEqual(runtime.adapter.calls,[])

    def test_missing_installed_authority_durably_blocked(self):
        runtime=p.installed_runtime(owner='owner',profile='production',approval_verifier_id='missing',site_adapter_id='missing')
        result=self.box.stage(runtime,proposal_id='sample',content=self.content,review_bundle=self.review,approval={})
        self.assertEqual(result['blocked_reason'],'approval_verifier_unconfigured')
        self.assertEqual(self.box.status()[0]['state'],'blocked')
        self.assertEqual(self.box.db.execute('SELECT count(*) FROM publication_events').fetchone()[0],1)
        self.assertFalse((self.root/'outbox/site/sample.md').exists())

    def test_fake_cannot_be_installed_as_production(self):
        with patch.dict(p._INSTALLED_APPROVAL_VERIFIERS,{'bad':self.authority}),patch.dict(p._INSTALLED_SITE_ADAPTERS,{'bad':self.runtime.adapter}):
            runtime=p.installed_runtime(owner=self.authority.owner,profile='production',approval_verifier_id='bad',site_adapter_id='bad')
            result=self.box.stage(runtime,proposal_id='sample',content=self.content,review_bundle=self.review,approval={})
        self.assertEqual(result['blocked_reason'],'synthetic_adapter_not_production')

    def test_same_action_replay_has_no_duplicate_effect(self):
        self.stage();first=self.deploy()
        again=self.box.execute(self.runtime,'sample','deploy')
        self.assertEqual(first,again);self.assertEqual(len(self.runtime.adapter.calls),2)

    def test_crash_after_adapter_before_ack_reuses_action(self):
        self.stage()
        def crash(where):raise RuntimeError('synthetic process interruption')
        with self.assertRaises(RuntimeError):self.box.execute(self.runtime,'sample','prepare',fault=crash)
        other=p.PublicationOutbox(self.root/'outbox')
        try:
            result=other.execute(self.runtime,'sample','prepare')
            self.assertEqual(other.status()[0]['state'],'prepared')
        finally:other.close()
        self.assertTrue(result['test_only']);self.assertEqual(len(self.runtime.adapter.calls),1)

    def test_supersession_preserves_prior_bytes_and_rollback_target(self):
        self.stage();old=self.deploy()
        revised=self.content+b'Corrected synthetic wording.\n'
        self.stage('correction',revised,supersedes='sample');new=self.deploy('correction')
        self.assertEqual(new['previous_version'],old['deployed_version'])
        blocked=self.rollback()
        self.assertEqual(blocked['reason'],'rollback_would_replace_newer_deployment')
        result=self.rollback('correction')
        self.assertEqual(result['restored_version'],old['deployed_version'])
        self.assertEqual((self.root/'outbox/site/sample.md').read_bytes(),self.content)
        self.assertEqual(len(self.box.status()),2)

    def test_mutating_proposal_requires_new_supersession(self):
        self.stage()
        with self.assertRaisesRegex(p.PublicationError,'immutable'):
            self.stage(content=self.content+b'changed')

    def test_mutated_staged_file_cannot_deploy(self):
        self.stage();self.box.execute(self.runtime,'sample','prepare')
        path=self.root/'outbox/site/sample.md';path.chmod(0o600);path.write_bytes(b'changed')
        with self.assertRaisesRegex(p.PublicationError,'staged_public_content'):
            self.box.execute(self.runtime,'sample','deploy')
        self.assertEqual(len(self.runtime.adapter.calls),1)

    def test_staged_symlink_fails_closed(self):
        self.stage();(self.root/'outbox/site/sample.md').symlink_to(self.source/'original.txt')
        with self.assertRaises(OSError):self.box.execute(self.runtime,'sample','prepare')

    def test_durable_history_is_not_replaced(self):
        self.stage();self.deploy()
        with self.assertRaises(sqlite3.IntegrityError):
            self.box.db.execute('DELETE FROM publication_events')
        self.box.db.rollback()

    def test_history_replace_rejected_with_recursive_triggers_disabled(self):
        self.stage();self.deploy()
        self.box.db.execute('PRAGMA recursive_triggers=OFF')
        before=[tuple(row) for row in self.box.db.execute('SELECT * FROM publication_events ORDER BY sequence')]
        for statement in ('INSERT OR REPLACE INTO publication_events SELECT * FROM publication_events',
                          'REPLACE INTO publication_events SELECT * FROM publication_events'):
            with self.subTest(statement=statement),self.assertRaises(sqlite3.IntegrityError):
                self.box.db.execute(statement)
            self.box.db.rollback()
            self.assertEqual([tuple(row) for row in self.box.db.execute('SELECT * FROM publication_events ORDER BY sequence')],before)
        # Protection still permits append-only normal rollback events.
        self.rollback()
        self.assertGreater(self.box.db.execute('SELECT count(*) FROM publication_events').fetchone()[0],len(before))

    def test_original_review_publication_recovery_roundtrip(self):
        # The review bundle references exact synthetic evidence. No real review is claimed.
        self.review=b.canonical({'test_only':True,'original_sha256':hashlib.sha256(b'synthetic agency record').hexdigest(),
                                 'public_content_sha256':hashlib.sha256(self.content).hexdigest(),
                                 'factual_review':'synthetic fixture','privacy_review':'synthetic fixture'})
        self.stage();self.deploy();self.rollback()
        receipt=self.source/'review.json';receipt.write_bytes(self.review)
        self.entries[-1]['source']=self.box.database
        result=self.backup();restored=self.restore(result)
        with sqlite3.connect(self.root/'restored/ledger/ledger.sqlite') as c:
            self.assertEqual(c.execute('SELECT state,test_only FROM publication_jobs').fetchone(),('rolled_back',1))
        self.assertEqual(restored['deployments'],0)
        self.assertEqual((self.root/'restored/receipts/review.json').read_bytes(),self.review)

    def test_partial_prepare_write_is_not_exposed_and_retry_recovers(self):
        self.stage();original=p.os.write;calls=0
        def interrupted(fd,raw):
            nonlocal calls
            calls+=1
            if calls==1:return original(fd,raw[:7])
            raise OSError('synthetic interrupted artifact write')
        with patch.object(p.os,'write',side_effect=interrupted):
            with self.assertRaisesRegex(OSError,'interrupted'):
                self.box.execute(self.runtime,'sample','prepare')
        self.assertFalse((self.box.site/'sample.md').exists())
        self.assertEqual(self.runtime.adapter.calls,[])
        reopened=p.PublicationOutbox(self.root/'outbox')
        try:
            result=reopened.execute(self.runtime,'sample','prepare')
            self.assertEqual(reopened.status()[0]['state'],'prepared')
        finally:reopened.close()
        self.assertEqual((self.box.site/'sample.md').read_bytes(),self.content)
        self.assertFalse(result['production']);self.assertEqual(len(self.runtime.adapter.calls),1)

    def test_completed_prepare_replay_rejects_mutated_artifact(self):
        self.stage();self.box.execute(self.runtime,'sample','prepare')
        path=self.box.site/'sample.md';path.chmod(0o600);path.write_bytes(b'mutated')
        with self.assertRaisesRegex(p.PublicationError,'staged_public_content_changed'):
            self.box.execute(self.runtime,'sample','prepare')
        self.assertEqual(len(self.runtime.adapter.calls),1)

    def test_completed_prepare_replay_rejects_missing_artifact(self):
        self.stage();self.box.execute(self.runtime,'sample','prepare')
        (self.box.site/'sample.md').unlink()
        with self.assertRaises(FileNotFoundError):self.box.execute(self.runtime,'sample','prepare')
        self.assertFalse((self.box.site/'sample.md').exists())
        self.assertEqual(len(self.runtime.adapter.calls),1)

    def test_completed_replay_revalidates_receipt_binding(self):
        self.stage();self.box.execute(self.runtime,'sample','prepare')
        before=json.loads(self.box.db.execute('SELECT receipt_json FROM publication_actions').fetchone()[0])
        for field in ('action','idempotency_key','content_sha256','review_bundle_sha256','reference','test_only','production'):
            broken=dict(before);broken[field]=None
            with self.box.db:self.box.db.execute('UPDATE publication_actions SET receipt_json=?',(json.dumps(broken),))
            with self.subTest(field=field),self.assertRaisesRegex(p.PublicationError,'adapter_receipt_binding'):
                self.box.execute(self.runtime,'sample','prepare')
        self.assertEqual(len(self.runtime.adapter.calls),1)

    def test_atomic_exposure_does_not_replace_existing_artifact(self):
        destination=self.box.site/'committed.md';destination.write_bytes(b'existing')
        with self.assertRaises(FileExistsError):p.atomic_public_artifact(destination,self.content,self.box.root)
        self.assertEqual(destination.read_bytes(),b'existing')

    def test_rollback_defaults_to_missing_separate_authority(self):
        self.stage();self.deploy()
        runtime=p.testing_runtime(self.authority,adapter=self.runtime.adapter,
                                  review_verifier=ExplicitSyntheticReviewStub())
        result=self.box.execute(runtime,'sample','rollback')
        self.assertEqual(result['reason'],'rollback_authority_unconfigured')
        self.assertEqual(self.box.status()[0]['state'],'deployed')

    def test_rollback_requires_fresh_explicit_authorization_argument(self):
        self.stage();self.deploy()
        result=self.box.execute(self.runtime,'sample','rollback')
        self.assertEqual(result['reason'],'explicit_rollback_authorization_required')
        self.assertEqual(self.box.status()[0]['state'],'deployed')
        self.assertEqual(len(self.runtime.adapter.calls),2)

    def test_wrong_version_rollback_authorization_blocks(self):
        self.stage();self.deploy()
        target=self.box.rollback_target(self.runtime,'sample');target['deployed_version']='synthetic:wrong'
        result=self.box.execute(self.runtime,'sample','rollback',rollback_authorization=self.withdrawal.approve(target))
        self.assertEqual(result['reason'],'rollback_authorization_invalid')
        self.assertEqual(self.box.status()[0]['state'],'deployed')
        self.assertEqual(len(self.runtime.adapter.calls),2)

    def test_rollback_can_withdraw_when_prepared_file_is_missing(self):
        self.stage();self.deploy();(self.box.site/'sample.md').unlink()
        receipt=self.rollback()
        self.assertIsNone(receipt['restored_version'])
        self.assertEqual(self.box.status()[0]['state'],'rolled_back')

    def test_legacy_blocked_projection_recovers_deployed_phase(self):
        self.stage();self.deploy()
        with self.box.db:self.box.db.execute("UPDATE publication_jobs SET state='blocked',blocked_reason='old hold'")
        result=self.box.execute(self.runtime,'sample','rollback')
        self.assertEqual(result['reason'],'explicit_rollback_authorization_required')
        self.assertEqual(self.box.status()[0]['state'],'deployed')
        self.assertFalse(self.rollback()['production'])

    def test_rollback_target_requires_intact_historical_deployment_receipt(self):
        self.stage();self.deploy()
        row=self.box.db.execute("SELECT receipt_json FROM publication_actions WHERE action='deploy'").fetchone()
        receipt=json.loads(row[0]);receipt['deployed_version']='synthetic:changed'
        with self.box.db:self.box.db.execute("UPDATE publication_actions SET receipt_json=? WHERE action='deploy'",(json.dumps(receipt),))
        with self.assertRaisesRegex(p.PublicationError,'deployment_receipt_history_mismatch'):
            self.box.rollback_target(self.runtime,'sample')
