"""Read-only snapshot-bound current extraction units and disposable FTS5 search."""
import hashlib,json,os,re,sqlite3,stat
from pathlib import Path
MAX_DB=8*1024**3
MAX_UNIT=8*1024**2
MAX_UNITS=10000
MAX_TOTAL=128*1024**2
MAX_SNAPSHOT_ROWS=200000
MAX_SNAPSHOT_BYTES=128*1024**2
HEX=re.compile(r"[0-9a-f]{64}\Z")
class RetainedUnitError(ValueError):
    def __init__(self,code): self.code=code;super().__init__(code)
def _no(code): raise RetainedUnitError(code)
def _sha(b): return hashlib.sha256(b).hexdigest()
def _canon(v): return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()
def _json(raw,code="unit_json_invalid"):
    def pairs(items):
        d={}
        for k,v in items:
            if k in d:_no("duplicate_json_key")
            d[k]=v
        return d
    try:return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _: _no(code))
    except RetainedUnitError:raise
    except (ValueError,TypeError,UnicodeError,RecursionError):_no(code)
def _path(value):
    try:s=os.fspath(value)
    except TypeError:_no("path_invalid")
    if type(s)is not str or not os.path.isabs(s) or ".." in Path(s).parts:_no("path_invalid")
    p=Path(os.path.normpath(s))
    for q in (p,*p.parents):
        try:
            if stat.S_ISLNK(q.lstat().st_mode):_no("symlink_path")
        except FileNotFoundError:pass
    return p
def _outside_git(p):
    p=_path(p)
    for q in (p,*p.parents):
        if (q/".git").exists() or (q/".git").is_symlink():_no("path_inside_git")
    return p
def _private(p):
    p=_outside_git(p)
    try:s=p.stat()
    except OSError:_no("directory_unavailable")
    if not stat.S_ISDIR(s.st_mode) or s.st_uid!=os.geteuid() or stat.S_IMODE(s.st_mode)&0o077:_no("private_directory_required")
    return p,(s.st_dev,s.st_ino,s.st_uid,stat.S_IMODE(s.st_mode))
