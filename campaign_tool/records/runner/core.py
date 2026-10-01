"""Single-owner, bounded and recoverable preparatory runner control plane."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid
from campaign_tool import __version__
from . import migration
from .contracts import IntegrationGap,StageReceipt

STAGES=('extract','catalog','detect','review','compare','privacy')

def js(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False)
def hid(v):return hashlib.sha256(js(v).encode()).hexdigest()
def hash_ok(v):return isinstance(v,str) and re.fullmatch('[0-9a-f]{64}',v) is not None

def private_path(value,directory=False):
    p=Path(os.path.abspath(value))
    if any(q.is_symlink() for q in (p,*p.parents)):raise ValueError('unsafe_control_path')
    mode=p.stat()
    if mode.st_uid!=os.getuid() or mode.st_mode&0o077 or not (stat.S_ISDIR(mode.st_mode) if directory else stat.S_ISREG(mode.st_mode)):
        raise ValueError('private_control_required')
    return p

@contextmanager
def writer_lock(root):
    root=private_path(root,True)
    fd=os.open(root/'writer.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if info.st_uid!=os.getuid() or info.st_mode&0o077 or not stat.S_ISREG(info.st_mode):raise ValueError('unsafe_writer_lock')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('runner_already_owned') from None
        yield
    finally:os.close(fd)

def load_profile(path):
    p=private_path(path)
    with p.open('rb') as f:raw=f.read(256*1024+1)
    if len(raw)>256*1024:raise ValueError('profile_bound')
    p=json.loads(raw)
    if p.get('version')!=1 or not isinstance(p.get('account_id'),str) or not 0<len(p['account_id'])<=256:raise ValueError('profile_invalid')
    folders=p.get('folders');stages=p.get('stages')
    if not isinstance(folders,list) or not 1<=len(folders)<=100 or any(not isinstance(x,str) or not x or len(x)>256 for x in folders) or len(set(folders))!=len(folders):raise ValueError('profile_folders_invalid')
    if not isinstance(stages,list) or not stages or len(set(stages))!=len(stages) or any(x not in STAGES for x in stages) or stages!=sorted(stages,key=STAGES.index):raise ValueError('profile_stages_invalid')
    p.setdefault('mode','preparation')
    if p['mode'] not in ('fixture','preparation','release'):raise ValueError('profile_mode_invalid')
    if not isinstance(p.get('runtime'),dict):raise ValueError('runtime_pin_missing')
    for field in ('version','code_sha256','image_digest','image_verification'):
        if not isinstance(p['runtime'].get(field),str):raise ValueError('runtime_pin_missing')
    if not hash_ok(p['runtime']['code_sha256']) or not re.fullmatch('sha256:[0-9a-f]{64}',p['runtime']['image_digest']):raise ValueError('runtime_pin_invalid')
    for field,default,limit in (('max_messages',1000,10000),('max_work',100,1000),('retry_limit',3,10),('retry_delay',60,86400),('lease_seconds',300,3600)):
        p.setdefault(field,default)
        if type(p[field]) is not int or not 1<=p[field]<=limit:raise ValueError('profile_budget_invalid')
    return p,hashlib.sha256(raw).hexdigest()

def observed_runtime(image_digest):
    # Exact code inputs for this preparatory slice. WP0 host-image attestation must replace
    # the caller-declared image field before any activation; this is stated in each run.
    from campaign_tool.records.intake import mail_delta,folder
    files=[Path(__file__),Path(migration.__file__),Path(__file__).with_name('contracts.py'),
           Path(__file__).with_name('adapters.py'),Path(__file__).with_name('__main__.py'),
           Path(__file__).with_name('__init__.py'),Path(__file__).with_name('wp1_bridge.py'),
           Path(__file__).with_name('canonical_mail.py'),Path(__file__).with_name('export_proof.py'),
           Path(__file__).with_name('attestation.py'),Path(mail_delta.__file__),Path(folder.__file__)]
    inventory=[]
    for p in files:
        if p.is_symlink() or p.stat().st_size>8*1024*1024:raise ValueError('runtime_source_bound')
        inventory.append((p.name,hashlib.sha256(p.read_bytes()).hexdigest()))
    return {'version':__version__,'code_sha256':hid(inventory),'image_digest':image_digest,
            'image_verification':'caller_declared_not_host_verified'}

def journal(root):
    p=root/'runner.sqlite'
    if p.exists():private_path(p)
    else:
        fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600);os.close(fd)
    con=sqlite3.connect(p,timeout=5);con.row_factory=sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON');con.execute('PRAGMA journal_mode=WAL');con.execute('PRAGMA recursive_triggers=ON')
    checksum=hashlib.sha256(migration.SQL.encode()).hexdigest()
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='runner_schema'").fetchone():
        if [tuple(r) for r in con.execute('SELECT * FROM runner_schema')]!=[(migration.VERSION,checksum)]:con.close();raise ValueError('runner_schema_mismatch')
    else:
        con.executescript('BEGIN IMMEDIATE;\n'+migration.SQL)
        con.execute('INSERT INTO runner_schema VALUES(?,?)',(migration.VERSION,checksum));con.commit()
    extension=hashlib.sha256(migration.RELIABILITY_SQL.encode()).hexdigest()
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='runner_reliability_schema'").fetchone():
        if [tuple(r) for r in con.execute('SELECT * FROM runner_reliability_schema')]!=[(migration.RELIABILITY_VERSION,extension)]:
            con.close();raise ValueError('runner_reliability_schema_mismatch')
    else:
        try:
            con.executescript('BEGIN IMMEDIATE;\n'+migration.RELIABILITY_SQL)
            con.execute('INSERT INTO runner_reliability_schema VALUES(?,?)',(migration.RELIABILITY_VERSION,extension));con.commit()
        except BaseException:con.rollback();con.close();raise
    return con

def safe_code(error):
    if isinstance(error,IntegrationGap):
        return str(error) if str(error) in {'wp1_promotion_adapter_unavailable','mail_header_bound','mail_header_identity_bound','stage_hook_unavailable','wp1_dependency_unavailable','wp1_installed_preservation_adapter_missing','wp1_preservation_content_bound','export_trusted_profile_mismatch','export_status_stale_or_future','export_index_receipt_gap','export_failed_coverage','export_command_timeout','export_command_failed','legacy_export_lock_busy','frozen_snapshot_changed','frozen_manifest_changed','frozen_receipt_changed','command_input_pin_mismatch','command_input_changed','export_argv_pin_mismatch','index_schema_unresolved','folder_schema_unresolved','export_status_invalid','export_status_counts_invalid','export_status_count_gap','snapshot_byte_bound','receipt_count_bound','receipt_directory_item_bound','index_parse_failed','index_row_bound','export_input_changed','export_input_bound','proof_identity_collision','receipt_changed_during_capture','duplicate_receipt_identity','duplicate_attachment_identity','index_identity_invalid','folder_coverage_invalid'} else 'integration_adapter_unavailable'
    if isinstance(error,ValueError) and str(error) in {'canonical_original_binding_conflict','canonical_occurrence_conflict','canonical_mail_identity_conflict','canonical_run_binding_conflict','canonical_finalization_conflict','canonical_enrollment_run_not_live','canonical_started_run_missing'}:return str(error)
    if isinstance(error,ConnectionError):return 'provider_connection_drop'
    return 'stage_or_input_rejected'  # Never put exception narratives/credentials into reports.

def event(con,rid,kind,key,code):con.execute('INSERT INTO runner_events(run_id,kind,item_key,code) VALUES(?,?,?,?)',(rid,kind,key,code))

def lifecycle_intent(con,identity,operation,outcome=None,summary=None):
    """Caller owns transaction. Once recorded, intended outcome never changes."""
    values=(identity['run_id'],operation,js(identity),outcome,js(summary or {}))
    old=con.execute('SELECT operation,identity,outcome,summary FROM runner_lifecycle WHERE run_id=?',(values[0],)).fetchone()
    if old:
        if tuple(old)!=values[1:]:raise ValueError('lifecycle_intent_conflict')
    else:con.execute('INSERT INTO runner_lifecycle(run_id,operation,identity,outcome,summary) VALUES(?,?,?,?,?)',values)


def service_lifecycle(con,backend,current_run,profile,clock,only=None):
    """Bounded durable retries, independently of the original control status.

    No terminal abandonment: lifecycle I/O retries are separately capped per run
    and exponentially delayed (at most a day). Domain work retry_limit is unchanged.
    """
    where="state='pending' AND next_eligible<=?";args=[clock()]
    if only is not None:where+=' AND run_id=?';args.append(only)
    args.append(min(profile['max_work'],100))
    rows=con.execute('SELECT * FROM runner_lifecycle WHERE '+where+' ORDER BY attempts,next_eligible,run_id LIMIT ?',args).fetchall()
    acknowledged=0
    for row in rows:
        with con:con.execute('UPDATE runner_lifecycle SET attempts=attempts+1 WHERE run_id=?',(row['run_id'],))
        try:
            identity=json.loads(row['identity']);payload=json.loads(row['summary'])
            handler=getattr(backend,'recover_run' if row['operation']=='recover' else 'finish_run',None)
            if not callable(handler):raise IntegrationGap('canonical_lifecycle_adapter_unavailable')
            ack=handler(identity) if row['operation']=='recover' else handler(identity,row['outcome'],payload)
            allowed=('finalized','already_finalized','absent') if row['operation']=='recover' else ('finalized','already_finalized')
            if ack not in allowed:raise ValueError('canonical_lifecycle_ack_invalid')
            with con:
                con.execute("UPDATE runner_lifecycle SET state='done',error_code=NULL,acknowledgment=? WHERE run_id=?",(ack,row['run_id']))
                event(con,current_run,'canonical_'+row['operation'],row['run_id'],'canonical_'+ack)
                if row['operation']=='finish':
                    control_summary=dict(payload,canonical_run_finalization=ack)
                    con.execute('UPDATE runner_runs SET status=?,ended=?,summary=? WHERE id=?',
                        (row['outcome'],clock(),js(control_summary),row['run_id']))
            acknowledged+=1
        except Exception as error:
            delay=min(86400,profile['retry_delay']*(2**min(row['attempts'],16)))
            with con:
                con.execute('UPDATE runner_lifecycle SET next_eligible=?,error_code=? WHERE run_id=?',
                    (clock()+delay,safe_code(error),row['run_id']))
                event(con,current_run,'canonical_'+row['operation'],row['run_id'],'canonical_lifecycle_pending')
    return acknowledged


def run(control_root,profile_path,exporter,backend,hooks,*,runtime_provider,clock=time.time,attestation_verifier=None):
    root=private_path(control_root,True);profile,config_hash=load_profile(profile_path)
    with writer_lock(root):
        runtime=runtime_provider()
        if runtime!=profile['runtime']:raise ValueError('runtime_pin_mismatch')
        from .attestation import assess
        attestation=assess(profile,runtime,attestation_verifier)
        if profile['mode']=='release' and attestation['state']!='independently_verified':raise ValueError('release_image_attestation_blocked')
        con=journal(root);rid=uuid.uuid4().hex;stamp=clock()
        identity={'run_id':rid,'runtime':runtime,'config_sha256':config_hash,'runner_schema':migration.VERSION,
                  'canonical_integration_verified':False,'mode':profile['mode'],'image_attestation':attestation}
        summary={'messages_preserved':0,'mail_failures':0,'stage_promotions':0,'stage_failures':0,'folder_alerts':0,'recovered_runs':0,
                 'canonical_integration_verified':False,'pipeline_complete':False,'mode':profile['mode'],
                 'image_attestation':attestation['state'],'release_ready':False,
                 'release_blockers':(['independent_image_attestation_missing_or_unverified'] if attestation['state']!='independently_verified' else [])+['canonical_pipeline_not_verified']}
        backend_started=False;backend_attempted=False
        try:
            with con:
                old=[r[0] for r in con.execute("SELECT id FROM runner_runs WHERE status='running'")]
                for previous in old:
                    if callable(getattr(backend,'recover_run',None)) and not con.execute('SELECT 1 FROM runner_lifecycle WHERE run_id=?',(previous,)).fetchone():
                        prior_identity=json.loads(con.execute('SELECT identity FROM runner_runs WHERE id=?',(previous,)).fetchone()[0])
                        lifecycle_intent(con,prior_identity,'recover')
                    con.execute("UPDATE runner_runs SET status='interrupted',ended=? WHERE id=?",(stamp,previous))
                    con.execute("UPDATE runner_work SET state='pending',lease_until=NULL,error_code='process_recovered' WHERE state='running' AND run_id=?",(previous,))
                # The held lock proves no previous runner is still the writer, even
                # if its final status was recorded before a control-plane crash.
                con.execute("UPDATE runner_work SET state='pending',lease_until=NULL,error_code='process_recovered' WHERE state='running'")
                summary['recovered_runs']=len(old)
                con.execute('INSERT INTO runner_runs VALUES(?,?,NULL,?,?,?)',(rid,stamp,'running',js(identity),'{}'))
            # Backfill pre-extension interrupted/pending-finalization history in bounded
            # slices; never overwrite already durable finish intent with recovery.
            if callable(getattr(backend,'recover_run',None)):
                with con:
                    legacy=con.execute("SELECT r.identity FROM runner_runs r WHERE r.id!=? AND NOT EXISTS(SELECT 1 FROM runner_lifecycle l WHERE l.run_id=r.id) AND (r.status='interrupted' OR EXISTS(SELECT 1 FROM runner_events e WHERE e.run_id=r.id AND e.code='canonical_finalization_pending')) ORDER BY r.started,r.id LIMIT 100",(rid,)).fetchall()
                    for previous in legacy:lifecycle_intent(con,json.loads(previous[0]),'recover')
            summary['canonical_lifecycle_retried']=service_lifecycle(con,backend,rid,profile,clock)
            prepare_export=getattr(exporter,'prepare',None)
            if callable(prepare_export):
                if hasattr(exporter,'spec'):
                    configured_export=profile.get('export')
                    if not isinstance(configured_export,dict) or hid(configured_export)!=hid(exporter.spec):
                        raise IntegrationGap('export_trusted_profile_mismatch')
                    identity['export_spec_sha256']=hid(configured_export)
                proof=prepare_export(identity,stamp,clock)
                if not isinstance(proof,dict) or proof.get('coverage_verified') is not True or not hash_ok(proof.get('proof_sha256')):raise ValueError('export_proof_unverified')
                identity['export_proof']=proof
                with con:con.execute('UPDATE runner_runs SET identity=? WHERE id=?',(js(identity),rid))
                summary['verified_mail_cutoff']=proof['cutoff'];summary['export_proof_sha256']=proof['proof_sha256']
            elif profile['mode']=='release':raise ValueError('release_export_proof_missing')
            else:summary['release_blockers'].append('durable_export_proof_missing')
            start_backend=getattr(backend,'start_run',None)
            if callable(start_backend):
                backend_attempted=True;start_backend(identity);backend_started=True
            scopes=list(exporter.folders())
            if len(scopes)>100 or len({f.name for f in scopes})!=len(scopes):raise ValueError('folder_inventory_invalid')
            exposed={f.name for f in scopes};configured=set(profile['folders'])
            with con:
                for name in sorted(exposed^configured):
                    event(con,rid,'folder_inventory',hid([profile['account_id'],name]),'unconfigured_folder' if name not in configured else 'configured_folder_unavailable')
                    summary['folder_alerts']+=1
            used=0
            scopes=[scope for scope in scopes if scope.name in configured]
            cursor=con.execute('SELECT folder FROM runner_mail_cursor WHERE account=?',(profile['account_id'],)).fetchone()
            if cursor and cursor[0] in [scope.name for scope in scopes]:
                offset=next(n for n,scope in enumerate(scopes) if scope.name==cursor[0])+1
                scopes=scopes[offset:]+scopes[:offset]
            for scope in scopes:
                if used>=profile['max_messages']:break
                if not isinstance(scope.name,str) or type(scope.uidvalidity) is not int or not 0<scope.uidvalidity<2**63:raise ValueError('folder_identity_invalid')
                key=(profile['account_id'],scope.name,scope.uidvalidity)
                with con:
                    con.execute('INSERT INTO runner_mail_cursor VALUES(?,?) ON CONFLICT(account) DO UPDATE SET folder=excluded.folder',(profile['account_id'],scope.name))
                    known=con.execute('SELECT uidvalidity FROM runner_folders WHERE account=? AND folder=?',key[:2]).fetchall()
                    if known and not any(r[0]==scope.uidvalidity for r in known):event(con,rid,'folder_epoch',hid(key),'uidvalidity_reset_reenumerate')
                    con.execute('INSERT OR IGNORE INTO runner_folders VALUES(?,?,?,0,?)',key+(rid,))
                after=con.execute('SELECT highest_uid FROM runner_folders WHERE account=? AND folder=? AND uidvalidity=?',key).fetchone()[0]
                last=after
                try:
                    for uid in exporter.uids(scope,after):
                        if used>=profile['max_messages']:break
                        if type(uid) is not int or not last<uid<2**63:raise ValueError('uid_order_invalid')
                        used+=1
                        try:
                            preserved=backend.preserve(exporter.receipt(scope,uid),profile['account_id'],scope,uid)
                            check_proof=getattr(exporter,'verify_preserved',None)
                            if callable(check_proof) and check_proof(preserved,scope,uid) is not True:raise ValueError('preserved_export_proof_mismatch')
                            expected_id=hid(['mail',profile['account_id'],scope.name,scope.uidvalidity,uid])
                            if preserved.message_id!=expected_id or not hash_ok(preserved.eml_sha256) or not hash_ok(preserved.receipt_sha256) or not 1<=len(preserved.documents)<=101 or any(not hash_ok(s) for s in preserved.documents) or preserved.eml_sha256 not in preserved.documents:raise ValueError('preservation_identity_invalid')
                            if len(preserved.attachments)>100 or any(not re.fullmatch('1(?:\\.[1-9][0-9]*)+',loc) or not hash_ok(sha) or sha not in preserved.documents for loc,sha in preserved.attachments) or len({loc for loc,sha in preserved.attachments})!=len(preserved.attachments):raise ValueError('mime_identity_invalid')
                            evidence=js({'documents':preserved.documents,'attachments':preserved.attachments})
                            with con:
                                row=con.execute('SELECT eml_sha,receipt_sha,evidence FROM runner_messages WHERE id=?',(expected_id,)).fetchone()
                                if row and tuple(row)!=(preserved.eml_sha256,preserved.receipt_sha256,evidence):raise ValueError('message_identity_conflict')
                                if not row:con.execute('INSERT INTO runner_messages VALUES(?,?,?,?,?,?,?,?,?)',(expected_id,*key,uid,preserved.eml_sha256,preserved.receipt_sha256,evidence,rid))
                                for sha in preserved.documents:
                                    for stage in profile['stages']:
                                        con.execute("INSERT OR IGNORE INTO runner_work(subject_sha,stage,state,run_id) VALUES(?,?,'pending',?)",(sha,stage,rid))
                                con.execute('UPDATE runner_folders SET highest_uid=?,run_id=? WHERE account=? AND folder=? AND uidvalidity=?',(uid,rid,*key))
                            summary['messages_preserved']+=1;last=uid
                        except Exception as error:
                            with con:event(con,rid,'mail_failure',hid([*key,uid]),safe_code(error))
                            summary['mail_failures']+=1;break  # Never advance checkpoint beyond this UID.
                except Exception as error:
                    with con:event(con,rid,'folder_failure',hid(key),safe_code(error))
                    summary['mail_failures']+=1
            # Dependency eligibility is part of SQL selection, BEFORE the bound.
            # Untried eligible work precedes retries; skipped descendants spend no
            # budget and cannot hide unrelated originals behind an exhausted parent.
            eligible=[];bindings=[]
            for position,stage in enumerate(profile['stages']):
                clause='w.stage=?';bindings.append(stage)
                for earlier in profile['stages'][:position]:
                    clause+=" AND EXISTS(SELECT 1 FROM runner_work p WHERE p.subject_sha=w.subject_sha AND p.stage=? AND p.state='done')"
                    bindings.append(earlier)
                eligible.append('('+clause+')')
            stage_order='CASE w.stage '+ ' '.join("WHEN '"+stage+"' THEN "+str(n) for n,stage in enumerate(STAGES))+' END'
            tasks=con.execute("SELECT w.* FROM runner_work w WHERE w.state IN ('pending','blocked') AND w.attempts<? AND w.next_eligible<=? AND ("+' OR '.join(eligible)+") ORDER BY w.attempts,w.subject_sha,"+stage_order+" LIMIT ?",(profile['retry_limit'],clock(),*bindings,profile['max_work'])).fetchall()
            for item in tasks:
                sha,stage=item['subject_sha'],item['stage'];earlier=profile['stages'][:profile['stages'].index(stage)]
                if any(not con.execute("SELECT 1 FROM runner_work WHERE subject_sha=? AND stage=? AND state='done'",(sha,s)).fetchone() for s in earlier):continue
                with con:
                    con.execute("UPDATE runner_work SET state='running',attempts=attempts+1,lease_until=?,run_id=? WHERE subject_sha=? AND stage=?",(clock()+profile['lease_seconds'],rid,sha,stage))
                try:
                    if stage not in hooks:raise IntegrationGap('stage_hook_unavailable')
                    receipt=hooks[stage](sha,identity)
                    if not isinstance(receipt,StageReceipt) or receipt.subject_sha256!=sha or receipt.stage!=stage:raise ValueError('stage_receipt_identity')
                    receipt.validate();backend.validate_and_promote(receipt,identity)
                    with con:
                        con.execute("UPDATE runner_work SET state='done',lease_until=NULL,receipt_sha=?,error_code=NULL WHERE subject_sha=? AND stage=?",(receipt.sha256,sha,stage))
                        event(con,rid,'stage_result',hid([sha,stage]),'backend_promotion_acknowledged')
                    summary['stage_promotions']+=1
                except Exception as error:
                    with con:
                        con.execute("UPDATE runner_work SET state='blocked',lease_until=NULL,next_eligible=?,error_code=? WHERE subject_sha=? AND stage=?",(clock()+profile['retry_delay'],safe_code(error),sha,stage))
                        event(con,rid,'stage_failure',hid([sha,stage]),safe_code(error))
                    summary['stage_failures']+=1
            with con:
                con.execute("UPDATE runner_work SET state='blocked',error_code='retry_exhausted' WHERE state IN ('pending','blocked') AND attempts>=?",(profile['retry_limit'],))
            summary['unfinished_work']=con.execute("SELECT count(*) FROM runner_work WHERE state!='done'").fetchone()[0]
            summary['blocked_work']=con.execute("SELECT count(*) FROM runner_work WHERE state='blocked'").fetchone()[0]
            status='completed_with_gaps' if any(summary[k] for k in ('mail_failures','stage_failures','folder_alerts','unfinished_work')) else 'slice_completed'
            finish_backend=getattr(backend,'finish_run',None)
            if backend_started and callable(finish_backend):
                with con:lifecycle_intent(con,identity,'finish',status,dict(summary))
                service_lifecycle(con,backend,rid,profile,clock,only=rid)
                outcome=con.execute('SELECT state,acknowledgment FROM runner_lifecycle WHERE run_id=?',(rid,)).fetchone()
                summary['canonical_run_finalization']=outcome['acknowledgment'] if outcome['state']=='done' else 'pending'
            summary['canonical_lifecycle_pending']=con.execute("SELECT count(*) FROM runner_lifecycle WHERE state='pending'").fetchone()[0]
            if summary['canonical_lifecycle_pending']:status='completed_with_gaps'
            with con:con.execute('UPDATE runner_runs SET ended=?,status=?,summary=? WHERE id=?',(clock(),status,js(summary),rid))
            return {'run_id':rid,'status':status,**summary}
        except Exception as error:
            # Preserve an already queued completion verbatim; never reinterpret a
            # failed acknowledgment as a contradictory failed canonical outcome.
            if backend_attempted:
                with con:
                    existing=con.execute('SELECT 1 FROM runner_lifecycle WHERE run_id=?',(rid,)).fetchone()
                    if not existing:
                        if backend_started and callable(getattr(backend,'finish_run',None)):
                            lifecycle_intent(con,identity,'finish','failed',{'error_code':safe_code(error),'pipeline_complete':False})
                        else:lifecycle_intent(con,identity,'recover')
                service_lifecycle(con,backend,rid,profile,clock,only=rid)
            with con:
                event(con,rid,'run_failure',rid,safe_code(error));con.execute('UPDATE runner_runs SET ended=?,status=?,summary=? WHERE id=?',(clock(),'failed',js({'error_code':safe_code(error)}),rid))
            raise ValueError('runner_failed_redacted') from None
        finally:con.close()
