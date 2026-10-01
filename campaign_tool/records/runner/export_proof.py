"""Owner-configured exporter wrapper and immutable, reconciled export snapshots.

No provider implementation or credential loading. Command invocation is explicit,
shell=False, output-discarding and bounded. Record content never selects argv.
"""
from contextlib import contextmanager
import csv
from datetime import datetime
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import time

from .core import private_path,hash_ok,js,hid
from .contracts import Folder,IntegrationGap
from campaign_tool.records.intake import mail_delta,folder as intake_folder

MAX_INDEX=16*1024*1024
MAX_RECEIPTS=2000
MAX_SNAPSHOT=64*1024*1024
MAX_ROWS=10000

class ExportBlocked(IntegrationGap):pass

def timestamp(value):
    if not isinstance(value,str) or len(value)>80:raise ExportBlocked('export_cutoff_invalid')
    try:
        dt=datetime.fromisoformat(value.replace('Z','+00:00'))
        if dt.tzinfo is None:raise ValueError('offset missing')
        return dt.timestamp()
    except ValueError:raise ExportBlocked('export_cutoff_invalid') from None

def read_exact(path,limit):
    with intake_folder.secure_open(path) as f:
        before=os.fstat(f.fileno())
        if before.st_size>limit:raise ExportBlocked('export_input_bound')
        raw=f.read(limit+1);after=os.fstat(f.fileno())
    fields=lambda x:(x.st_dev,x.st_ino,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
    current=Path(path).stat(follow_symlinks=False)
    if len(raw)>limit or fields(before)!=fields(after) or fields(after)!=fields(current):raise ExportBlocked('export_input_changed')
    return raw,fields(after)

def rooted(root,literal):
    # Explicit configured path, never a record-selected executable.
    return mail_delta.source_path(root,literal,root/'__unused_output__')

def file_digest(path):
    h=hashlib.sha256()
    with intake_folder.secure_open(path) as f:
        before=os.fstat(f.fileno())
        if before.st_size>128*1024*1024:raise ExportBlocked('command_input_bound')
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
        after=os.fstat(f.fileno())
    if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ExportBlocked('command_input_changed')
    return h.hexdigest()

@contextmanager
def export_lock(path):
    path=private_path(path)
    fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        original=os.fstat(fd)
        yield
        current=path.stat(follow_symlinks=False)
        if (original.st_dev,original.st_ino)!=(current.st_dev,current.st_ino):raise ExportBlocked('legacy_export_lock_changed')
    except BlockingIOError:raise ExportBlocked('legacy_export_lock_busy') from None
    finally:os.close(fd)

def validate_spec(spec):
    required={'argv','argv_sha256','command_inputs','root','status_path','state_path','receipt_dir','legacy_lock_path','indexes','folder_fields'}
    if not isinstance(spec,dict) or not required<=set(spec):raise ExportBlocked('export_profile_incomplete')
    argv=spec['argv']
    if not isinstance(argv,list) or not 1<=len(argv)<=32 or any(not isinstance(s,str) or not s or len(s)>4096 or '\x00' in s for s in argv) or not Path(argv[0]).is_absolute():raise ExportBlocked('export_argv_invalid')
    if spec['argv_sha256']!=hid(argv):raise ExportBlocked('export_argv_pin_mismatch')
    pins=spec['command_inputs']
    if not isinstance(pins,dict) or not 1<=len(pins)<=16 or argv[0] not in pins or any(not Path(p).is_absolute() or not hash_ok(h) for p,h in pins.items()):raise ExportBlocked('command_pins_incomplete')
    indexes=spec['indexes']
    if not isinstance(indexes,dict) or set(indexes)!={'messages','attachments'}:raise ExportBlocked('index_contract_missing')
    for kind,item in indexes.items():
        required_fields={'account_id','folder','uidvalidity','uid','sha256'}|({'part'} if kind=='attachments' else set())
        if not isinstance(item,dict) or item.get('format') not in ('csv','jsonl') or not isinstance(item.get('fields'),dict) or not required_fields<=set(item['fields']) or any(not isinstance(x,str) or not x for x in item['fields'].values()):raise ExportBlocked('index_schema_unresolved')
    if set(spec['folder_fields'])!={'folder','uidvalidity','messages'}:raise ExportBlocked('folder_schema_unresolved')
    if spec.get('attachment_part_encoding')!='exporter_receipt_part':raise ExportBlocked('attachment_part_encoding_unresolved')
    timeout=spec.get('timeout_seconds',300)
    if type(timeout) is not int or not 1<=timeout<=1800:raise ExportBlocked('export_timeout_invalid')
    env=spec.get('environment_names',['PATH','HOME','LANG','SSL_CERT_FILE','SSL_CERT_DIR'])
    if not isinstance(env,list) or len(env)>64 or any(not isinstance(x,str) or not x or len(x)>128 for x in env):raise ExportBlocked('export_environment_invalid')
    return timeout,env

def execute(spec):
    timeout,env_names=validate_spec(spec)
    for path,expected in spec['command_inputs'].items():
        if file_digest(path)!=expected:raise ExportBlocked('command_input_pin_mismatch')
    process=subprocess.Popen(spec['argv'],shell=False,cwd=spec['root'],
        env={name:os.environ[name] for name in env_names if name in os.environ},
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        start_new_session=True,close_fds=True)
    try:
        result=process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGKILL);process.wait()
        raise ExportBlocked('export_command_timeout') from None
    if result!=0:raise ExportBlocked('export_command_failed')
    # Detect a changed executable/script even when execution itself returned zero.
    for path,expected in spec['command_inputs'].items():
        if file_digest(path)!=expected:raise ExportBlocked('command_input_changed')

