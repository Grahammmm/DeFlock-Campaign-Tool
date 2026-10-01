"""Versioned canonical extraction enrollment. No stage acceptance is granted.

The WP1 store is a runtime dependency. Its implementation and authority registry
are neither copied nor altered here. Inputs and all derived artifacts are private.
"""
import hashlib
import importlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime,timezone
from pathlib import Path
from . import extraction_routes as routes
from .extract import ocr as page_ocr

VERSION="extraction-ledger-v1"
MAX_RECEIPT=16*1024*1024
MAX_DERIVED=64*1024*1024
MAX_UNIT=1024*1024
MAX_UNITS=10000
MAX_TOTAL=128*1024*1024
MAX_SOURCE=1024*1024*1024
PENDING_REASON="trusted_extraction_stage_adapter_unconfigured"
SCHEMA="""
CREATE TABLE extraction_adapter_schema(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL);
CREATE TABLE extraction_adapter_imports(
 id TEXT PRIMARY KEY, original_sha256 TEXT NOT NULL REFERENCES originals(sha256),
 receipt_sha256 TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, manifest_json TEXT NOT NULL,
 run_id TEXT NOT NULL REFERENCES runs(run_id), created_at TEXT NOT NULL,
 UNIQUE(original_sha256,receipt_sha256));
CREATE TABLE extraction_adapter_pages(
 import_id TEXT NOT NULL REFERENCES extraction_adapter_imports(id), page_no INTEGER NOT NULL,
 page_json TEXT NOT NULL, PRIMARY KEY(import_id,page_no));
CREATE TABLE extraction_adapter_current(
 original_sha256 TEXT PRIMARY KEY REFERENCES originals(sha256),
 import_id TEXT NOT NULL REFERENCES extraction_adapter_imports(id));
"""
for _table in ("extraction_adapter_imports","extraction_adapter_pages"):
    for _operation in ("UPDATE","DELETE"):
        SCHEMA+=(f"CREATE TRIGGER {_table}_{_operation.lower()} BEFORE {_operation} ON {_table} "
                 "BEGIN SELECT RAISE(ABORT,'extraction history is immutable'); END;\n")
SCHEMA+="""
CREATE TRIGGER extraction_import_no_replace BEFORE INSERT ON extraction_adapter_imports
 WHEN EXISTS(SELECT 1 FROM extraction_adapter_imports WHERE id=NEW.id OR
 (original_sha256=NEW.original_sha256 AND receipt_sha256=NEW.receipt_sha256))
 BEGIN SELECT RAISE(ABORT,'extraction history cannot be replaced'); END;
CREATE TRIGGER extraction_page_no_replace BEFORE INSERT ON extraction_adapter_pages
 WHEN EXISTS(SELECT 1 FROM extraction_adapter_pages WHERE import_id=NEW.import_id AND page_no=NEW.page_no)
 BEGIN SELECT RAISE(ABORT,'extraction page cannot be replaced'); END;
"""


class ExtractionBindingError(ValueError):
    """Fixed reason codes only; raw document text is never an error message."""


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def checked_sha(value):
    if not isinstance(value,str) or not re.fullmatch(r"[a-f0-9]{64}",value):
        raise ExtractionBindingError("invalid_hash")
    return value


def pairs(values):
    result={}
    for key,value in values:
        if key in result:raise ExtractionBindingError("duplicate_json_key")
        result[key]=value
    return result


def decode(raw):
    try:
        return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda x:(_ for _ in ()).throw(ExtractionBindingError("nonfinite_json")))
    except (UnicodeError,json.JSONDecodeError,RecursionError):
        raise ExtractionBindingError("invalid_json") from None


def checked_path(path):
    path=Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ExtractionBindingError("symlink_path")
    return path


def private_dir(path):
    path=checked_path(path)
    info=path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:
        raise ExtractionBindingError("private_directory_required")
    return path


