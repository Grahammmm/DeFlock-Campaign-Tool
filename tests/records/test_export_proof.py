"""Controlled exporter fixture only. No IMAP, messages, timers or live services."""
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from campaign_tool.records.runner import core,export_proof,attestation
from campaign_tool.records.runner.adapters import LegacyIntakeBackend
from tests.records import test_runner as fixtures

SCRIPT='''import datetime,json,pathlib,sys,time
root=pathlib.Path(sys.argv[1]);mode=sys.argv[2];stamp=float(sys.argv[3])
print('fictional private output must be discarded');print('fictional stderr',file=sys.stderr)
if mode=='failure':sys.exit(3)
if mode=='timeout':time.sleep(3)
receipt=json.loads((root/'seed.json').read_text())
(root/'receipts').mkdir(exist_ok=True)
(root/'receipts'/'one.json').write_text(json.dumps(receipt))
message={k:receipt[k] for k in ('account_id','folder','uidvalidity','uid')}
message['sha256']=receipt['original_eml']['sha256']
parts=[]
for obj in receipt['attachments']:
 row=dict(message,part=obj['part'],sha256=obj['sha256']);parts.append(row)
(root/'messages.jsonl').write_text('' if mode=='gap' else json.dumps(message)+'\\n')
(root/'attachments.jsonl').write_text(''.join(json.dumps(p)+'\\n' for p in parts))
iso=lambda value:datetime.datetime.fromtimestamp(value,datetime.timezone.utc).isoformat()
status={'time_utc':iso(stamp-1 if mode=='stale' else stamp),'failures':['fixture error'] if mode=='failed-status' else [],
 'folders':[{'folder':receipt['folder'],'uidvalidity':receipt['uidvalidity'],'messages_scanned':1}],
 'messages':1,'attachments':len(parts)}
(root/'SYNC-STATUS.json').write_text(json.dumps(status))
(root/'sync-state.json').write_text(json.dumps({'last_success':iso(stamp)}))
'''

class ExportProofTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.RunnerTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.delta.root;self.control=self.fixture.root
        (self.root/'seed.json').write_text(json.dumps(self.fixture.delta.receipt))
        self.script=self.fixture.delta.base/'fixture_exporter.py';self.script.write_text(SCRIPT);self.script.chmod(0o600)
        self.lock=self.root/'sync.lock';self.lock.write_text('');self.lock.chmod(0o600)
        self.executable=str(Path(sys.executable).resolve())
        self.spec={'argv':[self.executable,str(self.script),str(self.root),'success','1000'],
          'root':str(self.root),'status_path':'SYNC-STATUS.json','state_path':'sync-state.json','receipt_dir':'receipts',
          'legacy_lock_path':str(self.lock),'indexes':{
           'messages':{'path':'messages.jsonl','format':'jsonl','fields':{k:k for k in ('account_id','folder','uidvalidity','uid','sha256')}},
           'attachments':{'path':'attachments.jsonl','format':'jsonl','fields':{k:k for k in ('account_id','folder','uidvalidity','uid','sha256','part')}}},
          'folder_fields':{'folder':'folder','uidvalidity':'uidvalidity','messages':'messages_scanned'},
          'attachment_part_encoding':'exporter_receipt_part','timeout_seconds':1}
        self.fixture.profile['export']=self.spec
        self.pin()
    def pin(self):
        self.spec['argv_sha256']=core.hid(self.spec['argv'])
        self.spec['command_inputs']={self.executable:export_proof.file_digest(self.executable),str(self.script):export_proof.file_digest(self.script)}
        self.fixture.write_profile()
    def mode(self,value):self.spec['argv'][3]=value;self.pin()
    def wrapper(self):return export_proof.ExporterWrapper(self.control,self.spec,'synthetic-account')
    def invoke(self,wrapper=None):return self.fixture.invoke(provider=wrapper or self.wrapper())
    def prepare(self,wrapper=None):
        value=wrapper or self.wrapper()
        identity={'run_id':'fixture-run','config_sha256':hashlib.sha256(b'fixture config').hexdigest(),'export_spec_sha256':core.hid(self.spec)}
        proof=value.prepare(identity,1000,lambda:1000);return value,proof
    def test_success_exact_status_index_receipt_snapshot_before_checkpoint(self):
        value=self.wrapper();result=self.invoke(value)
        self.assertEqual(result['messages_preserved'],1);self.assertIn('export_proof_sha256',result)
        proof=json.loads((value.destination/'manifest.json').read_text())
        self.assertTrue(proof['coverage_verified'])
        for name,item in proof['entries'].items():
            raw=(value.destination/name).read_bytes();self.assertEqual(hashlib.sha256(raw).hexdigest(),item['sha256']);self.assertEqual(len(raw),item['bytes'])
        identity=json.loads(self.fixture.query('SELECT identity FROM runner_runs')[0][0])
        self.assertEqual(identity['export_proof']['proof_sha256'],result['export_proof_sha256'])
        self.assertEqual(self.fixture.query('SELECT highest_uid FROM runner_folders'),[(64,)])
    def test_old_status_cannot_advance_checkpoint_or_cutoff(self):
        self.mode('stale')
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.fixture.query('SELECT count(*) FROM runner_folders'),[(0,)])
        self.assertNotIn('export_proof',json.loads(self.fixture.query('SELECT identity FROM runner_runs')[0][0]))
    def test_index_gap_cannot_advance_checkpoint(self):
        self.mode('gap')
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.fixture.query('SELECT count(*) FROM runner_messages'),[(0,)])
    def test_failed_status_not_successful_export(self):
        self.mode('failed-status')
        with self.assertRaises(ValueError):self.invoke()
    def test_failed_command_output_not_in_events(self):
        self.mode('failure')
        with self.assertRaises(ValueError):self.invoke()
        self.assertNotIn('fictional',str(self.fixture.query('SELECT * FROM runner_events')))
    def test_shell_false_and_output_discarded(self):
        original=export_proof.subprocess.Popen
        with patch.object(export_proof.subprocess,'Popen',wraps=original) as call:self.prepare()
        kw=call.call_args.kwargs;self.assertIs(kw['shell'],False)
        self.assertEqual(kw['stdout'],export_proof.subprocess.DEVNULL);self.assertEqual(kw['stderr'],export_proof.subprocess.DEVNULL)
    def test_timeout_blocked(self):
        self.mode('timeout')
        with self.assertRaisesRegex(export_proof.ExportBlocked,'timeout'):self.prepare()
    def test_argv_changed_without_new_pin_rejected(self):
        self.spec['argv'][3]='failure'
        with self.assertRaisesRegex(export_proof.ExportBlocked,'argv_pin'):self.prepare()
    def test_code_input_changed_rejected(self):
        self.script.write_text(SCRIPT+'\n# changed\n')
        with self.assertRaisesRegex(export_proof.ExportBlocked,'input_pin'):self.prepare()
    def test_missing_index_identity_mapping_rejected(self):
        del self.spec['indexes']['messages']['fields']['uidvalidity']
        with self.assertRaisesRegex(export_proof.ExportBlocked,'schema'):self.prepare()
    def test_busy_exporter_lock_no_snapshot(self):
        with export_proof.export_lock(self.lock):
            with self.assertRaisesRegex(export_proof.ExportBlocked,'lock_busy'):self.prepare()
    def test_publication_failure_no_checkpoint(self):
        with patch.object(export_proof.os,'rename',side_effect=RuntimeError('fixture interruption')):
            with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.fixture.query('SELECT count(*) FROM runner_messages'),[(0,)])
    def test_frozen_receipt_tampering_detected(self):
        value,proof=self.prepare();path=Path(value.receipt(value.folders()[0],64));path.write_bytes(b'changed')
        with self.assertRaisesRegex(export_proof.ExportBlocked,'frozen_receipt'):value.receipt(value.folders()[0],64)
    def test_repeated_runs_no_duplicate_occurrences(self):
        self.invoke();second=self.invoke();self.assertEqual(second['messages_preserved'],0)
        self.assertEqual(self.fixture.query('SELECT count(*) FROM runner_messages'),[(1,)])
    def test_missing_independent_attestation_explicit(self):
        result=self.invoke();self.assertEqual(result['image_attestation'],'declared_only')
        self.assertIn('independent_image_attestation_missing_or_unverified',result['release_blockers'])
        self.assertEqual(result['mode'],'fixture');self.assertFalse(result['release_ready'])
    def test_release_mode_without_attestation_refused(self):
        self.fixture.profile['mode']='release';self.fixture.write_profile()
        with self.assertRaisesRegex(ValueError,'attestation_blocked'):self.invoke()
    def test_record_boolean_is_not_independent_attestation(self):
        raw=json.dumps({'image_digest':self.fixture.runtime['image_digest'],'code_sha256':self.fixture.runtime['code_sha256'],'verified':True}).encode()
        path=self.control/'image-proof.json';path.write_bytes(raw);path.chmod(0o600)
        profile={'image_attestation':{'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'verifier_id':'fixture-host-attestor-v1'}}
        result=attestation.assess(profile,self.fixture.runtime)
        self.assertEqual(result['state'],'unverified')
    def test_trusted_verifier_must_match_exact_artifact_and_runtime(self):
        raw=json.dumps({'image_digest':self.fixture.runtime['image_digest'],'code_sha256':self.fixture.runtime['code_sha256']}).encode()
        path=self.control/'image-proof.json';path.write_bytes(raw);path.chmod(0o600)
        profile={'image_attestation':{'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'verifier_id':'fixture-host-attestor-v1'}}
        # Synthetic equality checker only; never supplied to production.
        checker=lambda observed,runtime,verifier_id:observed==raw and runtime==self.fixture.runtime and verifier_id=='fixture-host-attestor-v1'
        self.assertEqual(attestation.assess(profile,self.fixture.runtime,checker)['state'],'independently_verified')
        path.write_bytes(raw+b' ');self.assertEqual(attestation.assess(profile,self.fixture.runtime,checker)['state'],'unverified')
    def test_unbound_export_configuration_never_executes(self):
        value=self.wrapper();self.spec['timeout_seconds']=2
        with patch.object(export_proof.subprocess,'Popen') as call:
            with self.assertRaises(ValueError):self.invoke(value)
        call.assert_not_called()
        self.assertEqual(self.fixture.query('SELECT count(*) FROM runner_messages'),[(0,)])
    def test_missing_trusted_export_hash_never_executes(self):
        with patch.object(export_proof.subprocess,'Popen') as call:
            with self.assertRaisesRegex(export_proof.ExportBlocked,'trusted_profile'):
                self.wrapper().prepare({'run_id':'fixture','config_sha256':'0'*64},1000,lambda:1000)
        call.assert_not_called()
    def test_changed_frozen_index_prevents_checkpoint(self):
        class IndexMutating(fixtures.SyntheticPromotingBackend):
            def preserve(backend,*args):
                result=super().preserve(*args)
                root=Path(args[0]).parent.parent
                (root/'messages.index').write_bytes(b'changed index')
                return result
        backend=IndexMutating(self.fixture.delta.root,self.fixture.delta.out)
        result=self.fixture.invoke(provider=self.wrapper(),backend=backend)
        self.assertEqual(result['mail_failures'],1)
        self.assertEqual(self.fixture.query('SELECT highest_uid FROM runner_folders'),[(0,)])
    def test_duplicate_json_fields_rejected(self):
        with self.assertRaises(ValueError):export_proof.decode('{"uid":1,"uid":2}')
    def test_rows_are_bounded_before_oversized_list(self):
        with patch.object(export_proof,'MAX_ROWS',1):
            with self.assertRaises(export_proof.ExportBlocked):
                export_proof.rows(b'{}\n{}\n',{'format':'jsonl'})
    def test_receipt_directory_inventory_is_bounded(self):
        directory=self.root/'receipts';directory.mkdir()
        for n in range(4):(directory/('unrelated-'+str(n))).write_bytes(b'')
        with patch.object(export_proof,'MAX_ROWS',3):
            with self.assertRaisesRegex(export_proof.ExportBlocked,'directory_item_bound'):self.prepare()
    def test_scheduler_templates_are_explicit_preparation_only(self):
        directory=Path(core.__file__).parent/'templates'
        timer=(directory/'records.timer.in').read_text();service=(directory/'records.service.in').read_text()
        for time in ('07:00:00','13:00:00','20:30:00'):
            self.assertIn('OnCalendar=*-*-* '+time+' America/Los_Angeles',timer)
        self.assertEqual(timer.count('OnCalendar='),3)
        self.assertIn('PREPARATION ONLY',timer);self.assertIn('@INSTALLED_TRUSTED_LAUNCHER@',service)
        self.assertIn('@OWNER_ONLY_MAIL_ENV@',service)
    def test_post_preservation_receipt_mismatch_cannot_checkpoint(self):
        class Mutating(fixtures.SyntheticPromotingBackend):
            def preserve(backend,*args):
                value=super().preserve(*args);Path(args[0]).write_bytes(b'changed');return value
        backend=Mutating(self.fixture.delta.root,self.fixture.delta.out)
        result=core.run(self.control,self.fixture.profile_path,self.wrapper(),backend,{'catalog':self.fixture.hook},
                        runtime_provider=lambda:self.fixture.runtime,clock=lambda:1000)
        self.assertEqual(result['mail_failures'],1);self.assertEqual(self.fixture.query('SELECT highest_uid FROM runner_folders'),[(0,)])

if __name__=='__main__':unittest.main()