def decode(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate JSON key')
            result[key]=value
        return result
    def constant(_):raise ValueError('nonfinite JSON')
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=constant)

def rows(raw,spec):
    try:
        text=raw.decode('utf-8')
        source=csv.DictReader(io.StringIO(text)) if spec['format']=='csv' else (decode(x) for x in text.splitlines() if x.strip())
        result=[]
        for row in source:
            if len(result)>=MAX_ROWS or not isinstance(row,dict):raise ExportBlocked('index_row_bound')
            result.append(row)
        return result
    except (ValueError,UnicodeError,csv.Error,RecursionError):raise ExportBlocked('index_parse_failed') from None

def mapped(row,fields):
    try:return {role:row[column] for role,column in fields.items()}
    except KeyError:raise ExportBlocked('index_schema_unresolved') from None

def number(v):
    if type(v) is int and 0<v<2**63:return v
    if isinstance(v,str) and v.isascii() and v.isdigit() and 0<int(v)<2**63:return int(v)
    raise ExportBlocked('export_identity_invalid')

def identity(row):
    if not isinstance(row['account_id'],str) or not isinstance(row['folder'],str):raise ExportBlocked('export_identity_invalid')
    return (row['account_id'],row['folder'],number(row['uidvalidity']),number(row['uid']))

