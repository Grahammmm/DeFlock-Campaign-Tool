"""Bounded, offline evidence capture. Binding verification is NOT review acceptance."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from . import store
from .migrations import receipt_projection_v001 as migration

VERSION = migration.VERSION
MAX_MANIFEST = 256 * 1024
MAX_INDEX = 8 * 1024 * 1024
MAX_SNAPSHOTS = 32 * 1024 * 1024
MAX_RECORD = 256 * 1024
MAX_ARTIFACT = 2 * 1024 * 1024
MAX_JOB_ARTIFACTS = 64 * 1024 * 1024
MAX_LINES = 10000
ROLES = {'artifact_path','artifact_hash','original_hash','original_hash_candidates','reviewed_content_hash'}
DECLARATIONS = {'reviewer','role','verdict','coverage','locators','challenges','supersedes','holds','created_at','status'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(v):
    return store.canonical(v)


def identity(*parts):
    return digest(canonical(parts).encode())


def strict_json(raw):
    def pairs(items):
        out = {}
        for k,v in items:
            if k in out: raise ValueError('duplicate JSON key')
            out[k]=v
        return out
    if isinstance(raw,bytes): raw=raw.decode('utf-8')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def is_hash(value):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def select(obj, field):
    for key in field.split('.'):
        if not isinstance(obj,dict) or key not in obj: return None
        obj=obj[key]
    return obj


def plan(raw, mapping):
    reasons=[]
    result={'original_hash':None,'artifact_hash':None,'artifact_path':None,
            'reviewed_content_hash':None,'artifact_source_locator':None,'declarations':{},'reasons':reasons}
    if len(raw)>MAX_RECORD:
        reasons.append('record_oversize'); return canonical(result)
    try:
        obj=strict_json(raw)
        if not isinstance(obj,dict): raise ValueError('not object')
    except (ValueError,UnicodeError,RecursionError):
        reasons.append('malformed_record'); return canonical(result)
    schema=strict_json(mapping)
    fields=schema['fields']
    for role in ('artifact_path','artifact_hash','reviewed_content_hash'):
        value=select(obj,fields[role]) if role in fields else None
        if value is not None:
            if role=='artifact_path' and isinstance(value,str) and value: result[role]=value
            elif role!='artifact_path' and is_hash(value): result[role]=value
            else: reasons.append('invalid_'+role)
    if result['artifact_path'] is not None:
        result['artifact_source_locator']=result['artifact_path']
        result['artifact_path']=schema.get('artifact_paths',{}).get(result['artifact_path'],result['artifact_path'])
    if 'original_hash' in fields and 'original_hash_candidates' in fields:
        reasons.append('ambiguous_original_hash_role')
    elif 'original_hash' in fields:
        value=select(obj,fields['original_hash'])
        if is_hash(value): result['original_hash']=value
        else: reasons.append('missing_or_invalid_original_hash')
    elif 'original_hash_candidates' in fields:
        values=select(obj,fields['original_hash_candidates'])
        if isinstance(values,list) and len(values)==1 and is_hash(values[0]): result['original_hash']=values[0]
        else: reasons.append('ambiguous_original_hash')
    else: reasons.append('missing_original_hash_role')
    for role,field in schema.get('declarations',{}).items():
        result['declarations'][role]=select(obj,field)
    if schema['kind']!='documents':
        if result['artifact_path'] is None: reasons.append('missing_artifact_path')
        if result['artifact_hash'] is None: reasons.append('missing_artifact_hash_role')
    return canonical(result)


def reasons(plan_json, artifact_sha, error, original_sha):
    p=strict_json(plan_json); result=list(p['reasons'])
    if error: result.append(error)
    if p['original_hash'] and original_sha is None: result.append('original_not_in_ledger')
    if artifact_sha and p['artifact_hash'] and artifact_sha!=p['artifact_hash']: result.append('artifact_hash_mismatch')
    if p['artifact_path'] and not p['artifact_hash']: result.append('artifact_hash_unproven')
    # Review declarations and reviewed-content identities remain unverified regardless of binding.
    return sorted(set(result))


def bound(p,a,e,o):
    value=strict_json(p)
    return int(bool(a and o and value['artifact_hash']==a and not reasons(p,a,e,o)))


def evidence(*values):
    values=list(values); values[4]=digest(values[4])
    return identity(*values)


def source_valid(sid,mid,ordinal,path,mapping,expected,body):
    try:
        spec=strict_json(body)['indexes'][ordinal]
        return int(sid==identity(mid,ordinal) and path==spec['path'] and expected==spec['sha256']
                   and mapping==canonical({'kind':spec['kind'],'fields':spec['fields'],'declarations':spec.get('declarations',{}),'artifact_paths':spec.get('artifact_paths',{})}))
    except (ValueError,IndexError,KeyError,TypeError): return 0


def line_at(body,number):
    if body is None:return None
    lines=body.splitlines(keepends=True)
    return lines[number-1] if 0<number<=len(lines) else None


def register(con):
    cache={}
    def captured_line(sid,number):
        if sid not in cache:
            source=con.execute('SELECT body FROM rp_sources WHERE id=?',(sid,)).fetchone()
            if not source or source[0] is None:return None
            if len(cache)>=16 or len(source[0])>MAX_INDEX:raise ValueError('SQL snapshot cache bound')
            cache[sid]=source[0].splitlines(keepends=True)
        lines=cache[sid]
        return lines[number-1] if 0<number<=len(lines) else None
    functions={'rp_sha':(1,digest),'rp_item_id':(2,identity),'rp_gap_id':(3,identity),
               'rp_source_valid':(7,source_valid),'rp_line':(2,captured_line),'rp_plan':(2,plan),
               'rp_evidence':(10,evidence),'rp_bound':(4,bound),
               'rp_reasons':(4,lambda *a:canonical(reasons(*a)))}
    for name,(arity,fn) in functions.items(): con.create_function(name,arity,fn,deterministic=True)


def install(con):
    register(con)
    checksum=digest(migration.SQL.encode())
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rp_schema'").fetchone():
        row=con.execute('SELECT version,checksum FROM rp_schema').fetchall()
        if [tuple(r) for r in row]!=[(VERSION,checksum)]: raise ValueError('sidecar schema identity mismatch')
    else:
        con.executescript('BEGIN IMMEDIATE;\n'+migration.SQL)
        con.execute('INSERT INTO rp_schema VALUES(?,?)',(VERSION,checksum)); con.commit()


def secure_fd(root,relative):
    p=Path(relative)
    if not isinstance(relative,str) or p.is_absolute() or not p.parts or any(x in ('..','.') for x in p.parts):
        raise ValueError('path_escape')
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for component in p.parts[:-1]:
            nxt=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd); fd=nxt
        return os.open(p.parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    finally: os.close(fd)


def capture(root,relative,limit):
    try:
        fd=secure_fd(root,relative)
        try:
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode): return None,'not_regular_file'
            if before.st_size>limit:return None,'oversize'
            with os.fdopen(fd,'rb',closefd=False) as stream: data=stream.read(limit+1)
            after=os.fstat(fd)
            other=secure_fd(root,relative)
            try: current=os.fstat(other)
            finally:os.close(other)
            signature=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
            if signature(before)!=signature(after) or signature(after)!=signature(current):return data,'changing_input'
            if len(data)>limit:return None,'oversize'
            return data,None
        finally:os.close(fd)
    except ValueError:return None,'path_escape'
    except FileNotFoundError:return None,'missing_file'
    except OSError:return None,'unsafe_or_unreadable_path'


def validate_manifest(raw):
    m=strict_json(raw)
    if not isinstance(m,dict) or m.get('version')!=1 or not isinstance(m.get('indexes'),list) or not 1<=len(m['indexes'])<=16:
        raise ValueError('invalid receipt manifest')
    for s in m['indexes']:
        if not isinstance(s,dict) or set(s)-{'path','sha256','kind','fields','declarations','artifact_paths','source_locator'}: raise ValueError('unknown manifest fields')
        if not isinstance(s.get('path'),str) or not is_hash(s.get('sha256')) or s.get('kind') not in ('documents','digests','reviews'):raise ValueError('invalid index spec')
        for field,allowed in (('fields',ROLES),('declarations',DECLARATIONS)):
            values=s.get(field,{})
            if not isinstance(values,dict) or set(values)-allowed or any(not isinstance(v,str) or not v or len(v)>256 for v in values.values()):raise ValueError('invalid explicit mapping')
        paths=s.get('artifact_paths',{})
        if not isinstance(paths,dict) or len(paths)>10000 or any(not isinstance(k,str) or not isinstance(v,str) or not k or not v or len(k)>4096 or len(v)>4096 for k,v in paths.items()):raise ValueError('invalid explicit artifact paths')
        if 'source_locator' in s and not isinstance(s['source_locator'],str):raise ValueError('invalid source locator')
        if 'fields' not in s:raise ValueError('missing fields mapping')
    return m


def before_commit():
    """Fault-injection seam for synthetic atomicity tests."""


def verify(con,mid):
    manifest=con.execute('SELECT * FROM rp_manifests WHERE id=?',(mid,)).fetchone()
    if not manifest or digest(manifest['body'])!=mid:raise ValueError('manifest mutation')
    spec=validate_manifest(manifest['body'])
    sources={r['id']:r for r in con.execute('SELECT * FROM rp_sources WHERE manifest_id=? ORDER BY ordinal',(mid,))}
    if len(sources)!=len(spec['indexes']):raise ValueError('source inventory mutation')
    if sum(len(r['body'] or b'') for r in sources.values())>MAX_SNAPSHOTS:raise ValueError('snapshot bound')
    lines={sid:(r['body'] or b'').splitlines(keepends=True) for sid,r in sources.items()}
    if sum(map(len,lines.values()))>MAX_LINES:raise ValueError('line bound')
    for source in sources.values():
        if not source_valid(source['id'],mid,source['ordinal'],source['path'],source['mapping'],source['expected_sha'],manifest['body']):raise ValueError('source mutation')
        if source['body'] is not None:
            if digest(source['body'])!=source['observed_sha'] or len(source['body'])!=source['size'] or len(source['body'])>MAX_INDEX:raise ValueError('source bytes mutation')
            if source['observed_sha']!=source['expected_sha'] and source['error']!='index_hash_mismatch':raise ValueError('source hash binding mutation')
            if source['observed_sha']==source['expected_sha'] and source['error'] not in (None,'changing_input'):raise ValueError('source status mutation')
        elif source['error'] not in ('missing_file','path_escape','unsafe_or_unreadable_path','not_regular_file','oversize','changing_input') or source['size'] is not None or source['observed_sha'] is not None:raise ValueError('source failure mutation')
    steps=con.execute('SELECT * FROM rp_steps WHERE manifest_id=? ORDER BY sequence',(mid,)).fetchall()
    for n,step in enumerate(steps,1):
        row=con.execute('SELECT * FROM rp_items WHERE id=?',(step['item_id'],)).fetchone()
        source=sources.get(row['source_id']) if row else None
        if row is None or source is None or step['sequence']!=n or row['manifest_id']!=mid or source['manifest_id']!=mid:raise ValueError('checkpoint mutation')
        if row['id']!=identity(source['id'],row['line']) or (row['line']>len(lines[source['id']]) or row['raw']!=lines[source['id']][row['line']-1]) or row['plan']!=plan(row['raw'],source['mapping']):raise ValueError('item source binding mutation')
        if row['artifact_sha']:
            art=con.execute('SELECT * FROM rp_artifacts WHERE sha=?',(row['artifact_sha'],)).fetchone()
            if not art or digest(art['body'])!=art['sha'] or len(art['body'])!=art['size']:raise ValueError('artifact mutation')
        p=strict_json(row['plan']); known=p['original_hash'] if con.execute('SELECT 1 FROM originals WHERE sha256=?',(p['original_hash'],)).fetchone() else None
        if known!=row['original_sha'] or row['binding_verified']!=bound(row['plan'],row['artifact_sha'],row['capture_error'],known):raise ValueError('original binding mutation')
        columns=('id','manifest_id','source_id','line','raw','plan','artifact_sha','capture_error','original_sha','binding_verified')
        if evidence(*(row[k] for k in columns))!=row['evidence_sha'] or step['evidence_sha']!=row['evidence_sha']:raise ValueError('evidence mutation')
        expected=sorted(reasons(row['plan'],row['artifact_sha'],row['capture_error'],known))
        gaps=con.execute('SELECT * FROM rp_gaps WHERE item_id=?',(row['id'],)).fetchall()
        if sorted(g['reason'] for g in gaps)!=expected or any(g['id']!=identity(source['id'],row['id'],g['reason']) or g['source_id']!=source['id'] or g['owner']!='caller' for g in gaps):raise ValueError('gap mutation')
    if con.execute('SELECT count(*) FROM rp_items WHERE manifest_id=?',(mid,)).fetchone()[0]!=len(steps):raise ValueError('orphan item without checkpoint')
    for source in con.execute('SELECT * FROM rp_sources WHERE manifest_id=?',(mid,)):
        gaps=con.execute('SELECT * FROM rp_gaps WHERE source_id=? AND item_id IS NULL',(source['id'],)).fetchall()
        expected=[source['error']] if source['error'] else []
        if [g['reason'] for g in gaps]!=expected or any(g['id']!=identity(source['id'],None,g['reason']) or g['owner']!='caller' for g in gaps):raise ValueError('source gap mutation')
    return len(steps)


def project(database,root,manifest,*,batch_size=100,max_batches=1):
    for v,maxval in ((batch_size,1000),(max_batches,100)):
        if type(v) is not int or not 1<=v<=maxval:raise ValueError('invalid bounded batch')
    root=store.checked_path(root,existing=False)
    if not root.is_dir() or root.stat().st_uid!=os.getuid() or root.stat().st_mode&0o077:raise ValueError('private caller root required')
    raw,error=capture(root,manifest,MAX_MANIFEST)
    if error:raise ValueError('manifest '+error)
    spec=validate_manifest(raw); mid=digest(raw)
    with store.ledger(database) as con:
        install(con)
        prior=con.execute('SELECT root FROM rp_manifests WHERE id=?',(mid,)).fetchone()
        if prior and prior[0]!=str(root):raise ValueError('manifest root changed')
        if not prior:
            con.execute('BEGIN IMMEDIATE')
            try:
                con.execute('INSERT INTO rp_manifests VALUES(?,?,?)',(mid,str(root),raw))
                total=0; lines=0
                for ordinal,s in enumerate(spec['indexes']):
                    sid=identity(mid,ordinal); body,err=capture(root,s['path'],min(MAX_INDEX,MAX_SNAPSHOTS-total))
                    sha=digest(body) if body is not None else None
                    if body is not None:
                        total+=len(body); lines+=len(body.splitlines())
                        if sha!=s['sha256']:err='index_hash_mismatch'
                    if lines>MAX_LINES:raise ValueError('manifest line bound exceeded')
                    mapping=canonical({'kind':s['kind'],'fields':s['fields'],'declarations':s.get('declarations',{}),'artifact_paths':s.get('artifact_paths',{})})
                    con.execute('INSERT INTO rp_sources VALUES(?,?,?,?,?,?,?,?,?,?)',(sid,mid,ordinal,s['path'],mapping,s['sha256'],sha,len(body) if body is not None else None,body,err))
                    if err:con.execute('INSERT INTO rp_gaps(id,source_id,item_id,reason) VALUES(?,?,NULL,?)',(identity(sid,None,err),sid,err))
                before_commit();con.commit()
            except BaseException:con.rollback();raise
        start=verify(con,mid)
        pending=[]
        for source in con.execute('SELECT * FROM rp_sources WHERE manifest_id=? AND error IS NULL ORDER BY ordinal',(mid,)):
            for line,record in enumerate(source['body'].splitlines(keepends=True),1):
                if not con.execute('SELECT 1 FROM rp_items WHERE source_id=? AND line=?',(source['id'],line)).fetchone():pending.append((source['id'],line,record,source['mapping']))
        for offset in range(0,min(len(pending),batch_size*max_batches),batch_size):
            con.execute('BEGIN IMMEDIATE')
            try:
                seq=con.execute('SELECT coalesce(max(sequence),0) FROM rp_steps WHERE manifest_id=?',(mid,)).fetchone()[0]
                budget=con.execute('SELECT coalesce(sum(size),0) FROM rp_artifacts').fetchone()[0]
                for sid,line,record,mapping in pending[offset:offset+batch_size]:
                    pjson=plan(record,mapping); p=strict_json(pjson); artsha=None;err=None
                    if p['artifact_path']:
                        data,err=capture(root,p['artifact_path'],min(MAX_ARTIFACT,max(0,MAX_JOB_ARTIFACTS-budget)))
                        if data is not None:
                            artsha=digest(data)
                            existing=con.execute('SELECT body,size FROM rp_artifacts WHERE sha=?',(artsha,)).fetchone()
                            if existing and (existing['body']!=data or existing['size']!=len(data)):raise ValueError('existing artifact mutation')
                            if not existing:
                                con.execute('INSERT INTO rp_artifacts VALUES(?,?,?)',(artsha,len(data),data));budget+=len(data)
                    original=p['original_hash'] if con.execute('SELECT 1 FROM originals WHERE sha256=?',(p['original_hash'],)).fetchone() else None
                    iid=identity(sid,line); verified=bound(pjson,artsha,err,original)
                    values=(iid,mid,sid,line,record,pjson,artsha,err,original,verified)
                    esha=evidence(*values)
                    con.execute('INSERT INTO rp_items VALUES(?,?,?,?,?,?,?,?,?,?,?)',values+(esha,))
                    for reason in reasons(pjson,artsha,err,original):
                        con.execute('INSERT INTO rp_gaps(id,source_id,item_id,reason) VALUES(?,?,?,?)',(identity(sid,iid,reason),sid,iid,reason))
                    seq+=1;con.execute('INSERT INTO rp_steps VALUES(?,?,?,?)',(mid,seq,iid,esha))
                before_commit();con.commit()
            except BaseException:con.rollback();raise
        processed=verify(con,mid)
        remaining=len(pending)-(processed-start)
        gaps=con.execute('SELECT count(*) FROM rp_gaps WHERE source_id IN (SELECT id FROM rp_sources WHERE manifest_id=?)',(mid,)).fetchone()[0]
        return {'adapter':VERSION,'manifest_id':mid,'processed':processed,'added':processed-start,
                'remaining':remaining,'gaps':gaps,'status':'in_progress' if remaining else 'captured_with_gaps' if gaps else 'captured',
                'binding_verified':con.execute('SELECT count(*) FROM rp_items WHERE manifest_id=? AND binding_verified=1',(mid,)).fetchone()[0],
                'stage_promotions':0,'review_accepted':False,'publication_ready':False,'end_to_end_complete':None}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',required=True);p.add_argument('--root',required=True)
    p.add_argument('--manifest',required=True,help='Relative manifest path under the private root')
    p.add_argument('--batch-size',type=int,default=100);p.add_argument('--max-batches',type=int,default=1)
    a=p.parse_args();print(canonical(project(a.database,a.root,a.manifest,batch_size=a.batch_size,max_batches=a.max_batches)))

if __name__=='__main__':main()
