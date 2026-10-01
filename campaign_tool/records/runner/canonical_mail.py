"""Optional WP1 canonical mail enrollment and CAS-verified preservation adapter."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
from importlib import import_module
from .adapters import LegacyIntakeBackend
from .contracts import IntegrationGap
from .core import hid,js,private_path
from .contracts import Folder
from .wp1_bridge import installed_preservation_runner
import os
import tempfile
from email.parser import BytesHeaderParser
from email import policy
from campaign_tool.records.intake import folder as intake_folder,mail_delta

class CanonicalMailBackend(LegacyIntakeBackend):
    """No generic pass callback. Only mail/attachment preservation is implemented.

    Catalog and all substantive stages remain explicitly unavailable. The trusted
    caller installs/pins WP1 separately; this module never copies its schema/code.
    """
    def __init__(self,mail_root,intake_output,database,*,stage_runner_factory=None,installed_preservation=True):
        super().__init__(mail_root,intake_output);self.database=database
        try:
            self.store=import_module('campaign_tool.records.ledger.store')
            self.stages=import_module('campaign_tool.records.ledger.stages')
        except ImportError:raise IntegrationGap('wp1_dependency_unavailable') from None
        self.run_identity=None
        self.stage_runner_factory=stage_runner_factory
        self.stage_runner=None
        self.installed_preservation=installed_preservation
    def _run_binding(self,identity):
        runtime=identity['runtime'];proof=identity.get('export_proof',{})
        return {'run_id':identity['run_id'],'engine_version':runtime['version'],
            'image_digest':runtime['image_digest'],'config_sha256':identity['config_sha256'],
            'code_sha256':runtime['code_sha256'],'mode':identity.get('mode','preparation'),
            'coverage_cutoff':proof.get('cutoff') if proof.get('coverage_verified') is True else None,
            'export_proof_sha256':proof.get('proof_sha256')}
    def start_run(self,identity):
        binding=self._run_binding(identity);stamp=datetime.now(timezone.utc).isoformat()
        summary={'runner_binding':binding,'image_verification':identity['runtime']['image_verification'],
                 'candidate':True,'account_provenance_status':'configured_namespace_unattested'}
        with self.store.ledger(self.database) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                old=c.execute('SELECT * FROM runs WHERE run_id=?',(identity['run_id'],)).fetchone()
                if old:
                    if old['kind']!='records-runner' or old['status']!='running' or old['ended_at'] is not None or (old['engine_version'],old['image_digest'],old['config_sha256'],old['coverage_cutoff'])!=(binding['engine_version'],binding['image_digest'],binding['config_sha256'],binding['coverage_cutoff']) or json.loads(old['summary']).get('runner_binding')!=binding:
                        raise ValueError('canonical_run_binding_conflict')
                else:
                    c.execute('INSERT INTO runs(run_id,kind,started_at,engine_version,image_digest,config_sha256,coverage_cutoff,status,summary) VALUES(?,?,?,?,?,?,?,?,?)',
                        (identity['run_id'],'records-runner',stamp,binding['engine_version'],binding['image_digest'],binding['config_sha256'],binding['coverage_cutoff'],'running',js(summary)))
                c.commit()
            except BaseException:c.rollback();raise
        self.run_identity=identity;self.stage_runner=None
    def _finalize(self,identity,status,summary):
        binding=self._run_binding(identity)
        if status not in ('completed','completed_with_gaps','failed','interrupted'):raise ValueError('canonical_final_status_invalid')
        with self.store.ledger(self.database) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                old=c.execute('SELECT * FROM runs WHERE run_id=?',(binding['run_id'],)).fetchone()
                if old is None:return 'absent'
                prior=json.loads(old['summary'])
                if old['kind']!='records-runner' or (old['engine_version'],old['image_digest'],old['config_sha256'],old['coverage_cutoff'])!=(binding['engine_version'],binding['image_digest'],binding['config_sha256'],binding['coverage_cutoff']) or prior.get('runner_binding')!=binding:raise ValueError('canonical_run_binding_conflict')
                if old['status']!='running' or old['ended_at'] is not None:
                    if status=='interrupted':return 'already_finalized'
                    if old['status']==status and prior.get('runner_summary')==summary:return 'already_finalized'
                    raise ValueError('canonical_finalization_conflict')
                prior['runner_summary']=summary;prior['runner_outcome']=status
                c.execute('UPDATE runs SET status=?,ended_at=?,summary=? WHERE run_id=?',
                    (status,datetime.now(timezone.utc).isoformat(),js(prior),binding['run_id']))
                c.commit();return 'finalized'
            except BaseException:c.rollback();raise
    def finish_run(self,identity,status,summary):
        """Bounded caller result, not a claim of provider/image/domain readiness."""
        mapped={'slice_completed':'completed','completed_with_gaps':'completed_with_gaps','failed':'failed'}
        if status not in mapped:raise ValueError('canonical_final_status_invalid')
        outcome=self._finalize(identity,mapped[status],summary)
        if outcome=='absent':raise ValueError('canonical_started_run_missing')
        return outcome
    def recover_run(self,identity):
        """Called only under the control writer lock for a prior interrupted run."""
        return self._finalize(identity,'interrupted',{'reason':'control_writer_recovered','pipeline_complete':False})
    def _eml_projection(self,value,receipt_path,account,scope,uid):
        path=self.output/'blobs'/value.eml_sha256;raw=bytearray();ended=False
        with intake_folder.secure_open(path) as f:
            before=os.fstat(f.fileno())
            while len(raw)<=64*1024:
                line=f.readline(64*1024+1-len(raw))
                if not line:ended=True;break
                raw.extend(line)
                if line in (b'\n',b'\r\n'):ended=True;break
            after=os.fstat(f.fileno())
        stat_key=lambda v:(v.st_dev,v.st_ino,v.st_size,v.st_mtime_ns,v.st_ctime_ns)
        if not ended or len(raw)>64*1024:raise IntegrationGap('mail_header_bound')
        if stat_key(before)!=stat_key(after) or stat_key(after)!=stat_key(path.stat(follow_symlinks=False)):raise ValueError('mail_headers_changed')
        parsed=BytesHeaderParser(policy=policy.default).parsebytes(bytes(raw))
        fields=[(str(name),str(content)) for name,content in parsed.items()]
        ids=[str(v).strip() for v in parsed.get_all('Message-ID',[])];mid=ids[0] if len(ids)==1 and ids[0] else None
        if mid is not None and len(mid)>4096:raise IntegrationGap('mail_header_identity_bound')
        declaration,_,_=mail_delta.load_receipt(receipt_path)
        received=declaration.get('internaldate');received_status='absent_or_unparsed'
        if isinstance(received,str) and len(received)<=256:
            try:
                dt=datetime.fromisoformat(received.replace('Z','+00:00'))
                if dt.tzinfo is None:raise ValueError('timezone required')
                received_status='exporter_declared_internaldate'
            except ValueError:received=None
        else:received=None
        headers=js({'schema':'verified-eml-headers-v1','fields':fields,'source_original_sha256':value.eml_sha256,
            'message_id_status':'single_declared_header' if mid is not None else 'missing_or_ambiguous_header',
            'parser_defects':[type(d).__name__ for d in parsed.defects],
            'received_at_source':received_status,'provider_attested':False})
        return (value.message_id,account,scope.name,scope.uidvalidity,uid,mid,value.eml_sha256,headers,received,'configured_namespace_unattested')
    def _capture_verification(self,path,expected):
        with intake_folder.secure_open(path) as f:raw=f.read(1024*1024+1)
        if len(raw)>1024*1024 or hashlib.sha256(raw).hexdigest()!=expected:
            raise ValueError('verification_receipt_changed')
        root=self.output/'verification-receipts';root.mkdir(mode=0o700,exist_ok=True);private_path(root,True)
        destination=root/expected
        if not destination.exists():
            fd,temporary=tempfile.mkstemp(prefix='.receipt-',dir=root)
            try:
                with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
                os.chmod(temporary,0o400)
                try:os.link(temporary,destination,follow_symlinks=False)
                except FileExistsError:pass
                directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY);os.fsync(directory);os.close(directory)
            finally:Path(temporary).unlink(missing_ok=True)
        with intake_folder.secure_open(destination) as f:stored=f.read(1024*1024+1)
        if stored!=raw:raise ValueError('verification_receipt_conflict')
        return str(destination)
    def _validator(self,context):
        packet=context['receipt'];subject=packet['subject_sha256']
        if packet['stage']!='preserve':return False
        try:
            with self.store.ledger(self.database,readonly=True) as c:
                original=c.execute('SELECT bytes,storage_path FROM originals WHERE sha256=?',(subject,)).fetchone()
                occurrences=c.execute('SELECT id,kind,source_ref,parent_occurrence_id,evidence FROM occurrences WHERE original_sha256=? LIMIT 1025',(subject,)).fetchall()
            expected=str(self.output/'blobs'/subject)
            if not original or original['storage_path']!=expected or not 1<=len(occurrences)<=1024:return False
            # Streaming verification has no raw-content/4 MiB memory requirement.
            h=hashlib.sha256();length=0
            with intake_folder.secure_open(expected) as f:
                before=os.fstat(f.fileno())
                for block in iter(lambda:f.read(1024*1024),b''):
                    length+=len(block)
                    if length>original['bytes']:return False
                    h.update(block)
                after=os.fstat(f.fileno())
            fields=lambda st:(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
            if fields(before)!=fields(after) or fields(after)!=fields(Path(expected).stat(follow_symlinks=False)) or length!=original['bytes'] or h.hexdigest()!=subject:return False
            envelope=context.get('preservation_evidence')
            if context.get('content_kind')=='preservation_evidence':
                if not isinstance(envelope,dict) or envelope.get('original_sha256')!=subject or envelope.get('byte_length')!=length or envelope.get('storage_ref')!=expected:return False
                if set(envelope.get('occurrence_ids',[]))!={row['id'] for row in occurrences}:return False
            elif hashlib.sha256(context['content']).hexdigest()!=subject or len(context['content'])!=length:return False
            observed_hashes=set()
            for row in occurrences:
                if row['kind'] not in ('mail','attachment'):return False
                evidence=json.loads(row['evidence']);source=json.loads(row['source_ref'])
                path=evidence.get('verification_receipt_path');digest=evidence.get('receipt_sha256')
                if not isinstance(path,str) or path!=str(self.output/'verification-receipts'/digest):return False
                with intake_folder.secure_open(path) as f:raw=f.read(1024*1024+1)
                if len(raw)>1024*1024 or hashlib.sha256(raw).hexdigest()!=digest:return False
                # Reuse the existing exact MIME/native receipt binder idempotently.
                # This is verified acquisition replay, not a hash-only guess.
                scope=Folder(source['folder'],source['uidvalidity'])
                checked=LegacyIntakeBackend.preserve(self,path,source['account'],scope,source['uid'])
                if checked.receipt_sha256!=digest:return False
                if row['kind']=='mail':
                    if row['id']!=checked.message_id or source['mime_path'] is not None or checked.eml_sha256!=subject:return False
                else:
                    loc=source['mime_path']
                    if row['parent_occurrence_id']!=checked.message_id or row['id']!=hid(['attachment',checked.message_id,loc]) or (loc,subject) not in checked.attachments:return False
                observed_hashes.add(digest)
            if envelope is not None and envelope['verification_receipt_sha256'] not in observed_hashes:return False
            return True
        except (OSError,ValueError,KeyError,TypeError,RecursionError):return False
    def preserve(self,receipt_path,account,scope,uid):
        if self.run_identity is None:raise IntegrationGap('canonical_run_identity_missing')
        value=super().preserve(receipt_path,account,scope,uid)
        stamp=datetime.now(timezone.utc).isoformat();rid=self.run_identity['run_id']
        verification_path=self._capture_verification(receipt_path,value.receipt_sha256)
        records=[(value.message_id,value.eml_sha256,'mail',None,'message')]
        records.extend((hid(['attachment',value.message_id,loc]),sha,'attachment',value.message_id,loc) for loc,sha in value.attachments)
        mail_values=self._eml_projection(value,receipt_path,account,scope,uid)
        with self.store.ledger(self.database) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                mail_rows=c.execute('SELECT * FROM mail_messages WHERE id=? OR (account=? AND folder=? AND uidvalidity=? AND uid=?)',(value.message_id,account,scope.name,scope.uidvalidity,uid)).fetchall()
                if len(mail_rows)>1 or (mail_rows and tuple(mail_rows[0])!=mail_values):raise ValueError('canonical_mail_identity_conflict')
                run=c.execute('SELECT status,ended_at FROM runs WHERE run_id=?',(rid,)).fetchone()
                if not run or (not mail_rows and (run['status']!='running' or run['ended_at'] is not None)):raise ValueError('canonical_enrollment_run_not_live')
                for oid,sha,kind,parent,loc in records:
                    proof=intake_folder.verify_blob(sha,self.output);path=str(self.output/'blobs'/sha)
                    original=c.execute('SELECT bytes,storage_path,scope FROM originals WHERE sha256=?',(sha,)).fetchone()
                    if original and (original['bytes']!=proof['bytes'] or original['storage_path']!=path or original['scope']!='in_scope'):raise ValueError('canonical_original_binding_conflict')
                    if not original:
                        c.execute('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)',(sha,proof['bytes'],'message/rfc822' if kind=='mail' else None,stamp,'original','in_scope',path,'captured',None,js({'method':'verified_export_receipt','receipt_sha256':value.receipt_sha256})))
                        for stage in self.store.STAGES:c.execute("INSERT INTO stage_state VALUES(?,?,'pending',NULL,?,?,?,?)",(sha,stage,'runner',stamp,rid,'domain validation pending'))
                    source=js({'account':account,'folder':scope.name,'uidvalidity':scope.uidvalidity,'uid':uid,'mime_path':loc if kind=='attachment' else None})
                    evidence=js({'receipt_sha256':value.receipt_sha256,'cas_sha256':sha,'bytes':proof['bytes'],'export_receipt_path':str(Path(receipt_path).absolute()),'verification_receipt_path':verification_path})
                    old=c.execute('SELECT original_sha256,kind,source_ref,parent_occurrence_id,acquisition_method,evidence FROM occurrences WHERE id=?',(oid,)).fetchone()
                    expected=(sha,kind,source,parent,'verified_mail_export',evidence)
                    if old and tuple(old)!=expected:
                        prior=json.loads(old['evidence']);new=json.loads(evidence)
                        old_path=prior.pop('export_receipt_path',None);new.pop('export_receipt_path',None)
                        # Same receipt hash and content-addressed verification receipt are
                        # already proven above; the superseded export path may be pruned.
                        if tuple(old)[:5]!=expected[:5] or prior!=new or not isinstance(old_path,str):raise ValueError('canonical_occurrence_conflict')
                    if not old:c.execute('INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)',(oid,sha,kind,source,parent,stamp,'verified_mail_export',evidence))
                if not mail_rows:c.execute('INSERT INTO mail_messages VALUES(?,?,?,?,?,?,?,?,?,?)',mail_values)
                c.commit()
            except BaseException:c.rollback();raise
        if self.stage_runner is None:
            if self.stage_runner_factory is not None:
                self.stage_runner=self.stage_runner_factory(self.database,self.run_identity,self._validator)
            elif self.installed_preservation:
                self.stage_runner=installed_preservation_runner(self.stages,self.database,self.run_identity,self._validator)
            else:raise IntegrationGap('wp1_installed_preservation_adapter_missing')
        for sha in value.documents:
            proof=intake_folder.verify_blob(sha,self.output)
            with self.store.ledger(self.database,readonly=True) as c:
                original=c.execute('SELECT first_seen_at FROM originals WHERE sha256=?',(sha,)).fetchone()
                state=c.execute("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'",(sha,)).fetchone()
                ids=[row[0] for row in c.execute('SELECT id FROM occurrences WHERE original_sha256=? ORDER BY id LIMIT 1025',(sha,))]
            if len(ids)>1024:raise IntegrationGap('preservation_occurrence_bound')
            envelope={'schema':'preservation-evidence-v1','original_sha256':sha,'byte_length':proof['bytes'],
                'storage_ref':str(self.output/'blobs'/sha),'verification_receipt_sha256':value.receipt_sha256,'occurrence_ids':ids}
            context={'receipt':{'subject_sha256':sha,'stage':'preserve'},'content':js(envelope).encode(),
                'content_kind':'preservation_evidence','preservation_evidence':envelope}
            if state and state['status']=='done':
                if not self._validator(context):raise ValueError('preservation_replay_domain_failed')
                continue
            registered=self.stage_runner.set_preservation_evidence(sha,envelope,author_id='runner-preserver',tier='A')
            claim=self.stage_runner.claim(sha,'preserve',ttl_seconds=300)
            packet={'schema':'ledger-stage-receipt-v1','subject_sha256':sha,'stage':'preserve','content_sha256':registered['content_sha256'],
                    'author_id':'runner-preserver','tier':'A','reviewer_id':'runner-preserver','role':'preserver','verdict':'pass',
                    'coverage':{'denominator':{'kind':'bytes','total':proof['bytes']},'covered':proof['bytes'],'scope':'selected'},
                    'locators':['cas:'+sha],'rationale':'CAS hash/length and canonical mail occurrence verified',
                    'model_or_tool':'runner-cas-preserver-v3','created_at_tz':original['first_seen_at'],'input_hashes':{'original':sha}}
            self.stage_runner.promote(sha,'preserve',json.dumps(packet,sort_keys=True).encode(),claim_id=claim['claim_id'])
        return value
