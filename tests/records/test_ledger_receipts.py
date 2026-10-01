"""Synthetic receipt evidence, never private campaign fixtures."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from campaign_tool.records.ledger import store, receipt_projection as rp

class ReceiptProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.root.chmod(0o700)
        self.db=self.root/'ledger.sqlite';store.initialize(self.db)
        self.original=hashlib.sha256(b'fictional original').hexdigest()
        with store.ledger(self.db) as c:
            c.execute('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)',(self.original,18,'text/plain',None,'original','in_scope',None,'captured',None,'{}'));c.commit()
        self.art=b'{"reviewer":"fictional checker","coverage":[1],"verdict":"declared pass"}'
        (self.root/'artifact.json').write_bytes(self.art)
        self.row={'original':self.original,'receipt':hashlib.sha256(self.art).hexdigest(),'path':'artifact.json',
                  'reviewed':hashlib.sha256(b'fictional proposal').hexdigest(),'reviewer':'fictional checker',
                  'coverage':[1],'holds':['unverified'],'supersedes':'fictional previous','challenges':['pending']}
        self.fields={'original_hash':'original','artifact_hash':'receipt','artifact_path':'path','reviewed_content_hash':'reviewed'}
        self.write([self.row])
    def tearDown(self):self.tmp.cleanup()
    def write(self,rows,raw=None):
        data=raw if raw is not None else b''.join(json.dumps(x).encode()+b'\n' for x in rows)
        (self.root/'index.jsonl').write_bytes(data)
        self.manifest={'version':1,'indexes':[{'path':'index.jsonl','sha256':hashlib.sha256(data).hexdigest(),
          'kind':'reviews','fields':self.fields,'declarations':{k:k for k in ('reviewer','coverage','holds','supersedes','challenges')}}]}
        (self.root/'manifest.json').write_text(json.dumps(self.manifest))
    def run_project(self,**kw):return rp.project(self.db,self.root,'manifest.json',**kw)
    def query(self,sql):
        with store.ledger(self.db,readonly=True) as c:return [tuple(r) for r in c.execute(sql)]
    def test_exact_bytes_roles_and_nonpromotion(self):
        before=store.counts(self.db);result=self.run_project()
        self.assertEqual(result['binding_verified'],1);self.assertFalse(result['review_accepted'])
        self.assertEqual(before,store.counts(self.db));self.assertEqual(self.query('SELECT body FROM rp_artifacts')[0][0],self.art)
        p=json.loads(self.query('SELECT plan FROM rp_items')[0][0]);self.assertNotEqual(p['original_hash'],p['reviewed_content_hash'])
        self.assertEqual(p['declarations']['holds'],['unverified']);self.assertEqual(p['declarations']['supersedes'],'fictional previous')
    def test_duplicate_replay_and_duplicate_declarations(self):
        self.write([self.row,self.row]);r=self.run_project();self.assertEqual(r['processed'],2)
        self.assertEqual(len(self.query('SELECT * FROM rp_artifacts')),1);self.assertEqual(self.run_project()['added'],0)
    def test_missing_artifact(self):
        (self.root/'artifact.json').unlink();r=self.run_project();self.assertEqual(r['binding_verified'],0)
        self.assertIn(('missing_file',),self.query('SELECT reason FROM rp_gaps'))
    def test_ambiguous_candidates(self):
        self.fields.pop('original_hash');self.fields['original_hash_candidates']='candidates'
        self.row['candidates']=[self.original,self.original];self.write([self.row])
        self.assertEqual(self.run_project()['binding_verified'],0)
        self.assertIn(('ambiguous_original_hash',),self.query('SELECT reason FROM rp_gaps'))
    def test_missing_hash_role_no_guess(self):
        self.fields.pop('artifact_hash');self.write([self.row]);self.assertEqual(self.run_project()['binding_verified'],0)
    def test_changed_artifact_preserved_but_not_verified(self):
        (self.root/'artifact.json').write_bytes(b'changed');self.assertEqual(self.run_project()['binding_verified'],0)
        self.assertEqual(self.query('SELECT body FROM rp_artifacts')[0][0],b'changed')
    def test_malformed_and_duplicate_keys_retained(self):
        raw=b'not json\n{"original":"x","original":"y"}\n';self.write([],raw)
        r=self.run_project();self.assertEqual(r['processed'],2);self.assertEqual(r['gaps'],2)
        self.assertEqual(b''.join(x[0] for x in self.query('SELECT raw FROM rp_items ORDER BY line')),raw)
    def test_root_escape(self):
        self.row['path']='../outside';self.write([self.row]);self.assertEqual(self.run_project()['binding_verified'],0)
        self.assertIn(('path_escape',),self.query('SELECT reason FROM rp_gaps'))
    def test_symlink(self):
        (self.root/'linked').symlink_to(self.root/'artifact.json');self.row['path']='linked';self.write([self.row])
        self.assertEqual(self.run_project()['binding_verified'],0)
    def test_oversize_artifact(self):
        with patch.object(rp,'MAX_ARTIFACT',8):r=self.run_project()
        self.assertEqual(r['binding_verified'],0);self.assertEqual(self.query('SELECT count(*) FROM rp_artifacts'),[(0,)])
    def test_checkpoint_resume(self):
        self.write([self.row]*3);self.assertEqual(self.run_project(batch_size=1)['remaining'],2)
        self.assertEqual(self.run_project(batch_size=1)['processed'],2);self.assertEqual(self.run_project(batch_size=1)['remaining'],0)
    def test_atomic_failure_recovery(self):
        self.run_project(batch_size=1)
        self.write([self.row]*2)
        with patch.object(rp,'before_commit',side_effect=[None,RuntimeError('synthetic crash')]):
            with self.assertRaises(RuntimeError):self.run_project()
        self.assertEqual(self.run_project()['processed'],2)
    def test_updates_deletes_and_replace_blocked(self):
        self.write([self.row,dict(self.row,original='invalid')]);self.run_project()
        with store.ledger(self.db) as c:
            rp.register(c);c.execute('PRAGMA recursive_triggers=OFF')
            for table in rp.migration.KEYS:
                for sql in (f'DELETE FROM {table}',f'UPDATE OR REPLACE {table} SET '+rp.migration.KEYS[table][0]+'='+rp.migration.KEYS[table][0],f'INSERT OR REPLACE INTO {table} SELECT * FROM {table} LIMIT 1'):
                    with self.assertRaises(sqlite3.DatabaseError,msg=sql):c.execute(sql)
    def test_fresh_insert_inconsistent_item_blocked(self):
        self.run_project()
        with store.ledger(self.db) as c:
            rp.register(c)
            with self.assertRaises(sqlite3.IntegrityError):c.execute("INSERT INTO rp_items SELECT 'new',manifest_id,source_id,2,raw,plan,artifact_sha,capture_error,original_sha,binding_verified,evidence_sha FROM rp_items")
    def test_mutated_binding_failclosed(self):
        self.run_project()
        with store.ledger(self.db) as c:
            c.execute('DROP TRIGGER rp_items_no_update');c.execute('UPDATE rp_items SET binding_verified=0');c.commit()
        with self.assertRaisesRegex(ValueError,'binding mutation'):self.run_project()
    def test_mutated_gap_failclosed(self):
        (self.root/'artifact.json').unlink();self.run_project()
        with store.ledger(self.db) as c:
            c.execute('DROP TRIGGER rp_gaps_no_update');c.execute("UPDATE rp_gaps SET reason='changed'");c.commit()
        with self.assertRaisesRegex(ValueError,'gap mutation'):self.run_project()
    def test_index_hash_mismatch_explicit_gap(self):
        (self.root/'index.jsonl').write_bytes(b'changed\n');r=self.run_project()
        self.assertEqual(r['status'],'captured_with_gaps');self.assertEqual(r['processed'],0)
        self.assertIn(('index_hash_mismatch',),self.query('SELECT reason FROM rp_gaps'))
    def test_missing_index_explicit_gap(self):
        (self.root/'index.jsonl').unlink();self.assertEqual(self.run_project()['gaps'],1)
    def test_changed_manifest_is_distinct_capture(self):
        first=self.run_project();self.row['coverage']=[2];self.write([self.row]);second=self.run_project()
        self.assertNotEqual(first['manifest_id'],second['manifest_id']);self.assertEqual(second['processed'],1)
    def test_invalid_batch_bounds(self):
        for n in (0,True,1001):
            with self.assertRaises(ValueError):self.run_project(batch_size=n)
    def test_changing_input_explicit_gap(self):
        real=rp.capture
        def changing(root,path,limit):return (None,'changing_input') if path=='artifact.json' else real(root,path,limit)
        with patch.object(rp,'capture',side_effect=changing):self.run_project()
        self.assertIn(('changing_input',),self.query('SELECT reason FROM rp_gaps'))
    def test_no_recursive_hash_selection(self):
        self.fields.pop('original_hash');self.row['nested']={'hash':self.original};self.write([self.row])
        self.assertEqual(self.run_project()['binding_verified'],0)
    def test_private_root_required(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(ValueError,'private'):self.run_project()
    def test_record_bounds_exact_snapshot(self):
        with patch.object(rp,'MAX_RECORD',4):r=self.run_project()
        self.assertEqual(r['processed'],1);self.assertIn(('record_oversize',),self.query('SELECT reason FROM rp_gaps'))
    def test_root_rebinding_rejected(self):
        self.run_project()
        with tempfile.TemporaryDirectory() as d:
            other=Path(d);other.chmod(0o700);(other/'manifest.json').write_bytes((self.root/'manifest.json').read_bytes())
            with self.assertRaisesRegex(ValueError,'root changed'):rp.project(self.db,other,'manifest.json')

    def test_old_new_collision_update_replace(self):
        self.write([self.row,self.row]);self.run_project()
        with store.ledger(self.db) as c:
            rp.register(c);c.execute('PRAGMA recursive_triggers=OFF')
            ids=[r[0] for r in c.execute('SELECT id FROM rp_items ORDER BY line')]
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute('UPDATE OR REPLACE rp_items SET id=? WHERE id=?',(ids[1],ids[0]))
        self.assertEqual(self.run_project()['processed'],2)
    def test_mutated_artifact_replay_failclosed(self):
        self.run_project()
        with store.ledger(self.db) as c:
            rp.register(c);c.execute('DROP TRIGGER rp_artifacts_no_update')
            # Bypass CHECK only to emulate an externally damaged snapshot.
            c.execute('PRAGMA ignore_check_constraints=ON');c.execute("UPDATE rp_artifacts SET body=x'00'");c.commit()
        with self.assertRaisesRegex(ValueError,'artifact mutation'):self.run_project()
    def test_source_snapshot_mutation_rejected(self):
        self.run_project()
        with store.ledger(self.db) as c:
            c.execute('DROP TRIGGER rp_sources_no_update');c.execute('PRAGMA ignore_check_constraints=ON')
            c.execute("UPDATE rp_sources SET body=x'00'");c.commit()
        with self.assertRaisesRegex(ValueError,'source bytes mutation'):self.run_project()
    def test_fresh_insert_wrong_source_mapping_rejected(self):
        self.run_project()
        with store.ledger(self.db) as c:
            rp.register(c)
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO rp_sources SELECT 'other',manifest_id,1,path,mapping,expected_sha,observed_sha,size,body,error FROM rp_sources")
    def test_line_count_bound_rollback(self):
        self.write([self.row]*3)
        with patch.object(rp,'MAX_LINES',2):
            with self.assertRaisesRegex(ValueError,'line bound'):self.run_project()
        self.assertEqual(self.query('SELECT count(*) FROM rp_manifests'),[(0,)])
    def test_non_utf8_is_malformed_not_guessed(self):
        self.write([],b'\xff\xfe{\x00}\x00\n')
        self.assertGreater(self.run_project()['gaps'],0)
    def test_different_reviewed_content_hash_not_receipt_identity(self):
        self.row['reviewed']=self.original;self.write([self.row]);self.run_project()
        p=json.loads(self.query('SELECT plan FROM rp_items')[0][0])
        self.assertEqual(p['reviewed_content_hash'],self.original)
        self.assertNotEqual(p['artifact_hash'],p['original_hash'])
    def test_changing_read_bytes_preserved_unaccepted(self):
        real=rp.capture
        def changing(root,path,limit):return (self.art,'changing_input') if path=='artifact.json' else real(root,path,limit)
        with patch.object(rp,'capture',side_effect=changing):r=self.run_project()
        self.assertEqual(r['binding_verified'],0)
        self.assertEqual(self.query('SELECT body FROM rp_artifacts')[0][0],self.art)

    def test_mutated_source_hash_status_rejected(self):
        (self.root/'index.jsonl').write_bytes(b'changed\n');self.run_project()
        with store.ledger(self.db) as c:
            rp.register(c);c.execute('DROP TRIGGER rp_sources_no_update');c.execute('UPDATE rp_sources SET error=NULL');c.commit()
        with self.assertRaisesRegex(ValueError,'source hash binding mutation'):self.run_project()
    def test_fresh_insert_without_artifact_or_gap_rejected(self):
        self.run_project()
        with store.ledger(self.db) as c:
            rp.register(c)
            with self.assertRaises(sqlite3.DatabaseError):
                c.execute("INSERT INTO rp_items SELECT 'fake',manifest_id,source_id,2,raw,plan,NULL,NULL,original_sha,0,evidence_sha FROM rp_items")

    def test_explicit_source_locator_to_frozen_artifact_mapping(self):
        self.row['path']='/fictional-source/receipt.json';self.write([self.row])
        self.manifest['indexes'][0]['artifact_paths']={'/fictional-source/receipt.json':'artifact.json'}
        (self.root/'manifest.json').write_text(json.dumps(self.manifest))
        self.assertEqual(self.run_project()['binding_verified'],1)
        p=json.loads(self.query('SELECT plan FROM rp_items')[0][0])
        self.assertEqual(p['artifact_source_locator'],'/fictional-source/receipt.json')
        self.assertEqual(p['artifact_path'],'artifact.json')
    def test_mapping_cannot_escape_root(self):
        self.manifest['indexes'][0]['artifact_paths']={'artifact.json':'../not-permitted'}
        (self.root/'manifest.json').write_text(json.dumps(self.manifest))
        self.assertEqual(self.run_project()['binding_verified'],0)
    def test_parent_capture_manifest_needs_explicit_roles(self):
        (self.root/'manifest.json').write_text(json.dumps({'schema':'private-receipt-index-capture-v1','entries':[]}))
        with self.assertRaisesRegex(ValueError,'manifest'):self.run_project()

    def test_empty_artifact_locator_is_explicit_gap(self):
        self.row['path']='';self.write([self.row]);self.assertEqual(self.run_project()['binding_verified'],0)
        self.assertIn(('invalid_artifact_path',),self.query('SELECT reason FROM rp_gaps'))
    def test_both_original_roles_remain_unresolved(self):
        self.fields['original_hash_candidates']='candidates';self.row['candidates']=[self.original];self.write([self.row])
        self.assertEqual(self.run_project()['binding_verified'],0)
        self.assertIn(('ambiguous_original_hash_role',),self.query('SELECT reason FROM rp_gaps'))


    def test_rehashed_nonexistent_source_line_rejected_before_commit(self):
        self.run_project()
        with store.ledger(self.db) as c:
            rp.register(c)
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'item evidence mismatch'):
                c.execute("""INSERT INTO rp_items
                    SELECT rp_item_id(source_id,2),manifest_id,source_id,2,raw,plan,
                    artifact_sha,capture_error,original_sha,binding_verified,
                    rp_evidence(rp_item_id(source_id,2),manifest_id,source_id,2,raw,plan,
                    artifact_sha,capture_error,original_sha,binding_verified)
                    FROM rp_items LIMIT 1""")
            c.commit()
            self.assertEqual(c.execute('SELECT count(*) FROM rp_items').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM rp_steps').fetchone()[0],1)
            self.assertEqual(list(c.execute('PRAGMA foreign_key_check')),[])
        replay=self.run_project()
        self.assertEqual(replay['processed'],1)
        self.assertEqual(replay['added'],0)

if __name__=='__main__':unittest.main()