def _read(p,limit):
    p=_path(p)
    try:fd=os.open(p,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_NONBLOCK",0))
    except OSError:_no("artifact_unavailable")
    try:
        a=os.fstat(fd)
        if not stat.S_ISREG(a.st_mode) or a.st_uid!=os.geteuid() or stat.S_IMODE(a.st_mode)&0o077 or a.st_size>limit:_no("artifact_permissions_or_size")
        parts=[];n=0
        while True:
            b=os.read(fd,min(65536,limit-n+1))
            if not b:break
            n+=len(b)
            if n>limit:_no("artifact_size_limit")
            parts.append(b)
        z=os.fstat(fd)
        if (a.st_dev,a.st_ino,a.st_size,a.st_mtime_ns,a.st_ctime_ns)!=(z.st_dev,z.st_ino,z.st_size,z.st_mtime_ns,z.st_ctime_ns):_no("artifact_changed")
        return b"".join(parts)
    finally:os.close(fd)
def _value(v):
    if v is None:return ["n",None]
    if isinstance(v,bytes):return ["b",v.hex()]
    if type(v)is int:return ["i",v]
    if type(v)is float:return ["f",v.hex()]
    if isinstance(v,str):return ["s",v]
    _no("snapshot_value_unsupported")
def ledger_snapshot(c):
    schema=[{"type":r[0],"name":r[1],"tbl_name":r[2],"sql":r[3]} for r in c.execute("select type,name,tbl_name,sql from sqlite_master order by type,name,tbl_name") if not r[1].startswith("sqlite_")]
    state={"schema":schema,"version":c.execute("pragma user_version").fetchone()[0],"tables":{}}
    names=[r[0] for r in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
    total_rows=0;total_bytes=0
    for t in names:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",t):_no("snapshot_schema_unknown")
        cols=c.execute('select cid,name,type,"notnull",dflt_value,pk from pragma_table_info(?) order by cid',(t,)).fetchall()
        if not cols:_no("snapshot_schema_unknown")
        q=",".join('"'+x[1].replace('"','""')+'"' for x in cols)
        rows=[]
        for r in c.execute('select '+q+' from "'+t+'"'):
            encoded=[_value(v) for v in r];rows.append(encoded)
            total_rows+=1;total_bytes+=len(_canon(encoded))
            if total_rows>MAX_SNAPSHOT_ROWS or total_bytes>MAX_SNAPSHOT_BYTES:_no("snapshot_bound")
        rows.sort(key=_canon);state["tables"][t]={"columns":[list(x) for x in cols],"rows":rows}
    return _sha(_canon(state))
def _connect(db,expected):
    p=_path(db)
    try:s=p.stat()
    except OSError:_no("database_unavailable")
    if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.geteuid() or stat.S_IMODE(s.st_mode)&0o077 or s.st_size>MAX_DB:_no("database_permissions_or_size")
    c=None
    try:
        c=sqlite3.connect("file:"+str(p)+"?mode=ro",uri=True,timeout=2)
        c.row_factory=sqlite3.Row;c.execute("pragma query_only=on");c.execute("begin")
        if ledger_snapshot(c)!=expected:_no("ledger_snapshot_stale")
        return c,p,(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    except RetainedUnitError:
        if c:c.close()
        raise
    except (sqlite3.Error,OSError):
        if c:c.close()
        _no("ledger_read_failed")
def _scope(c,imports):
    if type(imports)is not list or not imports or len(imports)>256:_no("import_scope_invalid")
    subjects=set();result=[];requested=0
    for x in imports:
        if type(x)is not dict or set(x)!={"original_sha256","import_id","manifest_sha256","unit_ids"}:_no("import_scope_invalid")
        subject,imp,msha,ids=(x[k] for k in ("original_sha256","import_id","manifest_sha256","unit_ids"))
        if any(type(v)is not str or not HEX.fullmatch(v) for v in (subject,imp,msha)) or subject in subjects:_no("import_scope_invalid")
        if type(ids)is not list or not ids or len(ids)>MAX_UNITS or len(set(ids))!=len(ids) or any(type(v)is not str or not HEX.fullmatch(v) for v in ids):_no("import_scope_invalid")
        requested+=len(ids)
        if requested>MAX_UNITS:_no("unit_count_limit")
        subjects.add(subject)
        ptr=c.execute("select import_id from extraction_adapter_current where original_sha256=?",(subject,)).fetchone()
        if ptr is None or ptr[0]!=imp:_no("current_import_mismatch")
        imported=c.execute("select * from extraction_adapter_imports where id=?",(imp,)).fetchone()
        if imported is None or imported["original_sha256"]!=subject or imported["manifest_sha256"]!=msha:_no("import_binding_mismatch")
        mr=imported["manifest_json"]
        if type(mr)is not str or _sha(mr.encode())!=msha:_no("manifest_hash_mismatch")
        manifest=_json(mr.encode(),"manifest_invalid")
        if type(manifest)is not dict or manifest.get("unit_ids")!=ids:_no("manifest_membership_mismatch")
        rows=c.execute("select * from units where original_sha256=? and json_valid(provenance_json) and json_extract(provenance_json,'$.extraction_import_id')=? order by legacy_ordinal,id limit ?",(subject,imp,len(ids)+1)).fetchall()
        if len(rows)!=len(ids) or {r["id"] for r in rows}!=set(ids):_no("current_unit_membership_mismatch")
        result.extend((subject,imp,manifest,r) for r in rows)
    if len(result)>MAX_UNITS:_no("unit_count_limit")
    return result
def read_current_units(database,evidence_root,expected_snapshot,expected_imports,*,max_units=MAX_UNITS,max_total_bytes=MAX_TOTAL):
    """Read declared current imports only; no original reader or ledger mutation.

    expected_imports entries: original_sha256, import_id, manifest_sha256, unit_ids.
    """
    if type(expected_snapshot)is not str or not HEX.fullmatch(expected_snapshot):_no("snapshot_pin_invalid")
    if type(max_units)is not int or not 1<=max_units<=MAX_UNITS or type(max_total_bytes)is not int or not 1<=max_total_bytes<=MAX_TOTAL:_no("reader_bound_invalid")
    root,rid=_private(evidence_root);c,db,did=_connect(database,expected_snapshot);units=[];total=0
    try:
        planned=_scope(c,expected_imports)
        if len(planned)>max_units:_no("unit_count_limit")
        for subject,imp,manifest,row in planned:
            ordinal=row["legacy_ordinal"]
            if row["status"]!="candidate_extracted" or type(ordinal)is not int or ordinal<1 or ordinal>len(manifest["unit_ids"]):_no("unit_metadata_invalid")
            if row["id"]!=manifest["unit_ids"][ordinal-1]:_no("unit_ordinal_mismatch")
            prov=_json(row["provenance_json"].encode(),"unit_provenance_invalid")
            digest=prov.get("payload_sha256")
            expected_path=root/digest if type(digest)is str and HEX.fullmatch(digest) else None
            if (prov.get("extraction_import_id")!=imp or prov.get("ordinal")!=ordinal or prov.get("source_units_sha256")!=manifest.get("artifacts",{}).get("derived_units",{}).get("sha256")
                or expected_path is None or row["derived_path"]!=str(expected_path)):_no("unit_provenance_mismatch")
            locator_text=row["locator"]
            if type(locator_text)is not str or not locator_text:_no("unit_locator_invalid")
            try:locator_size=len(locator_text.encode("utf-8"))
            except UnicodeError:_no("unit_locator_invalid")
            if locator_size>16384:_no("unit_locator_invalid")
            locator=_json(locator_text.encode(),"unit_locator_invalid")
            if _canon(locator).decode()!=locator_text:_no("unit_locator_noncanonical")
            artifact=_path(row["derived_path"])
            if artifact!=root/digest:_no("unit_artifact_outside_root")
            raw=_read(artifact,MAX_UNIT);total+=len(raw)
            if total>max_total_bytes:_no("unit_total_bytes_limit")
            if _sha(raw)!=digest:_no("unit_payload_hash_mismatch")
            item=_json(raw);text=item.get("text") if type(item)is dict else None
            if type(text)is not str or type(item.get("data"))is not dict or item.get("locator")!=locator:_no("unit_payload_shape_invalid")
            try:th=_sha(text.encode("utf-8"))
            except UnicodeError:_no("unit_payload_shape_invalid")
            if row["text_sha256"]!=th:_no("unit_text_hash_mismatch")
            if row["id"]!=_sha(_canon([imp,ordinal,digest])):_no("unit_identity_mismatch")
            units.append({"unit_id":row["id"],"original_sha256":subject,"import_id":imp,"ordinal":ordinal,"locator":locator,"locator_text":locator_text,
                "text":text,"data":item["data"],"artifact_sha256":digest,"text_sha256":th,"status":row["status"],"unit_type":row["unit_type"],
                "quality_status":prov.get("quality_status"),"review_status":prov.get("review_status"),"semantic_acceptance":prov.get("semantic_acceptance",False)})
        if len(units)!=sum(len(x["unit_ids"]) for x in expected_imports):_no("unit_count_mismatch")
        if _private(root)[1]!=rid:_no("artifact_root_changed")
        now=db.stat()
        if (now.st_dev,now.st_ino,now.st_size,now.st_mtime_ns,now.st_ctime_ns)!=did:_no("ledger_changed_during_read")
        if ledger_snapshot(c)!=expected_snapshot:_no("ledger_changed_during_read")
        # A second read transaction detects a commit that landed after our pinned snapshot began.
        fresh=sqlite3.connect("file:"+str(db)+"?mode=ro",uri=True,timeout=2)
        try:
            if ledger_snapshot(fresh)!=expected_snapshot:_no("ledger_changed_during_read")
        finally:fresh.close()
        return {"snapshot_sha256":expected_snapshot,"units":units,"unit_count":len(units),"artifact_bytes":total}
    except sqlite3.Error:_no("ledger_read_failed")
    finally:c.close()
def build_search_index(index_dir,read_result):
    """Build a replaceable private FTS5 index from a validated reader result."""
    if type(read_result)is not dict or set(read_result)!={"snapshot_sha256","units","unit_count","artifact_bytes"}:_no("search_input_invalid")
    snapshot_sha256=read_result["snapshot_sha256"];units=read_result["units"]
    if type(snapshot_sha256)is not str or not HEX.fullmatch(snapshot_sha256) or type(units)is not list or not units or len(units)>MAX_UNITS or read_result["unit_count"]!=len(units):_no("search_input_invalid")
    if type(read_result["artifact_bytes"])is not int or not 0<=read_result["artifact_bytes"]<=MAX_TOTAL:_no("search_input_invalid")
    rows=[];size=0;seen=set()
    for u in units:
        if type(u)is not dict or any(type(u.get(k))is not str for k in ("unit_id","original_sha256","import_id","artifact_sha256","text_sha256","locator_text","text")) or type(u.get("ordinal"))is not int or type(u.get("locator"))is not dict or type(u.get("data"))is not dict:_no("search_input_invalid")
        if any(not HEX.fullmatch(u[k]) for k in ("unit_id","original_sha256","import_id","artifact_sha256","text_sha256")) or u["ordinal"]<1:_no("search_input_invalid")
        try:
            text_bytes=u["text"].encode("utf-8")
            locator_bytes=_canon(u["locator"])
        except (UnicodeError,ValueError):_no("search_input_invalid")
        if _sha(text_bytes)!=u["text_sha256"]:_no("search_text_hash_mismatch")
        if locator_bytes.decode("utf-8")!=u["locator_text"]:_no("search_locator_mismatch")
        if _sha(_canon([u["import_id"],u["ordinal"],u["artifact_sha256"]]))!=u["unit_id"]:_no("search_unit_identity_mismatch")
        if u["unit_id"] in seen:_no("search_input_invalid")
        seen.add(u["unit_id"]);size+=len(text_bytes)
        if size>MAX_TOTAL:_no("unit_total_bytes_limit")
        rows.append((u["unit_id"],u["original_sha256"],u["locator_text"],u["text"]))
    rows.sort(key=lambda r:r[0])
    root,rid=_private(index_dir);p=root/("retained-search-"+snapshot_sha256+".sqlite")
    if p.exists() or p.is_symlink():_read(p,MAX_TOTAL);p.unlink()
    c=None
    try:
        c=sqlite3.connect(p);c.execute("pragma journal_mode=delete")
        c.execute("create virtual table units_fts using fts5(unit_id unindexed,original_sha256 unindexed,locator unindexed,text,tokenize='unicode61')")
        c.execute("create table index_binding(snapshot_sha256 text not null,unit_count integer not null,content_sha256 text not null)")
        c.executemany("insert into units_fts values(?,?,?,?)",rows);content=_sha(_canon(rows))
        c.execute("insert into index_binding values(?,?,?)",(snapshot_sha256,len(rows),content));c.commit();c.close();c=None
        os.chmod(p,0o600)
        s=p.stat()
        if s.st_uid!=os.geteuid() or stat.S_IMODE(s.st_mode)&0o077 or _private(root)[1]!=rid:_no("index_permissions")
        return {"path":str(p),"snapshot_sha256":snapshot_sha256,"unit_count":len(rows),"content_sha256":content}
    except RetainedUnitError:
        if c:c.close()
        try:p.unlink()
        except OSError:pass
        raise
    except (sqlite3.Error,OSError):
        if c:c.close()
        try:p.unlink()
        except OSError:pass
        _no("search_index_build_failed")
def search_index(index_path,expected_snapshot,query,*,expected_content_sha256,limit=50):
    if type(expected_snapshot)is not str or not HEX.fullmatch(expected_snapshot):_no("snapshot_pin_invalid")
    if type(expected_content_sha256)is not str or not HEX.fullmatch(expected_content_sha256):_no("index_content_pin_invalid")
    if type(query)is not str or not query.strip() or len(query.encode())>1024 or type(limit)is not int or not 1<=limit<=200:_no("search_query_invalid")
    p=_path(index_path)
    _private(p.parent)
    if p.name!="retained-search-"+expected_snapshot+".sqlite":_no("index_binding_mismatch")
    try:s=p.stat()
    except OSError:_no("search_index_unavailable")
    if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.geteuid() or stat.S_IMODE(s.st_mode)&0o077 or s.st_size>MAX_TOTAL:_no("index_permissions_or_size")
    c=None
    try:
        c=sqlite3.connect("file:"+str(p)+"?mode=ro",uri=True,timeout=2);c.execute("pragma query_only=on");c.execute("begin")
        binding=c.execute("select snapshot_sha256,unit_count,content_sha256 from index_binding").fetchone()
        if binding is None or binding[0]!=expected_snapshot or binding[2]!=expected_content_sha256 or type(binding[1]) is not int or not 0<binding[1]<=MAX_UNITS:_no("index_content_pin_mismatch")
        all_rows=c.execute("select unit_id,original_sha256,locator,text from units_fts order by unit_id limit ?",(MAX_UNITS+1,)).fetchall()
        encoded_rows=[tuple(r) for r in all_rows]
        try:encoded_size=sum(len(r[3].encode("utf-8")) for r in encoded_rows)
        except UnicodeError:_no("index_content_mismatch")
        if len(encoded_rows)!=binding[1] or len(encoded_rows)>MAX_UNITS or encoded_size>MAX_TOTAL or _sha(_canon(encoded_rows))!=expected_content_sha256:_no("index_content_mismatch")
        q='"'+query.strip().replace('"','""')+'"'
        rows=c.execute("select unit_id,original_sha256,locator,text from units_fts where units_fts match ? order by bm25(units_fts),unit_id limit ?",(q,limit)).fetchall()
        return [{"unit_id":r[0],"original_sha256":r[1],"locator":r[2],"text":r[3]} for r in rows]
    except RetainedUnitError:raise
    except (sqlite3.Error,OSError):_no("search_index_unavailable")
    finally:
        if c:c.close()

