"""Private durable outbox; installed production adapters intentionally absent."""
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from .backup import canonical,sha,write_new,new_directory,parent_fd,source_fd
from .review_gate import ReviewGateError, WP8ReviewGate

_TOKEN=object()
# Trusted installed code alone may supply registry entries in a reviewed release.
# No receipt/profile input can register callbacks or opt a test adapter into production.
_INSTALLED_APPROVAL_VERIFIERS={}
_INSTALLED_SITE_ADAPTERS={}
_INSTALLED_REVIEW_VERIFIERS={}
_INSTALLED_ROLLBACK_VERIFIERS={}


class PublicationError(ValueError):pass


def require(value,reason):
    if not value:raise PublicationError(reason)


def identity(value):
    require(isinstance(value,str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}",value),"invalid_identity")
    return value


def binding(proposal_id,content_hash,review_hash,supersedes):
    identity(proposal_id)
    if supersedes is not None:identity(supersedes)
    require(supersedes!=proposal_id,"self_supersession")
    return {"proposal_id":proposal_id,"public_content_sha256":content_hash,
            "review_bundle_sha256":review_hash,"supersedes":supersedes}


def atomic_public_artifact(destination,raw,private_root):
    """Expose only a complete fsynced artifact; never replace committed bytes.

    Incomplete scratch files are private siblings of the database, not site
    assets. A process crash may retain one; a retry uses a fresh exclusive name.
    """
    scratch=private_root/('.prepare-'+os.urandom(16).hex())
    with parent_fd(scratch) as (scratch_parent,scratch_name),parent_fd(destination) as (parent,name):
        fd=os.open(scratch_name,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600,dir_fd=scratch_parent)
        try:
            offset=0
            while offset<len(raw):
                written=os.write(fd,raw[offset:])
                require(written>0,'public_artifact_short_write')
                offset+=written
            os.fchmod(fd,0o400);os.fsync(fd)
            # link is atomic and fails if destination already exists, including
            # a dangling symlink. Never replace an existing artifact.
            os.link(scratch_name,name,src_dir_fd=scratch_parent,dst_dir_fd=parent,follow_symlinks=False)
            os.fsync(parent)
        finally:
            os.close(fd)
            os.unlink(scratch_name,dir_fd=scratch_parent)
            os.fsync(scratch_parent)


def validate_receipt(receipt,*,job,key,action,runtime):
    require(isinstance(receipt,dict) and receipt.get('content_sha256')==job['public_content_sha256'] and
            receipt.get('review_bundle_sha256')==job['review_bundle_sha256'] and receipt.get('idempotency_key')==key and receipt.get('action')==action and
            isinstance(receipt.get('reference'),str) and 0<len(receipt['reference'])<=2048 and
            receipt.get('test_only') is runtime.test_only and receipt.get('production') is (not runtime.test_only),'adapter_receipt_binding')


class SyntheticApprovalAuthority:
    """Local fixture signer. Never accepted by an installed production runtime."""
    def __init__(self,owner="synthetic-owner"):
        self.owner=identity(owner);self._key=os.urandom(32)
    def approve(self,proposal_id,content,review_bundle,*,supersedes=None):
        result={"schema":"synthetic-owner-approval-v1",**binding(proposal_id,sha(content),sha(review_bundle),supersedes),
                "owner_identity":self.owner,"test_only":True}
        result["signature"]=hmac.new(self._key,canonical(result),hashlib.sha256).hexdigest()
        return result
    def verify(self,approval,expected):
        if not isinstance(approval,dict):return False
        body=dict(approval);signature=body.pop("signature",None)
        required={"schema":"synthetic-owner-approval-v1",**expected,"owner_identity":self.owner,"test_only":True}
        return body==required and isinstance(signature,str) and hmac.compare_digest(
            signature,hmac.new(self._key,canonical(body),hashlib.sha256).hexdigest())


class SyntheticRollbackAuthority:
    """Separate explicit fixture-only withdrawal approval, never deployment approval."""
    def __init__(self,owner="synthetic-owner"):
        self.owner=identity(owner);self._key=os.urandom(32)
    def approve(self,target):
        body={"schema":"synthetic-withdrawal-approval-v1","owner_identity":self.owner,
              "test_only":True,"target":target}
        return {**body,"signature":hmac.new(self._key,canonical(body),hashlib.sha256).hexdigest()}
    def verify(self,approval,target):
        if not isinstance(approval,dict):return False
        body=dict(approval);signature=body.pop("signature",None)
        expected={"schema":"synthetic-withdrawal-approval-v1","owner_identity":self.owner,
                  "test_only":True,"target":target}
        return body==expected and isinstance(signature,str) and hmac.compare_digest(
            signature,hmac.new(self._key,canonical(body),hashlib.sha256).hexdigest())


class FakeSiteAdapter:
    """No I/O. Deterministic idempotency lookup simulates prepare/deploy/rollback."""
    def __init__(self):
        self.results={};self.calls=[];self.active_version=None;self.deployments={};self.versions={}
    def perform(self,action,key,*,content,job):
        if key in self.results:return self.results[key]
        require(action in ("prepare","deploy","rollback"),"invalid_action")
        result={"action":action,"idempotency_key":key,"content_sha256":sha(content),
                "review_bundle_sha256":job["review_bundle_sha256"],"test_only":True,
                "production":False,"reference":"synthetic:"+action+":"+key}
        if action=="deploy":
            version="synthetic:version:"+key
            self.deployments[job['proposal_id']]=(version,self.active_version)
            result.update(deployed_version=version,previous_version=self.active_version,
                          previous_content_sha256=sha(self.versions[self.active_version]) if self.active_version is not None else None)
            self.versions[version]=content;self.active_version=version
        elif action=="rollback":
            require(job['proposal_id'] in self.deployments,"synthetic_deployment_missing")
            version,previous=self.deployments[job['proposal_id']]
            require(self.active_version==version,"rollback_would_replace_newer_deployment")
            target=job.get('withdrawal_target')
            require(isinstance(target,dict) and target.get('deployed_version')==version and
                    target.get('rollback_reference')==previous and target.get('previous_public_content_sha256')==
                    (sha(self.versions[previous]) if previous is not None else None),'rollback_target_mismatch')
            result.update(rollback_ref=version,restored_version=previous,
                          restored_content_sha256=target['previous_public_content_sha256'])
            self.active_version=previous
        self.results[key]=result;self.calls.append((action,key))
        return result


class PublicationRuntime:
    def __init__(self,token,*,owner,profile,verifier,adapter,test_only,review_verifier=None,rollback_verifier=None):
        require(token is _TOKEN,"trusted_runtime_factory_required")
        self.owner=identity(owner);self.profile=identity(profile)
        self.verifier=verifier;self.adapter=adapter;self.test_only=test_only
        self.review_verifier=review_verifier;self.rollback_verifier=rollback_verifier


def testing_runtime(authority,adapter=None,*,review_verifier=None,rollback_authority=None):
    require(type(authority) is SyntheticApprovalAuthority,"synthetic_authority_required")
    require(adapter is None or type(adapter) is FakeSiteAdapter,"synthetic_adapter_required")
    require(review_verifier is None or getattr(review_verifier,'test_only',None) is True,'synthetic_review_gate_required')
    require(rollback_authority is None or (type(rollback_authority) is SyntheticRollbackAuthority and rollback_authority.owner==authority.owner),'synthetic_rollback_authority_required')
    return PublicationRuntime(_TOKEN,owner=authority.owner,profile="synthetic-only-v1",
                              verifier=authority,adapter=adapter or FakeSiteAdapter(),test_only=True,
                              review_verifier=review_verifier,rollback_verifier=rollback_authority)


def installed_runtime(*,owner,profile,approval_verifier_id,site_adapter_id,review_verifier_id=None,rollback_verifier_id=None):
    """Trusted startup selects installed IDs, never receipt-supplied callbacks."""
    identity(approval_verifier_id);identity(site_adapter_id)
    return PublicationRuntime(_TOKEN,owner=owner,profile=profile,
        verifier=_INSTALLED_APPROVAL_VERIFIERS.get(approval_verifier_id),
        adapter=_INSTALLED_SITE_ADAPTERS.get(site_adapter_id),test_only=False,
        review_verifier=_INSTALLED_REVIEW_VERIFIERS.get(review_verifier_id),
        rollback_verifier=_INSTALLED_ROLLBACK_VERIFIERS.get(rollback_verifier_id))


def configure_wp8_review_gate(verifier_id,*,root,authority):
    """Trusted startup only; never expose registration through receipt arguments."""
    identity(verifier_id)
    require(verifier_id not in _INSTALLED_REVIEW_VERIFIERS,'review_profile_already_configured')
    gate=WP8ReviewGate(root=root,authority=authority)
    _INSTALLED_REVIEW_VERIFIERS[verifier_id]=gate
    return gate


SCHEMA="""
CREATE TABLE IF NOT EXISTS publication_jobs(
 proposal_id TEXT PRIMARY KEY,public_content_sha256 TEXT NOT NULL,review_bundle_sha256 TEXT NOT NULL,
 content BLOB NOT NULL,review_bundle BLOB NOT NULL,approval_json TEXT NOT NULL,supersedes TEXT,
 owner_identity TEXT NOT NULL,profile TEXT NOT NULL,test_only INTEGER NOT NULL,
 state TEXT NOT NULL,blocked_reason TEXT);
CREATE TABLE IF NOT EXISTS publication_actions(
 action_key TEXT PRIMARY KEY,proposal_id TEXT NOT NULL REFERENCES publication_jobs(proposal_id),
 action TEXT NOT NULL,state TEXT NOT NULL,receipt_json TEXT);
CREATE TABLE IF NOT EXISTS publication_events(
 sequence INTEGER PRIMARY KEY,proposal_id TEXT NOT NULL REFERENCES publication_jobs(proposal_id),
 action TEXT NOT NULL,result TEXT NOT NULL,reason TEXT,receipt_sha256 TEXT);
CREATE TRIGGER IF NOT EXISTS publication_history_insert BEFORE INSERT ON publication_events
 WHEN EXISTS(SELECT 1 FROM publication_events WHERE sequence=NEW.sequence)
 BEGIN SELECT RAISE(ABORT,'publication history cannot be replaced');END;
CREATE TRIGGER IF NOT EXISTS publication_history_update BEFORE UPDATE ON publication_events
 BEGIN SELECT RAISE(ABORT,'publication history immutable');END;
CREATE TRIGGER IF NOT EXISTS publication_history_delete BEFORE DELETE ON publication_events
 BEGIN SELECT RAISE(ABORT,'publication history immutable');END;
"""


class PublicationOutbox:
    def __init__(self,root):
        self.root=Path(os.path.abspath(root))
        if not self.root.exists():new_directory(self.root)
        with parent_fd(self.root) as (parent,name):
            info=os.stat(name,dir_fd=parent,follow_symlinks=False)
            require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.geteuid() and not info.st_mode&0o077,"outbox_not_private")
        self.site=self.root/'site'
        if not self.site.exists():new_directory(self.site)
        with parent_fd(self.site) as (parent,name):
            info=os.stat(name,dir_fd=parent,follow_symlinks=False)
            require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.geteuid() and not info.st_mode&0o077,"outbox_not_private")
        self.database=self.root/'outbox.sqlite'
        with parent_fd(self.database) as (parent,name):
            fd=os.open(name,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=parent)
            try:
                info=os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.geteuid() and not info.st_mode&0o077,"outbox_database_not_private")
            finally:os.close(fd)
        self.db=sqlite3.connect(self.database,timeout=2);self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON');self.db.execute('PRAGMA synchronous=FULL')
        with self.lock():self.db.executescript(SCHEMA)
    def close(self):self.db.close()
    @contextmanager
    def lock(self):
        with parent_fd(self.root/'.lock') as (parent,name):
            fd=os.open(name,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=parent)
            try:
                info=os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.geteuid() and not info.st_mode&0o077,"outbox_lock_not_private")
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise PublicationError('outbox_busy') from None
                yield
            finally:os.close(fd)
    def _event(self,proposal,action,result,reason=None,receipt=None):
        self.db.execute('INSERT INTO publication_events(proposal_id,action,result,reason,receipt_sha256) VALUES(?,?,?,?,?)',
                        (proposal,action,result,reason,sha(canonical(receipt)) if receipt else None))
    def _authority(self,runtime,job):
        require(type(runtime) is PublicationRuntime,"trusted_runtime_required")
        require(job['owner_identity']==runtime.owner and job['profile']==runtime.profile and bool(job['test_only'])==runtime.test_only,"runtime_binding_mismatch")
        if runtime.verifier is None:return 'approval_verifier_unconfigured'
        if runtime.adapter is None:return 'site_adapter_unconfigured'
        if not runtime.test_only and (type(runtime.verifier) is SyntheticApprovalAuthority or type(runtime.adapter) is FakeSiteAdapter):return 'synthetic_adapter_not_production'
        if runtime.review_verifier is None:return 'review_gate_unconfigured'
        if not runtime.test_only and (type(runtime.review_verifier) is not WP8ReviewGate or runtime.review_verifier.test_only):return 'review_gate_not_production'
        expected=binding(job['proposal_id'],job['public_content_sha256'],job['review_bundle_sha256'],job['supersedes'])
        if runtime.verifier.verify(json.loads(job['approval_json']),expected) is not True:return 'owner_approval_invalid'
        return None
    def stage(self,runtime,*,proposal_id,content,review_bundle,approval,supersedes=None):
        require(type(runtime) is PublicationRuntime,"trusted_runtime_required")
        require(isinstance(content,bytes) and 0<len(content)<=1024*1024,"public_content_bound")
        require(isinstance(review_bundle,bytes) and 0<len(review_bundle)<=256*1024,"review_bundle_bound")
        require(isinstance(approval,dict) and len(canonical(approval))<=65536,"approval_bound")
        expected=binding(proposal_id,sha(content),sha(review_bundle),supersedes)
        proposed={**expected,'content':content,'review_bundle':review_bundle,'approval_json':canonical(approval).decode(),
                  'owner_identity':runtime.owner,'profile':runtime.profile,'test_only':int(runtime.test_only)}
        with self.lock(),self.db,ExitStack() as review_lock:
            old=self.db.execute('SELECT * FROM publication_jobs WHERE proposal_id=?',(proposal_id,)).fetchone()
            if old:
                require(all(old[k]==v for k,v in proposed.items()),'proposal_immutable_use_supersession')
                return dict(old)
            if supersedes is not None:
                require(self.db.execute('SELECT 1 FROM publication_jobs WHERE proposal_id=?',(supersedes,)).fetchone(),'superseded_proposal_missing')
            reason=self._authority(runtime,proposed)
            if reason is None:
                try:review_lock.enter_context(runtime.review_verifier.assess(proposed,owner=runtime.owner,test_only=runtime.test_only))
                except ReviewGateError as exc:reason=str(exc)
            state='blocked' if reason else 'staged'
            columns=list(proposed)+['state','blocked_reason']
            self.db.execute('INSERT INTO publication_jobs('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',
                            list(proposed.values())+[state,reason])
            self._event(proposal_id,'stage',state,reason)
            return {'proposal_id':proposal_id,'state':state,'blocked_reason':reason,'test_only':runtime.test_only,'production':False}
    def _phase(self,job):
        # Recover legacy blocked projections from retained successful events.
        row=self.db.execute("SELECT action FROM publication_events WHERE proposal_id=? AND result='complete' ORDER BY sequence DESC LIMIT 1",
                            (job['proposal_id'],)).fetchone()
        return {'prepare':'prepared','deploy':'deployed','rollback':'rolled_back'}[row['action']] if row else job['state']
    def _block(self,job,action,reason):
        with self.db:
            self.db.execute("UPDATE publication_jobs SET state=?,blocked_reason=? WHERE proposal_id=?",
                            (self._phase(job),reason,job['proposal_id']))
            self._event(job['proposal_id'],action,'blocked',reason)
        return {'state':'blocked','last_successful_phase':self._phase(job),'reason':reason,'production':False}
    def _deployment(self,runtime,proposal_id):
        row=self.db.execute("SELECT * FROM publication_jobs WHERE proposal_id=?",(proposal_id,)).fetchone()
        require(row is not None,'historical_approved_proposal_missing');job=dict(row)
        require(job['owner_identity']==runtime.owner and job['profile']==runtime.profile and
                bool(job['test_only'])==runtime.test_only,'historical_runtime_binding_mismatch')
        require(sha(job['content'])==job['public_content_sha256'] and
                sha(job['review_bundle'])==job['review_bundle_sha256'],'historical_approved_bytes_changed')
        key=sha(canonical({**binding(proposal_id,job['public_content_sha256'],job['review_bundle_sha256'],job['supersedes']),
                           'action':'deploy','profile':runtime.profile,'test_only':runtime.test_only}))
        row=self.db.execute("SELECT * FROM publication_actions WHERE action_key=? AND state='complete'",(key,)).fetchone()
        require(row is not None,'completed_deployment_required')
        receipt=json.loads(row['receipt_json'])
        validate_receipt(receipt,job=job,key=key,action='deploy',runtime=runtime)
        require(self.db.execute("SELECT 1 FROM publication_events WHERE proposal_id=? AND action='deploy' AND result='complete' AND receipt_sha256=?",
                                (proposal_id,sha(canonical(receipt)))).fetchone() is not None,'deployment_receipt_history_mismatch')
        version=receipt.get('deployed_version');previous=receipt.get('previous_version')
        require(isinstance(version,str) and 0<len(version)<=2048 and
                (previous is None or isinstance(previous,str) and 0<len(previous)<=2048),'deployment_version_required')
        return job,receipt
    def _withdrawal_target(self,runtime,proposal_id):
        job,deployed=self._deployment(runtime,proposal_id)
        previous=deployed['previous_version'];previous_job=None;previous_receipt=None
        if previous is not None:
            rows=self.db.execute("SELECT proposal_id FROM publication_actions WHERE action='deploy' AND state='complete' AND json_extract(receipt_json,'$.deployed_version')=? LIMIT 2",
                                 (previous,)).fetchall()
            require(len(rows)==1,'previous_approved_deployment_missing_or_ambiguous')
            previous_job,previous_receipt=self._deployment(runtime,rows[0]['proposal_id'])
            require(previous_job['proposal_id']!=proposal_id and
                    deployed.get('previous_content_sha256')==previous_job['public_content_sha256'],'previous_approved_content_mismatch')
        else:
            require(deployed.get('previous_content_sha256') is None,'empty_rollback_target_mismatch')
        return {'schema':'publication-withdrawal-target-v1','proposal_id':proposal_id,
                'owner_identity':runtime.owner,'profile':runtime.profile,'test_only':runtime.test_only,
                'deployed_version':deployed['deployed_version'],'deployed_public_content_sha256':job['public_content_sha256'],
                'deployed_review_bundle_sha256':job['review_bundle_sha256'],'deployment_receipt_sha256':sha(canonical(deployed)),
                'rollback_reference':previous,
                'previous_proposal_id':previous_job['proposal_id'] if previous_job else None,
                'previous_public_content_sha256':previous_job['public_content_sha256'] if previous_job else None,
                'previous_review_bundle_sha256':previous_job['review_bundle_sha256'] if previous_job else None,
                'previous_deployment_receipt_sha256':sha(canonical(previous_receipt)) if previous_receipt else None}
    def rollback_target(self,runtime,proposal_id):
        """Read-only target for a separate explicit trusted-owner authorization.

        Historical successful approval does not assert disputed content is valid.
        The adapter must still compare the live version before any withdrawal.
        """
        require(type(runtime) is PublicationRuntime,'trusted_runtime_required');identity(proposal_id)
        with self.lock():return self._withdrawal_target(runtime,proposal_id)
    def _withdrawal_authority(self,runtime,job,authorization,target):
        require(type(runtime) is PublicationRuntime,'trusted_runtime_required')
        require(job['owner_identity']==runtime.owner and job['profile']==runtime.profile and
                bool(job['test_only'])==runtime.test_only,'runtime_binding_mismatch')
        if runtime.adapter is None:return 'site_adapter_unconfigured'
        if runtime.rollback_verifier is None:return 'rollback_authority_unconfigured'
        if not runtime.test_only and (type(runtime.rollback_verifier) is SyntheticRollbackAuthority or type(runtime.adapter) is FakeSiteAdapter):
            return 'synthetic_rollback_not_production'
        if authorization is None:return 'explicit_rollback_authorization_required'
        if not isinstance(authorization,dict) or len(canonical(authorization))>65536:return 'rollback_authorization_bound'
        if runtime.rollback_verifier.verify(authorization,target) is not True:return 'rollback_authorization_invalid'
        return None
    def execute(self,runtime,proposal_id,action,*,fault=None,rollback_authorization=None):
        identity(proposal_id);require(action in ('prepare','deploy','rollback'),'invalid_action')
        require(fault is None or (type(runtime) is PublicationRuntime and runtime.test_only),'test_fault_only')
        with self.lock(),ExitStack() as review_lock:
            row=self.db.execute('SELECT * FROM publication_jobs WHERE proposal_id=?',(proposal_id,)).fetchone()
            require(row is not None,'proposal_missing');job=dict(row)
            require(sha(job['content'])==job['public_content_sha256'] and sha(job['review_bundle'])==job['review_bundle_sha256'],'stored_artifact_changed')
            target=None
            if action=='rollback':
                # Withdrawal of disputed content is NOT a new publication claim.
                # It needs separate explicit owner authority and historical bytes,
                # not an assertion that the disputed reviews are still valid.
                target=self._withdrawal_target(runtime,proposal_id)
                reason=self._withdrawal_authority(runtime,job,rollback_authorization,target)
            else:
                reason=self._authority(runtime,job)
                if reason is None:
                    try:review_lock.enter_context(runtime.review_verifier.assess(job,owner=runtime.owner,test_only=runtime.test_only))
                    except ReviewGateError as exc:reason=str(exc)
            if reason:return self._block(job,action,reason)
            key=sha(canonical({**binding(proposal_id,job['public_content_sha256'],job['review_bundle_sha256'],job['supersedes']),
                               'action':action,'profile':runtime.profile,'test_only':runtime.test_only}))
            previous=self.db.execute('SELECT * FROM publication_actions WHERE action_key=?',(key,)).fetchone()
            completed=previous is not None and previous['state']=='complete'
            if not completed:
                expected={'prepare':'staged','deploy':'prepared','rollback':'deployed'}[action]
                require(self._phase(job)==expected,'publication_action_order')
            if action=='rollback':
                # A damaged prepared file must not prevent withdrawing the exact
                # historically deployed version; immutable job bytes are checked.
                raw=job['content']
            else:
                suffix='.json' if type(runtime.review_verifier) is WP8ReviewGate else '.md'
                destination=self.site/(proposal_id+suffix)
                if action=='prepare' and not completed and not os.path.lexists(destination):
                    atomic_public_artifact(destination,job['content'],self.root)
                with source_fd(destination) as (fd,info):raw=os.read(fd,1024*1024+1)
                require(raw==job['content'],'staged_public_content_changed')
            if completed:
                receipt=json.loads(previous['receipt_json'])
                validate_receipt(receipt,job=job,key=key,action=action,runtime=runtime)
                if action=='rollback':
                    require(receipt.get('withdrawal_target_sha256')==sha(canonical(target)) and
                            receipt.get('rollback_ref')==target['deployed_version'] and
                            receipt.get('restored_version')==target['rollback_reference'] and
                            receipt.get('restored_content_sha256')==target['previous_public_content_sha256'],'rollback_receipt_binding')
                return receipt
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO publication_actions VALUES(?,?,?,'pending',NULL)",(key,proposal_id,action))
                self._event(proposal_id,action,'attempted')
            # Adapters receive only content and public hash/identity bindings, not
            # private review descriptors or owner authorization payloads.
            adapter_job=binding(proposal_id,job['public_content_sha256'],job['review_bundle_sha256'],job['supersedes'])
            if target is not None:adapter_job['withdrawal_target']=target
            try:receipt=runtime.adapter.perform(action,key,content=raw,job=adapter_job)
            except PublicationError as exc:
                if action=='rollback' and str(exc)=='rollback_would_replace_newer_deployment':
                    return self._block(job,action,str(exc))
                raise
            validate_receipt(receipt,job=job,key=key,action=action,runtime=runtime)
            if action=='rollback':
                require(receipt.get('rollback_ref')==target['deployed_version'] and
                        receipt.get('restored_version')==target['rollback_reference'] and
                        receipt.get('restored_content_sha256')==target['previous_public_content_sha256'],'rollback_receipt_binding')
                receipt={**receipt,'withdrawal_target_sha256':sha(canonical(target)),
                         'withdrawal_authorization_sha256':sha(canonical(rollback_authorization))}
            if fault:fault('after_adapter_before_ack')
            state={'prepare':'prepared','deploy':'deployed','rollback':'rolled_back'}[action]
            with self.db:
                self.db.execute("UPDATE publication_actions SET state='complete',receipt_json=? WHERE action_key=?",(canonical(receipt).decode(),key))
                self.db.execute('UPDATE publication_jobs SET state=?,blocked_reason=NULL WHERE proposal_id=?',(state,proposal_id))
                self._event(proposal_id,action,'complete',receipt=receipt)
            return receipt
    def status(self):
        return [dict(row) for row in self.db.execute('SELECT proposal_id,public_content_sha256,review_bundle_sha256,supersedes,state,blocked_reason,test_only FROM publication_jobs ORDER BY proposal_id')]
