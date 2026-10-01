"""Synthetic fairness and durable lifecycle regressions; no provider calls."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from campaign_tool.records.runner import core,migration
from campaign_tool.records.runner.contracts import Folder,StageReceipt
from tests.records import test_runner as fixtures


class FairnessTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RunnerTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def stage_case(self,budget):
        f=self.f
        f.profile.update(stages=['extract','catalog','detect'],max_work=budget,retry_limit=2,retry_delay=1)
        f.write_profile()
        low=min(f.delta.receipt['original_eml']['sha256'],f.delta.receipt['attachments'][0]['sha256'])
        def hook(stage):
            def call(subject,identity):
                if subject==low and stage=='extract':raise ValueError('synthetic blocked item')
                return StageReceipt(subject,stage,json.dumps({'subject_sha256':subject,'stage':stage}).encode())
            return call
        runs=[]
        for _ in range(8):
            result=f.invoke(hooks={s:hook(s) for s in f.profile['stages']});runs.append(result);f.time+=2
            self.assertLessEqual(result['stage_promotions']+result['stage_failures'],budget)
        rows=f.query('SELECT subject_sha,stage,state,attempts FROM runner_work')
        self.assertTrue(all(row[2]=='done' and row[3]==1 for row in rows if row[0]!=low))
        self.assertEqual([row[3] for row in rows if row[0]==low and row[1]=='extract'],[2])
        self.assertTrue(all(row[2]=='pending' and row[3]==0 for row in rows if row[0]==low and row[1]!='extract'))
        self.assertEqual(sum(run['stage_promotions'] for run in runs),3)
        self.assertFalse(any(run['release_ready'] for run in runs))

    def test_failed_ancestor_max_work_one(self):self.stage_case(1)
    def test_failed_ancestor_max_work_two(self):self.stage_case(2)

    def folder_case(self,budget,busy=False):
        f=self.f;names=['First'] if budget==1 else ['First','Second'];names.append('Healthy')
        f.scopes=[Folder(name,9) for name in names]
        f.profile.update(folders=names,max_messages=budget);f.write_profile()
        attempts=[]
        class Provider(fixtures.Transcript):
            def uids(self,scope,after):
                # Earlier folders can remain continuously nonempty.
                return list(range(after+1,after+budget+2)) if busy and scope.name!='Healthy' else ([64] if after<64 else [])
            def receipt(self,scope,uid):
                attempts.append((scope.name,uid))
                if not busy and scope.name!='Healthy':raise ConnectionError('synthetic failed UID')
                return f.receipt(scope,uid)
        provider=Provider(f.scopes,[64],f.receipt)
        for _ in range(4):
            before=len(attempts);f.invoke(provider=provider);f.time+=2
            self.assertLessEqual(len(attempts)-before,budget)
        self.assertIn(('Healthy',64),attempts)
        checkpoints=dict(f.query('SELECT folder,highest_uid FROM runner_folders'))
        self.assertEqual(checkpoints['Healthy'],64)
        if not busy:self.assertTrue(all(checkpoints[name]==0 for name in names if name!='Healthy'))
        # A new exporter instance/control run retains the persisted rotation.
        self.assertEqual(f.query("SELECT count(*) FROM runner_messages WHERE folder='Healthy'"),[(1,)])

    def test_failed_folders_max_messages_one(self):self.folder_case(1)
    def test_failed_folders_max_messages_two(self):self.folder_case(2)
    def test_busy_folders_max_messages_one(self):self.folder_case(1,True)
    def test_busy_folders_max_messages_two(self):self.folder_case(2,True)

    def reordered_folder_case(self,budget):
        f=self.f;names=['First','Second','Third','Healthy']
        scopes={name:Folder(name,9) for name in names}
        f.profile.update(folders=names,max_messages=budget);f.write_profile()
        attempts=[];inventories=[]
        class ReorderingProvider(fixtures.Transcript):
            def folders(self):
                cursor=f.query('SELECT folder FROM runner_mail_cursor')
                # Place Healthy immediately before the cursor. In provider order,
                # rotation can repeatedly spend the whole budget on failed peers.
                order=list(names)
                if cursor and cursor[0][0]!='Healthy':
                    last=cursor[0][0]
                    order=['Healthy',last]+[n for n in names if n not in ('Healthy',last)]
                inventories.append(order)
                return [scopes[n] for n in order]
            def uids(self,scope,after):return [64] if after<64 else []
            def receipt(self,scope,uid):
                attempts.append((scope.name,uid))
                if scope.name!='Healthy':raise ConnectionError('synthetic failed UID')
                return f.receipt(scope,uid)
        for _ in range(8):
            before=len(attempts)
            provider=ReorderingProvider(list(scopes.values()),[64],f.receipt)
            result=f.invoke(provider=provider);f.time+=2
            self.assertLessEqual(len(attempts)-before,budget)
            self.assertEqual(result['folder_alerts'],0)
            self.assertFalse(result['release_ready'])
        self.assertGreater(len({tuple(order) for order in inventories}),1)
        self.assertEqual([name for name,uid in attempts[:4]],names)
        self.assertEqual(attempts.count(('Healthy',64)),1)
        checkpoints={name:(epoch,uid) for name,epoch,uid in f.query(
            'SELECT folder,uidvalidity,highest_uid FROM runner_folders')}
        self.assertEqual(checkpoints['Healthy'],(9,64))
        self.assertTrue(all(checkpoints[name]==(9,0) for name in names if name!='Healthy'))
        self.assertEqual(f.query("SELECT count(*) FROM runner_messages WHERE folder='Healthy'"),[(1,)])

    def test_provider_reordering_max_messages_one(self):self.reordered_folder_case(1)
    def test_provider_reordering_max_messages_two(self):self.reordered_folder_case(2)

    def test_additive_migration_keeps_v1_data_and_rejects_extension_tamper(self):
        f=self.f;path=f.root/'runner.sqlite'
        con=sqlite3.connect(path);path.chmod(0o600)
        con.executescript(migration.SQL)
        con.execute('INSERT INTO runner_schema VALUES(?,?)',(migration.VERSION,hashlib.sha256(migration.SQL.encode()).hexdigest()))
        con.execute('INSERT INTO runner_runs VALUES(?,?,?,?,?,?)',('historical',1,2,'failed','{}','{}'))
        con.commit();con.close()
        con=core.journal(f.root)
        self.assertEqual(con.execute('SELECT status FROM runner_runs WHERE id=?',('historical',)).fetchone()[0],'failed')
        con.execute("UPDATE runner_reliability_schema SET checksum='tampered'");con.commit();con.close()
        with self.assertRaisesRegex(ValueError,'reliability_schema_mismatch'):core.journal(f.root)


def _wp1_available():
    """WP1 is in-tree after the records merge; an explicit overlay remains supported."""
    if os.environ.get('WP1_OVERLAY'):
        return True
    import importlib.util
    try:
        return importlib.util.find_spec('campaign_tool.records.ledger.store') is not None
    except ModuleNotFoundError:
        return False

@unittest.skipUnless(_wp1_available(),'WP1 not in tree and no overlay supplied')
class LifecycleTests(unittest.TestCase):
    def setUp(self):
        import campaign_tool.records
        if os.environ.get('WP1_OVERLAY'):
            overlay=str(Path(os.environ['WP1_OVERLAY'])/'campaign_tool/records')
            if overlay not in campaign_tool.records.__path__:campaign_tool.records.__path__.append(overlay)
        from campaign_tool.records.ledger import store
        from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
        self.store=store;self.backend_type=CanonicalMailBackend
        self.f=fixtures.RunnerTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.delta.base/'canonical'/'ledger.sqlite';store.initialize(self.db)

    def canonical(self,rid):
        with self.store.ledger(self.db,readonly=True) as con:
            row=con.execute('SELECT status,ended_at,summary FROM runs WHERE run_id=?',(rid,)).fetchone()
            return tuple(row) if row else None

    def test_recovery_failure_retries_after_control_interrupted(self):
        f=self.f
        class Backend(self.backend_type):
            crashed=False;calls=0
            def preserve(inner,*args):
                value=super().preserve(*args)
                if not inner.crashed:inner.crashed=True;raise SystemExit('synthetic crash')
                return value
            def recover_run(inner,*args):
                inner.calls+=1
                if inner.calls==1:raise RuntimeError('synthetic temporary I/O failure')
                return super().recover_run(*args)
        backend=Backend(f.delta.root,f.delta.out,self.db)
        with self.assertRaises(SystemExit):f.invoke(backend=backend)
        old=f.query("SELECT id FROM runner_runs WHERE status='running'")[0][0]
        second=f.invoke(backend=backend)
        self.assertEqual(self.canonical(old)[:2],('running',None))
        self.assertEqual(second['canonical_lifecycle_pending'],1)
        f.time+=2;third=f.invoke(backend=backend)
        self.assertEqual(backend.calls,2)
        self.assertEqual(self.canonical(old)[0],'interrupted')
        self.assertIsNotNone(self.canonical(old)[1])
        self.assertEqual(third['canonical_lifecycle_pending'],0)
        self.assertEqual(f.query('SELECT count(*) FROM runner_messages'),[(1,)])

    def test_finish_failure_retries_same_outcome_not_failed(self):
        f=self.f
        class Backend(self.backend_type):
            fail=True;calls=[]
            def finish_run(inner,identity,status,summary):
                inner.calls.append((identity['run_id'],status,json.dumps(summary,sort_keys=True)))
                if inner.fail:inner.fail=False;raise RuntimeError('synthetic finish unavailable')
                return super().finish_run(identity,status,summary)
        backend=Backend(f.delta.root,f.delta.out,self.db)
        first=f.invoke(backend=backend);old=first['run_id']
        self.assertEqual(first['canonical_run_finalization'],'pending')
        self.assertEqual(self.canonical(old)[:2],('running',None))
        f.time+=2;f.invoke(backend=backend)
        prior=[entry for entry in backend.calls if entry[0]==old]
        self.assertEqual(len(prior),2);self.assertEqual(prior[0],prior[1])
        self.assertNotEqual(self.canonical(old)[0],'running')
        self.assertEqual(f.query("SELECT state FROM runner_lifecycle WHERE run_id='"+old+"'"),[('done',)])

    def test_crash_after_canonical_finish_replays_exact_intent(self):
        f=self.f
        class Backend(self.backend_type):
            crash=True
            def finish_run(inner,*args):
                result=super().finish_run(*args)
                if inner.crash:inner.crash=False;raise SystemExit('synthetic after-canonical-commit crash')
                return result
        backend=Backend(f.delta.root,f.delta.out,self.db)
        with self.assertRaises(SystemExit):f.invoke(backend=backend)
        old=f.query("SELECT id FROM runner_runs WHERE status='running'")[0][0]
        self.assertNotEqual(self.canonical(old)[0],'running')
        before=self.canonical(old)
        f.invoke(backend=backend)
        self.assertEqual(self.canonical(old),before)
        self.assertEqual(f.query("SELECT state,acknowledgment FROM runner_lifecycle WHERE run_id='"+old+"'"),[('done','already_finalized')])
        self.assertEqual(f.query("SELECT status FROM runner_runs WHERE id='"+old+"'"),[('completed_with_gaps',)])

    def test_start_commit_then_exception_has_recoverable_intent(self):
        f=self.f
        class Backend(self.backend_type):
            fail=True
            def start_run(inner,*args):
                result=super().start_run(*args)
                if inner.fail:inner.fail=False;raise RuntimeError('synthetic startup response loss')
                return result
        backend=Backend(f.delta.root,f.delta.out,self.db)
        with self.assertRaises(ValueError):f.invoke(backend=backend)
        old=f.query('SELECT id FROM runner_runs')[0][0]
        self.assertEqual(self.canonical(old)[0],'interrupted')
        self.assertEqual(f.query("SELECT operation,state FROM runner_lifecycle WHERE run_id='"+old+"'"),[('recover','done')])
        self.assertEqual(f.query('SELECT count(*) FROM runner_messages'),[(0,)])

    def test_retry_backoff_persists_and_intent_payload_is_immutable(self):
        f=self.f
        class Backend(self.backend_type):
            calls=0
            def finish_run(inner,*args):inner.calls+=1;raise RuntimeError('synthetic prolonged outage')
        backend=Backend(f.delta.root,f.delta.out,self.db)
        first=f.invoke(backend=backend);old=first['run_id']
        f.invoke(backend=backend)
        row=f.query("SELECT attempts FROM runner_lifecycle WHERE run_id='"+old+"'")[0]
        self.assertEqual(row,(1,))
        with sqlite3.connect(f.root/'runner.sqlite') as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE runner_lifecycle SET outcome='failed' WHERE run_id=?",(old,))
        f.time+=2;f.invoke(backend=backend)
        self.assertEqual(f.query("SELECT attempts FROM runner_lifecycle WHERE run_id='"+old+"'"),[(2,)])

    def test_legacy_finalization_pending_event_gets_recovery(self):
        f=self.f;backend=self.backend_type(f.delta.root,f.delta.out,self.db)
        # Reproduce an older control history without erasing any original evidence.
        f.invoke(backend=backend)
        old=f.query('SELECT id FROM runner_runs')[0][0]
        identity=json.loads(f.query('SELECT identity FROM runner_runs')[0][0])
        legacy=dict(identity,run_id='legacy-pending-fixture')
        backend.start_run(legacy)
        con=core.journal(f.root)
        with con:
            con.execute('INSERT INTO runner_runs VALUES(?,?,?,?,?,?)',(legacy['run_id'],0,1,'failed',core.js(legacy),'{}'))
            core.event(con,legacy['run_id'],'canonical_finalization',legacy['run_id'],'canonical_finalization_pending')
        con.close()
        f.invoke(backend=backend)
        self.assertEqual(self.canonical(legacy['run_id'])[0],'interrupted')