class ExporterWrapper:
    """Execute only a trusted configured command, then expose frozen receipts.

    Production command execution has not been authorized by this implementation;
    the test suite supplies a local fixture script with no sockets/provider calls.
    """
    def __init__(self,control_root,spec,account):
        self.control=private_path(control_root,True);self.spec=spec;self.account=account
        self.root=private_path(spec['root'],True);self.prepared=False
    def prepare(self,run_identity,started_at,clock=time.time):
        if run_identity.get('export_spec_sha256') != hid(self.spec):
            raise ExportBlocked('export_trusted_profile_mismatch')
        execute(self.spec);finished=clock()
        with export_lock(self.spec['legacy_lock_path']):
            status_path=rooted(self.root,self.spec['status_path']);state_path=rooted(self.root,self.spec['state_path'])
            status_raw,status_stat=read_exact(status_path,1024*1024);state_raw,state_stat=read_exact(state_path,1024*1024)
            try:status=decode(status_raw);state=decode(state_raw)
            except (ValueError,RecursionError):raise ExportBlocked('export_status_invalid') from None
            if not isinstance(status,dict) or not isinstance(state,dict):raise ExportBlocked('export_status_invalid')
            if any(type(status.get(key)) is not int or status[key] < 0 for key in ('messages','attachments')):
                raise ExportBlocked('export_status_counts_invalid')
            cutoff=timestamp(status.get('time_utc'));last_success=timestamp(state.get('last_success'))
            if not isinstance(status.get('failures'),list) or status['failures']:raise ExportBlocked('export_failed_coverage')
            if not started_at<=cutoff<=finished+1 or not cutoff<=last_success<=finished+1:raise ExportBlocked('export_status_stale_or_future')
            scopes={};scope_counts={};ff=self.spec['folder_fields']
            if not isinstance(status.get('folders'),list) or len(status['folders'])>100:raise ExportBlocked('folder_coverage_invalid')
            for item in status['folders']:
                try:name=item[ff['folder']];valid=number(item[ff['uidvalidity']]);count=item[ff['messages']]
                except (KeyError,TypeError):raise ExportBlocked('folder_schema_unresolved') from None
                if not isinstance(name,str) or not name or name in scopes or type(count) is not int or count<0:raise ExportBlocked('folder_coverage_invalid')
                scopes[name]=valid;scope_counts[name]=count
            captures={'status.json':status_raw,'state.json':state_raw};source_meta=[(status_path,status_stat),(state_path,state_stat)]
            index_meta={}
            for kind,index in self.spec['indexes'].items():
                path=rooted(self.root,index['path']);raw,meta=read_exact(path,MAX_INDEX)
                captures[kind+'.index']=raw;source_meta.append((path,meta));index_meta[kind]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'rows':rows(raw,index)}
            receipt_dir=rooted(self.root,self.spec['receipt_dir'])
            if not receipt_dir.is_dir() or receipt_dir.is_symlink():raise ExportBlocked('receipt_directory_invalid')
            # No recursive traversal or filename-derived identity.
            paths=[];directory_items=0
            for path in receipt_dir.iterdir():
                directory_items+=1
                if directory_items>MAX_ROWS:raise ExportBlocked('receipt_directory_item_bound')
                if path.suffix=='.json':paths.append(path)
                if len(paths)>MAX_RECEIPTS:raise ExportBlocked('receipt_count_bound')
            messages={};attachments={};receipt_files={};excluded=0;total=sum(map(len,captures.values()))
            for path in sorted(paths):
                raw,meta=read_exact(path,mail_delta.MAX_RECEIPT);source_meta.append((path,meta))
                total+=len(raw)
                if total>MAX_SNAPSHOT:raise ExportBlocked('snapshot_byte_bound')
                receipt,scope,items=mail_delta.load_receipt(path)
                # Freeze the bytes actually parsed, not a later version of the receipt.
                if decode(raw)!=receipt:raise ExportBlocked('receipt_changed_during_capture')
                key=(scope[0],scope[1],number(scope[2]),number(scope[3]))
                sha=hashlib.sha256(raw).hexdigest();dest='receipts/'+sha+'.json';captures[dest]=raw
                if key[0]!=self.account or scopes.get(key[1])!=key[2]:excluded+=1;continue
                if key in messages:raise ExportBlocked('duplicate_receipt_identity')
                messages[key]=items[0]['sha'];receipt_files[key]=dest
                for item in items[1:]:
                    part=str(item.get('part_value',item['part']));akey=key+(part,)
                    if akey in attachments:raise ExportBlocked('duplicate_attachment_identity')
                    attachments[akey]=item['sha']
            actual_messages={};actual_attachments={}
            for kind,target in (('messages',actual_messages),('attachments',actual_attachments)):
                for row in index_meta[kind]['rows']:
                    item=mapped(row,self.spec['indexes'][kind]['fields']);key=identity(item)
                    if kind=='attachments':key+=(str(item['part']),)
                    if key in target or not hash_ok(item['sha256']):raise ExportBlocked('index_identity_invalid')
                    target[key]=item['sha256']
            if messages!=actual_messages or attachments!=actual_attachments:raise ExportBlocked('export_index_receipt_gap')
            if any(sum(1 for key in messages if key[1]==name)!=count for name,count in scope_counts.items()) or status.get('messages')!=len(messages) or status.get('attachments')!=len(attachments):raise ExportBlocked('export_status_count_gap')
            for path,expected in source_meta:
                current=path.stat(follow_symlinks=False)
                observed=(current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns,current.st_ctime_ns)
                if observed!=expected:raise ExportBlocked('export_input_changed')
            parent=self.control/'exports';parent.mkdir(mode=0o700,exist_ok=True);private_path(parent,True)
            temporary=Path(tempfile.mkdtemp(prefix='.capture-',dir=parent));(temporary/'receipts').mkdir(mode=0o700)
            entries={}
            for name,raw in captures.items():
                path=temporary/name
                fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
                entries[name]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
            manifest={'schema':'runner-export-proof-v1','run_id':run_identity['run_id'],'config_sha256':run_identity['config_sha256'],
                      'export_spec_sha256':run_identity['export_spec_sha256'],
                      'cutoff':status['time_utc'],'last_success':state['last_success'],'entries':entries,
                      'message_count':len(messages),'attachment_count':len(attachments),'excluded_historical_receipts':excluded,
                      'coverage_verified':True,'indexes':{k:{'sha256':v['sha256'],'bytes':v['bytes']} for k,v in index_meta.items()}}
            body=js(manifest).encode();proof_sha=hashlib.sha256(body).hexdigest()
            with (temporary/'manifest.json').open('xb') as out:out.write(body);out.flush();os.fsync(out.fileno())
            (temporary/'manifest.json').chmod(0o600)
            for directory in (temporary/'receipts',temporary):
                fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
            destination=parent/proof_sha
            if destination.exists():raise ExportBlocked('proof_identity_collision')
            os.rename(temporary,destination)
            fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
            # Validate every published byte once, then pin immutable file metadata
            # for bounded pre-checkpoint change detection. No recursive traversal.
            self.frozen_meta={}
            for name,item in entries.items():
                raw,meta=read_exact(destination/name,max(MAX_INDEX,mail_delta.MAX_RECEIPT))
                if len(raw)!=item['bytes'] or hashlib.sha256(raw).hexdigest()!=item['sha256']:
                    raise ExportBlocked('frozen_snapshot_changed')
                self.frozen_meta[name]=meta
            self.destination=destination;self.scopes=scopes;self.receipt_files=receipt_files;self.prepared=True
            self.manifest=manifest;self.proof_sha=proof_sha;self.messages=messages;self.attachments=attachments
            return {'schema':'runner-export-proof-v1','proof_sha256':proof_sha,'cutoff':status['time_utc'],
                    'coverage_verified':True,'snapshot_path':str(destination),'message_count':len(messages),
                    'attachment_count':len(attachments),'index_sha256':{k:v['sha256'] for k,v in index_meta.items()}}
    def folders(self):
        if not self.prepared:raise ExportBlocked('export_proof_not_prepared')
        return [Folder(name,valid) for name,valid in self.scopes.items()]
    def uids(self,scope,after_uid):return sorted(key[3] for key in self.receipt_files if key[1]==scope.name and key[2]==scope.uidvalidity and key[3]>after_uid)
    def receipt(self,scope,uid):
        path=self.destination/self.receipt_files[(self.account,scope.name,scope.uidvalidity,uid)]
        raw,_=read_exact(path,mail_delta.MAX_RECEIPT)
        if hashlib.sha256(raw).hexdigest()!=self.manifest['entries'][str(path.relative_to(self.destination))]['sha256']:raise ExportBlocked('frozen_receipt_changed')
        return str(path)
    def verify_preserved(self,value,scope,uid):
        for name,expected in self.frozen_meta.items():
            current=(self.destination/name).stat(follow_symlinks=False)
            observed=(current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns,current.st_ctime_ns)
            if observed!=expected:raise ExportBlocked('frozen_snapshot_changed')
        key=(self.account,scope.name,scope.uidvalidity,uid);name=self.receipt_files[key]
        manifest_raw,_=read_exact(self.destination/'manifest.json',1024*1024)
        if hashlib.sha256(manifest_raw).hexdigest()!=self.proof_sha:raise ExportBlocked('frozen_manifest_changed')
        raw,_=read_exact(self.destination/name,mail_delta.MAX_RECEIPT)
        expected=self.manifest['entries'][name]['sha256']
        docs={self.messages[key]}|{sha for akey,sha in self.attachments.items() if akey[:4]==key}
        return hashlib.sha256(raw).hexdigest()==expected==value.receipt_sha256 and value.eml_sha256==self.messages[key] and set(value.documents)==docs