def read_file(path,limit):
    path=checked_path(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid() or before.st_mode&0o077 or before.st_size>limit:
            raise ExtractionBindingError("artifact_permission_or_size")
        blocks=[];total=0
        while chunk:=os.read(fd,min(65536,limit-total+1)):
            total+=len(chunk)
            if total>limit:raise ExtractionBindingError("artifact_size_limit")
            blocks.append(chunk)
        after=os.fstat(fd)
        if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise ExtractionBindingError("artifact_changed")
        return b"".join(blocks)
    finally:os.close(fd)


def verify_original(path,expected,size):
    path=checked_path(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid() or before.st_mode&0o077 or before.st_size!=size or size>MAX_SOURCE:
            raise ExtractionBindingError("original_permission_or_size")
        digest=hashlib.sha256();total=0
        while chunk:=os.read(fd,65536):
            total+=len(chunk)
            if total>size:raise ExtractionBindingError("original_changed")
            digest.update(chunk)
        after=os.fstat(fd)
        if digest.hexdigest()!=expected or total!=size or (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise ExtractionBindingError("original_hash_or_change")
    finally:os.close(fd)


def store_blob(root,raw):
    digest=sha(raw);target=root/digest
    if not target.exists() and not target.is_symlink():
        fd,name=tempfile.mkstemp(prefix=".extraction-",dir=root)
        try:
            with os.fdopen(fd,"wb") as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            try:
                os.link(name,target);os.chmod(target,0o400)
                directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                try:os.fsync(directory)
                finally:os.close(directory)
            except FileExistsError:pass
        finally:Path(name).unlink(missing_ok=True)
    if sha(read_file(target,max(len(raw),1)))!=digest:
        raise ExtractionBindingError("evidence_cas_conflict")
    return digest,str(target)


def ensure_schema(con):
    found=con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='extraction_adapter_schema'").fetchone()
    digest=sha(SCHEMA.encode())
    if not found:
        con.executescript("BEGIN IMMEDIATE;\n"+SCHEMA)
        con.execute("INSERT INTO extraction_adapter_schema VALUES(?,?)",(1,digest));con.commit()
    else:
        rows=con.execute("SELECT version,checksum FROM extraction_adapter_schema").fetchall()
        if len(rows)!=1 or tuple(rows[0])!=(1,digest):raise ExtractionBindingError("adapter_schema_mismatch")


def _bind_ocr_receipts(receipt,run,subject,metadata,units,artifacts,gaps):
    """Bind per-page OCR receipts: verified receipt bytes, sidecar text == unit text,
    native units of every other page untouched, unchanged page denominator."""
    entries=receipt.get("ocr_receipts")
    if receipt.get("route",{}).get("route")!="extract:pdf" or not isinstance(entries,list) or not entries or len(entries)>MAX_UNITS:
        raise ExtractionBindingError("ocr_receipts_shape")
    if metadata.get("ocr_receipts")!=entries:raise ExtractionBindingError("ocr_receipts_metadata_mismatch")
    initial_raw=read_file(run/"derived"/"digest.json",MAX_RECEIPT);initial=decode(initial_raw)
    if not isinstance(initial,dict) or canonical(initial)!=canonical({k:v for k,v in metadata.items() if k!="ocr_receipts"}):
        raise ExtractionBindingError("ocr_pre_ocr_metadata_mismatch")
    native_raw=read_file(run/"derived"/"units.jsonl",MAX_DERIVED)
    native=[decode(line) for line in native_raw.splitlines()]
    if len(native)>MAX_UNITS or any(not isinstance(u,dict) for u in native):raise ExtractionBindingError("ocr_native_units_shape")
    artifacts.update(pre_ocr_metadata=initial_raw,native_units=native_raw)
    root=receipt.get("ocr_output_root")
    if not isinstance(root,str) or not os.path.isabs(root):raise ExtractionBindingError("ocr_output_root_unbound")
    private_dir(root)
    mapping={};texts={};sidecars={};unverified=False
    for entry in entries:
        if not isinstance(entry,dict) or set(entry)!={"page","receipt_id","receipt_sha256","status"}:raise ExtractionBindingError("ocr_receipt_entry_shape")
        page=entry["page"]
        if type(page) is not int or page<1 or page in mapping:raise ExtractionBindingError("ocr_receipt_entry_shape")
        try:loaded=page_ocr.load_page_receipt(root,subject,page,checked_sha(entry["receipt_id"]))
        except (ValueError,OSError):raise ExtractionBindingError("ocr_receipt_unverified") from None
        summary=routes.ocr_summary(loaded)
        if loaded["receipt_sha256"]!=entry["receipt_sha256"] or summary["status"]!=entry["status"]:
            raise ExtractionBindingError("ocr_receipt_hash_mismatch")
        versions=loaded["receipt"].get("tool_versions")
        if not isinstance(versions,dict) or not versions.get("tesseract"):unverified=True
        artifacts["ocr_receipt:"+entry["receipt_id"]]=loaded["receipt_bytes"]
        mapping[page]=summary
        if summary["status"] in routes.OCR_TEXT_STATES:
            sidecar=loaded["sidecar"]
            if sidecar is None:raise ExtractionBindingError("ocr_sidecar_missing")
            try:text=sidecar.decode("utf-8")
            except UnicodeError:raise ExtractionBindingError("ocr_sidecar_encoding") from None
            sidecars[page]=sidecar
            texts[page]=routes.ocr_unit(page,summary["receipt_id"],summary["receipt_sha256"],text)
    if canonical(routes.merge_ocr_units(native,texts))!=canonical(units):raise ExtractionBindingError("ocr_unit_merge_mismatch")
    for unit in units:
        data=unit.get("data") if isinstance(unit.get("data"),dict) else {}
        if "ocr_receipt_id" in data:
            page=unit["locator"].get("page")
            # The stored unit text bytes must be exactly the receipt's sidecar.txt bytes.
            if page not in sidecars or sha(unit["text"].encode("utf-8"))!=sha(sidecars[page]) or data["ocr_receipt_id"]!=mapping[page]["receipt_id"]:
                raise ExtractionBindingError("ocr_sidecar_hash_mismatch")
    gaps.append("ocr_visual_review_required")
    if unverified:gaps.append("ocr_runtime_version_unverified")
    return mapping


def validate_bundle(receipt_path,expected_receipt_sha,original_path,original):
    checked_sha(expected_receipt_sha)
    receipt_path=checked_path(receipt_path);run=private_dir(receipt_path.parent)
    raw_receipt=read_file(receipt_path,MAX_RECEIPT)
    if sha(raw_receipt)!=expected_receipt_sha:raise ExtractionBindingError("receipt_hash_mismatch")
    receipt=decode(raw_receipt)
    if not isinstance(receipt,dict) or receipt.get("schema")!="format-extraction-v1" or receipt.get("original_changed") is not False or receipt.get("review_status")!="not_reviewed":
        raise ExtractionBindingError("receipt_schema_or_review_claim")
    subject=checked_sha(receipt.get("original_sha256"))
    if subject!=original["sha256"] or Path(receipt.get("run_path",""))!=run or receipt_path.name!="extraction.json":
        raise ExtractionBindingError("source_receipt_unbound")
    if checked_path(original_path)!=checked_path(original["storage_path"]):
        raise ExtractionBindingError("canonical_source_path_unbound")
    verify_original(original_path,subject,original["bytes"])
    verify_original(run/"input",subject,original["bytes"])
    if receipt.get("parser_adapter")!=routes.VERSION or receipt.get("status") not in ("complete","partial","blocked"):
        raise ExtractionBindingError("unsupported_parser_receipt")
    units=receipt.get("units");pages=receipt.get("pages");issues=receipt.get("issues")
    if not isinstance(units,list) or len(units)>MAX_UNITS or not isinstance(pages,list) or len(pages)>MAX_UNITS or not isinstance(issues,list):
        raise ExtractionBindingError("invalid_units_or_pages")
    artifacts={"source_receipt":raw_receipt};unit_rows=[]
    if "ocr_derivative_sha256" in receipt or "ocr_derivative_path" in receipt:
        raise ExtractionBindingError("whole_document_ocr_unsupported")
    ocr="ocr_receipts" in receipt
    selected=run/("ocr-derived" if ocr else "derived")
    metadata_path=selected/"digest.json";units_path=selected/"units.jsonl"
    parser=receipt.get("parser");version=receipt.get("parser_version")
    gaps=[]
    if metadata_path.exists() or units or pages:
        private_dir(selected)
        raw_meta=read_file(metadata_path,MAX_RECEIPT);metadata=decode(raw_meta)
        if not isinstance(metadata,dict):raise ExtractionBindingError("parser_metadata_shape")
        raw_units=read_file(units_path,MAX_DERIVED)
        artifacts.update(parser_metadata=raw_meta,derived_units=raw_units)
        if any(not isinstance(value,str) or not value.strip() or value.lower() in ("unknown","unavailable","unverified") for value in (parser,version)):
            raise ExtractionBindingError("parser_provenance_unavailable")
        if metadata.get("parser")!=parser or metadata.get("parser_version")!=version or metadata.get("parser_components",{})!=receipt.get("parser_components",{}):
            raise ExtractionBindingError("parser_metadata_mismatch")
        lines=raw_units.splitlines(keepends=True)
        if len(lines)!=len(units):raise ExtractionBindingError("unit_denominator_mismatch")
        for ordinal,(unit,line) in enumerate(zip(units,lines),1):
            if len(line)>MAX_UNIT or not isinstance(unit,dict) or not isinstance(unit.get("locator"),dict) or not unit["locator"] or not isinstance(unit.get("kind"),str) or not unit["kind"] or not isinstance(unit.get("text"),str):
                raise ExtractionBindingError("unit_shape_or_size")
            if canonical(decode(line))!=canonical(unit):raise ExtractionBindingError("unit_receipt_mismatch")
            unit_rows.append({"ordinal":ordinal,"unit":unit,"raw":line,"text_sha256":sha(unit["text"].encode("utf-8"))})
        expected=metadata.get("counts",{}).get("pages_expected")
        route=receipt.get("route",{}).get("route")
        if route=="extract:pdf" and type(expected) is not int:raise ExtractionBindingError("pdf_page_denominator_missing")
        if ocr:
            mapping=_bind_ocr_receipts(receipt,run,subject,metadata,units,artifacts,gaps)
            computed=routes.page_manifest(units,expected,parser,version,ocr_receipts=mapping)
        else:
            computed=routes.page_manifest(units,expected,parser,version)
        if canonical(computed)!=canonical(pages):raise ExtractionBindingError("page_provenance_mismatch")
        if receipt["status"]=="complete" and any(p["status"]!="ok" for p in computed):raise ExtractionBindingError("false_complete_pages")
        for page in computed:
            if page["needs_visual_review"]:gaps.append("visual_review_pending")
    else:
        if receipt["status"]!="blocked" or units or pages or not issues:raise ExtractionBindingError("missing_parser_artifacts")
        gaps.append("extraction_route_blocked")
        parser=None;version=None
    children=receipt.get("children",[])
    if not isinstance(children,list) or len(children)>MAX_UNITS:raise ExtractionBindingError("children_bound")
    used=sum(map(len,artifacts.values()))+sum(len(x["raw"]) for x in unit_rows)
    if used>MAX_TOTAL:raise ExtractionBindingError("aggregate_evidence_bound")
    for child in children:
        if not isinstance(child,dict):raise ExtractionBindingError("child_shape")
        digest=checked_sha(child.get("sha"));size=child.get("bytes")
        if type(size) is not int or not 0<=size<=MAX_DERIVED or not isinstance(child.get("locator"),dict):raise ExtractionBindingError("child_shape")
        raw=read_file(run/"blobs"/digest,min(MAX_DERIVED,MAX_TOTAL-used))
        if "child:"+digest not in artifacts:used+=len(raw)
        if len(raw)!=size or sha(raw)!=digest:raise ExtractionBindingError("child_hash_mismatch")
        artifacts["child:"+digest]=raw
    if sum(map(len,artifacts.values()))+sum(len(x["raw"]) for x in unit_rows)>MAX_TOTAL:
        raise ExtractionBindingError("aggregate_evidence_bound")
    return receipt,artifacts,unit_rows,parser,version,sorted(set(gaps))


class ExtractionLedgerAdapter:
    def __init__(self,*,database,evidence_root,store_api=None,fault=None):
        self.database=Path(database)
        root=checked_path(evidence_root)
        root.mkdir(mode=0o700,parents=False,exist_ok=True)
        self.evidence_root=private_dir(root)
        try:self.store=store_api or importlib.import_module("campaign_tool.records.ledger.store")
        except ImportError:raise ExtractionBindingError("wp1_dependency_unavailable") from None
        self.fault=fault

    def enroll(self,*,original_path,receipt_path,receipt_sha256):
        # The canonical original must already have an authenticated intake identity.
        raw=read_file(receipt_path,MAX_RECEIPT)
        if sha(raw)!=checked_sha(receipt_sha256):raise ExtractionBindingError("receipt_hash_mismatch")
        decoded=decode(raw)
        if not isinstance(decoded,dict):raise ExtractionBindingError("receipt_shape")
        subject=checked_sha(decoded.get("original_sha256"))
        with self.store.ledger(self.database,readonly=True) as con:
            row=con.execute("SELECT * FROM originals WHERE sha256=?",(subject,)).fetchone()
            if row is None or not row["storage_path"]:raise ExtractionBindingError("canonical_original_unavailable")
            original=dict(row)
            if not con.execute("SELECT 1 FROM occurrences WHERE original_sha256=?",(subject,)).fetchone():raise ExtractionBindingError("canonical_occurrence_missing")
        receipt,artifacts,rows,parser,version,gaps=validate_bundle(receipt_path,receipt_sha256,original_path,original)
        import_id=sha(canonical([VERSION,subject,receipt_sha256]).encode())
        run_id="extraction-enroll:"+import_id
        stored={name:{"sha256":digest,"path":path,"bytes":len(value)} for name,value in artifacts.items() for digest,path in [store_blob(self.evidence_root,value)]}
        planned=[]
        for row in rows:
            digest,path=store_blob(self.evidence_root,row["raw"])
            unit=row["unit"];ordinal=row["ordinal"]
            provenance=canonical({"extraction_import_id":import_id,"receipt_sha256":receipt_sha256,"payload_sha256":digest,"ordinal":ordinal,"source_units_sha256":stored["derived_units"]["sha256"],"parser_provenance":"bound_to_derivative_metadata","parser_components":receipt.get("parser_components",{}),"ocr_receipt_ids":[entry["receipt_id"] for entry in receipt.get("ocr_receipts",[])],"review_status":"not_reviewed"})
            planned.append((sha(canonical([import_id,ordinal,digest]).encode()),subject,parser,version,canonical(unit["locator"]),unit["kind"],row["text_sha256"],path,"candidate_extracted",ordinal,provenance))
        manifest={"adapter":VERSION,"adapter_code_sha256":sha(Path(__file__).read_bytes()),"original_sha256":subject,"original_bytes":original["bytes"],"receipt_sha256":receipt_sha256,"artifacts":stored,"unit_ids":[r[0] for r in planned],"pages":receipt["pages"],"parser":parser,"parser_version":version,"source_status":receipt["status"],"issues":receipt["issues"],"gaps":gaps,"stage_promotions":0}
        manifest_json=canonical(manifest);manifest_sha=sha(manifest_json.encode())
        store_blob(self.evidence_root,manifest_json.encode())
        if self.fault:self.fault("after_evidence_bytes")
        stamp=datetime.now(timezone.utc).isoformat()
        reused=False;selection="held_existing_stage"
        with self.store.ledger(self.database) as con:
            ensure_schema(con)
            con.execute("BEGIN IMMEDIATE")
            try:
                current_original=con.execute("SELECT * FROM originals WHERE sha256=?",(subject,)).fetchone()
                if dict(current_original)!=original:raise ExtractionBindingError("canonical_original_changed")
                stages=con.execute("SELECT * FROM stage_state WHERE original_sha256=?",(subject,)).fetchall()
                if {r["stage"] for r in stages}!=set(self.store.STAGES):raise ExtractionBindingError("stage_slots_missing")
                prior=con.execute("SELECT * FROM extraction_adapter_imports WHERE id=?",(import_id,)).fetchone()
                if prior:
                    if prior["manifest_sha256"]!=manifest_sha or prior["manifest_json"]!=manifest_json or prior["run_id"]!=run_id:raise ExtractionBindingError("import_evidence_changed")
                    existing=con.execute("SELECT * FROM runs WHERE run_id=?",(run_id,)).fetchone()
                    if not existing or existing["summary"]!=manifest_json or existing["status"]!="pending_stage_validation":raise ExtractionBindingError("canonical_run_changed")
                    for row in planned:
                        old=con.execute("SELECT id,original_sha256,parser,parser_version,locator,unit_type,text_sha256,derived_path,status,legacy_ordinal,provenance_json FROM units WHERE id=?",(row[0],)).fetchone()
                        if old is None or tuple(old)!=row:raise ExtractionBindingError("canonical_unit_changed")
                    pages=[decode(r[0]) for r in con.execute("SELECT page_json FROM extraction_adapter_pages WHERE import_id=? ORDER BY page_no",(import_id,))]
                    if canonical(pages)!=canonical(receipt["pages"]):raise ExtractionBindingError("canonical_page_history_changed")
                    reused=True
                else:
                    con.execute("INSERT INTO runs(run_id,kind,started_at,ended_at,engine_version,image_digest,config_sha256,coverage_cutoff,status,summary) VALUES(?,?,?,?,?,?,?,?,?,?)",(run_id,"extraction_enrollment",stamp,stamp,VERSION,None,None,None,"pending_stage_validation",manifest_json))
                    con.execute("INSERT INTO extraction_adapter_imports VALUES(?,?,?,?,?,?,?)",(import_id,subject,receipt_sha256,manifest_sha,manifest_json,run_id,stamp))
                    con.executemany("INSERT INTO units(id,original_sha256,parser,parser_version,locator,unit_type,text_sha256,derived_path,status,legacy_ordinal,provenance_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",planned)
                    con.executemany("INSERT INTO extraction_adapter_pages VALUES(?,?,?)",[(import_id,page["page_no"],canonical(page)) for page in receipt["pages"]])
                    safe=all(r["status"]=="pending" for r in stages if r["stage"]!="preserve")
                    owned=con.execute("SELECT import_id FROM extraction_adapter_current WHERE original_sha256=?",(subject,)).fetchone()
                    prior_pages=con.execute("SELECT 1 FROM page_state WHERE original_sha256=? LIMIT 1",(subject,)).fetchone()
                    if prior_pages and not owned:
                        safe=False
                        selection="held_unbound_existing_pages"
                    if safe:
                        con.execute("INSERT INTO extraction_adapter_current VALUES(?,?) ON CONFLICT(original_sha256) DO UPDATE SET import_id=excluded.import_id",(subject,import_id))
                        con.execute("DELETE FROM page_state WHERE original_sha256=?",(subject,))
                        for page in receipt["pages"]:
                            con.execute("INSERT INTO page_state(original_sha256,page_no,method,method_version,derivative_sha256,confidence,needs_visual_review,status,reason) VALUES(?,?,?,?,?,?,?,?,?)",(subject,page["page_no"],page["method"],page["method_version"],page["derivative_sha256"],page["confidence"],int(page["needs_visual_review"]),page["status"],page["reason"]))
                        extract=next(r for r in stages if r["stage"]=="extract")
                        if extract["reason"]!=PENDING_REASON and not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage_content'").fetchone():
                            con.execute("UPDATE stage_state SET reason=?,updated_at=?,run_id=? WHERE original_sha256=? AND stage='extract' AND status='pending'",(PENDING_REASON,stamp,run_id,subject))
                pointer=con.execute("SELECT import_id FROM extraction_adapter_current WHERE original_sha256=?",(subject,)).fetchone()
                if pointer:
                    selection="current_pending_candidate" if pointer[0]==import_id else "historical_candidate"
                    current=con.execute("SELECT page_json FROM extraction_adapter_pages WHERE import_id=? ORDER BY page_no",(pointer[0],)).fetchall()
                    expected_pages=[(subject,p["page_no"],p["method"],p["method_version"],p["derivative_sha256"],p["confidence"],int(p["needs_visual_review"]),p["status"],p["reason"]) for p in [decode(r[0]) for r in current]]
                    actual=[tuple(r) for r in con.execute("SELECT original_sha256,page_no,method,method_version,derivative_sha256,confidence,needs_visual_review,status,reason FROM page_state WHERE original_sha256=? ORDER BY page_no",(subject,))]
                    if actual!=expected_pages:raise ExtractionBindingError("canonical_current_pages_changed")
                if self.fault:self.fault("before_canonical_commit")
                con.commit()
            except BaseException:con.rollback();raise
        if self.fault:self.fault("after_canonical_commit")
        return {"interface_version":VERSION,"import_id":import_id,"run_id":run_id,"original_sha256":subject,"receipt_sha256":receipt_sha256,"manifest_sha256":manifest_sha,"units":len(rows),"pages":len(receipt["pages"]),"selection":selection,"reused":reused,"stage_promotions":0,"extract_acceptance":"pending_trusted_validator","reason":PENDING_REASON,"gaps":gaps,"review_promotions":0}

    def validate_and_promote(self,*args,**kwargs):
        raise ExtractionBindingError(PENDING_REASON)
